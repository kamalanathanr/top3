"""
Celery configuration module
"""

from celery import Celery

# Create Celery app
app = Celery(
    'executor',
    broker='redis://localhost:6379/0',
    backend='redis://localhost:6379/1'
)

# Configure Celery
app.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
    task_track_started=True,
    task_time_limit=30 * 60,       # 30 minutes hard limit
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=1000,
    task_acks_late=True,           # task stays in Redis until it finishes — never silently lost
    task_reject_on_worker_lost=True,  # if worker dies mid-task, task goes back to queue
)

def get_celery_app():
    """Get Celery app instance"""
    return app
