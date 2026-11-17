"""
News refresh orchestration — the one place that decides *when* things are fetched.

Two jobs, each run only as a Celery task:

  news    src.tasks.refresh_news    all RSS sources → news_items
          every user-chosen interval (News → ⚙ → Refresh news every)
  events  src.tasks.refresh_events  upcoming-events timelines → news_events
          every EVENTS_INTERVAL_HOURS (src/news_events.py)

Triggers that can ask for a run:

  scheduler  A thread inside the Celery worker (started on worker_ready) checks
             every SCHEDULER_TICK_SEC and queues a job once it's due. Works with
             the plain `celery -A src.tasks.celery_app worker` command (no beat
             needed), fires as soon as the worker starts, and within a tick of
             the machine waking from sleep.
  page       An open News page polls /api/news/status; if news is due and none
             is queued/running, it queues a refresh (backup for the scheduler).
  button     The Refresh button (news) / ↻ on a timeline (events) — forced.

Redis keys keep each job to one run at a time:
  <job>:refresh-queued   set when a task is queued (cleared when it starts)
  <job>:refresh-running  held while a task runs
  news:scheduler-heartbeat  refreshed by the worker's scheduler every tick —
                         if missing, the worker isn't running

Every attempt is recorded in the `news_refresh_log` collection (shown on the
Logs page) and in the Celery worker's console:
  queued → running → done | skipped (+ reason) | error (+ message)
An entry stuck at "queued" means no worker picked it up.
"""

import asyncio
import logging
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone

import redis
from pymongo import MongoClient

from .database import DB_NAME, MONGO_URL
from .news_service import PREFERENCE_USER_ID, normalise_preferences, refresh_news

log = logging.getLogger("news.refresh")

REDIS_URL = "redis://localhost:6379/0"
# Celery queue for refresh tasks. Default queue normally; override (with a worker
# started with -Q <name>) to run refreshes on an isolated worker.
NEWS_QUEUE = os.environ.get("NEWS_REFRESH_QUEUE", "celery")
HEARTBEAT_KEY = "news:scheduler-heartbeat"

SCHEDULER_TICK_SEC = 30
QUEUE_EXPIRES_SEC = 300      # a queued task not picked up within 5 min is dropped (and logged as such)
HEARTBEAT_TTL_SEC = SCHEDULER_TICK_SEC * 3
ERROR_BACKOFF_SEC = 120      # after a failed run the scheduler waits this long before retrying


def _news_interval(db) -> int:
    profile = db.user_profiles.find_one({"user_id": PREFERENCE_USER_ID}, {"news_preferences": 1}) or {}
    return normalise_preferences(profile.get("news_preferences"))["refresh_minutes"] * 60


def _events_interval(db) -> int:
    from .news_events import EVENTS_INTERVAL_HOURS
    return EVENTS_INTERVAL_HOURS * 3600


JOBS = {
    "news":   {"task": "src.tasks.refresh_news",   "meta_id": "last_refresh",        "interval": _news_interval,   "lock_sec": 300},
    "events": {"task": "src.tasks.refresh_events", "meta_id": "events_last_refresh", "interval": _events_interval, "lock_sec": 1200},
}


def _queued_key(job): return f"{job}:refresh-queued"
def _running_key(job): return f"{job}:refresh-running"
def _backoff_key(job): return f"{job}:refresh-backoff"


_mongo = None
_redis = None


def _db():
    global _mongo
    if _mongo is None:
        _mongo = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
    return _mongo[DB_NAME]


def _r():
    global _redis
    if _redis is None:
        _redis = redis.Redis.from_url(REDIS_URL, socket_timeout=3, decode_responses=True)
    return _redis


def _utc(dt):
    return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt


def _ago(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    minutes = seconds // 60
    return f"{minutes}m" if minutes < 120 else f"{minutes // 60}h {minutes % 60}m"


# ── When is a run due? ───────────────────────────────────────────────────

def due_info(job: str = "news", db=None) -> dict:
    """Last run time, interval and whether the job is due (sync, pymongo)."""
    db = db if db is not None else _db()
    spec = JOBS[job]
    meta = db.news_meta.find_one({"_id": spec["meta_id"]}, {"fetched_at": 1}) or {}
    interval = spec["interval"](db)
    now = datetime.now(timezone.utc)
    fetched_at = _utc(meta.get("fetched_at"))
    age = (now - fetched_at).total_seconds() if fetched_at else None
    return {
        "fetched_at": fetched_at,
        "age_sec": age,
        "interval_sec": interval,
        "due": age is None or age >= interval,
        "next_refresh_at": fetched_at + timedelta(seconds=interval) if fetched_at else now,
    }


# ── Queueing ─────────────────────────────────────────────────────────────

def queue_refresh(trigger: str, force: bool = False, reason: str = "", job: str = "news") -> dict:
    """Ask Celery to run a job. Returns {"queued": bool, "task_id"?, "reason"}.

    Won't queue if that job is already running or queued, so many triggers
    firing at once still produce a single run."""
    r = _r()
    if r.exists(_running_key(job)):
        return {"queued": False, "reason": f"a {job} refresh is already running"}
    if not r.set(_queued_key(job), trigger, nx=True, ex=QUEUE_EXPIRES_SEC):
        return {"queued": False, "reason": f"a {job} refresh is already queued"}

    from .celery_config import get_celery_app
    now = datetime.now(timezone.utc)
    task_id = str(uuid.uuid4())
    # Log first, then send — the task updates this entry and may start instantly
    _db().news_refresh_log.insert_one({
        "_id": task_id, "job": job, "trigger": trigger, "force": force, "status": "queued",
        "queued_at": now, "reason": reason,
    })
    try:
        get_celery_app().send_task(
            JOBS[job]["task"], task_id=task_id,
            kwargs={"trigger": trigger, "force": force, "queued_at": now.isoformat()},
            expires=QUEUE_EXPIRES_SEC, queue=NEWS_QUEUE,
        )
    except Exception as e:
        r.delete(_queued_key(job))
        _db().news_refresh_log.update_one({"_id": task_id}, {"$set": {"status": "error", "reason": f"could not queue: {e}"}})
        raise
    log.info("[%s] queued refresh — trigger=%s%s%s", job, trigger, " (forced)" if force else "", f" · {reason}" if reason else "")
    return {"queued": True, "task_id": task_id, "reason": reason}


# ── Celery task bodies ───────────────────────────────────────────────────

def _run_news(db):
    meta = _run_async(refresh_news)
    # Fill in images the feeds didn't send (og:image from the article page)
    from .news_images import enrich_images
    try:
        imgs = enrich_images(db)
        meta["summary"] = f"images: {imgs['found']} found for {imgs['tried']} image-less articles" + (f" · {imgs['note']}" if imgs.get("note") else "")
    except Exception as e:
        log.warning("[news] image backfill failed: %s", e)
    return meta


def _run_events(db):
    from .news_events import refresh_events
    return refresh_events(db)


_RUNNERS = {"news": _run_news, "events": _run_events}


def run_refresh(task_id: str, trigger: str, force: bool, queued_at: str | None, job: str = "news") -> dict:
    """Body of the refresh tasks: lock, check due, run, record."""
    r = _r()
    db = _db()
    started = datetime.now(timezone.utc)
    r.delete(_queued_key(job))
    waited = (started - datetime.fromisoformat(queued_at)).total_seconds() if queued_at else None
    entry = {"job": job, "trigger": trigger, "force": force, "started_at": started, "waited_sec": waited}
    if queued_at:
        entry["queued_at"] = datetime.fromisoformat(queued_at)

    def finish(status: str, **fields) -> dict:
        finished = datetime.now(timezone.utc)
        fields.update(status=status, finished_at=finished, duration_sec=round((finished - started).total_seconds(), 1))
        db.news_refresh_log.update_one({"_id": task_id}, {"$set": {**entry, **fields}}, upsert=True)
        return {"status": status, **{k: v for k, v in fields.items() if k not in ("finished_at", "fetched_at")}}

    if not r.set(_running_key(job), task_id, nx=True, ex=JOBS[job]["lock_sec"]):
        log.info("[%s] skipped (trigger=%s): another run in progress", job, trigger)
        return finish("skipped", reason=f"another {job} refresh was already running")

    try:
        info = due_info(job, db)
        if not force and not info["due"]:
            reason = f"not due — last run {_ago(info['age_sec'])} ago, next in {_ago(info['interval_sec'] - info['age_sec'])}"
            log.info("[%s] skipped (trigger=%s): %s", job, trigger, reason)
            return finish("skipped", reason=reason)

        db.news_refresh_log.update_one({"_id": task_id}, {"$set": {**entry, "status": "running"}}, upsert=True)
        since = f"last run {_ago(info['age_sec'])} ago" if info["age_sec"] is not None else "first run"
        log.info("[%s] refresh started (trigger=%s%s) · %s", job, trigger, ", forced" if force else "", since)

        meta = _RUNNERS[job](db)
        result = finish(
            "done", reason=" · ".join(x for x in [since, meta.get("summary")] if x), items=meta["item_count"],
            new_items=meta.get("new_count"), sources_failed=meta.get("sources_failed", []), fetched_at=meta["fetched_at"],
        )
        log.info("[%s] refresh done (trigger=%s) in %.1fs · %s new · %s total%s%s", job, trigger, result["duration_sec"],
                 meta.get("new_count"), meta["item_count"], f" · {meta['summary']}" if meta.get("summary") else "",
                 f" · failed: {', '.join(meta['sources_failed'])}" if meta.get("sources_failed") else "")
        return result
    except Exception as e:
        log.exception("[%s] refresh failed (trigger=%s)", job, trigger)
        r.set(_backoff_key(job), "1", ex=ERROR_BACKOFF_SEC)
        return finish("error", reason=f"{type(e).__name__}: {e}"[:500])
    finally:
        if r.get(_running_key(job)) == task_id:
            r.delete(_running_key(job))


def _run_async(fn):
    """Run news_service's async refresh with its own motor client and event loop."""
    from motor.motor_asyncio import AsyncIOMotorClient

    async def go():
        client = AsyncIOMotorClient(MONGO_URL)
        try:
            return await fn(client[DB_NAME])
        finally:
            client.close()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(go())
    finally:
        loop.close()


# ── Scheduler (runs inside the Celery worker) ────────────────────────────

_scheduler_stop = threading.Event()


def _scheduler_loop():
    log.info("[scheduler] started — checking every %ss whether news / events are due", SCHEDULER_TICK_SEC)
    while not _scheduler_stop.is_set():
        try:
            _r().set(HEARTBEAT_KEY, datetime.now(timezone.utc).isoformat(), ex=HEARTBEAT_TTL_SEC)
            for job in JOBS:
                info = due_info(job)
                if info["due"] and not _r().exists(_backoff_key(job)):
                    since = f"last run {_ago(info['age_sec'])} ago" if info["age_sec"] is not None else "never run"
                    queue_refresh("scheduler", reason=f"due — {since} (interval {_ago(info['interval_sec'])})", job=job)
        except Exception as e:
            log.warning("[scheduler] tick failed: %s", e)
        # Event.wait uses a monotonic clock: after the Mac wakes from sleep this
        # returns within one tick, and the next check sees the overdue run.
        _scheduler_stop.wait(SCHEDULER_TICK_SEC)


def start_scheduler():
    threading.Thread(target=_scheduler_loop, name="news-scheduler", daemon=True).start()


def stop_scheduler():
    _scheduler_stop.set()


# ── Status for the page / Logs ───────────────────────────────────────────

def _job_status(job: str) -> dict:
    r = _r()
    info = due_info(job)
    last = _db().news_refresh_log.find_one({"job": job} if job != "news" else {"job": {"$in": ["news", None]}},
                                           sort=[("queued_at", -1)]) or {}
    return {
        "fetched_at": info["fetched_at"].isoformat() if info["fetched_at"] else None,
        "interval_min": info["interval_sec"] // 60,
        "due": info["due"],
        "next_refresh_at": info["next_refresh_at"].isoformat(),
        "refreshing": bool(r.exists(_running_key(job))),
        "queued": bool(r.exists(_queued_key(job))),
        "last": {
            "id": last.get("_id"), "status": last.get("status"), "trigger": last.get("trigger"),
            "reason": last.get("reason"),
            "finished_at": _utc(last["finished_at"]).isoformat() if last.get("finished_at") else None,
        } if last else None,
    }


def status() -> dict:
    """Everything the News page needs to show refresh state (sync). Top level = news job."""
    return {**_job_status("news"), "scheduler_alive": bool(_r().exists(HEARTBEAT_KEY)), "events": _job_status("events")}


def last_unreachable() -> list:
    """Sources the most recent news refresh couldn't reach, with their errors."""
    meta = _db().news_meta.find_one({"_id": "last_refresh"}, {"sources": 1}) or {}
    return [{"name": s["name"], "error": s.get("error")} for s in meta.get("sources", []) if not s.get("ok")]


def refresh_log(limit: int = 100) -> list:
    rows = list(_db().news_refresh_log.find().sort("queued_at", -1).limit(limit))
    now = datetime.now(timezone.utc)
    for row in rows:
        row["id"] = row.pop("_id")
        row.setdefault("job", "news")
        # A task that sat in the queue past its expiry was never run
        if row.get("status") == "queued" and (now - _utc(row["queued_at"])).total_seconds() > QUEUE_EXPIRES_SEC:
            row["status"] = "not picked up"
            row["reason"] = (row.get("reason") or "") + " — no Celery worker took it within 5 min"
        for key in ("queued_at", "started_at", "finished_at", "fetched_at"):
            if row.get(key):
                row[key] = _utc(row[key]).isoformat()
    return rows
