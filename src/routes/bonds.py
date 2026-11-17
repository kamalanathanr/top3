"""
Bonds API routes — IndiaBonds data, scrape trigger, progress monitoring.
Scoring logic ported from indiabonds/app.py.
"""

import base64
import re
from collections import Counter
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Query
from fastapi.responses import JSONResponse, Response

from src.database import get_db
from src.celery_config import get_celery_app

router = APIRouter(prefix="/api/bonds")


# ─── Rating helpers ───────────────────────────────────────────────────────────

_RATING_ORDER = [
    "AAA", "AA+", "AA", "AA-",
    "A+", "A", "A-",
    "BBB+", "BBB", "BBB-",
    "BB+", "BB", "BB-",
    "B+", "B", "B-", "D",
]
_RATING_RANK = {r: i for i, r in enumerate(_RATING_ORDER)}


def rating_rank(r: str) -> int:
    return _RATING_RANK.get(str(r).strip(), 999)


def normalize_rating(text: str | None) -> str:
    if not text:
        return ""
    m = re.search(r"\b(AAA|AA[+-]?|A[+-]?|BBB[+-]?|BB[+-]?|B[+-]?|D)(?!\w)", str(text))
    return m.group(1) if m else str(text).strip()


def meets_min_rating_level(rating: str, min_level: str) -> bool:
    if not min_level:
        return True
    norm = normalize_rating(rating)
    if not norm:
        return False
    if norm == "Sovereign":
        norm = "AAA"
    return _RATING_RANK.get(norm, 999) <= _RATING_RANK.get(min_level, 999)


# ─── Scoring ──────────────────────────────────────────────────────────────────

_CREDIT_POINTS = {
    "AAA": 10, "Sovereign": 10,
    "AA+": 9, "AA": 9, "AA-": 8,
    "A+": 7, "A": 7, "A-": 6,
    "BBB+": 5, "BBB": 5, "BBB-": 4,
    "BB+": 3, "BB": 2, "BB-": 2,
    "B+": 1, "B": 1, "B-": 1, "C": 1, "D": 0,
}


def credit_points(rating: str) -> int:
    r = str(rating or "").strip()
    if r in _CREDIT_POINTS:
        return _CREDIT_POINTS[r]
    norm = normalize_rating(r)
    return _CREDIT_POINTS.get(norm, -1)


def seniority_points(bond: dict) -> tuple[int, str]:
    sec_class = str(bond.get("security_class") or "").lower()
    rating    = str(bond.get("rating") or "").lower()
    has_trustee = bool(bond.get("debenture_trustee"))
    if rating == "sovereign" or "government" in sec_class or "state development" in sec_class:
        return 5, "Sovereign / Govt Security"
    if "municipal" in sec_class:
        return 4, "Municipal Bond"
    if "debenture" in sec_class:
        return (5, "Senior Secured (NCD w/ trustee)") if has_trustee else (3, "Senior Unsecured (NCD)")
    if "bond" in sec_class:
        return 3, "Senior Unsecured"
    return 3, "Senior Unsecured (default)"


def normalize_frequency(freq: str | None) -> str:
    if not freq:
        return ""
    f = str(freq).lower().strip()
    if "twelve" in f or "month" in f:
        return "Monthly"
    if "quarter" in f or "four" in f or "4" in f:
        return "Quarterly"
    if "twice" in f or "semi" in f or "2" in f:
        return "Semi-annually"
    if "once" in f or "annual" in f or "year" in f:
        return "Annually"
    return str(freq)


def safe_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    s_str = str(s).strip()
    try:
        if "T" in s_str:
            return datetime.fromisoformat(s_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        pass
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s_str, fmt)
        except ValueError:
            continue
    return None


def format_maturity_tenure(date_str: str | None) -> str:
    if not date_str:
        return "—"
    try:
        if isinstance(date_str, str) and "T" in date_str:
            mat = datetime.fromisoformat(date_str.replace("Z", "+00:00")).date()
        else:
            mat = datetime.strptime(str(date_str), "%Y-%m-%d").date()
        today = datetime.now().date()
        if mat <= today:
            return "Matured"
        delta = mat - today
        years  = delta.days // 365
        months = (delta.days % 365) // 30
        if years == 0:
            return f"{months}m" if months > 0 else "—"
        return f"{years}y" if months == 0 else f"{years}y.{months}m"
    except (ValueError, AttributeError):
        return "—"


