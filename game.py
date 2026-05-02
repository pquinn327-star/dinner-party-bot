"""
Game flow logic for Dinner Party.

These functions own the state machine:
    LOBBY -> SUBMISSION_OPEN -> GUESSING -> OVER

They are called from the Telegram handlers but contain no Telegram I/O —
the handlers are responsible for actually sending messages. This separation
keeps the rules testable.
"""

import json
import random
from typing import Optional

import db
import messages


def player_display(player: dict) -> str:
    """Plain-text display name for a player row."""
    if player.get("username"):
        return f"@{player['username']}"
    return player.get("display_name") or f"Player {player['user_id']}"


def player_mention_html(player: dict) -> str:
    """HTML mention that pings the user even without a @username."""
    name = player.get("display_name") or "Player"
    if player.get("username"):
        return f'@{player["username"]}'
    return f'<a href="tg://user?id={player["user_id"]}">{name}</a>'


# -------- submission phase --------

async def record_submission(db_path: str, game: dict, user,
                            guest_name: str) -> tuple[bool, str]:
    """
    Player DM'd their guest. Validate and store.
    Returns (ok, message_to_send_back_to_player).
    """
    guest_name = guest_name.strip()
    if not guest_name:
        return False, "Please send a non-empty guest name."
    if len(guest_name) > 100:
        return False, "Guest name too long. Keep it under 100 characters."

    if user.id == game["host_user_id"]:
        return False, "You're the host — you don't bring a guest."

    players = await db.get_players(db_path, game["id"])
    # Check for duplicate guest names (case-insensitive)
    lowered = guest_name.lower()
    for p in players:
        if p["user_id"] == user.id:
            continue
        if p.get("guest_name") and p["guest_name"].lower() == lowered:
            return False, (
                f"'{guest_name}' has already been claimed by another player. "
                f"Pick a different guest."
            )

    display_name = " ".join(filter(None, [user.first_name, user.last_name])) or user.username or f"Player {user.id}"

    await db.add_or_update_player(
        db_path,
        game["id"],
        user.id,
        user.username,
        display_name,
        guest_name=guest_name,
    )
    return True, f"✅ Got it. Your guest is: <b>{guest_name}</b>"


# -------- transition: submission -> guessing --------

async def close_submissions(db_path: str, game: dict,
                            min_players: int) -> tuple[bool, str, Optional[list[int]]]:
    """
    Lock submissions and set up turn order.
    Returns (ok, message, turn_order_user_ids).
    On success the game is in STATE_GUESSING.
    """
    players = await db.get_players(db_path, game["id"])
    submitted = [p for p in players if p.get("guest_name")]

    if len(submitted) < min_players:
        await db.update_game_state(db_path, game["id"], db.STATE_OVER)
        return False, (
            f"Only {len(submitted)} player(s) submitted a guest. "
            f"Need at least {min_players}. Game cancelled."
        ), None

    # Determine turn order
    settings = await db.get_or_create_settings(db_path, game["chat_id"])
    last_winner = settings.get("last_winner_id")

    submitted_ids = [p["user_id"] for p in submitted]
    if last_winner and last_winner in submitted_ids:
        first = last_winner
        rest = [uid for uid in submitted_ids if uid != first]
        random.shuffle(rest)
        order = [first] + rest
    else:
        order = submitted_ids[:]
        random.shuffle(order)

    await db.set_turn_order(db_path, game["id"], order)
    await db.update_game_state(db_path, game["id"], db.STATE_GUESSING)
    return True, "Submissions closed.", order


def render_announcement(template: str | None, players: list[dict],
                        order: list[int]) -> str:
    """Build the guest-list-and-turn-order announcement."""
    guests = sorted(
        [p["guest_name"] for p in players if p.get("guest_name")],
        key=str.lower,
    )
    guests_block = "\n".join(f"  • {g}" for g in guests)

    by_id = {p["user_id"]: p for p in players}
    turn_block = "  " + " → ".join(
        player_mention_html(by_id[uid]) for uid in order if uid in by_id
    )

    return messages.render_announce(template, guests=guests_block, turn_order=turn_block)


# -------- guessing phase --------

def current_turn_player_id(game: dict) -> Optional[int]:
    if not game.get("turn_order"):
        return None
    order = json.loads(game["turn_order"])
    if not order:
        return None
    idx = game.get("current_turn_idx") or 0
    return order[idx % len(order)]


