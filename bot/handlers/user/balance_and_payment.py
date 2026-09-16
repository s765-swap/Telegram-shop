import hashlib
import json
from decimal import Decimal, ROUND_HALF_UP

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery, SuccessfulPayment
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from bot.database.methods import (
    get_user_referral, buy_item_transaction, process_payment_with_referral,
    create_pending_payment, create_upi_scan_order, eligible_upi_admin_ids,
)
from bot.keyboards import back, payment_menu, close, get_payment_choice, upi_order_keyboard, manual_deposit_keyboard
from bot.logger_mesh import logger
from bot.database.methods.audit import log_audit
from bot.database.methods.cache_utils import safe_create_task
from bot.misc import EnvKeys, ItemPurchaseRequest, validate_telegram_id, validate_money_amount, PaymentRequest
from bot.misc.validators import UpiScanLinkRequest
from bot.handlers.other import _any_payment_method_enabled, is_safe_item_name, caller_name
from bot.misc.metrics import get_metrics
from bot.misc.services import (
    CryptoPayAPI, CryptoPayAPIError, send_stars_invoice, send_fiat_invoice,
    BinanceAPI, BinanceAPIError,
)
from bot.misc.services.payment import _minor_units_for, payload_amount
from bot.filters import ValidAmountFilter
from bot.i18n import localize, esc
from bot.states import BalanceStates, UpiScanFSM

router = Router()

UPI_SCAN_ITEM_NAME = "Upi scan"


async def _notify_referrer_bonus(bot, user_id: int, amount: Decimal | int, payer_name: str, payer_id: int):
    """Send referral bonus notification to the referrer if applicable."""
    referral_id = await get_user_referral(user_id)
    if not referral_id or not EnvKeys.REFERRAL_PERCENT:
        return
    try:
        clamped_percent = min(max(EnvKeys.REFERRAL_PERCENT, 0), 99)
        bonus = (Decimal(clamped_percent) / Decimal(100) * Decimal(amount)).quantize(Decimal("0.01"))
        if bonus > 0:
            await bot.send_message(
                referral_id,
                localize('payments.referral.bonus',
                         amount=bonus, name=esc(payer_name),
                         id=payer_id, currency=EnvKeys.PAY_CURRENCY),
                reply_markup=close()
            )
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        logger.error(f"Failed to send referral notification to user {referral_id}: {e}")


@router.callback_query(F.data == "replenish_balance")
async def replenish_balance_callback_handler(call: CallbackQuery, state: FSMContext):
    """Collect a manual deposit request; an admin credits the balance after payment."""
    await call.message.edit_text(
        localize("payments.replenish_prompt", currency=EnvKeys.PAY_CURRENCY),
        reply_markup=back('profile')
    )
    await state.set_state(BalanceStates.waiting_amount)


@router.message(BalanceStates.waiting_amount, ValidAmountFilter())
async def replenish_balance_amount(message: Message, state: FSMContext):
    """Notify the manual deposit admin about the requested amount."""
    try:
        # Validate amount using Pydantic
        amount = validate_money_amount(
            message.text,
            min_amount=Decimal(EnvKeys.MIN_AMOUNT),
            max_amount=Decimal(EnvKeys.MAX_AMOUNT)
        )

        user_name = esc(message.from_user.username or message.from_user.first_name or str(message.from_user.id))
        await message.bot.send_message(
            EnvKeys.MANUAL_DEPOSIT_ADMIN_ID,
            (
                "Manual deposit request\n"
                f"User: @{user_name}\n"
                f"Telegram ID: <code>{message.from_user.id}</code>\n"
                f"Requested amount: <b>{amount} {EnvKeys.PAY_CURRENCY}</b>"
            ),
            parse_mode="HTML",
        )
        await message.answer(
            localize(
                "payments.manual.request_sent",
                amount=amount,
                currency=EnvKeys.PAY_CURRENCY,
            ),
            reply_markup=manual_deposit_keyboard(EnvKeys.MANUAL_DEPOSIT_ADMIN_ID),
        )
        await state.clear()

    except (ValueError, TelegramBadRequest, TelegramForbiddenError):
        await message.answer(
            localize("payments.manual.admin_unavailable"),
            reply_markup=back('replenish_balance')
        )