def get_maturity_bonus(maturity_date_str: str | None) -> float:
    if not maturity_date_str:
        return 0.0
    try:
        mat_dt = parse_date(str(maturity_date_str))
        if not mat_dt:
            return 0.0
        mat   = mat_dt.date() if hasattr(mat_dt, "date") else mat_dt
        today = datetime.now().date()
        if mat <= today:
            return 0.0
        years = (mat - today).days / 365.0
        if years <= 1.0:  return 3.0
        if years <= 1.5:  return 2.5
        if years <= 2.0:  return 2.0
        if years <= 2.5:  return 1.0
        return 0.0
    except Exception:
        return 0.0


def score_bond(bond: dict) -> dict:
    cp = credit_points(bond.get("rating", ""))
    sp, slabel = seniority_points(bond)
    if cp < 0:
        bond.update(credit_pts=None, security_pts=sp, security_label=slabel,
                    total_score=None, zone="unrated")
    else:
        coupon_bonus = safe_float(bond.get("coupon_rate", 0))
        freq_bonus   = 1.0 if normalize_frequency(bond.get("payment_frequency", "")) in ("Monthly", "Quarterly") else 0.0
        mat_bonus    = get_maturity_bonus(bond.get("maturity_date"))
        total        = round(cp + sp + coupon_bonus + freq_bonus + mat_bonus, 2)
        zone = "green" if total >= 20 else "yellow" if total >= 16 else "orange" if total >= 12 else "red" if total >= 8 else "dark_red"
        bond.update(credit_pts=cp, security_pts=sp, security_label=slabel,
                    coupon_bonus=coupon_bonus, frequency_bonus=freq_bonus,
                    maturity_bonus=mat_bonus, total_score=total, zone=zone)
    bond["maturity_tenure"] = format_maturity_tenure(bond.get("maturity_date"))
    bond["payment_frequency_norm"] = normalize_frequency(bond.get("payment_frequency", ""))
    return bond


def serialize_bond(b: dict) -> dict:
    """Strip MongoDB _id and non-serialisable fields."""
    b.pop("_id", None)
    return b


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.get("")
async def get_bonds(
    min_rating_level: str = Query("", description="Minimum rating level e.g. AA"),
    mat_max_years: float  = Query(None, description="Max years to maturity"),
    frequency: str        = Query("", description="Paying frequency e.g. Monthly"),
    sort: str             = Query("score", description="score | rating | maturity | coupon_rate"),
    order: str            = Query("desc"),
):
    db = get_db()
    raw_bonds = await db.bonds.find({"bond_platform": "indiabonds"}).to_list(length=None)
    bonds = [score_bond(serialize_bond(b)) for b in raw_bonds]

    def passes(b: dict) -> bool:
        if min_rating_level and not meets_min_rating_level(b.get("rating", ""), min_rating_level):
            return False
        if mat_max_years is not None:
            mat_dt = parse_date(str(b.get("maturity_date") or ""))
            if mat_dt:
                mat   = mat_dt.date() if hasattr(mat_dt, "date") else mat_dt
                today = datetime.now().date()
                if mat > today and (mat - today).days / 365.0 > mat_max_years:
                    return False
        if frequency and normalize_frequency(b.get("payment_frequency", "")).lower() != frequency.lower():
            return False
        return True

    filtered = [b for b in bonds if passes(b)]

    def sort_key(b: dict):
        if sort == "score":
            s = b.get("total_score")
            return s if s is not None else (-1 if order == "desc" else 9e18)
        if sort == "rating":
            return rating_rank(b.get("rating", ""))
        if sort == "maturity":
            d = parse_date(str(b.get("maturity_date") or ""))
            return d.timestamp() if d else 9e18
        return safe_float(b.get(sort, 0))

    filtered.sort(key=sort_key, reverse=(order == "desc"))
    return JSONResponse({"total": len(filtered), "bonds": filtered})


@router.get("/stats")
async def get_stats():
    db = get_db()
    raw_bonds = await db.bonds.find({"bond_platform": "indiabonds"}).to_list(length=None)
    bonds = [score_bond(serialize_bond(b)) for b in raw_bonds]
    if not bonds:
        return JSONResponse({"total_bonds": 0})

    ytms    = [safe_float(b.get("ytm")) for b in bonds if b.get("ytm")]
    ratings = [normalize_rating(b.get("rating", "")) for b in bonds if b.get("rating")]
    last_scraped = max((b.get("scraped_at", "") for b in bonds), default="")

    return JSONResponse({
        "total_bonds":         len(bonds),
        "avg_ytm":             round(sum(ytms) / len(ytms), 2) if ytms else 0,
        "max_ytm":             round(max(ytms), 2) if ytms else 0,
        "min_ytm":             round(min(ytms), 2) if ytms else 0,
        "rating_distribution": dict(Counter(ratings).most_common()),
        "last_scraped":        last_scraped,
    })


