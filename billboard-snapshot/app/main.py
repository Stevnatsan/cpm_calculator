"""
Billboard calculator -- snapshot version.

Reads app/data/snapshot.json (produced by generate_snapshot.py) once at
startup and answers every request from memory. No BigQuery calls at
request time, so no GCP credentials are needed to deploy this anywhere.

Data freshness = whenever snapshot.json was last generated. See /api/meta
or the banner in the UI for that timestamp.

Run locally:
    pip install -r requirements.txt
    uvicorn app.main:app --reload --port 8000
    open http://localhost:8000
"""

import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
DATA_PATH = BASE_DIR / "data" / "snapshot.json"

app = FastAPI(title="Billboard Calculator (snapshot)")

# ------------------------------------------------------------------ auth
#
# Simple shared-password gate. Set APP_USERNAME / APP_PASSWORD as env vars
# on the host; if unset, auth is skipped (fine for local dev only).

security = HTTPBasic()


def require_auth(credentials: HTTPBasicCredentials = Depends(security)) -> None:
    expected_user = os.environ.get("APP_USERNAME")
    expected_pass = os.environ.get("APP_PASSWORD")
    if not expected_user or not expected_pass:
        return  # no credentials configured -- local dev, auth disabled
    user_ok = secrets.compare_digest(credentials.username, expected_user)
    pass_ok = secrets.compare_digest(credentials.password, expected_pass)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


# ------------------------------------------------------------------ snapshot data


def _load_snapshot():
    if not DATA_PATH.exists():
        raise RuntimeError(
            f"{DATA_PATH} not found. Run generate_snapshot.py locally first, "
            "then redeploy with that file included."
        )
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        payload = json.load(f)
    return payload


_SNAPSHOT = _load_snapshot()
SNAPSHOT_GENERATED_AT: str = _SNAPSHOT["generated_at"]


@dataclass
class Billboard:
    billboard_name: str
    inventory_name: str
    address: str
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


ALL_BILLBOARDS: List[Billboard] = [Billboard(**row) for row in _SNAPSHOT["rows"]]


class CalcRequest(BaseModel):
    city: str
    min_billboards: int = 1
    max_billboards: int = 3
    budget: Optional[float] = None  # None/0 = no budget limit
    months: int = 1
    impression_target: float = 0  # 0 = no impression target
    display_type: Optional[str] = None  # 'OOH' | 'DOOH' | None


def fetch_candidates(city: str, display_type: Optional[str]) -> List[Billboard]:
    city_key = city.strip().lower()
    rows = [
        b
        for b in ALL_BILLBOARDS
        if b.city_name.strip().lower() == city_key
        and (not display_type or b.display_type_name == display_type)
    ]
    # cheapest CPM first, same ordering the live version's SQL used
    rows.sort(key=lambda b: (b.cpm_calculated is None, b.cpm_calculated or 0.0))
    return rows[:400]


# ------------------------------------------------------------------ solver
#
# Unchanged from the live version -- see original README for the full
# rationale (greedy fill, not brute-force knapsack).


def blended_cpm(total_cost: float, total_impression: float) -> Optional[float]:
    if total_impression <= 0:
        return None
    return total_cost / (total_impression / 1000)


def solve(candidates, budget, months, impression_target, min_n, max_n):
    selected = []
    cum_cost = 0.0
    cum_impression = 0.0
    plans = []

    for b in candidates:
        if len(selected) >= max_n:
            break

        item_cost = b.monthly_price * months
        if budget and budget > 0 and (cum_cost + item_cost) > budget:
            continue

        selected.append(b)
        cum_cost += item_cost
        cum_impression += b.monthly_impression * months

        if len(selected) < min_n:
            continue

        plans.append(
            {
                "billboard_count": len(selected),
                "billboards": [x.__dict__ for x in selected],
                "total_cost": cum_cost,
                "budget_remaining": (budget - cum_cost) if budget and budget > 0 else None,
                "budget_used_pct": (cum_cost / budget * 100) if budget and budget > 0 else None,
                "total_impression": cum_impression,
                "blended_cpm": blended_cpm(cum_cost, cum_impression),
                "meets_impression_target": cum_impression >= impression_target,
                "impression_vs_target_pct": (
                    cum_impression / impression_target * 100
                    if impression_target > 0
                    else None
                ),
                "recommended": False,
            }
        )

        if impression_target > 0 and cum_impression >= impression_target:
            break

    if plans:
        if impression_target > 0:
            recommended = next((p for p in plans if p["meets_impression_target"]), plans[-1])
        else:
            recommended = plans[-1]
        recommended["recommended"] = True

    return plans