@router.message(BalanceStates.waiting_amount)
async def invalid_amount(message: Message, state: FSMContext):
    """
    Tell user the amount is invalid.
    """
    await message.answer(
        localize("payments.replenish_invalid",
                 min_amount=EnvKeys.MIN_AMOUNT,
                 max_amount=EnvKeys.MAX_AMOUNT,
                 currency=EnvKeys.PAY_CURRENCY),
        reply_markup=back('replenish_balance')
    )


@router.callback_query(
    BalanceStates.waiting_payment,
    F.data.in_(["pay_cryptopay", "pay_stars", "pay_fiat", "pay_binance"])
)
async def process_replenish_balance(call: CallbackQuery, state: FSMContext):
    """Create an invoice for the chosen payment method."""
    data = await state.get_data()
    amount = data.get('amount')

    if amount is None:
        await call.answer(localize("payments.session_expired"), show_alert=True)
        await call.message.edit_text(localize("menu.title"), reply_markup=back('back_to_menu'))
        await state.clear()
        return

    # Map callback data to provider
    provider_map = {
        "pay_cryptopay": "cryptopay",
        "pay_stars": "stars",
        "pay_fiat": "fiat",
        "pay_binance": "binance_usdt",
    }
    provider = provider_map.get(call.data)

    try:
        # Validate payment request
        payment_request = PaymentRequest(
            amount=Decimal(amount),
            currency=EnvKeys.PAY_CURRENCY,
            provider=provider
        )

        amount_dec = payment_request.amount
        ttl_seconds = int(EnvKeys.PAYMENT_TIME)

        if call.data == "pay_cryptopay":
            if not EnvKeys.CRYPTO_PAY_TOKEN:
                await call.answer(localize("payments.not_configured"), show_alert=True)
                return

            try:
                crypto = CryptoPayAPI()
                invoice = await crypto.create_invoice(
                    amount=float(amount_dec),
                    expires_in=ttl_seconds,
                    currency=payment_request.currency,
                    accepted_assets="TON,USDT,BTC,ETH",
                    payload=str(call.from_user.id),
                )
            except CryptoPayAPIError as e:
                await log_audit("cryptopay_error", level="ERROR", user_id=call.from_user.id, resource_type="Payment", details=f"[{e.code}] {e.name}")
                await call.answer(localize("payments.crypto.api_error", error=e.name), show_alert=True)
                return
            except Exception as e:
                await log_audit("cryptopay_invoice_fail", level="ERROR", user_id=call.from_user.id, resource_type="Payment", details=str(e))
                await call.answer(localize("payments.crypto.create_fail", error=str(e)), show_alert=True)
                return

            pay_url = invoice.get("mini_app_invoice_url")
            invoice_id = invoice.get("invoice_id")

            await create_pending_payment(
                provider="cryptopay",
                external_id=str(invoice_id),
                user_id=call.from_user.id,
                amount=int(amount_dec),
                currency=payment_request.currency,
            )

            await state.update_data(invoice_id=invoice_id, payment_type="cryptopay")

            await call.message.edit_text(
                localize("payments.invoice.summary",
                         amount=int(amount_dec),
                         minutes=int(ttl_seconds / 60),
                         button=localize("btn.check_payment"),
                         currency=payment_request.currency),
                reply_markup=payment_menu(pay_url)
            )

        elif call.data == "pay_stars":
            if EnvKeys.STARS_PER_VALUE > 0:
                try:
                    await send_stars_invoice(
                        bot=call.message.bot,
                        chat_id=call.from_user.id,
                        amount=int(amount_dec),
                    )
                except Exception as e:
                    await log_audit("stars_invoice_fail", level="ERROR", user_id=call.from_user.id, resource_type="Payment", details=str(e))
                    await call.answer(localize("payments.stars.create_fail", error=str(e)), show_alert=True)
                    return
                await state.clear()
            else:
                await call.answer(localize("payments.not_configured"), show_alert=True)
                return

        elif call.data == "pay_fiat":
            if not EnvKeys.TELEGRAM_PROVIDER_TOKEN:
                await call.answer(localize("payments.not_configured"), show_alert=True)
                return

            try:
                await send_fiat_invoice(
                    bot=call.message.bot,
                    chat_id=call.from_user.id,
                    amount=int(amount_dec),
                )
            except Exception as e:
                await log_audit("fiat_invoice_fail", level="ERROR", user_id=call.from_user.id, resource_type="Payment", details=str(e))
                await call.answer(localize("payments.fiat.create_fail", error=str(e)), show_alert=True)
                return
            await state.clear()

        elif call.data == "pay_binance":
            if not (EnvKeys.BINANCE_API_KEY and EnvKeys.BINANCE_API_SECRET):
                await call.answer(localize("payments.not_configured"), show_alert=True)
                return
            try:
                expected_usdt = Decimal(str(amount_dec)) * Decimal(str(EnvKeys.BINANCE_USDT_RATE))
                address = EnvKeys.BINANCE_DEPOSIT_ADDRESS
                memo = ""
                if not address:
                    binance = BinanceAPI()
                    address_data = await binance.get_deposit_address(EnvKeys.BINANCE_USDT_NETWORK)
                    address = address_data.get("address", "")
                    memo = address_data.get("tag", "")
                if not address:
                    raise BinanceAPIError("Binance deposit address is unavailable")
                await state.update_data(
                    binance_amount=int(amount_dec),
                    binance_expected_usdt=str(expected_usdt.quantize(Decimal("0.00000001"))),
                    binance_address=address,
                )
                await call.message.edit_text(
                    localize(
                        "payments.binance.instructions",
                        amount=expected_usdt.quantize(Decimal("0.00000001")),
                        network=EnvKeys.BINANCE_USDT_NETWORK,
                        address=address,
                        memo=memo or "-",
                    ),
                    reply_markup=back("replenish_balance"),
                )
                await state.set_state(BalanceStates.waiting_binance_txid)
            except Exception as e:
                logger.error("Binance deposit address error: %s", e)
                await call.answer(localize("payments.not_configured"), show_alert=True)

    except Exception as e:
        logger.error(f"Payment processing error: {e}")
        await state.clear()
        await call.answer(localize("errors.something_wrong"), show_alert=True)


