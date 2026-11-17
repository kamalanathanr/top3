"""
User profile routes
"""

import asyncio
from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import JSONResponse
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ..database import get_db
from ..models import UserProfile, FamilyMember
from ..utils import serialize_objectid

router = APIRouter(prefix="/api/profile")


@router.get("/timezone")
async def get_user_timezone():
    db = get_db()
    profile = await db.user_profiles.find_one({"user_id": "Kamal"})
    user_tz = profile.get("timezone", "Asia/Kolkata") if profile else "Asia/Kolkata"
    return {"timezone": user_tz}


@router.get("/convert-timestamp")
async def convert_timestamp(utc_timestamp: str = Query(...)):
    db = get_db()
    profile = await db.user_profiles.find_one({"user_id": "Kamal"})
    user_tz = profile.get("timezone", "Asia/Kolkata") if profile else "Asia/Kolkata"
    try:
        utc_dt = datetime.fromisoformat(utc_timestamp.replace('Z', '+00:00'))
        if utc_dt.tzinfo is None:
            utc_dt = utc_dt.replace(tzinfo=timezone.utc)
        tz = ZoneInfo(user_tz)
        local_dt = utc_dt.astimezone(tz)
        formatted = local_dt.strftime("%d %b %Y %H:%M:%S %Z")
        return {"formatted": formatted, "timezone": user_tz}
    except Exception as e:
        return {"error": str(e), "original": utc_timestamp}


@router.get("/assets/market-price")
async def get_asset_market_price(
    symbol: str      = Query("", description="Ticker, ISIN, or scheme code"),
    asset_type: str  = Query("Stocks", description="Stocks | MF | ETFs | Bonds"),
):
    """Fetch live market price via Yahoo Finance (Stocks/ETFs) or AMFI (MF)."""
    if not symbol.strip():
        return JSONResponse({"error": "symbol is required"}, status_code=400)

    def _fetch():
        import requests
        symbol_clean = symbol.strip()

        if asset_type in ("Stocks", "ETFs"):
            ticker = symbol_clean.upper()
            if "." not in ticker:
                ticker += ".NS"
            url = (f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}"
                   "?interval=1d&range=1d")
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            r.raise_for_status()
            result = r.json()["chart"]["result"][0]
            meta = result["meta"]
            price = meta.get("regularMarketPrice") or meta.get("chartPreviousClose")
            name  = meta.get("longName") or meta.get("shortName") or ticker
            return {"price": price, "currency": meta.get("currency", "INR"),
                    "name": name, "symbol": ticker}

        elif asset_type == "MF":
            if symbol_clean.isdigit():
                # AMFI scheme code — free public API
                r = requests.get(f"https://api.mfapi.in/mf/{symbol_clean}", timeout=10)
                r.raise_for_status()
                data = r.json()
                nav  = float(data["data"][0]["nav"])
                name = data["meta"]["scheme_name"]
                return {"price": nav, "currency": "INR", "name": name, "symbol": symbol_clean}
            else:
                # Try Yahoo Finance with .NS suffix
                ticker = symbol_clean.upper()
                if "." not in ticker:
                    ticker += ".NS"
                r = requests.get(
                    f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d",
                    headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
                r.raise_for_status()
                result = r.json()["chart"]["result"][0]
                meta   = result["meta"]
                price  = meta.get("regularMarketPrice") or meta.get("chartPreviousClose")
                return {"price": price, "currency": "INR",
                        "name": meta.get("longName") or ticker, "symbol": ticker}

        else:
            return {"error": "Bond pricing requires manual entry", "manual": True}

    try:
        data = await asyncio.to_thread(_fetch)
        return JSONResponse(data)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("")
async def get_profile():
    db = get_db()
    profile = await db.user_profiles.find_one({"user_id": "Kamal"})
    if not profile:
        return {"profile": {
            "qualification": "", "family_details": "", "hobbies": [],
            "history": "", "interests": [], "strategies": "",
            "timezone": "Asia/Kolkata", "currency": "INR", "city": "Bengaluru",
            "date_of_birth": None, "risk_profile": "Moderate",
            "investment_planning_horizon": None,
            "corpus": None, "monthly_surplus": None,
            "monthly_income": None, "emergency_fund": None,
            "gold_grams": None, "real_estate_value": None,
            "financial_goals": [], "family_members": [], "existing_assets": [],
        }}
    return {"profile": serialize_objectid(profile)}


@router.get("/download")
async def download_profile():
    db = get_db()
    profile = await db.user_profiles.find_one({"user_id": "Kamal"})
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    filename = f"profile-Kamal-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    return JSONResponse(
        content=serialize_objectid(profile),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@router.post("/import")
async def import_profile(payload: dict = Body(...)):
    db = get_db()
    profile_doc = payload.get("profile") if "profile" in payload else payload
    if not isinstance(profile_doc, dict):
        raise HTTPException(status_code=400, detail="Invalid profile JSON")

    profile_doc = dict(profile_doc)
    profile_doc.pop("_id", None)
    profile_doc["user_id"] = "Kamal"
    profile_doc["updated_at"] = datetime.now(timezone.utc).isoformat()

    await db.user_profiles.update_one(
        {"user_id": "Kamal"},
        {"$set": profile_doc},
        upsert=True
    )
    return {"imported": True, "profile": serialize_objectid(profile_doc)}


@router.post("")
async def create_or_update_profile(request: UserProfile):
    db = get_db()
    profile_doc = {
        "user_id": "Kamal",
        "qualification": request.qualification,
        "family_details": request.family_details,
        "hobbies": request.hobbies,
        "history": request.history,
        "interests": request.interests,
        "strategies": request.strategies,
        "timezone": request.timezone,
        "currency": request.currency,
        "city": request.city,
        "risk_profile": request.risk_profile,
        "corpus": request.corpus,
        "monthly_surplus": request.monthly_surplus,
        "monthly_income": request.monthly_income,
        "emergency_fund": request.emergency_fund,
        "date_of_birth": request.date_of_birth,
        "investment_planning_horizon": request.investment_planning_horizon,
        "gold_grams": request.gold_grams,
        "real_estate_value": request.real_estate_value,
        "financial_goals": [
            g.model_dump() if hasattr(g, 'model_dump') else g.dict()
            for g in request.financial_goals
        ],
        "family_members": [
            m.model_dump() if hasattr(m, 'model_dump') else m.dict()
            for m in request.family_members
        ],
        "existing_assets": [
            a.model_dump() if hasattr(a, 'model_dump') else a.dict()
            for a in request.existing_assets
        ],
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    await db.user_profiles.update_one(
        {"user_id": "Kamal"},
        {"$set": profile_doc},
        upsert=True
    )
    return {"updated": True, "profile": profile_doc}
