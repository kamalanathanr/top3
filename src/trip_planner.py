"""Trip planner - reads trip config, prices real flights, writes a report.

Every run prices flights live. The cache exists only so an interrupted or
budget-truncated run can resume without re-scraping what it already had; it is
deleted once a run completes cleanly, so the next run is fresh. Pass
--keep-cache to hold on to it (useful while iterating on the report format).

Config normally comes live from the `trip_details` MongoDB collection - the
same document the Trips page (templates/trips.html) saves via
/api/trip-details every time a traveller, date, or budget field changes there.
Resolves each candidate country to cities via trip_country_city_planner
(backed by the country_cities MongoDB collection), prices outbound and
return flights with flights.py, and ranks the options by total cost for the
whole party.

    python3 -m src.trip_planner
    python3 -m src.trip_planner --input src/trip_input_bkp.txt --dry-run

Every flight search drives a real browser and takes ~20-40s, so the config caps
the work with `date_flex`, `cities_per_country` and `max_searches`. Results are
cached in .trip_cache.json only for the duration of an unfinished run.

    Output goes to src/trip_output_<YYYYMMDD-HHMMSS>.txt, and src/trip_output.txt
    is refreshed to point at the same content so the latest is always there.
"""

import argparse
import itertools
import json
import os
import sys
import time
from datetime import datetime, timedelta

from pymongo import MongoClient

from src.flights import search_flights, RateLimited
from src.trip_country_city_planner import (
    get_cities, UnknownCountry, find_airport_code, build_itineraries, price_all,
    city_for_code, _key as country_key,
)

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUTPUT = os.path.join(HERE, "trip_output.txt")
CACHE_PATH = os.path.join(os.path.dirname(HERE), ".trip_cache.json")

MONGO_URL = "mongodb://admin:admin@localhost:27017/"
DB_NAME = "top3"
TRIP_DETAILS_USER_ID = "Kamal"

TRUTHY = {"yes", "true", "1", "y", "on"}

# Fallback only: the Trips page collects countries now ("Countries Interested
# In Visiting"), so these are used solely when nothing has been ticked there.
DEFAULT_COUNTRIES = ["Vietnam", "Cambodia"]
DEFAULT_VISITED = []
DEFAULT_NO_FLIGHT_ROUTES = []

# UI "Flight Preference" -> the sort values flights.py actually understands.
# flights.py has no "premium" sort, so it's mapped to the closest equivalent
# (non-stop: fewer, more comfortable connections).
FLIGHT_PREFERENCE_TO_SORT = {
    "cheapest": "cheapest",
    "fastest": "fastest",
    "nonstop": "non_stop",
    "premium": "non_stop",
}


def _airport_code_for(city_name):
    """City-name -> IATA code, via trip_country_city_planner's country_cities
    data. Falls back to BLR (Bengaluru) if the Starting Location on the Trips
    page is blank or isn't one of the cities that collection knows about.
    """
    return find_airport_code(city_name) or "BLR"


def load_config_from_mongo(user_id=TRIP_DETAILS_USER_ID):
    """Build the same cfg dict parse_input() would, but from the
    `trip_details` collection the Trips page writes to, filling in anything
    it doesn't collect with sensible defaults.
    """
    client = MongoClient(MONGO_URL)
    try:
        doc = client[DB_NAME].trip_details.find_one({"user_id": user_id}) or {}
    finally:
        client.close()

    today = datetime.now().date()
    default_start = today + timedelta(days=30)
    default_end = default_start + timedelta(days=7)

    traveller_names = doc.get("selected_traveller_names") or []
    travellers = len(traveller_names) or 1

    flight_pref = str(doc.get("flight_preference") or "cheapest").lower()
    prefer = FLIGHT_PREFERENCE_TO_SORT.get(flight_pref, "cheapest")

    # "Countries Interested In Visiting" ticks, stored as country_key slugs.
    countries = [c for c in (doc.get("interested_countries") or []) if c]
    if not countries:
        countries = list(DEFAULT_COUNTRIES)

    # "Cities To Visit" ticks: {country_key: [IATA codes]}. When a country has
    # ticks, only those cities are considered for it - and at this level the
    # best-connected of them is the one route that gets priced.
    city_filter = {
        country_key(k): [c for c in (v or []) if c]
        for k, v in (doc.get("selected_cities") or {}).items()
        if v
    }

    return {
        "source": _airport_code_for(doc.get("starting_location")),
        "travellers": str(travellers),
        "start_date": doc.get("date_from") or default_start.isoformat(),
        "end_date": doc.get("date_to") or default_end.isoformat(),
        # First-level pass: the two dates as entered. Flexing them to hunt a
        # cheaper combination is the job of the Optimize step, not this one -
        # here the question is only "does this route exist and roughly what
        # does it cost". `flexible_days` from the page is deliberately not
        # read; Optimize will use it.
        "date_flex": "0",
        "mode": "combine",
        "round_trip": "yes",
        # Depart and return on the entered dates, so every option is the same
        # to-and-fro trip and the totals are comparable.
        "fill_window": "yes",
        # One route per country at this level: home -> its best-connected
        # city and back. Multi-city routing within a country is an Optimize
        # concern.
        "cities_per_country": "1",
        "prefer": prefer,
        "pause_seconds": "3",
        "max_searches": "10",
        "countries": countries,
        "city_filter": city_filter,
        "visited": list(DEFAULT_VISITED),
        "no_flight_routes": list(doc.get("no_flight_routes") or DEFAULT_NO_FLIGHT_ROUTES),
    }