@router.message(BalanceStates.waiting_binance_txid, F.text)
async def process_binance_txid(message: Message, state: FSMContext):
    """Verify a Binance USDT deposit by blockchain transaction ID."""
    txid = (message.text or "").strip()
    if not txid or len(txid) > 200 or any(ch.isspace() for ch in txid):
        await message.answer(localize("payments.binance.invalid_txid"))
        return

    data = await state.get_data()
    amount = Decimal(str(data.get("binance_amount", 0)))
    expected_usdt = Decimal(str(data.get("binance_expected_usdt", 0)))
    if amount <= 0 or expected_usdt <= 0:
        await state.clear()
        await message.answer(localize("payments.session_expired"), reply_markup=back("profile"))
        return

    try:
        deposit = await BinanceAPI().verify_usdt_deposit(
            txid, float(expected_usdt), EnvKeys.BINANCE_USDT_NETWORK,
            data.get("binance_address", ""),
        )
    except BinanceAPIError:
        await message.answer(localize("payments.binance.verification_failed"))
        return

    if not deposit:
        await message.answer(localize("payments.binance.not_found"))
        return

    success, error_msg = await process_payment_with_referral(
        user_id=message.from_user.id,
        amount=amount,
        provider="binance_usdt",
        external_id=txid,
        referral_percent=EnvKeys.REFERRAL_PERCENT,
    )
    if not success and error_msg != "already_processed":
        await message.answer(localize("payments.processing_error"))
        return
    await message.answer(localize("payments.binance.confirmed", amount=amount, currency=EnvKeys.PAY_CURRENCY),
                         reply_markup=back("profile"))
    await state.clear()

