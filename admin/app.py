"""
Swarm admin panel.

One Flask app, pinned to the manager, with the docker socket mounted. It is a
root-equivalent console: it can redeploy production and read every application
credential. Everything mutating goes through POST + CSRF, and every deploy goes
through bin/app-env or bin/stack-deploy rather than calling `docker stack
deploy` directly — those two are what preserve the autoscaler's replica count
and CI's deployed image.

The one exception to "everything needs a session" is the CI deploy webhook,
which is bearer-token authenticated instead. See deploy_hook().
"""

import os

from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, session, url_for)

import auth
import catalog
import envstore
import hostops
import registry
import settings_def
import state

PREVIEW = os.environ.get("PREVIEW") == "1"
data = __import__("fixtures" if PREVIEW else "swarm")

INFRA_DIR = os.environ.get("INFRA_DIR", "/opt/infra")
APP_NAME = os.environ.get("APP_NAME", "cluster")
ROOT_DOMAIN = os.environ.get("ROOT_DOMAIN", "")

_SESSION_SECRET = auth._secret_value("SESSION_SECRET", "")
if not _SESSION_SECRET:
    if not PREVIEW:
        # Falling back to a constant would let anyone who has read this repo
        # forge a signed session cookie and skip the login entirely.
        raise SystemExit("refusing to start: the SESSION_SECRET docker secret is missing")
    _SESSION_SECRET = "preview-only-not-a-real-secret"

# The panel is served over https through the tunnel, where Secure cookies are
# correct. Reaching it directly on http://<master-ip>:3000 would silently drop
# the cookie and loop you back to the login with no error, so it is a knob.
_COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() not in ("0", "false", "no")

app = Flask(__name__)
app.config.update(
    SECRET_KEY=_SESSION_SECRET,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=_COOKIE_SECURE and not PREVIEW,
    PERMANENT_SESSION_LIFETIME=60 * 60 * 12,
    MAX_CONTENT_LENGTH=1024 * 1024,
)

NAV = [
    {"key": "overview", "label": "Overview", "endpoint": "overview"},
    {"key": "apps", "label": "Apps", "endpoint": "apps"},
    {"key": "cluster", "label": "Cluster", "endpoint": "cluster"},
    {"key": "autoscaler", "label": "Autoscaler", "endpoint": "autoscaler"},
    {"key": "alerts", "label": "Alerts", "endpoint": "alerts"},
    {"key": "settings", "label": "Settings", "endpoint": "settings"},
]

TABS = ["overview", "environment", "credentials", "deployments", "swarm", "logs"]


# --- derived views ---------------------------------------------------------

def _pick(value, env_name):
    """Catalog fields may be a scalar or a per-environment mapping."""
    if isinstance(value, dict):
        return value.get(env_name)
    return value


def ui_access(app_entry, env_name):
    """
    Where this service's own UI actually lives. Returns None when it has none.

    `reachable` is the honest bit: an internal service is on the overlay
    network only, so linking to it would hand you a URL that times out. We
    show the address and say it is not published instead.
    """
    ui = app_entry.get("ui")
    if not ui:
        return None
    kind = ui["kind"]
    if kind == catalog.UI_TUNNEL:
        prefix = _pick(ui.get("prefix", {}), env_name) or ""
        return {"url": f"https://{prefix}{APP_NAME}.{ROOT_DOMAIN}", "reachable": True,
                "note": "Public hostname on the Cloudflare tunnel."}
    if kind == catalog.UI_HOST:
        port = _pick(ui.get("port"), env_name)
        return {"url": f"http://{data.master_ip()}:{port}", "reachable": True,
                "note": "Published on the master's private address. Reachable from "
                        "inside the private network, or over your VPN."}
    host = _pick(ui.get("host"), env_name)
    port = _pick(ui.get("port"), env_name)
    return {"url": f"http://{host}:{port}{ui.get('path', '')}", "reachable": False,
            "note": "Overlay network only — this address resolves inside the cluster, "
                    "not from your machine. Reach it with an SSH tunnel to the master."}


