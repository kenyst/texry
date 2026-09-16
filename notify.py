"""Интерфейс VPN-провайдера. Сейчас — заглушка: ключи не выдаются.

На следующем этапе сюда подключается реальная панель (Marzban / 3x-ui / Remnawave и т.п.):
реализуйте VPNProvider и замените get_provider(). Остальной код трогать не нужно —
подписка, устройства и трафик уже хранятся в БД и привязаны к Telegram-аккаунту.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass
class VPNAccess:
    key: str  # vless://... или ключ подписки
    sub_url: str | None
    expires_at: datetime
    devices: int


class VPNProvider(Protocol):
    enabled: bool

    async def ensure_access(self, user_id: int, expires_at: datetime, devices: int, traffic_limit_mb: int) -> VPNAccess | None: ...
    async def reissue(self, user_id: int) -> VPNAccess | None: ...
    async def revoke(self, user_id: int) -> None: ...
    async def devices(self, user_id: int) -> list[dict]: ...
    async def traffic_used_mb(self, user_id: int) -> int: ...


class NullProvider:
    enabled = False

    async def ensure_access(self, user_id, expires_at, devices, traffic_limit_mb):
        return None

    async def reissue(self, user_id):
        return None

    async def revoke(self, user_id):
        return None

    async def devices(self, user_id):
        return []

    async def traffic_used_mb(self, user_id):
        return 0


_provider: VPNProvider = NullProvider()


def get_provider() -> VPNProvider:
    return _provider