async def advance_turn(db_path: str, game: dict) -> Optional[dict]:
    """
    Move current_turn_idx to next non-eliminated player.
    Returns the player whose turn it now is, or None if game should end
    (only one or zero players left).
    """
    order = json.loads(game["turn_order"])
    players = {p["user_id"]: p for p in await db.get_players(db_path, game["id"])}

    alive = [uid for uid in order if uid in players and not players[uid]["eliminated"]]
    if len(alive) <= 1:
        return None  # caller will declare winner

    idx = game.get("current_turn_idx") or 0
    n = len(order)
    for _ in range(n):
        idx = (idx + 1) % n
        candidate = order[idx]
        p = players.get(candidate)
        if p and not p["eliminated"]:
            await db.set_current_turn_idx(db_path, game["id"], idx)
            return p
    return None


async def find_target_player(db_path: str, game_id: int,
                             text_target: str) -> Optional[dict]:
    """
    Resolve a target string to a player.
    Accepts '@username' or part of a display name.
    """
    text_target = text_target.strip().lstrip("@").lower()
    players = await db.get_players(db_path, game_id)
    # exact username match first
    for p in players:
        if p.get("username") and p["username"].lower() == text_target:
            return p
    # then display-name prefix match
    for p in players:
        name = (p.get("display_name") or "").lower()
        if name == text_target or name.startswith(text_target):
            return p
    # then any substring match in display name
    for p in players:
        name = (p.get("display_name") or "").lower()
        if text_target in name:
            return p
    return None


async def validate_and_record_guess(
    db_path: str, game: dict, guesser_id: int,
    target_input: str, guest_name: str,
) -> tuple[bool, str, Optional[int]]:
    """
    Validate a guess and store it as pending (awaiting host adjudication).
    Returns (ok, message_for_chat, pending_guess_id).
    """
    # Is there already a pending guess?
    existing = await db.get_unresolved_guess(db_path, game["id"])
    if existing:
        return False, "There's still a pending guess being adjudicated by the host. Hold on.", None

    # Is it this player's turn?
    expected = current_turn_player_id(game)
    if expected != guesser_id:
        return False, "It isn't your turn.", None

    guesser = await db.get_player(db_path, game["id"], guesser_id)
    if not guesser or guesser["eliminated"]:
        return False, "You're not an active player in this game.", None

    target = await find_target_player(db_path, game["id"], target_input)
    if not target:
        return False, f"Couldn't find a player matching '{target_input}'.", None

    if target["user_id"] == guesser_id:
        return False, "You can't guess yourself.", None
    if target["eliminated"]:
        return False, f"{player_display(target)} is already out.", None

    # Validate guest name is one of the submitted guests
    players = await db.get_players(db_path, game["id"])
    valid_guests = {p["guest_name"].lower(): p["guest_name"]
                    for p in players if p.get("guest_name")}
    if guest_name.strip().lower() not in valid_guests:
        return False, (
            f"'{guest_name}' isn't one of tonight's guests. "
            f"Check the guest list and try again."
        ), None

    # Compute correctness
    actual_guest = (target.get("guest_name") or "").lower()
    is_correct = actual_guest == guest_name.strip().lower()

    pending_id = await db.create_pending_guess(
        db_path, game["id"], guesser_id, target["user_id"],
        valid_guests[guest_name.strip().lower()],  # canonical capitalization
        is_correct,
    )
    return True, "", pending_id


async def alive_players(db_path: str, game_id: int) -> list[dict]:
    return [p for p in await db.get_players(db_path, game_id) if not p["eliminated"]]


def alphabetized_guests(players: list[dict]) -> list[str]:
    """Return guest names sorted case-insensitively. Index in this list is stable
    for the duration of the game and is what we put in callback_data."""
    return sorted(
        [p["guest_name"] for p in players if p.get("guest_name")],
        key=str.lower,
    )


def eliminated_guest_set(players: list[dict]) -> set[str]:
    """Set of guest names belonging to eliminated players (lowercased for compare)."""
    return {
        p["guest_name"].lower()
        for p in players
        if p.get("guest_name") and p["eliminated"]
    }


def strikethrough_unicode(text: str) -> str:
    """Apply Unicode combining strikethrough to each character.
    Used for Telegram button labels, which don't support HTML formatting."""
    return "".join(c + "̶" for c in text)


def format_guest_list_html(players: list[dict], bullet: str = "•") -> str:
    """Render alphabetized guest list with <s>...</s> on eliminated guests.
    Suitable for HTML message bodies (not button labels)."""
    eliminated = eliminated_guest_set(players)
    lines = []
    for guest in alphabetized_guests(players):
        if guest.lower() in eliminated:
            lines.append(f"  {bullet} <s>{guest}</s>")
        else:
            lines.append(f"  {bullet} {guest}")
    return "\n".join(lines)
