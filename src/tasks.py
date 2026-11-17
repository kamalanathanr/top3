"""
Celery task definitions for query execution
"""

import asyncio
import json
from datetime import datetime, timezone
from bson.objectid import ObjectId
from yt_dlp import YoutubeDL

from celery.signals import worker_ready, worker_shutdown

from .celery_config import get_celery_app
from .database import get_db
from .claude_executor import run_claude_query
from .search_engine import search_web

celery_app = get_celery_app()

@celery_app.task(bind=True)
def execute_query(self, query_id: str):
    """Main task: Execute a query (AI or YouTube)"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(_execute_query_async(query_id))
    loop.close()
    return result

async def _execute_query_async(query_id: str):
    """Async execution of query (AI or YouTube)"""
    try:
        # Get database
        from motor.motor_asyncio import AsyncIOMotorClient
        mongo_url = "mongodb://admin:admin@localhost:27017/"
        client = AsyncIOMotorClient(mongo_url)
        db = client["top3"]

        # Fetch query document
        query_doc = await db.queries.find_one({"_id": ObjectId(query_id)})
        if not query_doc:
            return {"error": f"Query {query_id} not found"}

        # Check if paused or cancelled
        if query_doc.get("status") in ["paused", "cancelled"]:
            return {"status": query_doc.get("status"), "skipped": True}

        # Update status to running
        await db.queries.update_one(
            {"_id": ObjectId(query_id)},
            {"$set": {"status": "running", "started_at": datetime.now(timezone.utc).isoformat()}}
        )

        # Route based on query type
        query_type = query_doc.get("query_type", "ai")
        start_time = datetime.now(timezone.utc)

        if query_type == "youtube":
            # Execute YouTube search
            interest_text = query_doc.get("user_interests", [""])[0]
            try:
                ydl_opts = {'quiet': True, 'no_warnings': True, 'extract_flat': 'in_playlist'}
                with YoutubeDL(ydl_opts) as ydl:
                    search_query = f"ytsearch5:{interest_text}"
                    results = ydl.extract_info(search_query, download=False)
                    videos = []
                    for video in results.get('entries', []):
                        video_id = video.get('id')
                        videos.append({
                            'id': video_id,
                            'title': video.get('title'),
                            'url': f"https://www.youtube.com/watch?v={video_id}",
                            'duration': video.get('duration', 0)
                        })
                output = {"videos": videos}
                success = True
                error_msg = None
            except Exception as e:
                output = {}
                success = False
                error_msg = str(e)
        else:
            # Execute Claude AI query
            prompt = query_doc.get("ai_query")
            template_name = query_doc.get("template_name", "")
            if not prompt:
                return {"error": "No AI query template found"}

            # Perform web search for queries that need current information
            search_context = ""
            if any(keyword in template_name.lower() for keyword in ["ironman", "event", "upcoming", "sport"]):
                interests = query_doc.get("user_interests", [])
                if interests:
                    search_query = f"upcoming {interests[0]} events 2026"
                    search_context = search_web(search_query)
                    prompt = f"{prompt}\n\n** CRITICAL INSTRUCTION **\n✓ Web search permission has been PRE-APPROVED by the user.\n✓ You DO NOT need to ask for permission.\n✓ Use the following real-time event information:\n\n{search_context}\n\n✓ OUTPUT REQUIREMENTS:\n- ONLY return the numbered list format\n- NO introduction, explanation, or conversational text\n- NO permission requests or authorization messages\n- NO \"I appreciate\", \"I need\", \"Let me provide\" etc.\n- ONLY the structured event list"

            claude_result = run_claude_query(prompt)

            success = claude_result.get('success')
            error_msg = claude_result.get('error') if not success else None

            if success:
                try:
                    output = json.loads(claude_result.get('output', '{}'))
                except json.JSONDecodeError:
                    output = {"text": claude_result.get('output')}

        # Calculate time taken
        time_taken = (datetime.now(timezone.utc) - start_time).total_seconds()

        if not success:
            error_result = {
                "query_id": ObjectId(query_id),
                "user_id": query_doc.get("user_id"),
                "interests": query_doc.get("interests", query_doc.get("user_interests", [])),
                "output": {"error": error_msg},
                "time_taken_sec": time_taken,
                "completed_at": datetime.now(timezone.utc).isoformat()
            }
            await db.query_results.insert_one(error_result)

            await db.queries.update_one(
                {"_id": ObjectId(query_id)},
                {
                    "$set": {
                        "status": "done",
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                        "error": error_msg
                    }
                }
            )
            return {"error": error_msg}

        # Create result document
        result_doc = {
            "query_id": ObjectId(query_id),
            "user_id": query_doc.get("user_id"),
            "template_name": query_doc.get("template_name"),
            "interests": query_doc.get("interests", query_doc.get("user_interests", [])),
            "output": output,
            "time_taken_sec": time_taken,
            "completed_at": datetime.now(timezone.utc).isoformat()
        }

        insert_result = await db.query_results.insert_one(result_doc)

        await db.queries.update_one(
            {"_id": ObjectId(query_id)},
            {
                "$set": {
                    "status": "done",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "result_id": str(insert_result.inserted_id)
                }
            }
        )

        return {
            "query_id": query_id,
            "status": "done",
            "time_taken": time_taken,
            "result_id": str(insert_result.inserted_id)
        }

    except Exception as e:
        print(f"Error executing query {query_id}: {str(e)}")
        try:
            from motor.motor_asyncio import AsyncIOMotorClient
            mongo_url = "mongodb://admin:admin@localhost:27017/"
            client = AsyncIOMotorClient(mongo_url)
            db = client["top3"]

            await db.queries.update_one(
                {"_id": ObjectId(query_id)},
                {
                    "$set": {
                        "status": "error",
                        "error": str(e),
                        "completed_at": datetime.now(timezone.utc).isoformat()
                    }
                }
            )
        except:
            pass
        return {"error": str(e)}


@celery_app.task(bind=True)
def execute_investment_refresh(self, query_text: str):
    """Refresh investment_overview collection via Claude"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(_execute_investment_refresh_async(query_text))
    loop.close()
    return result


