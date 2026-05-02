"""
Solo test harness for Dinner Party.

Simulates a full game with N fake players against the real game logic
and database. No Telegram involved — this directly exercises game.py
and db.py.

Run with:  python solo_test.py
Or:        python solo_test.py --players 5 --auto

Modes:
    interactive (default) — you control each fake player turn by turn
    auto                  — bot plays itself, useful to verify happy path

Each run uses a fresh temporary database so it never touches your real
dinner_party.db.
"""

import argparse
import asyncio
import json
import random
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import db
import game
import messages


# ---------- pretty printing ----------

class C:
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GREEN = "\033[32m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    MAGENTA = "\033[35m"
    RESET = "\033[0m"

    @staticmethod
    def disable():
        for attr in ("BOLD", "DIM", "GREEN", "RED", "YELLOW", "CYAN", "MAGENTA", "RESET"):
            setattr(C, attr, "")


def banner(text: str) -> None:
    print(f"\n{C.BOLD}{C.CYAN}{'=' * 60}{C.RESET}")
    print(f"{C.BOLD}{C.CYAN}  {text}{C.RESET}")
    print(f"{C.BOLD}{C.CYAN}{'=' * 60}{C.RESET}\n")


def group(text: str) -> None:
    """Simulate a message posted to the group chat."""
    print(f"{C.MAGENTA}[GROUP]{C.RESET} {text}")


def host_dm(text: str) -> None:
    print(f"{C.YELLOW}[HOST DM]{C.RESET} {text}")


def player_dm(name: str, text: str) -> None:
    print(f"{C.CYAN}[DM → {name}]{C.RESET} {text}")


def info(text: str) -> None:
    print(f"{C.DIM}[debug] {text}{C.RESET}")


def html_to_console(text: str) -> str:
    """Strip basic HTML tags so output is readable in a terminal."""
    import re
    text = re.sub(r"</?b>", "", text)
    text = re.sub(r"<a [^>]*>([^<]*)</a>", r"\1", text)
    text = re.sub(r"<code>([^<]*)</code>", r"\1", text)
    return text


# ---------- fake players ----------

DEFAULT_NAMES = ["Alice", "Bob", "Charlie", "Diana", "Eve", "Frank", "Gina", "Hank"]
DEFAULT_GUESTS = [
    "Beyonce", "Drake", "Tom Hanks", "Oprah", "Elon Musk",
    "Taylor Swift", "Barack Obama", "Rihanna",
]


def make_players(n: int) -> list[dict]:
    return [
        {
            "user_id": 1000 + i,
            "username": DEFAULT_NAMES[i].lower(),
            "display_name": DEFAULT_NAMES[i],
            "guest": DEFAULT_GUESTS[i],
        }
        for i in range(n)
    ]


# ---------- the simulated game flow ----------

async def run_game(num_players: int, auto: bool) -> None:
    db_path = tempfile.mktemp(suffix=".db")
    info(f"using temp database at {db_path}")
    await db.init_db(db_path)

    chat_id = 1
    host_user_id = 99
    players = make_players(num_players)

    banner("SETUP")
    print(f"Players: {', '.join(p['display_name'] for p in players)}")
    print(f"Host: <non-playing admin>")
    print(f"Mode: {'AUTO (bot plays itself)' if auto else 'INTERACTIVE (you control each player)'}")

    # 1. Host runs /sethost and /newgame
    await db.set_host(db_path, chat_id, host_user_id)
    deadline = datetime.now(timezone.utc) + timedelta(seconds=300)
    game_id = await db.create_game(db_path, chat_id, host_user_id, deadline)
    group("🍽️ A new Dinner Party is forming. Submit your guests via DM.")

    # 2. Each player DMs their guest
    banner("SUBMISSION PHASE")
    for p in players:
        g = await db.get_active_game(db_path, chat_id)

        # Mock User-like object the function expects
        class FakeUser:
            id = p["user_id"]
            first_name = p["display_name"]
            last_name = None
            username = p["username"]

        if auto:
            guest = p["guest"]
        else:
            guest = input(f"{p['display_name']}'s guest [default: {p['guest']}]: ").strip() or p["guest"]
            p["guest"] = guest  # keep override for later

        ok, msg = await game.record_submission(db_path, g, FakeUser(), guest)
        player_dm(p["display_name"], html_to_console(msg))
        if not ok:
            print(f"{C.RED}Submission failed; aborting.{C.RESET}")
            return

    # 3. Close submissions
    banner("CLOSING SUBMISSIONS")
    g = await db.get_active_game(db_path, chat_id)
    ok, msg, order = await game.close_submissions(db_path, g, min_players=3)
    if not ok:
        print(f"{C.RED}{msg}{C.RESET}")
        return

    g = await db.get_active_game(db_path, chat_id)
    plist = await db.get_players(db_path, game_id)
    settings = await db.get_or_create_settings(db_path, chat_id)
    announcement = game.render_announcement(settings.get("msg_announce"), plist, order)
    group(html_to_console(announcement))

    # 4. Guessing loop
    banner("GUESSING PHASE")
    by_id = {p["user_id"]: p for p in players}

    while True:
        g = await db.get_active_game(db_path, chat_id)
        if not g or g["state"] != db.STATE_GUESSING:
            break

        current_uid = game.current_turn_player_id(g)
        current = by_id[current_uid]
        all_players = await db.get_players(db_path, game_id)
        alive = [p for p in all_players if not p["eliminated"]]
        alive_others = [p for p in alive if p["user_id"] != current_uid]
        guests = game.alphabetized_guests(all_players)
        eliminated_guests = game.eliminated_guest_set(all_players)

        # Render guest list with strikethrough markers for the debug view
        guest_display = [
            (game.strikethrough_unicode(g_name) if g_name.lower() in eliminated_guests else g_name)
            for g_name in guests
        ]

        group(f"🎯 @{current['username']}, it's your turn.")
        print(f"{C.DIM}    Alive: {[p['display_name'] for p in alive]}{C.RESET}")
        print(f"{C.DIM}    Guest list: {guest_display}{C.RESET}")

        # Decide guess
        if auto:
            target = random.choice(alive_others)
            # Bias slightly toward correct guesses to keep games short, but
            # not always — we want to exercise both code paths.
            if random.random() < 0.4:
                guest_name = target["guest_name"]
            else:
                guest_name = random.choice(guests)
            print(f"{C.DIM}    [auto] {current['display_name']} guesses {target['display_name']} brought {guest_name}{C.RESET}")
        else:
            # Interactive: show numbered options
            print(f"\n{C.BOLD}{current['display_name']}'s turn.{C.RESET} Pick a target:")
            for i, p in enumerate(alive_others):
                print(f"  [{i}] {p['display_name']}")
            while True:
                try:
                    sel = input("Target #: ").strip()
                    target_idx = int(sel)
                    if 0 <= target_idx < len(alive_others):
                        target = alive_others[target_idx]
                        break
                except ValueError:
                    pass
                print("Invalid; try again.")

            print("Pick a guest:")
            for i, gn in enumerate(guests):
                marker = " (already identified)" if gn.lower() in eliminated_guests else ""
                display = (game.strikethrough_unicode(gn)
                           if gn.lower() in eliminated_guests else gn)
                print(f"  [{i}] {display}{marker}")
            while True:
                try:
                    sel = input("Guest #: ").strip()
                    guest_idx = int(sel)
                    if 0 <= guest_idx < len(guests):
                        guest_name = guests[guest_idx]
                        break
                except ValueError:
                    pass
                print("Invalid; try again.")

        # Submit guess via the same path the bot uses
        target_input = f"@{target['username']}"
        ok, error_msg, pending_id = await game.validate_and_record_guess(
            db_path, g, current_uid, target_input, guest_name,
        )
        if not ok:
            print(f"{C.RED}Validation rejected: {error_msg}{C.RESET}")
            continue

        pending = await db.get_pending_guess(db_path, pending_id)
        target_player = await db.get_player(db_path, game_id, pending["target_id"])
        guesser = await db.get_player(db_path, game_id, pending["guesser_id"])

        group(f"📨 @{guesser['username']} guesses: {target_player['display_name']} brought {pending['guest_name']}. Awaiting host...")

        verdict = "✅ CORRECT" if pending["is_correct"] else "❌ WRONG"
        host_dm(f"Guesser: {guesser['display_name']} | Target: {target_player['display_name']} | Guest: {pending['guest_name']} | {verdict}")

        # Adjudicate (auto: just announce default)
        if pending["is_correct"]:
            announce = messages.render_correct(
                None,
                guesser=guesser["display_name"],
                target=target_player["display_name"],
                guest=pending["guest_name"],
            )
        else:
            announce = messages.render_wrong(
                None,
                guesser=guesser["display_name"],
                target=target_player["display_name"],
                guest=pending["guest_name"],
            )
        group(html_to_console(announce))

        if pending["is_correct"]:
            await db.eliminate_player(db_path, game_id, pending["target_id"])
        await db.resolve_guess(db_path, pending_id)

        # Correct → same player goes again; Wrong → advance rotation
        g = await db.get_active_game(db_path, chat_id)
        if pending["is_correct"]:
            alive_now = await game.alive_players(db_path, game_id)
            if len(alive_now) <= 1:
                if len(alive_now) == 1:
                    winner = alive_now[0]
                    await db.set_winner(db_path, game_id, winner["user_id"])
                    await db.set_last_winner(db_path, chat_id, winner["user_id"])
                    winner_msg = messages.render_winner(None, winner=winner["display_name"])
                    banner("GAME OVER")
                    group(html_to_console(winner_msg))
                else:
                    banner("GAME ENDED (no winner)")
                break
            group(f"🎯 @{current['username']}, you're on a streak — guess again!")
            # turn index stays the same; loop continues with same current_uid
        else:
            next_player = await game.advance_turn(db_path, g)
            if next_player is None:
                alive_now = await game.alive_players(db_path, game_id)
                if len(alive_now) == 1:
                    winner = alive_now[0]
                    await db.set_winner(db_path, game_id, winner["user_id"])
                    await db.set_last_winner(db_path, chat_id, winner["user_id"])
                    winner_msg = messages.render_winner(None, winner=winner["display_name"])
                    banner("GAME OVER")
                    group(html_to_console(winner_msg))
                else:
                    banner("GAME ENDED (no winner)")
                break

        if not auto:
            input(f"\n{C.DIM}[press Enter for next turn]{C.RESET}")


def parse_args():
    ap = argparse.ArgumentParser(description="Solo test harness for Dinner Party")
    ap.add_argument("--players", type=int, default=4,
                    help="Number of fake players (3–8). Default: 4")
    ap.add_argument("--auto", action="store_true",
                    help="Bot plays itself end-to-end (no input needed)")
    ap.add_argument("--seed", type=int, default=None,
                    help="Random seed for reproducible auto runs")
    ap.add_argument("--no-color", action="store_true",
                    help="Disable ANSI colors")
    args = ap.parse_args()
    if args.players < 3 or args.players > 8:
        print("--players must be between 3 and 8", file=sys.stderr)
        sys.exit(1)
    return args


def main():
    args = parse_args()
    # Ensure UTF-8 output on Windows so emoji don't crash the console
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if args.no_color:
        C.disable()
    elif sys.platform == "win32":
        # Enable ANSI VT mode on Windows 10+
        try:
            import os
            os.system("")
        except Exception:
            C.disable()
    if args.seed is not None:
        random.seed(args.seed)
    asyncio.run(run_game(args.players, args.auto))


if __name__ == "__main__":
    main()
