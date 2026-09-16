from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import ApiError, Ctx, current, require_sub, throttle
from app.db import user_lock
from app.models import Payment
from app.pricing import PLANS_BY_ID, public_config
from app.security.initdata import InvalidInitData, parse_contact_response
from app.services import blockgame, referrals, tapper
from app.services import payments as payment_service
from app.services import subscriptions as sub_service
from app.services.users import PHONE_REASON_TEXT, PhoneRejected, bind_phone
from app.services.vpn import get_provider

router = APIRouter(prefix="/api")


def _initials(first: str, last: str | None, username: str | None) -> str:
    parts = [p for p in [first, last] if p]
    if len(parts) >= 2:
        return (parts[0][:1] + parts[1][:1]).upper()
    if parts:
        return parts[0][:2].upper()
    return (username or "??")[:2].upper()


async def _me_payload(ctx: Ctx) -> dict:
    s, u, st = ctx.session, ctx.user, ctx.settings
    sub = await s.get(sub_service.Subscription, u.id)
    status = sub_service.status_payload(sub)
    active = status["active"]
    pending = await s.scalar(
        select(Payment).where(Payment.user_id == u.id, Payment.status.in_(["pending", "underpaid"])).order_by(Payment.id.desc())
    )
    tap_payload = None
    if active:
        tap_state = await tapper.get_state(s, u.id, st)
        tap_payload = tapper.payload(tap_state, sub, st)
    bot = st.bot_username
    return {
        "user": {
            "id": u.id,
            "first_name": u.first_name,
            "last_name": u.last_name,
            "username": u.username,
            "photo_url": u.photo_url,
            "initials": _initials(u.first_name, u.last_name, u.username),
            "phone_verified": u.phone_verified,
            "phone_tail": u.phone_tail,
        },
        "subscription": status,
        "plan_title": PLANS_BY_ID[sub.plan_id].title if active and sub and sub.plan_id in PLANS_BY_ID else None,
        "locked": not active,
        "tapper": tap_payload,
        "ref_link": f"https://t.me/{bot}?start=ref_{u.ref_code}" if active and bot else None,
        "pending_payment": payment_service.payment_payload(pending, st) if pending else None,
        "vpn_enabled": get_provider().enabled,
    }


@router.get("/config")
async def config(ctx: Ctx = Depends(current)):
    st = ctx.settings
    await ctx.session.commit()
    return {
        **public_config(st.max_devices),
        "bot_username": st.bot_username,
        "support_username": st.support_username,
        "brand": st.brand,
        "ref_bonus_gb": st.ref_bonus_gb,
        "gb_per_month": st.gb_per_month,
        "payments_configured": bool(st.da_page_url),
        "tap_goal_mb": st.tap_goal_mb,
        "game_goal": st.game_goal,
        "game_reward_months": st.game_reward_months,
        "x2_price": st.x2_price_rub,
        "x2_taps": st.x2_taps,
    }


@router.get("/me")
async def me(ctx: Ctx = Depends(current)):
    payload = await _me_payload(ctx)
    await ctx.session.commit()
    return payload


# ---------- Привязка телефона ----------

class PhoneIn(BaseModel):
    response: str = Field(max_length=4096)


@router.post("/phone")
async def phone(body: PhoneIn, ctx: Ctx = Depends(current)):
    throttle(ctx, "phone", 5, 300)
    try:
        contact = parse_contact_response(body.response, ctx.settings.bot_token)
    except InvalidInitData:
        raise ApiError(400, "bad_contact", "Не удалось проверить номер. Отправьте его боту.")
    if int(contact.get("user_id") or 0) != ctx.user.id:
        raise ApiError(400, "not_own", PHONE_REASON_TEXT["not_own"])
    async with user_lock(ctx.user.id):
        try:
            await bind_phone(ctx.session, ctx.user, str(contact["phone_number"]), ctx.settings)
        except PhoneRejected as e:
            await ctx.session.commit()  # сохраняем флаги риска
            raise ApiError(409, e.reason, PHONE_REASON_TEXT.get(e.reason, "Номер не принят."))
        await ctx.session.commit()
    return await me(ctx)


# ---------- Платежи ----------

class PaymentIn(BaseModel):
    kind: str = Field(pattern="^(subscription|devices|x2)$")
    plan_id: str | None = None
    devices: int = Field(default=1, ge=1, le=50)


@router.post("/payments")
async def create_payment(body: PaymentIn, ctx: Ctx = Depends(current)):
    throttle(ctx, "pay", 10, 600)
    async with user_lock(ctx.user.id):
        try:
            p = await payment_service.create_payment(ctx.session, ctx.user, ctx.settings, kind=body.kind,
                                                     plan_id=body.plan_id, devices=body.devices)
        except payment_service.PaymentError as e:
            raise ApiError(400, e.code, e.message)
        await ctx.session.commit()
    return payment_service.payment_payload(p, ctx.settings)


def _code(raw: str) -> str:
    return raw.upper().replace("TX-", "").replace("TX", "")[:6]


@router.get("/payments/{code}")
async def payment_status(code: str, ctx: Ctx = Depends(current)):
    p = await ctx.session.scalar(select(Payment).where(Payment.code == _code(code), Payment.user_id == ctx.user.id))
    if not p:
        raise ApiError(404, "not_found", "Платёж не найден.")
    await ctx.session.commit()
    return payment_service.payment_payload(p, ctx.settings)


