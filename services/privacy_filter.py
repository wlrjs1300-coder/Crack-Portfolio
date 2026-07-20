"""업로드 미디어의 얼굴과 차량 번호판을 저장 전에 흐림 처리한다."""

import os
import subprocess
import uuid
from pathlib import Path

import cv2
import numpy as np
from PIL import Image


class PrivacyFilterError(RuntimeError):
    pass


def _enabled():
    return os.getenv('PRIVACY_BLUR_ENABLED', 'true').lower() in ('1', 'true', 'yes')


def _detectors():
    base = Path(cv2.data.haarcascades)
    paths = [base / 'haarcascade_frontalface_default.xml', base / 'haarcascade_russian_plate_number.xml']
    detectors = [cv2.CascadeClassifier(str(path)) for path in paths if path.is_file()]
    return [detector for detector in detectors if not detector.empty()]


def _blur_regions(frame, detectors):
    height, width = frame.shape[:2]
    scale = min(1.0, 640.0 / max(width, 1))
    small = cv2.resize(frame, None, fx=scale, fy=scale) if scale < 1 else frame
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    for detector in detectors:
        for x, y, w, h in detector.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4, minSize=(20, 20)):
            x1, y1 = int(x / scale), int(y / scale)
            x2, y2 = min(width, int((x + w) / scale)), min(height, int((y + h) / scale))
            roi = frame[y1:y2, x1:x2]
            if roi.size:
                kernel = max(15, (min(roi.shape[:2]) // 3) | 1)
                frame[y1:y2, x1:x2] = cv2.GaussianBlur(roi, (kernel, kernel), 0)
    return frame


def blur_image_in_place(path):
    if not _enabled():
        return path
    detectors = _detectors()
    if not detectors:
        raise PrivacyFilterError('개인정보 마스킹 검출기를 불러올 수 없습니다.')
    source = Path(path)
    temporary = source.with_name(f'{source.stem}.{uuid.uuid4().hex}.privacy.jpg')
    try:
        with Image.open(source) as image:
            if getattr(image, 'is_animated', False):
                image.seek(0)
            rgb = np.asarray(image.convert('RGB'))
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        bgr = _blur_regions(bgr, detectors)
        Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)).save(temporary, 'JPEG', quality=90)
        source.unlink(missing_ok=True)
        temporary.replace(source)
        return str(source)
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        if isinstance(exc, PrivacyFilterError):
            raise
        raise PrivacyFilterError('이미지 개인정보 마스킹에 실패했습니다.') from exc


def blur_video_in_place(path):
    if not _enabled():
        return path
    detectors = _detectors()
    if not detectors:
        raise PrivacyFilterError('개인정보 마스킹 검출기를 불러올 수 없습니다.')
    source = Path(path)
    raw_output = source.with_name(f'{source.stem}.{uuid.uuid4().hex}.privacy-raw.mp4')
    final_output = source.with_name(f'{source.stem}.{uuid.uuid4().hex}.privacy.mp4')
    target = source.with_suffix('.mp4')
    cap = cv2.VideoCapture(str(source))
    writer = None
    try:
        if not cap.isOpened():
            raise PrivacyFilterError('마스킹할 동영상을 열 수 없습니다.')
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        if fps <= 0 or width <= 0 or height <= 0:
            raise PrivacyFilterError('동영상 메타데이터를 확인할 수 없습니다.')
        writer = cv2.VideoWriter(str(raw_output), cv2.VideoWriter_fourcc(*'mp4v'), fps, (width, height))
        if not writer.isOpened():
            raise PrivacyFilterError('마스킹 동영상 출력 파일을 만들 수 없습니다.')
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            writer.write(_blur_regions(frame, detectors))
        cap.release()
        writer.release()
        cap = None
        writer = None

        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        subprocess.run([
            ffmpeg, '-i', str(raw_output), '-i', str(source),
            '-map', '0:v:0', '-map', '1:a?', '-c:v', 'libx264', '-preset', 'veryfast',
            '-c:a', 'aac', '-movflags', '+faststart', '-shortest', '-y', str(final_output),
        ], check=True, capture_output=True, timeout=240)
        source.unlink(missing_ok=True)
        if target != source:
            target.unlink(missing_ok=True)
        final_output.replace(target)
        return str(target)
    except Exception as exc:
        if isinstance(exc, PrivacyFilterError):
            raise
        raise PrivacyFilterError('동영상 개인정보 마스킹에 실패했습니다.') from exc
    finally:
        if cap is not None:
            cap.release()
        if writer is not None:
            writer.release()
        raw_output.unlink(missing_ok=True)
        final_output.unlink(missing_ok=True)
