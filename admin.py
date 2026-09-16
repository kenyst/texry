"""OAuth-подключение аккаунта DonationAlerts. Ссылку выдаёт админ-команда /da в боте."""
from __future__ import annotations

import html

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import Settings, get_settings
from app.payments import donationalerts as da
from app.services.notify import notify_admins

router = APIRouter(prefix="/da")


def _page(title: str, text: str) -> HTMLResponse:
    body = f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<body style="margin:0;min-height:100vh;display:grid;place-items:center;background:#212842;color:#F0E7D5;font:16px/1.5 system-ui,sans-serif;padding:16px">
<div style="max-width:420px;text-align:center"><h1 style="font-size:22px">{html.escape(title)}</h1><p style="opacity:.75">{html.escape(text)}</p></div>"""
    return HTMLResponse(body)


@router.get("/login")
async def login(s: str, settings: Settings = Depends(get_settings)):
    admin_id = da.verify_state(s, settings.secret_key)
    if admin_id is None or admin_id not in settings.admin_ids:
        return _page("Ссылка устарела", "Запросите новую командой /da в боте.")
    if not settings.da_client_id:
        return _page("DonationAlerts не настроен", "Заполните DA_CLIENT_ID и DA_CLIENT_SECRET в .env.")
    return RedirectResponse(da.authorize_url(settings, s))


@router.get("/callback")
async def callback(code: str = "", state: str = "", error: str = "", settings: Settings = Depends(get_settings)):
    admin_id = da.verify_state(state, settings.secret_key)
    if error or not code or admin_id is None or admin_id not in settings.admin_ids:
        return _page("Не удалось подключить", "Попробуйте ещё раз командой /da в боте.")
    try:
        await da.exchange_code(settings, code)
    except Exception:
        return _page("Ошибка DonationAlerts", "Проверьте DA_CLIENT_ID, DA_CLIENT_SECRET и Redirect URI приложения.")
    await notify_admins("DonationAlerts подключён. Платежи проверяются автоматически.")
    return _page("Готово", "DonationAlerts подключён. Окно можно закрыть.")
