"""
Where components live on disk, and the rules for putting them there.

    /opt/infra/components/<name>/component.json   0640  the spec
    /opt/infra/components/<name>/env              0600  your environment
    /opt/infra/components/<name>/secret.env       0600  generated credentials
    /opt/infra/components/<name>/stack.yml        0600  last rendered stack

One directory per component, and one writer per file. That is the whole point:
the previous design had a single `config/app-<env>.env` edited by both the
application's environment editor and the database's credentials form, so a page
rendered before someone else's save would write their change back out again.
Nothing here is shared, so nothing here can be lost that way.

Stdlib only, deliberately. `bin/component` imports this module directly on the
master, where Flask and docker-py are not installed — they live in the panel's
image, not on the host.
"""

import json
import os
import re
import shutil
import tempfile
import time

INFRA_DIR = os.environ.get("INFRA_DIR", "/opt/infra")
COMPONENTS_DIR = os.path.join(INFRA_DIR, "components")

# Swarm object names, DNS labels and directory names all at once, so the
# strictest of the three wins. Leading letter because a name starting with a
# digit is a valid DNS label but reads as a mistake, and 32 characters because
# `<name>_redis-exporter` has to stay under Docker's 63-character service limit.
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")

# Taken by the infrastructure stacks. A component called `monitoring` would
# render a stack file that `docker stack deploy` merges into the real one.
RESERVED_NAMES = {"monitoring", "ingress", "admin", "edge", "docker", "swarm"}


class ComponentError(Exception):
    """Anything the caller should show a human rather than a traceback."""


def check_name(name):
    """Raises ComponentError, or returns the name unchanged."""
    if not name:
        raise ComponentError("A name is required.")
    if not NAME_RE.match(name):
        raise ComponentError(
            f"{name!r} is not a valid name. Use lowercase letters, digits and "
            "dashes, starting with a letter, 2 to 32 characters."
        )
    if name in RESERVED_NAMES:
        raise ComponentError(f"{name!r} is reserved by the infrastructure.")
    return name


def dir_for(name):
    return os.path.join(COMPONENTS_DIR, check_name(name))


def path_for(name, filename):
    return os.path.join(dir_for(name), filename)


def exists(name):
    return os.path.isfile(path_for(name, "component.json"))


def _write_atomic(path, text, mode):
    """
    Write via a temp file in the same directory, then rename.

    chmod happens on the temp file BEFORE the rename, so the final path never
    exists with default permissions — not even for the microsecond between
    create and chmod. These files hold database passwords.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_spec(name):
    path = path_for(name, "component.json")
    try:
        with open(path) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise ComponentError(f"No component named {name!r}.") from None
    except (OSError, ValueError) as exc:
        raise ComponentError(f"{path} is unreadable: {exc}") from None
    if not isinstance(data, dict) or "type" not in data:
        raise ComponentError(f"{path} is not a component spec.")
    data.setdefault("name", name)
    return data


def write_spec(name, data):
    data = dict(data, name=name)
    # Not setdefault: a fresh component carries created_at=None, and setdefault
    # treats a present-but-None key as already set.
    if not data.get("created_at"):
        data["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _write_atomic(path_for(name, "component.json"),
                  json.dumps(data, indent=2, sort_keys=True) + "\n", 0o640)
    return data


def list_names():
    try:
        entries = sorted(os.listdir(COMPONENTS_DIR))
    except OSError:
        return []
    return [e for e in entries if NAME_RE.match(e) and exists(e)]


def delete_dir(name):
    shutil.rmtree(dir_for(name), ignore_errors=True)


# --- env files --------------------------------------------------------------
# Same format `docker stack deploy` and every .env reader agree on: KEY=VALUE
# per line, blanks and # comments ignored. Parsing lives here rather than in the
# panel so the CLI and the panel cannot disagree about what a file means.

# What this FILE can hold, not what a shell would let you type. `MONGODB.DBNAME`
# and `spring.data.mongodb.uri` are real variable names that real applications
# read; the environment reaches the container as a compose `environment:`
# mapping and then through execve, neither of which cares about shell identifier
# rules. So the only names refused are the ones this format cannot round-trip:
# `=` is the separator, whitespace is stripped off both sides, and a line
# starting with `#` is read back as a comment and silently lost.
KEY_RE = re.compile(r"^[^\s=#][^\s=]*$")


def read_env(name, filename="env"):
    """Returns [{key, value}] in file order. A missing file is an empty one."""
    out = []
    try:
        with open(path_for(name, filename)) as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip():
                    out.append({"key": k.strip(), "value": v.strip()})
    except OSError:
        pass
    return out


def env_map(name, filename="env"):
    return {p["key"]: p["value"] for p in read_env(name, filename)}


def write_env(name, pairs, filename="env", header=None):
    lines = list(header or [])
    lines += [f"{p['key']}={p['value']}" for p in pairs]
    _write_atomic(path_for(name, filename), "\n".join(lines) + "\n", 0o600)


def validate_env(pairs):
    """Human-readable problems; empty means good to save."""
    problems, seen = [], set()
    for p in pairs:
        k = p["key"]
        if not KEY_RE.match(k):
            problems.append(f"{k!r} cannot be a variable name here: a name may "
                            f"not be empty, contain spaces or '=', or start "
                            f"with '#'.")
        elif k in seen:
            problems.append(f"{k} appears more than once.")
        seen.add(k)
        if "\n" in p["value"] or "\r" in p["value"]:
            problems.append(f"{k} cannot contain a line break.")
    return problems


def to_bulk(pairs):
    """The textarea form of the environment editor: one KEY=VALUE per line."""
    return "".join(f"{p['key']}={p['value']}\n" for p in pairs)


def parse_bulk(text):
    """
    The inverse of to_bulk. Returns (pairs, problems).

    Blank lines and whole-line `#` comments are dropped — the file has nowhere
    to keep them. A leading `export ` is accepted and stripped, because half the
    places people copy environment from are shell snippets. Nothing is unquoted:
    `A="b"` stores the quotes, because that is what the container receives, and
    silently disagreeing with the row editor about that would be worse.

    A line without `=` is an ERROR, not a silent skip. Dropping a pasted line
    without saying so is how you lose a variable and find out in production.
    Problems name the line number and never quote the line: these are
    credentials, and the message is rendered on a page someone may be sharing.
    """
    pairs, problems = [], []
    for n, raw in enumerate(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            problems.append(f"Line {n} is not KEY=VALUE.")
            continue
        k, v = line.split("=", 1)
        if not k.strip():
            problems.append(f"Line {n} has no variable name before the '='.")
            continue
        pairs.append({"key": k.strip(), "value": v.strip()})
    return pairs, problems
