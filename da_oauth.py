"""Платежи через DonationAlerts.

Схема: пользователь создаёт платёж → получает уникальный код (TX-XXXXXX) и сумму →
отправляет донат, указав код в сообщении → поллер забирает донаты через API →
match_donation() находит платёж по коду, сверяет сумму и валюту → apply_payment().
Каждый донат сохраняется в таблицу donations, поэтому повторно засчитать его нельзя.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import utcnow
from app.models import Donation, Payment, Subscription, TapState, User
from app.pricing import PLANS_BY_ID, plan_amount, upgrade_amount
from app.security.risk import random_code
from app.services import referrals as referral_service
from app.services import subscriptions as sub_service
from app.services.notify import notify_admins, notify_user

log = logging.getLogger(__name__)

CODE_RE = re.compile(r"T\s*X[\s\-_.:#]*([A-HJ-NP-Z2-9]{6})", re.IGNORECASE)


class PaymentError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def format_code(raw: str) -> str:
    return f"TX-{raw}"


def extract_codes(*texts: str | None) -> list[str]:
    found: list[str] = []
    for t in texts:
        if not t:
            continue
        for m in CODE_RE.finditer(t.upper()):
            c = m.group(1)
            if c not in found:
                found.append(c)
    return found


async def _unique_code(session: AsyncSession) -> str:
    while True:
        c = random_code(6)
        if not await session.scalar(select(Payment.id).where(Payment.code == c)):
            return c


async def create_payment(
    session: AsyncSession,
    user: User,
    settings: Settings,
    *,
    kind: str,
    plan_id: str | None = None,
    devices: int = 1,
) -> Payment:
    if user.is_banned:
        raise PaymentError("banned", "Аккаунт заблокирован.")
    if not user.phone_verified:
        raise PaymentError("phone_required", "Сначала привяжите номер телефона.")
    now = utcnow()
    devices = int(devices)
    if not 1 <= devices <= settings.max_devices:
        raise PaymentError("bad_devices", "Недопустимое число устройств.")

    active = await sub_service.get_active(session, user.id, now)
    months = 0
    if kind == "subscription":
        plan = PLANS_BY_ID.get(plan_id or "")
        if not plan:
            raise PaymentError("bad_plan", "Тариф не найден.")
        amount = plan_amount(plan, devices)
        months = plan.months
    elif kind == "devices":
        if not active:
            raise PaymentError("subscription_required", "Сначала оформите подписку.")
        if devices <= active.devices:
            raise PaymentError("downgrade", "Уменьшить число устройств можно при продлении.")
        amount = upgrade_amount(active.devices, devices, active.expires_at, now, settings.min_payment_rub)
        plan_id = active.plan_id
    elif kind == "x2":
        if not active:
            raise PaymentError("subscription_required", "Сначала оформите подписку.")
        amount = settings.x2_price_rub
        plan_id = None
        devices = active.devices
    else:
        raise PaymentError("bad_kind", "Неизвестный тип платежа.")

    # один незавершённый платёж на пользователя: старые отменяем
    await session.execute(
        update(Payment)
        .where(Payment.user_id == user.id, Payment.status == "pending")
        .values(status="cancelled", note="replaced")
    )
    payment = Payment(
        user_id=user.id,
        code=await _unique_code(session),
        kind=kind,
        plan_id=plan_id,
        months=months,
        devices=devices,
        amount=max(settings.min_payment_rub, int(amount)),
        status="pending",
        created_at=now,
        expires_at=now + timedelta(minutes=settings.payment_ttl_minutes),
    )
    session.add(payment)
    await session.flush()
    return payment


def payment_payload(p: Payment, settings: Settings) -> dict:
    return {
        "code": format_code(p.code),
        "kind": p.kind,
        "plan_id": p.plan_id,
        "months": p.months,
        "devices": p.devices,
        "amount": p.amount,
        "status": p.status,
        "created_at": p.created_at.isoformat() + "Z",
        "expires_at": p.expires_at.isoformat() + "Z",
        "paid_at": p.paid_at.isoformat() + "Z" if p.paid_at else None,
        "pay_url": settings.da_page_url,
        "currency": settings.da_currency,
    }


@dataclass
class DonationIn:
    id: int
    amount: float
    currency: str
    username: str | None
    message: str | None
    created_at: str | None
    amount_in_user_currency: float | None = None

    @classmethod
    def from_api(cls, d: dict) -> "DonationIn":
        return cls(
            id=int(d["id"]),
            amount=float(d.get("amount") or 0),
            currency=str(d.get("currency") or ""),
            username=d.get("username"),
            message=d.get("message"),
            created_at=d.get("created_at"),
            amount_in_user_currency=float(d["amount_in_user_currency"]) if d.get("amount_in_user_currency") is not None else None,
        )


def _rub_amount(d: DonationIn, settings: Settings) -> float | None:
    if d.currency.upper() == settings.da_currency.upper():
        return d.amount
    if d.amount_in_user_currency is not None:
        return d.amount_in_user_currency
    return None


async def process_donation(session: AsyncSession, d: DonationIn, settings: Settings) -> str:
    """Идемпотентно обрабатывает донат. Возвращает результат для логов."""
    if await session.get(Donation, d.id):
        return "duplicate"
    row = Donation(id=d.id, amount=d.amount, currency=d.currency, username=(d.username or "")[:128],
                   message=d.message, created_at=d.created_at)
    session.add(row)

    codes = extract_codes(d.message, d.username)
    if not codes:
        row.result = "no_code"
        return row.result

    now = utcnow()
    payment = await session.scalar(
        select(Payment).where(Payment.code.in_(codes)).order_by(Payment.id.desc()).with_for_update()
    )
    if payment is None:
        row.result = "unknown_code"
        await notify_admins(f"Донат #{d.id} на {d.amount:g} {d.currency} с неизвестным кодом {', '.join(codes)}.")
        return row.result
    row.payment_id = payment.id

    if payment.status == "paid":
        row.result = "already_paid"
        await notify_admins(f"Повторный донат #{d.id} по уже оплаченному коду {format_code(payment.code)} ({d.amount:g} {d.currency}).")
        return row.result
    if payment.status == "cancelled" and payment.note != "replaced":
        row.result = "cancelled"
        await notify_admins(f"Донат #{d.id} по отменённому платежу {format_code(payment.code)}. Проверьте вручную.")
        return row.result
    if payment.expires_at + timedelta(hours=settings.payment_grace_hours) < now:
        row.result = "too_late"
        await notify_admins(f"Донат #{d.id} по давно истёкшему коду {format_code(payment.code)}. Подтвердите вручную: /pay_confirm {payment.code}")
        return row.result

    rub = _rub_amount(d, settings)
    if rub is None:
        row.result = "bad_currency"
        payment.note = f"currency {d.currency}"
        await notify_admins(f"Донат #{d.id} в валюте {d.currency} по коду {format_code(payment.code)}. Подтвердите вручную: /pay_confirm {payment.code}")
        return row.result
    # недоплату можно добить вторым донатом с тем же кодом
    total = rub + (payment.paid_amount or 0 if payment.status == "underpaid" else 0)
    if total + 0.001 < payment.amount:
        row.result = "underpaid"
        payment.status = "underpaid"
        payment.paid_amount = total
        await notify_user(payment.user_id, f"Платёж {format_code(payment.code)}: получено {total:g} ₽ из {payment.amount} ₽. Доплатите разницу с тем же кодом или напишите в поддержку @{settings.support_username}.")
        await notify_admins(f"Недоплата по {format_code(payment.code)}: {total:g} из {payment.amount} ₽ (донат #{d.id}). /pay_confirm {payment.code}")
        return row.result

    payment.donation_id = d.id
    payment.paid_amount = total
    await apply_payment(session, payment, settings)
    row.result = "paid"
    return row.result


async def apply_payment(session: AsyncSession, payment: Payment, settings: Settings) -> None:
    """Активирует то, за что заплатили. Вызывается из поллера и из админ-команды /pay_confirm."""
    if payment.status == "paid":
        return
    user = await session.get(User, payment.user_id, with_for_update=True)
    assert user is not None
    payment.status = "paid"
    payment.paid_at = utcnow()

    if payment.kind == "subscription":
        plan = PLANS_BY_ID[payment.plan_id or "m1"]
        sub = await sub_service.extend(session, user, settings, plan_id=plan.id, months=plan.months,
                                       devices=payment.devices, bonus_gb=plan.bonus_gb)
        await notify_user(user.id, f"Оплата получена. Подписка «{plan.title}» активна до {sub.expires_at:%d.%m.%Y}.")
        if await referral_service.is_first_paid_subscription(session, user.id, payment.id):
            result, ref = await referral_service.evaluate_on_first_payment(session, user, settings)
            if result == "credited" and ref:
                await notify_user(ref.referrer_id, f"Ваш друг оформил подписку — начислено +{settings.ref_bonus_gb} ГБ.")
            elif result == "hold" and ref:
                await notify_admins(f"Реферал #{ref.id} на проверке ({ref.reason}). /ref_ok {ref.id} или /ref_no {ref.id}")
    elif payment.kind == "devices":
        sub = await session.get(Subscription, user.id, with_for_update=True)
        if sub and sub.is_active():
            sub.devices = max(sub.devices, payment.devices)
            await notify_user(user.id, f"Оплата получена. Теперь доступно устройств: {sub.devices}.")
        else:
            payment.note = "sub_inactive_on_apply"
            await notify_admins(f"Доплата {format_code(payment.code)} пришла, но подписка неактивна — проверьте.")
    elif payment.kind == "x2":
        tap = await session.get(TapState, user.id, with_for_update=True)
        if tap is None:
            tap = TapState(user_id=user.id)
            session.add(tap)
        tap.x2_left = (tap.x2_left or 0) + settings.x2_taps
        await notify_user(user.id, f"Оплата получена. x2 активен на {settings.x2_taps} тапов.")


async def expire_stale(session: AsyncSession) -> int:
    res = await session.execute(
        update(Payment).where(Payment.status == "pending", Payment.expires_at < utcnow()).values(status="expired")
    )
    return res.rowcount or 0
