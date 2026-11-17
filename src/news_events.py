"""
Upcoming-events timelines for the News page's event tabs (Finance, Hackathon,
Sports, Running, Triathlon).

Run by the Celery task ``src.tasks.refresh_events`` every EVENTS_INTERVAL_HOURS
(see src/news_refresh.py for scheduling and logging). For each tab:

  1. Structured sources where they exist: Devpost / Devfolio / Unstop hackathon
     APIs, and RacesIndia's race listings (running, trail, triathlon, swim,
     duathlon — plus the user's home city) parsed straight from its HTML.
  2. Event pages (race calendars, FOMC/RBI schedules, IPO and buyback lists)
     plus that tab's recent headlines are handed to Claude (Haiku), which
     returns only events whose dates are stated in the text.

Results replace that tab's rows in `news_events` — but only when the tab's run
succeeded, so a failed fetch never wipes a good timeline. Past events are
filtered out on read (end_date < today).
"""

import hashlib
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

from collections import defaultdict
from threading import BoundedSemaphore
from urllib.parse import quote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .claude_executor import run_claude_query
from .news_service import HEADERS, INDIA_HINTS as _INDIA_HINTS, NEWS_TABS

log = logging.getLogger("news.events")

EVENTS_INTERVAL_HOURS = 12
PAGE_TEXT_CHARS = 9000         # default per event page sent to Claude (override per page)
HEADLINES_PER_TAB = 80
HORIZON_DAYS = 400             # ignore events further out than this
PAGE_TIMEOUT = 20

BROWSER_HEADERS = {**HEADERS, "Accept": "text/html,application/json,*/*", "Accept-Language": "en-US,en;q=0.9"}

# Pages Claude reads for each tab: (name, url[, max chars]) — structured APIs are handled separately
EVENT_PAGES = {
    "Finance": [
        ("FOMC calendar", "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"),
        ("RBI monetary policy", "https://website.rbi.org.in/web/rbi/monetary-policy"),
        ("Bank of Japan meeting schedule", "https://www.boj.or.jp/en/mopo/mpmsche_minu/index.htm"),
        ("Chittorgarh IPO dashboard", "https://www.chittorgarh.com/ipo/ipo_dashboard.asp"),
        ("Chittorgarh buybacks", "https://www.chittorgarh.com/report/latest-buyback-issues-in-india/80/"),
    ],
    "Hackathon": [
        ("MLH events", "https://www.mlh.com/seasons/2027/events"),
    ],
    "Sports": [
        ("Cricbuzz cricket schedule", "https://www.cricbuzz.com/cricket-schedule/upcoming-series/all", 15000),
    ],
    "Running": [
        ("UTMB World Series", "https://utmb.world/utmb-world-series-events"),
        ("World Marathon Majors", "https://www.worldmarathonmajors.com/races"),
        ("RaceMart (India)", "https://racemart.in/events"),
    ],
    "Triathlon": [
        ("Indian Triathlon Federation calendar", "https://www.triathlonindia.com/events/event-calendar"),
        ("India swimathon list (Mumbai Sea Swimmers)", "https://www.mumbaiseaswimmers.com/indiaswimathonlist"),
        ("RaceMart (India)", "https://racemart.in/events"),
    ],
}

# Headlines worth showing Claude — keeps the prompt about schedulable things
HEADLINE_FILTER = {
    "Finance": r"\b(ipo|buyback|record date|rbi|mpc|repo|fed|fomc|rate (cut|hike|decision)|policy|listing|results|earnings|dividend|bonus|split|budget|gdp|inflation|cpi)\b",
    "Sports": r"\b(vs\.?|v |final|semi|match|series|tournament|cup|league|schedule|fixture|grand prix|open|championship|olympic|asian games|test|odi|t20)\b",
}

EVENT_GUIDE = {
    "Finance": "central-bank rate decisions (RBI MPC, US Fed FOMC — use kind 'Rate decision'), IPO open/close/listing "
               "dates (kind 'IPO'; name as '<Company> IPO opens' etc.), share buybacks and record dates (kind 'Buyback'), "
               "major results or economic data releases (kind 'Results' / 'Economic data'), budget dates (kind 'Policy')",
    "Hackathon": "hackathons and coding challenges (kind 'Hackathon')",
    "Sports": "matches, series, tournaments, finals and races with a stated date (kind 'Match' or 'Tournament')",
    "Running": "marathons, half marathons, 10Ks, ultra and trail races, in India and worldwide (kind 'Marathon', 'Trail' or 'Race')",
    "Triathlon": "triathlons, IRONMAN / 70.3 races, aquathlons, open-water swims and swimathons (kind 'Triathlon', 'IRONMAN', 'Swim')",
}


# ── Helpers ──────────────────────────────────────────────────────────────

def _event_id(tab: str, name: str, start: str) -> str:
    key = f"{tab}|{re.sub(r'[^a-z0-9]+', '', name.lower())[:60]}|{start}"
    return hashlib.sha1(key.encode()).hexdigest()[:20]


def _iso(d) -> str | None:
    if not d:
        return None
    if isinstance(d, str):
        d = d[:10]
        try:
            return date.fromisoformat(d).isoformat()
        except ValueError:
            return None
    return d.isoformat()


def _page_text(url: str, limit: int = PAGE_TEXT_CHARS) -> str:
    resp = requests.get(url, headers=BROWSER_HEADERS, timeout=PAGE_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "header", "footer", "nav"]):
        tag.decompose()
    # Keep real link targets next to their text ("OCEANMAN Ibiza [https://…]") so
    # Claude copies URLs instead of guessing them
    seen = set()
    for a in soup.find_all("a", href=True):
        href = urljoin(resp.url, a["href"].strip())
        if not href.startswith("http") or href in seen or href.rstrip("/") == resp.url.rstrip("/"):
            continue
        seen.add(href)
        a.append(f" [{href}]")
    return re.sub(r"\s+", " ", soup.get_text(" ")).strip()[:limit]


