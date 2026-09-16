from datetime import datetime, timezone

from sqlalchemy import func, select

from bot.database import Database
from bot.database.models import Role, User, UpiScanOrder
from bot.database.models import Permission


async def create_upi_scan_order(bought_id: int, buyer_id: int, link: str) -> int:
    async with Database().session() as session:
        order = UpiScanOrder(bought_id=bought_id, buyer_id=buyer_id, link=link)
        session.add(order)
        await session.flush()
        return order.id


async def eligible_upi_admin_ids() -> list[int]:
    async with Database().session() as session:
        result = await session.execute(
            select(User.telegram_id)
            .join(Role, Role.id == User.role_id)
            .where((Role.permissions.op('&')(~Permission.USE)) != 0, User.is_blocked.is_(False))
        )
        return list(result.scalars().all())


async def claim_upi_scan_order(order_id: int, admin_id: int) -> tuple[bool, str, UpiScanOrder | None]:
    async with Database().session() as session:
        eligible = (await session.execute(
            select(User.telegram_id)
            .join(Role, Role.id == User.role_id)
            .where(
                User.telegram_id == admin_id,
                (Role.permissions.op('&')(~Permission.USE)) != 0,
                User.is_blocked.is_(False),
            )
        )).scalar_one_or_none()
        if eligible is None:
            return False, 'not_admin', None

        order = (await session.execute(
            select(UpiScanOrder).where(UpiScanOrder.id == order_id).with_for_update()
        )).scalars().one_or_none()
        if not order:
            return False, 'not_found', None
        if order.status != 'pending':
            return False, 'already_claimed', order

        order.status = 'claimed'
        order.claimed_by = admin_id
        order.claimed_at = datetime.now(timezone.utc)
        return True, 'claimed', order


async def complete_upi_scan_order(order_id: int, admin_id: int) -> tuple[bool, str, UpiScanOrder | None]:
    async with Database().session() as session:
        order = (await session.execute(
            select(UpiScanOrder).where(UpiScanOrder.id == order_id).with_for_update()
        )).scalars().one_or_none()
        if not order:
            return False, 'not_found', None
        if order.claimed_by != admin_id:
            return False, 'not_claimed_by_you', order
        if order.status == 'completed':
            return False, 'already_completed', order
        if order.status != 'claimed':
            return False, 'not_claimed', order

        order.status = 'completed'
        order.completed_at = datetime.now(timezone.utc)
        return True, 'completed', order


async def upi_scan_admin_stats() -> list[dict]:
    async with Database().session() as session:
        result = await session.execute(
            select(
                User.telegram_id,
                func.count(UpiScanOrder.id).filter(UpiScanOrder.claimed_by == User.telegram_id).label('claimed'),
                func.count(UpiScanOrder.id).filter(
                    (UpiScanOrder.claimed_by == User.telegram_id) & (UpiScanOrder.status == 'completed')
                ).label('completed'),
            )
            .join(Role, Role.id == User.role_id)
            .outerjoin(UpiScanOrder, UpiScanOrder.claimed_by == User.telegram_id)
            .where((Role.permissions.op('&')(~Permission.USE)) != 0)
            .group_by(User.telegram_id)
            .order_by(User.telegram_id)
        )
        return [dict(row) for row in result.mappings().all()]