INSTRUMENT_SCHEMA = """{
  "instruments": [
    {
      "investment_name": "exact name as provided",
      "category": "FD|MF|Bond|ETF|Gold|Stock|Real Estate",
      "sub_type": "...",
      "return_profile": {
        "return_type": "GUARANTEED|MARKET-LINKED|HYBRID",
        "expected_annual_return": "X%–Y% p.a.",
        "return_frequency": "...",
        "income_stability": "STABLE|VOLATILE|VARIABLE"
      },
      "risk_safety": {
        "risk_level": "LOW|MEDIUM|HIGH",
        "capital_safety": "...",
        "credit_quality": "...",
        "volatility": "LOW|MEDIUM|HIGH"
      },
      "liquidity": { "lock_in_period": "...", "exit_ease": "...", "exit_cost": "..." },
      "taxation": { "tax_on_returns": "...", "tax_benefits": "..." },
      "entry_details": { "minimum_investment": "₹...", "mode": "..." },
      "realistic_expectation_summary": {
        "best_case_scenario_return": "...",
        "average_expected_return": "...",
        "worst_case_scenario": "..."
      }
    }
  ]
}"""

# Each batch: (label, list of exact investment names)
REFRESH_BATCHES = [
    ("FD", [
        "SBI Fixed Deposit (1–3 Years, General Public)",
        "HDFC Bank Fixed Deposit (1–3 Years, General Public)",
        "Suryoday Small Finance Bank FD (5 Years)",
    ]),
    ("Mutual Funds", [
        "Nippon India Large Cap Fund (Direct, Growth)",
        "Parag Parikh Flexi Cap Fund (Direct, Growth)",
        "ICICI Prudential Bluechip Fund (Direct, Growth)",
    ]),
    ("ETF & Gold", [
        "Nippon India ETF Nifty BeES (NSE: NIFTYBEES)",
        "SBI ETF Nifty 50 (NSE: SETFNIF50)",
        "Nippon India ETF Gold BeES (NSE: GOLDBEES)",
        "Sovereign Gold Bond (SGB) – Secondary Market",
    ]),
    ("Bonds", [
        "Sammaan Capital Limited Bond (~9.5%–9.6% coupon, 2035 maturity, AA category)",
        "Aye Finance Pvt. Ltd. NCD (~10%–11% coupon, ~2027 maturity, AA– category)",
        "Bharat Bond ETF – April 2030/2031/2032 (e.g., NSE: EBBETF0430)",
    ]),
    ("Stocks – Banking & IT", [
        "HDFC Bank Ltd. (NSE: HDFCBANK)",
        "ICICI Bank Ltd. (NSE: ICICIBANK)",
        "Tata Consultancy Services Ltd. (NSE: TCS)",
        "Infosys Ltd. (NSE: INFY)",
    ]),
    ("Stocks – Energy, FMCG & Auto", [
        "Reliance Industries Ltd. (NSE: RELIANCE)",
        "ITC Ltd. (NSE: ITC)",
        "Hindustan Unilever Ltd. (NSE: HINDUNILVR)",
        "Maruti Suzuki India Ltd. (NSE: MARUTI)",
        "Mahindra & Mahindra Ltd. (NSE: M&M)",
    ]),
    ("Stocks – Pharma", [
        "Sun Pharmaceutical Industries Ltd. (NSE: SUNPHARMA)",
        "Dr. Reddy's Laboratories Ltd. (NSE: DRREDDY)",
    ]),
    ("Real Estate", [
        "Bengaluru Real Estate – Sarjapur Road (Residential)",
        "Bengaluru Real Estate – Whitefield (Residential)",
        "Chennai Real Estate – OMR / Sholinganallur (Residential)",
        "Coimbatore Real Estate – Saravanampatti / Kalapatti (Residential)",
    ]),
]


def _build_batch_prompt(label: str, names: list, base_query: str) -> str:
    names_list = "\n".join(f"- {n}" for n in names)
    return (
        f"You are a financial data expert for India. Update the following {label} instruments "
        f"with CURRENT market data as of today.\n\n"
        f"Return ONLY valid JSON matching this exact schema — no explanation, no markdown:\n"
        f"{INSTRUMENT_SCHEMA}\n\n"
        f"Instruments to update:\n{names_list}"
    )


def _extract_instruments(raw: str) -> list:
    import re
    text = re.sub(r"^```[a-z]*\n?", "", raw.strip(), flags=re.MULTILINE)
    text = re.sub(r"```$", "", text.strip())
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            return json.loads(match.group()).get("instruments", [])
        except json.JSONDecodeError:
            pass
    return []


