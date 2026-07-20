"""복원용 DB 복사. 실행 전 환경변수로 원본/대상을 명시해야 합니다."""

from services.database_copy import copy_database_from_environment


if __name__ == '__main__':
    copy_database_from_environment()
