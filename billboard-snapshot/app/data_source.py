"""
Where the billboard data comes from.

Every endpoint in app/main.py reads billboards through `current()`, which
returns a DataStore: the list of usable billboards plus the lookups built
from it (by id, search index). There are two sources:

- snapshot (the default): app/data/snapshot.json, written by
  generate_snapshot.py. Loaded once at startup.
- bigquery: the same query generate_snapshot.py runs (app/queries.py), run
  live and cached for DATA_CACHE_SECONDS. Used only when DATA_SOURCE=bigquery
  AND a service-account credential is set. If the query fails, the app keeps
  serving the last good live data, or the snapshot if it has none yet, and
  /api/meta says so.

Environment variables (all optional):
    DATA_SOURCE                          snapshot | bigquery   (default snapshot)
    GOOGLE_APPLICATION_CREDENTIALS_JSON  service-account key, the whole JSON as text
    GOOGLE_APPLICATION_CREDENTIALS       or: path to a service-account key file
    DATA_CACHE_SECONDS                   how long live data is reused (default 3600)
    DATA_RETRY_SECONDS                   wait after a failed query (default 300)
    BQ_PROJECT, BQ_TABLE                 see app/queries.py
"""

import datetime
import decimal
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger("billboard.data")

SNAPSHOT_PATH = Path(__file__).resolve().parent / "data" / "snapshot.json"


@dataclass
class Billboard:
    inventory_id: str
    inventory_name: str
    inventory_address: str
    city_name: str
    district_name: str
    sub_district_name: str
    display_type_name: str
    lighting_type: str
    venue_type: str
    image_url: str
    latitude: Optional[float]
    longitude: Optional[float]
    monthly_price: float
    monthly_impression: float
    cpm: Optional[float]
    cpm_calculated: Optional[float]
    monthly_impression_source: Optional[str]
    last_active_period: Optional[str]


# The columns every row must carry, from the snapshot file or from BigQuery.
# app/queries.py selects exactly these names.
REQUIRED_COLUMNS = [f.name for f in fields(Billboard)]


class SchemaError(ValueError):
    """The data is missing columns the app needs."""


def check_columns(rows: List[dict]) -> None:
    if not rows:
        raise SchemaError("no rows")
    missing = [c for c in REQUIRED_COLUMNS if c not in rows[0]]
    if missing:
        raise SchemaError(
            "rows are missing columns " + ", ".join(missing)
            + ". The query in app/queries.py must select every Billboard field."
        )


def _plain(v):
    # BigQuery returns NUMERIC columns as Decimal; the app and JSON want floats.
    return float(v) if isinstance(v, decimal.Decimal) else v


def to_billboard(row: dict) -> Billboard:
    # Extra columns are ignored so the query can grow without breaking the app.
    return Billboard(**{c: _plain(row.get(c)) for c in REQUIRED_COLUMNS})


def _is_usable(b: Billboard) -> bool:
    # A few rows carry placeholder prices (Rp 1/month), which makes their CPM
    # round to Rp 0 and float them to the top of every cheapest-CPM ranking.
    # They aren't real offers, so leave them out of planning and search.
    return (
        b.monthly_price is not None
        and b.monthly_price > 1
        and b.monthly_impression is not None
        and b.cpm_calculated is not None
        and round(b.cpm_calculated) > 0
    )


def _search_text(b: Billboard) -> str:
    return " ".join(
        filter(
            None,
            [
                str(b.inventory_id),
                b.inventory_name,
                b.inventory_address,
                b.sub_district_name,
                b.district_name,
                b.city_name,
            ],
        )
    ).lower()


class DataStore:
    """One loaded copy of the data and everything the endpoints precompute from it."""

    def __init__(self, rows: List[dict], generated_at: str, source: str, note: Optional[str] = None):
        check_columns(rows)
        all_rows = [to_billboard(r) for r in rows]
        self.billboards: List[Billboard] = [b for b in all_rows if _is_usable(b)]
        self.hidden_count = len(all_rows) - len(self.billboards)
        self.by_id: Dict[str, Billboard] = {str(b.inventory_id): b for b in self.billboards}
        # Precomputed once per load so each search is a plain substring scan.
        self.search_index = [(_search_text(b), b) for b in self.billboards]
        self.generated_at = generated_at
        self.source = source  # "snapshot" | "bigquery"
        self.note = note  # shown in /api/meta, e.g. why live data isn't in use
        self.loaded_at = time.time()

    def with_note(self, note: Optional[str]) -> "DataStore":
        copy = object.__new__(DataStore)
        copy.__dict__.update(self.__dict__)
        copy.note = note
        return copy