def redis_credentials(env_name):
    """Internal and external connection details for one Redis."""
    pairs = {p["key"]: p["value"] for p in envstore.load(env_name)} if not PREVIEW else \
            {p["key"]: p["value"] for p in _preview_pairs(env_name)}
    password = pairs.get("REDIS_PASSWORD", "")
    external_port = pairs.get("REDIS_EXTERNAL_PORT", "")
    host = f"redis-{env_name}"
    internal = f"redis://default:{password}@{host}:6379"
    external = (f"redis://default:{password}@{data.master_ip()}:{external_port}"
                if external_port else "")
    return {
        "env": env_name,
        "password": password,
        "internal_host": host,
        "internal_port": "6379",
        "internal_url": internal,
        "external_port": external_port,
        "external_host": data.master_ip(),
        "external_url": external,
    }


def _preview_pairs(env_name):
    svc = data.service(f"app_api-{env_name}")
    extra = {"prod": {"REDIS_EXTERNAL_PORT": "46379"}, "staging": {}}.get(env_name, {})
    merged = {**{k: v for k, v in svc["env"].items() if k not in envstore.RESERVED}, **extra}
    return [{"key": k, "value": v, "sensitive": bool(envstore.SENSITIVE.search(k))}
            for k, v in merged.items()]


def webhook_for(app_key, env_name):
    base = f"https://admin-{APP_NAME}.{ROOT_DOMAIN}"
    token = "preview-token-not-real-000000" if PREVIEW else state.token_for(app_key, env_name)
    return {
        "url": f"{base}/hooks/deploy/{app_key}/{env_name}",
        "token": token,
        "curl": (f"curl -fsS -X POST {base}/hooks/deploy/{app_key}/{env_name} \\\n"
                 f"  -H 'X-Deploy-Token: {token}' \\\n"
                 f"  -H 'Content-Type: application/json' \\\n"
                 f"  -d '{{\"image\": \"ghcr.io/you/app:sha-'\"$GITHUB_SHA\"'\"}}'"),
    }


def _section_href(item):
    return url_for(next(n["endpoint"] for n in NAV if n["key"] == item["key"]))


def _app_href(key, env=None, tab=None):
    kwargs = {"key": key}
    if env:
        kwargs["env"] = env
    if tab:
        kwargs["tab"] = tab
    return url_for("app_detail", **kwargs)


@app.context_processor
def _globals():
    # Templates never build URLs themselves. The preview build swaps these for
    # in-page anchors, which is how one set of markup serves both.
    return {
        "nav": NAV,
        "csrf_token": auth.csrf_token,
        "cluster_name": APP_NAME,
        "root_domain": ROOT_DOMAIN,
        "user": auth.current_user(),
        "preview": PREVIEW,
        "section_href": _section_href,
        "app_href": _app_href,
        "action_href": lambda key: url_for("app_action", key=key),
        "env_href": lambda key: url_for("save_env", key=key),
        "creds_href": lambda key: url_for("save_credentials", key=key),
        "token_href": lambda key: url_for("rotate_token", key=key),
        "settings_href": lambda: url_for("save_settings"),
        "firewall_href": lambda key: url_for("firewall", key=key),
        "registry_href": lambda: url_for("save_registry"),
    }


def _require_csrf():
    if not auth.check_csrf():
        abort(400, "Your session expired. Reload the page and try again.")


def _no_writes_in_preview():
    if PREVIEW:
        abort(400, "This is a preview build with dummy data — nothing is written.")


