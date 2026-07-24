"""신고 미디어·위치정보·운영 테이블 보관 정책."""

import os
import threading
import time
from datetime import timedelta
from pathlib import Path

import click
from sqlalchemy import func, or_

from extensions import db
from models import AIJob, PasswordResetToken, RateLimitBucket, Report
from utils import get_now_kst


def _days(name, default):
    return max(1, int(os.getenv(name, str(default))))


def _delete_upload(app, stored_path):
    if not stored_path:
        return False
    relative = str(stored_path).replace('\\', '/').lstrip('/')
    if not relative.startswith('uploads/'):
        return False
    root = Path(app.root_path, 'uploads').resolve()
    target = Path(app.root_path, relative).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return False
    if target.is_file():
        target.unlink()
        return True
    return False


def apply_retention_policy(app):
    now = get_now_kst()
    media_cutoffs = {
        '삭제': now - timedelta(days=_days('DELETED_MEDIA_RETENTION_DAYS', 7)),
    }
    stats = {
        'media_files': 0, 'orphan_files': 0, 'reports_redacted': 0,
        'tokens': 0, 'jobs': 0, 'buckets': 0,
    }

    for status, cutoff in media_cutoffs.items():
        retention_started_at = func.coalesce(
            Report.status_changed_at, Report.last_checked_at, Report.created_at
        )
        reports = Report.query.filter(
            Report.status == status, retention_started_at < cutoff
        ).all()
        for report in reports:
            stats['media_files'] += int(_delete_upload(app, report.file_path))
            if report.thumbnail_path != report.file_path:
                stats['media_files'] += int(_delete_upload(app, report.thumbnail_path))
            report.file_path = None
            report.thumbnail_path = None
            report.file_type = None

    location_cutoff = now - timedelta(days=_days('LOCATION_RETENTION_DAYS', 365))
    location_reports = Report.query.filter(
        Report.created_at < location_cutoff,
        or_(Report.latitude.isnot(None), Report.longitude.isnot(None), Report.address.isnot(None)),
    ).all()
    for report in location_reports:
        report.latitude = None
        report.longitude = None
        report.address = None
        report.region_name = None
        stats['reports_redacted'] += 1

    stats['tokens'] = PasswordResetToken.query.filter(
        PasswordResetToken.expires_at < now - timedelta(days=1)
    ).delete(synchronize_session=False)
    stats['jobs'] = AIJob.query.filter(
        AIJob.status.in_(('completed', 'failed')),
        AIJob.updated_at < now - timedelta(days=_days('AI_JOB_RETENTION_DAYS', 30)),
    ).delete(synchronize_session=False)
    stats['buckets'] = RateLimitBucket.query.filter(
        RateLimitBucket.expires_at < now
    ).delete(synchronize_session=False)

    referenced_paths = set()
    for file_path, thumbnail_path in db.session.query(Report.file_path, Report.thumbnail_path):
        for stored_path in (file_path, thumbnail_path):
            if stored_path:
                referenced_paths.add('/' + str(stored_path).replace('\\', '/').lstrip('/'))
    orphan_cutoff = time.time() - max(
        1, int(os.getenv('ORPHAN_UPLOAD_RETENTION_HOURS', '24'))
    ) * 3600
    upload_root = Path(app.root_path, 'uploads').resolve()
    for media_dir in ('images', 'videos'):
        directory = upload_root / media_dir
        if not directory.is_dir():
            continue
        for candidate in directory.iterdir():
            if not candidate.is_file() or candidate.stat().st_mtime >= orphan_cutoff:
                continue
            stored_path = f'/uploads/{media_dir}/{candidate.name}'
            if stored_path not in referenced_paths:
                candidate.unlink()
                stats['orphan_files'] += 1
    db.session.commit()
    app.logger.info('보관 정책 실행 완료: %s', stats)
    return stats


def run_retention_loop(app, initial_delay=0):
    if initial_delay:
        threading.Event().wait(initial_delay)
    interval = max(3600, int(os.getenv('RETENTION_INTERVAL_SECONDS', '86400')))
    while True:
        try:
            with app.app_context():
                apply_retention_policy(app)
        except Exception:
            app.logger.exception('보관 정책 실행 실패')
        threading.Event().wait(interval)


def init_retention(app, start_scheduler=True):
    @app.cli.command('retention-run')
    def retention_run_command():
        """보관 정책을 즉시 한 번 실행합니다."""
        click.echo(apply_retention_policy(app))

    @app.cli.command('retention-loop')
    def retention_loop_command():
        """보관 정책 스케줄러를 포그라운드로 실행합니다."""
        click.echo('보관 정책 스케줄러를 시작했습니다. 종료하려면 Ctrl+C를 누르세요.')
        run_retention_loop(app)

    if not start_scheduler or os.getenv('RETENTION_SCHEDULER_ENABLED', 'true').lower() not in ('1', 'true', 'yes'):
        return

    threading.Thread(
        target=run_retention_loop, args=(app, 30),
        name='retention-policy', daemon=True,
    ).start()
