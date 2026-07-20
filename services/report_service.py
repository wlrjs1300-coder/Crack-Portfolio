import os
import re
import subprocess
import logging
import threading
from datetime import timedelta
from flask import Blueprint, render_template, session, redirect, url_for, request, jsonify, current_app
from extensions import db
from models import Report
from utils import extract_gps_from_exif, haversine, reverse_geocode, get_now_kst
from services.region_service import normalize_region_name
from services.media_security import (
    MediaValidationError, consume_pending_upload, extension_of, remember_pending_upload,
    sanitize_image_to_jpeg, save_and_validate, validate_saved_media,
)
from services.security import can_access_report, rate_limit
from services.privacy_filter import PrivacyFilterError, blur_image_in_place, blur_video_in_place

report_bp = Blueprint('report', __name__)
logger = logging.getLogger(__name__)
_ocr_reader = None
_ocr_lock = threading.Lock()

# [용어 정의] 상단바와 하단바를 제외한 실질적인 본문 영역을 '메인 콘텐츠 영역' 또는 '메인 영역'으로 정의합니다.
MAIN_CONTENT_AREA = "메인 콘텐츠 영역 (Main Content Area)"

# 허용된 확장자 (HEIC/HEIF 추가)
ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'heic', 'heif'}
ALLOWED_VIDEO_EXTENSIONS = {'mp4', 'mov', 'avi', 'm4v'}

UPLOAD_IMAGE_DIR = os.path.join('uploads', 'images')
UPLOAD_VIDEO_DIR = os.path.join('uploads', 'videos')


def convert_to_mp4(save_path: str, video_dir: str, filename: str):
    """MOV/AVI/M4V를 MP4로 변환. 반환값: (새 save_path, 새 file_path)"""
    ext = filename.rsplit('.', 1)[-1].lower()
    if ext == 'mp4':
        return save_path, f'/uploads/videos/{filename}'

    new_filename = filename.rsplit('.', 1)[0] + '.mp4'
    new_save_path = os.path.join(os.path.dirname(save_path), new_filename)
    try:
        import imageio_ffmpeg
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        subprocess.run(
            [ffmpeg_exe, '-i', save_path, '-vcodec', 'libx264', '-acodec', 'aac', '-y', new_save_path],
            check=True, capture_output=True, timeout=120
        )
        validate_saved_media(new_save_path, 'mp4')
        os.remove(save_path)
        logger.info('동영상 MP4 변환 완료')
        return new_save_path, f'/uploads/videos/{new_filename}'
    except Exception as e:
        if os.path.exists(new_save_path):
            os.remove(new_save_path)
        logger.warning('동영상 MP4 변환 실패, 검증된 원본 유지: %s', e)
        return save_path, f'/uploads/videos/{filename}'