def record_dead_routes_mongo(pairs, user_id=TRIP_DETAILS_USER_ID):
    """Same idea as record_dead_routes(), but appended to trip_details in
    MongoDB instead of rewriting a config file."""
    if not pairs:
        return []
    entries = sorted({f"{a}-{b}" for a, b in pairs})
    client = MongoClient(MONGO_URL)
    try:
        db = client[DB_NAME]
        existing = set((db.trip_details.find_one({"user_id": user_id}) or {})
                       .get("no_flight_routes") or [])
        added = sorted(set(entries) - existing)
        if added:
            db.trip_details.update_one(
                {"user_id": user_id},
                {"$addToSet": {"no_flight_routes": {"$each": added}}},
                upsert=True,
            )
        return added
    finally:
        client.close()


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def parse_input(path):
    """Parse the key: value + '- item' list format of trip_input.txt."""
    cfg, current_list = {}, None
    with open(path) as fh:
        for raw in fh:
            line = raw.split("#", 1)[0].rstrip()
            if not line.strip():
                continue
            if line.lstrip().startswith("- "):
                if current_list is None:
                    raise ValueError(f"List item outside a key: {raw.strip()!r}")
                cfg[current_list].append(line.lstrip()[2:].strip())
                continue
            if ":" not in line:
                raise ValueError(f"Unparseable line: {raw.strip()!r}")
            key, val = line.split(":", 1)
            key, val = key.strip(), val.strip()
            if val:
                cfg[key], current_list = val, None
            else:
                cfg[key], current_list = [], key
    return cfg


NO_ROUTE_HEADER = """
# Airport pairs MakeMyTrip does not operate. Discovered automatically when a
# search returns "Sorry, Flights Not Found", and skipped on later runs to save
# time. Delete a line to search that pair again (useful if a seasonal route
# reopens).
no_flight_routes:
"""


def record_dead_routes(path, pairs):
    """Append newly-found dead routes to the input file, in place.

    Rewrites only the no_flight_routes block, leaving every other line - and
    all comments - exactly as the user wrote them.
    """
    if not pairs:
        return []
    with open(path) as fh:
        lines = fh.read().splitlines()

    existing, start, end = set(), None, None
    for i, line in enumerate(lines):
        if line.strip().startswith("no_flight_routes:"):
            start = i
            end = i + 1
            while end < len(lines) and (lines[end].lstrip().startswith("- ")
                                        or not lines[end].strip()):
                if lines[end].lstrip().startswith("- "):
                    existing.add(lines[end].lstrip()[2:].strip().upper())
                    end += 1
                elif lines[end].strip():
                    break
                else:
                    break
            break

    added = sorted({f"{a}-{b}" for a, b in pairs} - existing)
    if not added:
        return []

    entries = [f"  - {r}" for r in sorted(existing | set(added))]
    if start is None:
        lines.extend(NO_ROUTE_HEADER.rstrip("\n").splitlines() + entries)
    else:
        lines[start:end] = ["no_flight_routes:"] + entries

    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return added


def _int(cfg, key, default=None):
    v = cfg.get(key, "")
    if isinstance(v, list) or v == "":
        return default
    return int(v)


def _date(s):
    return datetime.strptime(s.strip(), "%Y-%m-%d").date()


class Config:
    def __init__(self, cfg):
        self.source = str(cfg.get("source", "BLR")).upper()
        self.travellers = _int(cfg, "travellers", 1) or 1
        self.start = _date(cfg["start_date"])
        self.end = _date(cfg["end_date"])
        self.flex = _int(cfg, "date_flex", 0) or 0
        self.min_nights = _int(cfg, "min_nights")
        self.max_nights = _int(cfg, "max_nights")
        self.mode = str(cfg.get("mode", "compare")).lower()
        self.round_trip = str(cfg.get("round_trip", "yes")).lower() in TRUTHY
        self.cities_per_country = _int(cfg, "cities_per_country", 1) or 1
        self.prefer = str(cfg.get("prefer", "cheapest")).lower()
        self.max_searches = _int(cfg, "max_searches", 40) or 40
        self.pause_seconds = _int(cfg, "pause_seconds", 3) or 0
        self.countries = [c for c in cfg.get("countries", []) if c]
        # {country_key: [IATA codes]} - the cities ticked on the Trips page.
        self.city_filter = dict(cfg.get("city_filter") or {})
        # Stretch every itinerary across the whole window, so each one flies
        # out and back on the two dates entered rather than ending early.
        self.fill_window = str(cfg.get("fill_window", "no")).lower() in TRUTHY
        self.visited = {v.strip().lower() for v in cfg.get("visited", []) if v}
        # Airport pairs MakeMyTrip does not fly, learned from earlier runs.
        self.no_flight_routes = set()
        for r in cfg.get("no_flight_routes", []) or []:
            pair = r.replace("->", "-").strip().upper()
            if "-" in pair:
                a, b = pair.split("-", 1)
                self.no_flight_routes.add((a.strip(), b.strip()))

        if self.end < self.start:
            raise ValueError("end_date is before start_date")
        if self.mode not in ("compare", "route", "combine"):
            raise ValueError(
                f"mode must be compare|route|combine, got {self.mode!r}")

    @property
    def nights(self):
        return (self.end - self.start).days

    def outbound_dates(self):
        return [self.start + timedelta(days=d)
                for d in range(-self.flex, self.flex + 1)]

    def return_dates(self):
        return [self.end + timedelta(days=d)
                for d in range(-self.flex, self.flex + 1)]

    def valid_pair(self, out_date, ret_date):
        """Respect min/max nights when flexing the dates."""
        nights = (ret_date - out_date).days
        if nights < 1:
            return False
        if self.min_nights is not None and nights < self.min_nights:
            return False
        if self.max_nights is not None and nights > self.max_nights:
            return False
        return True


# --------------------------------------------------------------------------
# Flight lookup, cached
# --------------------------------------------------------------------------

