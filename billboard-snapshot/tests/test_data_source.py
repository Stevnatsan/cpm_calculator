"""
Run from billboard-snapshot/:
    pip install -r requirements.txt pytest httpx
    python -m pytest tests

No BigQuery access is needed: live data is faked.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import data_source  # noqa: E402
from data_source import DataProvider, DataStore, SchemaError  # noqa: E402

SNAPSHOT_ROWS = json.loads((ROOT / "app" / "data" / "snapshot.json").read_text())["rows"]


def fake_rows(n=3, city="Test City"):
    base = dict(SNAPSHOT_ROWS[0])
    rows = []
    for i in range(n):
        r = dict(base, inventory_id=900000 + i, city_name=city, monthly_price=1_000_000.0 * (i + 1))
        r["cpm_calculated"] = r["monthly_price"] / (r["monthly_impression"] / 1000)
        rows.append(r)
    return rows


# ------------------------------------------------------------------ loader


def test_snapshot_matches_the_app_columns():
    data_source.check_columns(SNAPSHOT_ROWS)


def test_old_script_columns_are_rejected():
    # The columns the previous generate_snapshot.py wrote.
    row = {k: v for k, v in SNAPSHOT_ROWS[0].items() if k not in ("inventory_id", "inventory_name", "inventory_address")}
    row.update(billboard_name="x", address="y")
    with pytest.raises(SchemaError) as e:
        DataStore([row], "now", "bigquery")
    assert "inventory_id" in str(e.value)


def test_query_selects_every_column_the_app_reads():
    import queries

    select = queries.SNAPSHOT_SQL.split("FROM (")[0]
    for col in data_source.REQUIRED_COLUMNS:
        assert col in select, col


# ------------------------------------------------------------------ provider


def make_provider(monkeypatch, fetch, **env):
    for k in ("DATA_SOURCE", "GOOGLE_APPLICATION_CREDENTIALS_JSON", "GOOGLE_APPLICATION_CREDENTIALS"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return DataProvider(fetch_live=fetch)


def test_default_is_snapshot_and_never_queries(monkeypatch):
    def boom():
        raise AssertionError("should not query")

    p = make_provider(monkeypatch, boom)
    assert p.current().source == "snapshot"
    assert p.current().note is None


def test_bigquery_without_credentials_stays_on_snapshot(monkeypatch):
    p = make_provider(monkeypatch, lambda: fake_rows(), DATA_SOURCE="bigquery")
    d = p.current()
    assert d.source == "snapshot" and "credential" in d.note


def test_bigquery_is_used_and_cached(monkeypatch):
    calls = []

    def fetch():
        calls.append(1)
        return fake_rows()

    p = make_provider(monkeypatch, fetch, DATA_SOURCE="bigquery", GOOGLE_APPLICATION_CREDENTIALS_JSON="{}")
    assert p.current().source == "bigquery"
    assert len(p.current().billboards) == 3
    assert len(calls) == 1  # second call served from cache


def test_failed_query_falls_back_to_snapshot(monkeypatch):
    def fetch():
        raise RuntimeError("permission denied")

    p = make_provider(monkeypatch, fetch, DATA_SOURCE="bigquery", GOOGLE_APPLICATION_CREDENTIALS_JSON="{}")
    d = p.current()
    assert d.source == "snapshot" and d.note
    assert "permission denied" in p.status()["last_error"]


def test_failed_refresh_keeps_last_live_data(monkeypatch):
    state = {"fail": False}

    def fetch():
        if state["fail"]:
            raise RuntimeError("timeout")
        return fake_rows()

    p = make_provider(
        monkeypatch, fetch, DATA_SOURCE="bigquery", GOOGLE_APPLICATION_CREDENTIALS_JSON="{}", DATA_CACHE_SECONDS="0"
    )
    assert p.current().source == "bigquery"
    state["fail"] = True
    d = p.current()
    assert d.source == "bigquery" and len(d.billboards) == 3 and d.note


# ------------------------------------------------------------------ endpoints on the snapshot


@pytest.fixture(scope="module")
def client():
    import os

    os.environ.pop("APP_USERNAME", None)
    os.environ.pop("APP_PASSWORD", None)
    os.environ.pop("DATA_SOURCE", None)
    data_source._provider = None
    from fastapi.testclient import TestClient

    import main

    return TestClient(main.app)


def test_every_endpoint_answers_on_the_snapshot(client):
    meta = client.get("/api/meta").json()
    assert meta["data_source"] == "snapshot" and meta["row_count"] > 10000

    city = client.get("/api/cities").json()["cities"][0]["city_name"]
    districts = client.get("/api/districts", params={"city": city}).json()["districts"]
    assert districts
    assert client.get("/api/points", params={"city": city}).json()["points"]

    found = client.get("/api/search", params={"q": "jakarta", "limit": 5}).json()
    assert found["total"] > 0
    ids = [str(b["inventory_id"]) for b in found["results"]]
    assert len(client.get("/api/boards", params={"ids": ",".join(ids)}).json()["boards"]) == len(ids)

    req = {"city": city, "min_billboards": 1, "max_billboards": 3, "months": 1}
    assert client.post("/api/calculate", json=req).json()["plans"]
    assert client.post("/api/budget_curve", json=req).json()["points"]
    assert "swaps" in client.post("/api/swaps", json={"ids": ids[:2]}).json()
    assert client.get("/api/market").json()["rows"]
    assert client.get("/api/market", params={"city": city}).json()["rows"]