def _clean_event(raw: dict, tab: str, default_source: str, default_url: str) -> dict | None:
    name = str(raw.get("name") or "").strip()
    start = _iso(raw.get("start_date"))
    end = _iso(raw.get("end_date")) or start
    if not name or not start:
        return None
    if end < start:
        end = start
    today = date.today()
    if end < today.isoformat() or start > (today + timedelta(days=HORIZON_DAYS)).isoformat():
        return None
    location = str(raw.get("location") or "").strip() or None
    mode = str(raw.get("mode") or "").strip().lower().replace("_", "-")
    if mode not in ("online", "in-person"):
        mode = "online" if location and re.search(r"\b(online|virtual|remote)\b", location, re.I) else "in-person"
    # Online events can still belong to a country ("Online, India" — e.g. an IPO's online subscription)
    country = str(raw.get("country") or "").strip() or infer_country(location)
    stars, stars_reason = _rate(tab, name, str(raw.get("kind") or ""), location, raw)
    return {
        "_id": _event_id(tab, name, start),
        "stars": stars,
        "stars_reason": stars_reason,
        "tab": tab,
        "name": name[:140],
        "start_date": start,
        "end_date": end,
        "location": location,
        "country": country,
        "mode": mode,
        "kind": (str(raw.get("kind") or "").strip()[:30] or None),
        "description": (str(raw.get("description") or "").strip()[:240] or None),
        "url": (str(raw.get("url") or "").strip() or default_url),
        "source": (str(raw.get("source") or "").strip() or default_source),
    }


# ── Popularity rating (1-5 "stars", drawn as rings on the timeline) ──────

_TOP_COMPANIES = {"RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "BHARTIARTL", "SBIN", "ITC", "LT", "HINDUNILVR"}
_BIG_CITIES = re.compile(r"\b(mumbai|delhi|bengaluru|bangalore|hyderabad|chennai|kolkata|pune|ahmedabad)\b", re.I)
_RULES = {   # tab → [(regex on name+kind, stars, reason)] — first match wins
    "Finance": [
        (r"\b(rbi|reserve bank|fomc|federal reserve|fed)\b.*(rate|policy|decision|meeting)|monetary policy committee", 5, "central-bank rate decision"),
        (r"\b(bank of japan|boj|ecb|european central bank|bank of england|pboc)\b", 4, "major central bank"),
        (r"\b(cpi|inflation|gdp|payrolls|jobs report|union budget)\b", 4, "market-moving data"),
        (r"\bncd issue\b|bond issue", 1, "debt issue"),
        (r"\bsme ipo\b", 2, "SME IPO"),
        (r"\bipo\b", 4, "mainboard IPO"),
        (r"buy ?back", 3, "buyback"),
        (r"\b(bonus|split|dividend|demerger|rights)\b", 2, "corporate action"),
    ],
    "Running": [
        (r"\b(bmw berlin|bank of america chicago|tcs (new york city|london)|boston|london|berlin|chicago|new york city|tokyo|sydney) marathon\b", 5, "World Marathon Major"),
        (r"tata mumbai marathon|tcs world 10k|(vedanta|airtel) delhi half marathon|tata steel (world )?25k|utmb mont-?blanc", 5, "flagship race"),
        (r"by utmb|\butmb\b", 4, "UTMB World Series"),
        (r"(bengaluru|bangalore|midnight|hyderabad|ladakh|ahmedabad|pune|kochi spice coast|chennai) (international )?marathon", 4, "major city marathon"),
    ],
    "Triathlon": [
        (r"ironman world championship|world triathlon championship|olympic|asian games", 5, "championship"),
        (r"ironman 70\.3|70\.3", 4, "IRONMAN 70.3"),
        (r"\bironman\b", 5, "full IRONMAN"),
        (r"oceanman|bergman|tigerman|hillman", 3, "established race series"),
    ],
    "Sports": [
        (r"world cup|final\b|olympic|asian games|ipl|champions trophy|grand slam|wimbledon", 5, "major tournament"),
        (r"\b(women|u19|u-19|legends|champions|qualifier|unofficial)\b", 2, "secondary fixture"),
        (r"\bindia\b", 4, "India fixture"),
        (r"\b(test|odi|t20i)\b", 3, "international fixture"),
    ],
}


def _rate(tab: str, name: str, kind: str, location: str | None, raw: dict) -> tuple[int, str]:
    """1-5 popularity: structured signals and tab rules first, Claude's own rating as fallback."""
    sig = raw.get("signals") or {}
    text = f"{name} {kind}"
    claude = raw.get("stars")
    claude = int(claude) if isinstance(claude, (int, float)) or str(claude).isdigit() else None

    if tab == "Hackathon":
        prize, people = sig.get("prize_usd") or 0, sig.get("participants") or 0
        stars = 5 if prize >= 100_000 else 4 if prize >= 25_000 else 3 if prize >= 5_000 else 2 if prize > 0 else 1
        stars = max(stars, 4 if people >= 5_000 else 3 if people >= 500 else 2 if people >= 100 else 1)
        if sig.get("featured"):
            stars = min(5, stars + 1)
        if not sig and claude:
            return max(1, min(5, claude)), "Claude's estimate"
        return stars, " · ".join(x for x in [f"${prize:,.0f} prizes" if prize else "", f"{people:,} registered" if people else ""] if x) or "small / new"

    if tab == "Finance" and kind.lower() == "results":
        sym = sig.get("symbol")
        return (4, "results — top-10 company") if sym in _TOP_COMPANIES else (3, "results — NIFTY 50 company")

    for rx, stars, reason in _RULES.get(tab, []):
        if re.search(rx, text, re.I):
            if claude and claude > stars:   # e.g. a mega IPO Claude knows is huge
                return min(5, claude), f"{reason} · Claude rates it higher"
            return stars, reason

    if claude:
        # 5 is reserved for events a rule recognises as major — except Finance/Sports, where Claude's market sense helps
        cap = 5 if tab in ("Finance", "Sports") else 4
        return max(1, min(cap, claude)), "Claude's estimate"
    if tab == "Running":
        base = {"marathon": 3, "ultra": 3, "half marathon": 2, "trail": 2}.get(kind.lower(), 1)
        if location and _BIG_CITIES.search(location):
            base += 1
        if (sig.get("distances") or 0) >= 4:
            base += 1
        return min(base, 3), "by distance / city"   # 4-5 reserved for named major races
    if tab == "Triathlon":
        return (2 if kind.lower() == "triathlon" or (sig.get("distances") or 0) >= 4 else 1), "by format"
    if tab == "Finance":
        return 1, "board meeting"
    return 2, "default"


# ── Where is an event / where is the user? ───────────────────────────────

_US_STATES = set("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA "
                 "RI SC SD TN TX UT VT VA WA WV WI WY DC".split())
_COUNTRY_ALIASES = {"usa": "United States", "us": "United States", "u.s.": "United States", "united states of america": "United States",
                    "uk": "United Kingdom", "england": "United Kingdom", "scotland": "United Kingdom", "wales": "United Kingdom",
                    "uae": "United Arab Emirates"}


def infer_country(location: str | None) -> str | None:
    """Best-effort country from a free-text location ("Udupi, Karnataka" → India, "Austin, TX" → United States)."""
    if not location:
        return None
    # "Online, India" → India; plain "Online" → None
    location = re.sub(r"\b(online|virtual|remote)\b", "", location, flags=re.I).strip(" ,;/-")
    if not location:
        return None
    if _INDIA_HINTS.search(location):
        return "India"
    last = location.split(",")[-1].strip()
    if last.upper() in _US_STATES:
        return "United States"
    if last.lower() in _COUNTRY_ALIASES:
        return _COUNTRY_ALIASES[last.lower()]
    return last if re.fullmatch(r"[A-Za-z .'-]{3,}", last) else None


_TZ_COUNTRY = {"Asia/Kolkata": "India", "Asia/Calcutta": "India", "Asia/Dubai": "United Arab Emirates", "Asia/Singapore": "Singapore",
               "Europe/London": "United Kingdom", "America/New_York": "United States", "America/Los_Angeles": "United States",
               "America/Chicago": "United States", "Australia/Sydney": "Australia"}


def home_location(profile: dict | None, country_cities: list) -> dict:
    """The user's home city/country — from the profile city (matched against
    country_cities), else the profile timezone. Drives "domestic" filtering."""
    profile = profile or {}
    city = (profile.get("city") or "").strip()
    for doc in country_cities:
        if any(c.get("city", "").lower() == city.lower() for c in doc.get("cities", [])):
            return {"city": city, "country": doc["country_key"].replace("-", " ").title(), "source": f"profile city ({city})"}
    country = infer_country(city) if city else None
    if country:
        return {"city": city, "country": country, "source": f"profile city ({city})"}
    if not city:
        ip = _ip_location()
        if ip:
            return {"city": ip["city"], "country": ip["country"], "source": "your IP address"}
    tz = profile.get("timezone") or "Asia/Kolkata"
    # No city known → the page labels the filter just "Local"
    return {"city": city or None, "country": _TZ_COUNTRY.get(tz, "India"), "source": f"profile timezone ({tz})"}


_ip_cache: dict = {}


def _ip_location() -> dict | None:
    """City/country from this machine's public IP (ip-api.com). Only used when the
    profile has no city; cached for 12h. Returns None if the lookup fails."""
    now = datetime.now(timezone.utc)
    if _ip_cache.get("at") and now - _ip_cache["at"] < timedelta(hours=12):
        return _ip_cache.get("value")
    value = None
    try:
        data = requests.get("http://ip-api.com/json/?fields=status,city,country", timeout=3).json()
        if data.get("status") == "success" and data.get("city"):
            value = {"city": data["city"], "country": data["country"]}
    except Exception as e:
        log.info("[events] IP location lookup failed: %s", e)
    _ip_cache.update(at=now, value=value)
    return value


def home_location_sync(db) -> dict:
    profile = db.user_profiles.find_one({"user_id": "Kamal"}, {"city": 1, "timezone": 1})
    return home_location(profile, list(db.country_cities.find({}, {"country_key": 1, "cities.city": 1})))


# ── RacesIndia (structured HTML) ─────────────────────────────────────────

RACESINDIA = "https://www.racesindia.com"
# tab → [(path, pages, kind)] — "{city}" is the user's home city; kind None = derive from distances
RACESINDIA_PAGES = {
    "Running": [("/sport/run", 5, None), ("/sport/trail-running", 2, "Trail"), ("/city/{city}", 3, None)],
    "Triathlon": [("/sport/triathlon", 3, "Triathlon"), ("/sport/swim", 2, "Swim"), ("/sport/duathlon", 1, "Duathlon")],
}
_RI_DATE = re.compile(r"^(\d{1,2})(?:\s([A-Za-z]{3}))?(?:\s(\d{4}))?\s+to\s+(\d{1,2})\s([A-Za-z]{3})\s(\d{4})$|^(\d{1,2})\s([A-Za-z]{3})\s(\d{4})$")


def _ri_dates(text: str):
    m = _RI_DATE.match(text.strip())
    if not m:
        return None, None
    if m.group(7):
        d = date(int(m.group(9)), _MONTHS[m.group(8).lower()], int(m.group(7)))
        return d, d
    end = date(int(m.group(6)), _MONTHS[m.group(5).lower()], int(m.group(4)))
    start = date(int(m.group(3) or m.group(6)), _MONTHS[(m.group(2) or m.group(5)).lower()], int(m.group(1)))
    return start, end


def _run_kind(distances: list) -> str:
    kms = [float(x) for d in distances for x in re.findall(r"([\d.]+)\s*km", d)]
    if any(k > 43 for k in kms):
        return "Ultra"
    if any(41.5 <= k <= 42.5 for k in kms):
        return "Marathon"
    if any(20.9 <= k <= 21.2 for k in kms):
        return "Half marathon"
    return "Race"


def _racesindia_page(path: str, kind: str | None, running_only: bool) -> list:
    resp = requests.get(RACESINDIA + path, headers=BROWSER_HEADERS, timeout=30)
    if resp.status_code == 404 and "page=" in path:
        return []   # past the last page
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    out = []
    for a in soup.select("a[href*='/races/']"):
        title = a.find("h2")
        if not title:
            continue
        pills = [s.get_text(" ", strip=True) for s in a.find_all("span") if "rounded-md" in (s.get("class") or [])]
        plain = [s.get_text(" ", strip=True) for s in a.find_all("span") if not s.get("class") and s.get_text(strip=True)]
        start = end = None
        location = sport = None
        for i, text in enumerate(plain):
            start, end = _ri_dates(text)
            if start:
                location = plain[i - 1] if i > 0 else None
                sport = " ".join(plain[i + 1:]).lower() or None
                break
        if not start:
            continue
        if running_only and sport and "run" not in sport:
            continue   # city pages mix cycling, hybrid, etc.
        distances = [p for p in pills if "more" not in p.lower()]
        name = title.get_text(" ", strip=True)
        if kind in ("Triathlon", "Swim", "Duathlon"):   # same race can appear on several sport pages
            kind = "Swim" if re.search(r"swim|aqua", name, re.I) else "Duathlon" if re.search(r"duathlon", name, re.I) and not re.search(r"triathlon", name, re.I) else kind
        out.append({
            "name": name, "start_date": start, "end_date": end,
            "location": location, "country": "India", "mode": "in-person",
            "kind": kind or _run_kind(distances),
            "description": ("Distances: " + " · ".join(distances[:8])) if distances else None,
            "url": RACESINDIA + a["href"], "source": "RacesIndia",
            "signals": {"distances": len(distances)},
        })
    return out


def _racesindia(tab_key: str, home: dict) -> list:
    jobs = []
    for path, pages, kind in RACESINDIA_PAGES.get(tab_key, []):
        if "{city}" in path:
            if not home.get("city") or home.get("country") != "India":
                continue
            path = path.format(city=re.sub(r"[^a-z]+", "-", home["city"].lower()).strip("-"))
        for page in range(1, pages + 1):
            jobs.append((f"{path}?page={page}" if page > 1 else path, kind, "/city/" in path))
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda j: _racesindia_page(*j), jobs))
    return [e for r in results for e in r]


# ── Structured hackathon APIs ────────────────────────────────────────────

_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _parse_devpost_dates(text: str):
    """'Aug 31 - Oct 23, 2026' / 'Oct 10 - 12, 2026' / 'Sep 15, 2025 - Jan 10, 2026' → (start, end)"""
    parts = [p.strip() for p in text.split(" - ")]
    if len(parts) != 2:
        return None, None
    end_m = re.match(r"([A-Za-z]{3})?\s*(\d{1,2}),\s*(\d{4})", parts[1])
    start_m = re.match(r"([A-Za-z]{3})\s*(\d{1,2})(?:,\s*(\d{4}))?", parts[0])
    if not end_m or not start_m:
        return None, None
    end_year = int(end_m.group(3))
    end_month = _MONTHS[(end_m.group(1) or start_m.group(1)).lower()]
    start_month = _MONTHS[start_m.group(1).lower()]
    start_year = int(start_m.group(3)) if start_m.group(3) else (end_year if start_month <= end_month else end_year - 1)
    return date(start_year, start_month, int(start_m.group(2))), date(end_year, end_month, int(end_m.group(2)))


def _devpost() -> list:
    resp = requests.get("https://devpost.com/api/hackathons", params={"status[]": ["upcoming", "open"], "page": 1},
                        headers=BROWSER_HEADERS, timeout=PAGE_TIMEOUT)
    resp.raise_for_status()
    out = []
    for h in resp.json().get("hackathons", []):
        start, end = _parse_devpost_dates(h.get("submission_period_dates") or "")
        prize = re.sub(r"<[^>]+>", "", h.get("prize_amount") or "")
        themes = ", ".join(t["name"] for t in h.get("themes", [])[:3])
        loc = (h.get("displayed_location") or {}).get("location")
        prize_usd = float(re.sub(r"[^\d.]", "", prize) or 0) if "$" in prize else 0
        online = (h.get("displayed_location") or {}).get("icon") == "globe" or (loc or "").lower() == "online"
        out.append({
            "name": h["title"], "start_date": start, "end_date": end,
            "location": loc, "mode": "online" if online else "in-person", "kind": "Hackathon",
            "description": " · ".join(x for x in [h.get("organization_name"), themes, f"prizes {prize}" if prize.strip("$0") else ""] if x),
            "url": h.get("url"), "source": "Devpost",
            "signals": {"prize_usd": prize_usd, "participants": h.get("registrations_count") or 0, "featured": bool(h.get("featured"))},
        })
    return out


def _devfolio() -> list:
    resp = requests.post("https://api.devfolio.co/api/search/hackathons", json={"type": "application_open", "from": 0, "size": 30},
                         headers=BROWSER_HEADERS, timeout=PAGE_TIMEOUT)
    resp.raise_for_status()
    out = []
    for hit in resp.json().get("hits", {}).get("hits", []):
        h = hit["_source"]
        out.append({
            "name": h["name"], "start_date": (h.get("starts_at") or "")[:10], "end_date": (h.get("ends_at") or "")[:10],
            "location": "Online" if h.get("is_online") else ", ".join(x for x in [h.get("city"), h.get("country")] if x),
            "mode": "online" if h.get("is_online") else "in-person", "country": None if h.get("is_online") else h.get("country"),
            "kind": "Hackathon", "description": (h.get("tagline") or (h.get("desc") or "").split("\n")[0])[:200],
            "url": f"https://{h['slug']}.devfolio.co/" if h.get("slug") else "https://devfolio.co/hackathons",
            "source": "Devfolio",
            "signals": {"participants": int(h.get("participants_count") or 0)},
        })
    return out


def _unstop() -> list:
    resp = requests.get("https://unstop.com/api/public/opportunity/search-result",
                        params={"opportunity": "hackathons", "per_page": 30, "oppstatus": "open"},
                        headers=BROWSER_HEADERS, timeout=PAGE_TIMEOUT)
    resp.raise_for_status()
    out = []
    for h in resp.json().get("data", {}).get("data", []):
        end = (h.get("end_date") or "")[:10]   # Unstop gives the closing date only
        org = (h.get("organisation") or {}).get("name")
        out.append({
            "name": h["title"], "start_date": end, "end_date": end,
            "location": "Online" if h.get("region") == "online" else ((h.get("address_with_country_logo") or {}).get("city") or "In person"),
            "mode": "online" if h.get("region") == "online" else "in-person",
            "country": None if h.get("region") == "online" else "India",   # Unstop is an Indian platform
            "kind": "Hackathon", "description": f"{org} · closes {end}" if org else f"closes {end}",
            "url": h.get("seo_url"), "source": "Unstop",
            "signals": {"participants": int(h.get("registerCount") or 0), "views": int(h.get("viewsCount") or 0)},
        })
    return out


# ── NSE (National Stock Exchange) — IPOs, corporate actions, board meetings ──

# Large caps whose results dates are worth a timeline slot (NIFTY 50 — update when the index changes)
NIFTY50 = set("""ADANIENT ADANIPORTS APOLLOHOSP ASIANPAINT AXISBANK BAJAJ-AUTO BAJFINANCE BAJAJFINSV BEL BHARTIARTL CIPLA COALINDIA
DRREDDY EICHERMOT ETERNAL GRASIM HCLTECH HDFCBANK HDFCLIFE HEROMOTOCO HINDALCO HINDUNILVR ICICIBANK INDUSINDBK INFY ITC JIOFIN
JSWSTEEL KOTAKBANK LT M&M MARUTI NESTLEIND NTPC ONGC POWERGRID RELIANCE SBILIFE SBIN SHRIRAMFIN SUNPHARMA TATACONSUM TATAMOTORS
TATASTEEL TCS TECHM TITAN TRENT ULTRACEMCO WIPRO""".split())


def _nse_session():
    s = requests.Session()
    s.headers.update({**BROWSER_HEADERS, "Referer": "https://www.nseindia.com/"})
    s.get("https://www.nseindia.com/", timeout=PAGE_TIMEOUT)   # sets the cookies the API needs (page itself may 403)
    return s


def _nse_date(text: str):
    try:
        return datetime.strptime(text.strip().title(), "%d-%b-%Y").date()
    except (ValueError, AttributeError):
        return None


def _nse_ipos() -> list:
    s = _nse_session()
    rows = s.get("https://www.nseindia.com/api/ipo-current-issue", timeout=PAGE_TIMEOUT).json()
    rows += s.get("https://www.nseindia.com/api/all-upcoming-issues?category=ipo", timeout=PAGE_TIMEOUT).json()
    out = []
    for r in rows:
        series = (r.get("series") or "").upper()
        label = {"EQ": "IPO", "SME": "SME IPO", "DEBT": "NCD issue"}.get(series, "Issue")
        price = r.get("issuePrice") or r.get("priceRange")
        out.append({
            "name": f"{r['companyName']} {label}", "start_date": _nse_date(r.get("issueStartDate") or ""),
            "end_date": _nse_date(r.get("issueEndDate") or ""), "location": "NSE, India", "country": "India", "mode": "in-person",
            "kind": "IPO" if series in ("EQ", "SME") else "Bond issue",
            "description": " · ".join(x for x in [f"{label} open for subscription", f"price {price}" if price and price != "None" else None,
                                                   f"symbol {r.get('symbol')}" if r.get("symbol") else None] if x),
            "url": "https://www.nseindia.com/market-data/all-upcoming-issues-ipo", "source": "NSE",
            "signals": {"series": series},
        })
    return out