# ------------------------------------------------------------------ snapshot


def load_snapshot(path: Path = SNAPSHOT_PATH) -> DataStore:
    if not path.exists():
        raise RuntimeError(
            f"{path} not found. Run generate_snapshot.py locally first, "
            "then redeploy with that file included."
        )
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    return DataStore(payload["rows"], payload["generated_at"], "snapshot")


# ------------------------------------------------------------------ bigquery


def has_credentials() -> bool:
    return bool(
        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
        or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    )


def bigquery_client():
    # Imported here so the snapshot-only deploy doesn't need the library.
    from google.cloud import bigquery

    from queries import PROJECT_ID

    key_json = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    if key_json:
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_info(json.loads(key_json))
        return bigquery.Client(project=PROJECT_ID, credentials=creds)
    # GOOGLE_APPLICATION_CREDENTIALS (a key file) or local gcloud login.
    return bigquery.Client(project=PROJECT_ID)


def fetch_bigquery_rows(timeout: float = 25.0) -> List[dict]:
    from queries import SNAPSHOT_SQL

    client = bigquery_client()
    job = client.query(SNAPSHOT_SQL, timeout=timeout)
    return [{k: _plain(v) for k, v in dict(r).items()} for r in job.result(timeout=timeout)]


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ provider


class DataProvider:
    def __init__(self, fetch_live=fetch_bigquery_rows, snapshot_loader=load_snapshot):
        self.mode = os.environ.get("DATA_SOURCE", "snapshot").strip().lower()
        self.ttl = float(os.environ.get("DATA_CACHE_SECONDS", "3600"))
        self.retry_after = float(os.environ.get("DATA_RETRY_SECONDS", "300"))
        self._fetch_live = fetch_live
        self._lock = threading.Lock()
        self._snapshot = snapshot_loader()
        self._live: Optional[DataStore] = None
        self._last_attempt = 0.0
        self._last_error: Optional[str] = None

        if self.mode not in ("snapshot", "bigquery"):
            log.warning("Unknown DATA_SOURCE=%r, using the snapshot", self.mode)
            self._snapshot = self._snapshot.with_note(f"Unknown DATA_SOURCE '{self.mode}', using the snapshot.")
            self.mode = "snapshot"
        elif self.mode == "bigquery" and not has_credentials():
            log.warning("DATA_SOURCE=bigquery but no service-account credential is set, using the snapshot")
            self._snapshot = self._snapshot.with_note(
                "Live data is switched on but no BigQuery credential is set, so this is the snapshot."
            )
            self.mode = "snapshot"

    def current(self) -> DataStore:
        if self.mode != "bigquery":
            return self._snapshot
        now = time.time()
        live = self._live
        fresh = live is not None and now - live.loaded_at < self.ttl
        waiting = self._last_error is not None and now - self._last_attempt < self.retry_after
        if not fresh and not waiting and self._lock.acquire(blocking=False):
            # One request refreshes; concurrent ones keep using what's loaded.
            try:
                self._refresh()
            finally:
                self._lock.release()
        return self._live or self._fallback()

    def _refresh(self) -> None:
        self._last_attempt = time.time()
        try:
            rows = self._fetch_live()
            self._live = DataStore(rows, _utc_now(), "bigquery")
            self._last_error = None
            log.info("Loaded %d billboards from BigQuery", len(self._live.billboards))
        except Exception as e:  # noqa: BLE001 -- any failure falls back, never breaks the app
            log.exception("BigQuery refresh failed, keeping previous data")
            self._last_error = f"{type(e).__name__}: {e}"[:300]
            if self._live is not None:
                self._live = self._live.with_note(
                    "The latest live refresh failed; showing the last live data loaded."
                )

    def _fallback(self) -> DataStore:
        return self._snapshot.with_note("Live data couldn't be loaded, so this is the snapshot.")

    def status(self) -> dict:
        return {
            "configured_source": self.mode,
            "last_error": self._last_error,
        }


_provider: Optional[DataProvider] = None


def provider() -> DataProvider:
    global _provider
    if _provider is None:
        _provider = DataProvider()
    return _provider


def current() -> DataStore:
    return provider().current()
