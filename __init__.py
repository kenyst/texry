from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import utcnow
from app.models import Referral, User
from app.security.risk import (
    W_PHONE_REUSE,
    PhoneRejected,
    check_phone_allowed,
    initial_risk,
    normalize_phone,
    phone_hash,
    random_code,
)


def parse_ref(start_param: str | None) -> str | None:
    if not start_param:
        return None
    p = start_param.strip()
    if p.startswith("ref_"):
        p = p[4:]
    return p.upper() if 4 <= len(p) <= 16 and p.isalnum() else None


async def _unique_ref_code(session: AsyncSession) -> str:
    while True:
        code = random_code(8)
        if not await session.scalar(select(User.id).where(User.ref_code == code)):
            return code


async def get_or_create_user(
    session: AsyncSession,
    tg: dict,
    settings: Settings,
    start_param: str | None = None,
    has_photo: bool | None = None,
    from_bot: bool = False,
) -> tuple[User, bool]:
    """Создаёт пользователя при первом входе. Реферер привязывается ТОЛЬКО при создании —
    сменить его потом нельзя, поэтому нельзя «перепривязать» старый аккаунт к своей ссылке."""
    user = await session.get(User, tg["id"], with_for_update=True)
    now = utcnow()
    created = False
    if user is None:
        created = True
        score, flags = initial_risk(tg["id"], tg.get("username"), has_photo, bool(tg.get("is_premium")), settings)
        user = User(
            id=tg["id"],
            username=tg.get("username"),
            first_name=(tg.get("first_name") or "")[:128],
            last_name=tg.get("last_name"),
            photo_url=tg.get("photo_url"),
            language=tg.get("language_code"),
            is_premium=bool(tg.get("is_premium")),
            has_photo=has_photo if has_photo is not None else (bool(tg.get("photo_url")) if "photo_url" in tg else None),
            ref_code=await _unique_ref_code(session),
            risk_score=score,
            risk_flags=",".join(flags),
            started_bot=from_bot,
            created_at=now,
            last_seen_at=now,
        )
        session.add(user)
        await session.flush()

        ref_code = parse_ref(start_param)
        if ref_code:
            referrer = await session.scalar(select(User).where(User.ref_code == ref_code))
            if referrer and referrer.id != user.id and not referrer.is_banned:
                user.referred_by_id = referrer.id
                session.add(Referral(referrer_id=referrer.id, invitee_id=user.id, status="joined"))
    else:
        user.username = tg.get("username")
        user.first_name = (tg.get("first_name") or user.first_name)[:128]
        user.last_name = tg.get("last_name")
        if tg.get("photo_url"):
            user.photo_url = tg["photo_url"]
        user.is_premium = bool(tg.get("is_premium"))
        if has_photo is not None:
            user.has_photo = has_photo
        user.last_seen_at = now
        if from_bot:
            user.started_bot = True
    return user, created


async def bind_phone(session: AsyncSession, user: User, raw_phone: str, settings: Settings) -> None:
    """Один номер — один аккаунт. Бросает PhoneRejected(reason)."""
    if user.phone_verified:
        return
    digits = normalize_phone(raw_phone)
    check_phone_allowed(digits, settings)
    h = phone_hash(digits, settings.secret_key)
    owner = await session.scalar(select(User).where(User.phone_hash == h))
    if owner and owner.id != user.id:
        user.add_flag("phone_reuse", W_PHONE_REUSE)
        owner.add_flag("phone_shared", 1)
        raise PhoneRejected("phone_taken")
    user.phone_hash = h
    user.phone_tail = digits[-4:]
    user.phone_verified_at = utcnow()


async def referrals_last_day(session: AsyncSession, referrer_id: int) -> int:
    since = utcnow() - timedelta(days=1)
    return int(
        await session.scalar(
            select(func.count(Referral.id)).where(Referral.referrer_id == referrer_id, Referral.created_at >= since)
        )
        or 0
    )


PHONE_REASON_TEXT = {
    "bad_format": "Не удалось распознать номер.",
    "virtual_number": "Виртуальные и анонимные номера не принимаются.",
    "phone_taken": "Этот номер уже привязан к другому аккаунту.",
    "not_own": "Можно привязать только собственный номер.",
}

__all__ = ["get_or_create_user", "bind_phone", "parse_ref", "PhoneRejected", "PHONE_REASON_TEXT", "referrals_last_day"]