# --- auth ------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if auth.current_user():
        return redirect(url_for("overview"))
    error = None
    if request.method == "POST":
        ip = request.headers.get("CF-Connecting-IP", request.remote_addr or "?")
        if auth.locked_out(ip):
            error = "Too many failed attempts. Try again in a few minutes."
        elif auth.verify(request.form.get("username", ""),
                         request.form.get("password", ""), ip):
            auth.start_session(request.form.get("username", "").strip())
            nxt = request.args.get("next", "")
            # "//evil.com" and "/\\evil.com" both start with "/" and are
            # protocol-relative — a bare startswith("/") is an open redirect.
            safe = nxt.startswith("/") and not nxt.startswith(("//", "/\\"))
            return redirect(nxt if safe else url_for("overview"))
        else:
            error = "That username and password combination did not work."
    return render_template("login.html", error=error, configured=auth.configured())


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/healthz")
def healthz():
    return {"ok": True}, 200


# --- CI deploy webhook -----------------------------------------------------

@app.post("/hooks/deploy/<key>/<env_name>")
def deploy_hook(key, env_name):
    """
    Deploy an image, called by your build pipeline. Token-authenticated, so it
    is the one route that does not need a session.

    The graceful part is not implemented here — it is the service's own
    update_config (start-first, monitor 90s, failure_action rollback). This
    endpoint waits for that to converge and returns 502 when Swarm rolled the
    update back, so a bad image fails the pipeline instead of quietly reverting.

    If you put the panel behind Cloudflare Access, exempt this path or give CI
    an Access service token — otherwise Access will block the request before it
    ever reaches Flask.
    """
    entry = catalog.BY_KEY.get(key)
    if not entry or not entry.get("deployments") or env_name not in entry["environments"]:
        abort(404)

    presented = (request.headers.get("X-Deploy-Token")
                 or request.args.get("token", ""))
    if not (PREVIEW or state.verify_token(key, env_name, presented)):
        # Deliberately terse: a token probe learns nothing from the response.
        return jsonify(error="unauthorized"), 401

    payload = request.get_json(silent=True) or request.form or {}
    image = (payload.get("image") or "").strip()
    problem = state.validate_image(image)
    if problem:
        return jsonify(error=problem), 400

    service_name = entry["environments"][env_name]
    ok, output = data.deploy_image(service_name, image)
    if not PREVIEW:
        state.record(key, env_name, image, "ci", ok, output,
                     actor=request.headers.get("User-Agent", "")[:60])
    return jsonify(ok=ok, service=service_name, image=image, detail=output), (200 if ok else 502)


# --- pages -----------------------------------------------------------------

@app.get("/")
@auth.login_required
def overview():
    return render_template("page_overview.html", section="overview",
                           s=data.summary(), apps=data.apps(), alerts=data.alerts())


@app.get("/apps")
@auth.login_required
def apps():
    grouped = {}
    for a in data.apps():
        grouped.setdefault(a["category"], []).append(a)
    ordered = [(c, grouped[c]) for c in catalog.CATEGORIES if c in grouped]
    return render_template("page_apps.html", section="apps", grouped=ordered)


@app.get("/apps/<key>")
@auth.login_required
def app_detail(key):
    a = data.app(key)
    if not a:
        abort(404)
    env_name = request.args.get("env") or next(iter(a["environments"]))
    if env_name not in a["environments"]:
        abort(404)
    svc = a["envs"][env_name]
    editable = bool(a.get("editable")) and env_name in ("prod", "staging")
    creds = redis_credentials(env_name) if a.get("credentials") == "redis" else None
    deployable = bool(a.get("deployments"))

    pairs = None
    if editable:
        pairs = _preview_pairs(env_name) if PREVIEW else envstore.load(env_name)

    tab = request.args.get("tab", "overview")
    if tab not in TABS:
        tab = "overview"

    # Every tab panel is rendered so switching is instant and shareable; `tab`
    # only decides which one starts visible.
    return render_template(
        "page_app_detail.html", section="apps", app_entry=a, env_name=env_name,
        svc=svc, tab=tab, editable=editable, pairs=pairs, creds=creds,
        access=ui_access(a, env_name),
        deployments=data.history(key, env_name) if deployable else None,
        webhook=webhook_for(key, env_name) if deployable else None,
        firewall=_firewall_state(creds),
        registries=data.registry_logins() if deployable else None,
        logs=data.logs(svc["name"]),
    )


def _firewall_state(creds):
    """Whether the master's firewall currently lets the published port through."""
    if not creds:
        return None
    port = creds.get("external_port")
    return {
        "available": True if PREVIEW else hostops.available(),
        "port": port,
        "open": data.port_is_open(port) if port else False,
    }


@app.post("/apps/<key>/env")
@auth.login_required
def save_env(key):
    _require_csrf()
    _no_writes_in_preview()
    a = data.app(key)
    if not a or not a.get("editable"):
        abort(404)
    env_name = request.form.get("env", "")
    if env_name not in a["environments"]:
        abort(400, "Unknown environment.")

    pairs = [{"key": k.strip(), "value": v}
             for k, v in zip(request.form.getlist("key"), request.form.getlist("value"))
             if k.strip()]
    problems = envstore.validate(pairs)
    if problems:
        for p in problems:
            flash(p, "bad")
        return redirect(_app_href(key, env_name, "environment"))

    envstore.save(env_name, pairs)
    ok, output = envstore.deploy()
    state.record(key, env_name, a["envs"][env_name]["image"], "panel", ok,
                 output, actor=auth.current_user() or "")
    flash("Configuration saved and deployed." if ok
          else f"Saved, but the deploy failed: {output}", "ok" if ok else "bad")
    return redirect(_app_href(key, env_name, "environment"))


@app.post("/apps/<key>/credentials")
@auth.login_required
def save_credentials(key):
    """Redis password and optional external port, both stored in app-<env>.env."""
    _require_csrf()
    _no_writes_in_preview()
    a = data.app(key)
    if not a or a.get("credentials") != "redis":
        abort(404)
    env_name = request.form.get("env", "")
    if env_name not in ("prod", "staging"):
        abort(400, "Unknown environment.")

    password = request.form.get("password", "").strip()
    port = request.form.get("external_port", "").strip()
    if not password:
        flash("The Redis password cannot be empty.", "bad")
        return redirect(_app_href(key, env_name, "credentials"))
    if port and (not port.isdigit() or not 1 <= int(port) <= 65535):
        flash(f"{port!r} is not a valid port number.", "bad")
        return redirect(_app_href(key, env_name, "credentials"))

    pairs = [p for p in envstore.load(env_name)
             if p["key"] not in ("REDIS_PASSWORD", "REDIS_EXTERNAL_PORT")]
    pairs.append({"key": "REDIS_PASSWORD", "value": password})
    if port:
        pairs.append({"key": "REDIS_EXTERNAL_PORT", "value": port})

    envstore.save(env_name, pairs)
    ok, output = envstore.deploy()
    flash("Credentials saved. Redis and every client were redeployed together."
          if ok else f"Saved, but the deploy failed: {output}", "ok" if ok else "bad")
    return redirect(_app_href(key, env_name, "credentials"))


@app.post("/apps/<key>/token")
@auth.login_required
def rotate_token(key):
    _require_csrf()
    _no_writes_in_preview()
    env_name = request.form.get("env", "")
    entry = catalog.BY_KEY.get(key)
    if not entry or env_name not in entry.get("environments", {}):
        abort(404)
    state.rotate_token(key, env_name)
    flash(f"New deploy token issued for {env_name}. Update your pipeline — the old "
          f"one stops working immediately.", "ok")
    return redirect(_app_href(key, env_name, "deployments"))