@router.callback_query(F.data == "check")
async def checking_payment(call: CallbackQuery, state: FSMContext):
    """
    Check CryptoPay invoice status and credit balance if paid.
    """
    user_id = call.from_user.id
    data = await state.get_data()
    payment_type = data.get("payment_type")

    if not payment_type:
        await call.answer(localize("payments.no_active_invoice"), show_alert=True)
        return

    if payment_type == "cryptopay":
        invoice_id = data.get("invoice_id")
        if not invoice_id:
            await call.answer(localize("payments.invoice_not_found"), show_alert=True)
            await state.clear()
            return

        try:
            crypto = CryptoPayAPI()
            info = await crypto.get_invoice(invoice_id)
        except CryptoPayAPIError as e:
            await log_audit("cryptopay_check_error", level="ERROR", user_id=user_id, resource_type="Payment", details=f"[{e.code}] {e.name}")
            await call.answer(localize("payments.crypto.api_error", error=e.name), show_alert=True)
            return
        except Exception as e:
            await log_audit("cryptopay_get_fail", level="ERROR", user_id=user_id, resource_type="Payment", details=str(e))
            await call.answer(localize("payments.crypto.check_fail", error=str(e)), show_alert=True)
            return

        status = info.get("status")
        if status == "paid":
            balance_amount = Decimal(str(info.get("amount", "0"))).quantize(Decimal("0.01"))

            if balance_amount <= 0:
                await call.answer(localize("payments.unable_determine_amount"), show_alert=True)
                return

            # Use transactional payment processing
            success, error_msg = await process_payment_with_referral(
                user_id=user_id,
                amount=balance_amount,
                provider="cryptopay",
                external_id=str(invoice_id),
                referral_percent=EnvKeys.REFERRAL_PERCENT
            )

            if not success:
                if error_msg == "already_processed":
                    await call.answer(localize("payments.already_processed"), show_alert=True)
                else:
                    await call.answer(localize("errors.general_error", e=error_msg), show_alert=True)
                return

            metrics = get_metrics()
            if metrics:
                metrics.track_event("payment", user_id, {"amount": balance_amount, "provider": "cryptopay"})

            # Send a notification to the referrer
            await _notify_referrer_bonus(call.bot, user_id, balance_amount, call.from_user.first_name, call.from_user.id)

            await call.message.edit_text(
                localize("payments.topped_simple",
                         amount=balance_amount,
                         currency=EnvKeys.PAY_CURRENCY),
                reply_markup=back('profile')
            )
            await state.clear()

            safe_create_task(log_audit(
                "balance_replenish",
                user_id=user_id,
                resource_type="Payment",
                details=f"name={caller_name(call)}, amount={balance_amount} {EnvKeys.PAY_CURRENCY}, provider=cryptopay",
            ))

        elif status == "active":
            await call.answer(localize("payments.not_paid_yet"))
        else:
            await call.answer(localize("payments.expired"), show_alert=True)


@router.pre_checkout_query()
async def pre_checkout_handler(query: PreCheckoutQuery):
    """Validate the payment before Telegram processes it."""
    try:
        payload = json.loads(query.invoice_payload or "{}")
    except Exception:
        await query.answer(ok=False, error_message="Invalid payload")
        return

    amount = payload_amount(payload)
    if amount <= 0:
        await query.answer(ok=False, error_message="Invalid amount")
        return

    if amount < int(EnvKeys.MIN_AMOUNT):
        await query.answer(ok=False, error_message="Amount below minimum")
        return

    if amount > int(EnvKeys.MAX_AMOUNT):
        await query.answer(ok=False, error_message="Amount exceeds maximum")
        return

    await query.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment_handler(message: Message):
    """
    Handle successful payment:
    - XTR (Stars): total_amount is ⭐. take CURRENCY from payload (amount) or convert ⭐ → CURRENCY.
    - Fiat: total_amount is minor units; divide by 100 (or 1 for JPY/KRW).
    """
    sp: SuccessfulPayment = message.successful_payment
    user_id = message.from_user.id

    payload = {}
    try:
        if sp.invoice_payload:
            payload = json.loads(sp.invoice_payload)
    except Exception:
        payload = {}

    amount = payload_amount(payload)

    if amount <= 0:
        if sp.currency == "XTR":
            # Stars, no usable payload: reverse the conversion as a last resort.
            amount = int(
                (Decimal(int(sp.total_amount)) / Decimal(str(EnvKeys.STARS_PER_VALUE)))
                .to_integral_value(rounding=ROUND_HALF_UP)
            )
        else:
            # Fiat: total_amount is exact in minor units, so this is lossless.
            currency = sp.currency.upper()
            multiplier = _minor_units_for(currency)
            amount = int(Decimal(sp.total_amount) / Decimal(multiplier))

    if amount <= 0:
        await message.answer(localize("payments.unable_determine_amount"), reply_markup=close())
        return

    # Idempotence
    provider = "telegram" if sp.currency != "XTR" else "stars"
    external_id = sp.telegram_payment_charge_id or sp.provider_payment_charge_id
    if not external_id:
        digest = hashlib.sha256(
            f"{provider}|{user_id}|{sp.currency}|{sp.total_amount}|{sp.invoice_payload or ''}".encode()
        ).hexdigest()
        external_id = f"{provider}:fallback:{digest[:32]}"
        logger.warning(
            "successful_payment without a charge id for user %s (%s %s); "
            "falling back to a derived idempotency key %s",
            user_id, sp.total_amount, sp.currency, external_id,
        )

    success, error_msg = await process_payment_with_referral(
        user_id=user_id,
        amount=Decimal(amount),
        provider=provider,
        external_id=external_id,
        referral_percent=EnvKeys.REFERRAL_PERCENT
    )

    if not success:
        if error_msg == "already_processed":
            await message.answer(localize("payments.already_processed"), reply_markup=close())
        else:
            await message.answer(localize("payments.processing_error"), reply_markup=close())
        return

    # Sending notification to referrer
    await _notify_referrer_bonus(message.bot, user_id, amount, message.from_user.first_name, message.from_user.id)

    metrics = get_metrics()
    if metrics:
        metrics.track_event("payment", user_id, {"amount": amount, "provider": provider})

    suffix = localize("payments.success_suffix.stars") if sp.currency == "XTR" else localize(
        "payments.success_suffix.tg")
    await message.answer(
        localize('payments.topped_with_suffix', amount=amount, suffix=suffix, currency=EnvKeys.PAY_CURRENCY),
        reply_markup=back('profile')
    )

    safe_create_task(log_audit(
        "balance_replenish",
        user_id=user_id,
        resource_type="Payment",
        details=f"name={caller_name(message)}, amount={amount} {EnvKeys.PAY_CURRENCY}, provider={suffix}",
    ))


