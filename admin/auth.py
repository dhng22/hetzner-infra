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

# Every third failure locks the source address out, and each lock is twice as
# long as the one before it. Three is enough for a typo and a retry; the
# doubling is what makes guessing stop being worth doing at all — the tenth
# strike is already over two hours, and an attacker cannot wait it out faster by
# guessing faster. A fixed window can be ground against forever at 6 tries per
# window, which is what this replaces.
LOCKOUT_EVERY = 3
LOCKOUT_BASE_SECONDS = 60
#: Where the doubling stops. Long enough to be useless to a guesser, short
#: enough that locking yourself out is a coffee and not a redeploy of the panel.
LOCKOUT_MAX_SECONDS = 24 * 3600
#: Failures are forgotten after this long without one, so a lockout ladder
#: climbed on Monday is not still standing on Friday.
ATTEMPT_TTL_SECONDS = 24 * 3600

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
#
# ip -> {"fails": n, "until": epoch, "seen": epoch}
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


def retry_after(ip):
    """Seconds until this address may try again. 0 when it may try now."""
    now = time.time()
    entry = _attempts.get(ip)
    if entry and now - entry["seen"] > ATTEMPT_TTL_SECONDS:
        # Expired rather than merely elapsed: the ladder is forgotten too, so a
        # stale entry cannot make tomorrow's first typo an hour-long lockout.
        _attempts.pop(ip, None)
        entry = None
    until = max(_global["until"], entry["until"] if entry else 0)
    return max(0, int(until - now))


def locked_out(ip):
    return retry_after(ip) > 0


def _lockout_seconds(fails):
    """
    How long the nth lock lasts. Doubling, capped.

    `fails // LOCKOUT_EVERY` is the strike number, so 3 failures is the first
    lock and every further three doubles it. Nothing here decays a strike on its
    own — that is `ATTEMPT_TTL_SECONDS`, which drops the whole entry.
    """
    strike = fails // LOCKOUT_EVERY
    return min(LOCKOUT_BASE_SECONDS * (2 ** (strike - 1)), LOCKOUT_MAX_SECONDS)


def _record_failure(ip):
    now = time.time()
    entry = _attempts.get(ip)
    if entry is None or now - entry["seen"] > ATTEMPT_TTL_SECONDS:
        entry = {"fails": 0, "until": 0.0, "seen": now}
    entry["fails"] += 1
    entry["seen"] = now
    if entry["fails"] % LOCKOUT_EVERY == 0:
        entry["until"] = now + _lockout_seconds(entry["fails"])
    if len(_attempts) >= MAX_TRACKED_IPS and ip not in _attempts:
        # Bound the table so a spoofed-address flood cannot exhaust memory.
        _attempts.clear()
    _attempts[ip] = entry

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