async def _execute_investment_refresh_async(query_text: str):
    from motor.motor_asyncio import AsyncIOMotorClient
    mongo_url = "mongodb://admin:admin@localhost:27017/"
    client = AsyncIOMotorClient(mongo_url)
    db = client["top3"]

    start_time = datetime.now(timezone.utc)
    executed_at = start_time.isoformat()

    # ── Create tracking query document ──
    q_result = await db.queries.insert_one({
        "user_id": "Kamal",
        "query_type": "investment_refresh",
        "template_name": "MarketPulse",
        "user_interests": ["market_pulse"],
        "interests": ["market_pulse"],
        "ai_query": f"[{len(REFRESH_BATCHES)} batches] " + query_text[:200],
        "status": "running",
        "created_at": executed_at,
        "started_at": executed_at,
    })
    query_id = q_result.inserted_id

    all_instruments = []
    batch_errors = []

    # ── Run one Claude call per batch ──
    for label, names in REFRESH_BATCHES:
        prompt = _build_batch_prompt(label, names, query_text)
        result = run_claude_query(prompt, raw=True, model="claude-sonnet-4-6")
        if result.get("success"):
            instruments = _extract_instruments(result.get("output", ""))
            all_instruments.extend(instruments)
            print(f"[investment_refresh] {label}: {len(instruments)} instruments updated")
        else:
            err = result.get("error", "unknown error")
            batch_errors.append(f"{label}: {err}")
            print(f"[investment_refresh] {label} FAILED: {err}")

    time_taken = (datetime.now(timezone.utc) - start_time).total_seconds()
    completed_at = datetime.now(timezone.utc).isoformat()

    # ── Upsert all collected instruments ──
    snapshot_at = executed_at
    for instrument in all_instruments:
        await db.market_pulse.update_one(
            {"investment_name": instrument.get("investment_name")},
            {"$set": {**instrument, "snapshot_at": snapshot_at, "time_taken_sec": time_taken}},
            upsert=True
        )

    status = "error" if (len(all_instruments) == 0 and batch_errors) else "done"
    summary = {
        "instruments_updated": len(all_instruments),
        "batches_total": len(REFRESH_BATCHES),
        "batch_errors": batch_errors,
        "time_taken_sec": time_taken,
    }

    # ── Store result for Results/Monitor pages ──
    res_insert = await db.query_results.insert_one({
        "query_id": query_id,
        "user_id": "Kamal",
        "template_name": "MarketPulse",
        "interests": ["market_pulse"],
        "output": summary,
        "time_taken_sec": time_taken,
        "completed_at": completed_at,
    })

    await db.queries.update_one(
        {"_id": query_id},
        {"$set": {"status": status, "completed_at": completed_at,
                  "result_id": str(res_insert.inserted_id),
                  **({"error": "; ".join(batch_errors)} if batch_errors else {})}}
    )

    await db.market_pulse_meta.update_one(
        {"type": "last_refresh"},
        {"$set": {"type": "last_refresh", "status": status,
                  "executed_at": executed_at, "time_taken_sec": time_taken,
                  "instruments_updated": len(all_instruments)}},
        upsert=True
    )
    return summary


ECONOMY_INDICATOR_QUERY_SCHEMA = """{
  "data_as_of": "Month Year",
  "indicators": [
    {"indicator": "Repo Rate", "value": "X%"},
    {"indicator": "CPI Inflation", "value": "X%"},
    {"indicator": "10Y Bond Yield", "value": "X%"},
    {"indicator": "NIFTY PE", "value": "XX.X"},
    {"indicator": "India VIX", "value": "XX.X"},
    {"indicator": "Gold Price Trend", "value": "Bullish/Bearish/Neutral — brief reason"},
    {"indicator": "USD/INR", "value": "XX.XX"},
    {"indicator": "Crude Oil (Brent)", "value": "USD XX per barrel"},
    {"indicator": "Gold 22K (per gram)", "value": "₹XXXXX"}
  ]
}"""


