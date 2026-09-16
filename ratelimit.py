"""Тестовый режим через Cloudflare Quick Tunnel (trycloudflare.com).

cloudflared, запущенный с `--metrics <адрес>`, отдаёт текущий публичный адрес
на `GET /quicktunnel` → {"hostname": "xxx.trycloudflare.com"}.
Адрес меняется при каждом перезапуске cloudflared, поэтому приложение само
подхватывает новый адрес и обновляет кнопку меню бота — править .env не нужно.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from app.config import Settings

log = logging.getLogger(__name__)


async def fetch_hostname(client: httpx.AsyncClient, metrics_url: str) -> str | None:
    try:
        r = await client.get(metrics_url.rstrip("/") + "/quicktunnel", timeout=5)
        r.raise_for_status()
        host = (r.json().get("hostname") or "").strip()
    except (httpx.HTTPError, ValueError):
        return None
    return host or None


def apply_hostname(settings: Settings, host: str) -> bool:
    """Записывает адрес в настройки. True — адрес изменился."""
    base = f"https://{host}"
    changed = settings.base_url != base
    settings.base_url = base
    settings.webapp_url = base + "/app/"
    return changed


async def wait_for_hostname(settings: Settings, timeout: float = 120, client: httpx.AsyncClient | None = None) -> str | None:
    own = client is None
    client = client or httpx.AsyncClient()
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            host = await fetch_hostname(client, settings.tunnel_metrics_url)
            if host:
                apply_hostname(settings, host)
                log.info("tunnel address: https://%s", host)
                return host
            await asyncio.sleep(2)
        return None
    finally:
        if own:
            await client.aclose()


async def watch(settings: Settings, on_change, stop: asyncio.Event, interval: float = 20) -> None:
    """Следит за сменой адреса туннеля и вызывает on_change() после обновления настроек."""
    async with httpx.AsyncClient() as client:
        while not stop.is_set():
            host = await fetch_hostname(client, settings.tunnel_metrics_url)
            if host and apply_hostname(settings, host):
                log.warning("tunnel address changed: https://%s", host)
                try:
                    await on_change()
                except Exception as e:  # не роняем приложение из-за Telegram API
                    log.warning("tunnel on_change failed: %s", e)
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
