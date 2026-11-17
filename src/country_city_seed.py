"""One-time seed data for the `country_cities` / `country_city_aliases`
MongoDB collections.

This is the data that used to live in trip_country_city.py (now
trip_country_city_bkp.py, kept only for reference). database.py imports
SEED_COUNTRIES / SEED_ALIASES to populate the collections the first time the
app starts against an empty database; trip_country_city_planner.py then reads
those collections at runtime instead of this module.

Each country entry is a list of (city, IATA code, note, min_nights,
ideal_nights) - min/ideal nights are what trip_country_city.py's separate
CITY_DAYS table used to carry per airport code; they're folded in here so
each city is one self-contained record.
"""

DEFAULT_DAYS = (2, 3)

SEED_COUNTRIES = {
    "vietnam": [
        ("Phu Quoc", "PQC", "Island beaches, resort stays", 2, 3),
        ("Ho Chi Minh City", "SGN", "Main southern hub, best connected from India", 2, 3),
        ("Hanoi", "HAN", "Capital, gateway to Ha Long Bay and the north", 2, 3),
        ("Da Nang", "DAD", "Central coast, base for Hoi An and Hue", 2, 4),
    ],
    "thailand": [
        ("Bangkok", "BKK", "Primary hub, cheapest fares from India", 2, 3),
        ("Phuket", "HKT", "Andaman beaches and islands", 3, 4),
        ("Krabi", "KBV", "Quieter beaches, Railay and Ao Nang", 2, 3),
        ("Chiang Mai", "CNX", "Northern culture, temples, cooler hills", 2, 3),
    ],
    "indonesia": [
        ("Denpasar (Bali)", "DPS", "Bali; the usual reason to go", 3, 5),
        ("Jakarta", "CGK", "Capital and main air hub", 1, 2),
        ("Yogyakarta", "JOG", "Borobudur, Prambanan, Javanese culture", 2, 2),
    ],
    "malaysia": [
        ("Kuala Lumpur", "KUL", "Main hub, very cheap from South India", 2, 3),
        ("Penang", "PEN", "Food capital, colonial George Town", 2, 2),
        ("Langkawi", "LGK", "Island beaches and duty-free", 2, 3),
    ],
    "singapore": [
        ("Singapore", "SIN", "City-state; short trips, high cost base", 2, 3),
    ],
    "sri lanka": [
        ("Colombo", "CMB", "Main entry, short hop from South India", 2, 4),
    ],
    "nepal": [
        ("Kathmandu", "KTM", "Himalayan trekking and heritage", 3, 5),
    ],
    "united arab emirates": [
        ("Dubai", "DXB", "Major hub, frequent cheap fares", 2, 4),
        ("Abu Dhabi", "AUH", "Quieter alternative to Dubai", 2, 2),
    ],
    "uae": [
        ("Dubai", "DXB", "Major hub, frequent cheap fares", 2, 4),
        ("Abu Dhabi", "AUH", "Quieter alternative to Dubai", 2, 2),
    ],
    "japan": [
        ("Tokyo", "NRT", "Main entry point", 3, 5),
        ("Osaka", "KIX", "Kansai; Kyoto and Nara base", 2, 4),
    ],
    "philippines": [
        ("Manila", "MNL", "Main hub", 1, 2),
        ("Cebu", "CEB", "Islands, diving, beaches", 3, 4),
    ],
    "cambodia": [
        ("Siem Reap", "SAI", "Angkor Wat", 1, 2),
    ],
    "maldives": [
        ("Male", "MLE", "Resort islands", 3, 5),
    ],
    "india": [
        ("Bengaluru", "BLR", "Southern hub", 1, 2),
        ("Delhi", "DEL", "Northern hub", 2, 3),
        ("Mumbai", "BOM", "Western hub", 2, 3),
    ],
    "australia": [
        ("Sydney", "SYD", "Main gateway, harbour and beaches", 3, 4),
        ("Melbourne", "MEL", "Culture, coffee, Great Ocean Road", 3, 4),
        ("Brisbane", "BNE", "Queensland, gateway to the Barrier Reef", 2, 3),
        ("Perth", "PER", "Closest Australian city to India", 2, 3),
    ],
    "egypt": [
        ("Cairo", "CAI", "Pyramids, Egyptian Museum, main hub", 3, 4),
        ("Hurghada", "HRG", "Red Sea diving and resorts", 3, 4),
        ("Luxor", "LXR", "Valley of the Kings, Karnak", 2, 2),
    ],
    "new zealand": [
        ("Auckland", "AKL", "Main international gateway", 3, 4),
        ("Christchurch", "CHC", "South Island base", 2, 3),
    ],
    "united kingdom": [
        ("London", "LHR", "Main hub", 3, 5),
        ("Manchester", "MAN", "Northern England gateway", 2, 3),
    ],
    "france": [
        ("Paris", "CDG", "Main hub", 3, 4),
        ("Nice", "NCE", "Riviera", 2, 3),
    ],
    "italy": [
        ("Rome", "FCO", "Main hub", 3, 4),
        ("Milan", "MXP", "North, lakes", 2, 3),
    ],
    "spain": [
        ("Madrid", "MAD", "Main hub", 2, 3),
        ("Barcelona", "BCN", "Coast", 3, 4),
    ],
    "germany": [
        ("Frankfurt", "FRA", "Main hub", 2, 3),
        ("Munich", "MUC", "Bavaria", 2, 3),
    ],
    "switzerland": [
        ("Zurich", "ZRH", "Main hub", 2, 3),
        ("Geneva", "GVA", "Alps", 2, 3),
    ],
    "greece": [
        ("Athens", "ATH", "Main hub", 2, 3),
        ("Heraklion", "HER", "Crete", 3, 4),
    ],
    "south africa": [
        ("Johannesburg", "JNB", "Main hub, safari gateway", 2, 3),
        ("Cape Town", "CPT", "Coast, Table Mountain", 3, 4),
    ],
    "kenya": [
        ("Nairobi", "NBO", "Safari gateway", 3, 4),
    ],
    "mauritius": [
        ("Port Louis", "MRU", "Beaches and resorts", 4, 6),
    ],
    "seychelles": [
        ("Mahe", "SEZ", "Island beaches", 4, 6),
    ],
    "usa": [
        ("New York", "JFK", "East coast hub", 3, 5),
        ("San Francisco", "SFO", "West coast hub", 3, 4),
    ],
    "united states": [
        ("New York", "JFK", "East coast hub", 3, 5),
        ("San Francisco", "SFO", "West coast hub", 3, 4),
    ],
    "canada": [
        ("Toronto", "YYZ", "Main hub", 3, 4),
        ("Vancouver", "YVR", "West coast", 3, 4),
    ],
    "qatar": [
        ("Doha", "DOH", "Gulf hub", 1, 2),
    ],
    "oman": [
        ("Muscat", "MCT", "Gulf, beaches and forts", 2, 3),
    ],
    "bhutan": [
        ("Paro", "PBH", "Only international airport", 4, 6),
    ],
    "turkey": [
        ("Istanbul", "IST", "Europe/Asia crossover hub", 2, 4),
    ],
    "georgia": [
        ("Tbilisi", "TBS", "Cheap, visa-friendly for Indians", 2, 4),
    ],
    "azerbaijan": [
        ("Baku", "GYD", "Caspian, visa-friendly", 2, 3),
    ],
    "south korea": [
        ("Seoul", "ICN", "Main hub", 3, 4),
    ],
    "hong kong": [
        ("Hong Kong", "HKG", "Main hub", 2, 3),
    ],
    "taiwan": [
        ("Taipei", "TPE", "Main hub", 2, 4),
    ],
    "netherlands": [
        ("Amsterdam", "AMS", "Main hub", 2, 3),
    ],
}

