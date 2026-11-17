"""
Template matching logic to map interests to relevant templates
Uses keyword mappings stored in database
"""

from .database import get_db

async def template_matches_interest_async(template_name: str, interest_text: str) -> bool:
    """Check if a template is relevant to the interest using database keywords"""
    db = get_db()

    # Get template from database
    template = await db.query_templates.find_one({"name": template_name})
    if not template:
        return False

    interest_lower = interest_text.lower()
    template_keywords = template.get("keywords", [])

    # If template has keywords defined, use them
    if template_keywords:
        # Check if any keyword matches the interest (case-insensitive)
        return any(keyword.lower() in interest_lower for keyword in template_keywords)

    # General/default template matches anything without keywords
    if template_name == "General Web Search":
        return True

    return False

def template_matches_interest(template_name: str, interest_text: str) -> bool:
    """
    Synchronous wrapper - for backwards compatibility
    Should be replaced with async version in interest_service.py
    """
    # Default fallback behavior
    template_lower = template_name.lower()
    interest_lower = interest_text.lower()

    if "international" in template_lower:
        return any(k in interest_lower for k in ["travel", "tourism", "international", "abroad"])
    elif "domestic" in template_lower:
        return any(k in interest_lower for k in ["india", "domestic", "travel", "goa", "delhi"])
    elif "ironman" in template_lower:
        return "ironman" in interest_lower or "70.3" in interest_lower

    return False
