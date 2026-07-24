import os
import sys
import certifi
import threading
import math
import secrets as secrets_module
import hashlib
from datetime import datetime, timedelta
from decimal import Decimal
from flask import Flask, render_template, session, redirect, url_for, send_from_directory, make_response, request, \
    jsonify
from flask import g
from dotenv import load_dotenv
import cv2

# 내부 모듈 임포트
from extensions import db, socketio, migrate
from models import Report, AiResult, Member, VideoDetection, AIJob
from utils import profanity_filter_available, reverse_geocode

# 서비스 Blueprint 임포트
from services.auth_service import auth_bp
from services.alert_service import alert_bp
from services.report_service import report_bp
from services.status_service import status_bp
from services.my_service import my_bp
from services.admin_service import admin_bp
from werkzeug.utils import secure_filename
from sqlalchemy.engine import URL
from services.security import (
    can_access_report,
    canonical_role,
    csrf_token,
    sync_authenticated_session,
    validate_csrf,
    require_roles,
)

# .env 파일 로드 (secrets 폴더 확인)
base_dir = os.path.dirname(__file__)
env_path = os.path.join(base_dir, 'secrets', '.env')

env_file_missing = not os.path.exists(env_path)
if env_file_missing:
    pass
else:
    load_dotenv(env_path)

is_flask_cli = os.getenv('FLASK_RUN_FROM_CLI', '').lower() == 'true'
is_ai_worker_cli = 'ai-worker' in sys.argv[1:]
runtime_services_enabled = not is_flask_cli or is_ai_worker_cli

app = Flask(__name__)
from services.logging_config import configure_logging
configure_logging(app)

app_env = os.getenv('APP_ENV', 'development').lower()
is_production = app_env == 'production'
process_role = os.getenv('APP_PROCESS_ROLE', 'all').strip().lower()
if process_role not in {'all', 'web', 'worker', 'scheduler'}:
    raise RuntimeError('APP_PROCESS_ROLE은 all, web, worker, scheduler 중 하나여야 합니다.')
if is_production and process_role == 'all':
    raise RuntimeError(
        '운영 환경에서는 APP_PROCESS_ROLE을 web/worker/scheduler로 분리해야 합니다.'
    )
app.config['PROCESS_ROLE'] = process_role
flask_secret_key = os.getenv('FLASK_SECRET_KEY')
if not flask_secret_key:
    if is_production:
        raise RuntimeError('운영 환경에는 FLASK_SECRET_KEY가 반드시 필요합니다.')
    flask_secret_key = secrets_module.token_urlsafe(48)
    app.logger.warning('개발용 임시 FLASK_SECRET_KEY를 생성했습니다. 재시작 시 세션이 초기화됩니다.')
if is_production and len(flask_secret_key) < 32:
    raise RuntimeError('운영 환경의 FLASK_SECRET_KEY는 32자 이상이어야 합니다.')
if env_file_missing:
    app.logger.warning('secrets/.env가 없어 환경변수와 개발 기본값만 사용합니다.')
app.secret_key = flask_secret_key
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.getenv(
        'SESSION_COOKIE_SECURE', 'true' if is_production else 'false'
    ).lower() in ('1', 'true', 'yes'),
)

# [용어 정의] 상단바와 하단바를 제외한 실질적인 본문 영역을 '메인 콘텐츠 영역' 또는 '메인 영역'으로 정의합니다.
MAIN_CONTENT_AREA = "메인 콘텐츠 영역 (Main Content Area)"

# DB 설정 (TiDB Cloud 연결 지원)
db_user = os.getenv('DB_USER')
db_password = os.getenv('DB_PASSWORD')
db_host = os.getenv('DB_HOST')
db_port = os.getenv('DB_PORT', '3306')
db_name = os.getenv('DB_NAME')
database_url = os.getenv('DATABASE_URL')

if database_url:
    app.config['SQLALCHEMY_DATABASE_URI'] = database_url