def _nse_corporate_actions() -> list:
    rows = _nse_session().get("https://www.nseindia.com/api/corporates-corporateActions?index=equities", timeout=PAGE_TIMEOUT).json()
    out = []
    for r in rows:
        subject = r.get("subject") or ""
        kind = next((k for k, rx in [("Buyback", "buy ?back"), ("Bonus", "bonus"), ("Split", "split"), ("Dividend", "dividend"),
                                     ("Demerger", "demerger"), ("Rights issue", "rights")] if re.search(rx, subject, re.I)), "Corporate action")
        out.append({
            "name": f"{r['comp']} — {kind} (ex-date)", "start_date": _nse_date(r.get("exDate") or ""), "end_date": None,
            "location": "NSE, India", "country": "India", "mode": "in-person", "kind": kind,
            "description": f"{subject}" + (f" · record date {r['recDate']}" if r.get("recDate") not in (None, "-") else ""),
            "url": f"https://www.nseindia.com/get-quotes/equity?symbol={quote(r.get('symbol') or '')}", "source": "NSE",
            "signals": {"symbol": r.get("symbol")},
        })
    return out


def _nse_board_meetings() -> list:
    rows = _nse_session().get("https://www.nseindia.com/api/event-calendar", timeout=PAGE_TIMEOUT).json()
    out = []
    for r in rows:
        purpose = r.get("purpose") or ""
        notable = re.search(r"buy ?back|bonus|split|dividend|fund raising", purpose, re.I)
        if r.get("symbol") not in NIFTY50 and not notable:
            continue   # results of small companies would swamp the timeline
        kind = "Results" if "result" in purpose.lower() else "Board meeting"
        out.append({
            "name": f"{r['company']} — {purpose}", "start_date": _nse_date(r.get("date") or ""), "end_date": None,
            "location": "NSE, India", "country": "India", "mode": "in-person", "kind": kind,
            "description": (r.get("bm_desc") or purpose)[:200],
            "url": f"https://www.nseindia.com/get-quotes/equity?symbol={quote(r.get('symbol') or '')}", "source": "NSE",
            "signals": {"symbol": r.get("symbol")},
        })
    return out


# ── OCEANMAN — the official site's public race list ─────────────────────
# oceanmanswim.com is a single-page app that reads races from its own public
# Supabase table with the read-only key in its JS bundle (exactly what every
# visitor's browser does). Unknown /races/<slug> paths still return 200 there,
# so links can't be verified after the fact — taking slugs from the source is
# the only reliable way to build them.

_oceanman_cfg: dict = {}


