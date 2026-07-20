import os
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

os.environ.setdefault('FLASK_SECRET_KEY', 'test-only-secret-key')
os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')
os.environ.setdefault('AI_QUEUE_AUTOSTART', 'false')
os.environ.setdefault('RETENTION_SCHEDULER_ENABLED', 'false')
os.environ.setdefault('APP_PROCESS_ROLE', 'web')

from PIL import Image
import cv2
import numpy as np
from werkzeug.security import check_password_hash, generate_password_hash

import app as app_module
from extensions import db
from models import AIJob, Member, PasswordResetToken, PointLog, Report
from services.privacy_filter import blur_image_in_place
from services.retention import apply_retention_policy
from services.media_security import MediaValidationError, sanitize_image_to_jpeg, validate_saved_media
from services.report_workflow import transition_report
from services.security import can_access_report
from services.backup import create_sqlite_backup, verify_backup


class P1RegressionTests(unittest.TestCase):
    def setUp(self):
        app_module.app.config.update(TESTING=True)
        with app_module.app.app_context():
            db.drop_all()
            db.create_all()

    def test_password_reset_table_is_created(self):
        with app_module.app.app_context():
            self.assertEqual(PasswordResetToken.query.count(), 0)

    def test_create_admin_cli_hashes_password_and_assigns_role(self):
        runner = app_module.app.test_cli_runner()
        result = runner.invoke(
            args=['create-admin'],
            input='admin_test\nadmin@example.com\n관리자\nstrong-password-123\nstrong-password-123\n',
        )
        self.assertEqual(result.exit_code, 0, result.output)
        with app_module.app.app_context():
            member = Member.query.filter_by(username='admin_test').one()
            self.assertTrue(member.is_admin)
            self.assertEqual(member.role, 'admin')
            self.assertTrue(check_password_hash(member.password_hash, 'strong-password-123'))

    def test_report_points_are_idempotent_per_report(self):
        with app_module.app.app_context():
            member = Member(username='tester', password_hash='x', email='tester@example.com', points=0)
            db.session.add(member)
            db.session.flush()
            report = Report(user_id=member.id, status='관리자 확인중')
            db.session.add(report)
            db.session.flush()

            transition_report(report, '처리완료')
            db.session.commit()
            transition_report(report, '처리 완료')
            db.session.commit()
            self.assertEqual(member.points, 20)
            self.assertEqual(PointLog.query.filter(PointLog.amount == 20).count(), 1)

            transition_report(report, '반려', '테스트')
            db.session.commit()
            transition_report(report, '반려', '테스트')
            db.session.commit()
            self.assertEqual(member.points, 10)
            self.assertEqual(PointLog.query.filter(PointLog.amount < 0).count(), 1)

    def test_jpeg_is_sanitized_without_in_place_corruption(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / 'sample.jpg'
            Image.new('RGB', (20, 20), 'red').save(source, 'JPEG')
            result, _ = sanitize_image_to_jpeg(str(source))
            self.assertEqual(Path(result), source)
            self.assertEqual(validate_saved_media(result, 'jpg'), 'image')

    def test_fake_image_signature_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / 'fake.jpg'
            source.write_bytes(b'not an image')
            with self.assertRaises(MediaValidationError):
                validate_saved_media(str(source), 'jpg')

    def test_sqlite_backup_is_consistent_and_checksum_verified(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / 'source.sqlite3'
            backup = Path(temp_dir) / 'backup.sqlite3'
            connection = sqlite3.connect(source)
            connection.execute('CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT NOT NULL)')
            connection.execute('INSERT INTO sample (value) VALUES (?)', ('백업 테스트',))
            connection.commit()
            connection.close()
            create_sqlite_backup(source, backup)
            import hashlib
            checksum = hashlib.sha256(backup.read_bytes()).hexdigest()
            backup.with_suffix('.sqlite3.sha256').write_text(
                f'{checksum}  {backup.name}\n', encoding='ascii'
            )
            self.assertEqual(verify_backup(backup), checksum)
            restored = sqlite3.connect(backup)
            self.assertEqual(restored.execute('SELECT value FROM sample').fetchone()[0], '백업 테스트')
            restored.close()

    def test_privacy_filter_keeps_image_decodable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / 'privacy.jpg'
            Image.new('RGB', (100, 100), 'white').save(source, 'JPEG')
            blur_image_in_place(str(source))
            self.assertEqual(validate_saved_media(str(source), 'jpg'), 'image')

    def test_video_report_upload_is_validated_masked_and_persisted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / 'sample.mp4'
            writer = cv2.VideoWriter(
                str(source), cv2.VideoWriter_fourcc(*'mp4v'), 5.0, (160, 120)
            )
            self.assertTrue(writer.isOpened())
            for index in range(5):
                frame = np.full((120, 160, 3), 40 + index * 20, dtype=np.uint8)
                writer.write(frame)
            writer.release()
            payload = source.read_bytes()

        with app_module.app.app_context():
            member = Member(
                username='video_user', password_hash='x', email='video@example.com'
            )
            db.session.add(member)
            db.session.commit()
            member_id = member.id

        client = app_module.app.test_client()
        with client.session_transaction() as flask_session:
            flask_session['user_id'] = member_id
            flask_session['_csrf_token'] = 'csrf-video'
        with patch.dict(os.environ, {'VIDEO_GPS_OCR_ENABLED': 'false'}), patch.dict(
            app_module.app.config, {'AI_AVAILABLE': False}
        ):
            response = client.post(
                '/api/report',
                data={
                    'title': '영상 통합 테스트',
                    'content': '자동 검증',
                    'csrf_token': 'csrf-video',
                    'file': (io.BytesIO(payload), 'sample.mp4'),
                },
                content_type='multipart/form-data',
            )
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        with app_module.app.app_context():
            report = Report.query.filter_by(user_id=member_id).one()
            stored = Path(app_module.app.root_path, report.file_path.lstrip('/'))
            self.addCleanup(lambda: stored.unlink(missing_ok=True))
            self.assertEqual(validate_saved_media(str(stored), 'mp4'), 'video')
            self.assertEqual(report.status, '관리자 확인중')

    def test_csrf_protects_mutating_api(self):
        client = app_module.app.test_client()
        response = client.post('/api/check_id', json={'username': 'tester'})
        self.assertEqual(response.status_code, 400)

    def test_routes_do_not_have_duplicate_method_rules(self):
        seen = set()
        duplicates = []
        for rule in app_module.app.url_map.iter_rules():
            for method in rule.methods - {'HEAD', 'OPTIONS'}:
                key = (str(rule), method)
                if key in seen:
                    duplicates.append(key)
                seen.add(key)
        self.assertEqual(duplicates, [])

    def test_manager_media_access_is_limited_to_assigned_region(self):
        with app_module.app.app_context():
            manager = Member(
                username='manager', password_hash='x', email='manager@example.com',
                role='manager', manager_region='서울특별시 강남구',
            )
            owner = Member(username='owner', password_hash='x', email='owner@example.com')
            db.session.add_all([manager, owner])
            db.session.flush()
            in_region = Report(user_id=owner.id, region_name='서울특별시 강남구')
            out_region = Report(user_id=owner.id, region_name='서울특별시 종로구')
            db.session.add_all([in_region, out_region])
            db.session.flush()
            self.assertTrue(can_access_report(manager, in_region))
            self.assertFalse(can_access_report(manager, out_region))

    def test_deleted_report_media_is_hidden_from_owner_but_available_to_admin(self):
        upload = Path(app_module.app.root_path, 'uploads', 'images', 'deleted-access-test.jpg')
        upload.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (10, 10), 'black').save(upload, 'JPEG')
        self.addCleanup(lambda: upload.unlink(missing_ok=True))
        with app_module.app.app_context():
            owner = Member(username='deleted_owner', password_hash='x', email='deleted-owner@example.com')
            admin = Member(
                username='deleted_admin', password_hash='x', email='deleted-admin@example.com',
                role='admin', is_admin=True,
            )
            db.session.add_all([owner, admin])
            db.session.flush()
            report = Report(
                user_id=owner.id, status='삭제', region_name='서울특별시 강남구',
                file_path='/uploads/images/deleted-access-test.jpg', file_type='image',
            )
            db.session.add(report)
            db.session.commit()
            owner_id, admin_id, report_id = owner.id, admin.id, report.id

        client = app_module.app.test_client()
        with client.session_transaction() as flask_session:
            flask_session['user_id'] = owner_id
        self.assertEqual(client.get('/uploads/images/deleted-access-test.jpg').status_code, 404)
        self.assertEqual(client.get(f'/alert/view/{report_id}').status_code, 404)
        self.assertEqual(client.get(f'/api/report/{report_id}/detections').status_code, 404)

        with client.session_transaction() as flask_session:
            flask_session.clear()
            flask_session['user_id'] = admin_id
        response = client.get('/uploads/images/deleted-access-test.jpg')
        self.assertEqual(response.status_code, 200)
        response.close()

    def test_security_headers_and_health_endpoint(self):
        response = app_module.app.test_client().get('/healthz')
        self.assertEqual(response.status_code, 200)
        self.assertIn("default-src 'self'", response.headers['Content-Security-Policy'])
        self.assertEqual(response.headers['X-Frame-Options'], 'DENY')

    def test_rate_limit_is_persisted_in_database(self):
        client = app_module.app.test_client()
        with client.session_transaction() as flask_session:
            flask_session['_csrf_token'] = 'csrf-rate'
        headers = {'X-CSRF-Token': 'csrf-rate'}
        responses = [client.post('/api/check_id', json={'username': 'available'}, headers=headers) for _ in range(21)]
        self.assertEqual(responses[-1].status_code, 429)

    def test_ai_job_is_persisted(self):
        with app_module.app.app_context():
            member = Member(username='queue_user', password_hash='x', email='queue@example.com')
            db.session.add(member)
            db.session.flush()
            report = Report(user_id=member.id, file_path='/uploads/images/a.jpg', file_type='image')
            db.session.add(report)
            db.session.commit()
            self.assertTrue(app_module.app.submit_ai_analysis(report.id, report.file_path, report.file_type))
            self.assertEqual(AIJob.query.filter_by(report_id=report.id, status='queued').count(), 1)

    def test_retention_removes_deleted_media_and_location(self):
        upload = Path(app_module.app.root_path, 'uploads', 'images', 'retention-test.jpg')
        recent_upload = Path(app_module.app.root_path, 'uploads', 'images', 'retention-recent-test.jpg')
        upload.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (10, 10), 'black').save(upload, 'JPEG')
        Image.new('RGB', (10, 10), 'black').save(recent_upload, 'JPEG')
        self.addCleanup(lambda: recent_upload.unlink(missing_ok=True))
        with app_module.app.app_context(), patch.dict(os.environ, {
            'DELETED_MEDIA_RETENTION_DAYS': '1', 'LOCATION_RETENTION_DAYS': '1',
        }):
            member = Member(username='retention_user', password_hash='x', email='retention@example.com')
            db.session.add(member)
            db.session.flush()
            report = Report(
                user_id=member.id, status='삭제', file_path='/uploads/images/retention-test.jpg',
                latitude=37.0, longitude=127.0, address='테스트',
                created_at=app_module.datetime.now() - app_module.timedelta(days=2),
                status_changed_at=app_module.datetime.now() - app_module.timedelta(days=2),
            )
            db.session.add(report)
            db.session.add(Report(
                user_id=member.id, status='삭제',
                file_path='/uploads/images/retention-recent-test.jpg',
                created_at=app_module.datetime.now() - app_module.timedelta(days=30),
                status_changed_at=app_module.datetime.now(),
            ))
            db.session.commit()
            apply_retention_policy(app_module.app)
            self.assertFalse(upload.exists())
            self.assertTrue(recent_upload.exists())
            self.assertIsNone(report.file_path)
            self.assertIsNone(report.latitude)

    def test_password_reset_token_is_emailed_and_single_use(self):
        sent_messages = []

        class FakeSMTP:
            def __init__(self, *_args, **_kwargs):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False
            def starttls(self):
                pass
            def login(self, *_args):
                pass
            def send_message(self, message):
                sent_messages.append(message)

        with app_module.app.app_context():
            db.session.add(Member(
                username='reset_user', password_hash=generate_password_hash('old-password'),
                email='reset@example.com', active=True,
            ))
            db.session.commit()

        client = app_module.app.test_client()
        with client.session_transaction() as flask_session:
            flask_session['_csrf_token'] = 'csrf-test'
        headers = {'X-CSRF-Token': 'csrf-test'}
        smtp_env = {'SMTP_HOST': 'smtp.test', 'SMTP_FROM': 'no-reply@test', 'PUBLIC_BASE_URL': 'https://test.local'}
        with patch.dict(os.environ, smtp_env), patch('services.auth_service.smtplib.SMTP', FakeSMTP):
            response = client.post('/api/find-pw', json={
                'username': 'reset_user', 'email': 'reset@example.com',
            }, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(sent_messages), 1)
        reset_url = sent_messages[0].get_content().splitlines()[2]
        token = parse_qs(urlparse(reset_url).query)['reset_token'][0]

        response = client.post('/api/reset-pw', json={
            'username': 'reset_user', 'password': 'new-password-123', 'token': token,
        }, headers=headers)
        self.assertEqual(response.status_code, 200)
        response = client.post('/api/reset-pw', json={
            'username': 'reset_user', 'password': 'another-password-123', 'token': token,
        }, headers=headers)
        self.assertEqual(response.status_code, 400)
        with app_module.app.app_context():
            member = Member.query.filter_by(username='reset_user').first()
            self.assertTrue(check_password_hash(member.password_hash, 'new-password-123'))


if __name__ == '__main__':
    unittest.main()