class Searcher:
    """Wraps search_flights with a disk cache and a hard call budget."""

    def __init__(self, travellers, prefer, budget, dry_run=False, verbose=True,
                 pause=0, dead_routes=None, log_sink=None):
        self.travellers = travellers
        self.prefer = prefer
        self.budget = budget
        self.pause = pause
        self.used = 0
        self.exhausted = False
        self.dry_run = dry_run
        self.verbose = verbose
        # Called with each finished progress line, so a background run can
        # stream its progress somewhere other than stderr (the Trips page
        # polls what the Celery task writes to MongoDB).
        self.log_sink = log_sink
        self._partial = ""
        self.cache = {}
        # Every leg ever looked up this run, for the "all options" report.
        self.seen = []
        # (country, reason) for anything not priced.
        self.skipped = []
        self.failures = 0
        # legs that raised, as opposed to legitimately having no flights
        self.errors = set()
        self.rate_limited = False
        # Known-dead pairs (from config) plus any found during this run.
        self.dead_routes = set(dead_routes or ())
        self.new_dead = set()
        self.skipped_dead = 0
        if os.path.exists(CACHE_PATH):
            try:
                with open(CACHE_PATH) as fh:
                    self.cache = json.load(fh)
            except Exception:
                self.cache = {}

    def _save(self):
        try:
            with open(CACHE_PATH, "w") as fh:
                json.dump(self.cache, fh)
        except Exception:
            pass

    def cheapest(self, src, dst, date):
        """Cheapest single flight for a leg, or None."""
        if (src, dst) in self.dead_routes:
            self.skipped_dead += 1
            self._log(f"  - {src}->{dst} skipped (no route; recorded as a dead "
                      f"route)")
            return None

        key = f"{src}-{dst}-{date}-{self.travellers}-{self.prefer}"
        if key in self.cache:
            self._record(src, dst, date, self.cache[key], cached=True)
            return self.cache[key]

        if self.dry_run:
            self._log(f"  [dry-run] would search {src}->{dst} {date}")
            return None
        if self.rate_limited:
            return None
        if self.used >= self.budget:
            self.exhausted = True
            self._log(f"  ! search budget ({self.budget}) exhausted, skipping "
                      f"{src}->{dst} {date}")
            return None

        if self.used and self.pause:
            time.sleep(self.pause)
        self.used += 1
        self._log(f"  [{self.used}/{self.budget}] {src}->{dst} {date} ...", end="")
        started = time.time()
        try:
            results = search_flights(
                src, dst, date, travellers=self.travellers,
                limit=1, sort=self.prefer, details=True,
            )
        except RateLimited as e:
            # Pointless to keep hammering - stop and let the cache resume.
            self.rate_limited = True
            self.failures += 1
            self._log(f" RATE LIMITED - stopping")
            self.cache.pop(key, None)
            self._save()
            return None
        except Exception as e:
            self.failures += 1
            self._log(f" failed ({type(e).__name__})")
            self.errors.add((src, dst, date))
            self.cache[key] = None
            self._save()
            self._record(src, dst, date, None)
            return None

        best = results[0] if results else None
        if best:
            self._log(f" {best['currency']} {best['price']:,} "
                      f"({best['airline']}, {time.time() - started:.0f}s)")
        elif getattr(search_flights, "last_status", "") == "no_flights":
            # MMT flies nothing between these airports - remember it.
            self.dead_routes.add((src, dst))
            self.new_dead.add((src, dst))
            self._log(f" NO ROUTE - will skip {src}->{dst} from now on "
                      f"({time.time() - started:.0f}s)")
        else:
            self._log(f" no flights on this date "
                      f"({time.time() - started:.0f}s)")
        self.cache[key] = best
        self._save()
        self._record(src, dst, date, best)
        return best

    def _record(self, src, dst, date, flight, cached=False):
        self.seen.append({
            "src": src, "dst": dst, "date": date,
            "flight": flight, "cached": cached,
        })

    def _log(self, msg, end="\n"):
        if self.verbose:
            print(msg, end=end, file=sys.stderr, flush=True)
        if not self.log_sink:
            return
        # _log is also used to build one line across several calls (the
        # "searching ..." prefix and its result); only emit finished lines.
        self._partial += msg
        if end == "\n":
            line, self._partial = self._partial, ""
            try:
                self.log_sink(line)
            except Exception:
                pass


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------

def price_destination(cfg, searcher, city):
    """Best outbound+return combination for one city.

    Returns (bundle, reason). `bundle` is None when nothing could be priced,
    and `reason` then says why - distinguishing "no flights exist" from
    "flights exist but no pairing fits min_nights/max_nights", which are very
    different problems for the user.
    """
    code = city["code"]
    out_options = []
    for d in cfg.outbound_dates():
        f = searcher.cheapest(cfg.source, code, d.isoformat())
        if f:
            out_options.append((d, f))

    if not out_options:
        return None, ("no outbound flights listed on "
                      f"{'/'.join(d.isoformat() for d in cfg.outbound_dates())}")

    if not cfg.round_trip:
        d, f = min(out_options, key=lambda x: x[1]["price"])
        return _bundle(cfg, city, (d, f), None), None

    ret_options = []
    for d in cfg.return_dates():
        f = searcher.cheapest(code, cfg.source, d.isoformat())
        if f:
            ret_options.append((d, f))

    if not ret_options:
        d, f = min(out_options, key=lambda x: x[1]["price"])
        bundle = _bundle(cfg, city, (d, f), None)
        bundle["warning"] = "No return flights found; outbound cost only."
        return bundle, None

    best = None
    for (od, of), (rd, rf) in itertools.product(out_options, ret_options):
        if not cfg.valid_pair(od, rd):
            continue
        total = of["price"] + rf["price"]
        if best is None or total < best[0]:
            best = (total, (od, of), (rd, rf))

    if best is None:
        # Flights exist both ways, but every pairing broke the nights limit.
        spans = sorted({(rd - od).days
                        for od, _ in out_options for rd, _ in ret_options
                        if (rd - od).days > 0})
        return None, (
            f"flights exist, but no date pairing fits the "
            f"{cfg.min_nights or '-'}-{cfg.max_nights or '-'} night limit "
            f"(possible spans with date_flex={cfg.flex}: "
            f"{', '.join(str(x) for x in spans) or 'none'}). "
            f"Widen min_nights/max_nights or raise date_flex."
        )
    return _bundle(cfg, city, best[1], best[2]), None


