"""Country -> candidate cities with airport codes.

The trip planner works in countries ("Vietnam"), but flights.py needs an
airport code ("SGN"). This module bridges the two, offering a couple of
genuinely different options per country rather than only the capital, so a
trip can be planned around whichever is cheaper to reach.

    from src.trip_country_city import get_cities, get_city
    get_cities("Vietnam", limit=2)
    # [{'city': 'Ho Chi Minh City', 'code': 'SGN', ...}, {'city': 'Hanoi', ...}]

    python -m src.trip_country_city Vietnam
    python -m src.trip_country_city --list
"""

import argparse
import json
import sys

# city, IATA, short note on what the city is for. Ordered best-connected first,
# since the first entry is the default when only one city is requested.
COUNTRY_CITIES = {
    "vietnam": [
        ("Phu Quoc", "PQC", "Island beaches, resort stays"),
        ("Ho Chi Minh City", "SGN", "Main southern hub, best connected from India"),
        ("Hanoi", "HAN", "Capital, gateway to Ha Long Bay and the north"),
        ("Da Nang", "DAD", "Central coast, base for Hoi An and Hue"),
    ],
    "thailand": [
        ("Bangkok", "BKK", "Primary hub, cheapest fares from India"),
        ("Phuket", "HKT", "Andaman beaches and islands"),
        ("Krabi", "KBV", "Quieter beaches, Railay and Ao Nang"),
        ("Chiang Mai", "CNX", "Northern culture, temples, cooler hills"),
    ],
    "indonesia": [
        ("Denpasar (Bali)", "DPS", "Bali; the usual reason to go"),
        ("Jakarta", "CGK", "Capital and main air hub"),
        ("Yogyakarta", "JOG", "Borobudur, Prambanan, Javanese culture"),
    ],
    "malaysia": [
        ("Kuala Lumpur", "KUL", "Main hub, very cheap from South India"),
        ("Penang", "PEN", "Food capital, colonial George Town"),
        ("Langkawi", "LGK", "Island beaches and duty-free"),
    ],
    "singapore": [
        ("Singapore", "SIN", "City-state; short trips, high cost base"),
    ],
    "sri lanka": [
        ("Colombo", "CMB", "Main entry, short hop from South India"),
    ],
    "nepal": [
        ("Kathmandu", "KTM", "Himalayan trekking and heritage"),
    ],
    "united arab emirates": [
        ("Dubai", "DXB", "Major hub, frequent cheap fares"),
        ("Abu Dhabi", "AUH", "Quieter alternative to Dubai"),
    ],
    "uae": [
        ("Dubai", "DXB", "Major hub, frequent cheap fares"),
        ("Abu Dhabi", "AUH", "Quieter alternative to Dubai"),
    ],
    "japan": [
        ("Tokyo", "NRT", "Main entry point"),
        ("Osaka", "KIX", "Kansai; Kyoto and Nara base"),
    ],
    "philippines": [
        ("Manila", "MNL", "Main hub"),
        ("Cebu", "CEB", "Islands, diving, beaches"),
    ],
    "cambodia": [
        ("Siem Reap", "SAI", "Angkor Wat")
    ],
    "maldives": [
        ("Male", "MLE", "Resort islands"),
    ],
    "india": [
        ("Bengaluru", "BLR", "Southern hub"),
        ("Delhi", "DEL", "Northern hub"),
        ("Mumbai", "BOM", "Western hub"),
    ],
    "australia": [
        ("Sydney", "SYD", "Main gateway, harbour and beaches"),
        ("Melbourne", "MEL", "Culture, coffee, Great Ocean Road"),
        ("Brisbane", "BNE", "Queensland, gateway to the Barrier Reef"),
        ("Perth", "PER", "Closest Australian city to India"),
    ],
    "egypt": [
        ("Cairo", "CAI", "Pyramids, Egyptian Museum, main hub"),
        ("Hurghada", "HRG", "Red Sea diving and resorts"),
        ("Luxor", "LXR", "Valley of the Kings, Karnak"),
    ],
    "new zealand": [
        ("Auckland", "AKL", "Main international gateway"),
        ("Christchurch", "CHC", "South Island base"),
    ],
    "united kingdom": [
        ("London", "LHR", "Main hub"),
        ("Manchester", "MAN", "Northern England gateway"),
    ],
    "france": [("Paris", "CDG", "Main hub"), ("Nice", "NCE", "Riviera")],
    "italy": [("Rome", "FCO", "Main hub"), ("Milan", "MXP", "North, lakes")],
    "spain": [("Madrid", "MAD", "Main hub"), ("Barcelona", "BCN", "Coast")],
    "germany": [("Frankfurt", "FRA", "Main hub"), ("Munich", "MUC", "Bavaria")],
    "switzerland": [("Zurich", "ZRH", "Main hub"), ("Geneva", "GVA", "Alps")],
    "greece": [("Athens", "ATH", "Main hub"), ("Heraklion", "HER", "Crete")],
    "south africa": [
        ("Johannesburg", "JNB", "Main hub, safari gateway"),
        ("Cape Town", "CPT", "Coast, Table Mountain"),
    ],
    "kenya": [("Nairobi", "NBO", "Safari gateway")],
    "mauritius": [("Port Louis", "MRU", "Beaches and resorts")],
    "seychelles": [("Mahe", "SEZ", "Island beaches")],
    "usa": [("New York", "JFK", "East coast hub"),
            ("San Francisco", "SFO", "West coast hub")],
    "united states": [("New York", "JFK", "East coast hub"),
                      ("San Francisco", "SFO", "West coast hub")],
    "canada": [("Toronto", "YYZ", "Main hub"),
               ("Vancouver", "YVR", "West coast")],
    "qatar": [("Doha", "DOH", "Gulf hub")],
    "oman": [("Muscat", "MCT", "Gulf, beaches and forts")],
    "bhutan": [("Paro", "PBH", "Only international airport")],
    "turkey": [("Istanbul", "IST", "Europe/Asia crossover hub")],
    "georgia": [("Tbilisi", "TBS", "Cheap, visa-friendly for Indians")],
    "azerbaijan": [("Baku", "GYD", "Caspian, visa-friendly")],
    "south korea": [("Seoul", "ICN", "Main hub")],
    "hong kong": [("Hong Kong", "HKG", "Main hub")],
    "taiwan": [("Taipei", "TPE", "Main hub")],
    "netherlands": [("Amsterdam", "AMS", "Main hub")],
}

