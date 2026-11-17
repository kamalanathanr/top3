# Architecture Overview

## System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                         Frontend                            │
│  (HTML/JS - index.html, monitor.html, etc.)                │
└────────────────┬────────────────────────────────────────────┘
                 │ HTTP/API
                 ▼
┌─────────────────────────────────────────────────────────────┐
│                  FastAPI Web Server                         │
│                   (app_new.py)                              │
│                                                             │
│  Route Handlers:                                           │
│  ├─ Pages (serve HTML)                                    │
│  ├─ API (JSON responses)                                  │
│  └─ Video (stream URLs)                                   │
└────────────┬────────────────────────────────┬──────────────┘
             │                                │
             ▼                                ▼
    ┌───────────────────┐         ┌──────────────────┐
    │    MongoDB        │         │  Redis Queue    │
    │  Collections:     │         │  (Celery)       │
    │  ├─ interests     │         └──────────────────┘
    │  ├─ queries       │                │
    │  ├─ query_results │                ▼
    │  ├─ query_templts │         ┌──────────────────┐
    │  └─ user_profiles │         │ Celery Worker    │
    │                   │         │  (executor_new)  │
    └───────────────────┘         │                  │
                                  │ Task Logic:      │
                                  │ ├─ YouTube      │
                                  │ ├─ Claude       │
                                  │ └─ Web Search   │
                                  └──────────────────┘
```

## Module Dependencies

```
                    ┌─────────────────┐
                    │   app_new.py    │ (FastAPI entry)
                    └────────┬────────┘
                             │
        ┌────────────────────┼────────────────────┐
        │                    │                    │
        ▼                    ▼                    ▼
    ┌────────────────────────────────────────────────────┐
    │              src/routes/                           │
    │  ┌──────────┬──────────┬──────────┬──────────────┐ │
    │  │  pages   │interests │templates │ queries etc  │ │
    │  └──────────┴──────────┴──────────┴──────────────┘ │
    └─────────────┬──────────────────────────────────────┘
                  │
        ┌─────────┴──────────────────────────────┐
        │                                        │
        ▼                                        ▼
    ┌──────────────────┐              ┌──────────────────┐
    │ src/database.py  │              │ src/models.py    │
    │                  │              │                  │
    │ get_db()         │              │ Pydantic models  │
    │ connect_db()     │              │                  │
    └──────────────────┘              └──────────────────┘
          ▲                                     
          │                                     
    ┌─────┴──────────────────────────────┐
    │      Service Layer                 │
    │                                    │
    │  ┌──────────────────────────────┐ │
    │  │ src/interest_service.py      │ │
    │  │ create_queries_for_interest()│ │
    │  └──────────────┬───────────────┘ │
    │                 │                  │
    │  ┌──────────────┴────────────────┐ │
    │  ▼              ▼                 ▼ │
    │ profile_       template_         youtube_
    │ handler.py     matcher.py        search.py
    │                                    │
    │              ┌──────────────────────┘
    │              │
    │  ┌───────────┴────────────┐
    │  ▼                        ▼
    │ claude_executor    search_engine
    │                        
    └────────────────────────────────┘
            │
            │ (Registers tasks)
            ▼
    ┌──────────────────────────────┐
    │    executor_new.py           │ (Celery entry)
    │                              │
    │  Imports from src/tasks.py   │
    └──────────────────────────────┘
```

## Request Flow

### Adding an Interest

```
1. User submits interest form
   │
   └─► POST /add-interest
       │
       └─► routes/interests.py:add_interest()
           │
           ├─► youtube_search.search_youtube_videos()
           │
           ├─► database.get_db() → insert interest
           │
           └─► interest_service.create_queries_for_interest()
               │
               ├─► 1. Create YouTube query
               │    └─► tasks.execute_query.delay()
               │
               ├─► 2. Smart template matching
               │    └─► template_matcher.template_matches_interest()
               │
               ├─► 3. Create matching AI queries
               │    └─► tasks.execute_query.delay()
               │
               └─► 4. Fallback: Create custom query
                    └─► tasks.execute_query.delay()

2. Celery Worker processes tasks (executor_new.py)
   │
   └─► tasks.execute_query()
       │
       ├─ For YouTube: youtube_search.search_youtube_videos()
       │
       └─ For AI:
           ├─► Check for Ironman/events keywords
           │   └─► search_engine.search_web() [if needed]
           │
           ├─► claude_executor.run_claude_query()
           │   ├─► subprocess: claude -p "..."
           │   └─► Filter permission messages
           │
           └─► database.get_db() → insert results