# Accept common aliases and shorthand people actually type.
SEED_ALIASES = {
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

# Continent grouping for the Trips page's "countries interested in visiting"
# list. "usa"/"united states" and "uae"/"united arab emirates" are kept as
# separate entries here (matching SEED_COUNTRIES) even though they're
# duplicates - the countries API dedupes which one it displays.
SEED_CONTINENTS = {
    "vietnam": "Asia", "thailand": "Asia", "indonesia": "Asia",
    "malaysia": "Asia", "singapore": "Asia", "sri lanka": "Asia",
    "nepal": "Asia", "united arab emirates": "Asia", "uae": "Asia",
    "japan": "Asia", "philippines": "Asia", "cambodia": "Asia",
    "maldives": "Asia", "india": "Asia", "qatar": "Asia", "oman": "Asia",
    "bhutan": "Asia", "turkey": "Asia", "georgia": "Asia",
    "azerbaijan": "Asia", "south korea": "Asia", "hong kong": "Asia",
    "taiwan": "Asia",

    "united kingdom": "Europe", "france": "Europe", "italy": "Europe",
    "spain": "Europe", "germany": "Europe", "switzerland": "Europe",
    "greece": "Europe", "netherlands": "Europe",

    "egypt": "Africa", "south africa": "Africa", "kenya": "Africa",
    "mauritius": "Africa", "seychelles": "Africa",

    "australia": "Oceania", "new zealand": "Oceania",

    "usa": "North America", "united states": "North America",
    "canada": "North America",
}
