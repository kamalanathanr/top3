"""
Page routing - serves HTML templates
"""

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter()

@router.get("/")
async def home():
    return FileResponse("templates/index.html", media_type="text/html")

@router.get("/templates-page")
async def templates_page():
    return FileResponse("templates/query_templates.html", media_type="text/html")

@router.get("/results-page")
async def results_page():
    return FileResponse("templates/results.html", media_type="text/html")

@router.get("/profile-page")
async def profile_page():
    return FileResponse("templates/profile.html", media_type="text/html")

@router.get("/news")
async def news_page():
    return FileResponse("templates/news.html", media_type="text/html")

@router.get("/investment-overview")
async def investment_overview_page():
    return FileResponse("templates/investment_overview.html", media_type="text/html")

@router.get("/trips")
async def trips_page():
    return FileResponse("templates/trips.html", media_type="text/html")

@router.get("/experiences")
async def experiences_page():
    return FileResponse("templates/experiences.html", media_type="text/html")

@router.get("/planning")
async def planning_page():
    return FileResponse("templates/planning.html", media_type="text/html")

@router.get("/cashflow")
async def cashflow_page():
    return FileResponse("templates/cashflow.html", media_type="text/html")

@router.get("/bonds")
async def bonds_page():
    return FileResponse("templates/bonds.html", media_type="text/html")

@router.get("/bonds-progress")
async def bonds_progress_page():
    return FileResponse("templates/bonds_progress.html", media_type="text/html")
