"""명시적으로 확인된 경우에만 데이터베이스 전체를 복사합니다."""

from services.database_copy import copy_database_from_environment


if __name__ == '__main__':
    copy_database_from_environment()
