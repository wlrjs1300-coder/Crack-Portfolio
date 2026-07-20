"""운영 환경에서 검색 가능한 JSON 구조 로그 설정."""

import json
import logging
import os
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
        }
        if record.exc_info:
            payload['exception'] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(app):
    level = getattr(logging, os.getenv('LOG_LEVEL', 'INFO').upper(), logging.INFO)
    formatter = JsonFormatter()
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(stream)
    log_path = os.getenv('LOG_FILE')
    if log_path:
        file_handler = RotatingFileHandler(
            log_path, maxBytes=int(os.getenv('LOG_MAX_BYTES', '10485760')),
            backupCount=int(os.getenv('LOG_BACKUP_COUNT', '5')), encoding='utf-8',
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    app.logger.handlers.clear()
    app.logger.propagate = True
