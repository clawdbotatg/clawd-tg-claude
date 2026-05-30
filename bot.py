#!/usr/bin/env python3
"""
Telegram <-> Claude Code bridge (streaming).

Talk to Claude Code from Telegram like a CLI chat:
  - every message you send is fed to `claude -p` and the reply STREAMS back live
  - text appears in a live-updating Telegram message as Claude writes it
  - tool activity (Bash, Edit, ...) shows as status lines so you see what it's doing
  - the conversation persists (session is resumed); /new starts fresh

Runs on your Claude subscription (NOT the API): ANTHROPIC_API_KEY is stripped
from the child env. Pure Python stdlib. No pip installs.
"""

import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "state.json")
ENV_PATH = os.path.join(HERE, ".env")


# ----------------------------- config -----------------------------

def load_env(path):
    vals = {}
    if not os.path.exists(path):
        return vals
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals


ENV = load_env(ENV_PATH)
TOKEN = ENV.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_CHAT_ID = str(ENV.get("ALLOWED_CHAT_ID", "")).strip()
WORKDIR = ENV.get("WORKDIR") or HERE
CLAUDE_BIN = ENV.get("CLAUDE_BIN") or shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
PERMISSION_FLAG = ENV.get("PERMISSION_FLAG", "--dangerously-skip-permissions")
CONTEXT_WINDOW_DEFAULT = int(ENV.get("CONTEXT_WINDOW", "200000"))

API = f"https://api.telegram.org/bot{TOKEN}"


def context_window(model):
    """Best-effort context window for the active model.

    A CONTEXT_WINDOW env value forces a fixed window; otherwise derive it from
    the model name. Claude 4.x Opus/Sonnet run with a 1M window here, so the old
    hardcoded 200k denominator made the fill % overshoot past 100%.
    """
    if ENV.get("CONTEXT_WINDOW"):
        return CONTEXT_WINDOW_DEFAULT
    m = (model or "").lower()
    if "opus-4" in m or "sonnet-4" in m:
        return 1_000_000
    return CONTEXT_WINDOW_DEFAULT


