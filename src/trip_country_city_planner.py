"""Country -> city/airport lookups, plus building and pricing itinerary
combinations across countries and cities.

Given a set of candidate countries (from trip_details in MongoDB, or a legacy
config file), this enumerates the realistic ways to spend the trip window and
prices each one:

  - one country, covering as many of its cities as fit   ("Vietnam only")
  - two or more countries in every visiting order        ("Vietnam -> Cambodia"
                                                          and the reverse)

Day allocation comes from the recommended nights per city, read from the
`country_cities` MongoDB collection (this used to be the trip_country_city.py
module's COUNTRY_CITIES/CITY_DAYS tables - see trip_country_city_bkp.py for
the original, kept only for reference; country_city_seed.py is what actually
seeds the collection now). When countries are combined, the one needing the
most time keeps its full allocation and the others are trimmed towards their
minimum - so a Vietnam + Cambodia trip spends most of the window in Vietnam
with a short Cambodia leg, rather than splitting evenly.

Flight legs priced per itinerary: home -> first city, each city-to-city hop
(domestic or international), and last city -> home.

Kept separate from trip_planner.py so the combination logic can be reasoned
about and tested on its own; pricing takes any object with a
`.cheapest(src, dst, date)` method, so it does not import the planner.

    from src.trip_country_city_planner import get_cities, get_city
    get_cities("Vietnam", limit=2)
    # [{'city': 'Ho Chi Minh City', 'code': 'SGN', ...}, {'city': 'Hanoi', ...}]

    python -m src.trip_country_city_planner Vietnam
    python -m src.trip_country_city_planner --list
"""

import argparse
import itertools
import json
import sys
from dataclasses import dataclass, field, asdict
from datetime import timedelta

from pymongo import MongoClient

MONGO_URL = "mongodb://admin:admin@localhost:27017/"
DB_NAME = "top3"

DEFAULT_DAYS = (2, 3)


class UnknownCountry(KeyError):
    """Raised when a country isn't in the country_cities collection."""


# --------------------------------------------------------------------------
# Country -> city/airport lookups (backed by MongoDB)
# --------------------------------------------------------------------------

# Process-local cache: {"countries": {key: [(city, code, note, min, ideal)]},
#                       "aliases": {alias: canonical_key}}
# Loaded once per process since this data changes rarely and every itinerary
# combination re-looks-up the same countries.
_cache = {"countries": None, "aliases": None}


def _load_from_mongo(force=False):
    if not force and _cache["countries"] is not None:
        return
    client = MongoClient(MONGO_URL)
    try:
        db = client[DB_NAME]
        countries = {}
        for doc in db.country_cities.find({}):
            countries[doc["country_key"]] = [
                (c["city"], c["code"], c.get("note", ""),
                 c.get("min_nights", DEFAULT_DAYS[0]),
                 c.get("ideal_nights", DEFAULT_DAYS[1]))
                for c in doc.get("cities", [])
            ]
        alias_doc = db.country_city_aliases.find_one({"_id": "aliases"}) or {}
        aliases = alias_doc.get("map", {})
    finally:
        client.close()
    _cache["countries"] = countries
    _cache["aliases"] = aliases


def reload_cache():
    """Force the next lookup to re-read country_cities/aliases from Mongo."""
    _load_from_mongo(force=True)


def _key(country: str) -> str:
    _load_from_mongo()
    k = " ".join(country.strip().lower().replace("_", " ").split())
    return _cache["aliases"].get(k, k)


def get_cities(country: str, limit: int = 2, only_codes=None):
    """Return up to `limit` candidate cities for a country.

    Each entry: {"country", "city", "code", "note", "min_nights", "ideal_nights"}.
    `only_codes` restricts the candidates to those airport codes - that is what
    the Trips page's "Cities To Visit" ticks mean: price these cities, not
    whichever ones happen to come first in the collection. An empty or
    unmatched filter falls back to the full list rather than returning nothing.
    Raises UnknownCountry if the country isn't in the collection.
    """
    _load_from_mongo()
    k = _key(country)
    if k not in _cache["countries"]:
        raise UnknownCountry(
            f"No cities known for {country!r}. Known: "
            f"{', '.join(sorted(_cache['countries']))}"
        )
    rows = _cache["countries"][k]
    if only_codes:
        wanted = {c.upper() for c in only_codes}
        picked = [r for r in rows if r[1].upper() in wanted]
        if picked:
            rows = picked
    rows = rows[: max(1, limit)]
    return [
        {
            "country": country.strip().title(), "city": city, "code": code,
            "note": note, "min_nights": lo, "ideal_nights": hi,
        }
        for city, code, note, lo, hi in rows
    ]


