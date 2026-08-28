"""
Where backups go, defined once for the whole cluster.

A component names a target; this file is what a name means. That split is the
same one the component model is built on — a database should not carry a copy of
an S3 endpoint, because then changing the endpoint means editing every database
that used it and finding the one you missed six months later.

S3 ONLY, AND THAT IS THE POINT
------------------------------
A backup that lives in the same cluster is not a backup. It survives a container
and a disk; it does not survive a mistake, a deleted project or a compromised
account, which are the three things people mean when they say they have backups.
Offering it as an option would have been this panel implying otherwise, so the
only kind is an S3-compatible bucket: AWS, Backblaze, Hetzner Object Storage, or
a MinIO of your own.

CREDENTIALS ARE DOCKER SECRETS, NOT ROWS IN THIS FILE
-----------------------------------------------------
A backup agent runs on whichever machine holds the database member, and the panel
cannot write a file there. Swarm can, at the mode and owner we ask for, which
makes a Swarm secret the only mechanism available — and they are immutable, so
rotation is a versioned name and a redeploy of the components using it. That is
the same dance `settings_def.py` documents for the cluster's own credentials, for
the same reason.

The definition lives in `state/storage.json` at 0600 and never contains a key.
"""

import json
import os
import re

from components import base, store

STATE_DIR = os.path.join(store.INFRA_DIR, "state")
PATH = os.path.join(STATE_DIR, "storage.json")

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")

KIND_S3 = "s3"
KINDS = (KIND_S3,)


def load():
    """[{name, kind, ...}] in file order. A missing file is an empty list."""
    try:
        with open(PATH) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def by_name(name):
    return next((t for t in load() if t.get("name") == name), None)


def names():
    return [t["name"] for t in load() if t.get("name")]


def check(target):
    """Human-readable problems with one target. Empty means it can be saved."""
    problems = []
    name = (target.get("name") or "").strip()
    if not NAME_RE.match(name):
        problems.append("A target name is lowercase letters, digits and dashes, "
                        "starting with a letter, 2 to 32 characters.")
    if target.get("kind") not in KINDS:
        problems.append(f"Kind must be one of: {', '.join(KINDS)}.")
    if not (target.get("bucket") or "").strip():
        problems.append("A target needs a bucket.")
    endpoint = (target.get("endpoint") or "").strip()
    if endpoint and not endpoint.startswith(("http://", "https://")):
        problems.append("The endpoint must start with http:// or https://.")
    if endpoint.startswith("http://"):
        # Not refused — a MinIO on a private network is a legitimate target —
        # but said out loud, because what travels over it is the database.
        problems.append("An http:// endpoint sends your entire database in "
                        "clear text. Use https:// unless this is a private "
                        "address you control end to end, and if it is, say so "
                        "by using https there too.")
    return problems


def save(target, access_key="", secret_key=""):
    """
    Write one target, creating or rotating its credential secret. Returns problems.

    Nothing is written when there are problems, and the CREDENTIAL is written
    first: a target row pointing at a secret that does not exist is a component
    that fails to deploy with a message about Swarm rather than about storage.
    """
    problems = check(target)
    if problems:
        return problems
    if access_key and secret_key:
        problems = write_credentials(target["name"], access_key, secret_key)
        if problems:
            return problems

    targets = [t for t in load() if t.get("name") != target["name"]]
    targets.append({k: v for k, v in target.items() if k != "secret_key"})
    targets.sort(key=lambda t: t["name"])
    store._write_atomic(PATH, json.dumps(targets, indent=2, sort_keys=True) + "\n",
                        0o600)
    return []


def remove(name, used_by=()):
    """
    Delete a target. Refuses while a component still names it.

    Refusing is the point: removing it silently would leave those components
    with a backup target that means nothing, and they would go on reporting
    success until somebody needed a restore.
    """
    if used_by:
        return [f"{name} is still the backup target for "
                f"{', '.join(sorted(used_by))}. Change those first."]
    targets = [t for t in load() if t.get("name") != name]
    store._write_atomic(PATH, json.dumps(targets, indent=2, sort_keys=True) + "\n",
                        0o600)
    return []


def secret_versions(name):
    prefix = f"storage-{name}-v"
    out = []
    for line in base.docker_out(["secret", "ls", "--format", "{{.Name}}"]).splitlines():
        line = line.strip()
        if line.startswith(prefix):
            try:
                out.append((int(line[len(prefix):]), line))
            except ValueError:
                continue
    return sorted(out)


def secret_name(name):
    versions = secret_versions(name)
    return versions[-1][1] if versions else ""


def write_credentials(name, access_key, secret_key):
    """
    Store one target's keys as the next version of a Swarm secret.

    Through stdin, never argv: the master's process table is readable by anything
    else on the box, and these are keys to a bucket holding your database. JSON
    rather than two secrets because they are one credential and rotating half of
    one is not a state worth being able to reach.
    """
    versions = secret_versions(name)
    version = (versions[-1][0] + 1) if versions else 1
    payload = json.dumps({"access_key": access_key, "secret_key": secret_key})
    ok, out = base.run(["docker", "secret", "create",
                        "--label", "infra.storage=" + name,
                        f"storage-{name}-v{version}", "-"], stdin=payload)
    if not ok:
        return [f"Could not store the credentials: {out}"]
    return []


def described(targets=None):
    """Rows for the panel."""
    out = []
    for target in (targets if targets is not None else load()):
        detail = f"{target.get('bucket', '')}/{target.get('prefix', '') or ''}".rstrip("/")
        out.append(dict(target,
                        where=target.get("endpoint") or "AWS S3",
                        detail=detail,
                        credential=secret_name(target.get("name", ""))))
    return out