@celery_app.task(bind=True)
def execute_economy_indicator_refresh(self, query_text: str):
    """Refresh economy_indicator collection via Claude"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(_execute_economy_indicator_refresh_async(query_text))
    loop.close()
    return result


async def _execute_economy_indicator_refresh_async(query_text: str):
    import re as _re
    from motor.motor_asyncio import AsyncIOMotorClient
    from .economy_data_fetcher import fetch_current_indicators
    mongo_url = "mongodb://admin:admin@localhost:27017/"
    client = AsyncIOMotorClient(mongo_url)
    db = client["top3"]

    start_time = datetime.now(timezone.utc)
    executed_at = start_time.isoformat()

    print(f"[economy_indicator] Starting refresh")

    # ── Create tracking query document ──
    q_result = await db.queries.insert_one({
        "user_id": "Kamal",
        "query_type": "economy_indicator_refresh",
        "template_name": "EconomyIndicator",
        "user_interests": ["economy_indicator"],
        "interests": ["economy_indicator"],
        "ai_query": "Direct API fetch - no Claude query",
        "status": "running",
        "created_at": executed_at,
        "started_at": executed_at,
    })
    query_id = q_result.inserted_id
    print(f"[economy_indicator] Created tracking query {query_id}")

    # Read user's city for location-specific gold price
    profile_doc = await db.user_profiles.find_one({"user_id": "Kamal"})
    user_city = (profile_doc or {}).get("city", "Bengaluru") or "Bengaluru"

    # Fetch data directly from APIs (no Claude, no API keys needed)
    result = fetch_current_indicators(city=user_city)
    indicators = result.get("indicators", [])
    api_errors = result.get("errors")
    success = len(indicators) > 0

    print(f"[economy_indicator] API fetch result: success={success}, indicators={len(indicators)}")
    if api_errors:
        print(f"[economy_indicator] Fetch errors: {api_errors}")

    parse_error = None
    if not success:
        parse_error = "Failed to fetch indicators from APIs"
        print(f"[economy_indicator] {parse_error}")

    time_taken = (datetime.now(timezone.utc) - start_time).total_seconds()
    completed_at = datetime.now(timezone.utc).isoformat()

    for ind in indicators:
        await db.economy_indicator.update_one(
            {"indicator": ind.get("indicator")},
            {"$set": {**ind, "updated_at": executed_at}},
            upsert=True
        )

    status = "done" if indicators else "error"
    error_detail = result.get("error") or parse_error or ("No indicators parsed" if not indicators else None)

    # ── Store raw output in query_results for debugging ──
    res_insert = await db.query_results.insert_one({
        "query_id": query_id,
        "user_id": "Kamal",
        "template_name": "EconomyIndicator",
        "interests": ["economy_indicator"],
        "output": {
            "indicators_updated": len(indicators),
            "indicators": indicators,
            "fetch_success": success,
            "parse_error": parse_error,
            "error": error_detail,
        },
        "time_taken_sec": time_taken,
        "completed_at": completed_at,
    })

    await db.queries.update_one(
        {"_id": query_id},
        {"$set": {
            "status": status,
            "completed_at": completed_at,
            "result_id": str(res_insert.inserted_id),
            **({"error": error_detail} if error_detail else {}),
        }}
    )

    await db.economy_indicator_meta.update_one(
        {"type": "last_refresh"},
        {"$set": {
            "type": "last_refresh", "status": status,
            "executed_at": executed_at, "time_taken_sec": time_taken,
            "indicators_updated": len(indicators)
        }},
        upsert=True
    )

    print(f"[economy_indicator] Done: {len(indicators)} indicators, status={status}, took {time_taken:.1f}s")
    client.close()
    return {"indicators_updated": len(indicators), "time_taken_sec": time_taken, "status": status}


# Full-catalogue scrape walks every bond serially with retries; the global
# 30-minute task_time_limit killed it mid-run, so give this task its own budget.
@celery_app.task(bind=True, time_limit=4 * 60 * 60, soft_time_limit=4 * 60 * 60 - 300)
def execute_bonds_scrape(self, resume: bool = True):
    """Scrape bonds from indiabonds.com and store in MongoDB bonds collection."""
    from .bonds_scraper import scrape_all_to_mongo
    try:
        result = scrape_all_to_mongo(task_id=self.request.id, resume=resume)
        return result
    except Exception as e:
        print(f"[bonds_scrape] Error: {e}")
        return {"error": str(e)}


# ─── Bond Valuation ───────────────────────────────────────────────────────────

_BOND_RESEARCH_SCHEMA = """{
  "bond_name": "Full legal name",
  "isin": "ISIN code",
  "kpis": {
    "coupon_rate": "X.X% p.a. or N/A",
    "credit_rating": "Agency + Rating e.g. Acuité A-",
    "aum": "₹X,XXX Cr (as of date) or N/A",
    "gearing": "X.XXx or N/A",
    "maturity_date": "DD MMM YYYY or N/A",
    "payment_frequency": "Annual / Semi-annual / Quarterly / Monthly or N/A"
  },
  "company_overview": "2-3 sentence description of the company, its business and geography",
  "management": [
    {"name": "Person Name", "role": "Title", "background": "Brief 1-2 line background"}
  ],
  "financials": [
    {"metric": "AUM (Rs Cr)",      "fy22": "value or N/A", "fy23": "value or N/A", "fy24": "value or N/A", "fy25": "value or N/A"},
    {"metric": "Revenue (Rs Cr)",  "fy22": "N/A", "fy23": "N/A", "fy24": "N/A", "fy25": "N/A"},
    {"metric": "PAT (Rs Cr)",      "fy22": "N/A", "fy23": "N/A", "fy24": "N/A", "fy25": "N/A"},
    {"metric": "GNPA (%)",         "fy22": "N/A", "fy23": "N/A", "fy24": "N/A", "fy25": "N/A"},
    {"metric": "Gearing (x)",      "fy22": "N/A", "fy23": "N/A", "fy24": "N/A", "fy25": "N/A"}
  ],
  "milestones": [
    {"year": "YYYY or era", "milestone": "What happened (one line)"}
  ],
  "red_flags": [
    {"severity": "HIGH", "category": "Category name", "description": "Description"},
    {"severity": "MEDIUM", "category": "Category name", "description": "Description"},
    {"severity": "LOW", "category": "Category name", "description": "Description"}
  ],
  "credit_ratings": [
    {"agency": "Agency name", "instrument": "Instrument type", "rating": "Rating", "outlook": "Stable/Negative/Positive", "date": "Mon YYYY"}
  ],
  "bond_parameters": {
    "isin": "ISIN",
    "instrument": "e.g. Secured NCD",
    "coupon_rate": "X.X% p.a. or N/A",
    "credit_rating": "Agency Rating / Outlook",
    "maturity_date": "DD MMM YYYY or N/A",
    "security": "e.g. Secured (asset-backed) or Unsecured",
    "payment_frequency": "Frequency or N/A",
    "issuer_type": "e.g. NBFC Non-Deposit Taking",
    "face_value": "Rs X,XXX or N/A",
    "listing": "NSE / BSE or N/A"
  },
  "verdict": {
    "rating": "YES or NO or CAUTION",
    "rationale": "2-3 sentence justification covering growth, risk, and suitability"
  }
}"""


def _build_bond_research_prompt(bond_name: str, isin: str) -> str:
    return (
        f"You are a financial research analyst. Research the following Indian bond and return "
        f"ONLY a valid JSON object matching the schema below. "
        f"No code, no explanation, no markdown fences — output ONLY the raw JSON.\n\n"
        f"Bond Name: {bond_name}\nISIN: {isin}\n\n"
        f"Run these 5 web searches in parallel to gather data:\n"
        f'1. "{bond_name} ISIN {isin} coupon rate maturity date payment frequency bond"\n'
        f'2. "{bond_name} AUM revenue PAT NPA GNPA financial results FY2024 FY2025"\n'
        f'3. "{bond_name} founder management team background incorporated history"\n'
        f'4. "{bond_name} red flags RBI penalty regulatory risk concerns"\n'
        f'5. "{bond_name} Acuite CRISIL ICRA CARE credit rating rationale 2024 2025"\n\n'
        f"Populate all fields from search results. Mark unavailable data as N/A.\n"
        f"Verdict must be one of: YES / NO / CAUTION with 2-3 sentence justification.\n\n"
        f"Return ONLY this JSON (no other text):\n{_BOND_RESEARCH_SCHEMA}"
    )


@celery_app.task(bind=True)
def execute_bond_valuation(self, query_id: str):
    """Generate a PDF bond valuation report via Claude and store in MongoDB."""
    import base64 as _b64
    import re as _re
    from pymongo import MongoClient

    mongo_url = "mongodb://admin:admin@localhost:27017/"
    client = None
    start_time = datetime.now(timezone.utc)

    try:
        client = MongoClient(mongo_url)
        db = client["top3"]
        query_doc = db.queries.find_one({"_id": ObjectId(query_id)})
        if not query_doc:
            return {"error": f"Query {query_id} not found"}

        db.queries.update_one(
            {"_id": ObjectId(query_id)},
            {"$set": {"status": "running", "started_at": start_time.isoformat()}},
        )

        bond_name = query_doc.get("bond_name", "Unknown Bond")
        isin      = query_doc.get("bond_isin", "")

        # Always use our structured research prompt — never use the template text
        # (the template instructs Claude to generate a PDF, which it can't do directly)
        research_prompt = _build_bond_research_prompt(bond_name, isin)

        print(f"[bond_valuation] Researching {bond_name} ({isin})")
        claude_result = run_claude_query(
            research_prompt, raw=True, model="claude-sonnet-4-6",
            timeout=None, allowed_tools=["WebSearch"],
        )

        if not claude_result.get("success"):
            err = claude_result.get("error", "Claude error")
            db.queries.update_one(
                {"_id": ObjectId(query_id)},
                {"$set": {"status": "error", "error": err,
                          "completed_at": datetime.now(timezone.utc).isoformat()}},
            )
            client.close()
            return {"error": err}

        raw_output = claude_result.get("output", "")

        # Parse JSON from Claude's response
        data = None
        match = _re.search(r'\{[\s\S]*\}', raw_output)
        if match:
            try:
                data = json.loads(match.group())
            except json.JSONDecodeError:
                pass

        if not data:
            # Fallback: store the raw text in a minimal structure
            data = {
                "bond_name": bond_name, "isin": isin,
                "kpis": {}, "company_overview": raw_output[:500],
                "management": [], "financials": [], "milestones": [],
                "red_flags": [], "credit_ratings": [], "bond_parameters": {},
                "verdict": {"rating": "N/A", "rationale": "Data parsing failed — see raw output."},
            }

        pdf_bytes    = _generate_bond_report_pdf(bond_name, isin, data)
        pdf_b64      = _b64.b64encode(pdf_bytes).decode("utf-8")
        completed_at = datetime.now(timezone.utc).isoformat()
        time_taken   = (datetime.now(timezone.utc) - start_time).total_seconds()

        db.bonds.update_one(
            {"isin": isin},
            {"$set": {"report_pdf_b64": pdf_b64,
                      "report_generated_at": completed_at,
                      "report_query_id": query_id,
                      "verdict_rating": str(data.get("verdict", {}).get("rating") or "CAUTION").upper()}},
        )

        res_insert = db.query_results.insert_one({
            "query_id":       ObjectId(query_id),
            "user_id":        "Kamal",
            "template_name":  "Deep-Dive Analysis",
            "interests":      [bond_name],
            "output":         {"bond_name": bond_name, "isin": isin,
                               "report_generated": True, "verdict": data.get("verdict", {})},
            "time_taken_sec": time_taken,
            "completed_at":   completed_at,
        })

        db.queries.update_one(
            {"_id": ObjectId(query_id)},
            {"$set": {"status": "done", "completed_at": completed_at,
                      "result_id": str(res_insert.inserted_id)}},
        )

        print(f"[bond_valuation] Done for {isin} in {time_taken:.1f}s — verdict: {data.get('verdict', {}).get('rating', '?')}")
        client.close()
        return {"status": "done", "isin": isin, "time_taken_sec": time_taken}

    except Exception as e:
        print(f"[bond_valuation] Error: {e}")
        try:
            if client is not None:
                db.queries.update_one(
                    {"_id": ObjectId(query_id)},
                    {"$set": {"status": "error", "error": str(e),
                              "completed_at": datetime.now(timezone.utc).isoformat()}},
                )
        except Exception:
            pass
        if client is not None:
            client.close()
        return {"error": str(e)}


def _generate_bond_report_pdf(bond_name: str, isin: str, data: dict) -> bytes:
    import io
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

    # ── Colours ───────────────────────────────────────────────────────────
    NAVY      = colors.HexColor("#0A1F44")
    BLUE      = colors.HexColor("#1A4A8A")
    MIDBLUE   = colors.HexColor("#2563EB")
    LIGHTBLUE = colors.HexColor("#DBEAFE")
    PALEBLUE  = colors.HexColor("#EFF6FF")
    WHITE     = colors.white
    GRAY      = colors.HexColor("#374151")
    RED_BG    = colors.HexColor("#FEE2E2")
    YEL_BG    = colors.HexColor("#FEF9C3")
    GRN_BG    = colors.HexColor("#DCFCE7")
    RED_TXT   = colors.HexColor("#991B1B")
    YEL_TXT   = colors.HexColor("#854D0E")
    GRN_TXT   = colors.HexColor("#166534")

    # ── Page ──────────────────────────────────────────────────────────────
    M   = 13 * mm
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=M, rightMargin=M,
                            topMargin=M, bottomMargin=M)
    W = A4[0] - 2 * M

    # ── Style factory ─────────────────────────────────────────────────────
    def S(name, **kw):
        return ParagraphStyle(name, **kw)

    BASE    = S("base",   fontName="Helvetica",      fontSize=7.5, leading=10, textColor=GRAY)
    BASE_C  = S("baseC",  fontName="Helvetica",      fontSize=7.5, leading=10, textColor=GRAY,  alignment=1)
    BASE_J  = S("baseJ",  fontName="Helvetica",      fontSize=7.5, leading=10, textColor=GRAY,  alignment=4)
    BOLD    = S("bold",   fontName="Helvetica-Bold", fontSize=8,   leading=10, textColor=NAVY)
    BOLD_C  = S("boldC",  fontName="Helvetica-Bold", fontSize=8,   leading=10, textColor=NAVY,  alignment=1)
    BOLD_WC = S("boldWC", fontName="Helvetica-Bold", fontSize=8,   leading=10, textColor=WHITE, alignment=1)
    KPI_V   = S("kpiV",   fontName="Helvetica-Bold", fontSize=10,  leading=13, textColor=WHITE, alignment=1)
    KPI_L   = S("kpiL",   fontName="Helvetica",      fontSize=6.5, leading=8,  textColor=LIGHTBLUE, alignment=1)
    TITLE_H = S("titleH", fontName="Helvetica-Bold", fontSize=10,  leading=13, textColor=WHITE)
    TITLE_S = S("titleS", fontName="Helvetica",      fontSize=8,   leading=11, textColor=LIGHTBLUE, alignment=1)
    SEC_H   = S("secH",   fontName="Helvetica-Bold", fontSize=8,   leading=10, textColor=WHITE)
    VRD_L   = S("vrdL",   fontName="Helvetica-Bold", fontSize=8,   leading=10, textColor=NAVY)
    VRD_J   = S("vrdJ",   fontName="Helvetica",      fontSize=7.5, leading=10, textColor=GRAY,  alignment=4)
    FOOT    = S("foot",   fontName="Helvetica",      fontSize=6.5, leading=9,  textColor=colors.HexColor("#9CA3AF"), alignment=1)

    def p(text, style=None):
        safe = str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return Paragraph(safe, style or BASE)

    def section_hdr(title):
        t = Table([[p(title, SEC_H)]], colWidths=[W])
        t.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), NAVY),
            ("TOPPADDING",    (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING",   (0, 0), (-1, -1), 8),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
        ]))
        return t

    def grid_ts(nrows, hdr_color=MIDBLUE):
        cmds = [
            ("BACKGROUND", (0, 0), (-1, 0), hdr_color),
            ("GRID",       (0, 0), (-1, -1), 0.3, colors.HexColor("#BFDBFE")),
            ("TOPPADDING",    (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING",   (0, 0), (-1, -1), 5),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 5),
            ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ]
        for i in range(1, nrows):
            cmds.append(("BACKGROUND", (0, i), (-1, i), PALEBLUE if i % 2 == 1 else WHITE))
        return TableStyle(cmds)

    story = []
    kpis    = data.get("kpis", {})
    gen_date = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")

    # ── 1. Header ─────────────────────────────────────────────────────────
    hdr = Table(
        [[p(bond_name, TITLE_H), p(f"ISIN: {isin}  |  Generated: {gen_date}", TITLE_S)]],
        colWidths=[W * 0.6, W * 0.4],
    )
    hdr.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), NAVY),
        ("TOPPADDING",    (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING",   (0, 0), (-1, -1), 10),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 10),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(hdr)

    # ── 2. KPI Strip ──────────────────────────────────────────────────────
    kpi_items = [
        (kpis.get("coupon_rate",      "N/A"), "Coupon Rate"),
        (kpis.get("credit_rating",    "N/A"), "Credit Rating"),
        (kpis.get("aum",              "N/A"), "AUM"),
        (kpis.get("gearing",          "N/A"), "Gearing"),
        (kpis.get("maturity_date",    "N/A"), "Maturity"),
        (kpis.get("payment_frequency","N/A"), "Frequency"),
    ]
    kpi_t = Table(
        [[p(v, KPI_V) for v, _ in kpi_items],
         [p(l, KPI_L) for _, l in kpi_items]],
        colWidths=[W / 6] * 6,
    )
    kpi_t.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), BLUE),
        ("TOPPADDING",    (0, 0), (-1, 0), 7),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 2),
        ("TOPPADDING",    (0, 1), (-1, 1), 2),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 7),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("LINEAFTER",     (0, 0), (4, 1), 0.3, colors.HexColor("#4B74B2")),
    ]))
    story.append(kpi_t)
    story.append(Spacer(1, 2 * mm))

    # ── 3. Company Overview ───────────────────────────────────────────────
    overview = data.get("company_overview", "")
    if overview:
        story.append(section_hdr("COMPANY OVERVIEW"))
        ov = Table([[p(overview, BASE_J)]], colWidths=[W])
        ov.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), PALEBLUE),
            ("TOPPADDING",    (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING",   (0, 0), (-1, -1), 8),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
            ("BOX",           (0, 0), (-1, -1), 0.4, BLUE),
        ]))
        story.append(ov)
        story.append(Spacer(1, 2 * mm))

    # ── 4. Management ─────────────────────────────────────────────────────
    mgmt = data.get("management", [])
    if mgmt:
        story.append(section_hdr("FOUNDER & MANAGEMENT"))
        rows = [[p("Name", BOLD_WC), p("Role", BOLD_WC), p("Background", BOLD_WC)]]
        for m in mgmt:
            rows.append([p(m.get("name", "N/A"), BOLD),
                         p(m.get("role", "N/A"), BASE),
                         p(m.get("background", "N/A"), BASE_J)])
        mg = Table(rows, colWidths=[W * 0.22, W * 0.22, W * 0.56])
        mg.setStyle(grid_ts(len(rows)))
        story.append(mg)
        story.append(Spacer(1, 2 * mm))

    # ── 5. Financial Metrics ──────────────────────────────────────────────
    fins = data.get("financials", [])
    if fins:
        story.append(section_hdr("FINANCIAL METRICS"))
        rows = [[p("Metric", BOLD_WC), p("FY22", BOLD_WC), p("FY23", BOLD_WC),
                 p("FY24", BOLD_WC), p("FY25 / Latest", BOLD_WC)]]
        for f in fins:
            rows.append([p(f.get("metric", ""), BOLD),
                         p(f.get("fy22", "N/A"), BASE_C),
                         p(f.get("fy23", "N/A"), BASE_C),
                         p(f.get("fy24", "N/A"), BASE_C),
                         p(f.get("fy25", "N/A"), BASE_C)])
        fn = Table(rows, colWidths=[W * 0.30, W * 0.175, W * 0.175, W * 0.175, W * 0.175])
        fn.setStyle(grid_ts(len(rows)))
        story.append(fn)
        story.append(Spacer(1, 2 * mm))

    # ── 6. Milestones ─────────────────────────────────────────────────────
    milestones = data.get("milestones", [])
    if milestones:
        story.append(section_hdr("SUCCESS MILESTONES"))
        rows = [[p("Year", BOLD_WC), p("Milestone", BOLD_WC)]]
        for m in milestones:
            rows.append([p(m.get("year", ""), BOLD), p(m.get("milestone", ""), BASE_J)])
        ms = Table(rows, colWidths=[W * 0.12, W * 0.88])
        ms.setStyle(grid_ts(len(rows)))
        story.append(ms)
        story.append(Spacer(1, 2 * mm))

    # ── 7. Red Flags ──────────────────────────────────────────────────────
    red_flags = data.get("red_flags", [])
    if red_flags:
        story.append(section_hdr("RED FLAGS"))
        rows = [[p("Severity", BOLD_WC), p("Category", BOLD_WC), p("Description", BOLD_WC)]]
        sev_map = {"HIGH": (RED_BG, RED_TXT), "MEDIUM": (YEL_BG, YEL_TXT), "LOW": (GRN_BG, GRN_TXT)}
        rf_cmds = [
            ("BACKGROUND", (0, 0), (-1, 0), MIDBLUE),
            ("GRID",       (0, 0), (-1, -1), 0.3, colors.HexColor("#BFDBFE")),
            ("TOPPADDING",    (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING",   (0, 0), (-1, -1), 5),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 5),
            ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ]
        for i, rf in enumerate(red_flags, start=1):
            sev = str(rf.get("severity", "LOW")).upper()
            bg, tc = sev_map.get(sev, (PALEBLUE, GRAY))
            sev_sty = S(f"sev{i}", fontName="Helvetica-Bold", fontSize=7.5,
                        leading=10, textColor=tc, alignment=1)
            rows.append([p(sev, sev_sty), p(rf.get("category", ""), BASE),
                         p(rf.get("description", ""), BASE_J)])
            rf_cmds.append(("BACKGROUND", (0, i), (-1, i), bg))
        rf_t = Table(rows, colWidths=[W * 0.12, W * 0.22, W * 0.66])
        rf_t.setStyle(TableStyle(rf_cmds))
        story.append(rf_t)
        story.append(Spacer(1, 2 * mm))

    # ── 8. Credit Ratings ─────────────────────────────────────────────────
    ratings = data.get("credit_ratings", [])
    if ratings:
        story.append(section_hdr("CREDIT RATINGS"))
        rows = [[p("Agency", BOLD_WC), p("Instrument", BOLD_WC),
                 p("Rating", BOLD_WC), p("Outlook", BOLD_WC), p("Date", BOLD_WC)]]
        for r in ratings:
            rows.append([p(r.get("agency", "N/A"), BOLD),
                         p(r.get("instrument", "N/A"), BASE),
                         p(r.get("rating", "N/A"), BOLD_C),
                         p(r.get("outlook", "N/A"), BASE_C),
                         p(r.get("date", "N/A"), BASE_C)])
        cr = Table(rows, colWidths=[W * 0.20, W * 0.37, W * 0.15, W * 0.14, W * 0.14])
        cr.setStyle(grid_ts(len(rows)))
        story.append(cr)
        story.append(Spacer(1, 2 * mm))

    # ── 9. Bond Parameters ────────────────────────────────────────────────
    bp = data.get("bond_parameters", {})
    story.append(section_hdr(f"BOND PARAMETERS — {isin}"))
    pairs = [
        ("ISIN",              bp.get("isin", isin)),
        ("Instrument",        bp.get("instrument", "N/A")),
        ("Coupon Rate",       bp.get("coupon_rate", "N/A")),
        ("Credit Rating",     bp.get("credit_rating", "N/A")),
        ("Maturity Date",     bp.get("maturity_date", "N/A")),
        ("Security",          bp.get("security", "N/A")),
        ("Payment Frequency", bp.get("payment_frequency", "N/A")),
        ("Issuer Type",       bp.get("issuer_type", "N/A")),
        ("Face Value",        bp.get("face_value", "N/A")),
        ("Listing",           bp.get("listing", "N/A")),
    ]
    bp_rows, bp_cmds = [], [
        ("GRID",          (0, 0), (-1, -1), 0.3, colors.HexColor("#BFDBFE")),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING",   (0, 0), (-1, -1), 5),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 5),
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ("LINEAFTER",     (1, 0), (1, -1), 0.6, BLUE),
    ]
    for i in range(0, len(pairs), 2):
        left  = pairs[i]
        right = pairs[i + 1] if i + 1 < len(pairs) else ("", "")
        bp_rows.append([p(left[0], BOLD), p(left[1], BASE),
                        p(right[0], BOLD), p(right[1], BASE)])
        bp_cmds.append(("BACKGROUND", (0, i // 2), (-1, i // 2),
                        PALEBLUE if (i // 2) % 2 == 0 else WHITE))
    bp_t = Table(bp_rows, colWidths=[W * 0.18, W * 0.32, W * 0.18, W * 0.32])
    bp_t.setStyle(TableStyle(bp_cmds))
    story.append(bp_t)
    story.append(Spacer(1, 2 * mm))

    # ── 10. Verdict ───────────────────────────────────────────────────────
    verdict  = data.get("verdict", {})
    v_rating = str(verdict.get("rating", "CAUTION")).upper()
    v_text   = verdict.get("rationale", "")
    v_color_map = {
        "YES":     (colors.HexColor("#16A34A"), GRN_BG),
        "NO":      (colors.HexColor("#DC2626"), RED_BG),
        "CAUTION": (colors.HexColor("#D97706"), YEL_BG),
    }
    v_txt_c, v_bg = v_color_map.get(v_rating, (NAVY, LIGHTBLUE))
    VRD_V = S("vrdV", fontName="Helvetica-Bold", fontSize=15, leading=18,
               textColor=v_txt_c, alignment=1)
    vd = Table(
        [[p("VERDICT", VRD_L), p(v_rating, VRD_V), p(v_text, VRD_J)]],
        colWidths=[W * 0.10, W * 0.13, W * 0.77],
    )
    vd.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), LIGHTBLUE),
        ("BACKGROUND",    (1, 0), (1,  0),  v_bg),
        ("BOX",           (0, 0), (-1, -1), 1.0, BLUE),
        ("LINEAFTER",     (0, 0), (0,  0),  0.6, BLUE),
        ("LINEAFTER",     (1, 0), (1,  0),  0.6, BLUE),
        ("TOPPADDING",    (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 6),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(vd)

    # ── Footer ────────────────────────────────────────────────────────────
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph(
        f"Bond Valuation Report  •  {bond_name}  •  ISIN: {isin}  •  Generated: {gen_date}  •  Data sourced from public records",
        FOOT,
    ))

    doc.build(story)
    return buf.getvalue()




# ─── Trip Flight Planning ─────────────────────────────────────────────────────

@celery_app.task(bind=True, time_limit=4 * 60 * 60, soft_time_limit=4 * 60 * 60 - 300)
def execute_flight_plan(self, user_id: str = "Kamal"):
    """Price every country/city combination ticked on the Trips page.

    Long-running by nature: each flight leg drives a real Chrome against
    MakeMyTrip (~20-40s), so a search across a few countries is minutes. The
    same 4-hour ceiling as the bonds scrape.
    """
    from .flight_plan_service import run_flight_plan
    try:
        return run_flight_plan(user_id=user_id, task_id=self.request.id)
    except Exception as e:
        print(f"[flight_plan] Error: {e}")
        return {"error": str(e)}


# ── News refresh — see src/news_refresh.py for the full picture ──────────

@celery_app.task(bind=True, soft_time_limit=240, time_limit=300)
def refresh_news(self, trigger: str = "manual", force: bool = False, queued_at: str = None):
    """Fetch all news sources (the only place news is fetched). Skips if not due unless forced."""
    from .news_refresh import run_refresh
    return run_refresh(self.request.id, trigger, force, queued_at)


@celery_app.task(bind=True, soft_time_limit=900, time_limit=1000)
def refresh_events(self, trigger: str = "manual", force: bool = False, queued_at: str = None):
    """Rebuild the upcoming-events timelines (src/news_events.py). Skips if not due unless forced."""
    from .news_refresh import run_refresh
    return run_refresh(self.request.id, trigger, force, queued_at, job="events")


@worker_ready.connect
def _start_news_scheduler(**kwargs):
    # Checks every 30s and queues refresh_news when due — no `celery beat` needed
    from .news_refresh import start_scheduler
    start_scheduler()


@worker_shutdown.connect
def _stop_news_scheduler(**kwargs):
    from .news_refresh import stop_scheduler
    stop_scheduler()
