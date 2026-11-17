## Task Delegation

When spawning subagents, use the cheapest model that can handle the task:
- Haiku: bulk mechanical tasks - file ops, formatting, renaming, 
  simple transformations. No judgment required.
- Sonnet: scoped research, code exploration, summarization, 
  synthesis across sources.
- Opus: only when real planning or tradeoffs are involved - 
  architecture, ambiguous requirements, high-stakes decisions.

### Spawn rules:
- Haiku subagents cannot spawn further subagents. 
  If they need to, the task was wrong-sized - return to parent.
- Max spawn depth: 2 (parent → subagent → one more tier, no deeper)
- If a subagent realizes it needs a smarter model, 
  it returns to the parent instead of escalating on its own.
  
# Top3 — AI Discovery Platform

## Architecture

```
Browser (HTMX + Tailwind)
       │
FastAPI (app.py)
       │
   ┌───┴───────────────┐
Motor (async)       Redis client
   │                    │
MongoDB             Celery Worker (tasks.py)
                        │
                   Claude CLI subprocess
                   YouTube (yt-dlp)
```

## How It Works

1. User adds an interest on the home page
2. FastAPI inserts into `interests` collection and calls `create_queries_for_interest()`
3. `interest_service.py` matches interest keywords against `query_templates` (≥80% keyword match → use that template, else use `is_default=True`)
4. A query document is inserted into `queries` with `status: pending`
5. `execute_query.delay(query_id)` enqueues to Redis/Celery
6. Celery worker runs Claude CLI subprocess with the formatted prompt
7. Output is stored in `query_results`; query status updated to `done`
8. Home page polls `/api/results` and renders tabs per extracted item

---

## File Structure

```
top3/
├── app.py                        # FastAPI app factory, router registration, startup events
├── executor.py                   # Legacy Celery worker entry point
├── requirements.txt
├── docker-compose.yml            # MongoDB + Redis containers
│
├── src/
│   ├── database.py               # Motor connection, index creation, seeds default templates
│   ├── celery_config.py          # Celery app connected to Redis broker
│   ├── interest_service.py       # Template keyword matching (80% rule), query creation
│   ├── tasks.py                  # Celery task: execute_query (AI + YouTube branches)
│   ├── claude_executor.py        # subprocess.run(['claude', '-p', prompt]) + output filtering
│   ├── search_engine.py          # Web search helper for event/sport queries
│   ├── youtube_search.py         # yt-dlp wrapper for YouTube search
│   ├── template_matcher.py       # Standalone keyword matcher utility
│   ├── profile_handler.py        # get_user_profile_context() — formats DB profile as string
│   ├── timezone_utils.py         # Timezone helpers (default: Asia/Kolkata)
│   ├── models.py                 # Pydantic models: InterestRequest, TemplateUpdate, UserProfile
│   ├── utils.py                  # serialize_objectid() helper
│   └── routes/
│       ├── pages.py              # Serves HTML files (/, /monitor, /templates-page, etc.)
│       ├── interests.py          # POST /add-interest, GET /interests, POST /delete-interest
│       ├── templates.py          # CRUD /api/query-templates
│       ├── queries.py            # CRUD /api/queries, pause/cancel
│       ├── results.py            # GET/PUT /api/results (with $lookup join to queries)
│       ├── profile.py            # GET/PUT /api/profile
│       └── video.py              # GET /video/{id}, POST /api/search-youtube/{id}
│
└── templates/
    ├── index.html                # Home: interests list + tabbed results + related videos
    ├── monitor.html              # Queue monitor: query statuses, pause/cancel controls
    ├── query_templates.html      # Template editor: left sidebar list + right edit panel
    ├── results.html              # Results browser: table view of all query_results
    └── profile.html              # User profile editor
```

---

## MongoDB Collections

### `interests`
```json
{
  "user_id": "Kamal",
  "text": "AI Meet-up at bengaluru",
  "added_at": "ISO datetime",
  "status": "active | deleted",
  "videos": [{ "id": "...", "title": "...", "url": "..." }]
}
```

### `query_templates`
```json
{
  "name": "General Web Search",
  "template": "Find {limit} results for {interests}. User profile: {profile}",
  "category": "general | travel | sports | business | entertainment | learning | health",
  "keywords": ["keyword1", "keyword2"],
  "description": "When to use this template",
  "results": 5,
  "is_default": true,
  "created_at": "ISO datetime"
}
```

### `queries`
```json
{
  "user_id": "Kamal",
  "template_name": "General Web Search",
  "template_id": "ObjectId",
  "query_type": "ai | youtube",
  "user_interests": ["AI Meet-up at bengaluru"],
  "interests": ["AI Meet-up at bengaluru"],
  "ai_query": "full formatted prompt sent to Claude",
  "status": "pending | running | done | error | paused | cancelled",
  "created_at": "ISO datetime",
  "started_at": "ISO datetime",
  "completed_at": "ISO datetime",
  "error": "error message if failed"
}
```

### `query_results`
```json
{
  "query_id": "ObjectId",
  "user_id": "Kamal",
  "template_name": "General Web Search",
  "interests": ["AI Meet-up at bengaluru"],
  "output": { "text": "Claude response text" },
  "time_taken_sec": 4.2,
  "completed_at": "ISO datetime"
}
```