def _bundle(cfg, city, out, ret):
    od, of = out
    per_adult = of["price"]
    legs = [{**of, "direction": "outbound", "date": od.isoformat()}]
    if ret:
        rd, rf = ret
        per_adult += rf["price"]
        legs.append({**rf, "direction": "return", "date": rd.isoformat()})
        nights = (rd - od).days
    else:
        nights = cfg.nights

    return {
        "complete": bool(ret) or not cfg.round_trip,
        "country": city["country"],
        "city": city["city"],
        "code": city["code"],
        "note": city["note"],
        "depart_date": od.isoformat(),
        "return_date": ret[0].isoformat() if ret else None,
        "nights": nights,
        "per_adult": per_adult,
        "total": per_adult * cfg.travellers,
        "currency": of["currency"],
        "price_basis": of.get("price_basis", ""),
        "legs": legs,
    }


def plan_compare(cfg, searcher):
    """Price every country independently and rank them."""
    options = []
    for country in cfg.countries:
        if country.strip().lower() in cfg.visited:
            searcher._log(f"- {country}: skipped (already visited)")
            searcher.skipped.append((country, "already visited"))
            continue
        try:
            cities = get_cities(country, cfg.cities_per_country)
        except UnknownCountry:
            searcher._log(f"- {country}: no airport mapping, skipped")
            searcher.skipped.append(
                (country, "no city/airport mapping in country_cities collection")
            )
            continue

        searcher._log(f"- {country}")
        for city in cities:
            bundle, reason = price_destination(cfg, searcher, city)
            if bundle:
                options.append(bundle)
            else:
                if searcher.used >= searcher.budget:
                    why = "search budget exhausted before this destination"
                elif any(e[1] == city["code"] or e[0] == city["code"]
                         for e in searcher.errors):
                    why = ("searches failed (see log) - re-run to retry")
                else:
                    why = reason or "no flights found on any searched date"
                searcher.skipped.append(
                    (f"{country} ({city['city']})", why)
                )

    return sorted(options, key=lambda o: o["total"])


