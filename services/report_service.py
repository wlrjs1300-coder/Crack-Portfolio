import os
import re
import time
import subprocess
from datetime import timedelta
from flask import Blueprint, render_template, session, redirect, url_for, request, jsonify
from werkzeug.utils import secure_filename
from extensions import db
from models import Report, AiResult, Member, PointLog
from utils import allowed_file, extract_gps_from_exif, haversine, reverse_geocode, get_now_kst
from services.region_service import normalize_region_name

report_bp = Blueprint('report', __name__)

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
    new_save_path = os.path.join(os.getcwd(), video_dir, new_filename)
    try:
        import imageio_ffmpeg
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        subprocess.run(
            [ffmpeg_exe, '-i', save_path, '-vcodec', 'libx264', '-acodec', 'aac', '-y', new_save_path],
            check=True, capture_output=True
        )
        os.remove(save_path)
        print(f"[VIDEO] Converted to MP4: {new_filename}")
        return new_save_path, f'/uploads/videos/{new_filename}'
    except Exception as e:
        print(f"[VIDEO] MP4 변환 실패, 원본 유지: {e}")
        return save_path, f'/uploads/videos/{filename}'


def extract_gps_from_video(video_path, original_filename=None):
    # 1단계: 바이너리 파싱
    try:
        with open(video_path, 'rb') as f:
            raw = f.read()

        # ©xyz 방식 (삼성/아이폰)
        idx = raw.find(b'\xa9xyz')
        if idx != -1:
            context = raw[idx:idx + 50].decode('utf-8', errors='ignore')
            match = re.search(r'([+-]\d{1,3}\.\d+)([+-]\d{1,3}\.\d+)', context)
            if match:
                lat_c, lng_c = float(match.group(1)), float(match.group(2))
                if 33.0 <= lat_c <= 38.5 and 124.0 <= lng_c <= 132.0:
                    print(f"[VIDEO GPS] Stage 1 (©xyz) success: {lat_c}, {lng_c}")
                    return lat_c, lng_c

        # 바이너리 전체 텍스트에서 좌표 패턴 탐색 (블랙박스 커스텀 박스 대응)
        text = raw.decode('utf-8', errors='ignore')
        match = re.search(r'([+-]?\d{2,3}\.\d{5,})[^\d]+([+-]?\d{2,3}\.\d{5,})', text)
        if match:
            lat_c, lng_c = float(match.group(1)), float(match.group(2))
            if 33.0 <= lat_c <= 38.5 and 124.0 <= lng_c <= 132.0:
                print(f"[VIDEO GPS] Stage 1 (binary scan) success: {lat_c}, {lng_c}")
                return lat_c, lng_c

        print("[VIDEO GPS] Stage 1 failed")
    except Exception as e:
        print(f"[VIDEO GPS] Stage 1 error: {e}")

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
        print("[VIDEO GPS] Stage 2 failed")
    except Exception as e:
        print(f"[VIDEO GPS] Stage 2 error: {e}")

    # 3단계: OCR
    try:
        import ssl
        ssl._create_default_https_context = ssl._create_unverified_context
        import cv2, easyocr
        cap = cv2.VideoCapture(video_path)
        fps = cap.get(cv2.CAP_PROP_FPS) or 30
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        reader = easyocr.Reader(['ko', 'en'], gpu=False, verbose=False)
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
        print("[VIDEO GPS] Stage 3 failed")
    except Exception as e:
        print(f"[VIDEO GPS] Stage 3 error: {e}")

    return None, None


@report_bp.route('/report', methods=['GET'])
def report_page():
    if not session.get('user_id'):
        return redirect(url_for('auth.login'))
    return render_template('report.html')


