"""add UPI scan order assignment tracking"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'g1b2c3d4e5f6'
down_revision: Union[str, None] = 'd7e8f9a0b1c2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'upi_scan_orders',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('bought_id', sa.Integer(), sa.ForeignKey('bought_goods.id', ondelete='CASCADE'), nullable=False, unique=True),
        sa.Column('buyer_id', sa.BigInteger(), sa.ForeignKey('users.telegram_id', ondelete='SET NULL'), nullable=True),
        sa.Column('link', sa.Text(), nullable=False),
        sa.Column('status', sa.String(16), nullable=False, server_default='pending'),
        sa.Column('claimed_by', sa.BigInteger(), sa.ForeignKey('users.telegram_id', ondelete='SET NULL'), nullable=True),
        sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_upi_scan_orders_status', 'upi_scan_orders', ['status'])
    op.create_index('ix_upi_scan_orders_buyer_id', 'upi_scan_orders', ['buyer_id'])
    op.create_index('ix_upi_scan_orders_claimed_by', 'upi_scan_orders', ['claimed_by'])


def downgrade() -> None:
    op.drop_table('upi_scan_orders')