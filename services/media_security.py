"""업로드 파일의 이름, 실제 형식, 크기와 영상 메타데이터 검증."""

import os
import time
import uuid
from pathlib import Path

import cv2
from flask import current_app, session
from PIL import Image


IMAGE_EXTENSIONS = {'jpg', 'jpeg', 'png', 'gif', 'heic', 'heif'}
VIDEO_EXTENSIONS = {'mp4', 'mov', 'avi', 'm4v'}
MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_VIDEO_BYTES = 100 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_VIDEO_SECONDS = 90
MAX_VIDEO_PIXELS = 3840 * 2160
PENDING_UPLOAD_TTL = 3600


class MediaValidationError(ValueError):
    pass


def extension_of(filename):
    return Path(filename or '').suffix.lower().lstrip('.')


def unique_filename(original_name):
    ext = extension_of(original_name)
    return f'{uuid.uuid4().hex}.{ext}'


def _read_header(path, size=32):
    with open(path, 'rb') as file_obj:
        return file_obj.read(size)


def _has_expected_signature(path, ext):
    header = _read_header(path)
    if ext in ('jpg', 'jpeg'):
        return header.startswith(b'\xff\xd8\xff')
    if ext == 'png':
        return header.startswith(b'\x89PNG\r\n\x1a\n')
    if ext == 'gif':
        return header.startswith((b'GIF87a', b'GIF89a'))
    if ext in ('heic', 'heif'):
        return len(header) >= 12 and header[4:8] == b'ftyp' and header[8:12] in {
            b'heic', b'heix', b'hevc', b'hevx', b'mif1', b'msf1'
        }
    if ext in ('mp4', 'mov', 'm4v'):
        return len(header) >= 12 and header[4:8] == b'ftyp'
    if ext == 'avi':
        return header.startswith(b'RIFF') and header[8:12] == b'AVI '
    return False


def validate_saved_media(path, ext):
    ext = ext.lower()
    size = os.path.getsize(path)
    if ext in IMAGE_EXTENSIONS:
        if size <= 0 or size > MAX_IMAGE_BYTES:
            raise MediaValidationError('이미지는 최대 25MB까지 업로드할 수 있습니다.')
        if not _has_expected_signature(path, ext):
            raise MediaValidationError('이미지의 실제 형식이 확장자와 일치하지 않습니다.')
        try:
            if ext in ('heic', 'heif'):
                import pillow_heif
                pillow_heif.register_heif_opener()
            with Image.open(path) as image:
                width, height = image.size
                image.verify()
            if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                raise MediaValidationError('이미지 해상도가 허용 범위를 초과했습니다.')
        except MediaValidationError:
            raise
        except Exception as exc:
            raise MediaValidationError('손상되었거나 지원하지 않는 이미지입니다.') from exc
        return 'image'

    if ext in VIDEO_EXTENSIONS:
        if size <= 0 or size > MAX_VIDEO_BYTES:
            raise MediaValidationError('영상은 최대 100MB까지 업로드할 수 있습니다.')
        if not _has_expected_signature(path, ext):
            raise MediaValidationError('영상의 실제 형식이 확장자와 일치하지 않습니다.')
        cap = cv2.VideoCapture(path)
        try:
            if not cap.isOpened():
                raise MediaValidationError('손상되었거나 지원하지 않는 영상입니다.')
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
            frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            duration = frames / fps if fps > 0 else 0
            if fps <= 0 or frames <= 0 or width <= 0 or height <= 0:
                raise MediaValidationError('영상 메타데이터를 확인할 수 없습니다.')
            if duration > MAX_VIDEO_SECONDS:
                raise MediaValidationError(f'영상 길이는 최대 {MAX_VIDEO_SECONDS}초까지 허용됩니다.')
            if width * height > MAX_VIDEO_PIXELS:
                raise MediaValidationError('영상 해상도는 최대 4K까지 허용됩니다.')
        finally:
            cap.release()
        return 'video'

    raise MediaValidationError('허용되지 않는 파일 확장자입니다.')


def save_and_validate(file_storage, media_kind=None):
    ext = extension_of(file_storage.filename)
    allowed = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
    if ext not in allowed:
        raise MediaValidationError('허용되지 않는 파일 확장자입니다.')
    if media_kind == 'image' and ext not in IMAGE_EXTENSIONS:
        raise MediaValidationError('이미지 파일만 허용됩니다.')
    if media_kind == 'video' and ext not in VIDEO_EXTENSIONS:
        raise MediaValidationError('영상 파일만 허용됩니다.')

    kind = 'image' if ext in IMAGE_EXTENSIONS else 'video'
    upload_dir = Path(current_app.root_path) / 'uploads' / ('images' if kind == 'image' else 'videos')
    upload_dir.mkdir(parents=True, exist_ok=True)
    filename = unique_filename(file_storage.filename)
    absolute_path = upload_dir / filename
    file_storage.save(absolute_path)
    try:
        validate_saved_media(str(absolute_path), ext)
    except Exception:
        absolute_path.unlink(missing_ok=True)
        raise
    return str(absolute_path), f'/uploads/{upload_dir.name}/{filename}', kind


def sanitize_image_to_jpeg(path):
    """EXIF를 제거하고 브라우저 호환 JPEG로 재인코딩한다."""
    source = Path(path)
    target = source.with_suffix('.jpg')
    temporary = source.with_name(f'{source.stem}.{uuid.uuid4().hex}.sanitized.jpg')
    try:
        if extension_of(source.name) in ('heic', 'heif'):
            import pillow_heif
            pillow_heif.register_heif_opener()
        with Image.open(source) as image:
            if getattr(image, 'is_animated', False):
                image.seek(0)
            image.convert('RGB').save(temporary, 'JPEG', quality=85, optimize=True)
        source.unlink(missing_ok=True)
        if target != source:
            target.unlink(missing_ok=True)
        temporary.replace(target)
        return str(target), f'/uploads/images/{target.name}'
    except Exception:
        temporary.unlink(missing_ok=True)
        raise MediaValidationError('이미지 안전 처리에 실패했습니다.')


def remember_pending_upload(path):
    now = int(time.time())
    entries = [
        item for item in session.get('_pending_uploads', [])
        if isinstance(item, dict) and now - int(item.get('created_at', 0)) <= PENDING_UPLOAD_TTL
    ]
    entries.append({'path': path, 'created_at': now})
    session['_pending_uploads'] = entries[-5:]


def consume_pending_upload(path):
    now = int(time.time())
    normalized = str(path or '').replace('\\', '/')
    entries = session.get('_pending_uploads', [])
    matched = False
    remaining = []
    for item in entries:
        fresh = isinstance(item, dict) and now - int(item.get('created_at', 0)) <= PENDING_UPLOAD_TTL
        if fresh and not matched and item.get('path') == normalized:
            matched = True
            continue
        if fresh:
            remaining.append(item)
    session['_pending_uploads'] = remaining
    if not matched or not normalized.startswith('/uploads/videos/'):
        return None

    relative = normalized.removeprefix('/uploads/').replace('/', os.sep)
    upload_root = os.path.realpath(os.path.join(current_app.root_path, 'uploads'))
    absolute = os.path.realpath(os.path.join(upload_root, relative))
    if os.path.commonpath((upload_root, absolute)) != upload_root or not os.path.isfile(absolute):
        return None
    try:
        validate_saved_media(absolute, extension_of(absolute))
    except MediaValidationError:
        return None
    return normalized