@router.callback_query(F.data == "buy_item")
async def buy_item_callback_handler(call: CallbackQuery, state: FSMContext):
    """Processing the purchase of goods with full transactional security."""
    try:
        # Get item name from state (stored when viewing item info)
        data = await state.get_data()
        raw_item_name = data.get('csrf_item')

        if not raw_item_name:
            await call.answer(localize("middleware.security.invalid_csrf"), show_alert=True)
            return

        metrics = get_metrics()

        # Validation via Pydantic
        purchase_request = ItemPurchaseRequest(
            item_name=raw_item_name,
            user_id=call.from_user.id
        )

        # Additional check for SQL injection
        if not is_safe_item_name(purchase_request.item_name):
            await call.answer(
                localize("errors.invalid_item_name"),
                show_alert=True
            )
            await log_audit("suspicious_item_name", level="WARNING", user_id=call.from_user.id, resource_type="Item", details=raw_item_name)
            return

        # User_id validation
        try:
            user_id = validate_telegram_id(call.from_user.id)
        except ValueError:
            await call.answer(localize("errors.invalid_user"), show_alert=True)
            return

        # Show the processing indicator
        await call.answer(localize("shop.purchase.processing"))

        # Get promo code from state if applied
        promo_code = data.get('applied_promo')

        # Execute a transactional purchase
        success, message, purchase_data = await buy_item_transaction(
            user_id,
            purchase_request.item_name,
            promo_code=promo_code,
        )

        if not success:
            # Error handling
            error_messages = {
                "user_not_found": "shop.purchase.fail.user_not_found",
                "item_not_found": "shop.item.not_found",
                "insufficient_funds": "shop.insufficient_funds",
                "out_of_stock": "shop.out_of_stock",
                "promo_invalid": "promo.not_found",
                "promo_expired": "promo.expired",
                "promo_max_uses": "promo.max_uses_reached",
                "promo_already_used": "promo.already_used",
                "promo_wrong_item": "promo.wrong_item",
                "promo_wrong_category": "promo.wrong_category",
            }

            error_text = localize(
                error_messages.get(message, "shop.purchase.fail.general"),
                message=message
            )

            await call.message.edit_text(
                error_text,
                reply_markup=back('back_to_item')
            )

            if message not in error_messages:
                await log_audit("purchase_error", level="ERROR", user_id=user_id, resource_type="Item", resource_id=purchase_request.item_name, details=message)
            return

        # Successful purchase - sanitize the output

        if metrics:
            metrics.track_event("purchase", call.from_user.id, {
                "item": purchase_request.item_name,
                "price": purchase_data['price']
            })
            metrics.track_conversion("purchase_funnel", "purchase", call.from_user.id)

        # Escaped, never "sanitized": a delivered value is data the buyer copies
        # verbatim, so a key that happens to contain <b> must show as <b>.
        safe_value = esc(purchase_data['value'])
        username = esc(call.from_user.username or call.from_user.first_name)

        # The promo was consumed by this purchase
        await state.update_data(applied_promo=None)

        from bot.keyboards.inline import simple_buttons
        buttons = [
            (f"📦 {purchase_data['item_name']}", f"bought-item:{purchase_data['bought_id']}:back_to_item"),
            (localize("btn.back"), "back_to_item"),
        ]

        await call.message.edit_text(
            localize(
                'shop.purchase.receipt',
                item_name=esc(purchase_data['item_name']),
                price=purchase_data['price'],
                unique_id=purchase_data['unique_id'],
                datetime=purchase_data['bought_datetime'],
                username=username,
                user_id=call.from_user.id,
                value=safe_value,
                currency=EnvKeys.PAY_CURRENCY,
            ),
            parse_mode='HTML',
            reply_markup=simple_buttons(buttons),
        )

        if purchase_data['item_name'] == UPI_SCAN_ITEM_NAME:
            await state.update_data(
                upi_scan_purchase_id=purchase_data['unique_id'],
                upi_scan_bought_id=purchase_data['bought_id'],
            )
            await call.message.answer(localize('shop.upi_scan.prompt'))
            await state.set_state(UpiScanFSM.waiting_link)

        safe_create_task(log_audit(
            "purchase",
            user_id=user_id,
            resource_type="Item",
            resource_id=purchase_request.item_name[:100],
            details=(
                f"name={caller_name(call)[:50]}, "
                f"price={purchase_data['price']} {EnvKeys.PAY_CURRENCY}, "
                f"unique_id={purchase_data['unique_id']}"
            ),
        ))

    except Exception as e:
        logger.error(f"Critical error in purchase handler: {e}")
        await call.answer(
            localize("errors.something_wrong"),
            show_alert=True
        )


