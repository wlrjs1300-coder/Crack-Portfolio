import os
import certifi
import threading
import math
from datetime import datetime, timedelta
from decimal import Decimal
from flask import Flask, render_template, session, redirect, url_for, send_from_directory, make_response, request, \
    jsonify
from dotenv import load_dotenv
from ultralytics import YOLO
import cv2

# 내부 모듈 임포트
from extensions import db, socketio
from models import Report, AiResult, Member, VideoDetection
from utils import reverse_geocode

# 서비스 Blueprint 임포트
from services.auth_service import auth_bp
from services.alert_service import alert_bp
from services.report_service import report_bp
from services.status_service import status_bp
from services.my_service import my_bp
from services.admin_service import admin_bp
from werkzeug.utils import secure_filename
import ssl
ssl._create_default_https_context = ssl._create_unverified_context

# .env 파일 로드 (secrets 폴더 확인)
base_dir = os.path.dirname(__file__)
env_path = os.path.join(base_dir, 'secrets', '.env')

if not os.path.exists(env_path):
    print("\n" + "!" * 50)
    print("⚠️  CRITICAL ERROR: 'secrets/.env' FILE NOT FOUND!")
    print("팀원들은 'secrets.example' 폴더의 내용을 참고하여 'secrets' 폴더를 생성하고")
    print("필요한 설정 파일들을 직접 만들어야 합니다.")
    print("!" * 50 + "\n")
else:
    load_dotenv(env_path)

app = Flask(__name__)

flask_secret_key = os.getenv('FLASK_SECRET_KEY')
if not flask_secret_key:
    raise RuntimeError(
        "FLASK_SECRET_KEY 환경변수가 설정되지 않았습니다. "
        "secrets/.env 파일을 확인하세요."
    )
app.secret_key = flask_secret_key

# [용어 정의] 상단바와 하단바를 제외한 실질적인 본문 영역을 '메인 콘텐츠 영역' 또는 '메인 영역'으로 정의합니다.
MAIN_CONTENT_AREA = "메인 콘텐츠 영역 (Main Content Area)"

# DB 설정 (TiDB Cloud 연결 지원)
db_user = os.getenv('DB_USER')
db_password = os.getenv('DB_PASSWORD')
db_host = os.getenv('DB_HOST')
db_port = os.getenv('DB_PORT', '3306')
db_name = os.getenv('DB_NAME')

if not all([db_user, db_password, db_host, db_name]):
    print("⚠️  Warning: Database environment variables are missing.")
    # 기본값 설정을 통해 최소한의 구성은 유지하거나 에러 처리 필요
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///temp_debug.db'
else:
    is_local_db = db_host in ('127.0.0.1', 'localhost')
    ssl_query = '' if is_local_db else f"?ssl_ca={certifi.where()}"
    app.config[
        'SQLALCHEMY_DATABASE_URI'] = f"mysql+pymysql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}{ssl_query}"

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,
    'pool_recycle': 3600,
    'connect_args': {
        'init_command': "SET time_zone = '+09:00'"
    }
}

# 업로드 설정 (최대 100MB)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024
UPLOAD_BASE_DIR = os.path.join(base_dir, 'uploads')
UPLOAD_IMAGE_DIR = os.path.join(UPLOAD_BASE_DIR, 'images')
UPLOAD_VIDEO_DIR = os.path.join(UPLOAD_BASE_DIR, 'videos')

# 디렉토리 생성
for d in [UPLOAD_IMAGE_DIR, UPLOAD_VIDEO_DIR]:
    if not os.path.exists(d):
        os.makedirs(d)

# DB 초기화
# app.py 60~62라인쯤에 추가
print("DEBUG: SQLALCHEMY_DATABASE_URI =", app.config.get('SQLALCHEMY_DATABASE_URI'))

db.init_app(app)
socketio.init_app(app)

# AI 모델 로드
try:
    model_path = os.path.join(base_dir, 'static', 'best.pt')
    model = YOLO(model_path)
