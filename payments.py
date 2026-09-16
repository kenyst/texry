from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import utcnow
from app.models import Payment, Referral, User
from app.services.subscriptions import MB_IN_GB, add_bonus_mb
from app.services.users import referrals_last_day

MONTHS_RU = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]
DAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


async def evaluate_on_first_payment(session: AsyncSession, invitee: User, settings: Settings) -> tuple[str, Referral | None]:
    """Вызывается после ПЕРВОЙ успешной оплаты подписки приглашённым.
    Возвращает (результат, реферал): credited | hold | rejected | none."""
    ref = await session.scalar(select(Referral).where(Referral.invitee_id == invitee.id).with_for_update())
    if not ref or ref.status != "joined":
        return "none", ref
    referrer = await session.get(User, ref.referrer_id, with_for_update=True)
    if referrer is None or referrer.is_banned:
        ref.status, ref.reason = "rejected", "referrer_banned"
        return "rejected", ref
    if invitee.phone_hash and referrer.phone_hash and invitee.phone_hash == referrer.phone_hash:
        ref.status, ref.reason = "rejected", "same_phone"
        return "rejected", ref

    reasons = []
    if invitee.risk_score >= settings.risk_hold_score:
        reasons.append(f"risk:{invitee.risk_flags}")
    if await referrals_last_day(session, referrer.id) > settings.ref_max_per_day:
        reasons.append("too_many_per_day")
    if reasons:
        ref.status, ref.reason = "hold", ";".join(reasons)[:128]
        return "hold", ref

    await credit(session, ref, referrer, settings)
    return "credited", ref


async def credit(session: AsyncSession, ref: Referral, referrer: User, settings: Settings) -> None:
    mb = settings.ref_bonus_gb * MB_IN_GB
    ref.status = "credited"
    ref.bonus_mb = mb
    ref.credited_at = utcnow()
    await add_bonus_mb(session, referrer, mb)


async def is_first_paid_subscription(session: AsyncSession, user_id: int, current_payment_id: int) -> bool:
    count = await session.scalar(
        select(func.count(Payment.id)).where(
            Payment.user_id == user_id,
            Payment.status == "paid",
            Payment.kind == "subscription",
            Payment.id != current_payment_id,
        )
    )
    return not count


def _mask(user: User | None) -> str:
    if user is None:
        return "—"
    if user.username:
        u = user.username
        return "@" + (u[:3] + "•••" if len(u) > 4 else u[:1] + "•••")
    name = user.first_name or "Пользователь"
    return name[:1] + "•••"


async def stats(session: AsyncSession, user_id: int, rng: str, now: datetime | None = None) -> dict:
    now = now or utcnow()
    refs = (await session.scalars(select(Referral).where(Referral.referrer_id == user_id))).all()
    total = len(refs)
    credited_mb = sum(r.bonus_mb for r in refs if r.status == "credited")

    if rng == "months":
        buckets = []
        y, m = now.year, now.month
        for i in range(5, -1, -1):
            yy, mm = y, m - i
            while mm <= 0:
                mm += 12
                yy -= 1
            buckets.append(((yy, mm), MONTHS_RU[mm - 1]))
        counts = {k: 0 for k, _ in buckets}
        for r in refs:
            k = (r.created_at.year, r.created_at.month)
            if k in counts:
                counts[k] += 1
        caption = "За 6 месяцев"
    elif rng == "years":
        years = list(range(now.year - 3, now.year + 1))
        buckets = [(yy, str(yy)) for yy in years]
        counts = {yy: 0 for yy in years}
        for r in refs:
            if r.created_at.year in counts:
                counts[r.created_at.year] += 1
        caption = "По годам"
    else:
        days = [(now - timedelta(days=i)).date() for i in range(6, -1, -1)]
        buckets = [(d, DAYS_RU[d.weekday()]) for d in days]
        counts = {d: 0 for d in days}
        for r in refs:
            d = r.created_at.date()
            if d in counts:
                counts[d] += 1
        caption = "За 7 дней"

    chart = [{"label": label, "value": counts[k]} for k, label in buckets]
    recent = sorted(refs, key=lambda r: r.created_at, reverse=True)[:5]
    invitees = {u.id: u for u in (await session.scalars(select(User).where(User.id.in_([r.invitee_id for r in recent])))).all()} if recent else {}
    return {
        "total": total,
        "credited_gb": credited_mb // MB_IN_GB,
        "caption": caption,
        "chart": chart,
        "recent": [
            {
                "name": _mask(invitees.get(r.invitee_id)),
                "created_at": r.created_at.isoformat() + "Z",
                "status": r.status,
                "gb": r.bonus_mb // MB_IN_GB,
            }
            for r in recent
        ],
    }
