"""
Interest management and query creation service
"""

from datetime import datetime, timezone
from bson.objectid import ObjectId
from .database import get_db
from .profile_handler import get_user_profile_context
from .celery_config import get_celery_app

async def create_queries_for_interest(interest_text: str):
    """Auto-create AI queries based on template matching"""
    try:
        db = get_db()
        celery_app = get_celery_app()

        # Get user profile context
        profile_context = await get_user_profile_context()

        # Get all templates
        all_templates = await db.query_templates.find().to_list(None)

        # Find best matching template based on 80% keyword similarity
        selected_template = None
        best_match_score = 0

        for template in all_templates:
            keywords = template.get("keywords", [])
            if not keywords:
                continue

            # Calculate match percentage
            matching_keywords = sum(
                1 for keyword in keywords
                if keyword.lower() in interest_text.lower()
            )
            match_percentage = (matching_keywords / len(keywords)) * 100 if keywords else 0

            # If >= 80% match and better than previous, select this template
            if match_percentage >= 80 and match_percentage > best_match_score:
                selected_template = template
                best_match_score = match_percentage

        # If no template with 80% match, use Default template
        if not selected_template:
            selected_template = await db.query_templates.find_one({"is_default": True})

        # If still no template found, skip creating queries
        if not selected_template:
            print(f"No matching template found for interest: {interest_text}")
            return

        template = selected_template

        # Build AI query by substituting {interests}, {limit}, and {profile} in the template
        ai_query_text = template.get("template", "").format(
            interests=interest_text,
            profile="below User Profile",
            limit=template.get("results", 5)
        )

        # Append profile context to AI query
        if profile_context:
            ai_query_text = f"{ai_query_text}\n\n{profile_context}"

        ai_query_doc = {
            "user_id": "Kamal",
            "template_name": template.get("name"),
            "template_id": template.get("_id"),
            "query_type": "ai",
            "interests": [interest_text],
            "user_interests": [interest_text],
            "ai_query": ai_query_text,
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat()
        }

        result = await db.queries.insert_one(ai_query_doc)
        ai_query_id = str(result.inserted_id)
        from .tasks import execute_query as execute_query_task
        execute_query_task.delay(ai_query_id)
        print(f"Created AI query using template '{template.get('name')}' ({best_match_score:.0f}% match) for interest: {interest_text}, query_id: {ai_query_id}")

    except Exception as e:
        print(f"Error creating queries: {e}")
