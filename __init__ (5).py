from __future__ import annotations

from datetime import timedelta
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy import func, select

from app.config import get_settings
from app.db import session_scope, utcnow
from app.models import Payment, Referral, Subscription, User
from app.payments import donationalerts as da
from app.pricing import PLANS_BY_ID
from app.services import payments as payment_service
from app.services import referrals as referral_service
from app.services import subscriptions as sub_service
from app.services.notify import notify_user

router = Router(name="admin")


def setup_filters() -> None:
    ids = get_settings().admin_ids
    router.message.filter(F.from_user.id.in_(ids))


HELP = """<b>Админ-команды</b>
/stats — статистика
/da — подключение DonationAlerts
/pay_confirm КОД — подтвердить платёж вручную
/grant ID МЕСЯЦЕВ [УСТРОЙСТВ] — выдать подписку
/user ID — карточка пользователя
/ban ID, /unban ID
/holds — рефералы на проверке
/ref_ok ID, /ref_no ID — решение по рефералу"""


@router.message(Command("admin"))
async def admin_help(message: Message):
    await message.answer(HELP)


@router.message(Command("stats"))
async def stats(message: Message):
    now = utcnow()
    day = now - timedelta(days=1)
    async with session_scope() as s:
        users = await s.scalar(select(func.count(User.id)))
        phones = await s.scalar(select(func.count(User.id)).where(User.phone_verified_at.is_not(None)))
        active = await s.scalar(select(func.count(Subscription.user_id)).where(Subscription.expires_at > now))
        paid = await s.scalar(select(func.count(Payment.id)).where(Payment.status == "paid"))
        revenue = await s.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.status == "paid"))
        revenue_day = await s.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.status == "paid", Payment.paid_at >= day))
        holds = await s.scalar(select(func.count(Referral.id)).where(Referral.status == "hold"))
    await message.answer(
        f"Пользователей: {users}\nС номером: {phones}\nАктивных подписок: {active}\n"
        f"Оплат: {paid} на {revenue} ₽ (за сутки {revenue_day} ₽)\nРефералов на проверке: {holds}\n\n"
        f"Мини-апп: {get_settings().webapp_url_clean}"
    )


@router.message(Command("da"))
async def da_cmd(message: Message):
    st = get_settings()
    status = await da.token_status(st)
    state = da.sign_state(message.from_user.id, st.secret_key)
    link = f"{st.base_url.rstrip('/')}/da/login?s={state}"
    await message.answer(
        f"DonationAlerts: {escape(status)}\n\n"
        f"Redirect URI для приложения DA:\n<code>{escape(da.redirect_uri(st))}</code>\n\n"
        f'<a href="{escape(link)}">Подключить аккаунт</a> (ссылка действует 15 минут)',
        disable_web_page_preview=True,
    )


@router.message(Command("pay_confirm"))
async def pay_confirm(message: Message, command: CommandObject):
    st = get_settings()
    code = (command.args or "").upper().replace("TX-", "").strip()
    async with session_scope() as s:
        p = await s.scalar(select(Payment).where(Payment.code == code).with_for_update())
        if not p:
            await message.answer("Платёж не найден.")
            return
        if p.status == "paid":
            await message.answer("Уже оплачен.")
            return
        p.note = f"manual by {message.from_user.id}"
        await payment_service.apply_payment(s, p, st)
    await message.answer(f"Платёж {payment_service.format_code(code)} подтверждён.")


@router.message(Command("grant"))
async def grant(message: Message, command: CommandObject):
    st = get_settings()
    parts = (command.args or "").split()
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        await message.answer("Формат: /grant ID МЕСЯЦЕВ [УСТРОЙСТВ]")
        return
    uid, months = int(parts[0]), int(parts[1])
    devices = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
    async with session_scope() as s:
        user = await s.get(User, uid)
        if not user:
            await message.answer("Пользователь не найден (он должен хотя бы раз запустить бота).")
            return
        current = await sub_service.get_active(s, uid)
        sub = await sub_service.extend(
            s, user, st,
            plan_id=current.plan_id if current else "m1",
            months=months,
            devices=min(max(devices, current.devices if current else 1), st.max_devices),
            energy_x2=False,
        )
        until = sub.expires_at
    await notify_user(uid, f"Вам выдана подписка до {until:%d.%m.%Y}.")
    await message.answer(f"Готово: подписка {uid} до {until:%d.%m.%Y}.")


