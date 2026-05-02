"""
Message templates for Dinner Party bot announcements.

Hosts can override any of these per-chat with /setmsg <key> <template>.
Available placeholders (any subset is fine):
    {guesser}   - name of the player making the guess
    {target}    - name of the player being guessed about
    {guest}     - the guest name
    {winner}    - winner's name (winner template only)
    {guests}    - newline-joined alphabetized guest list (announce template only)
    {turn_order} - arrow-joined turn order (announce template only)
"""

DEFAULT_CORRECT = (
    "✅ Spot on! {guesser} correctly identified that {target} brought {guest}.\n"
    "{target} is OUT."
)

DEFAULT_WRONG = (
    "❌ Wrong. {target} did not bring {guest}.\n"
    "{guesser}, you'll wait for your next turn."
)

DEFAULT_ANNOUNCE = (
    "🍽️ <b>Welcome to dinner.</b>\n\n"
    "Tonight's mystery guests are:\n{guests}\n\n"
    "Turn order:\n{turn_order}"
)

DEFAULT_WINNER = (
    "🏆 <b>{winner} wins!</b>\n"
    "Their guest was never correctly identified.\n"
    "They'll go first in the next game."
)


def render(template: str | None, default: str, **kwargs) -> str:
    """Render a template safely, falling back to default if needed."""
    chosen = template if template else default
    try:
        return chosen.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        # If a custom template uses an unknown placeholder, fall back
        return default.format(**kwargs)


def render_correct(template: str | None, guesser: str, target: str, guest: str) -> str:
    return render(template, DEFAULT_CORRECT, guesser=guesser, target=target, guest=guest)


def render_wrong(template: str | None, guesser: str, target: str, guest: str) -> str:
    return render(template, DEFAULT_WRONG, guesser=guesser, target=target, guest=guest)


def render_announce(template: str | None, guests: str, turn_order: str) -> str:
    return render(template, DEFAULT_ANNOUNCE, guests=guests, turn_order=turn_order)


def render_winner(template: str | None, winner: str) -> str:
    return render(template, DEFAULT_WINNER, winner=winner)
