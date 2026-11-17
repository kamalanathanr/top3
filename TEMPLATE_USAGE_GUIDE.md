# Template Management Guide

## Overview
Templates determine what AI analysis is performed when you add an interest. Keywords control which template is used.

## How It Works

### 1. **User Adds Interest**
```
Interest: "Marathon in India"
    ↓
System checks all templates for keyword matches
    ↓
"Domestic Travel" template matches (has "india" keyword)
    ↓
Creates: Domestic Travel query for "Marathon in India"
```

### 2. **Keyword Matching**
- **Exact matching** — looks for keywords in the interest text (case-insensitive)
- **All templates checked** — if multiple match, all get queries
- **Fallback** — if NO template matches → uses "General Web Search"

### 3. **Template Hierarchy**

| Template | Keywords | Category | Use For |
|----------|----------|----------|---------|
| **International Travel** | travel, tourism, destination, abroad, bali, mauritius, etc. | travel | International destinations |
| **Domestic Travel** | india, domestic, tamil nadu, kerala, goa, delhi, etc. | travel | India-specific destinations |
| **Ironman Events** | ironman, triathlon, 70.3 | sports | ONLY Ironman events |
| **General Web Search** | (empty) | general | Fallback - matches anything |

## Using the Templates Page

### ✅ View & Edit Templates

1. Go to `/templates-page`
2. Each template card shows:
   - **Name** — Template title
   - **Category** — travel, sports, business, etc.
   - **Keywords** — What triggers this template
   - **Description** — When to use it
   - **Query Template** — The actual AI prompt

### ✅ Edit Keywords

1. Click **"Edit Keywords"** button on any template
2. Modal opens with:
   - **Keywords** — comma-separated list
   - **Category** — dropdown to change
   - **Description** — when to use this template
3. Click **"Save Keywords"**
4. System immediately uses new keywords for next interests

### ✅ Edit Query Template

1. Modify the textarea with the AI prompt
2. Click **"Save Template"**
3. New prompt used for future queries

### ✅ Create New Template

1. Click **"+ Add Template"** button at top
2. Fill in:
   - **Template Name** — e.g., "Wedding Planning"
   - **Category** — travel, sports, business, entertainment, learning, health, general
   - **Keywords** — e.g., wedding, bride, groom, ceremony
   - **Description** — e.g., "For wedding and event planning"
   - **Query Template** — The AI prompt (use `{interests}` and `{profile}` as placeholders)
3. Click **"Create Template"**

## Example: Create Custom Template

### Scenario
You want to handle "Wedding Planning" interests differently.

### Steps

1. **Click "+ Add Template"**

2. **Fill Form:**
   - Name: `Wedding Planning`
   - Category: `Entertainment`
   - Keywords: `wedding, bride, groom, engagement, ceremony, reception, bridal, honeymoon`
   - Description: `For wedding and event planning`
   - Template:
     ```
     Based on user interests in {interests}, suggest 5 wedding planning ideas suitable for a {profile}. 
     Include:
     - Venue suggestions with capacity and ambiance
     - Estimated budget range
     - Timeline recommendations
     - Key vendors needed
     - Unique ideas to personalize
     ```

3. **Click "Create Template"**

4. **Test It:**
   - Add interest: "Wedding Planning in Goa"
   - System finds "Wedding Planning" template (matches "wedding")
   - Creates query: "Based on Wedding Planning in Goa..."

## Debugging: Check Which Template Was Used

Run the diagnostic script:
```bash
python3 check_interests.py
```

Output shows:
```
Interest: "Wedding Planning in Goa"
  • Wedding Planning: done
  • General Web Search: done
```

This means BOTH templates matched! The "Wedding Planning" template matched (has "wedding" keyword) AND "General Web Search" matched (it's the fallback).

### To Fix:
If you don't want "General Web Search" to match, don't create a query for it when other templates match. Or make sure your custom template keywords are specific enough.

## Best Practices

### ✅ DO
- Be **specific with keywords** — "ironman", "70.3" not "ironman, marathon, race"
- Test **before deploying** — check `check_interests.py` output
- Use **meaningful categories** — helps organize templates
- Write **clear descriptions** — explains when template applies
- Use **placeholders** — `{interests}`, `{profile}` in templates

### ❌ DON'T
- Add **too many keywords** — "marathon" in "Ironman" template causes mismatches
- Leave **keywords empty** — makes template a fallback (matches everything)
- Overlap keywords between specific templates — causes multiple queries

## API Reference

### List Templates
```bash
GET /api/query-templates
```

### Create Template
```bash
POST /api/query-templates
{
  "name": "Wedding Planning",
  "category": "entertainment",
  "keywords": ["wedding", "bride", "groom"],
  "description": "For wedding planning",
  "template": "Based on {interests}..."
}
```

### Update Keywords
```bash
PUT /api/query-templates/{template_id}/keywords
{
  "keywords": ["ironman", "triathlon"],
  "category": "sports",
  "description": "For Ironman triathlon events"
}
```

### Update Template
```bash
PUT /api/query-templates/{template_id}
{
  "template": "New AI prompt template..."
}
```

## Restart Required

After making changes, **restart FastAPI**:
```bash
# Press Ctrl+C in FastAPI terminal
python app.py
```

Changes take effect immediately for new interests added.

## Common Issues

### ❌ Wrong Template Used
- **Check keywords** — does interest contain the keyword?
- **Run diagnostic** — `python3 check_interests.py`
- **Update keywords** — click "Edit Keywords" on Templates page

### ❌ Multiple Templates Creating Queries
- **Expected behavior** — if interest matches multiple templates, all create queries
- **Solution** — make keywords more specific to avoid overlap

### ❌ "General Web Search" Always Used
- **Check** — does interest contain ANY keyword from other templates?
- **If no keywords match** — system correctly falls back to General Web Search
- **Update** — add relevant keywords to template

## Quick Checklist

- [ ] Reviewed default templates (International Travel, Domestic Travel, Ironman, General)
- [ ] Updated keywords if they're too broad
- [ ] Created custom templates for unique interests
- [ ] Tested with `check_interests.py`
- [ ] Restarted FastAPI server
