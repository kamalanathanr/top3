"""Runs the trip planner for the Trips page's "Find Flights" button.

The planner itself (trip_planner.py) was built as a CLI: config in, a text
report out on stdout. The Trips page needs the same work driven from the
`trip_details` document and the answer as data, with progress visible while it
runs - each leg drives a real browser and takes ~20-40s, so a "Find Flights"
click is minutes of work, not a request/response.

So this wraps the planner:
  - config comes from trip_details (the countries and cities ticked on the page)
  - progress lines are streamed into the `flight_plans` document as they happen
  - the finished plan is stored as JSON (plan_to_dict), ready to render

One document per user, `_id`-less and keyed on user_id, so the page always has
"the latest search" to show without hunting through history.
"""

from datetime import datetime, timezone

from pymongo import MongoClient

from src.trip_planner import (
    Config, Searcher, count_searches, load_config_from_mongo, plan_combine,
    plan_to_dict, record_dead_routes_mongo, MONGO_URL, DB_NAME,
    TRIP_DETAILS_USER_ID,
)

# Keep the streamed log bounded - it is progress, not an audit trail.
MAX_LOG_LINES = 400

# Hard ceiling on scrapes per click. Each one drives a real browser for ~20-40s,
# so this is really a time budget: ~150 legs is around 90 minutes. A selection
# that needs more than this gets partial results rather than running all day.
MAX_SEARCHES_CEILING = 150

# Roughly how long one leg takes end to end, for the "this will take about N
# minutes" line the page shows before any fare comes back.
SECONDS_PER_SEARCH = 32


def _now():
    return datetime.now(timezone.utc).isoformat()


def _collection(client):
    return client[DB_NAME].flight_plans


def mark_queued(user_id=TRIP_DETAILS_USER_ID, task_id=None):
    """Called from the API the moment the button is clicked, so the page shows
    "queued" immediately instead of an empty panel until the worker picks it up.
    """
    client = MongoClient(MONGO_URL)
    try:
        _collection(client).update_one(
            {"user_id": user_id},
            {"$set": {
                "user_id": user_id,
                "status": "queued",
                "task_id": task_id,
                "queued_at": _now(),
                "started_at": None,
                "completed_at": None,
                "error": None,
                "log": [],
                "plan": None,
            }},
            upsert=True,
        )
    finally:
        client.close()


def run_flight_plan(user_id=TRIP_DETAILS_USER_ID, task_id=None):
    """Price every country/city combination the Trips page has ticked.

    Returns a small summary for the Celery result; the plan itself lives in
    the flight_plans document.
    """
    client = MongoClient(MONGO_URL)
    plans = _collection(client)

    def set_fields(**fields):
        plans.update_one({"user_id": user_id}, {"$set": fields}, upsert=True)

    def log(line):
        line = (line or "").rstrip()
        if not line:
            return
        plans.update_one(
            {"user_id": user_id},
            {"$push": {"log": {"$each": [line], "$slice": -MAX_LOG_LINES}}},
            upsert=True,
        )

    try:
        set_fields(user_id=user_id, status="running", task_id=task_id,
                   started_at=_now(), completed_at=None, error=None,
                   log=[], plan=None)

        cfg_dict = load_config_from_mongo(user_id)
        cfg = Config(cfg_dict)
        if not cfg.countries:
            set_fields(status="error", completed_at=_now(),
                       error="No countries ticked under "
                             "\"Countries Interested In Visiting\".")
            return {"status": "error", "reason": "no countries"}

        log(f"Planning from {cfg.source} for {cfg.travellers} traveller(s), "
            f"{cfg.start} to {cfg.end} (+/-{cfg.flex}d), "
            f"preference: {cfg.prefer}")
        log(f"Countries: {', '.join(cfg.countries)}")

        # The default budget in load_config_from_mongo() is a CLI-era guess and
        # is far smaller than a real combine run needs, which silently truncated
        # most itineraries. Size it from the legs this config actually visits.
        needed = count_searches(cfg)
        cfg.max_searches = min(needed, MAX_SEARCHES_CEILING)
        eta = round(cfg.max_searches * SECONDS_PER_SEARCH / 60)
        log(f"{needed} distinct flight searches needed; running "
            f"{cfg.max_searches} (about {eta} min).")
        if needed > MAX_SEARCHES_CEILING:
            log(f"! Capped at {MAX_SEARCHES_CEILING}. Narrow the countries or "
                f"cities, or lower the flexible days, to price every option.")
        set_fields(searches_planned=cfg.max_searches, eta_minutes=eta)

        searcher = Searcher(cfg.travellers, cfg.prefer, cfg.max_searches,
                            verbose=True, pause=cfg.pause_seconds,
                            dead_routes=cfg.no_flight_routes, log_sink=log)

        itins = plan_combine(cfg, searcher)
        plan = plan_to_dict(cfg, itins, searcher)

        # Routes MakeMyTrip does not operate are remembered on trip_details so
        # the next search does not pay for them again.
        record_dead_routes_mongo(searcher.new_dead, user_id)

        set_fields(status="done", completed_at=_now(), plan=plan,
                   searches_used=searcher.used,
                   rate_limited=bool(searcher.rate_limited))
        log(f"Done - {len([o for o in plan['options'] if o['complete']])} "
            f"itinerary option(s) priced using {searcher.used} search(es).")
        return {
            "status": "done",
            "options": len(plan["options"]),
            "searches_used": searcher.used,
        }
    except Exception as e:
        set_fields(status="error", completed_at=_now(),
                   error=f"{type(e).__name__}: {e}")
        return {"status": "error", "reason": str(e)}
    finally:
        client.close()
