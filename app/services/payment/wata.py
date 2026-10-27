"""Mixin for integrating WATA payment links into the payment service."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from importlib import import_module
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.services.wata_service import WataAPIError, WataService
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


def _identifier(value: Any) -> str | None:
    return value if isinstance(value, str) and value and value == value.strip() else None


def _amount_matches(value: Any, amount_kopeks: Any) -> bool:
    """Compare exact minor units without rounding untrusted provider amounts."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return False
    if type(amount_kopeks) is not int or amount_kopeks <= 0:
        return False
    try:
        amount = Decimal(str(value))
        # Compare directly, without multiplication/quantize subject to context rounding.
        expected = Decimal(f'{amount_kopeks // 100}.{amount_kopeks % 100:02d}')
        return amount.is_finite() and amount > 0 and amount == expected
    except InvalidOperation, ValueError, TypeError:
        return False


def _matches_payment(
    payment: Any, payload: Any, *, webhook: bool = False, link: bool = False, require_paid: bool = True
) -> bool:
    """Bind provider evidence to the immutable local order before any mutation."""
    if not isinstance(payload, dict):
        return False
    if not _amount_matches(payload.get('amount'), payment.amount_kopeks):
        return False
    if payload.get('currency') != payment.currency or payment.currency != 'RUB':
        return False
    if not _identifier(payment.order_id) or payload.get('orderId') != payment.order_id:
        return False
    if not _identifier(payment.payment_link_id):
        return False
    if webhook:
        # WATA's current webhook example has paymentLinkId=null and a distinct
        # id/transactionId. Only transactionId identifies the API lookup; id is
        # ambiguous across API versions and must never stand in for a link ID.
        # The fetched transaction must still have the exact local paymentLinkId.
        callback_link = payload.get('paymentLinkId')
        if callback_link is not None and callback_link != payment.payment_link_id:
            return False
    elif payload.get('id' if link else 'paymentLinkId') != payment.payment_link_id:
        return False
    terminal = getattr(payment, 'terminal_public_id', None)
    if terminal and payload.get('terminalPublicId') != terminal:
        return False
    if link:
        return payload.get('status') in ('Opened', 'Closed', 'Paid')
    status_keys = ('transactionStatus',) if webhook else ('status', 'statusName')
    statuses = [payload[key] for key in status_keys if key in payload]
    allowed_statuses = ('Paid',) if require_paid else ('Created', 'Pending', 'Declined')
    if not statuses or any(value not in allowed_statuses for value in statuses):
        return False
    if payload.get('kind') != 'Payment' or payload.get('originalTransactionId'):
        return False
    return _identifier(payload.get('transactionId' if webhook else 'id')) is not None


def _extract_transaction_id(payment: Any, remote_link: dict[str, Any] | None = None) -> str | None:
    """Webhook id and link id are NOT transaction IDs; never interchange them."""
    callback = getattr(payment, 'callback_payload', None)
    if isinstance(callback, dict):
        key = 'transactionId' if 'transactionStatus' in callback else 'id'
        candidate = _identifier(callback.get(key))
        if candidate:
            return candidate
    metadata = getattr(payment, 'metadata_json', None)
    transaction = metadata.get('transaction') if isinstance(metadata, dict) else None
    if isinstance(transaction, dict):
        candidate = _identifier(transaction.get('id'))
        if candidate:
            return candidate
    return _identifier(remote_link.get('transactionId')) if isinstance(remote_link, dict) else None


