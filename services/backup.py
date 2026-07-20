"""DB의 일관된 백업 생성과 체크섬·SQLite 무결성 검증 CLI."""

import hashlib
import os
import shutil
import sqlite3
import stat
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import certifi
import click

from extensions import db


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def create_sqlite_backup(source_path, destination_path):
    source_path = Path(source_path).resolve()
    destination_path = Path(destination_path).resolve()
    if not source_path.is_file():
        raise RuntimeError('SQLite 원본 DB 파일을 찾을 수 없습니다.')
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(str(source_path))
    destination = sqlite3.connect(str(destination_path))
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()


def verify_backup(backup_path):
    backup_path = Path(backup_path).resolve()
    checksum_path = backup_path.with_suffix(backup_path.suffix + '.sha256')
    if not backup_path.is_file() or not checksum_path.is_file():
        raise RuntimeError('백업 파일 또는 체크섬 파일이 없습니다.')
    expected = checksum_path.read_text(encoding='ascii').strip().split()[0].lower()
    actual = _sha256(backup_path)
    if expected != actual:
        raise RuntimeError('백업 체크섬이 일치하지 않습니다.')
    if backup_path.suffix.lower() in {'.db', '.sqlite', '.sqlite3'}:
        connection = sqlite3.connect(f'file:{backup_path.as_posix()}?mode=ro', uri=True)
        try:
            if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('SQLite 백업 무결성 검사에 실패했습니다.')
            foreign_key_violations = connection.execute('PRAGMA foreign_key_check').fetchall()
            if foreign_key_violations:
                raise RuntimeError('SQLite 백업에 외래키 위반이 있습니다.')
        finally:
            connection.close()
    return actual


def create_database_backup(app, output_dir=None):
    output_root = Path(output_dir or os.getenv('BACKUP_DIRECTORY') or Path(app.root_path, 'backups')).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    engine_url = db.engine.url
    dialect = engine_url.get_backend_name()

    if dialect == 'sqlite':
        if not engine_url.database or engine_url.database == ':memory:':
            raise RuntimeError('메모리 SQLite DB는 파일 백업을 만들 수 없습니다.')
        backup_path = output_root / f'crack-{stamp}.sqlite3'
        temporary_path = backup_path.with_suffix('.sqlite3.partial')
        try:
            create_sqlite_backup(engine_url.database, temporary_path)
            temporary_path.replace(backup_path)
        finally:
            temporary_path.unlink(missing_ok=True)
    elif dialect in {'mysql', 'mariadb'}:
        executable = os.getenv('MYSQLDUMP_PATH') or shutil.which('mysqldump')
        if not executable:
            raise RuntimeError('mysqldump를 찾을 수 없습니다. MYSQLDUMP_PATH를 설정하세요.')
        backup_path = output_root / f'crack-{stamp}.sql'
        command = [
            executable, '--single-transaction', '--quick', '--routines', '--events',
            '--triggers', '--no-tablespaces', '--default-character-set=utf8mb4',
            '--host', engine_url.host or '127.0.0.1', '--port', str(engine_url.port or 3306),
            '--user', engine_url.username or '',
        ]
        if engine_url.host not in ('127.0.0.1', 'localhost'):
            command.extend(['--ssl-mode=VERIFY_CA', f'--ssl-ca={certifi.where()}'])
        command.append(engine_url.database or '')
        process_env = os.environ.copy()
        if engine_url.password:
            process_env['MYSQL_PWD'] = engine_url.password
        temporary_path = backup_path.with_suffix('.sql.partial')
        try:
            with open(temporary_path, 'wb') as output:
                subprocess.run(
                    command, stdout=output, stderr=subprocess.PIPE, check=True,
                    env=process_env, timeout=1800,
                )
            temporary_path.replace(backup_path)
        finally:
            temporary_path.unlink(missing_ok=True)
    else:
        raise RuntimeError(f'지원하지 않는 DB 백업 방식입니다: {dialect}')

    os.chmod(backup_path, stat.S_IRUSR | stat.S_IWUSR)
    checksum = _sha256(backup_path)
    checksum_path = backup_path.with_suffix(backup_path.suffix + '.sha256')
    checksum_path.write_text(f'{checksum}  {backup_path.name}\n', encoding='ascii')
    os.chmod(checksum_path, stat.S_IRUSR | stat.S_IWUSR)
    verify_backup(backup_path)
    return backup_path


def prune_old_backups(app, output_dir=None):
    output_root = Path(output_dir or os.getenv('BACKUP_DIRECTORY') or Path(app.root_path, 'backups')).resolve()
    if not output_root.is_dir():
        return 0
    cutoff = time.time() - max(1, int(os.getenv('BACKUP_RETENTION_DAYS', '30'))) * 86400
    removed = 0
    for candidate in output_root.glob('crack-*'):
        if candidate.is_file() and candidate.stat().st_mtime < cutoff:
            candidate.unlink()
            removed += 1
    return removed


def run_backup_loop(app):
    interval = max(3600, int(os.getenv('BACKUP_INTERVAL_SECONDS', '86400')))
    while True:
        try:
            with app.app_context():
                backup_path = create_database_backup(app)
                removed = prune_old_backups(app)
                app.logger.info('정기 DB 백업 완료: %s (정리 파일=%s)', backup_path, removed)
        except Exception:
            app.logger.exception('정기 DB 백업 실패')
        threading.Event().wait(interval)


def init_backup_cli(app):
    @app.cli.command('backup-create')
    @click.option('--output-dir', type=click.Path(file_okay=False, path_type=Path))
    def backup_create_command(output_dir):
        """일관된 DB 백업과 SHA-256 체크섬을 생성합니다."""
        try:
            backup_path = create_database_backup(app, output_dir)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f'백업 생성 및 검증 완료: {backup_path}')

    @app.cli.command('backup-verify')
    @click.argument('backup_path', type=click.Path(exists=True, dir_okay=False, path_type=Path))
    def backup_verify_command(backup_path):
        """백업 체크섬과 가능한 경우 DB 무결성을 검증합니다."""
        try:
            checksum = verify_backup(backup_path)
        except (OSError, RuntimeError, sqlite3.DatabaseError) as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f'백업 검증 완료: SHA-256 {checksum}')

    @app.cli.command('backup-loop')
    def backup_loop_command():
        """DB 백업을 설정된 주기로 실행하는 포그라운드 작업자입니다."""
        click.echo('DB 백업 스케줄러를 시작했습니다. 종료하려면 Ctrl+C를 누르세요.')
        run_backup_loop(app)