elif not all([db_user, db_password, db_host, db_name]):
    if is_production:
        raise RuntimeError('운영 환경에는 DATABASE_URL 또는 모든 DB_* 설정이 필요합니다.')
    app.logger.warning('DB 환경변수가 없어 개발용 SQLite DB를 사용합니다.')
    # 기본값 설정을 통해 최소한의 구성은 유지하거나 에러 처리 필요
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///temp_debug.db'
else:
    is_local_db = db_host in ('127.0.0.1', 'localhost')
    query = {} if is_local_db else {'ssl_ca': certifi.where()}
    app.config['SQLALCHEMY_DATABASE_URI'] = URL.create(
        'mysql+pymysql', username=db_user, password=db_password,
        host=db_host, port=int(db_port), database=db_name, query=query
    )

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,
    'pool_recycle': 3600,
}
if not database_url and db_user and db_password and db_host and db_name:
    app.config['SQLALCHEMY_ENGINE_OPTIONS']['connect_args'] = {
        'init_command': "SET time_zone = '+09:00'"
    }

# 업로드 설정 (최대 100MB)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024
app.config['MAX_FORM_MEMORY_SIZE'] = 512 * 1024
app.config['MAX_FORM_PARTS'] = 50
app.config['TEMPLATES_AUTO_RELOAD'] = not is_production
app.config['APP_BUILD_VERSION'] = os.getenv('APP_BUILD_VERSION') or datetime.utcnow().strftime('%Y%m%d%H%M%S')
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0 if not is_production else 31536000
UPLOAD_BASE_DIR = os.path.join(base_dir, 'uploads')
UPLOAD_IMAGE_DIR = os.path.join(UPLOAD_BASE_DIR, 'images')
UPLOAD_VIDEO_DIR = os.path.join(UPLOAD_BASE_DIR, 'videos')

# 디렉토리 생성
for d in [UPLOAD_IMAGE_DIR, UPLOAD_VIDEO_DIR]:
    if not os.path.exists(d):
        os.makedirs(d)

db.init_app(app)
socket_origins = [item.strip() for item in os.getenv('SOCKETIO_CORS_ORIGINS', '').split(',') if item.strip()]
socketio.init_app(app, cors_allowed_origins=socket_origins or None)
migrate.init_app(app, db)

# 관리 CLI와 웹 전용 프로세스에서는 모델을 역직렬화하지 않는다.
model = None
ai_model_configured = False
should_check_model = runtime_services_enabled and process_role in {'all', 'web', 'worker'}
should_load_model = should_check_model and process_role in {'all', 'worker'}
if should_check_model:
    try:
        model_path = os.getenv('YOLO_MODEL_PATH') or os.path.join(base_dir, 'static', 'best.pt')
        if not os.path.isfile(model_path):
            raise FileNotFoundError(
                f'YOLO 모델 파일이 없습니다: {model_path}. YOLO_MODEL_PATH를 설정하세요.'
            )
        expected_model_hash = os.getenv('YOLO_MODEL_SHA256', '').lower()
        if is_production and not expected_model_hash:
            raise RuntimeError('운영 환경에는 YOLO_MODEL_SHA256가 필요합니다.')
        if expected_model_hash:
            digest = hashlib.sha256()
            with open(model_path, 'rb') as model_file:
                for chunk in iter(lambda: model_file.read(1024 * 1024), b''):
                    digest.update(chunk)
            if not secrets_module.compare_digest(digest.hexdigest(), expected_model_hash):
                raise RuntimeError('YOLO 모델 파일 체크섬이 일치하지 않습니다.')
        ai_model_configured = True
        if should_load_model:
            from ultralytics import YOLO
            model = YOLO(model_path)
    except Exception as e:
        app.logger.warning('AI 모델 로드 실패: %s', e)
model_lock = threading.Lock()
app.config['AI_AVAILABLE'] = ai_model_configured
app.config['AI_WORKER_AVAILABLE'] = model is not None
app.config['PROFANITY_AVAILABLE'] = profanity_filter_available()

# Blueprint 등록
app.register_blueprint(auth_bp)
app.register_blueprint(alert_bp)
app.register_blueprint(report_bp)
app.register_blueprint(status_bp)
app.register_blueprint(my_bp)
app.register_blueprint(admin_bp)


@app.before_request
def enforce_request_security():
    if request.endpoint == 'static':
        return None
    upload_endpoints = {
        'report.upload_file', 'report.submit_report', 'status.update_report',
    }
    if request.endpoint not in upload_endpoints:
        request.max_content_length = 1024 * 1024
    auth_result = sync_authenticated_session()
    if auth_result is not None:
        return auth_result
    return validate_csrf()


@app.before_request
def inject_csp_nonce():
    g.csp_nonce = secrets_module.token_urlsafe(12)


