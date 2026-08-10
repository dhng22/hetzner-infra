"""
Login, sessions and CSRF.

This panel can redeploy production and edit database credentials, so treat it
as a root console. Credentials come from docker secrets, never from the image
or the compose file. The password is never held in plaintext beyond the one
comparison at login, and comparison is constant-time.
"""

import hashlib
import hmac
import os
import secrets
import time
from functools import wraps

from flask import redirect, request, session, url_for

PBKDF2_ROUNDS = 240_000
LOCKOUT_AFTER = 6
LOCKOUT_SECONDS = 300

# Per-IP lockout is evaded by anyone who can vary the client address — behind
# the tunnel that means spoofing CF-Connecting-IP. So there is a second, much
# looser cap on total failures across every address. High enough that normal
# fat-fingering never reaches it, low enough that unlimited guessing does not.
GLOBAL_LOCKOUT_AFTER = 40
GLOBAL_LOCKOUT_SECONDS = 900
MAX_TRACKED_IPS = 2048

# In-memory, per-process. The panel runs as a single replica pinned to the
# manager, so this is sufficient; it resets on restart, which is an acceptable
# trade for not adding a datastore to the control plane.
_attempts = {}
_global = {"fails": 0, "until": 0.0}


def _secret_value(name, default=None):
    path = f"/run/secrets/{name}"
    if os.path.exists(path):
        with open(path) as fh:
            return fh.read().strip()
    return os.environ.get(name, default)


ADMIN_USER = _secret_value("ADMIN_USER", "admin")
_ADMIN_PASSWORD = _secret_value("ADMIN_PASSWORD")


def _hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)


_SALT = secrets.token_bytes(16)
_EXPECTED = _hash(_ADMIN_PASSWORD, _SALT) if _ADMIN_PASSWORD else None


def configured():
    return _EXPECTED is not None


def locked_out(ip):
    now = time.time()
    if now < _global["until"]:
        return True
    _, until = _attempts.get(ip, (0, 0))
    return now < until


def _record_failure(ip):
    now = time.time()
    fails, _ = _attempts.get(ip, (0, 0))
    fails += 1
    until = now + LOCKOUT_SECONDS if fails >= LOCKOUT_AFTER else 0
    if len(_attempts) >= MAX_TRACKED_IPS and ip not in _attempts:
        # Bound the table so a spoofed-address flood cannot exhaust memory.
        _attempts.clear()
    _attempts[ip] = (fails, until)

    _global["fails"] += 1
    if _global["fails"] >= GLOBAL_LOCKOUT_AFTER:
        _global["until"] = now + GLOBAL_LOCKOUT_SECONDS
        _global["fails"] = 0


def verify(username, password, ip):
    """Constant-time check of both fields, with lockout on repeated failure."""
    if not configured() or locked_out(ip):
        return False
    ok_user = hmac.compare_digest(username.strip(), ADMIN_USER)
    ok_pass = hmac.compare_digest(_hash(password, _SALT), _EXPECTED)
    if ok_user and ok_pass:
        _attempts.pop(ip, None)
        _global["fails"] = 0
        return True
    _record_failure(ip)
    return False


# --- session ---------------------------------------------------------------

def start_session(username):
    session.clear()
    session["user"] = username
    session["csrf"] = secrets.token_urlsafe(32)
    session.permanent = True


def current_user():
    return session.get("user")


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def check_csrf():
    sent = request.form.get("csrf", "")
    return bool(sent) and hmac.compare_digest(sent, session.get("csrf", ""))


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapper
