"""
Where alerts go. A LIST, because one hard-coded destination was never the shape.

The bot token and the chat id used to be two rows on the Settings page, which
made "who gets alerted" a property of the cluster rather than a thing you have
any of. You could not have two — a team channel and a personal one — you could
not have none deliberately, and adding Slack meant editing a template by hand
and widening a check in `bin/stack-deploy` that nothing would have reminded you
about.

So: targets are created, listed and removed, alertmanager.yml is GENERATED from
them, and every alert goes to all of them. Telegram is the only kind
implemented; the shape is what makes a second one a `KINDS` entry and a render
function rather than a redesign.

    /opt/infra/state/alert_targets.json   0600   the list, tokens included

The token IS in that file, and that is the same trade every component's
credentials already make. A docker secret cannot be absent, so referencing one
would stop the whole monitoring stack deploying while alerting is unconfigured,
and it cannot be changed in place, so the panel could never edit it. The file is
root-only on the master and reaches Alertmanager as a bind mount, which is why
it does not appear in `docker service inspect` either.
"""

import json
import os
import re

from components import store

STATE_DIR = os.path.join(store.INFRA_DIR, "state")
PATH = os.path.join(STATE_DIR, "alert_targets.json")

#: PRESENCE, NOT SHAPE. There was a regex here for the bot token and another
#: for the chat id, and both rejected things that were perfectly valid — a
#: correctly pasted token, a channel named `@something` rather than numbered.
#:
#: The justification was that these values are written into generated YAML, and
#: that part was true; the conclusion was wrong. `bin/render-alertmanager` quotes
#: and escapes what it writes, so YAML safety was never resting on the pattern
#: match — and the target NAME never reaches that file at all, so its rule was
#: pure invention. Guessing the format of somebody else's credential and
#: refusing the paste is a worse failure than any it prevented: the alert that
#: never arrives is at least visible in the smoke test, whereas "that does not
#: look like a token" when it plainly is one leaves nowhere to go.
#:
#: What survives is the chat id, and only because Alertmanager leaves no choice:
#: it types that field as an int64, and `amtool check-config` rejects both
#: `chat_id: "-100123"` and `chat_id: "@channel"` with an unmarshal error. A
#: config it will not load is a monitoring stack that will not start, so that
#: one is checked here rather than discovered on deploy. The token is not, and
#: neither is the name — the name does not even reach the generated file.
CHAT_RE = re.compile(r"^-?\d{1,20}$")

KIND_TELEGRAM = "telegram"
#: Every kind that can be created today. Adding one means a value here, a branch
#: in `check`, and a branch in `receiver_config` — and nothing else, which is
#: the point of the list existing at all.
KINDS = (KIND_TELEGRAM,)


def load():
    try:
        with open(PATH) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def names():
    return [t["name"] for t in load() if t.get("name")]


def check(target):
    problems = []
    name = (target.get("name") or "").strip()
    if not name:
        problems.append("Give the target a name.")
    elif len(name) > 64:
        problems.append("That name is very long — keep it under 64 characters.")
    if target.get("kind") not in KINDS:
        problems.append(f"Kind must be one of: {', '.join(KINDS)}.")
    if target.get("kind") == KIND_TELEGRAM:
        if not (target.get("bot_token") or "").strip():
            problems.append("A Telegram target needs a bot token.")
        chat = str(target.get("chat_id") or "").strip()
        if not chat:
            problems.append("A Telegram target needs a chat id.")
        elif not CHAT_RE.match(chat):
            problems.append(
                "Alertmanager needs the chat id as a NUMBER — a group or channel "
                "is negative, like -1001234567890. A @name will not load, even "
                "though Telegram itself accepts one. Forward a message from the "
                "chat to @userinfobot if you need the number.")
    return problems


def save(target):
    problems = check(target)
    if problems:
        return problems
    targets = [t for t in load() if t.get("name") != target["name"]]
    targets.append(target)
    targets.sort(key=lambda t: t["name"])
    _write(targets)
    return []


def remove(name):
    _write([t for t in load() if t.get("name") != name])
    return []


def _write(targets):
    store._write_atomic(PATH, json.dumps(targets, indent=2, sort_keys=True) + "\n",
                        0o600)


def described():
    """
    Rows for the panel, token included.

    Shown behind the same click-to-reveal the database passwords and the deploy
    webhook token already use, rather than one-way masked. This is the panel's
    own copy of a credential it stored for you; being unable to read back what
    you pasted makes "is this the right token" unanswerable without going to the
    master with a shell.
    """
    out = []
    for target in load():
        out.append({
            "name": target.get("name", ""),
            "kind": target.get("kind", ""),
            "where": f"chat {target.get('chat_id', '')}",
            "token": target.get("bot_token") or "",
        })
    return out
