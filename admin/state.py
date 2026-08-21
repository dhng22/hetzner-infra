"""
Panel state that outlives a container restart: CI deploy tokens and deployment
history. Two small JSON files under /opt/infra/state, 0600.

Deliberately not a database. This is a single-replica panel pinned to the
manager on a box that already has the only copy of your metrics and Redis AOF;
adding a datastore to the control plane buys nothing and is one more thing to
back up.
"""

import json
import os
import re
import secrets
import time
from datetime import datetime, timezone

INFRA_DIR = os.environ.get("INFRA_DIR", "/opt/infra")
STATE_DIR = os.path.join(INFRA_DIR, "state")
TOKENS = os.path.join(STATE_DIR, "deploy-tokens.json")
HISTORY = os.path.join(STATE_DIR, "deployments.json")
HISTORY_MAX = 60

# A tag Swarm can actually act on. `latest` is rejected on purpose: Swarm
# compares the reference string, so redeploying an unchanged tag is a silent
# no-op and CI reports a success that never happened.
IMAGE_RE = re.compile(r"^[A-Za-z0-9][\w.\-/]*(:[\w][\w.\-]{0,127})?(@sha256:[a-f0-9]{64})?$")


def _read(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _write(path, payload):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# --- deploy tokens ---------------------------------------------------------
# One token per component, keyed by its name. This used to be keyed by
# (app_key, environment), which only made sense while "the app" was a single
# thing with exactly two environments. A component is one deployable unit and
# gets one token; two environments are two components and two tokens, which is
# also the blast radius you want — a leaked staging token cannot touch prod.

def token_for(name, create=True):
    tokens = _read(TOKENS, {})
    existing = tokens.get(name)
    if existing or not create:
        return existing
    tokens[name] = secrets.token_urlsafe(32)
    _write(TOKENS, tokens)
    return tokens[name]


def rotate_token(name):
    tokens = _read(TOKENS, {})
    tokens[name] = secrets.token_urlsafe(32)
    _write(TOKENS, tokens)
    return tokens[name]


def forget_token(name):
    """Called when a component is deleted, so a leaked token cannot outlive it."""
    tokens = _read(TOKENS, {})
    if tokens.pop(name, None) is not None:
        _write(TOKENS, tokens)


def verify_token(name, presented):
    import hmac
    expected = token_for(name, create=False)
    if not expected or not presented:
        return False
    return hmac.compare_digest(expected, presented)


# --- deployment history ----------------------------------------------------

def record(name, image, source, ok, detail="", actor=""):
    entries = _read(HISTORY, [])
    entries.insert(0, {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "epoch": time.time(),
        "component": name,
        "image": image,
        "source": source,          # "ci" | "panel"
        "actor": actor,
        "ok": bool(ok),
        "detail": detail[-2000:],
    })
    _write(HISTORY, entries[:HISTORY_MAX])


def history(name=None, limit=25):
    """
    Deployments, newest first. History is deliberately NOT dropped when a
    component is deleted: "what happened to the thing that used to be here" is
    the question you ask right after deleting the wrong one.
    """
    entries = _read(HISTORY, [])
    if name:
        entries = [e for e in entries if e.get("component") == name]
    return entries[:limit]


def validate_image(ref):
    """Returns an error string, or None when the reference is usable."""
    if not ref:
        return "No image was supplied. Send {\"image\": \"ghcr.io/you/app:sha-abc1234\"}."
    if len(ref) > 512:
        return "Image reference is too long."
    if not IMAGE_RE.match(ref):
        return f"{ref!r} is not a valid image reference."
    tag = ref.split("@")[0].rsplit(":", 1)
    if len(tag) == 1:
        return "Tag the image explicitly. An untagged reference means :latest."
    label = tag[1]
    # `latest`, and the `prod-latest` / `staging_latest` convention, are all
    # tags that get repointed at a new build. Deploying one asks Swarm to move a
    # service to the reference it is already on: digest resolution usually
    # notices, but when it cannot reach the registry Docker warns and proceeds
    # with the unchanged reference, which is a no-op your pipeline reports as
    # success. An immutable tag removes the question.
    if label == "latest" or label.endswith(("-latest", "_latest", ".latest")):
        return (f"Refusing the moving tag {label!r}. It gets repointed at each "
                f"build, so deploying it can silently no-op and report success. "
                f"Push it if you like, but deploy the immutable one "
                f"(e.g. prod-$GITHUB_SHA).")
    return None
