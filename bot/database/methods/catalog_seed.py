from sqlalchemy import select

from bot.database import Database
from bot.database.methods.cache_utils import safe_create_task
from bot.database.methods.read import invalidate_category_cache, invalidate_item_cache, invalidate_stats_cache
from bot.database.models import Categories, Goods, ItemValues
from bot.misc import EnvKeys


async def seed_upi_scan_catalog() -> None:
    """Create the single unlimited UPI scan service product idempotently."""
    category_name = EnvKeys.UPI_SCAN_CATEGORY
    item_name = "Upi scan"

    async with Database().session() as session:
        category = (await session.execute(
            select(Categories).where(Categories.name == category_name)
        )).scalars().one_or_none()
        if category is None:
            category = Categories(name=category_name)
            session.add(category)
            await session.flush()

        item = (await session.execute(
            select(Goods).where(Goods.name == item_name)
        )).scalars().one_or_none()
        if item is None:
            item = Goods(
                name=item_name,
                description="UPI scan service. After purchase, send your link to the admin.",
                price=EnvKeys.UPI_SCAN_PRICE,
                category_id=category.id,
            )
            session.add(item)
            await session.flush()

        stock = (await session.execute(
            select(ItemValues).where(
                ItemValues.item_id == item.id,
                ItemValues.value == "upi_scan_service",
            )
        )).scalars().one_or_none()
        if stock is None:
            session.add(ItemValues(item_id=item.id, value="upi_scan_service", is_infinity=True))

    safe_create_task(invalidate_stats_cache())
    safe_create_task(invalidate_category_cache(category_name))
    safe_create_task(invalidate_item_cache(item_name))