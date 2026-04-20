"""
Celery 워커 엔트리포인트.

실행:
  celery -A celery_worker:celery worker --loglevel=info --concurrency=1

Flask 앱을 한 번 생성해 celery 인스턴스를 꺼내온다.
task 실행 시 FlaskTask가 자동으로 app_context()를 열어준다.
"""
from dotenv import load_dotenv
load_dotenv()

from app import create_app

flask_app = create_app()
celery = flask_app.extensions["celery"]
