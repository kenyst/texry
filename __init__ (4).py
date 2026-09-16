from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.models import Subscription, User
from app.security.initdata import InvalidInitData, parse_init_data
from app.security.ratelimit import limiter
from app.services import subscriptions as sub_service
from app.services.users import get_or_create_user


class ApiError(HTTPException):
    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(status_code=status, detail={"error": code, "message": message})


@dataclass
class Ctx:
    user: User
    session: AsyncSession
    settings: Settings
    start_param: str | None


async def current(
    request: Request,
    authorization: str = Header(default=""),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Ctx:
    scheme, _, value = authorization.partition(" ")
    if scheme == "tma":
        try:
            data = parse_init_data(value, settings.bot_token, settings.init_data_max_age)
        except InvalidInitData:
            raise ApiError(401, "unauthorized", "Откройте приложение из Telegram.")
        tg, start_param = data["user"], data["start_param"]
    elif scheme == "dev" and settings.dev_mode and value.isdigit():
        existing = await session.get(User, int(value))
        tg = {"id": int(value), "first_name": existing.first_name if existing else "Dev",
              "last_name": existing.last_name if existing else None,
              "username": existing.username if existing else f"dev{value}"}
        start_param = None
    else:
        raise ApiError(401, "unauthorized", "Откройте приложение из Telegram.")

    ip = request.client.host if request.client else "?"
    if not limiter.hit(f"api:{tg['id']}", 240, 60) or not limiter.hit(f"ip:{ip}", 1200, 60):
        raise ApiError(429, "rate_limited", "Слишком много запросов, подождите немного.")

    user, _ = await get_or_create_user(session, tg, settings, start_param=start_param)
    if user.is_banned:
        await session.commit()
        raise ApiError(403, "banned", "Аккаунт заблокирован. Напишите в поддержку.")
    return Ctx(user=user, session=session, settings=settings, start_param=start_param)


async def require_sub(ctx: Ctx) -> Subscription:
    sub = await sub_service.get_active(ctx.session, ctx.user.id)
    if not sub:
        raise ApiError(402, "subscription_required", "Доступно после оформления подписки.")
    return sub


def throttle(ctx: Ctx, name: str, limit: int, window: float) -> None:
    if not limiter.hit(f"{name}:{ctx.user.id}", limit, window):
        raise ApiError(429, "rate_limited", "Слишком часто. Подождите немного.")
