"""Точка входа: FastAPI (API + статика мини-аппа) + бот aiogram (polling) + поллер DonationAlerts.

Запуск: uvicorn app.main:app --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import db
from app.api import da_oauth, routes
from app.config import get_settings
from app.services import notify

log = logging.getLogger("texry")
WEBAPP_DIR = Path(__file__).resolve().parent.parent / "webapp"
WEBHOOK_PATH = "/tg/webhook"
_runtime: dict = {}  # bot и dispatcher для webhook-режима


async def _start_bot(settings, stop: asyncio.Event) -> list[asyncio.Task]:
    from aiogram import Bot, Dispatcher
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from aiogram.types import BotCommand, MenuButtonWebApp, WebAppInfo

    from app.bot import admin, handlers

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    if not settings.bot_username:
        settings.bot_username = me.username or ""
    notify.setup(bot, settings.admin_ids)

    async def setup_menu() -> None:
        try:
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Открыть", web_app=WebAppInfo(url=settings.webapp_url_clean)))
            await bot.set_my_commands([BotCommand(command="start", description="Открыть Texry"), BotCommand(command="help", description="Помощь")])
        except Exception as e:  # не критично
            log.warning("menu setup failed: %s", e)

    await setup_menu()

    dp = Dispatcher()
    admin.setup_filters()
    dp.include_router(admin.router)
    dp.include_router(handlers.router)

    tasks: list[asyncio.Task] = []
    if settings.bot_mode == "webhook":
        # Telegram сам будит сервис запросом — подходит для бесплатных облаков со «сном»
        url = settings.base_url.rstrip("/") + WEBHOOK_PATH
        info = await bot.get_webhook_info()
        if info.url != url:
            await bot.set_webhook(url=url, secret_token=settings.webhook_secret,
                                  allowed_updates=dp.resolve_used_update_types())
        _runtime["bot"], _runtime["dp"] = bot, dp

        async def closer():
            await stop.wait()
            await bot.session.close()

        tasks.append(asyncio.create_task(closer()))
        log.info("bot @%s started (webhook %s), mini app: %s", settings.bot_username, url, settings.webapp_url_clean)
    else:
        async def polling():
            try:
                await bot.delete_webhook(drop_pending_updates=False)  # иначе getUpdates конфликтует с вебхуком
                await dp.start_polling(bot, handle_signals=False, allowed_updates=dp.resolve_used_update_types())
            finally:
                await bot.session.close()

        async def stopper():
            await stop.wait()
            await dp.stop_polling()

        tasks += [asyncio.create_task(polling()), asyncio.create_task(stopper())]
        log.info("bot @%s started (polling), mini app: %s", settings.bot_username, settings.webapp_url_clean)

    if settings.tunnel_metrics_url:
        from app import tunnel

        async def on_tunnel_change():
            await setup_menu()
            if settings.bot_mode == "webhook":
                await bot.set_webhook(url=settings.base_url.rstrip("/") + WEBHOOK_PATH, secret_token=settings.webhook_secret,
                                      allowed_updates=dp.resolve_used_update_types())
            await notify.notify_admins(
                f"Адрес мини-аппа сменился: {settings.webapp_url_clean}\n"
                "Кнопка меню обновлена. Старые кнопки в сообщениях больше не работают — открывайте через «Открыть»."
            )

        tasks.append(asyncio.create_task(tunnel.watch(settings, on_tunnel_change, stop)))
    return tasks


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # без записи каждого опроса DonationAlerts/туннеля
    if not settings.dev_mode and (len(settings.secret_key) < 16 or settings.secret_key == "change-me"):
        raise RuntimeError("SECRET_KEY слишком короткий: задайте случайную строку от 16 символов (лучше 40+)")
    if settings.dev_mode:
        log.warning("DEV_MODE включён: авторизация без Telegram разрешена. Никогда не включайте его на проде!")
    if settings.tunnel_metrics_url:
        from app import tunnel

        log.info("test mode: waiting for Cloudflare tunnel at %s", settings.tunnel_metrics_url)
        if not await tunnel.wait_for_hostname(settings):
            # не падаем: адрес подхватится, как только cloudflared подключится
            log.error("Cloudflare tunnel did not report an address yet — is cloudflared running with --metrics?")
            settings.base_url = settings.base_url or "http://127.0.0.1:8080"
            settings.webapp_url = settings.webapp_url or "http://127.0.0.1:8080/app/"
    if not settings.base_url or not settings.webapp_url:
        raise RuntimeError("Заполните BASE_URL и WEBAPP_URL в .env (или задайте TUNNEL_METRICS_URL для тестового режима)")
    db.init_engine(settings.database_url)
    await db.create_all()

    stop = asyncio.Event()
    tasks: list[asyncio.Task] = []
    if not (settings.dev_mode and settings.bot_token == "dev"):  # BOT_TOKEN=dev — локальная вёрстка без бота
        try:
            tasks += await _start_bot(settings, stop)
        except Exception as e:
            if not settings.dev_mode:
                raise
            log.warning("bot not started in dev mode: %s", e)

    from app.payments.donationalerts import run_poller

    tasks.append(asyncio.create_task(run_poller(settings, stop)))
    try:
        yield
    finally:
        stop.set()
        for t in tasks:
            try:
                await asyncio.wait_for(t, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                t.cancel()
        await db.dispose()


app = FastAPI(title="Texry", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(routes.router)
app.include_router(da_oauth.router)


@app.exception_handler(RequestValidationError)
async def _validation(_: Request, __: RequestValidationError):
    return JSONResponse(status_code=422, content={"detail": {"error": "validation", "message": "Некорректный запрос."}})


@app.middleware("http")
async def _headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/app"):
        response.headers["Cache-Control"] = "no-cache"
    if path.startswith("/api"):
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    import hmac

    settings = get_settings()
    token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if settings.bot_mode != "webhook" or not hmac.compare_digest(token, settings.webhook_secret):
        return JSONResponse(status_code=403, content={"ok": False})
    bot, dp = _runtime.get("bot"), _runtime.get("dp")
    if bot is None:
        return JSONResponse(status_code=503, content={"ok": False})
    from aiogram.types import Update

    try:
        update = Update.model_validate(await request.json(), context={"bot": bot})
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False})
    # обрабатываем прямо в запросе: сервис не уснёт, пока ответ не отправлен
    try:
        await dp.feed_update(bot, update)
    except Exception:
        log.exception("update %s failed", update.update_id)
    return {"ok": True}


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/")
async def root():
    return RedirectResponse("/app/")


app.mount("/app", StaticFiles(directory=WEBAPP_DIR, html=True), name="webapp")
