"""검증·명시적 확인·안전한 식별자 인용을 적용한 MySQL DB 복사 도구."""

import os
import re

import certifi
import pymysql
from dotenv import load_dotenv


IDENTIFIER = re.compile(r'^[A-Za-z0-9_]{1,64}$')


def _identifier(value, name):
    if not value or not IDENTIFIER.fullmatch(value):
        raise ValueError(f'{name}은 영문, 숫자, 밑줄 1~64자여야 합니다.')
    return f'`{value}`'


def copy_database_from_environment():
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), 'secrets', '.env'))
    source_raw = os.getenv('DB_COPY_SOURCE')
    target_raw = os.getenv('DB_COPY_TARGET')
    source = _identifier(source_raw, 'DB_COPY_SOURCE')
    target = _identifier(target_raw, 'DB_COPY_TARGET')
    if source_raw == target_raw:
        raise ValueError('원본과 대상 데이터베이스는 달라야 합니다.')
    expected = f'REPLACE:{target_raw}'
    if os.getenv('CONFIRM_DATABASE_REPLACE') != expected:
        raise RuntimeError(f'실행하려면 CONFIRM_DATABASE_REPLACE={expected}를 명시하세요.')

    required = ('DB_HOST', 'DB_USER', 'DB_PASSWORD')
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(f'필수 DB 설정 누락: {", ".join(missing)}')

    connection = pymysql.connect(
        host=os.environ['DB_HOST'], port=int(os.getenv('DB_PORT', '3306')),
        user=os.environ['DB_USER'], password=os.environ['DB_PASSWORD'],
        ssl=None if os.environ['DB_HOST'] in ('127.0.0.1', 'localhost') else {'ca': certifi.where()},
        charset='utf8mb4', autocommit=False,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(f'CREATE DATABASE IF NOT EXISTS {target} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
            cursor.execute(f'SHOW TABLES FROM {source}')
            tables = [row[0] for row in cursor.fetchall()]
            cursor.execute('SET FOREIGN_KEY_CHECKS=0')
            for table_raw in tables:
                table = _identifier(table_raw, 'table')
                cursor.execute(f'SHOW CREATE TABLE {source}.{table}')
                create_statement = cursor.fetchone()[1]
                cursor.execute(f'USE {target}')
                cursor.execute(f'DROP TABLE IF EXISTS {table}')
                cursor.execute(create_statement)
                cursor.execute(f'INSERT INTO {target}.{table} SELECT * FROM {source}.{table}')
            cursor.execute('SET FOREIGN_KEY_CHECKS=1')
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