@router.post("/payments/{code}/cancel")
async def payment_cancel(code: str, ctx: Ctx = Depends(current)):
    async with user_lock(ctx.user.id):
        p = await ctx.session.scalar(select(Payment).where(Payment.code == _code(code), Payment.user_id == ctx.user.id))
        if not p:
            raise ApiError(404, "not_found", "Платёж не найден.")
        if p.status == "pending":
            p.status = "cancelled"
            p.note = "user"
        await ctx.session.commit()
    return payment_service.payment_payload(p, ctx.settings)


# ---------- Ключ / устройства (VPN пока не подключён) ----------

@router.get("/key")
async def key(ctx: Ctx = Depends(current)):
    sub = await require_sub(ctx)
    provider = get_provider()
    devices = await provider.devices(ctx.user.id) if provider.enabled else []
    await ctx.session.commit()
    return {
        "ready": bool(sub.vpn_key or sub.sub_url),
        "key": sub.vpn_key,
        "sub_url": sub.sub_url,
        "expires_at": sub.expires_at.isoformat() + "Z",
        "devices_limit": sub.devices,
        "devices": devices,
        "vpn_enabled": provider.enabled,
    }


@router.post("/key/reissue")
async def key_reissue(ctx: Ctx = Depends(current)):
    await require_sub(ctx)
    throttle(ctx, "reissue", 3, 3600)
    provider = get_provider()
    if not provider.enabled:
        raise ApiError(409, "vpn_not_ready", "Выдача ключей начнётся после запуска серверов.")
    access = await provider.reissue(ctx.user.id)
    sub = await sub_service.get_active(ctx.session, ctx.user.id)
    if access and sub:
        sub.vpn_key, sub.sub_url = access.key, access.sub_url
    await ctx.session.commit()
    return await key(ctx)


# ---------- Рефералы ----------

@router.get("/referrals")
async def referral_stats(range: str = "days", ctx: Ctx = Depends(current)):
    await require_sub(ctx)
    data = await referrals.stats(ctx.session, ctx.user.id, range if range in {"days", "months", "years"} else "days")
    bot = ctx.settings.bot_username
    data["link"] = f"https://t.me/{bot}?start=ref_{ctx.user.ref_code}" if bot else None
    data["bonus_gb"] = ctx.settings.ref_bonus_gb
    await ctx.session.commit()
    return data


# ---------- Texry Abuse (тапалка) ----------

class TapIn(BaseModel):
    count: int = Field(ge=0, le=1000)


@router.get("/tapper")
async def tapper_state(ctx: Ctx = Depends(current)):
    sub = await require_sub(ctx)
    st = await tapper.get_state(ctx.session, ctx.user.id, ctx.settings)
    payload = tapper.payload(st, sub, ctx.settings)
    await ctx.session.commit()
    return payload


@router.post("/tapper/tap")
async def tapper_tap(body: TapIn, ctx: Ctx = Depends(current)):
    sub = await require_sub(ctx)
    throttle(ctx, "tap", 30, 10)
    async with user_lock(ctx.user.id):
        payload = await tapper.tap(ctx.session, ctx.user, sub, body.count, ctx.settings)
        await ctx.session.commit()
    return payload


@router.post("/tapper/claim")
async def tapper_claim(ctx: Ctx = Depends(current)):
    sub = await require_sub(ctx)
    throttle(ctx, "claim", 20, 60)
    async with user_lock(ctx.user.id):
        payload, promo = await tapper.claim(ctx.session, ctx.user, sub, ctx.settings)
        await ctx.session.commit()
    return {**payload, "promo": {"code": promo.code, "mb": promo.value} if promo else None}


# ---------- Игра ----------

class MoveIn(BaseModel):
    piece: int = Field(ge=0, le=2)
    r: int = Field(ge=0, le=7)
    c: int = Field(ge=0, le=7)


@router.get("/game")
async def game_state(ctx: Ctx = Depends(current)):
    await require_sub(ctx)
    gs = await blockgame.load(ctx.session, ctx.user.id)
    payload = await blockgame.payload(ctx.session, gs, ctx.settings)
    await ctx.session.commit()
    return payload


@router.post("/game/move")
async def game_move(body: MoveIn, ctx: Ctx = Depends(current)):
    await require_sub(ctx)
    throttle(ctx, "move", 12, 2)
    async with user_lock(ctx.user.id):
        try:
            payload, rewarded = await blockgame.move(ctx.session, ctx.user, ctx.settings, body.piece, body.r, body.c)
        except blockgame.IllegalMove as e:
            await ctx.session.rollback()
            raise ApiError(409, str(e), "Ход невозможен.")
        await ctx.session.commit()
    return {**payload, "rewarded_now": rewarded}


@router.post("/game/reset")
async def game_reset(ctx: Ctx = Depends(current)):
    await require_sub(ctx)
    throttle(ctx, "reset", 30, 60)
    async with user_lock(ctx.user.id):
        payload = await blockgame.reset(ctx.session, ctx.user, ctx.settings)
        await ctx.session.commit()
    return payload
