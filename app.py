"""
Main FastAPI application entry point
Imports all modular components and sets up the application
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from src.database import connect_database, disconnect_database, initialize_database

# Import route modules
from src.routes import pages, interests, templates, queries, results, profile, video, investment_overview, bonds, whatsapp_webhook, trip_details, countries, flight_plans, news

def create_app() -> FastAPI:
    """Factory function to create and configure FastAPI app"""

    app = FastAPI(
        title="Top3 Discovery Platform",
        description="AI-powered discovery platform with MongoDB, Celery, and Claude integration",
        version="1.0.0"
    )

    # Add CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # /api/news can return several MB of JSON when keeping a week of news
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    # Startup and shutdown events
    @app.on_event("startup")
    async def startup_event():
        """Initialize database on startup"""
        await connect_database()
        await initialize_database()
        print("Application startup complete")

    @app.on_event("shutdown")
    async def shutdown_event():
        """Close database connection on shutdown"""
        await disconnect_database()
        print("Application shutdown complete")

    # Include route routers
    app.include_router(pages.router)
    app.include_router(interests.router)
    app.include_router(templates.router)
    app.include_router(queries.router)
    app.include_router(results.router)
    app.include_router(profile.router)
    app.include_router(video.router)
    app.include_router(investment_overview.router)
    app.include_router(bonds.router)
    app.include_router(whatsapp_webhook.router)
    app.include_router(trip_details.router)
    app.include_router(countries.router)
    app.include_router(flight_plans.router)
    app.include_router(news.router)

    # Shared static assets (e.g. static/nav.js — single source of truth for the nav bar)
    app.mount("/static", StaticFiles(directory="static"), name="static")

    # Make browsers revalidate static files (cheap 304 via ETag) so edits such
    # as new menu links show up without a hard refresh
    @app.middleware("http")
    async def no_cache_static(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    return app

# Create application instance
app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