def get_city(country: str):
    """The single best-connected city for a country."""
    return get_cities(country, limit=1)[0]


def city_days(code):
    """(minimum, ideal) nights worth spending in the city with this code."""
    _load_from_mongo()
    for cities in _cache["countries"].values():
        for city, c, note, lo, hi in cities:
            if c.upper() == code.upper():
                return (lo, hi)
    return DEFAULT_DAYS


def find_airport_code(city_name):
    """Best-effort city-name -> IATA code lookup across every known country.

    Used by trip_planner.py to resolve a free-text "Starting Location" (e.g.
    from the Trips page) to an airport code.
    """
    if not city_name:
        return None
    _load_from_mongo()
    needle = city_name.strip().lower()
    for cities in _cache["countries"].values():
        for city, code, _note, _lo, _hi in cities:
            if city.lower() == needle:
                return code
    return None


# Transit hubs that itineraries route through but nobody would list as a
# destination, so they never appear in country_cities. Codes only show up here
# as a connection or layover, and a bare code is exactly what makes a route
# line unreadable - which is the whole point of naming them.
TRANSIT_AIRPORTS = {
    "BKK": "Bangkok", "DMK": "Bangkok", "KUL": "Kuala Lumpur",
    "SIN": "Singapore", "DXB": "Dubai", "AUH": "Abu Dhabi", "DOH": "Doha",
    "HKG": "Hong Kong", "CMB": "Colombo", "MAA": "Chennai",
    "HYD": "Hyderabad", "CCU": "Kolkata", "COK": "Kochi", "TRV": "Trivandrum",
    "GOI": "Goa", "AMD": "Ahmedabad", "PNQ": "Pune", "CAN": "Guangzhou",
    "PVG": "Shanghai", "ICN": "Seoul", "NRT": "Tokyo", "TPE": "Taipei",
    "MNL": "Manila", "CGK": "Jakarta", "DPS": "Bali", "KTM": "Kathmandu",
}


def city_for_code(code):
    """IATA code -> city name, or "" if nothing knows it.

    The reverse of find_airport_code(): the planner works in codes, but a
    route printed as "BLR -> PQC" tells a traveller nothing, so the UI needs
    the names back.
    """
    if not code:
        return ""
    code = str(code).strip().upper()
    _load_from_mongo()
    for cities in _cache["countries"].values():
        for city, c, note, lo, hi in cities:
            if c.upper() == code:
                return city
    return TRANSIT_AIRPORTS.get(code, "")


def known_countries():
    _load_from_mongo()
    return sorted({_cache["aliases"].get(k, k) for k in _cache["countries"]})


def country_plan(country, max_cities=2, only_codes=None):
    """Cities for a country plus how long covering them takes.

    Returns {"country", "cities": [...], "min_nights", "ideal_nights"} where the
    night totals are the sum across the chosen cities. This is what the
    combination planner uses to decide whether a country fits a trip window.
    """
    cities = get_cities(country, max_cities, only_codes=only_codes)
    return {
        "country": cities[0]["country"],
        "cities": cities,
        "min_nights": sum(c["min_nights"] for c in cities),
        "ideal_nights": sum(c["ideal_nights"] for c in cities),
    }


@dataclass
class Stop:
    country: str
    city: str
    code: str
    nights: int
    note: str = ""

    def to_dict(self):
        return asdict(self)


@dataclass
class Itinerary:
    label: str
    kind: str                      # "single" or "multi"
    stops: list = field(default_factory=list)
    legs: list = field(default_factory=list)
    nights_needed: int = 0
    per_adult: int = 0
    total: int = 0
    currency: str = "INR"
    start_date: str = ""
    end_date: str = ""
    complete: bool = False
    reason: str = ""

    def to_dict(self):
        d = asdict(self)
        d["stops"] = [s.to_dict() if isinstance(s, Stop) else s
                      for s in self.stops]
        return d