def ctx_tokens(usage):
    """Approx tokens occupying the context window for a turn."""
    if not usage:
        return 0
    return (usage.get("input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0))


# ----------------------------- state ------------------------------

def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {"session_id": None}


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f)


# --------------------------- telegram api -------------------------

def api_call(method, **params):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def md_to_html(text):
    """Render the subset of Markdown that survives Telegram's HTML parse_mode.

    Only *paired* delimiters become tags, so a half-streamed '**foo' stays a
    literal (escaped) until its closer arrives — the emitted HTML is always
    balanced. Telegram supports <b> <i> <code> <pre> <a>; headers/bullets have
    no tag, so we fold them into bold lines / bullet chars.
    """
    if not text:
        return text
    stash = []

    def keep(rendered):
        stash.append(rendered)
        return f"\x00{len(stash) - 1}\x00"

    # Pull code out first (before escaping) so its contents aren't markdown-parsed.
    text = re.sub(r"```[a-zA-Z0-9_+-]*\n?(.*?)```", lambda m: keep(f"<pre>{html.escape(m.group(1))}</pre>"), text, flags=re.S)
    text = re.sub(r"`([^`\n]+)`", lambda m: keep(f"<code>{html.escape(m.group(1))}</code>"), text)

    text = html.escape(text)

    # Stash links BEFORE emphasis so * / _ inside a URL or label can't be mangled.
    # url is already escaped above (&->&amp;), which is valid inside an href attr.
    text = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", lambda m: keep(f'<a href="{m.group(2)}">{m.group(1)}</a>'), text)

    # Emphasis: only *paired* delimiters become tags, so output is always balanced
    # and a half-streamed '**foo' stays a literal until its closer arrives.
    text = re.sub(r"\*\*([^\n]+?)\*\*", r"<b>\1</b>", text)                      # **bold**
    text = re.sub(r"(?<!\w)__([^\n]+?)__(?!\w)", r"<b>\1</b>", text)            # __bold__
    text = re.sub(r"(?<!\*)\*(?!\*)([^*\n]+?)\*(?!\*)", r"<i>\1</i>", text)      # *italic*
    text = re.sub(r"(?<!\w)_(?!_)([^_\n]+?)_(?!\w)", r"<i>\1</i>", text)        # _italic_ (not snake_case)
    text = re.sub(r"~~([^\n]+?)~~", r"<s>\1</s>", text)                          # ~~strike~~

    # Headers -> bold line (drop the #'s); strip any inner <b> so we don't nest.
    text = re.sub(r"^\s*#{1,6}\s+(.+)$", lambda m: "<b>" + re.sub(r"</?b>", "", m.group(1)) + "</b>", text, flags=re.M)
    # Bullets -> • (also numbered list markers kept as-is by not matching them)
    text = re.sub(r"^(\s*)[-*+]\s+", r"\1• ", text, flags=re.M)

    for i, rep in enumerate(stash):
        text = text.replace(f"\x00{i}\x00", rep)
    return text


def send_message(chat_id, text):
    if not text:
        return None
    r = api_call("sendMessage", chat_id=chat_id, text=md_to_html(text)[:4096], parse_mode="HTML")
    if not r.get("ok"):  # bad HTML / truncated tag — never drop the message
        r = api_call("sendMessage", chat_id=chat_id, text=text[:4096])
    return r.get("result", {}).get("message_id") if r.get("ok") else None


def edit_message(chat_id, message_id, text):
    r = api_call("editMessageText", chat_id=chat_id, message_id=message_id, text=md_to_html(text)[:4096], parse_mode="HTML")
    if not r.get("ok"):
        r = api_call("editMessageText", chat_id=chat_id, message_id=message_id, text=text[:4096])
    return bool(r.get("ok"))


def send_action(chat_id, action="typing"):
    api_call("sendChatAction", chat_id=chat_id, action=action)


def tg_poll(offset, timeout=30):
    url = f"{API}/getUpdates?timeout={timeout}&offset={offset}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout + 15) as resp:
        return json.load(resp)


# --------------------------- streamer -----------------------------

class Streamer:
    """Streams text into a live-updating Telegram message, throttled.

    A 'bubble' is one Telegram message. Text accumulates and we edit the
    bubble in place. When a tool runs (or the bubble gets too long) we
    finalize it and the next text starts a fresh bubble.
    """

    MIN_EDIT_INTERVAL = 1.2   # seconds between edits (Telegram-friendly)
    LIMIT = 3500              # split bubbles below Telegram's 4096 cap (HTML escaping/tags expand the source)

    def __init__(self, chat_id):
        self.chat_id = chat_id
        self.msg_id = None
        self.buf = ""
        self.rendered = ""
        self.last_edit = 0.0
        self.sent_anything = False

    def feed(self, text):
        self.buf += text
        while len(self.buf) > self.LIMIT:
            cut = self.buf.rfind("\n", 0, self.LIMIT)
            if cut <= 0:
                cut = self.LIMIT
            head, self.buf = self.buf[:cut], self.buf[cut:].lstrip("\n")
            self._push(head, force=True)
            self._new_bubble()
        self._push(self.buf)

    def _new_bubble(self):
        self.msg_id = None
        self.rendered = ""
        self.last_edit = 0.0

    def _push(self, text, force=False):
        text = text.strip("\n")
        if not text:
            return
        now = time.time()
        if not force and (now - self.last_edit) < self.MIN_EDIT_INTERVAL:
            return
        if text == self.rendered:
            return
        if self.msg_id is None:
            self.msg_id = send_message(self.chat_id, text)
            self.rendered = text
        elif edit_message(self.chat_id, self.msg_id, text):
            self.rendered = text
        self.last_edit = now
        self.sent_anything = True

    def block_done(self):
        """End of a text block: push the final state of the current bubble."""
        if self.buf.strip():
            self._push(self.buf, force=True)

    def end_bubble(self):
        """Finalize current bubble; next text starts a new one."""
        self.block_done()
        self._new_bubble()
        self.buf = ""

    def status(self, text):
        self.end_bubble()
        send_message(self.chat_id, text)
        self.sent_anything = True


# ----------------------------- claude -----------------------------

TOOL_EMOJI = {
    "Bash": "💻", "Edit": "✏️", "Write": "📝", "Read": "📖",
    "Grep": "🔎", "Glob": "🔎", "WebFetch": "🌐", "WebSearch": "🌐",
    "Task": "🤖", "TodoWrite": "✅",
}


def stream_claude(streamer, prompt, session_id):
    """Run one headless turn, streaming output to `streamer`.
    Returns (new_session_id, ok)."""
    cmd = [
        CLAUDE_BIN, "-p", prompt,
        "--output-format", "stream-json", "--verbose",
        "--include-partial-messages", PERMISSION_FLAG,
    ]
    if session_id:
        cmd += ["--resume", session_id]

    child_env = dict(os.environ)
    child_env.pop("ANTHROPIC_API_KEY", None)

    proc = subprocess.Popen(
        cmd, cwd=WORKDIR, env=child_env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    killer = threading.Timer(1800, proc.kill)
    killer.start()

    new_sid = session_id
    is_error = False
    model = None
    usage = None
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue

            t = ev.get("type")
            if ev.get("session_id"):
                new_sid = ev["session_id"]
            if not model:
                model = ev.get("model") or (ev.get("message") or {}).get("model")
            u = ev.get("usage") or (ev.get("message") or {}).get("usage")
            if u:
                usage = u

            if t == "stream_event":
                inner = ev.get("event", {})
                et = inner.get("type")
                if et == "content_block_start":
                    cb = inner.get("content_block", {})
                    if cb.get("type") == "tool_use":
                        name = cb.get("name", "tool")
                        emoji = TOOL_EMOJI.get(name, "🔧")
                        streamer.status(f"{emoji} {name}…")
                        send_action(streamer.chat_id)
                elif et == "content_block_delta":
                    delta = inner.get("delta", {})
                    if delta.get("type") == "text_delta":
                        streamer.feed(delta.get("text", ""))
                elif et == "content_block_stop":
                    streamer.block_done()
            elif t == "result":
                is_error = bool(ev.get("is_error"))
                if ev.get("result") and not streamer.sent_anything:
                    streamer.feed(ev["result"])
    finally:
        killer.cancel()
        proc.wait()
        streamer.end_bubble()

    if proc.returncode != 0 or is_error:
        return (new_sid, False, model, usage)
    return (new_sid, True, model, usage)


# ------------------------------ main ------------------------------

def handle_message(state, chat_id, text):
    cmd = text.strip().lower()

    if cmd in ("/new", "/clear", "/reset"):
        state["session_id"] = None
        state.pop("last_usage", None)
        state["ctx_warned"] = False
        save_state(state)
        send_message(chat_id, "🧹 Fresh session started. Context cleared (memory + CLAUDE.md still loaded).")
        return
    if cmd == "/start":
        send_message(chat_id, "👋 Connected to Claude Code. Just talk to me. /new starts a clean session.")
        return
    if cmd == "/whoami":
        send_message(chat_id, f"chat_id: {chat_id}\nsession: {state.get('session_id') or '(new)'}\nworkdir: {WORKDIR}")
        return
    if cmd == "/status":
        m = state.get("last_model") or "(unknown — send a message first)"
        used = ctx_tokens(state.get("last_usage"))
        if used:
            window = context_window(state.get("last_model"))
            pct = min(100, 100 * used // window)
            ctx_line = f"context: {used:,} / {window:,} ({pct}%)"
        else:
            ctx_line = "context: (no turn recorded yet)"
        send_message(chat_id, f"model: {m}\n{ctx_line}\nsession: {state.get('session_id') or '(new)'}")
        return

    send_action(chat_id)
    streamer = Streamer(chat_id)
    sid, ok, model, usage = stream_claude(streamer, text, state.get("session_id"))

    if not ok and state.get("session_id"):
        # resume probably failed; retry once with a fresh session
        send_message(chat_id, "(session expired — starting fresh)")
        streamer = Streamer(chat_id)
        sid, ok, model, usage = stream_claude(streamer, text, None)

    state["session_id"] = sid
    if model:
        state["last_model"] = model
    if usage:
        state["last_usage"] = usage
    save_state(state)

    if not streamer.sent_anything:
        send_message(chat_id, "(no output)" if ok else "(error — check tg-agent.log)")

    # one-time nudge as the context window fills up
    used = ctx_tokens(usage)
    window = context_window(model or state.get("last_model"))
    if used >= 0.8 * window and not state.get("ctx_warned"):
        pct = min(100, 100 * used // window)
        send_message(chat_id, f"⚠️ context {pct}% full — consider /new soon to stay sharp.")
        state["ctx_warned"] = True
        save_state(state)


def main():
    if not TOKEN:
        sys.exit("Missing TELEGRAM_BOT_TOKEN in .env")
    if not os.path.exists(CLAUDE_BIN):
        sys.exit(f"claude binary not found at {CLAUDE_BIN}")

    state = load_state()
    print(f"[tg-agent] up. workdir={WORKDIR} claude={CLAUDE_BIN}", flush=True)
    print(f"[tg-agent] allowed chat: {ALLOWED_CHAT_ID or '(ANY - set ALLOWED_CHAT_ID!)'}", flush=True)

    offset = 0
    while True:
        try:
            data = tg_poll(offset)
        except Exception as e:
            print(f"[poll error] {e}", file=sys.stderr, flush=True)
            time.sleep(3)
            continue

        for upd in data.get("result", []):
            offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("edited_message")
            if not msg or "text" not in msg:
                continue
            chat_id = str(msg["chat"]["id"])

            if ALLOWED_CHAT_ID and chat_id != ALLOWED_CHAT_ID:
                print(f"[ignored] unauthorized chat {chat_id}", flush=True)
                continue

            print(f"[msg from {chat_id}] {msg['text'][:80]}", flush=True)
            try:
                handle_message(state, chat_id, msg["text"])
            except Exception as e:
                print(f"[handle error] {e}", file=sys.stderr, flush=True)
                send_message(chat_id, f"(bridge error: {e})")


if __name__ == "__main__":
    main()
