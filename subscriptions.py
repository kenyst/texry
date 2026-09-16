"""Texry Abuse — тапалка. Всё считается на сервере: клиент присылает только число тапов
за последний интервал, сервер ограничивает скорость (token bucket), энергию и x2."""
from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import utcnow
from app.models import PromoCode, Subscription, TapState, User
from app.security.risk import W_FAST_TAPS, random_code
from app.services.subscriptions import add_bonus_mb

MAX_BATCH = 200


def _today(settings: Settings) -> str:
    return datetime.now(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d")


async def get_state(session: AsyncSession, user_id: int, settings: Settings, lock: bool = False) -> TapState:
    st = await session.get(TapState, user_id, with_for_update=lock)
    if st is None:
        st = TapState(user_id=user_id, day="", energy_used=0, score=0, total_taps=0, x2_left=0, promos=0,
                      tokens=settings.tap_burst, last_batch_at=0.0, suspicious_batches=0)
        session.add(st)
    today = _today(settings)
    if st.day != today:
        st.day = today
        st.energy_used = 0
    return st


def energy_max(sub: Subscription | None, settings: Settings) -> int:
    return settings.tap_energy_max * (2 if sub and sub.energy_x2 else 1)


def payload(st: TapState, sub: Subscription | None, settings: Settings) -> dict:
    emax = energy_max(sub, settings)
    return {
        "score": st.score,
        "energy": max(0, emax - st.energy_used),
        "energy_max": emax,
        "x2_left": st.x2_left,
        "per_tap": 2 if st.x2_left > 0 else 1,
        "promos": st.promos,
        "goal": settings.tap_goal_mb,
        "to_promo": max(0, settings.tap_goal_mb - st.score) if st.score < settings.tap_goal_mb else 0,
        "can_claim": st.score >= settings.tap_goal_mb,
        "x2_price": settings.x2_price_rub,
        "x2_taps": settings.x2_taps,
    }


def apply_taps(st: TapState, count: int, emax: int, settings: Settings, now: float | None = None) -> int:
    """Возвращает число засчитанных тапов."""
    now = now if now is not None else time.time()
    count = max(0, min(int(count), MAX_BATCH))
    elapsed = 0.0 if not st.last_batch_at else max(0.0, min(now - st.last_batch_at, 60.0))
    tokens = min(settings.tap_burst, (st.tokens if st.last_batch_at else settings.tap_burst) + elapsed * settings.tap_max_rate)
    energy_left = max(0, emax - st.energy_used)
    allowed = int(max(0, min(count, tokens, energy_left)))
    if count > allowed and allowed == int(tokens) and count - allowed > settings.tap_max_rate:
        st.suspicious_batches = (st.suspicious_batches or 0) + 1
    x2 = min(allowed, st.x2_left)
    st.tokens = tokens - allowed
    st.last_batch_at = now
    st.energy_used += allowed
    st.x2_left -= x2
    st.score += allowed + x2
    st.total_taps += allowed
    return allowed


async def tap(session: AsyncSession, user: User, sub: Subscription, count: int, settings: Settings) -> dict:
    st = await get_state(session, user.id, settings, lock=True)
    apply_taps(st, count, energy_max(sub, settings), settings)
    if st.suspicious_batches >= 20:
        user.add_flag("fast_taps", W_FAST_TAPS)
    return payload(st, sub, settings)


async def claim(session: AsyncSession, user: User, sub: Subscription, settings: Settings) -> tuple[dict, PromoCode | None]:
    st = await get_state(session, user.id, settings, lock=True)
    if st.score < settings.tap_goal_mb:
        return payload(st, sub, settings), None
    st.score -= settings.tap_goal_mb
    st.promos += 1
    promo = PromoCode(code="TAP-" + random_code(8), user_id=user.id, source="tapper", kind="mb", value=settings.tap_reward_mb)
    session.add(promo)
    await add_bonus_mb(session, user, settings.tap_reward_mb)
    promo.applied_at = utcnow()
    return payload(st, sub, settings), promo
