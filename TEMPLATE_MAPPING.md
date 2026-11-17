# Template Keyword Mapping System

## Problem Solved
Previously, "Marathon in India" triggered the "Ironman Events" template because "marathon" was hardcoded in the sports keywords. This has been fixed with a database-driven keyword mapping system.

## How It Works

### 1. **Database-Driven Keywords**
Each template now has `keywords` field in MongoDB:

```json
{
  "name": "Ironman Events",
  "keywords": ["ironman", "triathlon", "70.3"],
  "description": "For Ironman triathlon events only",
  "category": "sports"
}
```

### 2. **Smart Matching**
When you add an interest like "Marathon in India":
1. System fetches all templates from database
2. For each template, checks if ANY keyword appears in the interest text
3. Creates queries only for **exact keyword matches**
4. If NO template matches → uses "General Web Search" template

### 3. **Template Hierarchy**

| Template | Keywords | Use Case |
|----------|----------|----------|
| **International Travel** | travel, tourism, destination, abroad, international, bali, mauritius, thailand, europe, asia, etc. | Interests about traveling internationally |
| **Domestic Travel** | india, domestic, tamil nadu, kerala, goa, delhi, mumbai, bangalore, etc. | Interests about traveling within India |
| **Ironman Events** | ironman, triathlon, 70.3 | ONLY for Ironman triathlon events |
| **General Web Search** | (empty - catches all) | Default for unmatched interests |

## How to Update Keywords

### Method 1: Update via API
```bash
curl -X PUT http://localhost:8000/api/query-templates/{template_id}/keywords \
  -H "Content-Type: application/json" \
  -d '{
    "keywords": ["ironman", "triathlon", "half-ironman"],
    "description": "For Ironman triathlon events"
  }'
```

### Method 2: Update in MongoDB Directly
```javascript
db.query_templates.updateOne(
  { name: "Ironman Events" },
  { $set: { keywords: ["ironman", "triathlon", "70.3"] } }
)
```

### Method 3: Via Templates Management Page (UI - if implemented)
- Go to `/templates-page`
- Click on a template
- Edit keywords field
- Save

## Example Scenarios

### ✅ CORRECT - Now Works
- Interest: "Marathon in India"
  - ❌ Does NOT match "Ironman" template (only has "ironman", "triathlon", "70.3")
  - ✅ MATCHES "Domestic Travel" template (has "india")
  - Result: Domestic Travel query created

### ✅ CORRECT - Ironman Only
- Interest: "Ironman 70.3 India"
  - ✅ MATCHES "Ironman Events" template (has "ironman" and "70.3")
  - Result: Ironman Events query created

### ✅ CORRECT - Custom Sports
- Interest: "Cricket tournament in India"
  - ❌ Does NOT match any specific template (no exact keyword match)
  - ✅ USES "General Web Search" template (fallback)
  - Result: Web search query created

## Adding New Templates

When you add a new template, always include keywords:

```python
{
    "name": "Marathon Events",
    "template": "Find 5 upcoming marathons suitable for someone interested in: {interests}...",
    "category": "sports",
    "keywords": ["marathon", "half-marathon", "10k", "5k", "running event"],
    "description": "For marathon and running events"
}
```

## Restart Required
After making changes, restart FastAPI:
```bash
# Press Ctrl+C
python app.py
```

The Celery worker will automatically pick up the new templates for the next interests added.

## FAQ

**Q: What if an interest matches multiple templates?**
A: All matching templates will have queries created. For example, "Travel to Tokyo" matches both "International Travel" and could match general travel keywords.

**Q: Can I remove keywords?**
A: Yes - set `keywords: []` (empty array) to make it only catch by name or make it a fallback template.

**Q: What's "General Web Search"?**
A: It's the fallback template with no keywords. It catches ANY interest that doesn't match other templates. Perfect for custom/unique interests.

**Q: How do I debug which template was used?**
A: Check MongoDB:
```bash
python3 check_interests.py
```
Shows which templates were created for each interest.
