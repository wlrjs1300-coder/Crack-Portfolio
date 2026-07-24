"""add member address and coordinates

Revision ID: c35a1d8e72f4
Revises: b4f2187c6a90
Create Date: 2026-07-23

"""
from alembic import op
import sqlalchemy as sa


revision = 'c35a1d8e72f4'
down_revision = 'b4f2187c6a90'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('members', sa.Column('address', sa.String(length=255), nullable=True))
    op.add_column('members', sa.Column('latitude', sa.Float(), nullable=True))
    op.add_column('members', sa.Column('longitude', sa.Float(), nullable=True))


def downgrade():
    with op.batch_alter_table('members') as batch_op:
        batch_op.drop_column('longitude')
        batch_op.drop_column('latitude')
        batch_op.drop_column('address')
