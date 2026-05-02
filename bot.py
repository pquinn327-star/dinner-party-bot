"""
Dinner Party — Telegram bot entry point.

Run with:  python bot.py

Required env vars (see .env.example):
    TELEGRAM_BOT_TOKEN
    DATABASE_PATH                  (optional, default: dinner_party.db)
    SUBMISSION_WINDOW_SECONDS      (optional, default: 300)
    MIN_PLAYERS, MAX_PLAYERS       (optional)
"""

import asyncio
import logging
import os
import json
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

import db
import game
import messages

load_dotenv()

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
DB_PATH = os.environ.get("DATABASE_PATH", "dinner_party.db")
SUBMISSION_WINDOW_SECONDS = int(os.environ.get("SUBMISSION_WINDOW_SECONDS", "300"))
MIN_PLAYERS = int(os.environ.get("MIN_PLAYERS", "3"))
MAX_PLAYERS = int(os.environ.get("MAX_PLAYERS", "8"))

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("dinner_party")


# ============================================================
# /start — works in DM or group
# ============================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat.type == ChatType.PRIVATE:
        await update.message.reply_text(
            "Hi! I run the Dinner Party game.\n\n"
            "Add me to a group with your friends, then have someone run "
            "/sethost in that group to designate the host. The host runs "
            "/newgame to start a round.\n\n"
            "When a game is in its submission window, send me your guest "
            "name in this DM."
        )
    else:
        await update.message.reply_html(
            "👋 Dinner Party bot is here.\n\n"
            "<b>Setup:</b> Have one person run /sethost — they'll moderate.\n"
            "<b>Play:</b> The host runs /newgame to start a round.\n\n"
            "Type /help for the full command list."
        )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "<b>Commands</b>\n\n"
        "<b>Setup (group):</b>\n"
        "/sethost — make yourself the host, or reply to someone with /sethost to make them host\n\n"
        "<b>Game flow (group):</b>\n"
        "/newgame — host: open a new submission window\n"
        "/closesubmissions — host: end submission window early\n"
        "/cancelgame — host: abort the current game\n"
        "/status — see current game state\n"
        "/guess @player Guest Name — make your guess on your turn\n\n"
        "<b>During submission (DM the bot):</b>\n"
        "Just send the name of your guest. That's it.\n\n"
        "<b>Customizing announcements (group, host only):</b>\n"
        "/setmsg correct &lt;template&gt;\n"
        "/setmsg wrong &lt;template&gt;\n"
        "/setmsg announce &lt;template&gt;\n"
        "/setmsg winner &lt;template&gt;\n"
        "Placeholders: {guesser} {target} {guest} {winner} {guests} {turn_order}\n"
        "/resetmsg &lt;key&gt; — restore default for that template"
    )
    await update.message.reply_html(text)


# ============================================================
# /sethost
# ============================================================

async def cmd_sethost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat.type == ChatType.PRIVATE:
        await update.message.reply_text("/sethost only works in a group chat.")
        return

    # If replying to someone, they become host
    target_user = (
        update.message.reply_to_message.from_user
        if update.message.reply_to_message else update.effective_user
    )

    await db.set_host(DB_PATH, chat.id, target_user.id)
    name = target_user.first_name or target_user.username or "the host"
    await update.message.reply_html(f"🎩 <b>{name}</b> is now the host of this group.")


# ============================================================
# /newgame — host opens submission window
# ============================================================

