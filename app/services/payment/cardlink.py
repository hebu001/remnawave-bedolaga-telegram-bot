"""Cardlink billing with API-verified, atomic and replay-safe settlement."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud import cardlink as cardlink_crud
from app.database.models import CardlinkPayment, PaymentMethod, TransactionType
from app.services.cardlink_service import CardlinkAPIError, money_to_kopeks
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


class CardlinkPaymentMixin:
    async def create_cardlink_payment(
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
        service = getattr(self, 'cardlink_service', None)
        if not service or not service.is_available():
            return None
        if isinstance(amount_kopeks, bool) or not isinstance(amount_kopeks, int):
            return None
        if not settings.CARDLINK_MIN_AMOUNT_KOPEKS <= amount_kopeks <= settings.CARDLINK_MAX_AMOUNT_KOPEKS:
            return None
        order_id = f'cardlink_{uuid.uuid4().hex}'
        try:
            response = await service.create_payment_link(
                amount_kopeks=amount_kopeks,
                currency='RUB',
                description=description,
                order_id=order_id,
                # Cardlink POSTs to merchant-approved return URLs. Generic
                # cabinet/subpage SPA URLs passed by callers are GET-only.
                success_url=settings.CARDLINK_SUCCESS_REDIRECT_URL,
                fail_url=settings.CARDLINK_FAIL_REDIRECT_URL,
                language=language or settings.DEFAULT_LANGUAGE,
            )
        except CardlinkAPIError:
            logger.warning('Cardlink bill creation failed')
            return None
        bill_id = response.get('bill_id')
        payment_url = response.get('link_page_url')
        if not isinstance(bill_id, str) or not bill_id or not isinstance(payment_url, str) or not payment_url:
            logger.error('Cardlink create response is incomplete')
            return None
        payment = await cardlink_crud.create_cardlink_payment(
            db,
            user_id=user_id,
            bill_id=bill_id,
            order_id=order_id,
            amount_kopeks=amount_kopeks,
            description=description,
            url=payment_url,
            metadata={**(metadata or {}), 'response': response, 'language': language or settings.DEFAULT_LANGUAGE},
            expires_at=datetime.now(UTC) + timedelta(seconds=settings.CARDLINK_LINK_TTL_SECONDS),
        )
        return {
            'local_payment_id': payment.id,
            'bill_id': bill_id,
            'payment_id': bill_id,
            'payment_url': payment_url,
            'status': payment.status,
            'order_id': order_id,
        }

    @staticmethod
    def _validate_cardlink_bill(payment: CardlinkPayment, bill: dict[str, Any]) -> bool:
        """The callback authenticates neither status nor transaction/currency.

        Only a complete authenticated API bill that matches this immutable local
        order may authorize money movement. Never infer status from a callback.
        """
        if not isinstance(bill, dict):
            return False
        try:
            return (
                bill.get('id') == payment.bill_id
                and bill.get('order_id') == payment.order_id
                and bill.get('currency_in') == payment.currency == 'RUB'
                and bill.get('type') == 'NORMAL'
                and money_to_kopeks(bill.get('amount')) == payment.amount_kopeks
                and bill.get('status') in ('NEW', 'PROCESS', 'UNDERPAID', 'SUCCESS', 'OVERPAID', 'FAIL')
            )
        except ValueError, TypeError, CardlinkAPIError:
            return False

    async def process_cardlink_webhook(self, db: AsyncSession, payload: dict[str, Any]) -> bool:
        service = getattr(self, 'cardlink_service', None)
        if not service or not isinstance(payload, dict) or not service.verify_callback(payload):
            return False
        order_id = payload.get('InvId')
        if not isinstance(order_id, str) or not order_id:
            return False
        payment = await cardlink_crud.get_cardlink_payment_by_order_id(db, order_id)
        if payment is None or payload.get('TrsId') != payment.bill_id or payload.get('CurrencyIn') != payment.currency:
            return False
        # Remote I/O is done before row locks; both callback and poll converge
        # on the same lock and settlement code below.
        return await self._sync_cardlink_payment(db, payment, payload) is not None

    async def get_cardlink_payment_status(self, db: AsyncSession, local_payment_id: int) -> dict[str, Any] | None:
        payment = await cardlink_crud.get_cardlink_payment_by_id(db, local_payment_id)
        if payment is None:
            return None
        result = await self._sync_cardlink_payment(db, payment)
        if result is None:
            return None
        payment, bill = result
        return {'payment': payment, 'status': payment.status, 'is_paid': payment.is_paid, 'remote_bill': bill}

    async def _sync_cardlink_payment(
        self,
        db: AsyncSession,
        payment: CardlinkPayment,
        callback: dict[str, Any] | None = None,
    ) -> tuple[CardlinkPayment, dict[str, Any]] | None:
        service = getattr(self, 'cardlink_service', None)
        if service is None:
            return None
        try:
            bill = await service.get_bill(payment.bill_id)
        except CardlinkAPIError:
            logger.warning('Cardlink bill verification unavailable', payment_id=payment.id)
            return None
        if not isinstance(bill, dict) or not self._validate_cardlink_bill(payment, bill):
            logger.error('Cardlink bill does not match local order', payment_id=payment.id)
            return None
        payment = await cardlink_crud.get_cardlink_payment_by_id_for_update(db, payment.id)
        if payment is None or not self._validate_cardlink_bill(payment, bill):
            await db.rollback()
            return None
        try:
            status = bill['status']
            metadata = dict(payment.metadata_json or {})
            metadata['verified_bill'] = bill
            payment.metadata_json = metadata
            if callback is not None:
                payment.callback_payload = callback
            if status not in {'SUCCESS', 'OVERPAID'}:
                # A delayed or unsigned failed notification can never undo a receipt.
                if not payment.is_paid:
                    payment.status = status
                await db.commit()
                return payment, bill
            if payment.is_paid and metadata.get('purpose') not in {'guest_purchase', 'subpage_renewal'}:
                await db.commit()
                return payment, bill
            await self._finalize_cardlink_payment(db, payment, bill)
            return payment, bill
        except BaseException:
            await db.rollback()
            raise

    async def _finalize_cardlink_payment(
        self,
        db: AsyncSession,
        payment: CardlinkPayment,
        bill: dict[str, Any],
    ) -> CardlinkPayment:
        """Caller holds the receipt row lock through ledger and balance commit."""
        # _sync already authenticates and validates the API bill. Repeat the
        # settlement invariant here so another internal caller cannot persist a
        # paid receipt or dispatch guest/subpage fulfillment with weaker proof.
        if not self._validate_cardlink_bill(payment, bill) or bill['status'] not in ('SUCCESS', 'OVERPAID'):
            raise ValueError('cardlink_payment_evidence_mismatch')
        payment_module = import_module('app.services.payment_service')
        metadata = dict(payment.metadata_json or {})
        purpose = metadata.get('purpose')
        if payment.transaction_id and purpose not in {'guest_purchase', 'subpage_renewal'}:
            await db.commit()
            return payment
        payment.status = bill['status']
        payment.is_paid = True
        payment.paid_at = payment.paid_at or datetime.now(UTC)
        await db.flush()

        if purpose == 'guest_purchase':
            from app.services.payment.common import try_fulfill_guest_purchase

            # Capture first: guest fulfillment can roll back and retry in a
            # fresh session. Its own purchase lock guarantees idempotency.
            await db.commit()
            consumed = await try_fulfill_guest_purchase(
                db,
                metadata=metadata,
                payment_amount_kopeks=payment.amount_kopeks,
                provider_payment_id=payment.bill_id,
                provider_name='cardlink',
            )
            if consumed is None:
                raise ValueError('cardlink_guest_metadata_invalid')
            return payment
        if purpose == 'subpage_renewal':
            from app.services.subpage_payment_service import try_fulfill_subpage_renewal

            consumed = await try_fulfill_subpage_renewal(
                db,
                metadata=metadata,
                payment_amount_kopeks=payment.amount_kopeks,
                provider_payment_id=payment.bill_id,
                provider_name='cardlink',
                payment_user_id=payment.user_id,
                currency=payment.currency,
            )
            if consumed is None:
                raise ValueError('cardlink_subpage_metadata_invalid')
            return payment

        user = await payment_module.get_user_by_id(db, payment.user_id) if payment.user_id else None
        if user is None:
            # Do not acknowledge an unfulfillable topup as credited.
            raise ValueError('cardlink_payment_owner_missing')
        # Lock the user before inserting a transaction: its foreign key takes
        # KEY SHARE, which can deadlock with another receipt upgrading to UPDATE.
        from app.database.crud.user import lock_user_for_update

        user = await lock_user_for_update(db, user)
        transaction = await payment_module.create_transaction(
            db,
            user_id=payment.user_id,
            type=TransactionType.DEPOSIT,
            amount_kopeks=payment.amount_kopeks,
            description=f'Пополнение через Cardlink ({payment.bill_id})',
            payment_method=PaymentMethod.CARDLINK,
            external_id=payment.bill_id,
            is_completed=True,
            created_at=payment.created_at,
            commit=False,
        )
        await cardlink_crud.link_cardlink_payment_to_transaction(db, payment, transaction.id)
        old_balance = user.balance_kopeks
        was_first_topup = not user.has_made_first_topup
        user.balance_kopeks += payment.amount_kopeks
        user.updated_at = datetime.now(UTC)
        await db.commit()

        from app.database.crud.transaction import emit_transaction_side_effects

        await emit_transaction_side_effects(
            db,
            transaction,
            amount_kopeks=payment.amount_kopeks,
            user_id=payment.user_id,
            type=TransactionType.DEPOSIT,
            payment_method=PaymentMethod.CARDLINK,
            external_id=payment.bill_id,
        )
        user = await payment_module.get_user_by_id(db, user.id)
        if not user:
            logger.error('Пользователь не найден после коммита Cardlink', user_id=payment.user_id)
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
            logger.error('Ошибка обработки реферального пополнения Cardlink', error=error)

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
                logger.error('Ошибка отправки админ уведомления Cardlink', error=error)

        if getattr(self, 'bot', None) and user.telegram_id and settings.is_notifications_enabled():
            try:
                keyboard = await self.build_topup_success_keyboard(user)
                await self.bot.send_message(
                    user.telegram_id,
                    (
                        '✅ <b>Пополнение успешно!</b>\n\n'
                        f'💰 Сумма: {settings.format_price(payment.amount_kopeks)}\n'
                        '💳 Способ: Cardlink\n'
                        f'🆔 Транзакция: {transaction.id}\n\n'
                        'Баланс пополнен автоматически!'
                    ),
                    parse_mode='HTML',
                    reply_markup=keyboard,
                )
            except Exception as error:
                logger.error('Ошибка отправки уведомления пользователю Cardlink', error=error)

        try:
            from app.services.payment.common import send_cart_notification_after_topup

            await send_cart_notification_after_topup(user, payment.amount_kopeks, db, getattr(self, 'bot', None))
        except Exception as error:
            logger.debug('Не удалось отправить напоминание о корзине после Cardlink', error=error)

        return payment
