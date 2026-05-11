"""
Celery worker entry point.

Run:
  celery -A celery_worker:celery worker --loglevel=info --concurrency=1

Build the Flask app once and pull the celery instance off it.
At task execution time, FlaskTask opens an app_context() automatically.
"""
from dotenv import load_dotenv
load_dotenv()

from app import create_app

flask_app = create_app()
celery = flask_app.extensions["celery"]
