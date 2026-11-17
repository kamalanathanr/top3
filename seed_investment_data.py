"""
One-time seed script: loads investment_overview.json into MongoDB
Run: python seed_investment_data.py
"""

import json
import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime

MONGO_URL = "mongodb://admin:admin@localhost:27017/"
DB_NAME = "top3"
COLLECTION = "investment_overview"
JSON_FILE = "investment_overview.json"


async def seed():
    client = AsyncIOMotorClient(MONGO_URL)
    db = client[DB_NAME]
    collection = db[COLLECTION]

    with open(JSON_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    metadata = {
        "data_as_of": data.get("data_as_of"),
        "geography": data.get("geography"),
        "currency": data.get("currency"),
        "sources": data.get("sources", []),
    }

    instruments = data.get("instruments", [])

    # Drop existing documents so re-runs are idempotent
    await collection.drop()

    # Stamp each instrument with metadata + seeded_at
    docs = []
    for instrument in instruments:
        doc = {**instrument, **metadata, "seeded_at": datetime.utcnow().isoformat()}
        docs.append(doc)

    result = await collection.insert_many(docs)

    # Indexes for common query patterns
    await collection.create_index("investment_name", unique=True)
    await collection.create_index("category")
    await collection.create_index("sub_type")

    print(f"Inserted {len(result.inserted_ids)} instruments into '{COLLECTION}'")
    print(f"Categories: {sorted(set(d['category'] for d in docs))}")

    client.close()


if __name__ == "__main__":
    asyncio.run(seed())