@router.get("/scrape-log")
async def get_scrape_log():
    db = get_db()
    doc = await db.bonds_scrape_log.find_one({"_id": "current"})
    if not doc:
        return JSONResponse({"run": {}, "bonds": {}})
    doc.pop("_id", None)
    return JSONResponse(doc)


@router.get("/scrape-status-live")
async def get_scrape_status_live():
    """Progress summary derived from the scrape log stored in MongoDB."""
    db = get_db()
    doc = await db.bonds_scrape_log.find_one({"_id": "current"})
    if not doc:
        return JSONResponse({"available": False})

    run   = doc.get("run", {})
    bonds_map = doc.get("bonds", {})
    bonds_list = list(bonds_map.values()) if isinstance(bonds_map, dict) else bonds_map

    counts: dict[str, int] = Counter(b.get("status", "unknown") for b in bonds_list)
    rate_limited = sum(
        1 for b in bonds_list for e in b.get("events", [])
        if e.get("type") == "rate_limited"
    )
    backoff_events = sum(
        1 for b in bonds_list for e in b.get("events", [])
        if e.get("type") in ("rate_limited", "empty_data", "network_error")
    )
    done  = counts.get("success", 0) + counts.get("incomplete", 0) + counts.get("failed", 0)
    total = run.get("total_urls") or len(bonds_list)
    progress_pct = round(done / total * 100, 1) if total else 0

    return JSONResponse({
        "available":            True,
        "is_running":           run.get("is_running", False),
        "task_id":              run.get("task_id", ""),
        "started_at":           run.get("started_at"),
        "completed_at":         run.get("completed_at"),
        "total_urls":           total,
        "done":                 done,
        "progress_pct":         progress_pct,
        "counts":               dict(counts),
        "rate_limited_events":  rate_limited,
        "backoff_events":       backoff_events,
    })