# Recommended nights per city: (minimum worth going for, ideal stay).
# The trip planner uses these to work out how long an itinerary needs and how
# to split days when combining countries. Defaults apply to anything missing.
CITY_DAYS = {
    # Vietnam
    "SGN": (2, 3), "HAN": (2, 3), "DAD": (2, 4), "PQC": (2, 3),
    # Thailand
    "BKK": (2, 3), "HKT": (3, 4), "KBV": (2, 3), "CNX": (2, 3),
    # Indonesia / Malaysia / Singapore
    "DPS": (3, 5), "CGK": (1, 2), "JOG": (2, 2),
    "KUL": (2, 3), "PEN": (2, 2), "LGK": (2, 3), "SIN": (2, 3),
    # South Asia / Gulf
    "CMB": (2, 4), "KTM": (3, 5), "MLE": (3, 5),
    "DXB": (2, 4), "AUH": (2, 2), "DOH": (1, 2), "MCT": (2, 3),
    "PBH": (4, 6),
    # East / South-East Asia
    "NRT": (3, 5), "KIX": (2, 4), "ICN": (3, 4), "HKG": (2, 3),
    "TPE": (2, 4), "MNL": (1, 2), "CEB": (3, 4),
    "SAI": (1, 2), "PNH": (1, 2),
    # Caucasus / Turkey
    "IST": (2, 4), "TBS": (2, 4), "GYD": (2, 3),
    # Australia / NZ
    "SYD": (3, 4), "MEL": (3, 4), "BNE": (2, 3), "PER": (2, 3),
    "AKL": (3, 4), "CHC": (2, 3),
    # Africa / Egypt
    "CAI": (3, 4), "HRG": (3, 4), "LXR": (2, 2),
    "JNB": (2, 3), "CPT": (3, 4), "NBO": (3, 4), "MRU": (4, 6),
    "SEZ": (4, 6),
    # Europe / Americas
    "LHR": (3, 5), "MAN": (2, 3), "CDG": (3, 4), "NCE": (2, 3),
    "FCO": (3, 4), "MXP": (2, 3), "MAD": (2, 3), "BCN": (3, 4),
    "FRA": (2, 3), "MUC": (2, 3), "ZRH": (2, 3), "GVA": (2, 3),
    "ATH": (2, 3), "HER": (3, 4), "AMS": (2, 3),
    "JFK": (3, 5), "SFO": (3, 4), "YYZ": (3, 4), "YVR": (3, 4),
    # India
    "BLR": (1, 2), "DEL": (2, 3), "BOM": (2, 3),
}