@app.context_processor
def expose_csp_nonce():
    return {'csp_nonce': getattr(g, 'csp_nonce', '')}


@app.context_processor
def expose_build_version():
    return {
        'build_version': app.config.get('APP_BUILD_VERSION', 'dev')
    }


@app.after_request
def apply_security_headers(response):
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    response.headers.setdefault('Permissions-Policy', 'camera=(self), geolocation=(self), microphone=()')
    if not is_production:
        response.headers.setdefault('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
        response.headers.setdefault('Pragma', 'no-cache')
        response.headers.setdefault('Expires', '0')
    nonce = getattr(g, 'csp_nonce', '')
    script_src = [
        "'self'",
        f"'nonce-{nonce}'",
        'https://cdnjs.cloudflare.com',
        'https://cdn.jsdelivr.net',
        'https://cdn.socket.io',
        't1.daumcdn.net',
        'https://dapi.kakao.com',
    ]
    # 개발/로컬에서만 임시 완화: 기존 마크업의 inline 핸들러가
    # 아직 남아 있는 페이지에서 UI가 멈추는 현상을 방지하기 위해 사용합니다.
    is_local_host = request.host.split(':', 1)[0] in ('localhost', '127.0.0.1')
    if not is_production or is_local_host:
        script_src.append("'unsafe-inline'")
    response.headers.setdefault('Content-Security-Policy', (
        "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; form-action 'self'; "
        f"script-src {' '.join(script_src)}; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
        "font-src 'self' data: https://fonts.gstatic.com https://cdn.jsdelivr.net; "
        "img-src 'self' data: blob: https: http://t1.daumcdn.net http://mts.daumcdn.net; media-src 'self' blob:; "
        "connect-src 'self' ws: wss: dapi.kakao.com *.kakao.com https://cdn.socket.io; "
        "frame-src 'self' https://postcode.map.daum.net https://postcode.map.kakao.com http://postcode.map.kakao.com;"
    ))
    if request.is_secure:
        response.headers.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
    return response


@app.errorhandler(413)
def request_too_large(_error):
    return jsonify({'success': False, 'message': '업로드 파일이 허용된 최대 크기(100MB)를 초과했습니다.'}), 413


@app.errorhandler(500)
def internal_server_error(error):
    app.logger.error('Unhandled server error: %s', error)
    if request.path.startswith('/api/') or request.is_json:
        return jsonify({'success': False, 'message': '서버 처리 중 오류가 발생했습니다.'}), 500
    return render_template('500.html'), 500

# --- 공통 기능 및 API 설정 --- #

# 카카오 JS 키 로드 및 주입
kakao_js_key = os.getenv('KAKAO_JS_KEY', '')
try:
    key_file = os.path.join(base_dir, 'secrets', 'kakao_js_key.txt')
    if not kakao_js_key and os.path.isfile(key_file):
        with open(key_file, 'r', encoding='utf-8') as f:
            kakao_js_key = f.read().strip()
    app.config['KAKAO_JS_KEY'] = kakao_js_key
except Exception as e:
    app.logger.warning('Kakao JavaScript 키 로드 실패: %s', e)


@app.route('/healthz')
def healthz():
    return jsonify({'status': 'ok'})


@app.route('/readyz')
def readyz():
    from sqlalchemy import text as sa_text
    checks = {
        'database': False, 'ai_model': app.config['AI_AVAILABLE'], 'kakao_map': bool(kakao_js_key),
        'profanity_filter': app.config['PROFANITY_AVAILABLE'],
        'smtp': bool(os.getenv('SMTP_HOST') and os.getenv('SMTP_FROM')),
    }
    try:
        db.session.execute(sa_text('SELECT 1'))
        checks['database'] = True
    except Exception:
        db.session.rollback()
    require_ai = os.getenv('REQUIRE_AI_MODEL', 'false').lower() in ('1', 'true', 'yes')
    require_kakao = os.getenv('REQUIRE_KAKAO_KEYS', 'false').lower() in ('1', 'true', 'yes')
    require_profanity = os.getenv('REQUIRE_PROFANITY_FILTER', 'false').lower() in ('1', 'true', 'yes')
    require_smtp = os.getenv('REQUIRE_SMTP', 'false').lower() in ('1', 'true', 'yes')
    ready = (
        checks['database'] and (checks['ai_model'] or not require_ai)
        and (checks['kakao_map'] or not require_kakao)
        and (checks['profanity_filter'] or not require_profanity)
        and (checks['smtp'] or not require_smtp)
    )
    return jsonify({'status': 'ready' if ready else 'not_ready', 'checks': checks}), 200 if ready else 503


@app.route('/ops/metrics')
@require_roles('admin')
def ops_metrics():
    from models import RateLimitBucket
    from sqlalchemy import func, or_

    now = datetime.now()
    stale_seconds = max(60, int(os.getenv('AI_JOB_STALE_SECONDS', '900')))
    stale_cutoff = now - timedelta(seconds=stale_seconds)
    last_24h = now - timedelta(hours=24)

    job_status_rows = dict(
        db.session.query(AIJob.status, func.count(AIJob.id))
        .group_by(AIJob.status)
        .all()
    )
    report_status_rows = dict(
        db.session.query(Report.status, func.count(Report.id))
        .filter(Report.status.isnot(None))
        .group_by(Report.status)
        .all()
    )
    recent_failures = (
        AIJob.query
        .filter(AIJob.status == 'failed', AIJob.updated_at >= last_24h)
        .count()
    )
    stale_running = AIJob.query.filter(
        AIJob.status == 'running',
        or_(AIJob.locked_at.is_(None), AIJob.locked_at < stale_cutoff),
    ).count()
    active_rate_limit_buckets = RateLimitBucket.query.count()
    total_members = Member.query.count()
    total_reports = Report.query.count()

    return jsonify({
        'generated_at': now.isoformat(),
        'service': {
            'process_role': app.config.get('PROCESS_ROLE'),
            'ai_model_configured': bool(app.config.get('AI_AVAILABLE')),
            'ai_worker_available': bool(app.config.get('AI_WORKER_AVAILABLE')),
        },
        'jobs': {
            'pending': job_status_rows.get('queued', 0),
            'running': job_status_rows.get('running', 0),
            'completed': job_status_rows.get('completed', 0),
            'failed': job_status_rows.get('failed', 0),
            'recent_failed_24h': recent_failures,
            'stale_running': stale_running,
            'by_status': job_status_rows,
        },
        'reports': {
            'total': total_reports,
            'by_status': report_status_rows,
        },
        'platform': {
            'members_total': total_members,
            'active_rate_limit_buckets': active_rate_limit_buckets,
        },
    })


# --- Moved to services/admin_service.py ---

@app.context_processor
def inject_global_vars():
    """모든 템플릿에서 쓸 수 있는 전역 변수 주입"""
    admin_unread_count = 0
    if session.get('is_admin'):
        admin_unread_count = Report.query.filter(
            Report.status == '접수완료',
            Report.last_checked_at.is_(None),
        ).count()
    return dict(kakao_js_key=kakao_js_key, admin_unread_count=admin_unread_count, csrf_token=csrf_token())


# 정적 파일 서빙
@app.route('/manifest.json')
def serve_manifest():
    return send_from_directory('static', 'manifest.json')


@app.route('/sw.js')
def serve_sw():
    response = make_response(send_from_directory('static', 'sw.js'))
    response.headers['Content-Type'] = 'application/javascript'
    return response


@app.route('/favicon.ico')
def serve_favicon():
    favicon_path = os.path.join(base_dir, 'static', 'favicon.ico')
    if os.path.exists(favicon_path):
        return send_from_directory('static', 'favicon.ico', mimetype='image/png')
    return send_from_directory('static/icons', 'icon-192.png', mimetype='image/png')


@app.route('/uploads/<path:filename>')
def serve_uploads(filename):
    requested_path = '/uploads/' + filename.replace('\\', '/').lstrip('/')
    report = Report.query.filter(
        (Report.file_path.in_((requested_path, requested_path.lstrip('/')))) |
        (Report.thumbnail_path.in_((requested_path, requested_path.lstrip('/'))))
    ).first()
    if report is None:
        return jsonify({'success': False, 'message': '파일을 찾을 수 없습니다.'}), 404

    member = db.session.get(Member, session.get('user_id')) if session.get('user_id') else None
    if report.status == '삭제' and (not member or canonical_role(member) != 'admin'):
        return jsonify({'success': False, 'message': '파일을 찾을 수 없습니다.'}), 404
    if not can_access_report(member, report):
        return jsonify({'success': False, 'message': '파일 열람 권한이 없습니다.'}), 403
    return send_from_directory(UPLOAD_BASE_DIR, filename)


@app.route('/ppt/images/<path:filename>')
def serve_ppt_images(filename):
    return send_from_directory(os.path.join(base_dir, 'templates', 'ppt', 'images'), filename)


# 메인 및 공통 라우트
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/login_page')
def login_page():
    return redirect(url_for('auth.login'))


# --- 대시보드 고도화 유틸리티 함수 --- #

def normalize_region_name(region_text):
    if not region_text: return ''
    text = region_text.strip()
    parts = text.split()
    if len(parts) >= 2:
        first, second = parts[0], parts[1]
        if first.endswith('시') and (second.endswith('구') or second.endswith('군') or second.endswith('시')):
            return f"{first} {second}"
    return parts[0] if len(parts) >= 1 else ''


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000  # meters
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(
        dlon / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c


def get_priority_score(report, now=None):
    if now is None: now = datetime.now()
    score = 0
    # AI 신뢰도를 위험 점수로 활용 (None 방어 코드)
    confidence = float(report.ai_result.confidence or 0) if report.ai_result else 0
    status = report.status
    created_at = report.created_at

    if status == '접수완료': score += 100
    if confidence >= 80:
        score += 50
    elif confidence >= 50:
        score += 20

    # 반복 제보(그룹화 시 계산됨) - 여기서는 기본 점수만
    if status == '접수완료' and created_at and (now - created_at).total_seconds() >= 86400:
        score += 40
    return score


def get_priority_label(score):
    if score >= 150:
        return '긴급'
    elif score >= 80:
        return '주의'
    return '일반'


def group_reports(raw_reports):
    grouped = []
    used_ids = set()
    for r in raw_reports:
        if r.id in used_ids: continue
        group_members = [r]
        used_ids.add(r.id)
        reporter_ids = {r.user_id}

        for other in raw_reports:
            if other.id == r.id or other.id in used_ids: continue
            if r.latitude is None or r.longitude is None or other.latitude is None or other.longitude is None: continue

            distance = haversine_m(r.latitude, r.longitude, other.latitude, other.longitude)
            time_diff = abs((r.created_at - other.created_at).total_seconds())

            if distance <= 50 and time_diff <= 86400:
                used_ids.add(other.id)
                group_members.append(other)
                if other.user_id: reporter_ids.add(other.user_id)

        # 대표 리포트 선정 (가장 높은 신뢰도 기준)
        representative = max(group_members,
                             key=lambda x: (x.ai_result.confidence if x.ai_result else 0, x.created_at.timestamp()))
        representative.group_count = len(group_members)
        representative.reporter_count = len(reporter_ids)
        representative.members = group_members
        grouped.append(representative)
    return grouped


# --- Admin functions moved to admin_service.py ---

# AI 분석 함수 (Thread용 공통 기능)
def run_ai_analysis(report_id, file_path, file_type):
    if not model:
        raise RuntimeError('AI 모델을 사용할 수 없습니다.')
    abs_path = os.path.join(base_dir, file_path.lstrip('/'))
    cap = None
    out = None
    output_abs_path = None
    try:
        with app.app_context():
            AiResult.query.filter_by(report_id=report_id).delete(synchronize_session=False)
            VideoDetection.query.filter_by(report_id=report_id).delete(synchronize_session=False)
            db.session.commit()
        is_damaged = False
        max_conf = 0.0
        pothole_max_conf = 0.0
        max_pothole_in_frame = 0
        total_pothole_count = 0
        sinkhole_count = 0
        damage_type = "없음"
        annotated_path = None

        if file_type == 'video':
            # === 동영상 분석: 프레임 추출 후 YOLO 분석 및 박스 오버레이 인코딩 ===
            app.logger.info('동영상 AI 분석 시작 (report=%s)', report_id)
            cap = cv2.VideoCapture(abs_path)
            if not cap.isOpened():
                raise RuntimeError('동영상 파일을 열 수 없습니다.')

            fps = cap.get(cv2.CAP_PROP_FPS) or 30
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

            # 출력 파일 설정 (H.264 코덱 사용)
            name, ext = os.path.splitext(os.path.basename(abs_path))
            output_filename = f"res_{name}.mp4"
            output_abs_path = os.path.join(os.path.dirname(abs_path), output_filename)
            fourcc = cv2.VideoWriter_fourcc(*'avc1')  # H.264 브라우저 호환 코덱
            out = cv2.VideoWriter(output_abs_path, fourcc, fps, (width, height))
            if not out.isOpened():
                # avc1 실패 시 mp4v 폴백
                fourcc = cv2.VideoWriter_fourcc(*'mp4v')
                out = cv2.VideoWriter(output_abs_path, fourcc, fps, (width, height))
            if not out.isOpened():
                cap.release()
                raise RuntimeError('분석 동영상 출력 파일을 생성할 수 없습니다.')

            best_frame = None
            best_result = None
            best_conf = 0.0
            frame_idx = 0
            frame_detections = []

            sample_fps = max(0.2, float(os.getenv('AI_VIDEO_SAMPLE_FPS', '2')))
            sample_interval = max(int(round(fps / sample_fps)), 1)
            max_frames = max(1, int(fps * 90))

            while cap.isOpened():
                ret, frame = cap.read()
                if not ret:
                    break

                frame_h, frame_w = frame.shape[:2]
                current_time_sec = frame_idx / fps

                if frame_idx % sample_interval == 0:
                    with model_lock:
                        results = model(frame, verbose=False)
                    annotated_frame = results[0].plot()
                    out.write(annotated_frame)
                    for r in results:
                        if len(r.boxes) > 0:
                            frame_pothole_count = 0
                            for box in r.boxes:
                                cls_name = r.names[int(box.cls[0])]
                                conf = float(box.conf[0])
                                xyxy = box.xyxy[0].tolist()
                                nx1, ny1, nx2, ny2 = xyxy[0] / frame_w, xyxy[1] / frame_h, xyxy[2] / frame_w, xyxy[
                                    3] / frame_h

                                frame_detections.append({
                                    'frame_time': round(current_time_sec, 2),
                                    'class_name': cls_name,
                                    'confidence': round(conf, 4),
                                    'x1': round(nx1, 4), 'y1': round(ny1, 4),
                                    'x2': round(nx2, 4), 'y2': round(ny2, 4)
                                })

                                if 'pothole' in cls_name.lower():
                                    is_damaged = True
                                    total_pothole_count += 1
                                    frame_pothole_count += 1
                                    if conf > pothole_max_conf:
                                        pothole_max_conf = conf
                                elif 'sinkhole' in cls_name.lower():
                                    is_damaged = True
                                    sinkhole_count += 1

                                if conf > max_conf:
                                    max_conf, damage_type = conf, cls_name
                                if conf > best_conf:
                                    best_conf = conf
                                    best_frame = frame.copy()
                                    best_result = results[0]

                            if frame_pothole_count > max_pothole_in_frame:
                                max_pothole_in_frame = frame_pothole_count
                else:
                    out.write(frame)

                frame_idx += 1
                if frame_idx >= max_frames:
                    break

            cap.release()
            out.release()
            app.logger.info('동영상 AI 분석 완료 (report=%s, frames=%s, detections=%s)', report_id, frame_idx, len(frame_detections))

            # 프레임별 검출 결과를 DB에 일괄 저장
            if frame_detections:
                with app.app_context():
                    for det in frame_detections:
                        db.session.add(VideoDetection(
                            report_id=report_id,
                            frame_time=det['frame_time'],
                            class_name=det['class_name'],
                            confidence=det['confidence'],
                            x1=det['x1'], y1=det['y1'],
                            x2=det['x2'], y2=det['y2']
                        ))
                    db.session.commit()
                    app.logger.info('동영상 검출 결과 저장 (report=%s, count=%s)', report_id, len(frame_detections))

            # 가장 높은 신뢰도 프레임을 AI 결과 썸네일로 저장
            if best_result is not None and best_frame is not None:
                annotated_filename = f"{name}_ai.jpg"
                annotated_abs = os.path.join(base_dir, 'uploads', 'images', annotated_filename)
                os.makedirs(os.path.dirname(annotated_abs), exist_ok=True)
                cv2.imwrite(annotated_abs, best_result.plot())
                annotated_path = f'/uploads/images/{annotated_filename}'
                app.logger.info('AI 대표 프레임 저장 (report=%s)', report_id)

            # 프레임별 검출 정보와 대표 이미지만 사용하므로 중간 인코딩 영상은 보관하지 않는다.
            if os.path.isfile(output_abs_path):
                os.remove(output_abs_path)

        else:
            # === 이미지 분석 (기존 로직) ===
            with model_lock:
                results = model(abs_path, verbose=False)

            for r in results:
                if len(r.boxes) > 0:
                    frame_pothole_count = 0
                    for box in r.boxes:
                        cls_name = r.names[int(box.cls[0])]
                        conf = float(box.conf[0])
                        if 'pothole' in cls_name.lower():
                            is_damaged = True
                            total_pothole_count += 1
                            frame_pothole_count += 1
                            if conf > pothole_max_conf: pothole_max_conf = conf
                        elif 'sinkhole' in cls_name.lower():
                            is_damaged = True
                            sinkhole_count += 1

                        if conf > max_conf: max_conf, damage_type = conf, cls_name

                    if frame_pothole_count > max_pothole_in_frame:
                        max_pothole_in_frame = frame_pothole_count

            if (is_damaged or (len(results) > 0 and len(results[0].boxes) > 0)):
                name = os.path.splitext(os.path.basename(abs_path))[0]
                annotated_filename = f"{name}_ai.jpg"
                annotated_abs = os.path.join(os.path.dirname(abs_path), annotated_filename)
                cv2.imwrite(annotated_abs, results[0].plot())
                annotated_path = f'/uploads/images/{annotated_filename}'

        with app.app_context():
            rpt = db.session.get(Report, report_id)
            if rpt:
                db.session.add(AiResult(report_id=report_id, is_damaged=is_damaged, confidence=round(max_conf * 100, 1),
                                        damage_type=damage_type))
                if annotated_path:
                    rpt.thumbnail_path = annotated_path  # 원본 경로는 보존하되 새로 갱신

                # [FIX] 원본 file_path를 보존하여 브라우저에서 항상 재생 가능하도록 함
                # AI 분석 영상(res_*.mp4)은 코덱 호환성 문제로 재생 불가할 수 있으므로
                # 원본 영상 경로를 유지하고 thumbnail_path만 갱신
                # (기존: rpt.file_path = encoded_video_path)

                # AI 분석 승인 조건: (포트홀 60% 이상) OR (단일 프레임 포트홀 3개 이상) OR (싱크홀 1개 이상)
                is_valid_report = (pothole_max_conf >= 0.6) or (max_pothole_in_frame >= 3) or (sinkhole_count > 0)

                rpt.reject_reason = None
                app.logger.info(
                    'AI 분석 완료 (report=%s, valid=%s)', rpt.id, is_valid_report
                )
                db.session.commit()
    except Exception:
        app.logger.exception('AI Analysis Error for report %s', report_id)
        raise
    finally:
        if cap is not None:
            cap.release()
        if out is not None:
            out.release()
        if output_abs_path and os.path.isfile(output_abs_path):
            os.remove(output_abs_path)


# current_app을 통해 접근 가능하도록 바인딩
app.run_ai_analysis = run_ai_analysis
from services.ai_queue import AIJobQueue, init_ai_worker_cli
queue_supported = app.config['AI_AVAILABLE'] and process_role in {'all', 'web', 'worker'}
start_embedded_workers = runtime_services_enabled and not is_flask_cli and process_role == 'all'
app.ai_job_queue = AIJobQueue(
    app, run_ai_analysis, start_workers=start_embedded_workers
) if queue_supported else None
app.submit_ai_analysis = app.ai_job_queue.submit if app.ai_job_queue else lambda *_args, **_kwargs: False
init_ai_worker_cli(app, app.ai_job_queue)
from services.retention import init_retention
init_retention(
    app,
    start_scheduler=runtime_services_enabled and not is_flask_cli and process_role == 'all',
)
from services.admin_cli import init_admin_cli
init_admin_cli(app)
from services.backup import init_backup_cli
init_backup_cli(app)

# 서버 실행부
if __name__ == '__main__':
    with app.app_context():
        from flask_migrate import upgrade
        upgrade()
        from services.report_workflow import backfill_legacy_statuses
        backfill_legacy_statuses()
    app.logger.info('CRACK 서버 준비 완료: http://127.0.0.1:9200')
    # 사용자가 0.0.0.0을 브라우저에 입력하는 오류를 방지하기 위해 127.0.0.1로 바인딩
    socketio.run(app, host='0.0.0.0', port=9200, debug=False)
