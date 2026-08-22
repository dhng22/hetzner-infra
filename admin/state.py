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


# --- what version of the infrastructure this cluster is running -------------

VERSION_FILE = os.path.join(STATE_DIR, "version.json")


def infra_version():
    """
    What `bin/infra-update` last did, for the panel to show.

    Three separate facts, and the third is the one that is easy to leave out:
    `checked_at`. A cluster that is up to date and a cluster whose updater died
    a week ago report the same commit — the only thing that distinguishes them
    is when the check last succeeded, so an absent or stale `checked_at` is the
    signal, not a missing detail.
    """
    data = _read(VERSION_FILE, {})
    commit = data.get("commit") or ""
    remote = data.get("remote") or ""
    return {
        "configured": bool(data),
        "commit": commit,
        "short": commit[:12],
        "branch": data.get("branch") or "",
        "updated_at": data.get("updated_at") or "",
        "checked_at": data.get("checked_at") or "",
        "previous": (data.get("previous") or "")[:12],
        "status": data.get("status") or "",
        "detail": data.get("detail") or "",
        # Known to be behind: the last check saw a commit we have not applied.
        # Normally momentary — the same run that notices this also applies it —
        # so seeing it persist means the apply half is failing.
        "behind": bool(remote and commit and remote != commit),
        "remote_short": remote[:12],
    }


# --- deployment history ----------------------------------------------------
#
# A deploy has THREE outcomes, not two, and the missing one is why this history
# used to lie. `docker stack deploy` is detached: it exits 0 once Swarm accepts
# the spec, which is before any task has started. Recording that exit code as
# the result meant every deploy was written down as a success — including the
# ones whose tasks then failed and were reverted by `failure_action: rollback`.
# The page said "deployed" while the service ran the previous image.
#
# So the CLI's exit code only decides PENDING vs FAILED, and the real verdict
# arrives later from Swarm's own UpdateStatus, via reconcile().

PENDING = "pending"      # accepted by Swarm, tasks not yet converged
DONE = "done"            # Swarm reports the rollout completed
FAILED = "failed"        # rejected outright, or rolled back after failing
SUPERSEDED = "superseded"  # another deploy overtook it; its fate is unknowable


def status_of(entry):
    """
    An entry's status, tolerating records written before statuses existed.

    Old entries have only `ok`, and every one of them says True because that is
    what the detached exit code reported. They are read as `done` rather than
    rewritten: inventing a verdict for a deploy nobody observed would be the
    same mistake in the other direction.
    """
    if entry.get("status"):
        return entry["status"]
    return DONE if entry.get("ok") else FAILED


def record(name, image, source, ok, detail="", actor="", status=None):
    """
    `status` defaults to PENDING for an accepted deploy, because acceptance is
    all a detached deploy can tell us. Callers that genuinely waited for
    convergence — the CI webhook runs `docker service update` attached — pass
    DONE explicitly and are believed.
    """
    if status is None:
        status = PENDING if ok else FAILED
    entries = _read(HISTORY, [])
    entries.insert(0, {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "epoch": time.time(),
        "component": name,
        "image": image,
        "source": source,          # "ci" | "panel"
        "actor": actor,
        "ok": bool(ok),
        "status": status,
        "detail": detail[-2000:],
    })
    _write(HISTORY, entries[:HISTORY_MAX])


def reconcile(name, verdict, started_epoch=None):
    """
    Settle this component's pending deploys against Swarm's verdict.

    Only the newest pending entry can claim the verdict: UpdateStatus describes
    the LAST rollout and nothing else, so anything older that is still pending
    was overtaken and its outcome is genuinely unknown. Marking those
    SUPERSEDED rather than guessing keeps the one property this whole file
    exists for — never report an outcome that was not observed.

    `started_epoch` guards against a stale verdict being pinned to a deploy that
    has only just been accepted: if Swarm's rollout began before this record was
    written, it is describing the previous deploy, so leave the record pending.
    """
    entries = _read(HISTORY, [])
    changed = False
    newest = None
    for entry in entries:                      # newest first
        if entry.get("component") != name or status_of(entry) != PENDING:
            continue
        if newest is None:
            newest = entry
        else:
            entry["status"] = SUPERSEDED
            changed = True

    if newest is not None and verdict in (DONE, FAILED):
        # 30s of slack: the record is written just before the CLI call returns,
        # so its timestamp can sit marginally after the rollout Swarm reports.
        if started_epoch is None or started_epoch >= newest.get("epoch", 0) - 30:
            newest["status"] = verdict
            newest["ok"] = verdict == DONE
            changed = True

    if changed:
        _write(HISTORY, entries)


#: How long a deploy may sit unconverged before it is called a failure. Longer
#: than any healthy rollout here: parallelism 1 over `monitor` 60s plus an image
#: pull, on the slowest component, with room to spare.
PENDING_GRACE_SECONDS = 15 * 60


def expire_pending(name, now=None):
    """
    Fail this component's pending deploys once they are too old to still be
    happening.

    Reconciliation can only settle a deploy that Swarm has an answer for. A
    deploy whose tasks never converge — no node can place them, the image never
    pulls — produces no answer at all, so without this it would read "deploying"
    forever, which is indistinguishable from a rollout that started ten seconds
    ago and is the reason "pending" was useless as a status.
    """
    now = now if now is not None else time.time()
    entries = _read(HISTORY, [])
    changed = False
    for entry in entries:
        if entry.get("component") != name or status_of(entry) != PENDING:
            continue
        if now - entry.get("epoch", now) > PENDING_GRACE_SECONDS:
            entry["status"] = FAILED
            entry["ok"] = False
            entry["detail"] = ((entry.get("detail") or "") +
                               "\n\nNever converged. Swarm accepted the spec but the "
                               "tasks did not reach running.")[-2000:]
            changed = True
    if changed:
        _write(HISTORY, entries)


def history(name=None, limit=25):
    """
    Deployments, newest first. History is deliberately NOT dropped when a
    component is deleted: "what happened to the thing that used to be here" is
    the question you ask right after deleting the wrong one.
    """
    entries = _read(HISTORY, [])
    if name:
        entries = [e for e in entries if e.get("component") == name]
    for entry in entries:
        entry["status"] = status_of(entry)
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
