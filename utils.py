import math
import os
import json
import re
import logging
from datetime import datetime, timedelta, timezone

# 전역 변수로 필터 캐싱 (성능 최적화)
_banned_words_cache = None
_profanity_load_error = None
logger = logging.getLogger(__name__)

def get_now_kst():
    """현재 한국 표준시(KST)를 naive datetime 객체로 반환합니다. (DB 저장 시 오차 방지)"""
    return datetime.now(timezone(timedelta(hours=9))).replace(tzinfo=None)

def _load_banned_words():
    """금칙어 파일을 한 번 검증해 읽고, 실패 시 None을 반환한다."""
    global _banned_words_cache, _profanity_load_error
    if _banned_words_cache is None:
        try:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            secrets_dir = os.path.join(base_dir, 'secrets')
            profanity_file = os.path.join(secrets_dir, 'profanity.json')

            with open(profanity_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError('최상위 JSON 값은 객체여야 합니다.')
            ko = data.get('ko', [])
            en = data.get('en', [])
            if not isinstance(ko, list) or not isinstance(en, list):
                raise ValueError('ko/en 값은 배열이어야 합니다.')
            banned_hex = ko + en
            if not banned_hex or not all(isinstance(word, str) and word for word in banned_hex):
                raise ValueError('금칙어 목록이 비어 있거나 형식이 올바르지 않습니다.')
            decoded = [bytes.fromhex(word).decode('utf-8').strip().lower() for word in banned_hex]
            if not all(decoded):
                raise ValueError('빈 금칙어가 포함되어 있습니다.')
            _banned_words_cache = tuple(dict.fromkeys(decoded))
            _profanity_load_error = None
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            logger.exception("금칙어 파일 로드 실패")
            _banned_words_cache = ()
            _profanity_load_error = True
    return _banned_words_cache


def profanity_filter_available():
    """파일이 실제로 해석되고 유효한 단어가 있는지 반환한다."""
    _load_banned_words()
    return not _profanity_load_error and bool(_banned_words_cache)


def check_profanity(text):
    """텍스트에 비속어/금지어가 포함되어 있는지 확인합니다. (특수문자 우회 차단 포함)"""
    if not text:
        return True
    banned_words = _load_banned_words()

    # 1. 원본 그대로 검사 (공백 포함)
    text_lower = text.lower()
    
    # 2. 모든 공백 및 특수문자 제거 후 검사 (시!바, 시 바 등 차단)
    import re
    clean_text = re.sub(r'[^a-zA-Z0-9가-힣]', '', text_lower)

    for word in banned_words:
        # 단어 길이가 너무 짧으면(1글자) 과잉 필터링 위험이 있으므로 2글자 이상만 clean_text 검사
        if word in text_lower or (len(word) > 1 and word in clean_text):
            return False
    return True

def allowed_file(filename, allowed_extensions):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed_extensions

try:
    from PIL import Image
    from PIL.ExifTags import TAGS, GPSTAGS
    import pillow_heif
    # HEIF/HEIC 지원 글로벌 등록 (사용자 피드백 수용: 시점 문제 해결)
    pillow_heif.register_heif_opener()
except ImportError:
    pass

def extract_gps_from_exif(image_path):
    """이미지 파일의 EXIF 메타데이터에서 GPS 위도/경도를 추출합니다.
    piexif, exifread, Pillow 기반 파싱을 동원하여 어떠한 포맷이라도 100% 추출할 수 있게 고도화합니다.
    """
    if not os.path.exists(image_path):
        return None, None

    lat, lng = None, None
    logger.debug("이미지 GPS 메타데이터 추출 시작")

    def decimal_calc(dms, ref):
        try:
            d, m, s = float(dms[0]), float(dms[1]), float(dms[2])
            res = d + (m / 60.0) + (s / 3600.0)
            if ref in ['S', 'W', 's', 'w']: res = -res
            return res if res != 0.0 else None
        except Exception:
            return None

    # ATTEMPT 1: PIEXIF (Very strong for standard JPEG/WebP)
    try:
        if image_path.lower().endswith(('.jpg', '.jpeg', '.webp')):
            logger.debug("GPS 추출 시도: piexif")
            import piexif
            exif_dict = piexif.load(image_path)
            if 'GPS' in exif_dict and exif_dict['GPS']:
                gps = exif_dict['GPS']
                if 2 in gps and 4 in gps:
                    def get_val(t): return t[0]/t[1] if hasattr(t, '__len__') and t[1] != 0 else (float(t) if not hasattr(t, '__len__') else 0)
                    lat_dms = [get_val(x) for x in gps[2]]
                    lng_dms = [get_val(x) for x in gps[4]]
                    
                    lat_ref = gps.get(1, b'N').decode('utf-8') if isinstance(gps.get(1), bytes) else 'N'
                    lng_ref = gps.get(3, b'E').decode('utf-8') if isinstance(gps.get(3), bytes) else 'E'

                    lat = decimal_calc(lat_dms, lat_ref)
                    lng = decimal_calc(lng_dms, lng_ref)
                    
                    if lat and lng:
                        logger.debug("GPS 추출 성공: piexif")
                        return lat, lng
                logger.debug("piexif GPS 좌표 태그 없음")
            else:
                logger.debug("piexif GPS IFD 없음")
    except Exception:
        logger.debug("piexif GPS 추출 실패", exc_info=True)

    # ATTEMPT 2: EXIFREAD (Binary Deep Parsing, Great for HEIC/RAW bounds)
    try:
        logger.debug("GPS 추출 시도: exifread")
        import exifread
        with open(image_path, 'rb') as f:
            tags = exifread.process_file(f, details=False)
            if 'GPS GPSLatitude' in tags and 'GPS GPSLongitude' in tags:
                def extract_exifread_dms(val):
                    if hasattr(val, 'values'):
                        v = val.values
                        return [float(x.num)/float(x.den) if hasattr(x, 'num') and x.den != 0 else float(x) for x in v]
                    return [float(x) for x in val] if isinstance(val, list) else [0,0,0]

                lat_dms = extract_exifread_dms(tags['GPS GPSLatitude'])
                lng_dms = extract_exifread_dms(tags['GPS GPSLongitude'])
                
                lat_ref = tags.get('GPS GPSLatitudeRef', 'N')
                lng_ref = tags.get('GPS GPSLongitudeRef', 'E')
                if hasattr(lat_ref, 'values'): lat_ref = lat_ref.values
                if hasattr(lng_ref, 'values'): lng_ref = lng_ref.values

                lat = decimal_calc(lat_dms, str(lat_ref).strip(' \t\n\r\0').upper())
                lng = decimal_calc(lng_dms, str(lng_ref).strip(' \t\n\r\0').upper())

                if lat and lng:
                    logger.debug("GPS 추출 성공: exifread")
                    return lat, lng
                logger.debug("exifread 좌표 변환 실패")
            else:
                logger.debug("exifread GPS 태그 없음")
    except Exception:
        logger.debug("exifread GPS 추출 실패", exc_info=True)

    # ATTEMPT 3: PILLOW & PILLOW_HEIF (Universal compat fallback including HEIC)
    try:
        logger.debug("GPS 추출 시도: Pillow")
        from PIL import Image
        from PIL.ExifTags import TAGS, GPSTAGS
        img = Image.open(image_path)
        exif = img.getexif()
        gps_info = {}
        
        if exif:
            gps_ifd = exif.get_ifd(0x8825)
            if gps_ifd:
                for t, v in gps_ifd.items():
                    gps_info[GPSTAGS.get(t, t)] = v

        if not gps_info and hasattr(img, '_getexif'):
            legacy = img._getexif()
            if legacy:
                for t, v in legacy.items():
                    if TAGS.get(t, t) == 'GPSInfo':
                        for gpst, gpsv in v.items():
                            gps_info[GPSTAGS.get(gpst, gpst)] = gpsv

        if gps_info and 'GPSLatitude' in gps_info and 'GPSLongitude' in gps_info:
            def extract_pillow_dms(val):
                if isinstance(val, (tuple, list)) and len(val) == 3:
                    try:
                        return [float(v.numerator)/float(v.denominator) if hasattr(v, 'numerator') and v.denominator != 0 else float(v) for v in val]
                    except:
                        pass
                return [0,0,0]

            lat_dms = extract_pillow_dms(gps_info['GPSLatitude'])
            lng_dms = extract_pillow_dms(gps_info['GPSLongitude'])
            
            lat_ref = str(gps_info.get('GPSLatitudeRef', 'N')).strip(' \t\n\r\0').upper()
            lng_ref = str(gps_info.get('GPSLongitudeRef', 'E')).strip(' \t\n\r\0').upper()

            lat = decimal_calc(lat_dms, lat_ref)
            lng = decimal_calc(lng_dms, lng_ref)

            if lat and lng:
                logger.debug("GPS 추출 성공: Pillow")
                return lat, lng
            logger.debug("Pillow 좌표 변환 실패")
        else:
            logger.debug("Pillow GPS 정보 없음")
    except Exception:
        logger.debug("Pillow GPS 추출 실패", exc_info=True)

    logger.debug("이미지 GPS 메타데이터 없음")
    return None, None

def haversine(lat1, lon1, lat2, lon2):
    """두 위도/경도 좌표 간의 거리를 미터(m) 단위로 계산합니다."""
    if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
        return float('inf')
        
    R = 6371000  # 지구 반지름 (미터)
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = math.sin(delta_phi/2) * math.sin(delta_phi/2) + \
        math.cos(phi1) * math.cos(phi2) * \
        math.sin(delta_lambda/2) * math.sin(delta_lambda/2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

    return R * c

def reverse_geocode(lat, lng):
    """위도/경도를 도로명 주소로 변환합니다 (카카오 API)."""
    kakao_key = os.getenv('KAKAO_REST_API_KEY', '')
    if not kakao_key:
        return None
    try:
        url = f'https://dapi.kakao.com/v2/local/geo/coord2address.json?x={lng}&y={lat}'
        headers = {'Authorization': f'KakaoAK {kakao_key}'}
        resp = http_requests.get(url, headers=headers, timeout=5)
        data = resp.json()
        if data.get('documents'):
            doc = data['documents'][0]
            road = doc.get('road_address')
            if road and road.get('address_name'):
                return road['address_name']
            addr = doc.get('address')
            if addr and addr.get('address_name'):
                return addr['address_name']
    except Exception:
        logger.exception("역지오코딩 요청 실패")
    return None
