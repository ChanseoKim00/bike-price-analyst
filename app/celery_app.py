"""
Celery 초기화 — Flask 앱 컨텍스트를 task에서 자동으로 진입하도록 FlaskTask 주입.

브로커/결과 저장소는 Railway Redis 플러그인이 제공하는 REDIS_URL을 사용.
이 인스턴스는 웹 프로세스(enqueue용)와 워커 프로세스(실행용) 양쪽에서 공유된다.
"""
import os

from celery import Celery, Task


def celery_init_app(app):
    class FlaskTask(Task):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)

    celery_app = Celery(app.name, task_cls=FlaskTask)

    redis_url = os.environ.get("REDIS_URL")
    if redis_url:
        celery_app.conf.update(
            broker_url=redis_url,
            result_backend=redis_url,
            task_track_started=True,
            task_time_limit=300,          # 하드 타임아웃 (SIGKILL)
            task_soft_time_limit=270,     # 소프트 타임아웃
            result_expires=3600,          # 결과 1시간 후 만료
            broker_connection_retry_on_startup=True,
            worker_prefetch_multiplier=1, # 분석은 장시간 → 1개씩만 prefetch
            task_acks_late=False,         # revoke(terminate=True)가 재실행되지 않도록
        )

    celery_app.set_default()
    app.extensions["celery"] = celery_app
    return celery_app
