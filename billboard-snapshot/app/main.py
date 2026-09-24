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

import hashlib
import json
import math
import os
import re
import secrets
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
DATA_PATH = BASE_DIR / "data" / "snapshot.json"

app = FastAPI(title="Billboard Calculator (snapshot)")

# ------------------------------------------------------------------ auth
#
# Simple shared-password gate via a real login page + cookie, rather than
# browser-native HTTP Basic Auth -- some serverless hosts (Vercel included)
# don't reliably pass the WWW-Authenticate header through, so the browser
# never shows the popup and you just see raw JSON. A cookie set from a
# normal HTML form sidesteps that entirely.
#
# Set APP_USERNAME / APP_PASSWORD as env vars on the host; if unset, auth
# is skipped (fine for local dev only).

COOKIE_NAME = "billboard_session"


def _expected_creds():
    return os.environ.get("APP_USERNAME"), os.environ.get("APP_PASSWORD")


def _session_token(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def is_authenticated(request: Request) -> bool:
    user, pw = _expected_creds()
    if not user or not pw:
        return True  # no credentials configured -- local dev, auth disabled
    token = request.cookies.get(COOKIE_NAME)
    return bool(token) and secrets.compare_digest(token, _session_token(pw))


def require_auth(request: Request) -> None:
    """For /api/* routes: plain 401 if the session cookie is missing/wrong."""
    if not is_authenticated(request):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")


LOGIN_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Billboard Calculator - Sign in</title>
<style>
  body {{ margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
         background:#f6f7f9; font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
  .card {{ background:#fff; border:1px solid #e3e6ea; border-radius:10px; padding:28px;
           width:280px; box-shadow:0 1px 3px rgba(0,0,0,.06); }}
  h1 {{ font-size:17px; margin:0 0 18px; }}
  label {{ display:block; font-size:11px; font-weight:600; color:#6b7280;
           text-transform:uppercase; letter-spacing:.04em; margin-bottom:4px; }}
  input {{ width:100%; padding:8px 10px; margin-bottom:14px; border:1px solid #e3e6ea;
           border-radius:8px; font-size:14px; box-sizing:border-box; }}
  button {{ width:100%; padding:9px; border:none; border-radius:8px; background:#1a56db;
            color:#fff; font-weight:700; cursor:pointer; }}
  .err {{ color:#b42318; font-size:12.5px; margin:-6px 0 14px; }}
</style>
</head>
<body>
  <form class="card" method="post" action="/login">
    <h1>Billboard Calculator</h1>
    {error_html}
    <label for="u">Username</label>
    <input id="u" name="username" autocomplete="username" autofocus>
    <label for="p">Password</label>
    <input id="p" name="password" type="password" autocomplete="current-password">
    <button type="submit">Sign in</button>
  </form>
</body>
</html>"""


@app.get("/login", response_class=HTMLResponse)
def login_form(error: Optional[str] = None):
    error_html = '<div class="err">Incorrect username or password.</div>' if error else ""
    return LOGIN_PAGE.format(error_html=error_html)


@app.post("/login")
def login_submit(username: str = Form(...), password: str = Form(...)):
    user, pw = _expected_creds()
    if not user or not pw:
        return RedirectResponse(url="/", status_code=303)
    if secrets.compare_digest(username, user) and secrets.compare_digest(password, pw):
        resp = RedirectResponse(url="/", status_code=303)
        resp.set_cookie(
            COOKIE_NAME,
            _session_token(pw),
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=60 * 60 * 24 * 30,  # 30 days
        )
        return resp
    return RedirectResponse(url="/login?error=1", status_code=303)


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


def _is_usable(b: Billboard) -> bool:
    # A few rows carry placeholder prices (Rp 1/month), which makes their CPM
    # round to Rp 0 and float them to the top of every cheapest-CPM ranking.
    # They aren't real offers, so leave them out of planning and search.
    return b.monthly_price > 1 and b.cpm_calculated is not None and round(b.cpm_calculated) > 0


_ALL_ROWS = [Billboard(**row) for row in _SNAPSHOT["rows"]]
ALL_BILLBOARDS: List[Billboard] = [b for b in _ALL_ROWS if _is_usable(b)]
HIDDEN_COUNT = len(_ALL_ROWS) - len(ALL_BILLBOARDS)


# ------------------------------------------------------------------ areas
#
# The same district is spelled several ways in the source data
# ("Setiabudi", "Setia Budi", "Kecamatan Setiabudi"), so districts are
# grouped by a normalised key and shown under their most common spelling.

_DISTRICT_PREFIXES = ("kecamatan ", "kec. ", "kec ")


def _strip_district_prefix(name: str) -> str:
    n = (name or "").strip()
    for prefix in _DISTRICT_PREFIXES:
        if n.lower().startswith(prefix):
            return n[len(prefix):].strip()
    return n


def district_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _strip_district_prefix(name).lower())


def distance_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance (haversine)."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class Area(BaseModel):
    districts: Optional[List[str]] = None  # district keys from /api/districts
    center_lat: Optional[float] = None
    center_lng: Optional[float] = None
    radius_km: Optional[float] = None

    def active(self) -> bool:
        return bool(self.districts) or self.has_point()

    def has_point(self) -> bool:
        return (
            self.center_lat is not None
            and self.center_lng is not None
            and bool(self.radius_km)
            and self.radius_km > 0
        )

    def matches(self, b: Billboard) -> bool:
        if self.districts and district_key(b.district_name) not in self.districts:
            return False
        if self.has_point():
            if b.latitude is None or b.longitude is None:
                return False
            if distance_km(self.center_lat, self.center_lng, b.latitude, b.longitude) > self.radius_km:
                return False
        return True


class CalcRequest(BaseModel):
    city: str
    min_billboards: int = 1
    max_billboards: int = 3
    budget: Optional[float] = None  # None/0 = no budget limit
    months: int = 1
    impression_target: float = 0  # 0 = no impression target
    display_type: Optional[str] = None  # 'OOH' | 'DOOH' | None
    area: Optional[Area] = None  # optional district / distance targeting


def fetch_candidates(city: str, display_type: Optional[str], area: Optional[Area] = None) -> List[Billboard]:
    city_key = city.strip().lower()
    rows = [
        b
        for b in ALL_BILLBOARDS
        if b.city_name.strip().lower() == city_key
        and (not display_type or b.display_type_name == display_type)
        and (not area or area.matches(b))
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
        "hidden_count": HIDDEN_COUNT,
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


@app.get("/api/districts")
def districts(city: str, _: None = Depends(require_auth)):
    city_key = city.strip().lower()
    counts: Counter = Counter()
    spellings: dict = defaultdict(Counter)
    for b in ALL_BILLBOARDS:
        if b.city_name.strip().lower() != city_key or not b.district_name:
            continue
        key = district_key(b.district_name)
        if not key:
            continue
        counts[key] += 1
        spellings[key][_strip_district_prefix(b.district_name)] += 1
    rows = [
        {"key": k, "label": spellings[k].most_common(1)[0][0], "inventory_count": n}
        for k, n in counts.items()
    ]
    rows.sort(key=lambda r: r["label"].lower())
    return {"districts": rows}


@app.get("/api/points")
def points(city: str, display_type: Optional[str] = None, _: None = Depends(require_auth)):
    """Every billboard in a city as a small map point, for the planner map."""
    city_key = city.strip().lower()
    return {
        "points": [
            {
                "id": b.inventory_id,
                "name": b.inventory_name,
                "lat": b.latitude,
                "lng": b.longitude,
                "cpm": b.cpm_calculated,
            }
            for b in ALL_BILLBOARDS
            if b.city_name.strip().lower() == city_key
            and b.latitude is not None
            and b.longitude is not None
            and (not display_type or b.display_type_name == display_type)
        ]
    }


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


# Precomputed once at startup so each search is a plain substring scan.
_SEARCH_INDEX = [(_search_text(b), b) for b in ALL_BILLBOARDS]


@app.get("/api/search")
def search(
    q: str = "",
    city: Optional[str] = None,
    display_type: Optional[str] = None,
    limit: int = 50,
    _: None = Depends(require_auth),
):
    """Free-text billboard search for the custom plan builder.

    Every whitespace-separated word in `q` must appear somewhere in the
    billboard's id, name, address, sub-district, district or city, so
    "menteng atas" and "atas menteng" both match. Billboards containing the
    exact phrase come first, then the rest; each group cheapest CPM first.
    """
    words = q.lower().split()
    city_key = city.strip().lower() if city else None
    limit = max(1, min(limit, 200))

    phrase = " ".join(words)

    matches = [
        (phrase in text, b)
        for text, b in _SEARCH_INDEX
        if all(w in text for w in words)
        and (not city_key or b.city_name.strip().lower() == city_key)
        and (not display_type or b.display_type_name == display_type)
    ]
    # exact-phrase hits first, then cheapest CPM
    matches.sort(key=lambda m: (not m[0], m[1].cpm_calculated is None, m[1].cpm_calculated or 0.0))
    matches = [b for _, b in matches]
    return {
        "total": len(matches),
        "results": [b.__dict__ for b in matches[:limit]],
    }


@app.post("/api/calculate")
def calculate(req: CalcRequest, _: None = Depends(require_auth)):
    if req.min_billboards < 1 or req.max_billboards < req.min_billboards:
        return JSONResponse({"error": "invalid billboard count range"}, status_code=400)
    if req.max_billboards > 6:
        return JSONResponse(
            {"error": "max 6 billboards per plan (combination count explodes)"},
            status_code=400,
        )

    area = req.area if req.area and req.area.active() else None
    candidates = fetch_candidates(req.city, req.display_type, area)

    if not candidates:
        where = f"the selected area of {req.city}" if area else req.city
        return {
            "message": f"No priced inventory with impression data found in {where}."
            + (" Try a bigger radius or more districts." if area else ""),
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
def index(request: Request):
    if not is_authenticated(request):
        return RedirectResponse(url="/login")
    return FileResponse(str(BASE_DIR / "static" / "index.html"))