except Exception as e:
    print(f"Error loading YOLO model: {e}")
    model = None

# Blueprint 등록
app.register_blueprint(auth_bp)
app.register_blueprint(alert_bp)
app.register_blueprint(report_bp)
app.register_blueprint(status_bp)
app.register_blueprint(my_bp)
app.register_blueprint(admin_bp)

# --- 공통 기능 및 API 설정 --- #

# 카카오 JS 키 로드 및 주입
kakao_js_key = ""
try:
    with open(os.path.join(base_dir, 'secrets', 'kakao_js_key.txt'), 'r', encoding='utf-8') as f:
        kakao_js_key = f.read().strip()
    app.config['KAKAO_JS_KEY'] = kakao_js_key
except Exception as e:
    print(f"Error loading kakao js key: {e}")


# --- Moved to services/admin_service.py ---

@app.context_processor
def inject_global_vars():
    """모든 템플릿에서 쓸 수 있는 전역 변수 주입"""
    admin_unread_count = 0
    if session.get('is_admin'):
        admin_unread_count = Report.query.filter_by(status='관리자 확인중').count()
    return dict(kakao_js_key=kakao_js_key, admin_unread_count=admin_unread_count)


# 정적 파일 서빙
@app.route('/manifest.json')
def serve_manifest():
    return send_from_directory('static', 'manifest.json')


@app.route('/sw.js')
def serve_sw():
    response = make_response(send_from_directory('static', 'sw.js'))
    response.headers['Content-Type'] = 'application/javascript'
    return response


@app.route('/uploads/<path:filename>')
def serve_uploads(filename):
    return send_from_directory(UPLOAD_BASE_DIR, filename)


@app.route('/ppt/images/<path:filename>')
def serve_ppt_images(filename):
    return send_from_directory(os.path.join(base_dir, 'templates', 'ppt', 'images'), filename)