DEFAULT_DAYS = (2, 3)


def city_days(code):
    """(minimum, ideal) nights worth spending in the city with this code."""
    return CITY_DAYS.get(code.upper(), DEFAULT_DAYS)


# Accept common aliases and shorthand people actually type.
ALIASES = {
    "bali": "indonesia",
    "emirates": "united arab emirates",
    "u.a.e.": "united arab emirates",
    "korea": "south korea",
    "srilanka": "sri lanka",
    "lanka": "sri lanka",
    "siam": "thailand",
    "nippon": "japan",
    "hongkong": "hong kong",
    "uk": "united kingdom",
    "britain": "united kingdom",
    "england": "united kingdom",
    "america": "usa",
    "u.s.": "usa",
    "u.s.a.": "usa",
    "nz": "new zealand",
    "holland": "netherlands",
}


class UnknownCountry(KeyError):
    """Raised when a country isn't in the table."""


def _key(country: str) -> str:
    k = " ".join(country.strip().lower().replace("_", " ").split())
    return ALIASES.get(k, k)


def get_cities(country: str, limit: int = 2):
    """Return up to `limit` candidate cities for a country.

    Each entry: {"country", "city", "code", "note"}.
    Raises UnknownCountry if the country isn't in the table.
    """
    k = _key(country)
    if k not in COUNTRY_CITIES:
        raise UnknownCountry(
            f"No cities known for {country!r}. Known: "
            f"{', '.join(sorted(COUNTRY_CITIES))}"
        )
    rows = COUNTRY_CITIES[k][: max(1, limit)]
    out = []
    for c, code, note in rows:
        lo, hi = city_days(code)
        out.append({
            "country": country.strip().title(), "city": c, "code": code,
            "note": note, "min_nights": lo, "ideal_nights": hi,
        })
    return out


def get_city(country: str):
    """The single best-connected city for a country."""
    return get_cities(country, limit=1)[0]


def country_plan(country, max_cities=2):
    """Cities for a country plus how long covering them takes.

    Returns {"country", "cities": [...], "min_nights", "ideal_nights"} where the
    night totals are the sum across the chosen cities. This is what the
    combination planner uses to decide whether a country fits a trip window.
    """
    cities = get_cities(country, max_cities)
    return {
        "country": cities[0]["country"],
        "cities": cities,
        "min_nights": sum(c["min_nights"] for c in cities),
        "ideal_nights": sum(c["ideal_nights"] for c in cities),
    }


def known_countries():
    return sorted({ALIASES.get(k, k) for k in COUNTRY_CITIES})


def main():
    ap = argparse.ArgumentParser(description="Country -> city + airport code")
    ap.add_argument("country", nargs="?", help="e.g. Vietnam")
    ap.add_argument("--limit", type=int, default=2, help="how many cities")
    ap.add_argument("--list", action="store_true", help="list known countries")
    args = ap.parse_args()

    if args.list or not args.country:
        for c in known_countries():
            codes = ", ".join(x[1] for x in COUNTRY_CITIES[c])
            print(f"{c.title():24} {codes}")
        return

    try:
        print(json.dumps(get_cities(args.country, args.limit), indent=2))
    except UnknownCountry as e:
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