@router.post("/scrape")
async def trigger_scrape(resume: bool = True):
    """Launch bonds scraper as a background Celery task.

    Before queuing, revokes all active bond valuation tasks and clears the
    bonds and bonds_scrape_log collections so the scrape starts clean.
    """
    try:
        db = get_db()
        celery = get_celery_app()

        # Revoke only pending bond valuation tasks (running ones are left to finish)
        active_valuations = await db.queries.find(
            {"query_type": "bond_valuation", "status": "pending",
             "celery_task_id": {"$exists": True}},
            {"celery_task_id": 1},
        ).to_list(length=None)

        task_ids_to_revoke = [doc["celery_task_id"] for doc in active_valuations if doc.get("celery_task_id")]
        for tid in task_ids_to_revoke:
            celery.control.revoke(tid, terminate=True, signal="SIGTERM")

        if task_ids_to_revoke:
            await db.queries.update_many(
                {"celery_task_id": {"$in": task_ids_to_revoke}},
                {"$set": {"status": "cancelled",
                          "completed_at": datetime.now(timezone.utc).isoformat()}},
            )

        # Clear bonds and scrape log so the refresh starts from scratch
        await db.bonds.delete_many({"bond_platform": "indiabonds"})
        await db.bonds_scrape_log.delete_many({})

        from src.tasks import execute_bonds_scrape
        task = execute_bonds_scrape.delay(resume=resume)
        return JSONResponse({
            "task_id": task.id,
            "status": "queued",
            "revoked_valuations": len(task_ids_to_revoke),
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/scrape-task-status/{task_id}")
async def get_scrape_task_status(task_id: str):
    """Check Celery task state for a scrape job."""
    celery = get_celery_app()
    result = celery.AsyncResult(task_id)
    data = {"task_id": task_id, "state": result.state}
    if result.state == "SUCCESS":
        data["result"] = result.result
    elif result.state == "FAILURE":
        data["error"] = str(result.result)
    return JSONResponse(data)


# ─── Debt Gap & Bond Selection ────────────────────────────────────────────────

def _compute_allocation_profile(profile: dict) -> str:
    """Replicate planning.html's risk score computation to derive Conservative/Moderate/Aggressive."""
    import re as _re
    from datetime import date

    def _calc_age(dob_str: str) -> int:
        if not dob_str:
            return 0
        try:
            parts = dob_str[:10].split("-")
            dob = date(int(parts[0]), int(parts[1]), int(parts[2]))
            today = date.today()
            age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
            return max(0, age)
        except Exception:
            return 0

    def _scored_age(age: int) -> float:
        if age == 0:   return 5.5
        if age < 30:   return 9.5
        if age <= 40:  return 7.5
        if age <= 50:  return 5.5
        if age <= 60:  return 3.5
        return 1.0

    def _scored_risk(rp: str) -> float:
        if rp == "Aggressive": return 8.0
        if rp == "Moderate":   return 5.5
        return 3.0

    def _scored_emergency(months: float) -> float:
        if months >= 12: return 9.5
        if months >= 6:  return 7.0
        if months >= 3:  return 4.0
        return 1.0

    def _scored_horizon(y: int) -> float:
        if y >= 10: return 9.5
        if y >= 5:  return 7.0
        if y >= 3:  return 4.0
        if y > 0:   return 1.0
        return 1.0

    # Age
    head = next((m for m in (profile.get("family_members") or []) if m.get("is_head")), None)
    age = _calc_age(head.get("dob", "") if head else "")

    # Risk appetite
    stated_risk = profile.get("risk_profile", "Moderate")

    # Emergency fund
    assets = profile.get("existing_assets") or []
    emerg_value = sum(
        float(a.get("market_value") or 0) for a in assets
        if a.get("asset_type") == "Emergency Fund"
        or "emergency" in (a.get("name") or "").lower()
        or "emergency" in (a.get("goal") or "").lower()
    )

    # Monthly expenses from financial_goals
    annual_exp = 0.0
    mul = {"monthly": 12, "quarterly": 4, "4_months": 3, "6_months": 2, "annually": 1}
    for g in (profile.get("financial_goals") or []):
        amt = float(g.get("amount") or 0)
        if g.get("recurring"):
            annual_exp += amt * mul.get(g.get("frequency", ""), 1)
        else:
            annual_exp += amt
    monthly_exp = annual_exp / 12

    emerg_months = (emerg_value / monthly_exp) if (emerg_value > 0 and monthly_exp > 0) else 0.0
    has_emergency = emerg_value > 0

    horizon = int(profile.get("investment_planning_horizon") or 0)
    income_score = 4.5

    sc = 1.0 if has_emergency else (10 / 9)
    score = (
        _scored_age(age)        * 20 * sc / 100
        + _scored_risk(stated_risk) * 30 * sc / 100
        + (_scored_emergency(emerg_months) * 0.10 if has_emergency else 0)
        + _scored_horizon(horizon) * 20 * sc / 100
        + income_score             * 20 * sc / 100
    )

    if score <= 3.9:   return "Conservative"
    if score <= 6.9:   return "Moderate"
    return "Aggressive"


@router.get("/debt-gap")
async def get_debt_gap():
    """Return Debt/FD-Bonds allocation gap from the user's profile vs risk-based target."""
    import re as _re

    db = get_db()
    profile = await db.user_profiles.find_one({"user_id": "Kamal"})
    if not profile:
        return JSONResponse({"gap": None, "error": "No profile found"})

    existing_assets = profile.get("existing_assets", [])

    # Derive allocation profile the same way the Planning page does (computed score, not stored label)
    alloc_profile = _compute_allocation_profile(profile)

    # Conservative has an explicit "FD / Bonds" row (25%); Bonds assets map to "fd" key.
    # Macro rules only reference the "Debt" asset name, so Conservative's "FD / Bonds" row
    # is not macro-adjusted — only Moderate/Aggressive "Debt" row gets adjusted.
    has_fd_row = alloc_profile == "Conservative"
    if alloc_profile == "Conservative":
        base_target_pct = 25.0   # "FD / Bonds" row — not macro-adjusted
    elif alloc_profile == "Aggressive":
        base_target_pct = 15.0   # "Debt" row
    else:
        base_target_pct = 25.0   # Moderate "Debt" row

    # Apply same macro adjustments as Planning page (only for Moderate/Aggressive "Debt" row)
    debt_adj = 0.0
    if not has_fd_row:
        def _parse_num(s: str) -> float | None:
            m = _re.search(r"([\d.]+)", str(s or ""))
            return float(m.group(1)) if m else None

        indicators = await db.economy_indicator.find().to_list(length=None)
        for ind in indicators:
            name = (ind.get("indicator") or "").lower()
            val = _parse_num(ind.get("value"))
            if val is None:
                continue
            if any(k in name for k in ("cpi", "inflation", "consumer price")):
                if val > 6:
                    debt_adj -= 5.0
            if any(k in name for k in ("nifty pe", "nifty 50 pe", "p/e", "pe ratio", "price to earnings")):
                if val > 22:
                    debt_adj += 5.0
            if any(k in name for k in ("vix", "volatility index", "fear index")):
                if val > 20:
                    debt_adj += 5.0

    target_pct = max(0.0, base_target_pct + debt_adj)

    def _type_to_key(asset_type: str) -> str | None:
        t = (asset_type or "").strip()
        if t in ("Stocks", "ETFs", "MF"):
            return "equity"
        if t == "Bonds":
            return "fd" if has_fd_row else "debt"
        if t in ("FDs", "Emergency Fund"):
            return "cash"
        if t == "Gold":
            return "gold"
        return None

    total_portfolio = 0.0
    by_key: dict[str, float] = {}
    for asset in existing_assets:
        v = float(asset.get("market_value") or 0)
        if v > 0:
            key = _type_to_key(asset.get("asset_type", ""))
            if key:
                total_portfolio += v
                by_key[key] = by_key.get(key, 0.0) + v

    if total_portfolio == 0:
        return JSONResponse({"gap": None, "total_portfolio": 0,
                             "error": "No portfolio assets found", "alloc_profile": alloc_profile})

    bond_key = "fd" if has_fd_row else "debt"
    current_value = by_key.get(bond_key, 0.0)
    target_value = (target_pct / 100) * total_portfolio
    gap = current_value - target_value   # negative = shortfall (need to buy more bonds)

    return JSONResponse({
        "gap": round(gap, 2),
        "current_debt": round(current_value, 2),
        "target_debt": round(target_value, 2),
        "target_pct": round(target_pct, 1),
        "total_portfolio": round(total_portfolio, 2),
        "alloc_profile": alloc_profile,
        "invest_recommended": round(abs(gap), 2) if gap < 0 else 0,
    })


@router.get("/selected")
async def get_selected_bonds():
    """Return the user's selected bond ISINs with full scoring details."""
    db = get_db()
    profile = await db.user_profiles.find_one({"user_id": "Kamal"}, {"selected_bonds": 1})
    isins: list[str] = list((profile or {}).get("selected_bonds", []))

    bonds: list[dict] = []
    if isins:
        raw = await db.bonds.find({"isin": {"$in": isins}}).to_list(length=None)
        scored = {b["isin"]: score_bond(serialize_bond(b)) for b in raw}
        bonds = [scored[isin] for isin in isins if isin in scored]

    return JSONResponse({"selected_bond_isins": isins, "bonds": bonds})


@router.post("/selected/toggle")
async def toggle_selected_bond(payload: dict = Body(...)):
    """Add or remove a bond ISIN from the user's persisted selection."""
    db = get_db()
    isin = (payload.get("isin") or "").strip()
    if not isin:
        return JSONResponse({"error": "isin required"}, status_code=400)

    profile = await db.user_profiles.find_one({"user_id": "Kamal"}, {"selected_bonds": 1})
    current: list[str] = list((profile or {}).get("selected_bonds", []))

    if isin in current:
        current.remove(isin)
        action = "removed"
    else:
        current.append(isin)
        action = "added"

    await db.user_profiles.update_one(
        {"user_id": "Kamal"},
        {"$set": {"selected_bonds": current}},
        upsert=True,
    )
    return JSONResponse({"action": action, "selected_bond_isins": current})


# ─── Bond Valuation ───────────────────────────────────────────────────────────

@router.post("/{isin}/valuation")
async def trigger_bond_valuation(isin: str):
    """Create a query doc and launch a bond valuation Celery task."""
    db = get_db()

    # Find the Deep-Dive Analysis template
    template_doc = await db.query_templates.find_one({"name": {"$regex": "deep.dive", "$options": "i"}})
    if not template_doc:
        template_doc = await db.query_templates.find_one({"category": {"$regex": "deep.dive", "$options": "i"}})

    template_text = template_doc.get("template", "") if template_doc else ""

    # Find bond name
    bond_doc = await db.bonds.find_one({"isin": isin})
    bond_name = (bond_doc or {}).get("issuer_name", isin)

    # Substitute all placeholder variants used in templates
    prompt = (template_text
              .replace("<Bond Name>", bond_name)
              .replace("<Bond ISIN No>", isin)
              .replace("<Bond ISIN>", isin)
              .replace("{bond_name}", bond_name)
              .replace("{isin}", isin))

    if not prompt.strip():
        prompt = (
            f"Provide a comprehensive bond valuation analysis for:\n"
            f"Bond Name: {bond_name}\nISIN: {isin}\n\n"
            f"Include: issuer overview, credit risk, yield analysis, "
            f"liquidity, recommendation, and risk factors."
        )

    now = datetime.now(timezone.utc).isoformat()
    q_result = await db.queries.insert_one({
        "user_id": "Kamal",
        "query_type": "bond_valuation",
        "template_name": "Deep-Dive Analysis",
        "user_interests": [bond_name],
        "interests": [bond_name],
        "ai_query": prompt,
        "bond_name": bond_name,
        "bond_isin": isin,
        "status": "pending",
        "created_at": now,
    })
    query_id = str(q_result.inserted_id)

    from src.tasks import execute_bond_valuation
    task = execute_bond_valuation.delay(query_id)

    # Store task_id so we can resume polling after a page refresh
    await db.queries.update_one(
        {"_id": q_result.inserted_id},
        {"$set": {"celery_task_id": task.id}},
    )

    return JSONResponse({"task_id": task.id, "query_id": query_id, "status": "queued"})


@router.get("/active-valuations")
async def get_active_valuations():
    """Return {isin: task_id} for bond valuation queries still running/pending."""
    db = get_db()
    active = await db.queries.find(
        {"query_type": "bond_valuation", "status": {"$in": ["pending", "running"]}},
        {"bond_isin": 1, "celery_task_id": 1},
    ).to_list(length=None)
    return JSONResponse({
        doc["bond_isin"]: doc["celery_task_id"]
        for doc in active
        if doc.get("bond_isin") and doc.get("celery_task_id")
    })


@router.get("/valuation/task-status/{task_id}")
async def get_valuation_task_status(task_id: str):
    """Check Celery task state for a bond valuation job."""
    celery = get_celery_app()
    result = celery.AsyncResult(task_id)
    data = {"task_id": task_id, "state": result.state}
    if result.state == "SUCCESS":
        data["result"] = result.result
    elif result.state == "FAILURE":
        data["error"] = str(result.result)
    return JSONResponse(data)


@router.post("/backfill-verdict-ratings")
async def backfill_verdict_ratings():
    """One-time backfill: copy verdict_rating from query_results into bonds that have
    a report but are missing the verdict_rating field (generated before it was added)."""
    db = get_db()
    missing = await db.bonds.find(
        {"report_generated_at": {"$exists": True}, "verdict_rating": {"$exists": False}},
        {"isin": 1},
    ).to_list(length=None)

    updated, skipped = 0, 0
    for doc in missing:
        isin = doc.get("isin")
        if not isin:
            continue
        result = await db.query_results.find_one(
            {"output.isin": isin, "output.verdict": {"$exists": True}},
            sort=[("completed_at", -1)],
        )
        rating = (result or {}).get("output", {}).get("verdict", {}).get("rating") if result else None
        if rating:
            await db.bonds.update_one(
                {"isin": isin},
                {"$set": {"verdict_rating": str(rating).upper()}},
            )
            updated += 1
        else:
            skipped += 1

    return JSONResponse({"updated": updated, "skipped": skipped, "total_missing": len(missing)})


@router.get("/{isin}/report/download")
async def download_bond_report(isin: str):
    """Return the stored PDF report for a bond."""
    db = get_db()
    bond_doc = await db.bonds.find_one({"isin": isin})
    if not bond_doc or not bond_doc.get("report_pdf_b64"):
        return JSONResponse({"error": "No report found for this bond"}, status_code=404)

    pdf_bytes = base64.b64decode(bond_doc["report_pdf_b64"])
    issuer_raw = bond_doc.get("issuer_name", "") or isin
    safe_issuer = re.sub(r'[^\w]', '_', issuer_raw)
    safe_issuer = re.sub(r'_+', '_', safe_issuer).strip('_')
    filename = f"{safe_issuer}_{isin}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
