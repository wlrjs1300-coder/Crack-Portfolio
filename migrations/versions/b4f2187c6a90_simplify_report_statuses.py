"""simplify report statuses

Revision ID: b4f2187c6a90
Revises: 9286228b6520
Create Date: 2026-07-23

"""
from alembic import op


revision = 'b4f2187c6a90'
down_revision = '9286228b6520'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "UPDATE report SET status = '접수완료', reject_reason = NULL "
        "WHERE status IN ('AI 분석중', '관리자 확인중', '담당자 확인중')"
    )
    op.execute("UPDATE report SET status = '삭제' WHERE status = '반려'")
    op.execute("UPDATE report SET status = '처리중' WHERE status = '신고 처리중'")
    op.execute("UPDATE report SET status = '처리완료' WHERE status = '처리 완료'")


def downgrade():
    # 단순화 전 상태는 데이터만으로 복원할 수 없으므로 현재 상태를 유지한다.
    pass
