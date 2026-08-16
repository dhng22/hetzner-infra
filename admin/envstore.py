"""
Read and write config/app-<env>.env.

The panel edits these files and then hands off to bin/app-env, which is the
only thing in the system allowed to run `docker stack deploy`. Keeping the
deploy in one place is what stops a redeploy from resetting the autoscaler's
replica count or rolling back CI's image.
"""

import os
import re
import subprocess

# Topology owned by stacks/app.yml. Mirrors RESERVED in bin/app-env; the two
# must agree, so the panel refuses the same names the CLI refuses.
RESERVED = {"KTOR_ENV", "REDIS_HOST", "REDIS_PORT"}

# Shown masked in the UI. Not a security boundary — these are plain env vars by
# design — just a guard against reading a database password off a shared screen.
SENSITIVE = re.compile(r"(PASSWORD|SECRET|TOKEN|_KEY|URI|URL|DSN)$", re.I)

KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

INFRA_DIR = os.environ.get("INFRA_DIR", "/opt/infra")


def path_for(env_name):
    if env_name not in ("prod", "staging"):
        raise ValueError(f"unknown environment {env_name!r}")
    return os.path.join(INFRA_DIR, "config", f"app-{env_name}.env")


def load(env_name):
    """Returns [{key, value, sensitive}] preserving file order."""
    out = []
    try:
        with open(path_for(env_name)) as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                if k:
                    out.append({"key": k, "value": v.strip(),
                                "sensitive": bool(SENSITIVE.search(k))})
    except FileNotFoundError:
        pass
    return out


def validate(pairs):
    """Returns a list of human-readable problems; empty means good to save."""
    problems, seen = [], set()
    for p in pairs:
        k = p["key"]
        if not KEY_RE.match(k):
            problems.append(f"{k!r} is not a valid variable name.")
        elif k in RESERVED:
            problems.append(f"{k} is set by the stack file and cannot be overridden here.")
        elif k in seen:
            problems.append(f"{k} appears more than once.")
        seen.add(k)
        if "\n" in p["value"] or "\r" in p["value"]:
            problems.append(f"{k} cannot contain a line break.")
    return problems


def save(env_name, pairs):
    """Rewrite the file, keeping the explanatory header intact."""
    path = path_for(env_name)
    header = []
    try:
        with open(path) as fh:
            for raw in fh:
                if raw.strip().startswith("#") or not raw.strip():
                    header.append(raw.rstrip("\n"))
                else:
                    break
    except FileNotFoundError:
        header = [f"# {env_name} application configuration."]

    body = "\n".join(f"{p['key']}={p['value']}" for p in pairs)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        fh.write("\n".join(header).rstrip("\n") + "\n\n" + body + "\n")
    os.replace(tmp, path)


def _run(argv, timeout=420):
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return proc.returncode == 0, (proc.stdout + proc.stderr).strip()
    except subprocess.TimeoutExpired:
        return False, ("Timed out. The deploy may still be converging — check "
                       "`docker service ls` on the master.")
    except OSError as exc:
        return False, f"Could not run {argv[0]}: {exc}"


def deploy():
    """Redeploy the app stack through bin/app-env. Returns (ok, output)."""
    return _run([os.path.join(INFRA_DIR, "bin", "app-env"), "deploy"])


def deploy_stack(name):
    """Redeploy monitoring or admin. The app stack must go through deploy()."""
    if name == "app":
        return deploy()
    return _run([os.path.join(INFRA_DIR, "bin", "stack-deploy"), name])


# --- infra.env --------------------------------------------------------------
# Edited in place, comments and ordering preserved, so the file stays the
# readable document it is in the repo rather than a machine-written blob.

INFRA_ENV = os.environ.get("INFRA_ENV", "/etc/infra/infra.env")


def _split_comment(rest):
    """
    Split everything after the `=` into (value, trailing comment).

        "6    # a BUDGET cap"   -> ("6", "    # a BUDGET cap")
        "     # UTC; HH:MM=N"   -> ("",  "     # UTC; HH:MM=N")

    infra.env is a documented file — most lines carry a trailing comment
    explaining the number — and it is consumed two ways: `set -a; source` in
    bootstrap, and this parser in the panel. They have to agree, or the panel
    shows one thing and the cluster runs another.

    These are shell rules, deliberately:
      VAR=abc # note   -> abc      (whitespace before # starts a comment)
      VAR=abc#def      -> abc#def  (no whitespace, so it is part of the value)
      VAR=  # note     -> ''       (an empty value, not the comment text)
    The middle case matters: tokens and webhook URLs contain '#' and must not
    be truncated. A quoted value is taken whole for the same reason.

    `rest` must NOT be pre-stripped — the whitespace before a '#' is exactly
    what decides between the first case and the second.
    """
    stripped = rest.lstrip()
    lead = rest[:len(rest) - len(stripped)]
    if stripped[:1] in ("'", '"'):
        quote = stripped[0]
        end = stripped.find(quote, 1)
        if end != -1:
            return stripped[:end + 1], lead + stripped[end + 1:]
        return stripped, lead
    for i, ch in enumerate(stripped):
        # i == 0 is only a comment if something separated it from the '=';
        # `VAR=#x` assigns "#x", `VAR= #x` assigns "".
        if ch == "#" and (i > 0 and stripped[i - 1] in " \t" or i == 0 and lead):
            value = stripped[:i].rstrip()
            # Keep the separating whitespace with the comment. Re-emitting
            # `KEY=` + `# note` with nothing between them would turn the
            # comment into the value on the next `source` — `VAR=#x` assigns
            # "#x". The whitespace is what makes it a comment.
            return value, (rest[len(lead) + len(value):] if value else rest)
    return stripped.rstrip(), rest[len(lead) + len(stripped.rstrip()):]


def load_infra():
    values = {}
    try:
        with open(INFRA_ENV) as fh:
            for raw in fh:
                line = raw.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    # v is deliberately not stripped — see _split_comment.
                    values[k.strip()] = _split_comment(v)[0]
    except OSError:
        pass
    return values


def save_infra(updates):
    """
    Apply {key: value} to infra.env. Only keys already present are rewritten —
    the panel must not invent configuration the cloud-init does not document.
    Returns the keys that actually changed.
    """
    try:
        with open(INFRA_ENV) as fh:
            lines = fh.read().splitlines()
    except OSError as exc:
        raise RuntimeError(f"cannot read {INFRA_ENV}: {exc}") from exc

    changed, out = [], []
    for raw in lines:
        stripped = raw.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                new = str(updates[key])
                # Compare against, and re-attach, the value without its trailing
                # comment. Without this every save rewrites the line (the old
                # "value" still had the comment glued on), reports a change that
                # did not happen, and erases the documentation as it goes.
                old, comment = _split_comment(stripped.split("=", 1)[1])
                if new != old:
                    changed.append(key)
                indent = raw[:len(raw) - len(raw.lstrip())]
                out.append(f"{indent}{key}={new}{comment}")
                continue
        out.append(raw)

    if changed:
        tmp = f"{INFRA_ENV}.tmp"
        with open(tmp, "w") as fh:
            fh.write("\n".join(out) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, INFRA_ENV)
    return changed