# --------------------------------------------------------------------------
# Day allocation
# --------------------------------------------------------------------------

def allocate_nights(plans, available):
    """Split `available` nights across country plans.

    The country needing the most time is the primary and keeps as much as
    possible; the others are trimmed towards their minimum first. Returns
    None if even every country's minimum does not fit.
    """
    order = sorted(plans, key=lambda p: p["ideal_nights"], reverse=True)
    floor = sum(p["min_nights"] for p in order)
    if floor > available:
        return None

    # Start everyone at their minimum, then hand out what's left to the
    # primary first, up to each country's ideal.
    alloc = {p["country"]: p["min_nights"] for p in order}
    spare = available - floor
    for p in order:
        if spare <= 0:
            break
        room = p["ideal_nights"] - alloc[p["country"]]
        take = min(room, spare)
        alloc[p["country"]] += take
        spare -= take

    # Anything still spare is left unused: a shorter trip inside the window is
    # legitimate (and usually cheaper), so nights are never padded past ideal.
    return alloc


def _split_across_cities(plan, nights):
    """Distribute a country's nights across its cities, biggest draw first."""
    cities = sorted(plan["cities"], key=lambda c: c["ideal_nights"],
                    reverse=True)
    # Drop cities that no longer fit, keeping at least one.
    kept = []
    remaining = nights
    for c in cities:
        if remaining >= c["min_nights"] or not kept:
            kept.append(c)
            remaining -= c["min_nights"]
    per = {c["code"]: c["min_nights"] for c in kept}
    spare = nights - sum(per.values())
    for c in kept:
        if spare <= 0:
            break
        room = max(0, c["ideal_nights"] - per[c["code"]])
        take = min(room, spare)
        per[c["code"]] += take
        spare -= take
    return [
        Stop(country=c["country"], city=c["city"], code=c["code"],
             nights=per[c["code"]], note=c["note"])
        for c in kept if per[c["code"]] > 0
    ]


# --------------------------------------------------------------------------
# Building candidates
# --------------------------------------------------------------------------

def _fill_window(stops, nights):
    """Stretch a stop list so the nights add up to the whole trip window.

    allocate_nights() never pads past a city's ideal stay, so a plan usually
    ends before the window does. That is right when the dates are being
    optimised, but the first-level search prices one fixed departure and one
    fixed return - the dates the traveller actually entered - so the last leg
    has to land on the return date. The surplus goes to the longest stop,
    matching the rule that the primary destination keeps the most time.
    """
    spare = nights - sum(s.nights for s in stops)
    if spare > 0 and stops:
        primary = max(stops, key=lambda s: s.nights)
        primary.nights += spare
    return stops


def build_itineraries(countries, cities_per_country, available_nights,
                      max_countries_combined=3, on_skip=None,
                      city_filter=None, fill_window=False):
    """Every single-country and multi-country ordering worth pricing.

    `city_filter` is {country_key: [airport codes]} - the cities ticked on the
    Trips page. Countries absent from it are planned from the full city list.

    `fill_window` stretches every itinerary to use the whole window, so each one
    departs and returns on the two dates the traveller entered.
    """
    city_filter = city_filter or {}
    plans = {}
    for c in countries:
        try:
            plans[c] = country_plan(c, cities_per_country,
                                    only_codes=city_filter.get(_key(c)))
        except UnknownCountry:
            if on_skip:
                on_skip(c, "no city/airport mapping in country_cities collection")

    out = []

    # One country, as many cities as fit.
    for name, plan in plans.items():
        alloc = allocate_nights([plan], available_nights)
        if alloc is None:
            out.append(Itinerary(
                label=f"{plan['country']} only", kind="single",
                nights_needed=plan["min_nights"],
                reason=(f"needs at least {plan['min_nights']} nights, "
                        f"window allows {available_nights}"),
            ))
            continue
        stops = _split_across_cities(plan, alloc[plan["country"]])
        if fill_window:
            stops = _fill_window(stops, available_nights)
        out.append(Itinerary(
            label=f"{plan['country']} only ({len(stops)} "
                  f"{'city' if len(stops) == 1 else 'cities'})",
            kind="single", stops=stops,
            nights_needed=sum(s.nights for s in stops),
        ))

    # Combinations, in every order - the order changes which flights are
    # needed and therefore the price.
    names = list(plans)
    for size in range(2, min(len(names), max_countries_combined) + 1):
        for combo in itertools.combinations(names, size):
            for order in itertools.permutations(combo):
                chosen = [plans[n] for n in order]
                alloc = allocate_nights(chosen, available_nights)
                label = " -> ".join(plans[n]["country"] for n in order)
                if alloc is None:
                    out.append(Itinerary(
                        label=label, kind="multi",
                        nights_needed=sum(p["min_nights"] for p in chosen),
                        reason=(f"needs at least "
                                f"{sum(p['min_nights'] for p in chosen)} "
                                f"nights, window allows {available_nights}"),
                    ))
                    continue
                stops = []
                for plan in chosen:
                    stops.extend(
                        _split_across_cities(plan, alloc[plan["country"]])
                    )
                if fill_window:
                    stops = _fill_window(stops, available_nights)
                out.append(Itinerary(
                    label=label, kind="multi", stops=stops,
                    nights_needed=sum(s.nights for s in stops),
                ))
    return out