# ------------------------------------------------------------------ routes


@app.get("/api/meta")
def meta(_: None = Depends(require_auth)):
    return {
        "snapshot_generated_at": SNAPSHOT_GENERATED_AT,
        "row_count": len(ALL_BILLBOARDS),
    }


@app.get("/api/cities")
def cities(_: None = Depends(require_auth)):
    counts: dict = {}
    for b in ALL_BILLBOARDS:
        if not b.city_name:
            continue
        counts[b.city_name] = counts.get(b.city_name, 0) + 1
    rows = [{"city_name": k, "inventory_count": v} for k, v in counts.items()]
    rows.sort(key=lambda r: -r["inventory_count"])
    return {"cities": rows}


@app.post("/api/calculate")
def calculate(req: CalcRequest, _: None = Depends(require_auth)):
    if req.min_billboards < 1 or req.max_billboards < req.min_billboards:
        return JSONResponse({"error": "invalid billboard count range"}, status_code=400)
    if req.max_billboards > 6:
        return JSONResponse(
            {"error": "max 6 billboards per plan (combination count explodes)"},
            status_code=400,
        )

    candidates = fetch_candidates(req.city, req.display_type)

    if not candidates:
        return {
            "message": f"No priced inventory with impression data found in {req.city}.",
            "plans": [],
            "candidates": [],
            "candidate_count": 0,
            "plan_count": 0,
            "feasible_count": 0,
        }

    budget = req.budget if req.budget and req.budget > 0 else None
    target = req.impression_target if req.impression_target and req.impression_target > 0 else 0

    plans = solve(
        candidates,
        budget=budget,
        months=req.months,
        impression_target=target,
        min_n=req.min_billboards,
        max_n=req.max_billboards,
    )
    feasible = [p for p in plans if p["meets_impression_target"]]
    recommended = next((p for p in plans if p["recommended"]), None)

    if not plans:
        by_price = sorted(candidates, key=lambda c: c.monthly_price)
        if len(by_price) < req.min_billboards:
            message = (
                f"Only {len(by_price)} priced billboard(s) found in {req.city} -- "
                f"not enough to build a {req.min_billboards}-billboard plan."
            )
        else:
            floor_cost = sum(c.monthly_price for c in by_price[: req.min_billboards]) * req.months
            message = (
                f"Nothing fits. Even the cheapest possible {req.min_billboards}-billboard "
                f"combination in {req.city} costs about Rp {floor_cost:,.0f} for "
                f"{req.months} month(s), above your Rp {budget:,.0f} budget."
            )
    elif target and not feasible:
        message = (
            f"Reached only {recommended['total_impression']:,.0f} impressions "
            f"({recommended['impression_vs_target_pct']:.0f}% of your "
            f"{target:,.0f} target) using {recommended['billboard_count']} billboard(s)"
            + (
                f", limited by your Rp {budget:,.0f} budget. Raise the budget to add more."
                if budget
                else ", the most this city's inventory allows within your billboard-count limit. "
                "Raise 'Billboards max' to consider more."
            )
        )
    elif target:
        pct_note = f" ({recommended['budget_used_pct']:.0f}% of your budget)" if budget else ""
        message = (
            f"Cheapest way to reach {target:,.0f} impressions: "
            f"{recommended['billboard_count']} billboard(s) for "
            f"Rp {recommended['total_cost']:,.0f}{pct_note}."
        )
    else:
        message = (
            f"Best use of your budget: {recommended['billboard_count']} billboard(s) for "
            f"Rp {recommended['total_cost']:,.0f}, {recommended['total_impression']:,.0f} "
            "impressions"
            + (f" ({recommended['budget_used_pct']:.0f}% of budget used)." if budget else ".")
        )

    return {
        "message": message,
        "candidate_count": len(candidates),
        "plan_count": len(plans),
        "feasible_count": len(feasible),
        "plans": plans,
        "candidates": [c.__dict__ for c in candidates[:100]],
    }


@app.get("/")
def index(_: None = Depends(require_auth)):
    return FileResponse(str(BASE_DIR / "static" / "index.html"))
