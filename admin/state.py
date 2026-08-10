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

def token_for(app_key, env, create=True):
    tokens = _read(TOKENS, {})
    existing = tokens.get(app_key, {}).get(env)
    if existing or not create:
        return existing
    tokens.setdefault(app_key, {})[env] = secrets.token_urlsafe(32)
    _write(TOKENS, tokens)
    return tokens[app_key][env]


def rotate_token(app_key, env):
    tokens = _read(TOKENS, {})
    tokens.setdefault(app_key, {})[env] = secrets.token_urlsafe(32)
    _write(TOKENS, tokens)
    return tokens[app_key][env]


def verify_token(app_key, env, presented):
    import hmac
    expected = token_for(app_key, env, create=False)
    if not expected or not presented:
        return False
    return hmac.compare_digest(expected, presented)


# --- deployment history ----------------------------------------------------

def record(app_key, env, image, source, ok, detail="", actor=""):
    entries = _read(HISTORY, [])
    entries.insert(0, {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "epoch": time.time(),
        "app": app_key,
        "env": env,
        "image": image,
        "source": source,          # "ci" | "panel"
        "actor": actor,
        "ok": bool(ok),
        "detail": detail[-2000:],
    })
    _write(HISTORY, entries[:HISTORY_MAX])


def history(app_key=None, env=None, limit=25):
    entries = _read(HISTORY, [])
    if app_key:
        entries = [e for e in entries if e.get("app") == app_key]
    if env:
        entries = [e for e in entries if e.get("env") == env]
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