# --------------------------------------------------------------------------
# Pricing
# --------------------------------------------------------------------------

def price_itinerary(itin, searcher, home, start_date, travellers):
    """Price every leg of an itinerary departing on `start_date`.

    Mutates and returns `itin`. `complete` is True only when every leg priced.
    """
    if not itin.stops:
        return itin

    hops = [(home, itin.stops[0].code, start_date)]
    cursor = start_date
    for a, b in zip(itin.stops, itin.stops[1:]):
        cursor = cursor + timedelta(days=a.nights)
        hops.append((a.code, b.code, cursor))
    cursor = cursor + timedelta(days=itin.stops[-1].nights)
    hops.append((itin.stops[-1].code, home, cursor))

    legs, per_adult, missing = [], 0, []
    for src, dst, date in hops:
        f = searcher.cheapest(src, dst, date.isoformat())
        if not f:
            missing.append(f"{src}->{dst} {date.isoformat()}")
            legs.append({"from": src, "to": dst, "date": date.isoformat(),
                         "flight": None})
            continue
        per_adult += f["price"]
        legs.append({"from": src, "to": dst, "date": date.isoformat(),
                     "flight": f})

    itin.legs = legs
    itin.per_adult = per_adult
    itin.total = per_adult * travellers
    itin.start_date = start_date.isoformat()
    itin.end_date = hops[-1][2].isoformat()
    itin.complete = not missing
    if missing:
        itin.reason = f"no flights for: {', '.join(missing[:3])}"
    return itin


def price_all(itineraries, searcher, home, start_date, travellers, flex=0):
    """Price each itinerary, trying start_date +/- flex and keeping the best."""
    priced = []
    for itin in itineraries:
        if not itin.stops:
            priced.append(itin)          # already carries its reason
            continue

        best = None
        for offset in range(-flex, flex + 1):
            candidate = Itinerary(
                label=itin.label, kind=itin.kind, stops=list(itin.stops),
                nights_needed=itin.nights_needed,
            )
            price_itinerary(candidate, searcher, home,
                            start_date + timedelta(days=offset), travellers)
            if not candidate.complete:
                best = best or candidate
                continue
            if best is None or not best.complete or candidate.total < best.total:
                best = candidate
        priced.append(best or itin)
    return priced


# --------------------------------------------------------------------------
# CLI (country -> city + airport code lookup)
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Country -> city + airport code")
    ap.add_argument("country", nargs="?", help="e.g. Vietnam")
    ap.add_argument("--limit", type=int, default=2, help="how many cities")
    ap.add_argument("--list", action="store_true", help="list known countries")
    args = ap.parse_args()

    if args.list or not args.country:
        _load_from_mongo()
        for c in known_countries():
            codes = ", ".join(x[1] for x in _cache["countries"][c])
            print(f"{c.title():24} {codes}")
        return

    try:
        print(json.dumps(get_cities(args.country, args.limit), indent=2))
    except UnknownCountry as e:
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