def plan_route(cfg, searcher):
    """Visit every country in listed order on one trip, splitting the nights."""
    stops = []
    for country in cfg.countries:
        if country.strip().lower() in cfg.visited:
            continue
        try:
            stops.append(get_cities(country, 1)[0])
        except UnknownCountry:
            searcher._log(f"- {country}: no airport mapping, skipped")
            searcher.skipped.append(
                (country, "no city/airport mapping in country_cities collection")
            )
    if not stops:
        return {"legs": [], "total": 0, "stops": []}

    per_stop = max(1, cfg.nights // len(stops))
    hops, cursor, total = [], cfg.start, 0
    prev = cfg.source

    for i, city in enumerate(stops):
        f = searcher.cheapest(prev, city["code"], cursor.isoformat())
        hops.append({"from": prev, "to": city["code"], "city": city["city"],
                     "country": city["country"], "date": cursor.isoformat(),
                     "flight": f})
        if f:
            total += f["price"]
        prev = city["code"]
        cursor = cursor + timedelta(days=per_stop)

    if cfg.round_trip:
        f = searcher.cheapest(prev, cfg.source, cfg.end.isoformat())
        hops.append({"from": prev, "to": cfg.source, "city": "Home",
                     "country": "", "date": cfg.end.isoformat(), "flight": f})
        if f:
            total += f["price"]

    return {"legs": hops, "total": total * cfg.travellers,
            "per_adult": total, "stops": stops, "nights_each": per_stop}


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def _rule(char="=", n=78):
    return char * n


def render_searched(cfg, searcher, options):
    """Every leg that was priced, grouped by city, with the picks marked.

    Shows why a given date won: the alternatives sit next to it.
    """
    if not searcher.seen:
        return []

    # The legs each option actually chose, so they can be flagged.
    chosen = set()
    for o in options:
        for leg in o["legs"]:
            code = o["code"]
            src = cfg.source if leg["direction"] == "outbound" else code
            dst = code if leg["direction"] == "outbound" else cfg.source
            chosen.add((src, dst, leg["date"]))

    # Group by the far end of the leg, de-duplicating repeat lookups.
    groups, order = {}, []
    for row in searcher.seen:
        code = row["dst"] if row["src"] == cfg.source else row["src"]
        key = (code, row["src"], row["dst"], row["date"])
        if key in groups:
            continue
        groups[key] = row
        if code not in order:
            order.append(code)

    label = {}
    for o in options:
        label[o["code"]] = f"{o['country']} - {o['city']}"

    L = ["", _rule("-"), "ALL OPTIONS SEARCHED", _rule("-"),
         "Every date priced for each destination. '<<' marks the combination",
         "chosen above. Blank fare = no flight found for that date.",
         f"A cheaper fare may go unpicked if the pairing breaks the "
         f"{cfg.min_nights or '-'}-{cfg.max_nights or '-'} night limit."]

    for code in order:
        rows = [(k, v) for k, v in groups.items() if k[0] == code]
        if not rows:
            continue
        L.append("")
        L.append(f"{label.get(code, code)} ({code})")
        L.append(f"   {'DIRECTION':<10}{'DATE':<13}{'AIRLINE':<22}"
                 f"{'DURATION':<10}{'STOPS':<10}{'FARE/ADULT':>12}")
        for k, row in sorted(rows, key=lambda r: (r[0][1] != cfg.source,
                                                  r[0][3])):
            _, src, dst, date = k
            direction = "outbound" if src == cfg.source else "return"
            f = row["flight"]
            mark = " <<" if (src, dst, date) in chosen else ""
            if not f:
                L.append(f"   {direction:<10}{date:<13}"
                         f"{'- no flights found -':<22}")
                continue
            stops = ("non-stop" if f["stops"] == 0
                     else f"{f['stops']} stop"
                          f"{'s' if f['stops'] > 1 else ''}")
            L.append(f"   {direction:<10}{date:<13}{f['airline'][:21]:<22}"
                     f"{f['duration']:<10}{stops:<10}"
                     f"{f['currency']} {f['price']:>8,}{mark}")

    total = len(groups)
    L.append("")
    L.append(f"{total} legs priced across {len(order)} destinations "
             f"({searcher.used} live searches, "
             f"{total - searcher.used} from cache).")
    return L


def render_compare(cfg, options, searcher=None):
    L = []
    L.append(_rule())
    L.append("TRIP PLAN - country comparison")
    L.append(_rule())
    L.append(f"From            : {cfg.source}")
    L.append(f"Travellers      : {cfg.travellers}")
    L.append(f"Window          : {cfg.start} to {cfg.end} "
             f"({cfg.nights} nights), flex +/-{cfg.flex}d")
    if cfg.min_nights or cfg.max_nights:
        L.append(f"Nights allowed  : {cfg.min_nights or '-'} to "
                 f"{cfg.max_nights or '-'}")
    L.append(f"Round trip      : {'yes' if cfg.round_trip else 'no'}")
    L.append(f"Generated       : {datetime.now():%Y-%m-%d %H:%M:%S}")
    L.append("")

    if searcher is not None and searcher.skipped:
        L.append(_rule("-"))
        L.append("NOT PRICED")
        L.append(_rule("-"))
        for country, why in searcher.skipped:
            L.append(f"  {country:<16} {why}")
        L.append("")

    if searcher is not None and searcher.rate_limited:
        L.append("! MakeMyTrip rate-limited this run and it stopped early.")
        L.append("  Wait 10-15 minutes and re-run; the cache resumes where it")
        L.append("  left off. Raising pause_seconds (in trip_details or the "
                 "config file) makes this less likely.")
        L.append("")

    if searcher is not None and searcher.used >= searcher.budget:
        L.append(f"! Search budget ({searcher.budget}) was exhausted - some "
                 f"destinations may be missing or")
        L.append(f"  priced on fewer dates. Raise max_searches (in "
                 f"trip_details or the config file) and re-run;")
        L.append(f"  completed searches are cached so it resumes.")
        L.append("")

    if not options:
        L.append("No priced options. Either every search failed or the search")
        L.append("budget ran out before any country was costed. Re-run to")
        L.append("continue - completed searches are cached.")
        return "\n".join(L)

    full = [o for o in options if o.get("complete")]
    partial = [o for o in options if not o.get("complete")]

    L.append(_rule("-"))
    L.append("RANKED BY TOTAL COST FOR THE PARTY")
    L.append(_rule("-"))
    L.append(f"{'#':<3}{'COUNTRY':<12}{'CITY':<20}{'DATES':<24}"
             f"{'NIGHTS':<8}{'TOTAL':>12}")
    for i, o in enumerate(full, 1):
        dates = f"{o['depart_date']} - {o['return_date']}"
        L.append(f"{i:<3}{o['country'][:11]:<12}{o['city'][:19]:<20}"
                 f"{dates:<24}{o['nights']:<8}"
                 f"{o['currency']} {o['total']:>8,}")
    if not full:
        L.append("  (none - no destination was priced both ways)")
    L.append("")

    if partial:
        L.append("INCOMPLETE - NOT COMPARABLE WITH THE ABOVE")
        L.append("These have no return leg priced, so the total is outbound "
                 "only and")
        L.append("understates the real trip cost. Re-run with a higher "
                 "max_searches.")
        for o in partial:
            L.append(f"   {o['country'][:11]:<12}{o['city'][:19]:<20}"
                     f"{o['depart_date']} outbound only    "
                     f"{o['currency']} {o['total']:>8,}")
        L.append("")

    if not full:
        return "\n".join(L)

    options, best = full, full[0]
    L.append(f"BEST VALUE: {best['country']} ({best['city']}) - "
             f"{best['currency']} {best['total']:,} for {cfg.travellers} "
             f"traveller{'s' if cfg.travellers > 1 else ''}")
    if len(options) > 1:
        diff = options[-1]["total"] - best["total"]
        L.append(f"That is {best['currency']} {diff:,} less than the most "
                 f"expensive option priced.")
    L.append("")

    L.append(_rule("-"))
    L.append("DETAIL")
    L.append(_rule("-"))
    for i, o in enumerate(full + partial, 1):
        L.append("")
        flag = "" if o.get("complete") else "   [OUTBOUND ONLY]"
        L.append(f"{i}. {o['country']} - {o['city']} ({o['code']}){flag}")
        L.append(f"   {o['note']}")
        L.append(f"   {o['nights']} nights, {o['depart_date']} to "
                 f"{o['return_date'] or 'n/a'}")
        for leg in o["legs"]:
            stops = ("non-stop" if leg["stops"] == 0
                     else f"{leg['stops']} stop"
                          f"{'s' if leg['stops'] > 1 else ''}"
                          f" via {leg.get('stop_via') or '?'}")
            L.append(f"   {leg['direction']:<9} {leg['date']}  "
                     f"{leg['airline'][:18]:<19} "
                     f"{leg['depart_time']}-{leg['arrive_time']} "
                     f"{leg['duration']:<8} {stops:<22} "
                     f"{leg['currency']} {leg['price']:,}")
            bits = []
            if leg.get("flight_number"):
                bits.append(f"flight {leg['flight_number']}")
            if leg.get("cabin_baggage"):
                bits.append(f"cabin {leg['cabin_baggage']}")
            if leg.get("checkin_baggage"):
                bits.append(f"check-in {leg['checkin_baggage']}")
            bits.append(leg.get("refundable")
                        or "refundable: not stated by MMT")
            L.append(f"             {' | '.join(bits)}")
            if leg.get("layovers"):
                lay = ", ".join(
                    f"{l['code'] or l['airport']} {l['duration']}"
                    for l in leg["layovers"])
                L.append(f"             layover: {lay}")
            if leg.get("search_url"):
                L.append(f"             {leg['search_url']}")
        L.append(f"   per adult: {o['currency']} {o['per_adult']:,}"
                 f"  (outbound + return)")
        L.append(f"   x {cfg.travellers} travellers: "
                 f"{o['currency']} {o['total']:,}")
        if o.get("warning"):
            L.append(f"   ! {o['warning']}")
    if searcher is not None:
        L.extend(render_searched(cfg, searcher, options))

    L.append("")
    L.append(_rule())
    L.append("Prices are what MakeMyTrip listed at generation time and move "
             "daily.")
    L.append(_rule())
    return "\n".join(L)


class _LegCounter:
    """A Searcher stand-in that records legs instead of scraping them.

    plan_combine() prices the same leg for several itineraries and date
    offsets, and the real Searcher's cache collapses those into one scrape -
    so the only honest measure of a run's cost is the number of *distinct*
    (from, to, date) legs. Counting them first is free (no network) and lets
    the budget be set from the actual work rather than a guess.
    """

    def __init__(self):
        self.legs = set()
        self.skipped = []
        self.used = 0
        self.budget = float("inf")
        self.errors = set()

    def cheapest(self, src, dst, date):
        self.legs.add((src, dst, date))
        # A plausible fare, so every itinerary is treated as priced and the
        # walk reaches every leg it would really need.
        return {"price": 1, "currency": "INR", "duration_minutes": 1,
                "stops": 0, "airline": "", "flight_number": ""}

    def _log(self, *a, **kw):
        pass


def count_searches(cfg):
    """How many distinct flight searches this config would actually run."""
    counter = _LegCounter()
    plan_combine(cfg, counter)
    return len(counter.legs)


def plan_combine(cfg, searcher):
    """Every single- and multi-country itinerary, priced."""
    available = cfg.max_nights or cfg.nights
    itins = build_itineraries(
        [c for c in cfg.countries if c.strip().lower() not in cfg.visited],
        cfg.cities_per_country, available,
        on_skip=lambda c, why: searcher.skipped.append((c, why)),
        city_filter=cfg.city_filter, fill_window=cfg.fill_window,
    )
    searcher._log(f"  {len(itins)} itinerary combinations to price "
                  f"(window allows {available} nights)")
    return price_all(itins, searcher, cfg.source, cfg.start, cfg.travellers,
                     flex=cfg.flex)


def render_combine(cfg, itins, searcher=None):
    L = [_rule(), "TRIP PLAN - country and city combinations", _rule()]
    L.append(f"From            : {cfg.source}")
    L.append(f"Travellers      : {cfg.travellers}")
    L.append(f"Window          : {cfg.start} to {cfg.end} "
             f"({cfg.nights} nights), flex +/-{cfg.flex}d")
    L.append(f"Nights allowed  : {cfg.min_nights or '-'} to "
             f"{cfg.max_nights or cfg.nights}")
    L.append(f"Generated       : {datetime.now():%Y-%m-%d %H:%M:%S}")
    L.append("")

    if searcher is not None and searcher.rate_limited:
        L.append("! MakeMyTrip rate-limited this run; it stopped early.")
        L.append("  Check with: python3 -m src.flights --diagnose")
        L.append("")

    done = [i for i in itins if i.complete]
    part = [i for i in itins if not i.complete]
    done.sort(key=lambda i: i.total)

    L.append(_rule("-"))
    L.append("RANKED BY TOTAL FLIGHT COST FOR THE PARTY")
    L.append(_rule("-"))
    L.append(f"{'#':<3}{'ITINERARY':<34}{'NIGHTS':<8}{'DATES':<24}"
             f"{'TOTAL':>12}")
    for n, i in enumerate(done, 1):
        L.append(f"{n:<3}{i.label[:33]:<34}{i.nights_needed:<8}"
                 f"{i.start_date} - {i.end_date:<11}"
                 f"{i.currency} {i.total:>8,}")
    if not done:
        L.append("  (none priced completely)")
    L.append("")

    if done:
        b = done[0]
        L.append(f"CHEAPEST: {b.label} - {b.currency} {b.total:,} for "
                 f"{cfg.travellers} travellers, {b.nights_needed} nights")
        singles = [i for i in done if i.kind == "single"]
        multis = [i for i in done if i.kind == "multi"]
        if singles and multis:
            extra = multis[0].total - singles[0].total
            L.append(f"Adding a second country costs "
                     f"{b.currency} {abs(extra):,} "
                     f"{'more' if extra > 0 else 'LESS'} than the best "
                     f"single-country trip "
                     f"({multis[0].label} vs {singles[0].label}).")
        L.append("")

    L.append(_rule("-"))
    L.append("DETAIL")
    L.append(_rule("-"))
    for n, i in enumerate(done + part, 1):
        L.append("")
        flag = "" if i.complete else "   [INCOMPLETE]"
        L.append(f"{n}. {i.label}{flag}")
        if i.stops:
            L.append("   Days: " + ", ".join(
                f"{s.city} ({s.code}) {s.nights}n" for s in i.stops))
        if i.reason:
            L.append(f"   ! {i.reason}")
        for leg in i.legs:
            f = leg["flight"]
            if not f:
                L.append(f"   {leg['date']}  {leg['from']} -> "
                         f"{leg['to']:<5} NO FLIGHT FOUND")
                continue
            stops = ("non-stop" if f["stops"] == 0
                     else f"{f['stops']} stop"
                          f"{'s' if f['stops'] > 1 else ''} "
                          f"via {f.get('stop_via') or '?'}")
            L.append(f"   {leg['date']}  {leg['from']} -> {leg['to']:<5} "
                     f"{f['airline'][:20]:<21} {f['depart_time']}-"
                     f"{f['arrive_time']} {f['duration']:<8} {stops:<20} "
                     f"{f['currency']} {f['price']:,}")
            bits = []
            if f.get("flight_number"):
                bits.append(f"flight {f['flight_number']}")
            if f.get("cabin_baggage"):
                bits.append(f"cabin {f['cabin_baggage']}")
            if f.get("checkin_baggage"):
                bits.append(f"check-in {f['checkin_baggage']}")
            if bits:
                L.append(f"        {' | '.join(bits)}")
            if f.get("search_url"):
                L.append(f"        book: {f['search_url']}")
        if i.complete:
            L.append(f"   per adult: {i.currency} {i.per_adult:,}   "
                     f"x {cfg.travellers} = {i.currency} {i.total:,}")

    if searcher is not None and (searcher.new_dead or searcher.skipped_dead):
        L.append("")
        L.append(_rule("-"))
        L.append("ROUTES WITH NO SERVICE")
        L.append(_rule("-"))
        for a, b in sorted(searcher.new_dead):
            L.append(f"  {a} -> {b}   MakeMyTrip operates no flights "
                     f"(recorded as a dead route)")
        if searcher.skipped_dead:
            L.append(f"  {searcher.skipped_dead} search(es) skipped on "
                     f"previously recorded dead routes.")

    if searcher is not None and searcher.skipped:
        L.append("")
        L.append(_rule("-"))
        L.append("NOT PRICED")
        L.append(_rule("-"))
        for c, why in searcher.skipped:
            L.append(f"  {c:<20} {why}")

    L.append("")
    L.append(_rule())
    L.append("Links open MakeMyTrip's results for that leg and date - the "
             "fare shown")
    L.append("is what was listed at generation time and moves daily.")
    L.append(_rule())
    return "\n".join(L)


# --------------------------------------------------------------------------
# Structured output (what the Trips page renders)
# --------------------------------------------------------------------------

# How the ranked list is ordered for each "Flight Preference" on the Trips
# page. The preference already picked each individual leg inside flights.py;
# this makes the itinerary ranking agree with it, so a "fastest" search isn't
# ranked by price.
def _itinerary_rank_key(itin, prefer):
    total_minutes = sum(
        (leg.get("flight") or {}).get("duration_minutes", 0)
        for leg in itin.legs
    )
    total_stops = sum(
        (leg.get("flight") or {}).get("stops", 0) for leg in itin.legs
    )
    if prefer == "fastest":
        return (total_minutes, itin.total)
    if prefer == "non_stop":
        return (total_stops, itin.total)
    return (itin.total, total_minutes)


def _leg_summary(leg):
    """One leg, flattened for the UI - every field flights.py captured."""
    f = leg.get("flight")
    base = {"from": leg["from"], "to": leg["to"], "date": leg["date"]}
    if not f:
        return {**base, "found": False}
    stops = f.get("stops", 0)
    return {
        **base,
        "found": True,
        "airline": f.get("airline", ""),
        "flight_number": f.get("flight_number", ""),
        "depart_time": f.get("depart_time", ""),
        "arrive_time": f.get("arrive_time", ""),
        "duration": f.get("duration", ""),
        "duration_minutes": f.get("duration_minutes", 0),
        "stops": stops,
        "stops_label": ("Non-stop" if stops == 0
                        else f"{stops} stop{'s' if stops > 1 else ''}"
                             + (f" via {f['stop_via']}" if f.get("stop_via") else "")),
        "stop_via": f.get("stop_via", ""),
        "arrives_next_day": f.get("arrives_next_day", 0),
        "price": f.get("price", 0),
        "currency": f.get("currency", "INR"),
        "price_basis": f.get("price_basis", ""),
        "cabin_baggage": f.get("cabin_baggage", ""),
        "checkin_baggage": f.get("checkin_baggage", ""),
        "refundable": f.get("refundable", ""),
        "layover_minutes": f.get("layover_minutes", 0),
        "layovers": f.get("layovers", []),
        "segments": f.get("segments", []),
        "route": f.get("route", []),
        "search_url": f.get("search_url", ""),
    }


def plan_to_dict(cfg, itins, searcher=None):
    """The same plan render_combine() prints, as JSON for the Trips page.

    Ranked list first (complete itineraries, ordered by the flight preference),
    then the ones that could not be fully priced - those are kept rather than
    dropped so the page can say why a combination is missing.
    """
    done = sorted([i for i in itins if i.complete],
                  key=lambda i: _itinerary_rank_key(i, cfg.prefer))
    part = [i for i in itins if not i.complete]

    options = []
    for rank, itin in enumerate(done + part, 1):
        legs = [_leg_summary(l) for l in itin.legs]
        options.append({
            "rank": rank,
            "label": itin.label,
            "kind": itin.kind,
            "complete": itin.complete,
            "reason": itin.reason,
            "nights": itin.nights_needed,
            "start_date": itin.start_date,
            "end_date": itin.end_date,
            "per_adult": itin.per_adult,
            "total": itin.total,
            "currency": itin.currency,
            "total_duration_minutes": sum(l["duration_minutes"] for l in legs
                                          if l.get("found")),
            "total_stops": sum(l["stops"] for l in legs if l.get("found")),
            "stops": [s.to_dict() for s in itin.stops],
            "legs": legs,
        })

    # code -> city name for every airport this plan mentions, so the page can
    # print "Bengaluru (BLR)" instead of leaving the traveller to decode IATA.
    codes = {cfg.source}
    for opt in options:
        for leg in opt["legs"]:
            codes.update([leg["from"], leg["to"]])
            for sg in leg.get("segments") or []:
                codes.update([sg.get("from"), sg.get("to")])
            for lo in leg.get("layovers") or []:
                codes.add(lo.get("code"))
            codes.update(leg.get("route") or [])
            codes.update(c.strip() for c in (leg.get("stop_via") or "").split(","))
    airports = {}
    for code in codes:
        name = city_for_code(code) if code else ""
        if name:
            airports[str(code).strip().upper()] = name

    return {
        "generated_at": datetime.now().isoformat(),
        "source": cfg.source,
        "airports": airports,
        "travellers": cfg.travellers,
        "start_date": cfg.start.isoformat(),
        "end_date": cfg.end.isoformat(),
        "nights": cfg.nights,
        "flex_days": cfg.flex,
        "prefer": cfg.prefer,
        "countries": list(cfg.countries),
        "currency": done[0].currency if done else "INR",
        "options": options,
        "not_priced": [{"what": what, "why": why}
                       for what, why in (searcher.skipped if searcher else [])],
        "dead_routes": sorted(f"{a}-{b}" for a, b in
                              (searcher.new_dead if searcher else set())),
        "searches_used": searcher.used if searcher else 0,
        "search_budget": cfg.max_searches,
        "rate_limited": bool(searcher and searcher.rate_limited),
    }


def render_route(cfg, plan):
    L = []
    L.append(_rule())
    L.append("TRIP PLAN - multi-country route")
    L.append(_rule())
    L.append(f"From            : {cfg.source}")
    L.append(f"Travellers      : {cfg.travellers}")
    L.append(f"Window          : {cfg.start} to {cfg.end} "
             f"({cfg.nights} nights)")
    L.append(f"Generated       : {datetime.now():%Y-%m-%d %H:%M}")
    L.append("")
    if not plan["legs"]:
        L.append("No route could be built - no known countries in the input.")
        return "\n".join(L)

    L.append(f"Route: " + " -> ".join(
        [cfg.source] + [s["code"] for s in plan["stops"]]
        + ([cfg.source] if cfg.round_trip else [])))
    L.append(f"About {plan['nights_each']} nights per stop.")
    L.append("")
    L.append(_rule("-"))
    for leg in plan["legs"]:
        f = leg["flight"]
        if not f:
            L.append(f"{leg['date']}  {leg['from']} -> {leg['to']:<5} "
                     f"NO FLIGHT FOUND")
            continue
        L.append(f"{leg['date']}  {leg['from']} -> {leg['to']:<5} "
                 f"{f['airline'][:18]:<19} {f['depart_time']}-"
                 f"{f['arrive_time']} {f['duration']:<8} "
                 f"{f['currency']} {f['price']:,}")
    L.append(_rule("-"))
    L.append(f"per adult        : {plan['per_adult']:,}")
    L.append(f"x {cfg.travellers} travellers  : {plan['total']:,}")
    L.append("")
    return "\n".join(L)


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Plan a trip from the Trips page (trip_details in MongoDB), "
                     "or a legacy config file via --input")
    ap.add_argument("--input", default=None,
                    help="legacy trip_input.txt-style file (e.g. "
                         "src/trip_input_bkp.txt); if omitted, config is read "
                         "live from the trip_details MongoDB collection")
    ap.add_argument("--output", default=None,
                    help="explicit output path; default writes a timestamped "
                         "file plus trip_output.txt")
    ap.add_argument("--keep-cache", action="store_true",
                    help="do not clear the resume cache after a clean run")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be searched without scraping")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    source_desc = args.input or "trip_details (MongoDB)"
    cfg_dict = parse_input(args.input) if args.input else load_config_from_mongo()
    cfg = Config(cfg_dict)
    searcher = Searcher(cfg.travellers, cfg.prefer, cfg.max_searches,
                        dry_run=args.dry_run, verbose=not args.quiet,
                        pause=cfg.pause_seconds,
                        dead_routes=cfg.no_flight_routes)

    planned = len([c for c in cfg.countries
                   if c.strip().lower() not in cfg.visited])
    if cfg.mode == "compare":
        est = planned * cfg.cities_per_country * (2 if cfg.round_trip else 1) \
              * (2 * cfg.flex + 1)
        searcher._log(
            f"Planning from {source_desc}: compare mode, {planned} countries, "
            f"~{est} searches (budget {cfg.max_searches}, ~20-40s each)"
        )
        if est > cfg.max_searches:
            searcher._log(
                f"  ! estimate exceeds max_searches; the run will stop early. "
                f"Lower date_flex or cities_per_country, or raise "
                f"max_searches."
            )
    elif cfg.mode == "combine":
        searcher._log(f"Planning from {source_desc}: combine mode, "
                      f"{planned} countries, budget {cfg.max_searches}")
    else:
        searcher._log(f"Planning from {source_desc}: route mode, "
                      f"{planned} countries")

    if cfg.mode == "combine":
        itins = plan_combine(cfg, searcher)
        text = render_combine(cfg, itins, searcher)
    elif cfg.mode == "route":
        plan = plan_route(cfg, searcher)
        text = render_route(cfg, plan)
    else:
        options = plan_compare(cfg, searcher)
        text = render_compare(cfg, options, searcher)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if args.output:
        written = [args.output]
    else:
        base = os.path.join(HERE, f"trip_output_{stamp}.txt")
        written = [base, DEFAULT_OUTPUT]

    for path in written:
        with open(path, "w") as fh:
            fh.write(text + "\n")

    if args.input:
        added = record_dead_routes(args.input, searcher.new_dead)
    else:
        added = record_dead_routes_mongo(searcher.new_dead)
    if added:
        searcher._log(f"\nRecorded {len(added)} dead route(s) in "
                      f"{source_desc}: {', '.join(added)}")
    if searcher.skipped_dead:
        searcher._log(f"Skipped {searcher.skipped_dead} search(es) on known "
                      f"dead routes.")

    searcher._log(f"\nSearches used: {searcher.used}/{cfg.max_searches}")
    for path in written:
        searcher._log(f"Written to {path}")

    # The cache is a resume aid, not a price store - drop it once the run has
    # actually finished, so the next run re-prices everything live.
    clean = (not searcher.exhausted and not searcher.rate_limited
             and not searcher.failures
             and not args.dry_run)
    if clean and not args.keep_cache:
        try:
            os.remove(CACHE_PATH)
            searcher._log("Cache cleared - next run will search live.")
        except FileNotFoundError:
            pass
    elif not args.keep_cache:
        why = ("rate limited" if searcher.rate_limited
               else "budget exhausted" if searcher.exhausted
               else f"{searcher.failures} failed search(es)")
        searcher._log(f"Cache kept ({why}) - re-run to resume where it "
                      f"stopped.")
    print(text)


if __name__ == "__main__":
    main()
