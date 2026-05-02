"""
SQLite data layer for Dinner Party bot.

All async via aiosqlite. One DB file, one connection-per-call (fine for our scale).
"""

import aiosqlite
import json
from typing import Optional, Any
from datetime import datetime, timezone

# States a game can be in
STATE_LOBBY = "lobby"                    # game created, waiting for host to open submissions
STATE_SUBMISSION = "submission_open"     # accepting guest submissions via DM
STATE_GUESSING = "guessing"              # turn-based guessing phase
STATE_OVER = "over"                      # finished


SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    host_user_id INTEGER NOT NULL,
    state TEXT NOT NULL,
    submission_deadline TEXT,
    current_turn_idx INTEGER DEFAULT 0,
    turn_order TEXT,                          -- JSON array of user_ids in turn order
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    winner_id INTEGER
);

CREATE TABLE IF NOT EXISTS players (
    game_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    username TEXT,                            -- @handle, may be NULL
    display_name TEXT NOT NULL,
    guest_name TEXT,                          -- guest they submitted
    eliminated INTEGER DEFAULT 0,
    PRIMARY KEY (game_id, user_id),
    FOREIGN KEY (game_id) REFERENCES games(id)
);

CREATE TABLE IF NOT EXISTS pending_guesses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER NOT NULL,
    guesser_id INTEGER NOT NULL,
    target_id INTEGER NOT NULL,
    guest_name TEXT NOT NULL,
    is_correct INTEGER NOT NULL,              -- bot pre-computes this
    resolved INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (game_id) REFERENCES games(id)
);

CREATE TABLE IF NOT EXISTS chat_settings (
    chat_id INTEGER PRIMARY KEY,
    host_user_id INTEGER,
    last_winner_id INTEGER,
    msg_correct TEXT,
    msg_wrong TEXT,
    msg_announce TEXT,
    msg_winner TEXT
);

CREATE INDEX IF NOT EXISTS idx_games_chat_active
    ON games(chat_id, state);
"""


async def init_db(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(SCHEMA)
        await db.commit()


# ---------- chat_settings ----------

async def get_or_create_settings(db_path: str, chat_id: int) -> dict:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT * FROM chat_settings WHERE chat_id = ?", (chat_id,)
        )).fetchone()
        if row:
            return dict(row)
        await db.execute(
            "INSERT INTO chat_settings (chat_id) VALUES (?)", (chat_id,)
        )
        await db.commit()
        return {"chat_id": chat_id, "host_user_id": None, "last_winner_id": None,
                "msg_correct": None, "msg_wrong": None,
                "msg_announce": None, "msg_winner": None}


async def set_host(db_path: str, chat_id: int, user_id: int) -> None:
    await get_or_create_settings(db_path, chat_id)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE chat_settings SET host_user_id = ? WHERE chat_id = ?",
            (user_id, chat_id),
        )
        await db.commit()


async def set_last_winner(db_path: str, chat_id: int, user_id: int) -> None:
    await get_or_create_settings(db_path, chat_id)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE chat_settings SET last_winner_id = ? WHERE chat_id = ?",
            (user_id, chat_id),
        )
        await db.commit()


async def set_message_template(db_path: str, chat_id: int, key: str, template: str) -> None:
    """key in {correct, wrong, announce, winner}"""
    if key not in ("correct", "wrong", "announce", "winner"):
        raise ValueError(f"unknown template key: {key}")
    await get_or_create_settings(db_path, chat_id)
    column = f"msg_{key}"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            f"UPDATE chat_settings SET {column} = ? WHERE chat_id = ?",
            (template, chat_id),
        )
        await db.commit()


# ---------- games ----------

async def get_active_game(db_path: str, chat_id: int) -> Optional[dict]:
    """Return the game in this chat that is not 'over', if any."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT * FROM games WHERE chat_id = ? AND state != ? "
            "ORDER BY id DESC LIMIT 1",
            (chat_id, STATE_OVER),
        )).fetchone()
        return dict(row) if row else None


async def create_game(db_path: str, chat_id: int, host_user_id: int,
                      submission_deadline: datetime) -> int:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "INSERT INTO games (chat_id, host_user_id, state, submission_deadline) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, host_user_id, STATE_SUBMISSION, submission_deadline.isoformat()),
        )
        await db.commit()
        return cur.lastrowid


async def update_game_state(db_path: str, game_id: int, state: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE games SET state = ? WHERE id = ?", (state, game_id)
        )
        await db.commit()


async def set_turn_order(db_path: str, game_id: int, order: list[int]) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE games SET turn_order = ?, current_turn_idx = 0 WHERE id = ?",
            (json.dumps(order), game_id),
        )
        await db.commit()


async def set_current_turn_idx(db_path: str, game_id: int, idx: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE games SET current_turn_idx = ? WHERE id = ?", (idx, game_id)
        )
        await db.commit()


async def set_winner(db_path: str, game_id: int, user_id: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE games SET winner_id = ?, state = ? WHERE id = ?",
            (user_id, STATE_OVER, game_id),
        )
        await db.commit()


# ---------- players ----------

async def add_or_update_player(db_path: str, game_id: int, user_id: int,
                               username: Optional[str], display_name: str,
                               guest_name: Optional[str] = None) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO players (game_id, user_id, username, display_name, guest_name)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(game_id, user_id) DO UPDATE SET
                   username = excluded.username,
                   display_name = excluded.display_name,
                   guest_name = COALESCE(excluded.guest_name, players.guest_name)""",
            (game_id, user_id, username, display_name, guest_name),
        )
        await db.commit()


async def get_players(db_path: str, game_id: int) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM players WHERE game_id = ?", (game_id,)
        )).fetchall()
        return [dict(r) for r in rows]


async def get_player(db_path: str, game_id: int, user_id: int) -> Optional[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT * FROM players WHERE game_id = ? AND user_id = ?",
            (game_id, user_id),
        )).fetchone()
        return dict(row) if row else None


async def eliminate_player(db_path: str, game_id: int, user_id: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE players SET eliminated = 1 WHERE game_id = ? AND user_id = ?",
            (game_id, user_id),
        )
        await db.commit()


# ---------- guesses ----------

async def create_pending_guess(db_path: str, game_id: int, guesser_id: int,
                               target_id: int, guest_name: str,
                               is_correct: bool) -> int:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            """INSERT INTO pending_guesses
               (game_id, guesser_id, target_id, guest_name, is_correct)
               VALUES (?, ?, ?, ?, ?)""",
            (game_id, guesser_id, target_id, guest_name, 1 if is_correct else 0),
        )
        await db.commit()
        return cur.lastrowid


async def get_pending_guess(db_path: str, guess_id: int) -> Optional[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT * FROM pending_guesses WHERE id = ?", (guess_id,)
        )).fetchone()
        return dict(row) if row else None


async def get_unresolved_guess(db_path: str, game_id: int) -> Optional[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT * FROM pending_guesses WHERE game_id = ? AND resolved = 0 "
            "ORDER BY id DESC LIMIT 1",
            (game_id,),
        )).fetchone()
        return dict(row) if row else None


async def resolve_guess(db_path: str, guess_id: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE pending_guesses SET resolved = 1 WHERE id = ?", (guess_id,)
        )
        await db.commit()