@router.message(Command("user"))
async def user_card(message: Message, command: CommandObject):
    if not (command.args or "").strip().isdigit():
        await message.answer("Формат: /user ID")
        return
    uid = int(command.args.strip())
    async with session_scope() as s:
        u = await s.get(User, uid)
        if not u:
            await message.answer("Не найден.")
            return
        sub = await s.get(Subscription, uid)
        refs = await s.scalar(select(func.count(Referral.id)).where(Referral.referrer_id == uid))
        paid = await s.scalar(select(func.count(Payment.id)).where(Payment.user_id == uid, Payment.status == "paid"))
    if sub:
        plan = PLANS_BY_ID[sub.plan_id].title if sub.plan_id in PLANS_BY_ID else sub.plan_id
        sub_line = f"{plan}, до {sub.expires_at:%d.%m.%Y} ({'активна' if sub.is_active() else 'истекла'})"
    else:
        sub_line = "нет"
    phone = f"••{u.phone_tail}" if u.phone_tail else "не привязан"
    await message.answer(
        f"<b>{escape(u.first_name)}</b> @{escape(u.username or '—')} <code>{u.id}</code>\n"
        f"Телефон: {phone}\n"
        f"Риск: {u.risk_score} [{escape(u.risk_flags or '—')}]\n"
        f"Бан: {'да' if u.is_banned else 'нет'}\n"
        f"Подписка: {sub_line}"
    )
    await message.answer(f"Оплат: {paid}, пригласил: {refs}, пригласил его: {u.referred_by_id or '—'}")


async def _set_ban(message: Message, command: CommandObject, value: bool):
    if not (command.args or "").strip().isdigit():
        await message.answer("Укажите ID.")
        return
    async with session_scope() as s:
        u = await s.get(User, int(command.args.strip()))
        if not u:
            await message.answer("Не найден.")
            return
        u.is_banned = value
    await message.answer("Заблокирован." if value else "Разблокирован.")


@router.message(Command("ban"))
async def ban(message: Message, command: CommandObject):
    await _set_ban(message, command, True)


@router.message(Command("unban"))
async def unban(message: Message, command: CommandObject):
    await _set_ban(message, command, False)


@router.message(Command("holds"))
async def holds(message: Message):
    async with session_scope() as s:
        rows = (await s.scalars(select(Referral).where(Referral.status == "hold").order_by(Referral.id).limit(30))).all()
    if not rows:
        await message.answer("Очередь пуста.")
        return
    await message.answer("\n".join(f"#{r.id}: {r.referrer_id} ← {r.invitee_id} ({escape(r.reason or '')})" for r in rows))


@router.message(Command("ref_ok"))
async def ref_ok(message: Message, command: CommandObject):
    st = get_settings()
    rid = int(command.args) if (command.args or "").isdigit() else 0
    async with session_scope() as s:
        ref = await s.get(Referral, rid, with_for_update=True)
        if not ref or ref.status != "hold":
            await message.answer("Не найден или не на проверке.")
            return
        referrer = await s.get(User, ref.referrer_id, with_for_update=True)
        await referral_service.credit(s, ref, referrer, st)
        referrer_id = referrer.id
    await notify_user(referrer_id, f"Ваш друг оформил подписку — начислено +{st.ref_bonus_gb} ГБ.")
    await message.answer("Начислено.")


@router.message(Command("ref_no"))
async def ref_no(message: Message, command: CommandObject):
    rid = int(command.args) if (command.args or "").isdigit() else 0
    async with session_scope() as s:
        ref = await s.get(Referral, rid)
        if not ref or ref.status != "hold":
            await message.answer("Не найден или не на проверке.")
            return
        ref.status, ref.reason = "rejected", f"admin {message.from_user.id}"
    await message.answer("Отклонено.")
