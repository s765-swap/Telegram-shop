from sqlalchemy import select

from bot.database import Database
from bot.database.methods.create import add_values_to_item, create_category, create_item
from bot.database.methods.delete import delete_category
from bot.database.models import Categories
from bot.misc import EnvKeys


async def seed_upi_scan_catalog() -> None:
    """Create the single unlimited UPI scan service product idempotently."""
    category_name = EnvKeys.UPI_SCAN_CATEGORY
    item_name = "Upi scan"

    async with Database().session() as session:
        category_exists = (await session.execute(
            select(Categories.id).where(Categories.name == category_name)
        )).scalar_one_or_none()

    if not category_exists:
        await create_category(category_name)

    await create_item(
        item_name=item_name,
        item_description="UPI scan service. After purchase, send your link to the admin.",
        item_price=EnvKeys.UPI_SCAN_PRICE,
        category_name=category_name,
    )
    await add_values_to_item(item_name, "upi_scan_service", is_infinity=True)


async def reset_and_seed_catalog() -> None:
    """One-time destructive catalog reset, then seed the UPI service product."""
    async with Database().session() as session:
        names = list((await session.execute(select(Categories.name))).scalars().all())
    for name in names:
        await delete_category(name)
    await seed_upi_scan_catalog()