3. Results appear on frontend
   │
   └─► GET /api/results
       │
       └─► routes/results.py:list_results()
           │
           └─► Returns formatted results
```

### Getting User Profile

```
User visits profile page
   │
   ├─► GET /profile-page
   │   └─► Serve templates/profile.html
   │
   └─► GET /api/profile
       │
       └─► routes/profile.py:get_profile()
           │
           └─► profile_handler.get_user_profile_context()
               │
               └─► database.get_db().user_profiles.find_one()
```

## Data Models

### Interest
```python
{
    "_id": ObjectId,
    "user_id": "Kamal",
    "text": "Ironman training",
    "added_at": ISO8601_timestamp,
    "videos": [
        {
            "id": "video_id",
            "title": "...",
            "thumbnail": "...",
            "duration": 600
        }
    ]
}
```

### Query
```python
{
    "_id": ObjectId,
    "user_id": "Kamal",
    "template_name": "Ironman Events",
    "template_id": ObjectId,
    "query_type": "ai" | "youtube",
    "user_interests": ["Ironman training"],
    "ai_query": "Full prompt text...",
    "status": "pending" | "running" | "done" | "paused" | "cancelled",
    "created_at": ISO8601_timestamp,
    "started_at": ISO8601_timestamp,
    "completed_at": ISO8601_timestamp,
    "error": "error message if failed"
}
```

### Query Result
```python
{
    "_id": ObjectId,
    "query_id": ObjectId,
    "user_id": "Kamal",
    "template_name": "Ironman Events",
    "output": {
        "text": "...",
        "videos": [...],
        "error": "..."
    },
    "time_taken_sec": 4.5,
    "completed_at": ISO8601_timestamp
}
```

### User Profile
```python
{
    "_id": ObjectId,
    "user_id": "Kamal",
    "qualification": "...",
    "family_details": "...",
    "hobbies": ["..."],
    "history": "...",
    "interests": ["..."],
    "strategies": "...",
    "updated_at": ISO8601_timestamp
}
```

## Deployment Architecture

```
┌──────────────────────────────────────────────────┐
│             Production Environment               │
│                                                  │
│  ┌────────────────────────────────────────────┐ │
│  │  Docker Compose                            │ │
│  │                                            │ │
│  │  Services:                                 │ │
│  │  ├─ MongoDB (port 27017)                  │ │
│  │  └─ Redis (port 6379)                     │ │
│  └────────────────────────────────────────────┘ │
│                     ▲                            │
│                     │                            │
│  ┌──────────────────┴──────────────────────┐   │
│  │         Host System                     │   │
│  │                                         │   │
│  │  ┌──────────────┐  ┌────────────────┐  │   │
│  │  │ FastAPI App  │  │ Celery Worker  │  │   │
│  │  │  (port 8000) │  │                │  │   │
│  │  └──────────────┘  └────────────────┘  │   │
│  │       app_new.py      executor_new.py   │   │
│  └─────────────────────────────────────────┘   │
│                                                  │
│  Client Access:                                  │
│  ├─ http://localhost:8000 (browser)            │
│  └─ http://localhost:8000/api/* (API calls)    │
└──────────────────────────────────────────────────┘
```

## Technology Stack

| Layer | Technology |
|-------|-----------|
| **Frontend** | HTML5, Tailwind CSS, JavaScript |
| **Web Server** | FastAPI (Python async) |
| **Task Queue** | Celery + Redis |
| **Database** | MongoDB |
| **AI Integration** | Claude CLI |
| **Video Search** | yt-dlp (YouTube) |
| **Web Search** | DuckDuckGo API |

## Performance Characteristics

- **API Response Time**: <100ms for list operations
- **YouTube Search**: 1-2 seconds
- **Claude Query**: 3-30 seconds (depends on query)
- **Web Search**: 1-2 seconds
- **Database Indexes**: Created on user_id, created_at for optimal query performance
- **Concurrent Queries**: Handled by Celery worker pool

## Security Considerations

- MongoDB authentication enabled (admin/admin)
- Redis running on localhost only
- Claude queries validated for permission messages
- No sensitive data in logs
- User_id hardcoded to "Kamal" (can be extended to multi-user)
