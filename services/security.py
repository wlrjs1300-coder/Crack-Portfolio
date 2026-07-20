"""공통 인증·권한·CSRF 보안 도우미."""

import os
import secrets
import time
import threading
import hashlib
from datetime import timedelta
from collections import defaultdict, deque
from functools import wraps

from flask import abort, current_app, jsonify, redirect, request, session, url_for
from sqlalchemy.exc import OperationalError, ProgrammingError

from extensions import db
from models import Member, RateLimitBucket
from utils import get_now_kst


VALID_ROLES = {"admin", "manager", "user"}
_rate_lock = threading.Lock()
_rate_buckets = defaultdict(deque)


def canonical_role(member: Member) -> str:
    """레거시 is_admin과 role을 하나의 권한 값으로 정규화한다."""
    if member.is_admin:
        return "admin"
    role = member.role if member.role in VALID_ROLES else None
    if role:
        return role
    return "user"


def sync_authenticated_session():
    """매 요청마다 DB 상태를 기준으로 세션 권한과 계정 활성 상태를 검증한다."""
    user_id = session.get("user_id")
    if not user_id:
        return None

    member = db.session.get(Member, user_id)
    if member is None or not member.active:
        session.clear()
        if request.path.startswith("/api/") or request.is_json:
            return jsonify({"success": False, "message": "로그인이 필요하거나 사용할 수 없는 계정입니다."}), 401
        return redirect(url_for("auth.login"))

    role = canonical_role(member)
    session["user_role"] = role
    session["role"] = role
    session["is_admin"] = role == "admin"
    session["user_name"] = member.nickname or member.username
    return None


def csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def validate_csrf():
    if request.method in {"GET", "HEAD", "OPTIONS", "TRACE"}:
        return None

    expected = session.get("_csrf_token")
    supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
    if not expected or not supplied or not secrets.compare_digest(str(expected), str(supplied)):
        if request.path.startswith("/api/") or request.is_json:
            return jsonify({"success": False, "message": "요청 보안 토큰이 유효하지 않습니다."}), 400
        abort(400, description="요청 보안 토큰이 유효하지 않습니다.")
    return None


def require_roles(*allowed_roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("user_id"):
                return jsonify({"success": False, "message": "로그인이 필요합니다."}), 401
            if session.get("user_role") not in allowed_roles:
                return jsonify({"success": False, "message": "권한이 없습니다."}), 403
            return view(*args, **kwargs)
        return wrapped
    return decorator


def can_access_report(member, report):
    """소유자·관리자·담당 지역 매니저만 민감 신고 자료에 접근한다."""
    if not member or not report:
        return False
    role = canonical_role(member)
    if role == 'admin' or str(report.user_id) == str(member.id):
        return True
    if role != 'manager' or not member.manager_region or not report.region_name:
        return False
    from services.region_service import parse_region_hierarchy
    manager_parts = parse_region_hierarchy(member.manager_region)
    report_parts = parse_region_hierarchy(report.region_name)
    if not manager_parts or len(report_parts) < len(manager_parts):
        return False
    return report_parts[:len(manager_parts)] == manager_parts


def rate_limit(limit: int, window_seconds: int):
    """DB 고정 윈도우 제한. DB 미초기화 상태에서만 메모리 방식으로 폴백한다."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if request.method in {'GET', 'HEAD', 'OPTIONS'}:
                return view(*args, **kwargs)
            # 프록시 신뢰 설정(ProxyFix) 없이 전달 헤더를 믿으면 IP를 쉽게 위조할 수 있다.
            identity = session.get('user_id') or request.remote_addr or 'unknown'
            identity = str(identity)
            endpoint = request.endpoint or request.path
            raw_key = f'{endpoint}|{identity}|{window_seconds}'
            now_epoch = int(time.time())
            window_epoch = now_epoch - (now_epoch % window_seconds)
            bucket_key = hashlib.sha256(f'{raw_key}|{window_epoch}'.encode()).hexdigest()
            remaining_seconds = window_epoch + window_seconds - now_epoch
            expires_at = get_now_kst() + timedelta(seconds=remaining_seconds)
            try:
                dialect = db.engine.dialect.name
                values = {'bucket_key': bucket_key, 'count': 1, 'expires_at': expires_at}
                if dialect == 'mysql':
                    from sqlalchemy.dialects.mysql import insert
                    statement = insert(RateLimitBucket).values(**values)
                    statement = statement.on_duplicate_key_update(count=RateLimitBucket.count + 1)
                elif dialect == 'sqlite':
                    from sqlalchemy.dialects.sqlite import insert
                    statement = insert(RateLimitBucket).values(**values)
                    statement = statement.on_conflict_do_update(
                        index_elements=['bucket_key'], set_={'count': RateLimitBucket.count + 1}
                    )
                elif dialect == 'postgresql':
                    from sqlalchemy.dialects.postgresql import insert
                    statement = insert(RateLimitBucket).values(**values)
                    statement = statement.on_conflict_do_update(
                        index_elements=['bucket_key'], set_={'count': RateLimitBucket.count + 1}
                    )
                else:
                    raise NotImplementedError(dialect)
                db.session.execute(statement)
                db.session.commit()
                count = db.session.get(RateLimitBucket, bucket_key).count
                if secrets.randbelow(100) == 0:
                    RateLimitBucket.query.filter(RateLimitBucket.expires_at < get_now_kst()).delete(
                        synchronize_session=False
                    )
                    db.session.commit()
                if count > limit:
                    retry_after = max(1, window_epoch + window_seconds - time.time())
                    response = jsonify({'success': False, 'message': '요청이 너무 많습니다. 잠시 후 다시 시도해주세요.'})
                    response.status_code = 429
                    response.headers['Retry-After'] = str(int(retry_after))
                    return response
                return view(*args, **kwargs)
            except (OperationalError, ProgrammingError, NotImplementedError):
                db.session.rollback()
                if os.getenv('APP_ENV', 'development').lower() == 'production':
                    current_app.logger.exception('DB 요청 제한을 적용할 수 없어 요청을 거부합니다.')
                    return jsonify({
                        'success': False,
                        'message': '요청 제한 서비스를 일시적으로 사용할 수 없습니다.',
                    }), 503
                current_app.logger.warning('DB 요청 제한을 사용할 수 없어 메모리 방식으로 폴백합니다.')

            key = (endpoint, identity)
            now = time.monotonic()
            cutoff = now - window_seconds
            with _rate_lock:
                bucket = _rate_buckets[key]
                while bucket and bucket[0] < cutoff:
                    bucket.popleft()
                if len(bucket) >= limit:
                    retry_after = max(1, int(window_seconds - (now - bucket[0])))
                    response = jsonify({'success': False, 'message': '요청이 너무 많습니다. 잠시 후 다시 시도해주세요.'})
                    response.status_code = 429
                    response.headers['Retry-After'] = str(retry_after)
                    return response
                bucket.append(now)
            return view(*args, **kwargs)
        return wrapped
    return decorator
