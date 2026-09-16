"""Отправка уведомлений в Telegram. Сервисы вызывают notify_*, бот подставляется при старте."""
from __future__ import annotations

import logging
from typing import Protocol

log = logging.getLogger(__name__)


class _Sender(Protocol):
    async def send_message(self, chat_id: int, text: str, **kwargs): ...


_bot: _Sender | None = None
_admins: list[int] = []
outbox: list[tuple[int, str]] = []  # для тестов


def setup(bot: _Sender | None, admin_ids: list[int]) -> None:
    global _bot, _admins
    _bot = bot
    _admins = list(admin_ids)


async def notify_user(user_id: int, text: str, **kwargs) -> None:
    outbox.append((user_id, text))
    if _bot is None:
        return
    try:
        await _bot.send_message(user_id, text, **kwargs)
    except Exception as e:  # пользователь мог заблокировать бота
        log.info("notify_user %s failed: %s", user_id, e)


async def notify_admins(text: str) -> None:
    for admin_id in _admins:
        await notify_user(admin_id, text)