# 내 게시글 삭제하기
@app.route('/api/report/<int:report_id>/delete', methods=['POST'])
def delete_report(report_id):
    from sqlalchemy import text as sa_text

    current_user_id = session.get('user_id')
    if not current_user_id:
        return jsonify({'success': False, 'message': '로그인이 필요합니다.'}), 401

    rpt = Report.query.get_or_404(report_id)

    # ✅ 세션 대신 DB에서 직접 admin 여부 확인 (세션 오염 방지)
    row = db.session.execute(
        sa_text("SELECT is_admin, role FROM members WHERE id = :uid LIMIT 1"),
        {'uid': current_user_id}
    ).mappings().first()

    is_admin = False
    if row:
        is_admin = (row.get('is_admin') == 1) or (row.get('role') == 'admin')

    # admin은 모든 글 삭제 가능, 일반 사용자는 본인 글만
    if not is_admin and str(rpt.user_id) != str(current_user_id):
        return jsonify({'success': False, 'message': '본인 제보만 삭제할 수 있습니다.'}), 403

    try:
        rpt.status = '삭제'
        db.session.commit()
        return jsonify({'success': True, 'message': '제보가 삭제되었습니다.'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500


# 메인 및 공통 라우트
@app.route('/')
def index():
    if not session.get('user_id'):
        return redirect(url_for('alert.alert_page'))

    # [보정] 세션 어드민 권한 동기화 (DB 상태와 세션 불일치 해결)
    user = db.session.get(Member, session['user_id'])
    if user:
        session['is_admin'] = user.is_admin

    return render_template('index.html')


@app.route('/login_page')
def login_page():
    return redirect(url_for('auth.login'))


@app.route('/map-test')
def map_test():
    return render_template('map_test.html')


@app.route('/sw.js')
def sw():
    return app.send_static_file('sw.js')


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

    if status == '관리자 확인중': score += 100
    if confidence >= 80:
        score += 50
    elif confidence >= 50:
        score += 20

    # 반복 제보(그룹화 시 계산됨) - 여기서는 기본 점수만
    if status == '관리자 확인중' and created_at and (now - created_at).total_seconds() >= 86400:
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
    if not model: return
    abs_path = os.path.join(base_dir, file_path.lstrip('/'))
    try:
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
            print(f"[AI Video] Starting video analysis: {abs_path}")
            cap = cv2.VideoCapture(abs_path)
            if not cap.isOpened():
                print(f"[AI Video] ERROR: Cannot open video file")
                return

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

            best_frame = None
            best_result = None
            best_conf = 0.0
            frame_idx = 0
            frame_detections = []

            sample_interval = max(int(fps // 5), 1)

            while cap.isOpened():
                ret, frame = cap.read()
                if not ret:
                    break

                frame_h, frame_w = frame.shape[:2]
                current_time_sec = frame_idx / fps

                results = model(frame, verbose=False)
                # 현재 프레임에 CV 박스 그리기
                annotated_frame = results[0].plot()
                out.write(annotated_frame)

                # DB 저장용 데이터 추출 (초당 약 5번만 기록)
                if frame_idx % sample_interval == 0:
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

                frame_idx += 1
                # 혹시 너무 길어지는걸 방지하기 위해 1.5분(2700프레임) 단위로 자르기
                if frame_idx >= 2700:
                    break

            cap.release()
            out.release()
            print(
                f"[AI Video] Analyzed {frame_idx} frames. Detections={len(frame_detections)}, Pothole={total_pothole_count}, Sinkhole={sinkhole_count}")
            print(f"[AI Video] Output video saved to {output_abs_path}")

            encoded_video_path = f'/uploads/videos/{output_filename}'

            # 프레임별 검출 결과를 DB에 일괄 저장
            if frame_detections:
                with app.app_context():
                    from models import VideoDetection
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
                    print(f"[AI Video] Saved {len(frame_detections)} detections to DB")

            # 가장 높은 신뢰도 프레임을 AI 결과 썸네일로 저장
            if best_result is not None and best_frame is not None:
                annotated_filename = f"{name}_ai.jpg"
                annotated_abs = os.path.join(base_dir, 'uploads', 'images', annotated_filename)
                os.makedirs(os.path.dirname(annotated_abs), exist_ok=True)
                cv2.imwrite(annotated_abs, best_result.plot())
                annotated_path = f'/uploads/images/{annotated_filename}'
                print(f"[AI Video] Best frame saved: {annotated_path}")

        else:
            # === 이미지 분석 (기존 로직) ===
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
            rpt = Report.query.get(report_id)
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

                if is_valid_report:
                    rpt.status = '관리자 확인중'
                    print(f"[SOCKET] Emitting new_report for {rpt.address}")
                    socketio.emit('new_report', {'address': rpt.address or '위치 미상', 'report_id': rpt.id})
                else:
                    rpt.status = '반려'
                    if total_pothole_count == 0 and sinkhole_count == 0:
                        rpt.reject_reason = 'AI 분석 결과 도로 파손(포트홀/싱크홀)이 감지되지 않았습니다. 다시 정확하게 촬영해주세요.'
                    else:
                        rpt.reject_reason = 'AI 분석 결과 도로 파손 유효성 기준(포트홀 신뢰도 60% 미만 등)에 미달했습니다. 명확하게 다시 촬영해주세요.'
                db.session.commit()
    except Exception as e:
        print(f"AI Analysis Error: {e}")


# current_app을 통해 접근 가능하도록 바인딩
app.run_ai_analysis = run_ai_analysis

# 서버 실행부
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    print("\n" + "=" * 50)
    print("🚀  CRACK SERVER v1.2.8  READY")
    print("📈  Smart Road Safety Platform")
    print("▶ 로컬 접속 주소 : http://127.0.0.1:9200")
    print("=" * 50 + "\n")
    # 사용자가 0.0.0.0을 브라우저에 입력하는 오류를 방지하기 위해 127.0.0.1로 바인딩
    socketio.run(app, host='0.0.0.0', port=9200, debug=False, allow_unsafe_werkzeug=True)