async def cmd_newgame(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat.type == ChatType.PRIVATE:
        await update.message.reply_text("/newgame only works in a group chat.")
        return

    user = update.effective_user
    settings = await db.get_or_create_settings(DB_PATH, chat.id)
    if settings["host_user_id"] is None:
        await update.message.reply_text(
            "No host has been set yet. Have someone run /sethost first."
        )
        return
    if settings["host_user_id"] != user.id:
        await update.message.reply_text("Only the host can start a new game.")
        return

    existing = await db.get_active_game(DB_PATH, chat.id)
    if existing:
        await update.message.reply_text(
            f"There's already an active game in state '{existing['state']}'. "
            f"Use /cancelgame to abort it first."
        )
        return

    deadline = datetime.now(timezone.utc) + timedelta(seconds=SUBMISSION_WINDOW_SECONDS)
    game_id = await db.create_game(DB_PATH, chat.id, user.id, deadline)

    bot_username = (await context.bot.get_me()).username
    minutes = SUBMISSION_WINDOW_SECONDS // 60

    await update.message.reply_html(
        f"🍽️ <b>A new Dinner Party is forming.</b>\n\n"
        f"DM me (@{bot_username}) the name of the guest you're bringing. "
        f"You have <b>{minutes} minute(s)</b>.\n\n"
        f"Min players: {MIN_PLAYERS} • Max: {MAX_PLAYERS}\n"
        f"The host will run /closesubmissions when ready, or it'll close automatically."
    )

    # Schedule auto-close
    context.job_queue.run_once(
        auto_close_submissions,
        when=SUBMISSION_WINDOW_SECONDS,
        data={"chat_id": chat.id, "game_id": game_id},
        name=f"close_game_{game_id}",
    )


async def auto_close_submissions(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    chat_id = data["chat_id"]
    game_id = data["game_id"]

    g = await db.get_active_game(DB_PATH, chat_id)
    if not g or g["id"] != game_id or g["state"] != db.STATE_SUBMISSION:
        return  # already closed/cancelled
    await _do_close_submissions(context, chat_id, g)


async def cmd_close_submissions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat.type == ChatType.PRIVATE:
        return

    g = await db.get_active_game(DB_PATH, chat.id)
    if not g:
        await update.message.reply_text("No active game.")
        return
    if g["state"] != db.STATE_SUBMISSION:
        await update.message.reply_text("Submissions aren't open right now.")
        return
    if update.effective_user.id != g["host_user_id"]:
        await update.message.reply_text("Only the host can close submissions.")
        return

    await _do_close_submissions(context, chat.id, g)


async def _do_close_submissions(context: ContextTypes.DEFAULT_TYPE,
                                chat_id: int, g: dict) -> None:
    ok, msg, order = await game.close_submissions(DB_PATH, g, MIN_PLAYERS)
    if not ok:
        await context.bot.send_message(chat_id, msg)
        return

    # Refresh game with new state/order
    g = await db.get_active_game(DB_PATH, chat_id)
    players = await db.get_players(DB_PATH, g["id"])
    settings = await db.get_or_create_settings(DB_PATH, chat_id)

    announcement = game.render_announcement(
        settings.get("msg_announce"), players, order
    )
    await context.bot.send_message(
        chat_id, announcement, parse_mode=ParseMode.HTML
    )

    # Announce first turn
    await _announce_current_turn(context, chat_id, g)


async def _announce_current_turn(context: ContextTypes.DEFAULT_TYPE,
                                 chat_id: int, g: dict) -> None:
    """Announce in the group AND DM the active player a button menu."""
    uid = game.current_turn_player_id(g)
    if uid is None:
        return
    p = await db.get_player(DB_PATH, g["id"], uid)
    if not p:
        return
    mention = game.player_mention_html(p)
    await context.bot.send_message(
        chat_id,
        f"🎯 {mention}, it's your turn. Check your DMs to make your guess "
        f"(or type <code>/guess @player Guest Name</code> here).",
        parse_mode=ParseMode.HTML,
    )

    # DM the player with target-pick buttons
    await _send_target_picker_dm(context, g, p)


async def _send_target_picker_dm(context: ContextTypes.DEFAULT_TYPE,
                                 g: dict, guesser: dict) -> None:
    """DM the current-turn player a keyboard of who they could accuse."""
    players = await db.get_players(DB_PATH, g["id"])
    alive_targets = [p for p in players
                     if not p["eliminated"] and p["user_id"] != guesser["user_id"]]

    rows = []
    for p in alive_targets:
        label = game.player_display(p)
        rows.append([InlineKeyboardButton(
            label, callback_data=f"picktarget:{g['id']}:{p['user_id']}"
        )])
    rows.append([InlineKeyboardButton(
        "❌ Cancel", callback_data=f"cancelpick:{g['id']}"
    )])

    keyboard = InlineKeyboardMarkup(rows)

    try:
        await context.bot.send_message(
            guesser["user_id"],
            "🎯 <b>Your turn.</b>\n\n"
            "Step 1 of 2: Who do you think brought a guest?",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    except Exception as e:
        log.warning("Could not DM player %s: %s", guesser["user_id"], e)
        await context.bot.send_message(
            g["chat_id"],
            f"⚠️ I couldn't DM {game.player_display(guesser)} — "
            f"they need to /start me in DM first. They can still use "
            f"<code>/guess @player Guest Name</code> in this group.",
            parse_mode=ParseMode.HTML,
        )


# ============================================================
# Two-step inline picker (DM): pick target -> pick guest
# ============================================================

async def cb_pick_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Player tapped a target. Show the guest list keyboard."""
    query = update.callback_query
    parts = query.data.split(":")
    # picktarget:gameId:targetId
    game_id = int(parts[1])
    target_id = int(parts[2])

    g = await _get_game_by_id(game_id)
    if not g or g["state"] != db.STATE_GUESSING:
        await query.answer("Game is no longer active.", show_alert=True)
        await query.edit_message_text("This game is over.")
        return

    # Validate this is still the picker's turn
    if game.current_turn_player_id(g) != query.from_user.id:
        await query.answer("It's no longer your turn.", show_alert=True)
        await query.edit_message_text("It's no longer your turn.")
        return

    # Validate target is still alive
    target = await db.get_player(DB_PATH, game_id, target_id)
    if not target or target["eliminated"]:
        await query.answer("That player is no longer in the game.", show_alert=True)
        # Refresh keyboard with current state
        guesser = await db.get_player(DB_PATH, game_id, query.from_user.id)
        await _send_target_picker_dm(context, g, guesser)
        await query.edit_message_text("Player no longer available — re-sending your turn menu.")
        return

    # Show guest picker keyboard
    players = await db.get_players(DB_PATH, game_id)
    guests = game.alphabetized_guests(players)
    eliminated = game.eliminated_guest_set(players)

    rows = []
    pair = []
    for idx, guest in enumerate(guests):
        # Strikethrough on button label if the guest's owner is eliminated.
        # Button text doesn't support HTML, so we use Unicode combining chars.
        label = (game.strikethrough_unicode(guest)
                 if guest.lower() in eliminated else guest)
        pair.append(InlineKeyboardButton(
            label, callback_data=f"pickguest:{game_id}:{target_id}:{idx}"
        ))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([
        InlineKeyboardButton("← Change player",
                             callback_data=f"backtotargets:{game_id}"),
        InlineKeyboardButton("❌ Cancel",
                             callback_data=f"cancelpick:{game_id}"),
    ])

    await query.answer()
    await query.edit_message_text(
        f"🎯 <b>Your turn.</b>\n\n"
        f"Step 2 of 2: You think <b>{game.player_display(target)}</b> brought… "
        f"which guest?\n\n"
        f"{game.format_guest_list_html(players)}\n\n"
        f"<i>(Struck-through guests have already been correctly identified.)</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_pick_guest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Player tapped a guest. Submit the guess."""
    query = update.callback_query
    parts = query.data.split(":")
    # pickguest:gameId:targetId:guestIdx
    game_id = int(parts[1])
    target_id = int(parts[2])
    guest_idx = int(parts[3])

    g = await _get_game_by_id(game_id)
    if not g or g["state"] != db.STATE_GUESSING:
        await query.answer("Game is no longer active.", show_alert=True)
        await query.edit_message_text("This game is over.")
        return

    if game.current_turn_player_id(g) != query.from_user.id:
        await query.answer("It's no longer your turn.", show_alert=True)
        await query.edit_message_text("It's no longer your turn.")
        return

    players = await db.get_players(DB_PATH, game_id)
    guests = game.alphabetized_guests(players)
    if guest_idx < 0 or guest_idx >= len(guests):
        await query.answer("Invalid guest selection.", show_alert=True)
        return
    guest_name = guests[guest_idx]

    target = await db.get_player(DB_PATH, game_id, target_id)
    if not target:
        await query.answer("Target not found.", show_alert=True)
        return

    # Submit the guess via the same path as /guess
    target_input = (target.get("username") and f"@{target['username']}") \
                   or target["display_name"]
    ok, error_msg, pending_id = await game.validate_and_record_guess(
        DB_PATH, g, query.from_user.id, target_input, guest_name
    )
    if not ok:
        await query.answer(error_msg, show_alert=True)
        return

    pending = await db.get_pending_guess(DB_PATH, pending_id)
    guesser = await db.get_player(DB_PATH, game_id, pending["guesser_id"])

    await query.answer("Submitted!")
    await query.edit_message_text(
        f"📨 Submitted: <b>{game.player_display(target)}</b> brought "
        f"<b>{pending['guest_name']}</b>.\n\nAwaiting host adjudication...",
        parse_mode=ParseMode.HTML,
    )

    # Same group + host-DM flow as the /guess command
    await context.bot.send_message(
        g["chat_id"],
        f"📨 {game.player_mention_html(guesser)} guesses: "
        f"<b>{game.player_display(target)}</b> brought "
        f"<b>{pending['guest_name']}</b>.\n"
        f"Awaiting host...",
        parse_mode=ParseMode.HTML,
    )

    verdict = "✅ CORRECT" if pending["is_correct"] else "❌ WRONG"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📣 Announce (default message)",
                              callback_data=f"announce:{pending_id}")],
        [InlineKeyboardButton("✏️ Custom message…",
                              callback_data=f"custom:{pending_id}")],
    ])
    try:
        await context.bot.send_message(
            g["host_user_id"],
            f"<b>Guess to adjudicate</b>\n\n"
            f"Guesser: {game.player_display(guesser)}\n"
            f"Target: {game.player_display(target)}\n"
            f"Guest: <b>{pending['guest_name']}</b>\n\n"
            f"Verdict: {verdict}",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    except Exception as e:
        log.warning("Could not DM host (%s): %s", g["host_user_id"], e)
        await context.bot.send_message(
            g["chat_id"],
            "⚠️ I couldn't DM the host. The host needs to /start me in DM first."
        )


async def cb_back_to_targets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Player tapped 'Change player' — go back to target picker."""
    query = update.callback_query
    parts = query.data.split(":")
    game_id = int(parts[1])

    g = await _get_game_by_id(game_id)
    if not g or g["state"] != db.STATE_GUESSING:
        await query.answer("Game is no longer active.", show_alert=True)
        await query.edit_message_text("This game is over.")
        return
    if game.current_turn_player_id(g) != query.from_user.id:
        await query.answer("It's no longer your turn.", show_alert=True)
        return

    players = await db.get_players(DB_PATH, game_id)
    guesser = next((p for p in players if p["user_id"] == query.from_user.id), None)
    if not guesser:
        await query.answer("You're not in this game.", show_alert=True)
        return

    alive_targets = [p for p in players
                     if not p["eliminated"] and p["user_id"] != guesser["user_id"]]
    rows = [
        [InlineKeyboardButton(game.player_display(p),
                              callback_data=f"picktarget:{g['id']}:{p['user_id']}")]
        for p in alive_targets
    ]
    rows.append([InlineKeyboardButton("❌ Cancel",
                                      callback_data=f"cancelpick:{g['id']}")])

    await query.answer()
    await query.edit_message_text(
        "🎯 <b>Your turn.</b>\n\nStep 1 of 2: Who do you think brought a guest?",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cb_cancel_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Player tapped Cancel — close the menu without submitting."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Menu closed. Send /start me here, or use "
        "<code>/guess @player Guest Name</code> in the group when you're ready.",
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# DM handler — guest submissions
# ============================================================

async def dm_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    if text.startswith("/"):
        return  # commands handled elsewhere

    user = update.effective_user

    # Check if host has a pending custom-announcement context
    pending_custom = context.user_data.get("pending_custom_announce")
    if pending_custom:
        await _post_custom_announcement(update, context, pending_custom, text)
        return

    # Find an active game where this user is registered as a player or
    # could become one (i.e., a game in submission state in any chat).
    g = await _find_submission_game_for_user(user.id)
    if not g:
        await update.message.reply_text(
            "There's no open submission window right now. "
            "Wait for the host to run /newgame."
        )
        return

    ok, reply = await game.record_submission(DB_PATH, g, user, text)
    await update.message.reply_html(reply)

    if ok:
        # Have all eligible players submitted? If so, auto-close early.
        players = await db.get_players(DB_PATH, g["id"])
        submitted = [p for p in players if p.get("guest_name")]
        if len(submitted) >= MAX_PLAYERS:
            await _do_close_submissions(context, g["chat_id"], g)


async def _find_submission_game_for_user(user_id: int) -> dict | None:
    """
    Find a game in SUBMISSION state. v1 assumption: a user is in at most
    one active submission window at a time. Walks all submission-state games.
    """
    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (await conn.execute(
            "SELECT * FROM games WHERE state = ? ORDER BY id DESC",
            (db.STATE_SUBMISSION,),
        )).fetchall()
        return dict(rows[0]) if rows else None


# ============================================================
# /guess
# ============================================================

async def cmd_guess(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat.type == ChatType.PRIVATE:
        await update.message.reply_text("/guess works in the group chat.")
        return

    g = await db.get_active_game(DB_PATH, chat.id)
    if not g or g["state"] != db.STATE_GUESSING:
        await update.message.reply_text("No game is in the guessing phase.")
        return

    args_text = " ".join(context.args) if context.args else ""
    if not args_text.strip():
        await update.message.reply_text(
            "Usage: /guess @player Guest Name\n"
            "Example: /guess @sarah Beyoncé"
        )
        return

    parts = args_text.split(None, 1)
    if len(parts) < 2:
        await update.message.reply_text(
            "Need both a player and a guest. Example: /guess @sarah Beyoncé"
        )
        return

    target_input, guest_name = parts[0], parts[1]

    ok, error_msg, pending_id = await game.validate_and_record_guess(
        DB_PATH, g, update.effective_user.id, target_input, guest_name
    )
    if not ok:
        await update.message.reply_text(error_msg)
        return

    pending = await db.get_pending_guess(DB_PATH, pending_id)
    guesser = await db.get_player(DB_PATH, g["id"], pending["guesser_id"])
    target = await db.get_player(DB_PATH, g["id"], pending["target_id"])

    # Acknowledge in group
    await update.message.reply_html(
        f"📨 {game.player_mention_html(guesser)} guesses: "
        f"<b>{game.player_display(target)}</b> brought "
        f"<b>{pending['guest_name']}</b>.\n"
        f"Awaiting host..."
    )

    # DM the host with verdict + announce buttons
    verdict = "✅ CORRECT" if pending["is_correct"] else "❌ WRONG"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "📣 Announce (default message)",
            callback_data=f"announce:{pending_id}",
        )],
        [InlineKeyboardButton(
            "✏️ Custom message…",
            callback_data=f"custom:{pending_id}",
        )],
    ])
    try:
        await context.bot.send_message(
            g["host_user_id"],
            f"<b>Guess to adjudicate</b>\n\n"
            f"Guesser: {game.player_display(guesser)}\n"
            f"Target: {game.player_display(target)}\n"
            f"Guest: <b>{pending['guest_name']}</b>\n\n"
            f"Verdict: {verdict}",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    except Exception as e:
        log.warning("Could not DM host (%s): %s", g["host_user_id"], e)
        await update.message.reply_text(
            "⚠️ I couldn't DM the host. The host needs to /start me in DM first."
        )


# ============================================================
# Host adjudication callbacks
# ============================================================

async def cb_announce(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    pending_id = int(query.data.split(":", 1)[1])
    await _resolve_guess(context, pending_id, custom_text=None,
                         host_user_id=query.from_user.id, query=query)


async def cb_custom(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    pending_id = int(query.data.split(":", 1)[1])
    context.user_data["pending_custom_announce"] = pending_id
    await query.edit_message_text(
        query.message.text + "\n\n📝 Send me your custom announcement now. "
                              "Your next message will be posted to the group as-is."
    )


async def _post_custom_announcement(update: Update,
                                    context: ContextTypes.DEFAULT_TYPE,
                                    pending_id: int, text: str) -> None:
    context.user_data.pop("pending_custom_announce", None)
    await _resolve_guess(context, pending_id, custom_text=text,
                         host_user_id=update.effective_user.id, query=None)
    await update.message.reply_text("📣 Posted to the group.")


async def _resolve_guess(context: ContextTypes.DEFAULT_TYPE,
                         pending_id: int, custom_text: str | None,
                         host_user_id: int, query) -> None:
    pending = await db.get_pending_guess(DB_PATH, pending_id)
    if not pending or pending["resolved"]:
        if query:
            await query.edit_message_text("This guess has already been resolved.")
        return

    g = await _get_game_by_id(pending["game_id"])
    if not g or g["state"] != db.STATE_GUESSING:
        if query:
            await query.edit_message_text("Game is no longer active.")
        return

    if host_user_id != g["host_user_id"]:
        if query:
            await query.edit_message_text("Only the host can adjudicate.")
        return

    guesser = await db.get_player(DB_PATH, g["id"], pending["guesser_id"])
    target = await db.get_player(DB_PATH, g["id"], pending["target_id"])
    settings = await db.get_or_create_settings(DB_PATH, g["chat_id"])

    is_correct = bool(pending["is_correct"])
    if custom_text:
        announcement = custom_text
    elif is_correct:
        announcement = messages.render_correct(
            settings.get("msg_correct"),
            guesser=game.player_display(guesser),
            target=game.player_display(target),
            guest=pending["guest_name"],
        )
    else:
        announcement = messages.render_wrong(
            settings.get("msg_wrong"),
            guesser=game.player_display(guesser),
            target=game.player_display(target),
            guest=pending["guest_name"],
        )

    await context.bot.send_message(
        g["chat_id"], announcement, parse_mode=ParseMode.HTML
    )

    if is_correct:
        await db.eliminate_player(DB_PATH, g["id"], target["user_id"])

    await db.resolve_guess(DB_PATH, pending_id)

    if query:
        await query.edit_message_text(
            query.message.text + "\n\n✅ Resolved and announced."
        )

    # Re-fetch game then advance turn or end game
    g = await _get_game_by_id(g["id"])
    next_player = await game.advance_turn(DB_PATH, g)

    if next_player is None:
        # Game over
        alive = await game.alive_players(DB_PATH, g["id"])
        if len(alive) == 1:
            winner = alive[0]
            await db.set_winner(DB_PATH, g["id"], winner["user_id"])
            await db.set_last_winner(DB_PATH, g["chat_id"], winner["user_id"])
            winner_msg = messages.render_winner(
                settings.get("msg_winner"),
                winner=game.player_mention_html(winner),
            )
            await context.bot.send_message(
                g["chat_id"], winner_msg, parse_mode=ParseMode.HTML
            )
        else:
            await db.update_game_state(DB_PATH, g["id"], db.STATE_OVER)
            await context.bot.send_message(g["chat_id"], "Game ended.")
        return

    # Otherwise announce next turn
    await context.bot.send_message(
        g["chat_id"],
        f"🎯 {game.player_mention_html(next_player)}, it's your turn.\n"
        f"Make your guess: <code>/guess @player Guest Name</code>",
        parse_mode=ParseMode.HTML,
    )


async def _get_game_by_id(game_id: int) -> dict | None:
    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        row = await (await conn.execute(
            "SELECT * FROM games WHERE id = ?", (game_id,)
        )).fetchone()
        return dict(row) if row else None


# ============================================================
# /status, /cancelgame, /setmsg, /resetmsg
# ============================================================

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat.type == ChatType.PRIVATE:
        return
    g = await db.get_active_game(DB_PATH, chat.id)
    if not g:
        await update.message.reply_text("No active game in this chat.")
        return

    players = await db.get_players(DB_PATH, g["id"])
    lines = [f"<b>Game state:</b> {g['state']}"]
    if g["state"] == db.STATE_SUBMISSION:
        submitted = [p for p in players if p.get("guest_name")]
        lines.append(f"Submissions in: {len(submitted)}/{MAX_PLAYERS}")
    elif g["state"] == db.STATE_GUESSING:
        alive = [p for p in players if not p["eliminated"]]
        lines.append(f"Alive: {len(alive)} / {len(players)}")
        uid = game.current_turn_player_id(g)
        if uid:
            p = next((x for x in players if x["user_id"] == uid), None)
            if p:
                lines.append(f"Turn: {game.player_display(p)}")
        # Show the guest list with eliminated ones struck through
        lines.append("")
        lines.append("<b>Guest list:</b>")
        lines.append(game.format_guest_list_html(players))
    await update.message.reply_html("\n".join(lines))


async def cmd_cancelgame(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat.type == ChatType.PRIVATE:
        return
    g = await db.get_active_game(DB_PATH, chat.id)
    if not g:
        await update.message.reply_text("No active game.")
        return
    if update.effective_user.id != g["host_user_id"]:
        await update.message.reply_text("Only the host can cancel.")
        return
    await db.update_game_state(DB_PATH, g["id"], db.STATE_OVER)
    await update.message.reply_text("Game cancelled.")


async def cmd_setmsg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat.type == ChatType.PRIVATE:
        return
    settings = await db.get_or_create_settings(DB_PATH, chat.id)
    if settings["host_user_id"] != update.effective_user.id:
        await update.message.reply_text("Only the host can set message templates.")
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /setmsg <correct|wrong|announce|winner> <template>"
        )
        return
    key = context.args[0].lower()
    if key not in ("correct", "wrong", "announce", "winner"):
        await update.message.reply_text("Unknown key. Use: correct, wrong, announce, winner")
        return
    template = " ".join(context.args[1:])
    await db.set_message_template(DB_PATH, chat.id, key, template)
    await update.message.reply_text(f"Template '{key}' updated.")


async def cmd_resetmsg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat.type == ChatType.PRIVATE:
        return
    settings = await db.get_or_create_settings(DB_PATH, chat.id)
    if settings["host_user_id"] != update.effective_user.id:
        await update.message.reply_text("Only the host can reset templates.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /resetmsg <correct|wrong|announce|winner>")
        return
    key = context.args[0].lower()
    if key not in ("correct", "wrong", "announce", "winner"):
        await update.message.reply_text("Unknown key.")
        return
    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            f"UPDATE chat_settings SET msg_{key} = NULL WHERE chat_id = ?",
            (chat.id,),
        )
        await conn.commit()
    await update.message.reply_text(f"Template '{key}' reset to default.")


# ============================================================
# bootstrap
# ============================================================

async def post_init(app: Application) -> None:
    await db.init_db(DB_PATH)
    log.info("DB initialized at %s", DB_PATH)


def main() -> None:
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("sethost", cmd_sethost))
    app.add_handler(CommandHandler("newgame", cmd_newgame))
    app.add_handler(CommandHandler("closesubmissions", cmd_close_submissions))
    app.add_handler(CommandHandler("cancelgame", cmd_cancelgame))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("guess", cmd_guess))
    app.add_handler(CommandHandler("setmsg", cmd_setmsg))
    app.add_handler(CommandHandler("resetmsg", cmd_resetmsg))

    app.add_handler(CallbackQueryHandler(cb_announce, pattern=r"^announce:"))
    app.add_handler(CallbackQueryHandler(cb_custom, pattern=r"^custom:"))
    app.add_handler(CallbackQueryHandler(cb_pick_target, pattern=r"^picktarget:"))
    app.add_handler(CallbackQueryHandler(cb_pick_guest, pattern=r"^pickguest:"))
    app.add_handler(CallbackQueryHandler(cb_back_to_targets, pattern=r"^backtotargets:"))
    app.add_handler(CallbackQueryHandler(cb_cancel_pick, pattern=r"^cancelpick:"))

    # DM handler for guest submissions and host custom announcements
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
        dm_message,
    ))

    log.info("Starting bot...")
    app.run_polling()


if __name__ == "__main__":
    main()
