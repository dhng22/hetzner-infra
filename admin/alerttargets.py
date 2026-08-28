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

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
TOKEN_RE = re.compile(r"^\d{4,}:[A-Za-z0-9_-]{20,}$")
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
    if not NAME_RE.match((target.get("name") or "").strip()):
        problems.append("A target name is lowercase letters, digits and dashes, "
                        "starting with a letter, 2 to 32 characters.")
    if target.get("kind") not in KINDS:
        problems.append(f"Kind must be one of: {', '.join(KINDS)}.")
    if target.get("kind") == KIND_TELEGRAM:
        token = (target.get("bot_token") or "").strip()
        if not token:
            problems.append("A Telegram target needs a bot token.")
        elif not TOKEN_RE.match(token):
            # Shape-checked, not merely non-empty. Two reasons, and the second
            # is the one that matters: a token is `<digits>:<secret>` so a
            # pasted username or a truncated copy is caught here rather than by
            # alerts silently not arriving — and this value is written into a
            # generated YAML file, so refusing everything outside this alphabet
            # is what makes that rendering incapable of producing a file
            # Alertmanager cannot parse. A whole monitoring stack that will not
            # deploy is an expensive way to find out a token had a newline in it.
            problems.append("That does not look like a Telegram bot token. They "
                            "are digits, a colon, then letters, digits, dashes "
                            "and underscores — as @BotFather gives them to you.")
        chat = str(target.get("chat_id") or "").strip()
        if not chat:
            problems.append("A Telegram target needs a chat id.")
        elif not CHAT_RE.match(chat):
            problems.append("A Telegram chat id is a number, and a group's is "
                            "negative. Message @userinfobot from the chat if you "
                            "are not sure what yours is.")
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
    """Rows for the panel, with the token masked rather than absent."""
    out = []
    for target in load():
        token = target.get("bot_token") or ""
        out.append({
            "name": target.get("name", ""),
            "kind": target.get("kind", ""),
            "where": f"chat {target.get('chat_id', '')}",
            # The token itself is NOT here. It was, unread by any template, and
            # a value that reaches a page is one render away from being on it.
            "masked": ("…" + token[-6:]) if len(token) > 6 else "set",
        })
    return out