@router.message(UpiScanFSM.waiting_link, F.text)
async def receive_upi_scan_link(message: Message, state: FSMContext):
    """Forward the buyer's UPI link to the configured owner."""
    try:
        link = UpiScanLinkRequest(link=message.text).link
    except ValueError:
        await message.answer(localize('shop.upi_scan.invalid_link'))
        return

    data = await state.get_data()
    purchase_id = data.get('upi_scan_purchase_id', 'unknown')
    buyer_name = esc(message.from_user.username or message.from_user.first_name or str(message.from_user.id))
    admin_text = (
        "UPI scan request\n"
        f"Buyer: @{buyer_name} ({message.from_user.id})\n"
        f"Purchase: {purchase_id}\n"
        f"Link: {esc(link)}"
    )

    try:
        order_id = await create_upi_scan_order(data['upi_scan_bought_id'], message.from_user.id, link)
        admin_text = f"Order UPI-{order_id}\n" + admin_text
        admin_ids = await eligible_upi_admin_ids()
        if EnvKeys.OWNER_ID not in admin_ids:
            admin_ids.append(EnvKeys.OWNER_ID)
        delivered = 0
        for admin_id in admin_ids:
            try:
                await message.bot.send_message(
                    admin_id,
                    admin_text,
                    parse_mode='HTML',
                    reply_markup=upi_order_keyboard(order_id),
                )
                delivered += 1
            except (TelegramBadRequest, TelegramForbiddenError) as e:
                logger.warning("Failed to notify UPI admin %s for order %s: %s", admin_id, order_id, e)
        if not delivered:
            await message.answer(localize('shop.upi_scan.delivery_failed'))
            return
    except Exception as e:
        logger.error("Failed to create UPI scan order %s: %s", purchase_id, e)
        await message.answer(localize('shop.upi_scan.delivery_failed'))
        return

    await message.answer(localize('shop.upi_scan.sent'))
    await state.clear()