class WataPaymentMixin:
    """Encapsulates creation and status handling for WATA payment links."""

    async def create_wata_payment(
        self,
        db: AsyncSession,
        user_id: int | None,
        amount_kopeks: int,
        description: str,
        *,
        language: str | None = None,
        return_url: str | None = None,
        failed_url: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if not getattr(self, 'wata_service', None):
            logger.error('WATA service is not initialised')
            return None

        if amount_kopeks < settings.WATA_MIN_AMOUNT_KOPEKS:
            logger.warning(
                'Сумма WATA меньше минимальной: <',
                amount_kopeks=amount_kopeks,
                WATA_MIN_AMOUNT_KOPEKS=settings.WATA_MIN_AMOUNT_KOPEKS,
            )
            return None

        if amount_kopeks > settings.WATA_MAX_AMOUNT_KOPEKS:
            logger.warning(
                'Сумма WATA больше максимальной: >',
                amount_kopeks=amount_kopeks,
                WATA_MAX_AMOUNT_KOPEKS=settings.WATA_MAX_AMOUNT_KOPEKS,
            )
            return None

        payment_module = import_module('app.services.payment_service')

        # Добавляем идентификатор плательщика (telegram_id или email) в описание
        try:
            if user_id is not None:
                user = await payment_module.get_user_by_id(db, user_id)
            else:
                user = None
            if user:
                if user.telegram_id:
                    description = f'{description} | ID: {user.telegram_id}'
                elif user.email:
                    description = f'{description} | {user.email}'
        except Exception as error:
            logger.debug('Не удалось получить данные пользователя для описания WATA', error=error)

        order_id = f'wata_{user_id or "guest"}_{uuid.uuid4().hex[:12]}'

        try:
            response = await self.wata_service.create_payment_link(  # type: ignore[union-attr]
                amount_kopeks=amount_kopeks,
                currency='RUB',
                description=description,
                order_id=order_id,
                success_url=return_url,
                fail_url=failed_url,
            )
        except WataAPIError as error:
            logger.error('Ошибка создания WATA платежа', error=error)
            return None
        except Exception as error:  # pragma: no cover - safety net
            logger.exception('Непредвиденная ошибка при создании WATA платежа', error=error)
            return None

        payment_link_id = response.get('id')
        payment_url = response.get('url') or response.get('paymentUrl')
        status = response.get('status') or 'Opened'
        terminal_public_id = response.get('terminalPublicId')
        success_url = response.get('successRedirectUrl')
        fail_url = response.get('failRedirectUrl')

        if not payment_link_id:
            logger.error('WATA API не вернула идентификатор платежной ссылки', response=response)
            return None

        expiration_raw = response.get('expirationDateTime')
        expires_at = WataService._parse_datetime(expiration_raw)

        payment_metadata = {
            **(metadata or {}),
            'response': response,
            'language': language or settings.DEFAULT_LANGUAGE,
        }

        local_payment = await payment_module.create_wata_payment(
            db=db,
            user_id=user_id,
            payment_link_id=payment_link_id,
            amount_kopeks=amount_kopeks,
            currency='RUB',
            description=description,
            status=status,
            type_=response.get('type'),
            url=payment_url,
            order_id=order_id,
            metadata=payment_metadata,
            expires_at=expires_at,
            terminal_public_id=terminal_public_id,
            success_redirect_url=success_url,
            fail_redirect_url=fail_url,
        )

        logger.info(
            'Создан WATA платеж',
            payment_link_id=payment_link_id,
            amount_kopeks=amount_kopeks / 100,
            user_id=user_id,
        )

        return {
            'local_payment_id': local_payment.id,
            'payment_link_id': payment_link_id,
            'payment_url': payment_url,
            'status': status,
            'order_id': order_id,
        }

    async def process_wata_webhook(
        self,
        db: AsyncSession,
        payload: dict[str, Any],
    ) -> bool:
        """Handles asynchronous webhook notifications from WATA."""

        payment_module = import_module('app.services.payment_service')

        if not isinstance(payload, dict):
            logger.error('WATA webhook payload не является словарём', payload=payload)
            return False

        order_id_raw = payload.get('orderId')
        payment_link_raw = payload.get('paymentLinkId')
        transaction_status_raw = payload.get('transactionStatus')

        order_id = str(order_id_raw) if order_id_raw else None
        payment_link_id = str(payment_link_raw) if payment_link_raw else None
        transaction_status = transaction_status_raw if isinstance(transaction_status_raw, str) else ''

        if not order_id and not payment_link_id:
            logger.error('WATA webhook без orderId и paymentLinkId', payload=payload)
            return False

        if not transaction_status:
            logger.error('WATA webhook без статуса транзакции', payload=payload)
            return False

        payment = None
        if order_id:
            payment = await payment_module.get_wata_payment_by_order_id(db, order_id)
        if not payment and payment_link_id:
            payment = await payment_module.get_wata_payment_by_link_id(db, payment_link_id)

        if not payment:
            logger.error('WATA платеж не найден', order_id=order_id, payment_link_id=payment_link_id)
            return False

        verified_transaction = None
        is_paid_callback = transaction_status == 'Paid'
        if not _matches_payment(payment, payload, webhook=True, require_paid=is_paid_callback):
            logger.warning('WATA callback rejected: payment evidence mismatch', payment_id=payment.id)
            return False
        if is_paid_callback:
            if not getattr(self, 'wata_service', None):
                return False
            try:
                verified_transaction = await self.wata_service.get_transaction(payload['transactionId'])
            except Exception as error:
                logger.warning('WATA callback verification unavailable', error_type=type(error).__name__)
                return False
            if (
                not _matches_payment(payment, verified_transaction)
                or verified_transaction['id'] != payload['transactionId']
                or verified_transaction.get('terminalPublicId') != payload.get('terminalPublicId')
            ):
                logger.warning('WATA callback rejected: API evidence mismatch', payment_id=payment.id)
                return False

        # Lock after the network request, then recheck evidence against the locked order.
        wata_crud = import_module('app.database.crud.wata')
        locked = await wata_crud.get_wata_payment_by_id_for_update(db, payment.id)
        if not locked:
            logger.error('WATA: не удалось заблокировать платёж', payment_id=payment.id)
            return False
        payment = locked
        if not _matches_payment(payment, payload, webhook=True, require_paid=is_paid_callback):
            return False
        if payment.is_paid and not is_paid_callback:
            # A delayed failed attempt must not downgrade a settled payment.
            return True

        status_lower = transaction_status.lower()
        metadata = dict(getattr(payment, 'metadata_json', {}) or {})
        metadata['last_webhook'] = payload
        terminal_public_id = (
            payload.get('terminalPublicId') or payload.get('terminal_public_id') or payload.get('terminalPublicID')
        )

        if status_lower == 'paid':
            if not _matches_payment(payment, payload, webhook=True) or not _matches_payment(
                payment, verified_transaction
            ):
                return False
            if payment.is_paid and metadata.get('purpose') != 'subpage_renewal':
                logger.info('WATA платеж уже помечен как оплачен', payment_link_id=payment.payment_link_id)
                # Update callback payload without releasing the lock prematurely
                payment.callback_payload = verified_transaction
                payment.metadata_json = metadata
                if terminal_public_id:
                    payment.terminal_public_id = terminal_public_id
                await db.commit()
                return True

            # Inline field updates — NO intermediate commit that would release FOR UPDATE lock
            payment.status = transaction_status
            payment.last_status = transaction_status
            payment.callback_payload = payload
            payment.metadata_json = metadata
            if terminal_public_id:
                payment.terminal_public_id = terminal_public_id
            await db.flush()

            await self._finalize_wata_payment(db, payment, verified_transaction)
            return True

        # Non-success statuses: safe to use update_wata_payment_status (commits)
        update_kwargs: dict[str, Any] = {
            'metadata': metadata,
            'callback_payload': payload,
            'status': transaction_status,
            'last_status': transaction_status,
        }
        if not payment.is_paid:
            update_kwargs['is_paid'] = False

        payment = await payment_module.update_wata_payment_status(
            db,
            payment=payment,
            **update_kwargs,
        )

        if status_lower == 'declined':
            logger.info('WATA платеж отклонён', payment_link_id=payment.payment_link_id)

        return True

    async def get_wata_payment_status(
        self,
        db: AsyncSession,
        local_payment_id: int,
    ) -> dict[str, Any] | None:
        payment_module = import_module('app.services.payment_service')

        payment = await payment_module.get_wata_payment_by_id(db, local_payment_id)
        if not payment:
            return None

        remote_link = None
        transaction_payload = None
        service = getattr(self, 'wata_service', None)
        if service and not payment.is_paid:
            try:
                remote_link = await service.get_payment_link(payment.payment_link_id)
            except Exception as error:
                logger.warning('WATA link verification unavailable', error_type=type(error).__name__)

            # A contradictory link must not be persisted or used to finalize an order.
            link_valid = remote_link is None or _matches_payment(payment, remote_link, link=True)
            if not link_valid:
                logger.warning('WATA status rejected: link evidence mismatch', payment_id=payment.id)
            else:
                transaction_id = _extract_transaction_id(payment, remote_link)
                if transaction_id:
                    try:
                        candidate = await service.get_transaction(transaction_id)
                        if _matches_payment(payment, candidate) and candidate['id'] == transaction_id:
                            transaction_payload = candidate
                    except Exception as error:
                        logger.warning('WATA transaction verification unavailable', error_type=type(error).__name__)
                if transaction_payload is None:
                    try:
                        response = await service.search_transactions(
                            order_id=payment.order_id,
                            payment_link_id=payment.payment_link_id,
                            status='Paid',
                            limit=5,
                        )
                        items = response.get('items') if isinstance(response, dict) else None
                        if isinstance(items, list):
                            # Search filters are hints, not proof of identity or amount.
                            transaction_payload = next(
                                (item for item in items if _matches_payment(payment, item)), None
                            )
                    except Exception as error:
                        logger.warning('WATA search verification unavailable', error_type=type(error).__name__)

                if transaction_payload is not None:
                    wata_crud = import_module('app.database.crud.wata')
                    locked = await wata_crud.get_wata_payment_by_id_for_update(db, payment.id)
                    if locked:
                        payment = locked
                        if not payment.is_paid and _matches_payment(payment, transaction_payload):
                            payment = await self._finalize_wata_payment(db, payment, transaction_payload)

        return {
            'payment': payment,
            'status': payment.status,
            'is_paid': payment.is_paid,
            'remote_link': remote_link,
            'transaction': transaction_payload,
        }

    async def _finalize_wata_payment(
        self,
        db: AsyncSession,
        payment: Any,
        transaction_payload: dict[str, Any],
    ) -> Any:
        payment_module = import_module('app.services.payment_service')

        # Defense in depth: every caller, including future recovery paths, must
        # supply verified Paid/Payment evidence before receipts, gifts or balance.
        if not _matches_payment(payment, transaction_payload):
            logger.warning('WATA finalization rejected: payment evidence mismatch', payment_id=payment.id)
            return payment

        # FOR UPDATE lock already acquired by caller — just check idempotency
        if payment.transaction_id and (payment.metadata_json or {}).get('purpose') != 'subpage_renewal':
            logger.info(
                'WATA платеж уже привязан к транзакции',
                payment_link_id=payment.payment_link_id,
                transaction_id=payment.transaction_id,
            )
            return payment

        paid_at = None
        if isinstance(transaction_payload, dict):
            paid_at = WataService._parse_datetime(transaction_payload.get('paymentTime'))
        if not paid_at and getattr(payment, 'paid_at', None):
            paid_at = payment.paid_at
        existing_metadata = dict(getattr(payment, 'metadata_json', {}) or {})

        invoice_message = existing_metadata.get('invoice_message') or {}
        if getattr(self, 'bot', None) and invoice_message:
            chat_id = invoice_message.get('chat_id')
            message_id = invoice_message.get('message_id')
            if chat_id and message_id:
                try:
                    await self.bot.delete_message(chat_id, message_id)
                except Exception as delete_error:  # pragma: no cover - depends on rights
                    logger.warning('Не удалось удалить счёт WATA', message_id=message_id, delete_error=delete_error)
                else:
                    existing_metadata.pop('invoice_message', None)

        existing_metadata['transaction'] = transaction_payload

        # Inline field updates — NO intermediate commit that would release FOR UPDATE lock
        payment.status = 'Paid'
        payment.is_paid = True
        payment.paid_at = paid_at
        payment.callback_payload = transaction_payload
        payment.metadata_json = existing_metadata
        await db.flush()

        # --- Guest purchase flow (landing page) ---
        wata_metadata = dict(getattr(payment, 'metadata_json', {}) or {})
        from app.services.payment.common import try_fulfill_guest_purchase

        guest_result = await try_fulfill_guest_purchase(
            db,
            metadata=wata_metadata,
            payment_amount_kopeks=payment.amount_kopeks,
            provider_payment_id=payment.payment_link_id,
            provider_name='wata',
        )
        if guest_result is not None:
            return payment

        # --- Subscription-page renewal flow -------------------------------
        from app.services.subpage_payment_service import try_fulfill_subpage_renewal

        subpage_result = await try_fulfill_subpage_renewal(
            db,
            metadata=wata_metadata,
            payment_amount_kopeks=payment.amount_kopeks,
            provider_payment_id=payment.payment_link_id,
            provider_name='wata',
            payment_user_id=payment.user_id,
            currency=payment.currency,
        )
        if subpage_result is not None:
            return payment

        user = await payment_module.get_user_by_id(db, payment.user_id)
        if not user:
            logger.error('Пользователь не найден при обработке WATA', user_id=payment.user_id)
            return payment

        transaction_external_id = str(transaction_payload.get('id') or transaction_payload.get('transactionId') or '')
        description = f'Пополнение через WATA ({payment.payment_link_id})'

        transaction = await payment_module.create_transaction(
            db,
            user_id=payment.user_id,
            type=TransactionType.DEPOSIT,
            amount_kopeks=payment.amount_kopeks,
            description=description,
            payment_method=PaymentMethod.WATA,
            external_id=transaction_external_id or payment.payment_link_id,
            is_completed=True,
            created_at=getattr(payment, 'created_at', None),
            commit=False,
        )

        await payment_module.link_wata_payment_to_transaction(db, payment, transaction.id)

        # Lock user row to prevent concurrent balance race conditions
        from app.database.crud.user import lock_user_for_update

        user = await lock_user_for_update(db, user)

        old_balance = user.balance_kopeks
        was_first_topup = not user.has_made_first_topup

        user.balance_kopeks += payment.amount_kopeks
        user.updated_at = datetime.now(UTC)
        await db.commit()

        # Emit deferred side-effects after atomic commit
        from app.database.crud.transaction import emit_transaction_side_effects

        await emit_transaction_side_effects(
            db,
            transaction,
            amount_kopeks=payment.amount_kopeks,
            user_id=payment.user_id,
            type=TransactionType.DEPOSIT,
            payment_method=PaymentMethod.WATA,
            external_id=transaction_external_id or payment.payment_link_id,
        )

        user = await payment_module.get_user_by_id(db, user.id)
        if not user:
            logger.error('Пользователь не найден после коммита WATA', user_id=payment.user_id)
            return payment

        promo_group = user.get_primary_promo_group()
        subscription = getattr(user, 'subscription', None)
        referrer_info = format_referrer_info(user)
        topup_status = '🆕 Первое пополнение' if was_first_topup else '🔄 Пополнение'

        try:
            from app.services.referral_service import process_referral_topup

            await process_referral_topup(
                db,
                user.id,
                payment.amount_kopeks,
                getattr(self, 'bot', None),
            )
        except Exception as error:
            logger.error('Ошибка обработки реферального пополнения WATA', error=error)

        if was_first_topup and not user.has_made_first_topup and not user.referred_by_id:
            user.has_made_first_topup = True
            await db.commit()
            await db.refresh(user)

        if getattr(self, 'bot', None):
            try:
                from app.services.admin_notification_service import AdminNotificationService

                notification_service = AdminNotificationService(self.bot)
                await notification_service.send_balance_topup_notification(
                    user,
                    transaction,
                    old_balance,
                    topup_status=topup_status,
                    referrer_info=referrer_info,
                    subscription=subscription,
                    promo_group=promo_group,
                    db=db,
                )
            except Exception as error:
                logger.error('Ошибка отправки админ уведомления WATA', error=error)

        if getattr(self, 'bot', None) and user.telegram_id and settings.is_notifications_enabled():
            try:
                keyboard = await self.build_topup_success_keyboard(user)
                await self.bot.send_message(
                    user.telegram_id,
                    (
                        '✅ <b>Пополнение успешно!</b>\n\n'
                        f'💰 Сумма: {settings.format_price(payment.amount_kopeks)}\n'
                        '🦊 Способ: WATA\n'
                        f'🆔 Транзакция: {transaction.id}\n\n'
                        'Баланс пополнен автоматически!'
                    ),
                    parse_mode='HTML',
                    reply_markup=keyboard,
                )
            except Exception as error:
                logger.error('Ошибка отправки уведомления пользователю WATA', error=error)

        try:
            from app.services.payment.common import send_cart_notification_after_topup

            await send_cart_notification_after_topup(user, payment.amount_kopeks, db, getattr(self, 'bot', None))
        except Exception as error:
            logger.debug('Не удалось отправить напоминание о корзине после WATA', error=error)

        return payment
