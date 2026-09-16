from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import utcnow
from app.models import Subscription, User
from app.pricing import PLANS_BY_ID

MB_IN_GB = 1024
DAYS_IN_MONTH = 30


async def get_active(session: AsyncSession, user_id: int, now: datetime | None = None) -> Subscription | None:
    sub = await session.get(Subscription, user_id)
    return sub if sub and sub.is_active(now) else None


def status_payload(sub: Subscription | None, now: datetime | None = None) -> dict:
    """Всё по нулям, пока подписки нет."""
    now = now or utcnow()
    if not sub or not sub.is_active(now):
        return {
            "active": False,
            "expired": bool(sub),
            "plan_id": None,
            "devices": 0,
            "gb_left": 0,
            "gb_total": 0,
            "days_left": 0,
            "expires_at": sub.expires_at.isoformat() + "Z" if sub else None,
            "energy_x2": False,
            "key_ready": False,
        }
    left_mb = max(0, sub.traffic_limit_mb - sub.traffic_used_mb)
    seconds_left = (sub.expires_at - now).total_seconds()
    return {
        "active": True,
        "expired": False,
        "plan_id": sub.plan_id,
        "devices": sub.devices,
        "gb_left": round(left_mb / MB_IN_GB, 1) if left_mb % MB_IN_GB else left_mb // MB_IN_GB,
        "gb_total": sub.traffic_limit_mb // MB_IN_GB,
        "days_left": max(0, int(-(-seconds_left // 86400))),  # округление вверх
        "expires_at": sub.expires_at.isoformat() + "Z",
        "energy_x2": sub.energy_x2,
        "key_ready": bool(sub.vpn_key or sub.sub_url),
    }


async def extend(
    session: AsyncSession,
    user: User,
    settings: Settings,
    *,
    plan_id: str,
    months: int,
    devices: int,
    bonus_gb: int = 0,
    energy_x2: bool | None = None,
) -> Subscription:
    """Продлевает (или создаёт) подписку. Если подписка активна — срок добавляется к текущей дате окончания."""
    now = utcnow()
    sub = await session.get(Subscription, user.id, with_for_update=True)
    add_mb = (months * settings.gb_per_month + bonus_gb) * MB_IN_GB
    if sub is None:
        sub = Subscription(user_id=user.id, plan_id=plan_id, devices=devices, started_at=now, expires_at=now,
                           traffic_limit_mb=0, traffic_used_mb=0, energy_x2=False)
        session.add(sub)
    if not sub.is_active(now):
        # истекшая подписка начинается заново, остаток трафика сгорает
        sub.started_at = now
        sub.expires_at = now
        sub.traffic_limit_mb = 0
        sub.traffic_used_mb = 0
        sub.energy_x2 = False
    sub.expires_at = sub.expires_at + timedelta(days=months * DAYS_IN_MONTH)
    sub.traffic_limit_mb += add_mb
    sub.plan_id = plan_id
    sub.devices = max(1, devices)
    plan = PLANS_BY_ID.get(plan_id)
    if energy_x2 is None:
        energy_x2 = bool(plan and plan.energy_x2)
    sub.energy_x2 = sub.energy_x2 or energy_x2
    # накопленные бонусы (рефералы, промокоды) применяются при активации
    if user.pending_bonus_mb:
        sub.traffic_limit_mb += user.pending_bonus_mb
        user.pending_bonus_mb = 0
    return sub


async def add_bonus_mb(session: AsyncSession, user: User, mb: int) -> bool:
    """True — добавлено к активной подписке, False — отложено до оплаты."""
    sub = await session.get(Subscription, user.id, with_for_update=True)
    if sub and sub.is_active():
        sub.traffic_limit_mb += mb
        return True
    user.pending_bonus_mb = (user.pending_bonus_mb or 0) + mb
    return False