def extract_gps_from_video(video_path, original_filename=None):
    # 1단계: 바이너리 파싱
    try:
        with open(video_path, 'rb') as f:
            raw = f.read(5 * 1024 * 1024)
            file_size = os.path.getsize(video_path)
            if file_size > 5 * 1024 * 1024:
                f.seek(max(0, file_size - 5 * 1024 * 1024))
                raw += f.read(5 * 1024 * 1024)

        # ©xyz 방식 (삼성/아이폰)
        idx = raw.find(b'\xa9xyz')
        if idx != -1:
            context = raw[idx:idx + 50].decode('utf-8', errors='ignore')
            match = re.search(r'([+-]\d{1,3}\.\d+)([+-]\d{1,3}\.\d+)', context)
            if match:
                lat_c, lng_c = float(match.group(1)), float(match.group(2))
                if 33.0 <= lat_c <= 38.5 and 124.0 <= lng_c <= 132.0:
                    logger.debug('동영상 GPS 추출 성공: xyz 메타데이터')
                    return lat_c, lng_c

        # 바이너리 전체 텍스트에서 좌표 패턴 탐색 (블랙박스 커스텀 박스 대응)
        text = raw.decode('utf-8', errors='ignore')
        match = re.search(r'([+-]?\d{2,3}\.\d{5,})[^\d]+([+-]?\d{2,3}\.\d{5,})', text)
        if match:
            lat_c, lng_c = float(match.group(1)), float(match.group(2))
            if 33.0 <= lat_c <= 38.5 and 124.0 <= lng_c <= 132.0:
                logger.debug('동영상 GPS 추출 성공: 바이너리 스캔')
                return lat_c, lng_c

        logger.debug('동영상 GPS 바이너리 추출 실패')
    except Exception:
        logger.debug('동영상 GPS 바이너리 추출 오류', exc_info=True)

    # 2단계: 로그 파일
    try:
        base_path = os.path.splitext(video_path)[0]
        for ext in ['.gps', '.nmea', '.txt']:
            log_path = base_path + ext
            if os.path.exists(log_path):
                with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                nmea = re.search(r'\$GP(?:RMC|GGA),[\d.]*,A?,(\d{2})(\d{2}\.\d+),([NS]),(\d{3})(\d{2}\.\d+),([EW])',
                                 content)
                if nmea:
                    lat = (int(nmea.group(1)) + float(nmea.group(2)) / 60) * (-1 if nmea.group(3) == 'S' else 1)
                    lng = (int(nmea.group(4)) + float(nmea.group(5)) / 60) * (-1 if nmea.group(6) == 'W' else 1)
                    return lat, lng
        logger.debug('동영상 GPS 보조 로그 추출 실패')
    except Exception:
        logger.debug('동영상 GPS 보조 로그 추출 오류', exc_info=True)

    # 3단계: OCR
    try:
        if os.getenv('VIDEO_GPS_OCR_ENABLED', 'true').lower() not in ('1', 'true', 'yes'):
            return None, None
        import cv2, easyocr
        cap = cv2.VideoCapture(video_path)
        fps = cap.get(cv2.CAP_PROP_FPS) or 30
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        global _ocr_reader
        with _ocr_lock:
            if _ocr_reader is None:
                _ocr_reader = easyocr.Reader(['ko', 'en'], gpu=False, verbose=False)
            reader = _ocr_reader
        coord_re = re.compile(r'([-+]?\d{1,3}\.\d{4,})')
        for i in range(min(5, int(fps * 3))):
            cap.set(cv2.CAP_PROP_POS_FRAMES, i * max(1, total // 5))
            ret, frame = cap.read()
            if not ret:
                continue
            h, w = frame.shape[:2]
            roi = frame[int(h * 0.8):h, 0:w]
            coords = coord_re.findall(' '.join(reader.readtext(roi, detail=0)))
            for j in range(len(coords) - 1):
                lat_c, lng_c = float(coords[j]), float(coords[j + 1])
                if 33.0 <= lat_c <= 38.5 and 124.0 <= lng_c <= 132.0:
                    cap.release()
                    return lat_c, lng_c
        cap.release()
        logger.debug('동영상 GPS OCR 추출 실패')
    except Exception:
        logger.debug('동영상 GPS OCR 추출 오류', exc_info=True)

    return None, None


@report_bp.route('/report', methods=['GET'])
def report_page():
    if not session.get('user_id'):
        return redirect(url_for('auth.login'))
    return render_template('report.html')


@report_bp.route('/api/upload', methods=['POST'])
@rate_limit(10, 3600)
def upload_file():
    if not session.get('user_id'):
        return jsonify({'success': False, 'message': '로그인이 필요합니다.'}), 401
    if 'file' not in request.files:
        return jsonify({'success': False, 'message': '파일이 없습니다.'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'message': '선택된 파일이 없습니다.'}), 400

    try:
        save_path, uploaded_path, kind = save_and_validate(file)
    except MediaValidationError as exc:
        return jsonify({'success': False, 'message': str(exc)}), 400

    if kind == 'image':
        lat, lng = extract_gps_from_exif(save_path)
        import math
        if lat is not None and (math.isnan(lat) or math.isinf(lat)):
            lat = None
        if lng is not None and (math.isnan(lng) or math.isinf(lng)):
            lng = None

        address = None
        if lat is not None and lng is not None:
            address = reverse_geocode(lat, lng)
        os.remove(save_path)

        return jsonify({
            'success': True,
            'message': '이미지 업로드 성공 (GPS 추출 시도)',
            'path': None,
            'gps': {'lat': lat, 'lng': lng} if lat is not None and lng is not None else None,
            'address': address
        })

    if kind == 'video':
        filename = os.path.basename(save_path)
        # MOV 등 → MP4 변환
        save_path, video_path = convert_to_mp4(save_path, UPLOAD_VIDEO_DIR, filename)
        vid_lat, vid_lng = extract_gps_from_video(save_path, file.filename)
        address = None
        if vid_lat is not None and vid_lng is not None:
            address = reverse_geocode(vid_lat, vid_lng)
        try:
            save_path = blur_video_in_place(save_path)
            video_path = f'/uploads/videos/{os.path.basename(save_path)}'
        except PrivacyFilterError as exc:
            os.remove(save_path)
            return jsonify({'success': False, 'message': str(exc)}), 500
        remember_pending_upload(video_path)

        return jsonify({
            'success': True,
            'message': '동영상 업로드 성공',
            'path': video_path,
            'gps': {'lat': vid_lat, 'lng': vid_lng} if vid_lat is not None and vid_lng is not None else None,
            'address': address
        })

    return jsonify({'success': False, 'message': '지원하지 않는 파일입니다.'}), 400


@report_bp.route('/api/report', methods=['POST'])
@rate_limit(10, 3600)
def submit_report():
    if not session.get('user_id'):
        return jsonify({'success': False, 'message': '제보를 위해 로그인이 필요합니다.'}), 401

    user_id = session.get('user_id')
    title = request.form.get('title', '').strip()
    content = (request.form.get('content') or '').strip()
    latitude = request.form.get('latitude')
    longitude = request.form.get('longitude')
    address = request.form.get('address')

    if not title or len(title) > 30:
        return jsonify({'success': False, 'message': '제목은 1자 이상 30자 이하로 입력해주세요.'}), 400
    if len(content) > 5000 or len(address or '') > 255:
        return jsonify({'success': False, 'message': '신고 내용 또는 주소가 허용 길이를 초과했습니다.'}), 400

    file_path = None
    file_type = None
    save_path = None

    # 영상 이중 업로드 방지 - 이미 /api/upload에서 저장된 path 재사용
    pre_uploaded_path = request.form.get('pre_uploaded_path')
    if pre_uploaded_path:
        file_path = consume_pending_upload(pre_uploaded_path)
        if not file_path:
            return jsonify({'success': False, 'message': '사전 업로드 파일이 유효하지 않거나 만료되었습니다.'}), 400
        file_type = 'video'
        save_path = os.path.realpath(os.path.join(current_app.root_path, file_path.lstrip('/').replace('/', os.sep)))

    elif 'file' in request.files and request.files['file'].filename != '':
        file = request.files['file']
        try:
            save_path, file_path, file_type = save_and_validate(file)
        except MediaValidationError as exc:
            return jsonify({'success': False, 'message': str(exc)}), 400

        if file_type == 'image':

            front_has_gps = bool(latitude and longitude)
            if not front_has_gps:
                logger.debug('클라이언트 GPS가 없어 서버 추출을 시도합니다.')
                exif_lat, exif_lng = extract_gps_from_exif(save_path)
                if exif_lat and exif_lng:
                    latitude = exif_lat
                    longitude = exif_lng
                    logger.debug('서버 이미지 GPS 추출 성공')
                else:
                    logger.debug('서버 이미지 GPS 추출 실패')
            else:
                logger.debug('클라이언트 제공 GPS 사용')

            try:
                blur_image_in_place(save_path)
                save_path, file_path = sanitize_image_to_jpeg(save_path)
            except (MediaValidationError, PrivacyFilterError) as exc:
                if save_path and os.path.isfile(save_path):
                    os.remove(save_path)
                return jsonify({'success': False, 'message': str(exc)}), 400

        elif file_type == 'video':

            # MOV 등 → MP4 변환
            save_path, file_path = convert_to_mp4(save_path, UPLOAD_VIDEO_DIR, os.path.basename(save_path))

            front_has_gps = bool(latitude and longitude)
            if not front_has_gps:
                vid_lat, vid_lng = extract_gps_from_video(save_path, file.filename)
                if vid_lat and vid_lng:
                    latitude, longitude = vid_lat, vid_lng
                    logger.debug('서버 동영상 GPS 추출 성공')
                else:
                    logger.debug('서버 동영상 GPS 추출 실패')
            try:
                save_path = blur_video_in_place(save_path)
                file_path = f'/uploads/videos/{os.path.basename(save_path)}'
            except PrivacyFilterError as exc:
                os.remove(save_path)
                return jsonify({'success': False, 'message': str(exc)}), 500

    if not file_path or not file_type:
        return jsonify({'success': False, 'message': '신고할 이미지 또는 동영상을 첨부해주세요.'}), 400

    import math
    try:
        lat = float(latitude) if latitude else None
        lng = float(longitude) if longitude else None
        if lat is not None and (not math.isfinite(lat) or not -90 <= lat <= 90):
            raise ValueError
        if lng is not None and (not math.isfinite(lng) or not -180 <= lng <= 180):
            raise ValueError
    except (ValueError, TypeError):
        if save_path and os.path.isfile(save_path):
            os.remove(save_path)
        return jsonify({'success': False, 'message': '위치 좌표 형식이 올바르지 않습니다.'}), 400

    if lat is not None and lng is not None and not address:
        address = reverse_geocode(lat, lng)

    # 중복 신고 제한
    if lat is not None and lng is not None:
        yesterday = get_now_kst() - timedelta(hours=24)
        duplicate = Report.query.filter(
            Report.user_id == user_id,
            Report.created_at >= yesterday,
            Report.latitude.isnot(None),
            Report.longitude.isnot(None)
        ).all()
        for r in duplicate:
            if haversine(lat, lng, r.latitude, r.longitude) <= 50:
                if save_path and os.path.isfile(save_path):
                    os.remove(save_path)
                return jsonify({'success': False, 'message': '이미 1일 내 반경 50m 이내에 신고하신 건이 있습니다.'}), 400

    new_report = Report(
        user_id=user_id,
        title=title,
        content=content,
        latitude=lat,
        longitude=lng,
        address=address,
        region_name=normalize_region_name(address),
        file_path=file_path,
        file_type=file_type,
        status='AI 분석중'
    )
    db.session.add(new_report)
    db.session.commit()

    queued = current_app.config.get('AI_AVAILABLE', False) and hasattr(current_app, 'submit_ai_analysis') and current_app.submit_ai_analysis(
        new_report.id, file_path, file_type
    )
    if not queued:
        new_report.status = '관리자 확인중'
        ai_available = current_app.config.get('AI_AVAILABLE', False)
        new_report.reject_reason = (
            'AI 분석 대기열이 가득 차 관리자 수동 확인으로 전환되었습니다.' if ai_available
            else 'AI 모델이 설정되지 않아 관리자 수동 확인으로 전환되었습니다.'
        )
        db.session.commit()
        return jsonify({
            'success': True,
            'message': '신고가 접수되어 관리자 수동 확인으로 전환되었습니다.',
            'report_id': new_report.id,
        }), 202

    return jsonify({'success': True, 'message': '제보가 성공적으로 접수되어 AI 분석을 시작합니다.', 'report_id': new_report.id})


@report_bp.route('/api/report/status/<int:report_id>', methods=['GET'])
def get_report_status(report_id):
    rpt = db.get_or_404(Report, report_id)
    from models import Member
    member = db.session.get(Member, session.get('user_id')) if session.get('user_id') else None
    if not can_access_report(member, rpt):
        return jsonify({'success': False, 'message': '권한이 없습니다.'}), 403
    return jsonify({
        'status': rpt.status,
        'is_analyzing': rpt.status == 'AI 분석중'
    })
