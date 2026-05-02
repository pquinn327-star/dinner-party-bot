# Dinner Party — Telegram Bot

A turn-based deduction game for 3–8 players plus a non-playing host.

## How the game works

1. Each player privately DMs the bot a "guest" name they're bringing to the party
2. Once submissions close, the bot announces the **anonymous** guest list to the group
3. Players take turns guessing **who brought which guest**
4. A correct guess eliminates the targeted player; wrong guesses cost nothing
5. Last unguessed player wins and goes first in the next game
6. The **host** is a non-playing moderator who confirms each guess and can post custom announcements

## Setup (~10 minutes)

### 1. Create your bot

1. Open Telegram, search for `@BotFather`, send `/newbot`
2. Give it a name and username
3. Copy the **bot token** it gives you
4. Send `/setprivacy` to BotFather → select your bot → choose **Disable** (so it can read `/guess` commands in groups)

### 2. Local install

```bash
python3.11 -m venv venv
source venv/bin/activate         # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# edit .env and paste your TELEGRAM_BOT_TOKEN
python bot.py
```

The bot uses long polling, so you don't need a public URL or webhook setup.

### 3. Add to your group

1. Add the bot to your Telegram group
2. **Each player must DM `/start` to the bot once** so it can DM them later (Telegram doesn't allow bots to DM strangers)
3. The host must do this too — otherwise adjudication DMs won't go through

### 4. Play

In the group:
- One person runs `/sethost` — they become the host (or reply to someone with `/sethost` to make them host)
- The host runs `/newgame` — opens a 5-minute submission window
- Each player DMs the bot one guest name
- The host runs `/closesubmissions` (or wait for the timer)
- Bot announces the anonymous guest list and the random turn order
- On your turn: `/guess @sarah Beyoncé`
- Host gets a DM with the guess + the bot's verdict (it knows the answer) and either taps "Announce" for the default message or "Custom message" to type their own

## Commands reference

**Setup**
- `/sethost` — make the message sender (or reply target) the host

**Game flow (host)**
- `/newgame` — open submission window
- `/closesubmissions` — end submission window early
- `/cancelgame` — abort current game

**Game flow (players)**
- `/guess @player Guest Name` — make your guess on your turn
- `/status` — see current state

**Customizing announcements (host)**
- `/setmsg correct <template>` — message when a guess is correct
- `/setmsg wrong <template>` — message when a guess is wrong
- `/setmsg announce <template>` — guest list reveal
- `/setmsg winner <template>` — winner announcement
- `/resetmsg <key>` — restore default

Available placeholders: `{guesser}`, `{target}`, `{guest}`, `{winner}`, `{guests}`, `{turn_order}`

Example:
```
/setmsg correct 🎉 Bullseye! {guesser} sniffed out that {target} brought {guest}. {target}, get out.
```

## Deploying to Railway (free tier)

1. Push this folder to a GitHub repo
2. Create a new Railway project from the repo
3. Set environment variable `TELEGRAM_BOT_TOKEN` in Railway's dashboard
4. Add a persistent volume mounted at `/app/data` and set `DATABASE_PATH=/app/data/dinner_party.db` (otherwise the DB resets on every deploy)
5. Add a `Procfile` containing: `worker: python bot.py`

That's it — the free tier easily handles this game's traffic.

## Architecture

- `bot.py` — Telegram handlers and main entry point
- `game.py` — game state machine (no Telegram I/O)
- `db.py` — async SQLite layer
- `messages.py` — default templates with placeholder substitution

State machine: `LOBBY → SUBMISSION_OPEN → GUESSING → OVER`

## Known limitations / things to extend later

- A user can only be in one active game at a time (across all groups). If you want multi-game support, add a way for users to specify which group their submission DM is for.
- The bot doesn't broadcast turn-deadline reminders. Could add with `job_queue.run_once`.
- No leaderboard yet — winner_id is stored per game but not aggregated.
- "Custom announcement" mode is per-host-DM-session; if the host abandons it mid-flow there's no timeout. Easy fix: clear `pending_custom_announce` after N seconds.