@app.post("/apps/<key>/action")
@auth.login_required
def app_action(key):
    _require_csrf()
    _no_writes_in_preview()
    a = data.app(key)
    if not a:
        abort(404)
    env_name = request.form.get("env", next(iter(a["environments"])))
    action = request.form.get("action", "")
    svc_name = a["environments"].get(env_name)
    if not svc_name:
        abort(400, "Unknown environment.")

    if action == "redeploy":
        ok, out = data.redeploy_stack()
    elif action == "restart":
        ok, out = data.restart(svc_name)
    elif action == "rollback":
        ok, out = data.rollback(svc_name)
    elif action == "deploy-image":
        image = request.form.get("image", "").strip()
        problem = state.validate_image(image)
        if problem:
            flash(problem, "bad")
            return redirect(_app_href(key, env_name, "deployments"))
        ok, out = data.deploy_image(svc_name, image)
        state.record(key, env_name, image, "panel", ok, out, actor=auth.current_user() or "")
    else:
        abort(400, "Unknown action.")
    flash(out or ("Done." if ok else "Failed."), "ok" if ok else "bad")
    return redirect(_app_href(key, env_name))


@app.post("/apps/<key>/firewall")
@auth.login_required
def firewall(key):
    """Open or close a port on the master, over the restricted SSH channel."""
    _require_csrf()
    _no_writes_in_preview()
    a = data.app(key)
    if not a or a.get("credentials") != "redis":
        abort(404)
    env_name = request.form.get("env", "")
    if env_name not in ("prod", "staging"):
        abort(400, "Unknown environment.")
    port = request.form.get("port", "").strip()
    action = request.form.get("firewall_action", "")

    if action == "open":
        ok, out = hostops.open_port(port)
    elif action == "close":
        ok, out = hostops.close_port(port)
    else:
        abort(400, "Unknown action.")
    flash(out, "ok" if ok else "bad")
    return redirect(_app_href(key, env_name, "credentials"))


@app.post("/registry")
@auth.login_required
def save_registry():
    """
    Log in to a container registry so private images can be pulled.

    The credential lands in the docker config shared with the host, which is
    what `--with-registry-auth` reads when shipping it to workers.
    """
    _require_csrf()
    _no_writes_in_preview()
    if request.form.get("registry_action") == "logout":
        ok, out = registry.logout(request.form.get("registry", "").strip())
    else:
        ok, out = registry.login(
            request.form.get("registry", "").strip(),
            request.form.get("username", "").strip(),
            request.form.get("password", ""),
        )
        # Deliberately not written back to infra.env. The token can never be
        # synced there (we hold it only long enough to pipe into docker login),
        # so syncing just the username would leave a half-true pair that looks
        # authoritative. infra.env's GHCR_* are first-boot seeds; this is live.
    flash(out, "ok" if ok else "bad")
    return redirect(_app_href("app", request.form.get("env", "prod"), "deployments"))


@app.get("/cluster")
@auth.login_required
def cluster():
    return render_template("page_cluster.html", section="cluster",
                           nodes=data.nodes(), s=data.summary())


@app.get("/autoscaler")
@auth.login_required
def autoscaler():
    return render_template("page_autoscaler.html", section="autoscaler",
                           a=data.autoscaler_state(), groups=_settings_groups(["Scaling"]))


@app.get("/alerts")
@auth.login_required
def alerts():
    return render_template("page_alerts.html", section="alerts", alerts=data.alerts())


@app.get("/settings")
@auth.login_required
def settings():
    return render_template("page_settings.html", section="settings",
                           groups=_settings_groups())


@app.post("/settings")
@auth.login_required
def save_settings():
    _require_csrf()
    _no_writes_in_preview()
    values = envstore.load_infra()
    updates = {}
    for key in request.form.getlist("key"):
        if not settings_def.editable(key) or key not in values:
            continue
        submitted = request.form.get(f"value__{key}")
        if submitted is not None and submitted.strip() != values[key]:
            updates[key] = submitted.strip()

    if not updates:
        flash("Nothing changed.", "ok")
        return redirect(url_for("settings"))

    try:
        changed = envstore.save_infra(updates)
    except RuntimeError as exc:
        flash(str(exc), "bad")
        return redirect(url_for("settings"))

    stacks = settings_def.stacks_for(changed)
    messages = [f"Saved {', '.join(changed)}."]
    all_ok = True
    for stack in stacks:
        ok, out = envstore.deploy_stack(stack)
        all_ok = all_ok and ok
        messages.append(f"{stack}: {'redeployed' if ok else 'deploy failed — ' + out}")
    if not stacks:
        messages.append("No redeploy needed for these values.")
    flash(" ".join(messages), "ok" if all_ok else "bad")
    return redirect(url_for("settings"))