def _oceanman_config() -> tuple[str, str]:
    if not _oceanman_cfg:
        home = requests.get("https://oceanmanswim.com/", headers=BROWSER_HEADERS, timeout=PAGE_TIMEOUT).text
        bundle = re.search(r"/assets/index-[A-Za-z0-9_-]+\.js", home).group(0)
        js = requests.get("https://oceanmanswim.com" + bundle, headers=BROWSER_HEADERS, timeout=30).text
        _oceanman_cfg["url"] = re.search(r"https://[a-z0-9]+\.supabase\.co", js).group(0)
        _oceanman_cfg["key"] = max(re.findall(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", js), key=len)
    return _oceanman_cfg["url"], _oceanman_cfg["key"]


def _oceanman() -> list:
    url, key = _oceanman_config()
    try:
        resp = requests.get(f"{url}/rest/v1/events_public",
                            params={"select": "name,slug,location,country,start_date,end_date,distances,is_external,external_url,"
                                              "is_featured,is_active,environment", "is_active": "eq.true", "order": "start_date.asc"},
                            headers={"apikey": key, "Authorization": f"Bearer {key}"}, timeout=PAGE_TIMEOUT)
        resp.raise_for_status()
    except Exception:
        _oceanman_cfg.clear()   # bundle / key may have rotated — re-read next time
        raise
    out = []
    for r in resp.json():
        name = " ".join(w.capitalize() for w in re.split(r"\s*·\s*", r["name"])[0].split())
        dists = r.get("distances") or []
        dist_txt = " · ".join(str(d.get("name") or d) if isinstance(d, dict) else str(d) for d in dists[:6]) if isinstance(dists, list) else ""
        out.append({
            "name": name.upper() if re.match(r"(ultra)?oceanman", name, re.I) else f"OCEANMAN {name}",
            "start_date": r.get("start_date"), "end_date": r.get("end_date"),
            "location": ", ".join(x for x in [name, r.get("country")] if x), "country": r.get("country"), "mode": "in-person",
            "kind": "Swim", "description": " · ".join(x for x in [f"{r['environment']} swim" if r.get("environment") else "Open-water swim",
                                                                 dist_txt] if x),
            "url": r["external_url"] if r.get("is_external") and r.get("external_url") else f"https://oceanmanswim.com/races/{r['slug']}",
            "source": "OCEANMAN", "stars": 4 if r.get("is_featured") else 3,
        })
    return out


# ── IRONMAN — race cards on ironman.com/races ────────────────────────────

def _ironman() -> list:
    resp = requests.get("https://www.ironman.com/races", headers=BROWSER_HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    out = []
    for card in soup.select("div.highlighted-card-wrapper"):
        date_el, name_el, link = card.select_one(".date"), card.find(["h2", "h3"]), card.find("a", href=True)
        if not (date_el and name_el and link):
            continue
        try:
            start = datetime.strptime(date_el.get_text(strip=True), "%B %d, %Y").date()
        except ValueError:
            continue
        loc_el = card.select_one("span.label")
        legs = [x.get_text(" ", strip=True) for x in card.find_all(["div", "span"]) if x.find(True) is None]
        course = " ".join(legs).replace(" —", ",").strip(", ")
        course = re.sub(r"^.*?(Swim .*)$", r"\1", course)
        name = name_el.get_text(" ", strip=True)
        out.append({
            "name": name, "start_date": start, "end_date": start,
            "location": loc_el.get_text(" ", strip=True) if loc_el else None, "mode": "in-person",
            "kind": "IRONMAN", "description": course if course.startswith("Swim") else None,
            "url": urljoin("https://www.ironman.com/", link["href"]), "source": "IRONMAN",
        })
    return out


TAB_APIS = {
    "Hackathon": [("Devpost", _devpost), ("Devfolio", _devfolio), ("Unstop", _unstop)],
    "Triathlon": [("IRONMAN", _ironman), ("OCEANMAN", _oceanman)],
    "Finance": [("NSE IPOs", _nse_ipos), ("NSE corporate actions", _nse_corporate_actions), ("NSE board meetings", _nse_board_meetings)],
}


# ── Claude extraction ────────────────────────────────────────────────────

def _headlines(db, tab: dict) -> list:
    since = datetime.now(timezone.utc) - timedelta(days=3)
    docs = db.news_items.find(
        {"category": {"$in": tab["categories"]}, "published_at": {"$gte": since}},
        {"title": 1, "summary": 1, "source": 1, "published_at": 1, "link": 1},
    ).sort("score", -1).limit(400)
    rx = re.compile(HEADLINE_FILTER[tab["key"]], re.I) if tab["key"] in HEADLINE_FILTER else None
    lines, refs = [], {}
    for d in docs:
        text = f"{d['title']} — {(d.get('summary') or '')[:200]}"
        if rx and not rx.search(text):
            continue
        ref = f"H{len(lines) + 1}"
        refs[ref] = (d["link"], d["source"])
        lines.append(f"- {ref} [{d['source']}, {d['published_at']:%Y-%m-%d}] {text}")
        if len(lines) >= HEADLINES_PER_TAB:
            break
    return lines, refs


def _claude_events(tab_key: str, pages: list, headlines: list) -> list:
    today = date.today()
    blocks = [f"### Page P{i}: {name}\n{text}" for i, (name, url, text) in enumerate(pages, 1)]
    if headlines:
        blocks.append("### Recent headlines (ref [source, published date] title — summary)\n" + "\n".join(headlines))
    prompt = f"""Today is {today:%A, %d %B %Y}. You extract an upcoming-events timeline for the "{tab_key}" section of a news app.

Relevant events: {EVENT_GUIDE[tab_key]}.

Rules:
- Use ONLY dates stated in the text below (resolve relative dates like "next Friday" against the article's published date). Never guess or use outside knowledge.
- Only events ending on or after {today.isoformat()} and starting before {(today + timedelta(days=HORIZON_DAYS)).isoformat()}.
- One entry per event; merge duplicates. At most 40 events.
- description: one short factual line (max 20 words) — what it is, key detail (distance, prize, price band, rate expected…).
- ref: the id of the page (P1, P2…) or headline (H1, H2…) the event came from — always set it.
- url: ONLY if the event has its own link in [square brackets] on a page, copy that exact URL; otherwise leave it empty. Never construct, shorten or guess a URL.
- source: the page name or headline source it came from.
- stars: 1-5 — how big / popular / important the event is (5 = world-famous or market-moving, 3 = notable, 1 = minor or local).

Return ONLY a JSON array, no prose:
[{{"name": "...", "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD", "location": "City, Country or Online", "country": "full English country name, or null if online/unknown", "mode": "online or in-person", "kind": "...", "description": "...", "ref": "P1 or H3", "url": "", "source": "...", "stars": 3}}]
If there are no qualifying events, return [].

{chr(10).join(blocks)}
"""
    _claude_events.shown_text = prompt   # used to reject invented URLs
    result = run_claude_query(prompt, raw=True, timeout=300)
    if not result.get("success"):
        raise RuntimeError(result.get("error", "Claude call failed")[:300])
    match = re.search(r"\[.*\]", result["output"], re.S)
    if not match:
        raise RuntimeError(f"Claude returned no JSON array: {result['output'][:200]}")
    return json.loads(match.group(0))


# ── Link checking ────────────────────────────────────────────────────────

LINK_CHECK_TTL = timedelta(days=7)      # a link that worked (or was broken) is trusted this long
LINK_UNVERIFIED_TTL = timedelta(days=1) # slow / bot-blocked sites: don't re-hammer them every run
LINK_TIMEOUT = 8
LINK_PER_HOST = 3                       # be gentle — sites throttle bursts
_BROKEN_STATUSES = {404, 410}           # 401/403/429/5xx/timeouts = can't tell (bot-blocked etc.) → keep


def _check_url(url: str) -> str:
    """'ok' | 'broken' | 'unverified'"""
    try:
        r = requests.head(url, headers=BROWSER_HEADERS, timeout=LINK_TIMEOUT, allow_redirects=True)
        if r.status_code in (400, 403, 405, 501) or r.status_code >= 500:   # many sites reject HEAD
            r = requests.get(url, headers=BROWSER_HEADERS, timeout=LINK_TIMEOUT, allow_redirects=True, stream=True)
            r.close()
        if r.status_code in _BROKEN_STATUSES:
            return "broken"
        return "ok" if r.status_code < 400 else "unverified"
    except (requests.exceptions.ConnectionError, requests.exceptions.InvalidURL, requests.exceptions.MissingSchema) as e:
        # DNS failure / refused / invalid → the link can't work
        return "unverified" if isinstance(e, requests.exceptions.ConnectTimeout) else "broken"
    except Exception:
        return "unverified"


def _verify_links(db, events: list, fallback_for) -> dict:
    """Check every event URL (cached in news_link_checks); swap broken ones for a fallback.
    fallback_for(event) → URL. Returns counts for the refresh log."""
    now = datetime.now(timezone.utc)
    urls = {e["url"] for e in events if e.get("url")}
    cached = {d["_id"]: d["status"] for d in db.news_link_checks.find({"_id": {"$in": list(urls)}})
              if now - d["checked_at"].replace(tzinfo=timezone.utc) < (LINK_UNVERIFIED_TTL if d["status"] == "unverified" else LINK_CHECK_TTL)}
    todo = [u for u in urls if u not in cached]
    hosts = defaultdict(lambda: BoundedSemaphore(LINK_PER_HOST))

    def run(u):
        with hosts[urlparse(u).netloc]:
            return u, _check_url(u)

    with ThreadPoolExecutor(max_workers=12) as pool:
        fresh = dict(pool.map(run, todo))
    for u, st in fresh.items():
        db.news_link_checks.replace_one({"_id": u}, {"_id": u, "status": st, "checked_at": now}, upsert=True)
    status = {**cached, **fresh}

    replaced = 0
    for e in events:
        st = status.get(e.get("url")) if e.get("url") else "broken"
        if st == "broken":
            e["original_url"] = e.get("url")
            e["url"] = fallback_for(e)
            e["link_status"] = "replaced"
            replaced += 1
        else:
            e["link_status"] = st
    return {"checked": len(todo), "cached": len(cached), "replaced": replaced}


def _search_url(name: str) -> str:
    return "https://www.google.com/search?q=" + quote(name)


# ── One tab / all tabs ───────────────────────────────────────────────────

def _refresh_tab(db, tab: dict, home: dict) -> dict:
    key = tab["key"]
    events, failed, notes = [], [], []

    if key in RACESINDIA_PAGES:
        try:
            races = _racesindia(key, home)
            events += [(e, "RacesIndia", e["url"]) for e in races]
            notes.append(f"RacesIndia {len(races)}")
        except Exception as e:
            failed.append("RacesIndia")
            log.warning("[events] %s: RacesIndia failed: %s", key, e)

    for name, fn in TAB_APIS.get(key, []):
        try:
            events += [(e, name, e.get("url")) for e in fn()]
        except Exception as e:
            failed.append(name)
            log.warning("[events] %s: %s failed: %s", key, name, e)

    pages = []
    for name, url, *limit in EVENT_PAGES.get(key, []):
        try:
            pages.append((name, url, _page_text(url, *limit)))
        except Exception as e:
            failed.append(name)
            log.warning("[events] %s: page %s failed: %s", key, name, e)

    headlines, headline_refs = _headlines(db, tab)
    if pages or headlines:
        extracted = _claude_events(key, pages, headlines)   # raises → tab keeps its old events
        shown = _claude_events.shown_text
        page_refs = {f"P{i}": (url, name) for i, (name, url, _) in enumerate(pages, 1)}
        invented = 0
        for e in extracted:
            u = str(e.get("url") or "").strip()
            if u and u not in shown:   # Claude made it up (e.g. oceanmanswim.com/elsalvador) — never trust it
                u = ""
                invented += 1
            # No own link → the exact page / headline it came from, looked up by ref (never typed by Claude)
            ref_link, ref_source = (page_refs.get(str(e.get("ref") or "").strip())
                                    or headline_refs.get(str(e.get("ref") or "").strip()) or (None, None))
            e["url"] = u or ref_link
            if ref_source and not e.get("source"):
                e["source"] = ref_source
        events += [(e, "Claude", None) for e in extracted]
        if invented:
            notes.append(f"{invented} invented URLs dropped")
        notes.append(f"{len(pages)} pages + {len(headlines)} headlines → {len(extracted)} events")

    cleaned = {}
    for raw, source, url in events:
        ev = _clean_event(raw, key, source, url or "")
        if ev and ev["_id"] not in cleaned:
            cleaned[ev["_id"]] = ev

    # Broken links fall back to the page the event came from, else a web search
    page_urls = {name: url for name, url, *_ in EVENT_PAGES.get(key, [])}
    links = _verify_links(db, list(cleaned.values()),
                          lambda e: page_urls.get(e.get("source")) or _search_url(e["name"]))
    notes.append(f"links: {links['replaced']} broken replaced, {links['checked']} checked, {links['cached']} cached")
    replaced = links["replaced"]

    now = datetime.now(timezone.utc)
    docs = [{**e, "extracted_at": now} for e in cleaned.values()]
    db.news_events.delete_many({"tab": key})
    if docs:
        db.news_events.insert_many(docs)
    return {"tab": key, "count": len(docs), "failed": failed, "notes": notes, "links_replaced": replaced}


def refresh_events(db) -> dict:
    """Refresh every event tab (sync, pymongo db). Returns stats for the refresh log."""
    tabs = [t for t in NEWS_TABS if t.get("events")]
    home = home_location_sync(db)
    results, errors = [], []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(_refresh_tab, db, t, home): t["key"] for t in tabs}
        for future, key in futures.items():
            try:
                results.append(future.result())
            except Exception as e:
                errors.append(key)
                log.warning("[events] %s failed — keeping its previous events: %s", key, e)

    now = datetime.now(timezone.utc).replace(microsecond=0)
    total = db.news_events.count_documents({"end_date": {"$gte": date.today().isoformat()}})
    per_tab = {r["tab"]: r["count"] for r in results}
    failed = sorted({f for r in results for f in r["failed"]} | {f"{k} (whole tab)" for k in errors})
    db.news_meta.replace_one({"_id": "events_last_refresh"},
                             {"fetched_at": now, "item_count": total, "per_tab": per_tab, "sources_failed": failed}, upsert=True)
    replaced = sum(r.get("links_replaced", 0) for r in results)
    return {"fetched_at": now, "item_count": total, "new_count": None, "sources_failed": failed,
            "summary": ", ".join(f"{k} {v}" for k, v in per_tab.items()) + f" · links: {replaced} broken replaced"}
