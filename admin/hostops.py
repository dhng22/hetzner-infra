"""
The panel's channel to the host itself.

The docker socket covers everything inside the cluster, but the firewall is a
property of the master's kernel, so it needs a way out of the container. That
way is one SSH key whose authorized_keys entry forces bin/panel-hostops — the
panel cannot choose what runs, only which of that script's three verbs to ask
for. See the comments in bin/panel-hostops.
"""

import os
import subprocess

KEY = os.environ.get("PANEL_SSH_KEY", "/run/secrets/PANEL_SSH_KEY")
HOST = os.environ.get("MASTER_PRIVATE_IP", "")
USER = os.environ.get("PANEL_SSH_USER", "root")


def available():
    return bool(HOST) and os.path.exists(KEY)


def _ssh(command):
    if not available():
        return False, ("Host access is not configured. Set PANEL_SSH_KEY and "
                       "MASTER_PRIVATE_IP on the admin service.")
    argv = [
        "ssh",
        "-i", KEY,
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "UserKnownHostsFile=/tmp/panel_known_hosts",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=8",
        f"{USER}@{HOST}",
        command,
    ]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        out = (proc.stdout + proc.stderr).strip()
        return proc.returncode == 0, out
    except subprocess.TimeoutExpired:
        return False, "Timed out reaching the host over SSH."
    except OSError as exc:
        return False, f"Could not run ssh: {exc}"


def _valid_port(port):
    return str(port).isdigit() and 1 <= int(port) <= 65535 and int(port) != 22


def open_port(port):
    if not _valid_port(port):
        return False, f"{port!r} is not a port the panel may open."
    ok, out = _ssh(f"ufw-allow {int(port)}")
    return ok, out or f"Opened {port}/tcp on the master."


def close_port(port):
    if not _valid_port(port):
        return False, f"{port!r} is not a port the panel may close."
    ok, out = _ssh(f"ufw-deny {int(port)}")
    return ok, out or f"Closed {port}/tcp on the master."


def status():
    ok, out = _ssh("ufw-status")
    return ok, out


def repo_check():
    """
    Can the master still reach the repo it updates itself from?

    Asked of the MASTER rather than answered here, and that is the whole point:
    the panel has no git binary, and even if it had one it would be the wrong
    place to answer from. What matters is whether the process that does the
    pulling, on the machine it pulls from, with the credential as that machine
    reads it, can reach the repo — and the only honest way to know that is to
    make it try. `infra-update --check` stops before any change.

    Returns (ok, output). A cluster with no host access gets a truthful "cannot
    check" rather than a claim either way.
    """
    return _ssh("repo-check")


def port_is_open(port):
    """Best-effort read of whether ufw currently allows this port."""
    if not port:
        return False
    ok, out = status()
    if not ok:
        return False
    needle = f"{port}/tcp"
    return any(needle in line and "ALLOW" in line.upper() for line in out.splitlines())
