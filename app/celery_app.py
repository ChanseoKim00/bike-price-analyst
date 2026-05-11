"""
Celery initialization — single module-level instance with `include` to guarantee task registration.

Notes:
  - The `celery` instance is created at import time and `include=["app.tasks"]` pins the task path immediately.
  - The Flask app is wired in separately via `init_celery_for_flask(app)`. The Task base class is swapped
    so tasks automatically enter a Flask app_context when executed.
  - Web and worker processes both import the same celery instance, eliminating the "task missing because
    default app binding never happened" issue you can hit with the `@shared_task + set_default` pattern.

Broker / result backend = REDIS_URL injected by the Railway Redis plugin.
"""
import os

from celery import Celery

REDIS_URL = os.environ.get("REDIS_URL")

celery = Celery(
    "bike_price_analyst",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["app.tasks"],
)

celery.conf.update(
    task_track_started=True,
    task_time_limit=300,          # hard timeout (SIGKILL)
    task_soft_time_limit=270,     # soft timeout
    result_expires=3600,          # results expire after 1 hour
    broker_connection_retry_on_startup=True,
    worker_prefetch_multiplier=1, # analyze tasks are long-running → prefetch only one at a time
    task_acks_late=False,         # so revoke(terminate=True) doesn't cause re-execution
    worker_redirect_stdouts_level="INFO",  # downgrade print() redirect level from WARNING to INFO — keeps normal logs from showing up red
)


def init_celery_for_flask(flask_app):
    """Swap the Task base class so Celery tasks run inside a Flask app_context, and register on extensions."""
    base_task = celery.Task

    class FlaskTask(base_task):
        abstract = True

        def __call__(self, *args, **kwargs):
            with flask_app.app_context():
                return self.run(*args, **kwargs)

    celery.Task = FlaskTask
    flask_app.extensions["celery"] = celery
    return celery
