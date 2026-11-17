"""
Query template routes
"""

from fastapi import APIRouter
from pydantic import BaseModel
from typing import List, Optional
from bson.objectid import ObjectId
from datetime import datetime, timezone

from ..database import get_db
from ..models import TemplateUpdate
from ..utils import serialize_objectid

class TemplateKeywordsUpdate(BaseModel):
    """Update template keywords"""
    keywords: List[str] = []
    description: Optional[str] = None
    category: Optional[str] = None

class TemplateCreate(BaseModel):
    """Create new template"""
    name: str
    template: str
    category: str
    keywords: List[str] = []
    description: Optional[str] = None
    results: int = 5
    is_default: bool = False

router = APIRouter(prefix="/api/query-templates")

@router.get("")
async def list_templates():
    """List all query templates"""
    db = get_db()
    templates = await db.query_templates.find().to_list(None)
    templates = [serialize_objectid(t) for t in templates]
    return {"templates": templates}

@router.put("/{template_id}")
async def update_template(template_id: str, request: TemplateUpdate):
    """Update a query template"""
    db = get_db()

    update_data = {"template": request.template}

    if request.name:
        update_data["name"] = request.name
    if request.category:
        update_data["category"] = request.category
    if request.keywords is not None:
        update_data["keywords"] = request.keywords
    if request.description is not None:
        update_data["description"] = request.description
    if request.results:
        update_data["results"] = request.results
    if request.is_default is not None:
        update_data["is_default"] = request.is_default
        # If setting as default, unset other defaults
        if request.is_default:
            await db.query_templates.update_many(
                {"_id": {"$ne": ObjectId(template_id)}, "is_default": True},
                {"$set": {"is_default": False}}
            )

    result = await db.query_templates.update_one(
        {"_id": ObjectId(template_id)},
        {"$set": update_data}
    )
    return {"updated": result.modified_count > 0}

@router.put("/{template_id}/keywords")
async def update_template_keywords(template_id: str, request: TemplateKeywordsUpdate):
    """Update template keywords and description"""
    db = get_db()
    update_data = {
        "keywords": request.keywords,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    if request.description:
        update_data["description"] = request.description
    if request.category:
        update_data["category"] = request.category

    result = await db.query_templates.update_one(
        {"_id": ObjectId(template_id)},
        {"$set": update_data}
    )
    return {
        "updated": result.modified_count > 0,
        "keywords": request.keywords,
        "message": f"Template keywords updated. Now matches interests containing: {', '.join(request.keywords) if request.keywords else 'none (fallback)'}"
    }

@router.post("")
async def create_template(request: TemplateCreate):
    """Create a new query template"""
    db = get_db()

    # Check if template name already exists
    existing = await db.query_templates.find_one({"name": request.name})
    if existing:
        return {
            "success": False,
            "error": f"Template '{request.name}' already exists"
        }

    # If setting as default, unset any other defaults
    if request.is_default:
        await db.query_templates.update_many(
            {"is_default": True},
            {"$set": {"is_default": False}}
        )

    template_doc = {
        "name": request.name,
        "template": request.template,
        "category": request.category,
        "keywords": request.keywords,
        "description": request.description or "",
        "results": request.results,
        "is_default": request.is_default,
        "created_at": datetime.now(timezone.utc).isoformat()
    }

    result = await db.query_templates.insert_one(template_doc)

    return {
        "success": True,
        "template_id": str(result.inserted_id),
        "message": f"Template '{request.name}' created successfully"
    }
