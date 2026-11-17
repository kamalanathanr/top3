# Modular Architecture Guide

The codebase has been refactored into clean, organized modules for better maintainability and understanding.

## Directory Structure

```
top3/
├── src/
│   ├── __init__.py
│   ├── database.py              # MongoDB connection & initialization
│   ├── utils.py                 # Utility functions (serialization, etc.)
│   ├── models.py                # Pydantic models for validation
│   ├── youtube_search.py        # YouTube search functionality
│   ├── template_matcher.py      # Template matching logic
│   ├── profile_handler.py       # User profile operations
│   ├── interest_service.py      # Interest management & query creation
│   ├── celery_config.py         # Celery configuration
│   ├── claude_executor.py       # Claude CLI execution
│   ├── search_engine.py         # Web search (DuckDuckGo)
│   ├── tasks.py                 # Celery task definitions
│   │
│   └── routes/
│       ├── __init__.py
│       ├── pages.py             # HTML page routes
│       ├── interests.py         # Interest API routes
│       ├── templates.py         # Template API routes
│       ├── queries.py           # Query API routes
│       ├── results.py           # Results API routes
│       ├── profile.py           # Profile API routes
│       └── video.py             # Video streaming routes
│
├── templates/                   # HTML templates
│   ├── index.html
│   ├── monitor.html
│   ├── query_templates.html
│   ├── results.html
│   └── profile.html
│
├── app_new.py                   # Main FastAPI app (use this!)
├── executor_new.py              # Celery worker entry (use this!)
├── app.py                       # [OLD] Keep for reference
├── executor.py                  # [OLD] Keep for reference
├── requirements.txt
├── docker-compose.yml
└── MODULAR_STRUCTURE.md         # This file
```

## Module Breakdown

### Core Modules

#### `src/database.py`
**Responsibility:** MongoDB connection and initialization
- `connect_database()` - Initialize MongoDB connection
- `disconnect_database()` - Close MongoDB connection
- `initialize_database()` - Create indexes and seed templates
- `get_db()` - Get database instance

**Usage:**
```python
from src.database import connect_database, get_db

await connect_database()
db = get_db()
```

#### `src/utils.py`
**Responsibility:** Common utility functions
- `serialize_objectid()` - Convert MongoDB ObjectId to string (recursive)

**Usage:**
```python
from src.utils import serialize_objectid

data = serialize_objectid(mongo_document)
```

#### `src/models.py`
**Responsibility:** Pydantic validation models
- `InterestRequest` - Add interest payload
- `QueryRequest` - Create query payload
- `TemplateUpdate` - Update template payload
- `ResultUpdate` - Update result payload
- `UserProfile` - User profile payload

**Usage:**
```python
from src.models import InterestRequest

@app.post("/interests")
async def add(request: InterestRequest):
    # Auto-validated by Pydantic
```

### Service Modules

#### `src/youtube_search.py`
**Responsibility:** YouTube video search and streaming
- `search_youtube_videos()` - Search YouTube using yt-dlp
- `get_video_stream()` - Get video stream URL

**Usage:**
```python
from src.youtube_search import search_youtube_videos, get_video_stream

videos = search_youtube_videos("ironman training", max_results=5)
stream = await get_video_stream(video_id)
```

#### `src/template_matcher.py`
**Responsibility:** Match interests to relevant templates
- `template_matches_interest()` - Smart template matching

**Matching Rules:**
- **Domestic Travel:** Travel keywords + India-specific keywords
- **International Travel:** Travel keywords + International keywords
- **Ironman Events:** Sports/event keywords

**Usage:**
```python
from src.template_matcher import template_matches_interest

if template_matches_interest("International Travel", "Bali vacation"):
    # Create International Travel query
```

#### `src/profile_handler.py`
**Responsibility:** User profile management
- `get_user_profile_context()` - Format profile for queries

**Usage:**
```python
from src.profile_handler import get_user_profile_context

context = await get_user_profile_context()
full_prompt = f"{original_prompt}\n\n{context}"
```

#### `src/interest_service.py`
**Responsibility:** Interest management and auto-query creation
- `create_queries_for_interest()` - Auto-create YouTube + AI queries

**Process:**
1. Creates YouTube query
2. Matches interest to templates (smart matching)
3. Creates AI queries for matching templates
4. Fallback: Creates custom query if no templates match

**Usage:**
```python
from src.interest_service import create_queries_for_interest

await create_queries_for_interest("Mauritius family vacation")
```

