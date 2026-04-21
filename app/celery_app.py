"""
Celery 초기화 — 모듈 레벨 단일 인스턴스 + include로 task를 확실히 등록.

포인트:
  - `celery` 인스턴스는 모듈 import 시점에 생성돼 바로 `include=["app.tasks"]`로 task 경로를 고정.
  - Flask app은 별도로 `init_celery_for_flask(app)`에서 연결. task 실행 시 Flask app_context를
    자동 진입하도록 Task 베이스 클래스를 교체한다.
  - 웹/워커 어디서 import하든 동일한 celery 인스턴스를 보게 되므로, `@shared_task + set_default`
    패턴에서 발생할 수 있는 "default app에 binding이 안 돼 task 누락" 문제를 제거.

브로커/결과 저장소는 Railway Redis 플러그인이 주입하는 REDIS_URL.
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
    task_time_limit=300,          # 하드 타임아웃 (SIGKILL)
    task_soft_time_limit=270,     # 소프트 타임아웃
    result_expires=3600,          # 결과 1시간 후 만료
    broker_connection_retry_on_startup=True,
    worker_prefetch_multiplier=1, # 분석은 장시간 → 1개씩만 prefetch
    task_acks_late=False,         # revoke(terminate=True)가 재실행되지 않도록
)


def init_celery_for_flask(flask_app):
    """Celery task가 Flask app_context 안에서 실행되도록 Task 베이스 교체 + extensions 등록."""
    base_task = celery.Task

    class FlaskTask(base_task):
        abstract = True

        def __call__(self, *args, **kwargs):
            with flask_app.app_context():
                return self.run(*args, **kwargs)

    celery.Task = FlaskTask
    flask_app.extensions["celery"] = celery
    return celery
