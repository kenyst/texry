"""«Игры — Texry»: блок-пазл 8×8 (порт логики из макета) с серверной авторитетностью.

Сервер хранит поле, фигуры и счёт; клиент присылает только ход (фигура, строка, столбец).
Случайность (новые фигуры, мусорные блоки) детерминирована от секретного сида сессии
и номера хода, поэтому клиент не может её предсказать или перебрать.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import random
import secrets
import time
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import utcnow
from app.models import GameReward, GameSession, PromoCode, User
from app.security.risk import random_code
from app.services import subscriptions as sub_service
from app.services.notify import notify_admins

N = 8
PIECES: dict[int, list[list[tuple[int, int]]]] = {
    0: [[(0, 0)], [(0, 0), (0, 1)], [(0, 0), (1, 0)], [(0, 0), (0, 1), (1, 0), (1, 1)], [(0, 0), (0, 1), (0, 2)],
        [(0, 0), (1, 0), (2, 0)], [(0, 0), (1, 0), (1, 1)], [(0, 1), (1, 0), (1, 1)]],
    1: [[(0, 0), (0, 1), (0, 2), (0, 3)], [(0, 0), (1, 0), (2, 0), (3, 0)], [(0, 0), (0, 1), (0, 2), (1, 1)],
        [(0, 1), (0, 2), (1, 0), (1, 1)], [(0, 0), (0, 1), (1, 1), (1, 2)], [(0, 1), (1, 0), (1, 1), (1, 2), (2, 1)],
        [(0, 0), (0, 1), (1, 0), (1, 1)], [(0, 0), (1, 0), (2, 0), (2, 1), (2, 2)]],
    2: [[(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2), (2, 0), (2, 1), (2, 2)], [(0, 0), (0, 1), (0, 2), (0, 3), (0, 4)],
        [(0, 0), (1, 0), (2, 0), (3, 0), (4, 0)], [(0, 0), (0, 2), (1, 0), (1, 1), (1, 2)],
        [(0, 0), (0, 1), (1, 1), (2, 1), (2, 2)], [(0, 1), (1, 0), (1, 1), (1, 2), (2, 1)],
        [(0, 0), (0, 1), (0, 2), (1, 0), (1, 2)]],
}


def tier(score: int) -> int:
    return 0 if score < 100_000 else 1 if score < 300_000 else 2


def pool_for(score: int) -> list[list[tuple[int, int]]]:
    t = tier(score)
    if t == 0:
        return PIECES[0]
    if t == 1:
        return PIECES[1] + PIECES[0][3:]
    return PIECES[2] + PIECES[1][:3]


@dataclass
class GameState:
    board: list[bool] = field(default_factory=lambda: [False] * (N * N))
    tray: list[list[list[int]]] = field(default_factory=list)
    used: list[bool] = field(default_factory=lambda: [False, False, False])
    score: int = 0
    over: bool = False
    moves: int = 0

    def to_json(self) -> str:
        return json.dumps({"b": "".join("1" if x else "0" for x in self.board), "t": self.tray, "u": self.used,
                           "s": self.score, "o": self.over, "m": self.moves}, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> "GameState":
        d = json.loads(raw)
        return cls(board=[c == "1" for c in d["b"]], tray=d["t"], used=d["u"], score=d["s"], over=d["o"], moves=d["m"])


def _rng(seed: str, *parts: object) -> random.Random:
    digest = hmac.new(seed.encode(), ":".join(map(str, parts)).encode(), hashlib.sha256).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def new_tray(seed: str, score: int, moves: int) -> list[list[list[int]]]:
    rng = _rng(seed, "tray", moves)
    pool = pool_for(score)
    return [[list(c) for c in pool[rng.randrange(len(pool))]] for _ in range(3)]


def new_game(seed: str) -> GameState:
    return GameState(tray=new_tray(seed, 0, 0))


def fits(board: list[bool], piece, r: int, c: int) -> bool:
    for dr, dc in piece:
        rr, cc = r + dr, c + dc
        if not (0 <= rr < N and 0 <= cc < N) or board[rr * N + cc]:
            return False
    return True


def any_fits(board: list[bool], tray, used: list[bool]) -> bool:
    for i, p in enumerate(tray):
        if used[i]:
            continue
        for r in range(N):
            for c in range(N):
                if fits(board, p, r, c):
                    return True
    return False


class IllegalMove(Exception):
    pass


def place(state: GameState, seed: str, piece_idx: int, r: int, c: int) -> int:
    """Применяет ход. Возвращает набранные очки."""
    if state.over:
        raise IllegalMove("game_over")
    if not (0 <= piece_idx < 3) or state.used[piece_idx]:
        raise IllegalMove("bad_piece")
    if not (isinstance(r, int) and isinstance(c, int)):
        raise IllegalMove("bad_cell")
    piece = state.tray[piece_idx]
    if not fits(state.board, piece, r, c):
        raise IllegalMove("no_fit")

    board = state.board[:]
    for dr, dc in piece:
        board[(r + dr) * N + (c + dc)] = True
    gain = len(piece) * 5
    full_rows = [i for i in range(N) if all(board[i * N + j] for j in range(N))]
    full_cols = [i for i in range(N) if all(board[j * N + i] for j in range(N))]
    lines = len(full_rows) + len(full_cols)
    for i in full_rows:
        for j in range(N):
            board[i * N + j] = False
    for i in full_cols:
        for j in range(N):
            board[j * N + i] = False
    if lines:
        gain += lines * lines * 900

    used = state.used[:]
    used[piece_idx] = True
    state.moves += 1
    score = state.score + gain
    tray = state.tray
    if all(used):
        tray = new_tray(seed, score, state.moves)
        used = [False, False, False]

    t = tier(score)
    junk = 1 if t == 1 else 3 if t == 2 else 0
    rng = _rng(seed, "junk", state.moves)
    for _ in range(junk):
        empty = [i for i, v in enumerate(board) if not v]
        if empty:
            board[empty[rng.randrange(len(empty))]] = True

    state.board, state.tray, state.used, state.score = board, tray, used, score
    state.over = not any_fits(board, tray, used)
    return gain


# ---------- слой БД ----------

async def load(session: AsyncSession, user_id: int, lock: bool = False) -> GameSession:
    gs = await session.get(GameSession, user_id, with_for_update=lock)
    if gs is None:
        seed = secrets.token_hex(16)
        gs = GameSession(user_id=user_id, seed=seed, state=new_game(seed).to_json(), best_score=0, rewarded=False, last_move_at=0.0)
        session.add(gs)
    return gs


async def rewards_count(session: AsyncSession, user_id: int) -> int:
    return int(await session.scalar(select(func.count(GameReward.id)).where(GameReward.user_id == user_id)) or 0)


async def payload(session: AsyncSession, gs: GameSession, settings: Settings) -> dict:
    st = GameState.from_json(gs.state)
    t = tier(st.score)
    left = settings.game_reward_limit - await rewards_count(session, gs.user_id)
    return {
        "board": st.board,
        "tray": st.tray,
        "used": st.used,
        "score": st.score,
        "over": st.over,
        "moves": st.moves,
        "tier": t,
        "best": gs.best_score,
        "goal": settings.game_goal,
        "rewarded": gs.rewarded,
        "rewards_left": max(0, left),
        "reward_months": settings.game_reward_months,
    }


async def move(session: AsyncSession, user: User, settings: Settings, piece: int, r: int, c: int) -> tuple[dict, bool]:
    gs = await load(session, user.id, lock=True)
    now = time.time()
    if now - gs.last_move_at < settings.game_min_move_interval:
        raise IllegalMove("too_fast")
    st = GameState.from_json(gs.state)
    place(st, gs.seed, piece, r, c)
    gs.state = st.to_json()
    gs.last_move_at = now
    gs.best_score = max(gs.best_score, st.score)

    rewarded_now = False
    if st.score >= settings.game_goal and not gs.rewarded:
        if await rewards_count(session, user.id) < settings.game_reward_limit:
            promo = PromoCode(code="GAME-" + random_code(8), user_id=user.id, source="game", kind="months",
                              value=settings.game_reward_months)
            session.add(promo)
            await session.flush()
            session.add(GameReward(user_id=user.id, score=st.score, moves=st.moves, promo_id=promo.id))
            sub = await sub_service.get_active(session, user.id)
            await sub_service.extend(session, user, settings, plan_id=sub.plan_id if sub else "m1",
                                     months=settings.game_reward_months, devices=sub.devices if sub else 1,
                                     energy_x2=False)
            promo.applied_at = utcnow()
            gs.rewarded = True
            rewarded_now = True
            await notify_admins(f"Награда игры: user {user.id}, {st.score} очков за {st.moves} ходов. Промокод {promo.code}.")
    return await payload(session, gs, settings), rewarded_now


async def reset(session: AsyncSession, user: User, settings: Settings) -> dict:
    gs = await load(session, user.id, lock=True)
    gs.seed = secrets.token_hex(16)
    gs.state = new_game(gs.seed).to_json()
    gs.rewarded = False
    gs.last_move_at = 0.0
    gs.started_at = utcnow()
    return await payload(session, gs, settings)
