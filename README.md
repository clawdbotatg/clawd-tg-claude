# clawd-tg-claude — Telegram ↔ Claude Code

Talk to Claude Code from Telegram like a normal chat. Context persists; `/new` resets it.
Runs on your Claude **subscription** (no API key in env — `ANTHROPIC_API_KEY` is stripped
from the child). Full-access (`--dangerously-skip-permissions`), locked to one chat id.

Pure Python stdlib — no pip installs.

## Setup (one time)

1. **Make the bot:** in Telegram, message **@BotFather**, send `/newbot`, pick a name.
   Copy the token.
2. `cp .env.example .env` and paste the token → `TELEGRAM_BOT_TOKEN=...`
3. **Find your chat id:** run `python3 bot.py`, message the bot once; the console prints
   the chat id of whoever messaged. Paste it → `ALLOWED_CHAT_ID=...` and restart.
4. The bot now only answers you.

## Use

- Just talk. Every message → Claude, reply streams back, context carries over.
- `/new` — wipe context, start a clean session (CLAUDE.md + memory reload).
- `/whoami` — show current chat id, session id, working dir.

## Config

- `CLAUDE.md` — boot/orientation context loaded on every fresh session.
- `.env` → `WORKDIR` — launch cwd for Claude.
- `.env`, `state.json`, and `*.log` are gitignored. **Never commit `.env`.**

## Run on boot (macOS launchd)

Managed by a LaunchAgent (`RunAtLoad` + `KeepAlive`) so it auto-starts on login and
restarts on crash:

    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.buidlguidl.tg-agent.plist
    launchctl kickstart -k gui/$(id -u)/com.buidlguidl.tg-agent   # restart
    launchctl bootout   gui/$(id -u)/com.buidlguidl.tg-agent      # stop

Only one process may poll the Telegram token at a time — don't also run `bot.py` by hand.
