"""
Utility functions for data serialization and transformation
"""

from datetime import date, datetime
from bson.objectid import ObjectId
from bson.decimal128 import Decimal128

def serialize_objectid(obj):
    """Recursively convert Mongo/Python values to JSON-safe values."""
    if isinstance(obj, ObjectId):
        return str(obj)
    elif isinstance(obj, (datetime, date)):
        return obj.isoformat()
    elif isinstance(obj, Decimal128):
        return str(obj)
    elif isinstance(obj, dict):
        return {k: serialize_objectid(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [serialize_objectid(item) for item in obj]
    return obj
