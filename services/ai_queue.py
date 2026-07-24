"""DB에 작업을 보존하는 다중 프로세스 안전 AI 작업 큐."""

import os
import socket
import threading
import time
import uuid
from datetime import timedelta

import click
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError

from extensions import db
from models import AIJob, Report
from utils import get_now_kst


class AIJobQueue:
    def __init__(self, app, worker, start_workers=True):
        self.app = app
        self.worker = worker
        self.workers = max(1, int(os.getenv('AI_MAX_WORKERS', '1')))
        self.max_pending = max(self.workers, int(os.getenv('AI_MAX_PENDING_JOBS', '10')))
        self.retries = max(0, int(os.getenv('AI_JOB_RETRIES', '1')))
        self.poll_seconds = max(0.2, float(os.getenv('AI_QUEUE_POLL_SECONDS', '1')))
        self.stale_seconds = max(60, int(os.getenv('AI_JOB_STALE_SECONDS', '900')))
        self.heartbeat_seconds = max(5, min(60, self.stale_seconds // 3))
        self.worker_prefix = f'{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}'
        self.wakeup = threading.Event()
        autostart = os.getenv('AI_QUEUE_AUTOSTART', 'true').lower() in ('1', 'true', 'yes')
        if start_workers and autostart:
            for index in range(self.workers):
                thread = threading.Thread(
                    target=self._loop, args=(f'{self.worker_prefix}-{index}',),
                    name=f'ai-worker-{index}', daemon=True,
                )
                thread.start()

    def run_forever(self):
        """전용 작업자 프로세스에서 AI 큐를 포그라운드로 실행한다."""
        self._loop(f'{self.worker_prefix}-foreground')

    def submit(self, report_id, file_path, file_type):
        if not file_path:
            return False
        try:
            active = AIJob.query.filter(
                AIJob.report_id == report_id,
                AIJob.status.in_(('queued', 'running')),
            ).first()
            if active:
                return True
            pending = AIJob.query.filter(AIJob.status.in_(('queued', 'running'))).count()
            if pending >= self.max_pending:
                return False
            db.session.add(AIJob(
                report_id=report_id, file_path=file_path, file_type=file_type,
                status='queued', max_attempts=self.retries + 1,
                active_key=f'report:{report_id}',
            ))
            db.session.commit()
            self.wakeup.set()
            return True
        except IntegrityError:
            db.session.rollback()
            return True
        except Exception:
            db.session.rollback()
            self.app.logger.exception('AI 작업 저장 실패 (report=%s)', report_id)
            return False

    def _loop(self, worker_id):
        while True:
            job_id = None
            try:
                with self.app.app_context():
                    job_id = self._claim(worker_id)
                if job_id is None:
                    self.wakeup.wait(self.poll_seconds)
                    self.wakeup.clear()
                    continue
                self._execute(job_id)
            except (OperationalError, ProgrammingError):
                # 최초 마이그레이션/create_all 이전에는 테이블이 없을 수 있다.
                time.sleep(self.poll_seconds)
            except Exception:
                self.app.logger.exception('AI 큐 작업자 오류 (job=%s)', job_id)
                time.sleep(self.poll_seconds)

    def _claim(self, worker_id):
        now = get_now_kst()
        stale_before = now - timedelta(seconds=self.stale_seconds)
        AIJob.query.filter(
            AIJob.status == 'running',
            or_(AIJob.locked_at.is_(None), AIJob.locked_at < stale_before),
        ).update({
            AIJob.status: 'queued', AIJob.worker_id: None,
            AIJob.locked_at: None, AIJob.available_at: now,
        }, synchronize_session=False)
        db.session.commit()

        candidates = AIJob.query.filter(
            AIJob.status == 'queued', AIJob.available_at <= now,
        ).order_by(AIJob.created_at.asc()).limit(5).all()
        for candidate in candidates:
            claimed = AIJob.query.filter_by(id=candidate.id, status='queued').update({
                AIJob.status: 'running', AIJob.worker_id: worker_id,
                AIJob.locked_at: now, AIJob.attempts: AIJob.attempts + 1,
            }, synchronize_session=False)
            db.session.commit()
            if claimed == 1:
                return candidate.id
        return None

    def _execute(self, job_id):
        with self.app.app_context():
            job = db.session.get(AIJob, job_id)
            if not job or job.status != 'running':
                return
            report_id, file_path, file_type = job.report_id, job.file_path, job.file_type
        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat,
            args=(job_id, heartbeat_stop),
            name=f'ai-heartbeat-{job_id}',
            daemon=True,
        )
        heartbeat.start()
        try:
            self.worker(report_id, file_path, file_type)
        except Exception as exc:
            heartbeat_stop.set()
            heartbeat.join(timeout=1)
            self._finish_failure(job_id, exc)
            return
        heartbeat_stop.set()
        heartbeat.join(timeout=1)
        with self.app.app_context():
            job = db.session.get(AIJob, job_id)
            if job:
                job.status = 'completed'
                job.active_key = None
                job.locked_at = None
                job.worker_id = None
                job.last_error = None
                db.session.commit()

    def _heartbeat(self, job_id, stop_event):
        while not stop_event.wait(self.heartbeat_seconds):
            try:
                with self.app.app_context():
                    updated = AIJob.query.filter_by(id=job_id, status='running').update({
                        AIJob.locked_at: get_now_kst(),
                    }, synchronize_session=False)
                    db.session.commit()
                    if updated != 1:
                        return
            except Exception:
                with self.app.app_context():
                    db.session.rollback()
                self.app.logger.exception('AI 작업 heartbeat 갱신 실패 (job=%s)', job_id)

    def _finish_failure(self, job_id, error):
        self.app.logger.error(
            'AI 작업 실패 (job=%s)', job_id,
            exc_info=(type(error), error, error.__traceback__),
        )
        with self.app.app_context():
            job = db.session.get(AIJob, job_id)
            if not job:
                return
            job.last_error = str(error)[:500]
            job.locked_at = None
            job.worker_id = None
            if job.attempts < job.max_attempts:
                job.status = 'queued'
                job.available_at = get_now_kst() + timedelta(seconds=min(2 ** job.attempts, 30))
                self.wakeup.set()
            else:
                job.status = 'failed'
                job.active_key = None
            db.session.commit()


def init_ai_worker_cli(app, queue):
    @app.cli.command('ai-worker')
    def ai_worker_command():
        """AI 분석 큐 작업자를 포그라운드로 실행합니다."""
        if queue is None or not app.config.get('AI_WORKER_AVAILABLE'):
            raise click.ClickException(
                'AI 모델을 불러오지 못했습니다. APP_PROCESS_ROLE=worker 및 YOLO 설정을 확인하세요.'
            )
        click.echo('AI 작업자를 시작했습니다. 종료하려면 Ctrl+C를 누르세요.')
        queue.run_forever()
