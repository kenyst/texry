"""Тарифы из макета. Цены — за 1 устройство; для N устройств цена пропорциональна
месячной стоимости устройств (1-е 199 ₽, 2-е 149 ₽, 3–5-е 119 ₽, 6–10-е 89 ₽)."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime


@dataclass(frozen=True)
class Plan:
    id: str
    title: str
    months: int
    price: int  # ₽ за 1 устройство
    note: str
    bonus_gb: int
    energy_x2: bool
    badge: str

    @property
    def bonus_label(self) -> str:
        parts = []
        if self.bonus_gb:
            parts.append(f"+{self.bonus_gb} ГБ")
        if self.energy_x2:
            parts.append("x2 энергия")
        return " и ".join(parts) if parts else "—"


PLANS: list[Plan] = [
    Plan("m1", "1 месяц", 1, 199, "Попробовать без обязательств", 0, False, ""),
    Plan("m3", "3 месяца", 3, 499, "Как 2 месяца + 1 в подарок", 10, False, ""),
    Plan("m6", "6 месяцев", 6, 849, "2 месяца бесплатно", 30, False, "ХИТ"),
    Plan("m12", "12 месяцев", 12, 1290, "Максимальная выгода — 5 месяцев в подарок", 100, True, "ВЫГОДНЕЕ ВСЕГО"),
]
PLANS_BY_ID = {p.id: p for p in PLANS}
BASE_MONTH_PRICE = 199


def device_price(i: int) -> int:
    """Цена i-го устройства в месяц."""
    return 199 if i == 1 else 149 if i == 2 else 119 if i <= 5 else 89


def devices_monthly(n: int) -> int:
    return sum(device_price(i) for i in range(1, n + 1))


def plan_amount(plan: Plan, devices: int) -> int:
    return int(round(plan.price * devices_monthly(devices) / devices_monthly(1)))


def upgrade_amount(old_devices: int, new_devices: int, expires_at: datetime, now: datetime, minimum: int) -> int:
    """Доплата за увеличение устройств до конца текущего периода."""
    days_left = max(0.0, (expires_at - now).total_seconds() / 86400)
    diff = devices_monthly(new_devices) - devices_monthly(old_devices)
    return max(minimum, math.ceil(diff * days_left / 30))


def public_config(max_devices: int) -> dict:
    return {
        "plans": [{**asdict(p), "bonus_label": p.bonus_label} for p in PLANS],
        "device_prices": [device_price(i) for i in range(1, max_devices + 1)],
        "base_month_price": BASE_MONTH_PRICE,
    }
