from extensions import db
from utils import get_now_kst
from sqlalchemy import event, inspect

class Member(db.Model):
    __tablename__ = 'members'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    nickname = db.Column(db.String(80), nullable=True)
    is_admin = db.Column(db.Boolean, default=False)
    points = db.Column(db.Integer, default=0) # 크래커 포인트
    created_at = db.Column(db.DateTime, default=get_now_kst)
    email = db.Column(db.String(120), unique=True, nullable=False)
    region_city = db.Column(db.String(50), nullable=True)
    region_district = db.Column(db.String(50), nullable=True)
    role = db.Column(db.String(20), nullable=True)  # 'admin' / 'manager' / 'user'
    manager_region = db.Column(db.String(50), nullable=True)
    active = db.Column(db.Boolean, default=True, server_default='1')

class Report(db.Model):
    __tablename__ = 'report'
    __table_args__ = (
        db.Index('ix_report_status_created_at', 'status', 'created_at'),
        db.Index('ix_report_user_created_at', 'user_id', 'created_at'),
    )
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('members.id', ondelete='CASCADE'), nullable=True, index=True)
    title = db.Column(db.String(255), nullable=True)
    content = db.Column(db.Text, nullable=True)
    latitude = db.Column(db.Float, nullable=True)
    longitude = db.Column(db.Float, nullable=True)
    address = db.Column(db.String(255), nullable=True)
    file_path = db.Column(db.String(512), nullable=True)
    file_type = db.Column(db.String(50), nullable=True)
    thumbnail_path = db.Column(db.String(512), nullable=True) # AI가 생성한 썸네일 경로
    status = db.Column(db.String(20), default='관리자 확인중', index=True)
    reject_reason = db.Column(db.String(500), nullable=True)
    region_name = db.Column(db.String(255), nullable=True)
    last_checked_at = db.Column(db.DateTime, nullable=True)
    status_changed_at = db.Column(db.DateTime, nullable=True, default=get_now_kst)
    created_at = db.Column(db.DateTime, default=get_now_kst, index=True)
    author = db.relationship('Member', backref=db.backref('reports', lazy=True))

class AiResult(db.Model):
    __tablename__ = 'ai_results'
    id = db.Column(db.Integer, primary_key=True)
    report_id = db.Column(db.Integer, db.ForeignKey('report.id', ondelete='CASCADE'), nullable=False, index=True)
    is_damaged = db.Column(db.Boolean, default=False)
    confidence = db.Column(db.Float, nullable=True)
    damage_type = db.Column(db.String(50), nullable=True)
    created_at = db.Column(db.DateTime, default=get_now_kst)
    report = db.relationship('Report', backref=db.backref('ai_result', uselist=False, cascade='all, delete-orphan'))

class PointLog(db.Model):
    __tablename__ = 'point_logs'
    __table_args__ = (
        db.Index('ix_point_logs_user_created_at', 'user_id', 'created_at'),
    )
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('members.id', ondelete='CASCADE'), nullable=False, index=True)
    amount = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.String(255), nullable=False)
    idempotency_key = db.Column(db.String(100), nullable=True, unique=True, index=True)
    created_at = db.Column(db.DateTime, default=get_now_kst, index=True)
    member = db.relationship('Member', backref=db.backref('point_logs', lazy=True, order_by='PointLog.created_at.desc()'))

class UserSettings(db.Model):
    __tablename__ = 'user_settings'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('members.id', ondelete='CASCADE'), nullable=False, unique=True)
    notification_enabled = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=get_now_kst)
    member = db.relationship('Member', backref=db.backref('settings', uselist=False))

class Notice(db.Model):
    __tablename__ = 'notices'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False)
    content = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(50), default='시스템')
    author_id = db.Column(db.Integer, db.ForeignKey('members.id', ondelete='SET NULL'), nullable=True)
    created_at = db.Column(db.DateTime, default=get_now_kst)
    author = db.relationship('Member', backref=db.backref('notices', lazy=True))

class VideoDetection(db.Model):
    """동영상 프레임별 AI 검출 결과"""
    __tablename__ = 'video_detections'
    id = db.Column(db.Integer, primary_key=True)
    report_id = db.Column(db.Integer, db.ForeignKey('report.id', ondelete='CASCADE'), nullable=False, index=True)
    frame_time = db.Column(db.Float, nullable=False)        # 검출 시점 (초)
    class_name = db.Column(db.String(100), nullable=False)   # 검출 클래스명
    confidence = db.Column(db.Float, nullable=False)          # 신뢰도 (0~1)
    x1 = db.Column(db.Float, nullable=False)                  # 바운딩박스 좌상단 x (비율 0~1)
    y1 = db.Column(db.Float, nullable=False)                  # 바운딩박스 좌상단 y
    x2 = db.Column(db.Float, nullable=False)                  # 바운딩박스 우하단 x
    y2 = db.Column(db.Float, nullable=False)                  # 바운딩박스 우하단 y
    created_at = db.Column(db.DateTime, default=get_now_kst)
    report = db.relationship('Report', backref=db.backref('video_detections', lazy=True, cascade='all, delete-orphan'))

class CrackTalk(db.Model):
    __tablename__ = 'crack_talk'
    id = db.Column(db.Integer, primary_key=True)
    author_id = db.Column(db.Integer, db.ForeignKey('members.id', ondelete='CASCADE'), nullable=False)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=get_now_kst, index=True)
    is_blinded = db.Column(db.Boolean, default=False, nullable=False)
    # Relationship
    author = db.relationship('Member', backref=db.backref('crack_talks', lazy=True, order_by='CrackTalk.created_at.asc()'))


class PasswordResetToken(db.Model):
    __tablename__ = 'password_reset_tokens'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('members.id', ondelete='CASCADE'), nullable=False, index=True)
    token_hash = db.Column(db.String(64), unique=True, nullable=False, index=True)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    used_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=get_now_kst, nullable=False)
    member = db.relationship('Member', backref=db.backref('password_reset_tokens', lazy=True, cascade='all, delete-orphan'))


class AIJob(db.Model):
    __tablename__ = 'ai_jobs'
    __table_args__ = (
        db.Index('ix_ai_jobs_status_available_at', 'status', 'available_at'),
    )
    id = db.Column(db.Integer, primary_key=True)
    report_id = db.Column(db.Integer, db.ForeignKey('report.id', ondelete='CASCADE'), nullable=False, index=True)
    active_key = db.Column(db.String(64), unique=True, nullable=True, index=True)
    file_path = db.Column(db.String(512), nullable=False)
    file_type = db.Column(db.String(20), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='queued', index=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    max_attempts = db.Column(db.Integer, nullable=False, default=2)
    available_at = db.Column(db.DateTime, nullable=False, default=get_now_kst, index=True)
    locked_at = db.Column(db.DateTime, nullable=True, index=True)
    worker_id = db.Column(db.String(80), nullable=True)
    last_error = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=get_now_kst)
    updated_at = db.Column(db.DateTime, nullable=False, default=get_now_kst, onupdate=get_now_kst)
    report = db.relationship('Report', backref=db.backref('ai_jobs', lazy=True, cascade='all, delete-orphan'))


class RateLimitBucket(db.Model):
    __tablename__ = 'rate_limit_buckets'
    bucket_key = db.Column(db.String(64), primary_key=True)
    count = db.Column(db.Integer, nullable=False, default=1)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)


@event.listens_for(Report, 'before_update')
def record_report_status_change(_mapper, _connection, target):
    if inspect(target).attrs.status.history.has_changes():
        target.status_changed_at = get_now_kst()
