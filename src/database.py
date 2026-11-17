"""
Database connection and initialization module
Handles MongoDB connection setup and index creation
"""

import motor.motor_asyncio
from datetime import datetime

from src.country_city_seed import SEED_COUNTRIES, SEED_ALIASES, SEED_CONTINENTS
from src.news_service import MAX_RETENTION_DAYS as NEWS_RETENTION_DAYS

MONGO_URL = "mongodb://admin:admin@localhost:27017/"
DB_NAME = "top3"

mongo_client = None
db = None

async def connect_database():
    """Initialize MongoDB connection"""
    global mongo_client, db
    mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URL)
    db = mongo_client[DB_NAME]
    print("MongoDB connected")

async def disconnect_database():
    """Close MongoDB connection"""
    global mongo_client
    if mongo_client:
        mongo_client.close()
        print("MongoDB disconnected")

async def initialize_database():
    """Create indexes and seed initial data"""
    global db

    # Create indexes
    await db.interests.create_index("user_id")
    await db.query_templates.create_index("name", unique=True)
    await db.queries.create_index([("user_id", 1), ("created_at", -1)])
    await db.query_results.create_index("query_id")
    await db.user_profiles.create_index("user_id", unique=True)
    await db.trip_details.create_index("user_id", unique=True)
    await db.bonds.create_index("isin", unique=True)
    await db.bonds.create_index("bond_platform")
    await db.country_cities.create_index("country_key", unique=True)
    await db.news_items.create_index("link", unique=True)
    await db.news_items.create_index([("score", -1), ("published_at", -1)])
    await db.news_items.create_index("published_at", expireAfterSeconds=NEWS_RETENTION_DAYS * 24 * 3600)
    # Liked stories — deliberately no TTL / retention cleanup
    await db.news_saved.create_index("link", unique=True)
    await db.news_saved.create_index([("user_id", 1), ("liked_at", -1)])
    # Refresh history for the Logs page — kept 30 days
    await db.news_refresh_log.create_index("queued_at", expireAfterSeconds=30 * 24 * 3600)
    # Upcoming-events timelines (src/news_events.py)
    await db.news_events.create_index([("tab", 1), ("start_date", 1)])
    await db.news_items.create_index("image", sparse=True)   # /api/news/img lookups
    # Event link checks — cached results, expire after 30 days
    await db.news_link_checks.create_index("checked_at", expireAfterSeconds=30 * 24 * 3600)

    # Seed user profile if not present
    user_profile_exists = await db.user_profiles.find_one({"user_id": "Kamal"})
    if not user_profile_exists:
        await db.user_profiles.insert_one({
            "user_id": "Kamal",
            "qualification": "",
            "family_details": "",
            "hobbies": [],
            "history": "",
            "interests": [],
            "strategies": "",
            "timezone": "Asia/Kolkata"
        })

    # Seed query templates if not present
    templates_count = await db.query_templates.count_documents({})
    if templates_count == 0:
        templates = [
            {
                "name": "International Travel",
                "template": "Based on user interests: {interests}, suggest 5 best international travel destinations that suit a {profile}. Include destination name, why it's suitable, estimated cost, best time to visit, and key attractions.",
                "category": "travel",
                "keywords": ["travel", "tourism", "destination", "abroad", "international", "overseas", "mauritius", "bali", "thailand", "paris", "europe", "asia", "africa", "island", "maldives", "sri lanka", "singapore", "dubai"],
                "description": "For international travel interests"
            },
            {
                "name": "Domestic Travel",
                "template": "Based on user interests: {interests}, suggest 5 best domestic travel destinations. Include destination name, why it's suitable, estimated cost, best time to visit, and key attractions.",
                "category": "travel",
                "keywords": ["india", "domestic", "local", "within india", "tamil nadu", "kerala", "goa", "rajasthan", "delhi", "mumbai", "bangalore", "hyderabad", "agra", "himalaya"],
                "description": "For domestic (India) travel interests"
            },
            {
                "name": "Ironman Events",
                "template": "Find 5 upcoming Ironman events suitable for someone interested in: {interests}. IMPORTANT: Return ONLY the structured list in this exact format, with NO introduction or explanation:\n\n**1. Event Name (Location)**\n- Location: City/Country\n- Date: Month/Season\n- Distance: 1.9km swim + 90km bike + 21.1km run\n- Registration Cost: Amount in USD\n- Difficulty: Level\n\n**2. Event Name (Location)**\n- Location: ...\n\nDo NOT include any permission requests, explanations, or conversational text. Return ONLY the numbered list.",
                "category": "sports",
                "keywords": ["ironman", "triathlon", "70.3"],
                "description": "For Ironman triathlon events only"
            },
            {
                "name": "General Web Search",
                "template": "Based on the user's interest in {interests}, provide 5 personalized recommendations, suggestions, or information that would be most helpful. Format as a structured list with clear categories and actionable details.",
                "category": "general",
                "keywords": [],
                "description": "Default template for any interest that doesn't match specific templates"
            }
        ]
        await db.query_templates.insert_many(templates)

    # Ensure MarketPulse template exists
    await db.query_templates.update_one(
        {"name": "MarketPulse"},
        {"$setOnInsert": {
            "name": "MarketPulse",
            "template": (
                "You are a financial data expert for India. Provide CURRENT market data for "
                "India investment instruments (FDs, Mutual Funds, ETFs, Bonds, Stocks, Gold, "
                "Real Estate) as of today. Return ONLY valid JSON with an 'instruments' array "
                "where each item has: investment_name, category, sub_type, return_profile "
                "(return_type, expected_annual_return, return_frequency, income_stability), "
                "risk_safety (risk_level, capital_safety, credit_quality, volatility), "
                "liquidity (lock_in_period, exit_ease, exit_cost), taxation (tax_on_returns, "
                "tax_benefits), entry_details (minimum_investment, mode), and "
                "realistic_expectation_summary (best_case_scenario_return, "
                "average_expected_return, worst_case_scenario). No explanation or markdown."
            ),
            "category": "MarketPulse",
            "keywords": ["investment", "market", "india", "stocks", "mutual funds"],
            "description": "MarketPulse: Investment instrument refresh for India market"
        }},
        upsert=True
    )

    # Ensure EconomyIndicator template exists
    await db.query_templates.update_one(
        {"name": "EconomyIndicator"},
        {"$setOnInsert": {
            "name": "EconomyIndicator",
            "template": (
                "You are a financial data expert for India. Provide the CURRENT values for the "
                "following economic indicators as of today. Return ONLY valid JSON — no "
                "explanation, no markdown, no code blocks.\n\n"
                "Use this exact schema:\n"
                "{\n"
                '  "data_as_of": "Month Year",\n'
                '  "indicators": [\n'
                '    {"indicator": "Repo Rate", "value": "X%"},\n'
                '    {"indicator": "CPI Inflation", "value": "X%"},\n'
                '    {"indicator": "10Y Bond Yield", "value": "X%"},\n'
                '    {"indicator": "NIFTY PE", "value": "XX.X"},\n'
                '    {"indicator": "India VIX", "value": "XX.X"},\n'
                '    {"indicator": "Gold Price Trend", "value": "Bullish/Bearish/Neutral — brief reason"},\n'
                '    {"indicator": "USD/INR", "value": "XX.XX"},\n'
                '    {"indicator": "Crude Oil (Brent)", "value": "USD XX per barrel"},\n'
                '    {"indicator": "Gold 22K (per gram)", "value": "₹XXXXX"}\n'
                "  ]\n"
                "}\n\n"
                "Return ONLY the JSON object with accurate current values."
            ),
            "category": "EconomyIndicator",
            "keywords": ["economy", "india", "repo rate", "inflation", "bond yield", "nifty", "vix", "gold", "crude"],
            "description": "EconomyIndicator: Key India economic indicators"
        }},
        upsert=True
    )

    # Seed country -> city/airport data (used by trip_country_city_planner.py)
    # if not present. Source data lives in country_city_seed.py.
    country_cities_count = await db.country_cities.count_documents({})
    if country_cities_count == 0:
        docs = [
            {
                "country_key": country_key,
                "continent": SEED_CONTINENTS.get(country_key, "Other"),
                "cities": [
                    {
                        "city": city, "code": code, "note": note,
                        "min_nights": min_nights, "ideal_nights": ideal_nights,
                    }
                    for city, code, note, min_nights, ideal_nights in cities
                ],
            }
            for country_key, cities in SEED_COUNTRIES.items()
        ]
        await db.country_cities.insert_many(docs)
    else:
        # Backfill continent on docs seeded before this field existed.
        for country_key, continent in SEED_CONTINENTS.items():
            await db.country_cities.update_one(
                {"country_key": country_key, "continent": {"$exists": False}},
                {"$set": {"continent": continent}},
            )

    aliases_exists = await db.country_city_aliases.find_one({"_id": "aliases"})
    if not aliases_exists:
        await db.country_city_aliases.insert_one({
            "_id": "aliases",
            "map": SEED_ALIASES,
        })

    print("Database initialized")

def get_db():
    """Get database instance"""
    return db
