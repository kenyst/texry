from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    WebAppInfo,
)

from app.config import get_settings
from app.db import session_scope, user_lock
from app.services.users import PHONE_REASON_TEXT, PhoneRejected, bind_phone, get_or_create_user

log = logging.getLogger(__name__)
router = Router(name="user")


def tg_dict(u) -> dict:
    return {
        "id": u.id,
        "username": u.username,
        "first_name": u.first_name,
        "last_name": u.last_name,
        "language_code": u.language_code,
        "is_premium": bool(u.is_premium),
    }


def open_app_kb(label: str = "Открыть Texry") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, web_app=WebAppInfo(url=get_settings().webapp_url_clean))]])


def phone_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Привязать номер телефона", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Нажмите кнопку ниже",
    )


async def _has_photo(bot: Bot, user_id: int) -> bool | None:
    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
        return photos.total_count > 0
    except Exception:
        return None


@router.message(CommandStart())
async def start(message: Message, command: CommandObject, bot: Bot):
    if message.chat.type != "private" or not message.from_user or message.from_user.is_bot:
        return
    settings = get_settings()
    has_photo = await _has_photo(bot, message.from_user.id)
    async with session_scope() as s:
        user, created = await get_or_create_user(s, tg_dict(message.from_user), settings, start_param=command.args,
                                                 has_photo=has_photo, from_bot=True)
        banned, phone_ok, invited = user.is_banned, user.phone_verified, bool(created and user.referred_by_id)

    if banned:
        await message.answer(f"Аккаунт заблокирован. Поддержка: @{settings.support_username}")
        return

    lines = [
        f"<b>{settings.brand}</b> — безопасный интернет в Telegram.",
        "",
        "Всё управление — в мини-приложении: подписка, ключ, устройства, рефералы и игры.",
    ]
    if invited:
        lines += ["", "Вы пришли по приглашению друга — он получит бонус после вашей первой оплаты."]
    await message.answer("\n".join(lines), reply_markup=open_app_kb())

    if not phone_ok:
        await message.answer(
            "Чтобы защитить акции и промокоды от накруток, подписка привязывается к аккаунту и номеру телефона. "
            "Один номер — один аккаунт. Номер хранится в зашифрованном виде (хеш).",
            reply_markup=phone_kb(),
        )


@router.message(F.contact)
async def contact(message: Message):
    if not message.from_user or message.chat.type != "private":
        return
    settings = get_settings()
    c = message.contact
    if c.user_id != message.from_user.id:
        await message.answer(PHONE_REASON_TEXT["not_own"], reply_markup=phone_kb())
        return
    async with session_scope() as s:
        user, _ = await get_or_create_user(s, tg_dict(message.from_user), settings, from_bot=True)
        async with user_lock(user.id):
            try:
                already = user.phone_verified
                await bind_phone(s, user, c.phone_number, settings)
                text = "Номер уже привязан." if already else "Номер привязан. Теперь можно оформить подписку."
                ok = True
            except PhoneRejected as e:
                text = PHONE_REASON_TEXT.get(e.reason, "Номер не принят.")
                ok = False
    if ok:
        await message.answer(text, reply_markup=ReplyKeyboardRemove())
        await message.answer("Вернитесь в приложение:", reply_markup=open_app_kb())
    else:
        await message.answer(f"{text}\nЕсли это ошибка — напишите @{settings.support_username}.", reply_markup=ReplyKeyboardRemove())


@router.message(F.text == "/app")
async def app_cmd(message: Message):
    await message.answer("Мини-приложение:", reply_markup=open_app_kb())


@router.message(F.text == "/help")
async def help_cmd(message: Message):
    s = get_settings()
    await message.answer(
        "/start — открыть приложение\n/app — кнопка приложения\n\n"
        f"Поддержка: @{s.support_username}",
    )
