"""
Container registry credentials.

Why this has to exist rather than relying on the bootstrap `docker login`:
`--with-registry-auth` ships the credential *from the client that runs the
deploy*. Bootstrap logs in on the host, so the host's ~/.docker/config.json has
it — but a deploy triggered from this panel runs inside the admin container,
which has its own filesystem. Without the same config the deploy would ship no
credential and every worker created afterwards would fail to pull with
"no basic auth credentials", long after the deploy reported success.

So stacks/admin.yml mounts the host's /root/.docker into the container. Both
see one file: bootstrap's login is visible here, and a login performed here is
visible to the host. This module is what performs that login.
"""

import json
import os
import subprocess

DOCKER_CONFIG = os.environ.get("DOCKER_CONFIG", "/root/.docker")
CONFIG_JSON = os.path.join(DOCKER_CONFIG, "config.json")


def logins():
    """Registries the shared docker config currently holds a credential for."""
    try:
        with open(CONFIG_JSON) as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return []
    out = []
    for host, entry in (cfg.get("auths") or {}).items():
        user = ""
        raw = entry.get("auth")
        if raw:
            import base64
            try:
                user = base64.b64decode(raw).decode("utf-8", "replace").split(":", 1)[0]
            except Exception:
                user = ""
        out.append({"registry": host, "username": user})
    return sorted(out, key=lambda r: r["registry"])


def login(registry, username, password):
    """
    Run `docker login`. The password goes in on stdin, never argv — argv is
    world-readable in /proc on the host that shares this PID namespace.
    """
    if not registry or not username or not password:
        return False, "Registry, username and password are all required."
    if any(c.isspace() for c in registry) or any(c.isspace() for c in username):
        return False, "Registry and username cannot contain whitespace."

    os.makedirs(DOCKER_CONFIG, exist_ok=True)
    try:
        proc = subprocess.run(
            ["docker", "login", registry, "--username", username, "--password-stdin"],
            input=password, capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return False, f"Timed out talking to {registry}."
    except OSError as exc:
        return False, f"Could not run docker: {exc}"

    out = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        return False, out or f"Login to {registry} was rejected."
    return True, (f"Logged in to {registry} as {username}. New deploys ship this "
                  f"credential to workers.")


def logout(registry):
    try:
        proc = subprocess.run(["docker", "logout", registry],
                              capture_output=True, text=True, timeout=30)
        return proc.returncode == 0, (proc.stdout + proc.stderr).strip()
    except OSError as exc:
        return False, f"Could not run docker: {exc}"