### `user_profiles`
```json
{
  "user_id": "Kamal",
  "qualification": "",
  "family_details": "",
  "hobbies": [],
  "history": "",
  "interests": [],
  "strategies": "",
  "timezone": "Asia/Kolkata"
}
```

---

## Query Template Placeholders

| Placeholder | Replaced With |
|-------------|--------------|
| `{interests}` | The interest text the user added |
| `{limit}` | `results` field from `query_templates` collection |
| `{profile}` | "below User Profile" (followed by full profile context appended) |

**Recommended template output format** (for reliable item extraction):
```
**1. Item Title**
Description text here.
- Key point 1
- Key point 2

**2. Item Title**
Description text here.
- Key point 1
```

The frontend extracts items by splitting on `**N. Title**` patterns. If that fails, it falls back to `# Heading` format, then blank-line-separated blocks.

---

## Template Matching Logic (`interest_service.py`)

1. Fetch all templates from `query_templates`
2. For each template, count how many of its `keywords` appear in the interest text
3. If `matching / total >= 80%` → use that template (pick best match)
4. If no template reaches 80% → use `is_default: true` template
5. If no default → skip (log warning)

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Home page |
| GET | `/monitor` | Queue monitor page |
| GET | `/templates-page` | Template editor page |
| GET | `/results-page` | Results browser page |
| GET | `/profile-page` | User profile page |
| GET | `/interests` | List active interests |
| POST | `/add-interest` | Add interest (HTMX, returns HTML fragment) |
| POST | `/delete-interest/{text}` | Soft-delete interest (status → deleted) |
| GET | `/api/query-templates` | List all templates |
| POST | `/api/query-templates` | Create template |
| PUT | `/api/query-templates/{id}` | Update template |
| DELETE | `/api/query-templates/{id}` | Delete template |
| GET | `/api/queries` | List all queries |
| POST | `/api/queries` | Create query manually |
| POST | `/api/queries/{id}/pause` | Pause query |
| DELETE | `/api/queries/{id}` | Cancel/delete query |
| GET | `/api/results` | List results (with `$lookup` join) |
| PUT | `/api/results/{id}` | Edit result output |
| GET | `/api/profile` | Get user profile |
| PUT | `/api/profile` | Update user profile |
| POST | `/api/search-youtube/{id}` | Search YouTube for item title |
| GET | `/video/{id}` | Stream/proxy YouTube video |

---

## UI Design System

All pages share a consistent theme using **Tailwind CSS** (CDN).

### Colours
| Token | Value | Usage |
|-------|-------|-------|
| Primary | `blue-600` (#2563eb) | Buttons, active tabs, brand logo, links |
| Background | `gray-50 → white` gradient | Page background |
| Card | `white` + `border-gray-200` shadow-sm | Content panels |
| Active item | `blue-50` + `border-blue-500` | Selected template/tab |
| Success | `green-100` text `green-800` | Save confirmation |
| Error | `red-100` text `red-800` | Error notifications |
| Warning | `orange-600` | Data extraction issues |

### Typography
- Font stack: `-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Roboto', sans-serif`
- Page titles: `text-3xl font-bold text-gray-900`
- Section headers: `text-lg font-bold text-gray-900`
- Body: `text-gray-700`, small labels `text-gray-600 text-sm`

### Layout Patterns
- **Navigation**: Fixed top bar, `max-w-7xl mx-auto`, `z-50`
- **Page body**: `pt-20 pb-10` to clear fixed nav, `max-w-7xl mx-auto px-6`
- **Two-column editor** (Templates page): `lg:grid-cols-4` — 1 col sidebar + 3 col editor
- **Results layout** (Home page): `lg:grid-cols-4` — 1 col ranking panel + 3 col content
- **Tab content** (Home page): `lg:grid-cols-3` — 2 col AI content + 1 col related videos

### Component Classes (defined in `<style>`)
```css
.btn-primary     /* blue-600 filled button */
.btn-secondary   /* gray-200 outlined button */
.nav-link        /* gray-700 hover:blue-600 nav anchor */
.card            /* white rounded-lg shadow-sm border-gray-200 */
.form-input      /* w-full px-4 py-3 bg-gray-50 rounded-lg with focus ring */
.template-item   /* p-4 border rounded-lg cursor-pointer hover:bg-blue-50 */
.tab-button      /* borderless tab with blue active underline */
.item-content    /* prose-like line-height 1.8 for AI content */
.item-section    /* AI content section block with left blue border */
```

---

## Running Locally

```bash
# 1. Start infrastructure
docker compose up -d          # MongoDB on :27017, Redis on :6379

# 2. Install dependencies
pip install -r requirements.txt

# 3. Start API server
python app.py                 # FastAPI on http://localhost:8000

# 4. Start Celery worker (separate terminal)
celery -A src.tasks.celery_app worker --loglevel=info   # also runs the news refresh scheduler (src/news_refresh.py)
```

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- ALWAYS read graphify-out/GRAPH_REPORT.md before reading any source files, running grep/glob searches, or answering codebase questions. The graph is your primary map of the codebase.
- IF graphify-out/wiki/index.md EXISTS, navigate it instead of reading raw files
- For cross-module "how does X relate to Y" questions, prefer `graphify query "<question>"`, `graphify path "<A>" "<B>"`, or `graphify explain "<concept>"` over grep — these traverse the graph's EXTRACTED + INFERRED edges instead of scanning files
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
