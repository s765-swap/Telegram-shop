from aiogram import F, Router
from aiogram.types import CallbackQuery

from bot.database.methods import claim_upi_scan_order, complete_upi_scan_order, upi_scan_admin_stats
from bot.database.methods.audit import log_audit
from bot.database.methods.read import check_role_cached
from bot.database.models import Permission
from bot.handlers.other import display_name
from bot.i18n import localize
from bot.keyboards.inline import simple_buttons

router = Router()


def _work_keyboard(order_id: int):
    return simple_buttons([("Complete order", f"upi_complete:{order_id}")])


@router.callback_query(F.data.startswith('upi_claim:'))
async def claim_upi_order_handler(call: CallbackQuery):
    try:
        order_id = int(call.data.split(':', 1)[1])
    except (ValueError, IndexError):
        await call.answer(localize('errors.invalid_data'), show_alert=True)
        return

    success, code, _order = await claim_upi_scan_order(order_id, call.from_user.id)
    if not success:
        await call.answer({
            'not_admin': 'Only owner-approved admins can claim orders.',
            'not_found': 'Order not found.',
            'already_claimed': 'This order has already been claimed by another admin.',
        }.get(code, 'Unable to claim order.'), show_alert=True)
        return

    await call.message.edit_reply_markup(reply_markup=_work_keyboard(order_id))
    await call.answer('Order assigned to you.')
    await log_audit('upi_order_claimed', user_id=call.from_user.id,
                    resource_type='UpiScanOrder', resource_id=str(order_id))


@router.callback_query(F.data.startswith('upi_complete:'))
async def complete_upi_order_handler(call: CallbackQuery):
    try:
        order_id = int(call.data.split(':', 1)[1])
    except (ValueError, IndexError):
        await call.answer(localize('errors.invalid_data'), show_alert=True)
        return

    success, code, _order = await complete_upi_scan_order(order_id, call.from_user.id)
    if not success:
        await call.answer({
            'not_claimed_by_you': 'Only the assigned admin can complete this order.',
            'already_completed': 'This order is already completed.',
            'not_found': 'Order not found.',
        }.get(code, 'Unable to complete order.'), show_alert=True)
        return

    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer('Order completed.')
    await log_audit('upi_order_completed', user_id=call.from_user.id,
                    resource_type='UpiScanOrder', resource_id=str(order_id))


@router.callback_query(F.data == 'upi_stats')
async def upi_stats_handler(call: CallbackQuery):
    permissions = await check_role_cached(call.from_user.id) or 0
    if not (permissions & Permission.OWN):
        await call.answer(localize('middleware.security.not_admin'), show_alert=True)
        return

    rows = await upi_scan_admin_stats()
    lines = ['UPI scan admin work:']
    for row in rows:
        name = await display_name(call.bot, row['telegram_id'])
        lines.append(f"{name} ({row['telegram_id']}): claimed {row['claimed']}, completed {row['completed']}")
    await call.message.edit_text('\n'.join(lines) if rows else 'No UPI scan work recorded.',
                                reply_markup=simple_buttons([('Back', 'shop_management')]))