@report_bp.route('/api/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'success': False, 'message': '파일이 없습니다.'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'message': '선택된 파일이 없습니다.'}), 400

    original_name = file.filename
    print(f"[UPLOAD] Original filename: '{original_name}'")

    import uuid
    original_ext = ''
    if '.' in original_name:
        original_ext = original_name.rsplit('.', 1)[1].lower()

    safe_name = secure_filename(original_name)
    if not safe_name or '.' not in safe_name:
        safe_name = f"{uuid.uuid4().hex[:12]}.{original_ext}" if original_ext else safe_name

    filename = f"{int(time.time())}_{safe_name}"
    print(f"[UPLOAD] Safe filename: '{filename}', Extension: '{original_ext}'")

    if allowed_file(filename, ALLOWED_IMAGE_EXTENSIONS):
        save_path = os.path.join(os.getcwd(), UPLOAD_IMAGE_DIR, filename)
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        file.save(save_path)
        print(f"[UPLOAD] Image saved to: {save_path} (size: {os.path.getsize(save_path)} bytes)")

        lat, lng = extract_gps_from_exif(save_path)
        print(f"[UPLOAD] GPS extraction result: lat={lat}, lng={lng}")

        import math
        if lat is not None and (math.isnan(lat) or math.isinf(lat)):
            lat = None
        if lng is not None and (math.isnan(lng) or math.isinf(lng)):
            lng = None

        address = None
        if lat and lng:
            address = reverse_geocode(lat, lng)
            print(f"[UPLOAD] Reverse geocoded address: {address}")

        return jsonify({
            'success': True,
            'message': '이미지 업로드 성공 (GPS 추출 시도)',
            'path': f'/uploads/images/{filename}',
            'gps': {'lat': lat, 'lng': lng} if lat and lng else None,
            'address': address
        })

    elif allowed_file(filename, ALLOWED_VIDEO_EXTENSIONS):
        save_path = os.path.join(os.getcwd(), UPLOAD_VIDEO_DIR, filename)
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        file.save(save_path)

        # MOV 등 → MP4 변환
        save_path, video_path = convert_to_mp4(save_path, UPLOAD_VIDEO_DIR, filename)

        vid_lat, vid_lng = extract_gps_from_video(save_path, original_name)
        address = None
        if vid_lat and vid_lng:
            address = reverse_geocode(vid_lat, vid_lng)

        return jsonify({
            'success': True,
            'message': '동영상 업로드 성공',
            'path': video_path,
            'gps': {'lat': vid_lat, 'lng': vid_lng} if vid_lat and vid_lng else None,
            'address': address
        })

    else:
        print(f"[UPLOAD] REJECTED: filename='{filename}', ext='{original_ext}' not in allowed list")
        return jsonify({'success': False, 'message': f'허용되지 않는 파일 형식입니다. (감지된 확장자: {original_ext})'}), 400


@report_bp.route('/api/report', methods=['POST'])
def submit_report():
    if not session.get('user_id'):
        return jsonify({'success': False, 'message': '제보를 위해 로그인이 필요합니다.'}), 401

    user_id = session.get('user_id')
    title = request.form.get('title', '')[:30]
    content = request.form.get('content')
    latitude = request.form.get('latitude')
    longitude = request.form.get('longitude')
    address = request.form.get('address')

    file_path = None
    file_type = None

    # 영상 이중 업로드 방지 - 이미 /api/upload에서 저장된 path 재사용
    pre_uploaded_path = request.form.get('pre_uploaded_path')
    if pre_uploaded_path:
        file_path = pre_uploaded_path
        file_type = 'video'

    elif 'file' in request.files and request.files['file'].filename != '':
        file = request.files['file']
        import uuid
        original_name = file.filename
        original_ext = ''
        if '.' in original_name:
            original_ext = original_name.rsplit('.', 1)[1].lower()
        safe_name = secure_filename(original_name)
        if not safe_name or '.' not in safe_name:
            safe_name = f"{uuid.uuid4().hex[:12]}.{original_ext}" if original_ext else safe_name
        filename = f"{int(time.time())}_{safe_name}"

        if allowed_file(filename, ALLOWED_IMAGE_EXTENSIONS):
            save_path = os.path.join(os.getcwd(), UPLOAD_IMAGE_DIR, filename)
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            file.save(save_path)

            front_has_gps = bool(latitude and longitude)
            if not front_has_gps:
                print(f"[SUBMIT] Frontend didn't provide GPS. Attempting server-side extraction...")
                exif_lat, exif_lng = extract_gps_from_exif(save_path)
                if exif_lat and exif_lng:
                    latitude = exif_lat
                    longitude = exif_lng
                    print(f"[SUBMIT] ✅ Server-side GPS extraction succeeded: lat={latitude}, lng={longitude}")
                else:
                    print(f"[SUBMIT] ❌ Server-side GPS extraction also failed")
            else:
                print(f"[SUBMIT] ✅ Using GPS from frontend: lat={latitude}, lng={longitude}")

            try:
                from PIL import Image
                import pillow_heif
                pillow_heif.register_heif_opener()

                image = Image.open(save_path)
                file_ext = filename.rsplit('.', 1)[1].lower()

                if file_ext in ['heic', 'heif']:
                    new_filename = filename.rsplit('.', 1)[0] + ".jpg"
                    new_save_path = os.path.join(os.getcwd(), UPLOAD_IMAGE_DIR, new_filename)
                    if image.mode in ("RGBA", "P"):
                        image = image.convert("RGB")
                    image.save(new_save_path, "JPEG", quality=85)
                    os.remove(save_path)
                    file_path = f'/uploads/images/{new_filename}'
                else:
                    if image.mode in ("RGBA", "P"):
                        image = image.convert("RGB")
                    image.save(save_path, "JPEG", quality=85)
                    file_path = f'/uploads/images/{filename}'

            except Exception as e:
                print(f"Image processing (EXIF Strip) Error: {e}")
                file_path = f'/uploads/images/{filename}'

            file_type = 'image'

        elif allowed_file(filename, ALLOWED_VIDEO_EXTENSIONS):
            save_path = os.path.join(os.getcwd(), UPLOAD_VIDEO_DIR, filename)
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            file.save(save_path)

            # MOV 등 → MP4 변환
            save_path, file_path = convert_to_mp4(save_path, UPLOAD_VIDEO_DIR, filename)
            file_type = 'video'

            front_has_gps = bool(latitude and longitude)
            if not front_has_gps:
                vid_lat, vid_lng = extract_gps_from_video(save_path, original_name)
                if vid_lat and vid_lng:
                    latitude, longitude = vid_lat, vid_lng
                    print(f"[SUBMIT] ✅ Video GPS: {latitude}, {longitude}")
                else:
                    print("[SUBMIT] ❌ Video GPS all stages failed.")
        else:
            return jsonify({'success': False, 'message': '이미지 또는 영상 형식이 올바르지 않습니다.'}), 400

    import math
    try:
        lat = float(latitude) if latitude else None
        lng = float(longitude) if longitude else None
        if lat is not None and math.isnan(lat): lat = None
        if lng is not None and math.isnan(lng): lng = None
    except (ValueError, TypeError):
        lat, lng = None, None

    if lat and lng and not address:
        address = reverse_geocode(lat, lng)

    # 중복 신고 제한
    if lat and lng:
        yesterday = get_now_kst() - timedelta(hours=24)
        duplicate = Report.query.filter(
            Report.user_id == user_id,
            Report.created_at >= yesterday,
            Report.latitude.isnot(None),
            Report.longitude.isnot(None)
        ).all()
        for r in duplicate:
            if haversine(lat, lng, r.latitude, r.longitude) <= 50:
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

    from flask import current_app
    if hasattr(current_app, 'run_ai_analysis'):
        import threading
        thread = threading.Thread(target=current_app.run_ai_analysis, args=(new_report.id, file_path, file_type))
        thread.start()

    return jsonify({'success': True, 'message': '제보가 성공적으로 접수되어 AI 분석을 시작합니다.', 'report_id': new_report.id})


@report_bp.route('/api/report/status/<int:report_id>', methods=['GET'])
def get_report_status(report_id):
    rpt = Report.query.get_or_404(report_id)
    return jsonify({
        'status': rpt.status,
        'is_analyzing': rpt.status == 'AI 분석중'
    })