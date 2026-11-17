"""
Countries routes — serves the country_cities collection to the Trips page's
"Countries interested in visiting" list, grouped by continent.
"""

from fastapi import APIRouter

from ..database import get_db

router = APIRouter(prefix="/api/countries")

# Duplicate country_key entries that share identical city data with a more
# common name (see country_city_seed.py) - excluded from the display list so
# the same country doesn't show up twice.
DISPLAY_EXCLUDE = {"usa", "uae"}


@router.get("")
async def list_countries():
    db = get_db()
    docs = await db.country_cities.find(
        {"country_key": {"$nin": list(DISPLAY_EXCLUDE)}}
    ).to_list(length=None)

    continents = {}
    for doc in docs:
        continent = doc.get("continent", "Other")
        continents.setdefault(continent, []).append({
            "key": doc["country_key"],
            "name": doc["country_key"].title(),
            "cities": [
                {"city": c["city"], "code": c["code"]}
                for c in doc.get("cities", [])
            ],
        })

    for countries in continents.values():
        countries.sort(key=lambda c: c["name"])

    ordered = {}
    for continent in sorted(continents, key=lambda c: (c == "Other", c)):
        ordered[continent] = continents[continent]

    return {"continents": ordered}