#### `src/search_engine.py`
**Responsibility:** Web search functionality
- `search_web()` - Query DuckDuckGo API for current info

**Usage:**
```python
from src.search_engine import search_web

results = search_web("upcoming Ironman events 2026")
```

#### `src/claude_executor.py`
**Responsibility:** Claude CLI execution
- `run_claude_query()` - Execute Claude and filter responses

**Features:**
- Aggressive permission message filtering
- Web search authorization handling
- Error handling and timeouts

**Usage:**
```python
from src.claude_executor import run_claude_query

result = run_claude_query(prompt)
if result['success']:
    output = result['output']
```

### Task Management

#### `src/celery_config.py`
**Responsibility:** Celery configuration
- `get_celery_app()` - Get configured Celery instance

#### `src/tasks.py`
**Responsibility:** Celery task definitions
- `execute_query()` - Main query execution task
- `_execute_query_async()` - Async query execution logic

**Task Flow:**
1. Fetch query from MongoDB
2. Check if paused/cancelled
3. Route to YouTube or Claude
4. Store results
5. Update query status

### API Routes

All routes are modularized by function:

#### `src/routes/pages.py` - Page Routes
- `GET /` - Home page
- `GET /monitor` - Monitor page
- `GET /templates-page` - Templates page
- `GET /results-page` - Results page
- `GET /profile-page` - Profile page

#### `src/routes/interests.py` - Interest Management
- `POST /add-interest` - Add new interest
- `GET /interests` - List all interests
- `POST /delete-interest/{text}` - Delete interest

#### `src/routes/templates.py` - Template Management
- `GET /api/query-templates` - List templates
- `PUT /api/query-templates/{id}` - Update template

#### `src/routes/queries.py` - Query Management
- `POST /api/queries` - Create query
- `GET /api/queries` - List queries
- `POST /api/queries/{id}/pause` - Pause query
- `DELETE /api/queries/{id}` - Cancel query

#### `src/routes/results.py` - Results Management
- `GET /api/results` - List results
- `PUT /api/results/{id}` - Update result

#### `src/routes/profile.py` - Profile Management
- `GET /api/profile` - Get user profile
- `POST /api/profile` - Update user profile

#### `src/routes/video.py` - Video Streaming
- `GET /video/{id}` - Get video stream URL

## How to Use

### Starting the Application

**FastAPI Server:**
```bash
# Use the new modular app
python app_new.py

# Or with uvicorn
uvicorn app_new:app --reload --host 0.0.0.0 --port 8000
```

**Celery Worker:**
```bash
# Use the new modular executor
python -m celery -A executor_new worker --loglevel=info

# Or
celery -A executor_new worker --loglevel=info
```

### Adding New Features

**Example: Adding a new route**

1. Create module: `src/routes/my_feature.py`
```python
from fastapi import APIRouter
from ..database import get_db

router = APIRouter(prefix="/api/my-feature")

@router.get("")
async def list_items():
    db = get_db()
    items = await db.my_collection.find().to_list(None)
    return {"items": items}
```

2. Import in `app_new.py`:
```python
from src.routes import my_feature

app.include_router(my_feature.router)
```

**Example: Adding a new service**

1. Create module: `src/my_service.py`
```python
async def my_function():
    from .database import get_db
    db = get_db()
    # Your logic here
```

2. Import where needed:
```python
from src.my_service import my_function

await my_function()
```

## Key Benefits

✅ **Clear Separation of Concerns** - Each module has a single responsibility
✅ **Easy to Test** - Modular structure allows unit testing per module
✅ **Scalable** - Easy to add new routes and services
✅ **Maintainable** - Changes are localized to specific modules
✅ **Reusable** - Import functions across different routes
✅ **Professional** - Industry-standard structure

## Migration from Old Code

The old `app.py` and `executor.py` contain all the logic. The new modular structure extracts this into organized modules while maintaining the same functionality.

**No changes needed in:**
- Templates (HTML files)
- Docker setup
- Requirements

**Just use:**
- `app_new.py` instead of `app.py`
- `executor_new.py` instead of `executor.py`

## Troubleshooting

**Import errors:**
- Make sure you're in the project root directory
- Use relative imports within the src package: `from ..database import get_db`
- Use absolute imports from outside: `from src.database import get_db`

**Circular imports:**
- The interest_service imports from tasks, which imports from database
- This is resolved by importing at function level where needed

**Database not found:**
- Make sure MongoDB is running: `docker compose up -d`
- Check connection string in `src/database.py`