def _settings_groups(only=None):
    values = _infra_values()
    out = []
    for title, keys in settings_def.GROUPS:
        if only and title not in only:
            continue
        rows = []
        for k in keys:
            if k not in values:
                continue
            mode, stack, why = settings_def.describe(k)
            rows.append({
                "key": k, "value": values[k], "mode": mode, "stack": stack, "why": why,
                "masked": any(m in k for m in settings_def.MASK_HINT),
                "editable": mode == settings_def.EDIT,
            })
        if rows:
            out.append({"title": title, "rows": rows})
    return out


def _infra_values():
    if PREVIEW:
        return PREVIEW_INFRA
    return envstore.load_infra()


PREVIEW_INFRA = {
    "APP_NAME": "aichat", "ROOT_DOMAIN": "acme.dev", "HCLOUD_LOCATION": "hel1",
    "HCLOUD_NETWORK_NAME": "prod-net", "HCLOUD_SSH_KEY_NAME": "my-laptop",
    "WORKER_IMAGE": "ubuntu-24.04", "WORKER_TYPE": "cpx21",
    "HCLOUD_TOKEN": "hcl_9f2bc41d77aa0e35", "GHCR_USER": "acme-bot",
    "GHCR_TOKEN": "ghp_a71ccf20e9bb14d0",
    "APP_IMAGE_PROD": "ghcr.io/acme/aichat-api:sha-9f3ac21",
    "APP_IMAGE_STAGING": "ghcr.io/acme/aichat-api:sha-c40e8b7",
    "SLO_P95_MS": "500", "SCALE_UP_P95_RATIO": "0.8", "SCALE_DOWN_P95_RATIO": "0.4",
    "SCALE_UP_CPU": "70", "SCALE_DOWN_CPU": "30", "NODE_PRESSURE_PCT": "80",
    "MIN_REPLICAS": "2", "MAX_REPLICAS": "12", "REPLICAS_PER_WORKER": "2",
    "MIN_WORKERS": "1", "MAX_WORKERS": "6", "SUSTAIN_UP_SECONDS": "90",
    "SUSTAIN_DOWN_SECONDS": "900", "SCALE_UP_FACTOR": "0.5",
    "COOLDOWN_UP_SECONDS": "300", "COOLDOWN_DOWN_SECONDS": "900",
    "REPLICA_COOLDOWN_SECONDS": "60", "SCHEDULE_FLOOR": "08:00-20:00=2",
    "DRY_RUN": "false", "APP_CPU_LIMIT": "1.0", "APP_SERVICE": "app_api-prod",
    "ADMIN_USER": "admin", "ADMIN_PASSWORD": "hunter2hunter2",
    "GRAFANA_ADMIN_USER": "admin", "GRAFANA_ADMIN_PASSWORD": "s3cr3t-grafana",
    "CF_TUNNEL_TOKEN": "eyJhIjoiN2Y0MGQ5YTIi", "CI_SSH_PUBLIC_KEY": "ssh-ed25519 AAAAC3Nza...",
    "ALERT_WEBHOOK_URL": "https://hooks.slack.com/services/T0/B0/xY",
    "MONGO_URI_PROD": "mongodb+srv://appuser:s3cr3t@cluster0.mongodb.net/appdb",
    "MONGO_URI_STAGING": "mongodb+srv://appuser:s3cr3t@cluster0.mongodb.net/appdb_staging",
    "REDIS_PASSWORD_PROD": "8f2b91c4de77a0135be2", "REDIS_PASSWORD_STAGING": "b71ce0aa2f9384d51c60",
}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "3000")))
