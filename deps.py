"""Настройки приложения. Всё читается из переменных окружения / файла .env."""
from __future__ import annotations

import hashlib
import os
from functools import lru_cache
from typing import Annotated

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Telegram ---
    bot_token: str
    bot_username: str = ""  # заполняется автоматически через getMe, если пусто
    webapp_url: str = ""  # публичный HTTPS-адрес мини-аппа, например https://texry.app/app/
    base_url: str = ""  # публичный HTTPS-адрес сервера, например https://texry.app
    # Тестовый режим: адрес берётся из Cloudflare Quick Tunnel автоматически
    # (например http://127.0.0.1:20241). Тогда BASE_URL и WEBAPP_URL можно не заполнять.
    tunnel_metrics_url: str = ""
    # polling — бот сам опрашивает Telegram (сервер/компьютер);
    # webhook — Telegram присылает обновления на BASE_URL/tg/webhook (бесплатные облака, которые «засыпают»)
    bot_mode: str = "polling"
    admin_ids: Annotated[list[int], NoDecode] = []
    support_username: str = "texry_support"
    brand: str = "Texry"

    # --- Инфраструктура ---
    database_url: str = "sqlite+aiosqlite:///./data/texry.db"
    secret_key: str  # длинная случайная строка: подпись state, хеш телефонов, сид игр
    timezone: str = "Europe/Moscow"
    init_data_max_age: int = 86400  # сек, сколько живёт initData
    dev_mode: bool = False  # ТОЛЬКО для локальной разработки: авторизация заголовком "dev <id>"

    # --- DonationAlerts ---
    da_client_id: str = ""
    da_client_secret: str = ""
    da_page_url: str = ""  # https://www.donationalerts.com/r/<ваш_ник>
    da_access_token: str = ""  # необязательно: токен вручную вместо OAuth
    da_poll_interval: int = 10  # сек; лимит API — 60 запросов/мин
    da_currency: str = "RUB"
    payment_ttl_minutes: int = 60
    payment_grace_hours: int = 24  # донат с кодом просроченного платежа всё ещё засчитывается
    min_payment_rub: int = 10

    # --- Антиабуз ---
    blocked_phone_prefixes: Annotated[list[str], NoDecode] = ["888"]  # 888 — анонимные номера Fragment
    new_account_id_threshold: int = 8_000_000_000  # ID выше — «свежий» аккаунт (приблизительно)
    risk_hold_score: int = 3  # с такого балла реферал уходит на ручную проверку
    ref_max_per_day: int = 20  # больше рефералов в сутки у одного пользователя — на проверку
    tap_max_rate: float = 12.0  # тапов в секунду (человеческий предел ~10–12)
    tap_burst: float = 36.0
    game_min_move_interval: float = 0.12  # сек между ходами

    # --- Продукт ---
    gb_per_month: int = 100
    ref_bonus_gb: int = 5
    tap_energy_max: int = 5000
    tap_goal_mb: int = 500
    tap_reward_mb: int = 500
    x2_price_rub: int = 49
    x2_taps: int = 500
    game_goal: int = 1_000_000
    game_reward_months: int = 3
    game_reward_limit: int = 1  # сколько раз за всё время аккаунт может получить награду игры
    max_devices: int = 10

    @field_validator("bot_mode", mode="before")
    @classmethod
    def _parse_mode(cls, v):
        v = (v or "polling").strip().lower()
        if v not in {"polling", "webhook"}:
            raise ValueError("BOT_MODE должен быть polling или webhook")
        return v

    @model_validator(mode="after")
    def _platform_urls(self):
        """Koyeb и Render сами сообщают публичный адрес сервиса — подставляем его, если BASE_URL не задан."""
        if not self.base_url:
            koyeb = os.environ.get("KOYEB_PUBLIC_DOMAIN", "").strip()
            render = os.environ.get("RENDER_EXTERNAL_URL", "").strip()
            if koyeb:
                self.base_url = "https://" + koyeb.removeprefix("https://").rstrip("/")
            elif render:
                self.base_url = render.rstrip("/")
        if self.base_url and not self.webapp_url:
            self.webapp_url = self.base_url.rstrip("/") + "/app/"
        return self

    @property
    def webhook_secret(self) -> str:
        return hashlib.sha256(f"tg-webhook:{self.secret_key}".encode()).hexdigest()

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _parse_ids(cls, v):
        if isinstance(v, str):
            return [int(x) for x in v.replace(" ", "").split(",") if x]
        return v

    @field_validator("blocked_phone_prefixes", mode="before")
    @classmethod
    def _parse_prefixes(cls, v):
        if isinstance(v, str):
            return [x.strip().lstrip("+") for x in v.split(",") if x.strip()]
        return v

    @property
    def webapp_url_clean(self) -> str:
        return self.webapp_url.rstrip("/") + "/"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
