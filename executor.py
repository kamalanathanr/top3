"""
Celery Worker Entry Point
Imports Celery app and tasks for worker execution
"""

from src.celery_config import get_celery_app
import src.tasks  # Import module to register tasks

# Get the Celery app instance
app = get_celery_app()

# Make sure tasks are registered
from src.tasks import execute_query

if __name__ == '__main__':
    app.start()
