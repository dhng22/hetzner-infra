"""
Swarm admin panel.

One Flask app, pinned to the manager, with the docker socket mounted. It is a
root-equivalent console: it can deploy anything and read every credential in the
cluster. Everything mutating goes through POST + CSRF.

The routes are generic. `/components/<name>` renders whatever tabs that
component declares, `/components/<name>/action` dispatches whatever verbs it
offers, and the create and settings forms are built from its `fields()`. Adding
a component type touches none of this file — which is the difference from the
version this replaces, where a dozen routes branched on `key == "app"`,
`credentials == "redis"` and `env in ("prod", "staging")`.

The one exception to "everything needs a session" is the CI deploy webhook,
which is bearer-token authenticated instead. See deploy_hook().
"""

import os

from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, session, url_for)

import auth
import catalog
import components
import envstore
import hostops
import registry
import settings_def
import state
from components import store as component_store

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
    {"key": "components", "label": "Components", "endpoint": "components_index"},
    {"key": "cluster", "label": "Cluster", "endpoint": "cluster"},
    {"key": "autoscaler", "label": "Autoscaler", "endpoint": "autoscaler"},
    {"key": "alerts", "label": "Alerts", "endpoint": "alerts"},
    {"key": "settings", "label": "Settings", "endpoint": "settings"},
]


# --- derived views ---------------------------------------------------------

def system_access(entry):
    """
    Where an infrastructure service's own UI lives. None when it has none.

    `reachable` is the honest bit: an internal service is on the overlay network
    only, so linking to it would hand you a URL that times out. We show the
    address and say it is not published instead.
    """
    ui = entry.get("ui")
    if not ui:
        return None
    if ui["kind"] == catalog.UI_TUNNEL:
        return {"url": f"https://{ui.get('prefix', '')}{APP_NAME}.{ROOT_DOMAIN}",
                "reachable": True, "note": "Public hostname on the Cloudflare tunnel."}
    if ui["kind"] == catalog.UI_HOST:
        return {"url": f"http://{data.master_ip()}:{ui['port']}", "reachable": True,
                "note": "Published on the master's private address. Reachable from "
                        "inside the private network, or over your VPN."}
    return {"url": f"http://{ui['host']}:{ui['port']}{ui.get('path', '')}",
            "reachable": False,
            "note": "Overlay network only — this address resolves inside the cluster, "
                    "not from your machine. Reach it with an SSH tunnel to the master."}


def webhook_for(name):
    base = f"https://admin-{APP_NAME}.{ROOT_DOMAIN}"
    token = "preview-token-not-real-000000" if PREVIEW else state.token_for(name)
    return {
        "url": f"{base}/hooks/deploy/{name}",
        "token": token,
        "curl": (f"curl -fsS -X POST {base}/hooks/deploy/{name} \\\n"
                 f"  -H 'X-Deploy-Token: {token}' \\\n"
                 f"  -H 'Content-Type: application/json' \\\n"
                 f"  -d '{{\"image\": \"ghcr.io/you/app:sha-'\"$GITHUB_SHA\"'\"}}'"),
    }


def _section_href(item):
    return url_for(next(n["endpoint"] for n in NAV if n["key"] == item["key"]))


def _component_href(name, tab=None):
    kwargs = {"name": name}
    if tab:
        kwargs["tab"] = tab
    return url_for("component_detail", **kwargs)


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
        "types": components.TYPES,
        "system_access": system_access,
        "section_href": _section_href,
        "component_href": _component_href,
        "action_href": lambda name: url_for("component_action", name=name),
        "env_href": lambda name: url_for("save_component_env", name=name),
        "spec_href": lambda name: url_for("save_component_spec", name=name),
        "delete_href": lambda name: url_for("delete_component", name=name),
        "token_href": lambda name: url_for("rotate_token", name=name),
        "firewall_href": lambda name: url_for("firewall", name=name),
        "creds_href": lambda name: url_for("save_credentials", name=name),
        "new_href": lambda type_name: url_for("component_new", type=type_name),
        "create_href": lambda: url_for("component_create"),
        "settings_href": lambda: url_for("save_settings"),
        "registry_href": lambda: url_for("save_registry"),
        "stack_href": lambda: url_for("deploy_system"),
        "logout_href": lambda: url_for("logout"),
    }


def _require_csrf():
    if not auth.check_csrf():
        abort(400, "Your session expired. Reload the page and try again.")


def _no_writes_in_preview():
    if PREVIEW:
        abort(400, "This is a preview build with dummy data — nothing is written.")


def _load(name):
    try:
        return components.load(name)
    except components.ComponentError:
        abort(404)


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
    # CSRF-checked like every other POST. Signing someone out is a small harm,
    # but it is still a state change a third-party page should not be able to
    # trigger, and "it is only logout" is how the exception list starts.
    _require_csrf()
    session.clear()
    return redirect(url_for("login"))


@app.get("/healthz")
def healthz():
    return {"ok": True}, 200


# --- CI deploy webhook -----------------------------------------------------

@app.post("/hooks/deploy/<name>")
def deploy_hook(name):
    """
    Deploy an image, called by your build pipeline. Token-authenticated, so it
    is the one route that does not need a session.

    The graceful part is not implemented here — it is the service's own
    update_config (start-first, monitor, failure_action rollback). This endpoint
    waits for that to converge and returns 502 when Swarm rolled the update
    back, so a bad image fails the pipeline instead of quietly reverting.

    If you put the panel behind Cloudflare Access, exempt this path or give CI
    an Access service token — otherwise Access blocks the request before it ever
    reaches Flask.
    """
    try:
        component = components.load(name)
    except components.ComponentError:
        abort(404)
    if component.TYPE != "app":
        abort(404)

    presented = request.headers.get("X-Deploy-Token") or request.args.get("token", "")
    if not (PREVIEW or state.verify_token(name, presented)):
        # Deliberately terse: a token probe learns nothing from the response.
        return jsonify(error="unauthorized"), 401

    payload = request.get_json(silent=True) or request.form or {}
    image = (payload.get("image") or "").strip()
    problem = state.validate_image(image)
    if problem:
        return jsonify(error=problem), 400

    ok, output = data.deploy_image(component.service, image)
    if not PREVIEW:
        state.record(name, image, "ci", ok, output,
                     actor=request.headers.get("User-Agent", "")[:60])
    return jsonify(ok=ok, component=name, service=component.service,
                   image=image, detail=output), (200 if ok else 502)


# --- pages -----------------------------------------------------------------

@app.get("/")
@auth.login_required
def overview():
    return render_template("page_overview.html", section="overview",
                           s=data.summary(), views=data.component_views(),
                           alerts=data.alerts(), topo=data.topology())


@app.get("/api/topology")
@auth.login_required
def api_topology():
    """
    Live feed for the cluster map on the Overview page.

    Read-only and session-authenticated like every other page — it exposes
    service names and task counts, the same thing the page already renders, so
    there is nothing extra to leak. The page renders server-side first and this
    only refreshes it, so the view still works with JS off.
    """
    return jsonify(data.topology())


# --- components ------------------------------------------------------------

@app.get("/components")
@auth.login_required
def components_index():
    views = data.component_views()
    grouped = {}
    for view in views:
        grouped.setdefault(view["category"], []).append(view)
    order = ["Application", "Data"]
    ordered = ([(c, grouped[c]) for c in order if c in grouped]
               + [(c, v) for c, v in grouped.items() if c not in order])
    return render_template("page_components.html", section="components",
                           grouped=ordered, views=views, new_groups=components.groups())


@app.get("/components/new")
@auth.login_required
def component_new():
    type_name = request.args.get("type", "app")
    if type_name not in components.TYPES:
        abort(404)
    cls = components.TYPES[type_name]
    return render_template("page_component_new.html", section="components",
                           cls=cls, type_name=type_name, values={}, problems=[], name="",
                           siblings=components.types_in_group(cls.GROUP))


@app.post("/components")
@auth.login_required
def component_create():
    _require_csrf()
    _no_writes_in_preview()
    type_name = request.form.get("type", "")
    if type_name not in components.TYPES:
        abort(400, "Unknown component type.")
    name = request.form.get("name", "").strip()
    cls = components.TYPES[type_name]

    try:
        component, problems = components.create(type_name, name, request.form)
    except components.ComponentError as exc:
        problems, component = [str(exc)], None

    if problems:
        # Re-render with what they typed rather than redirecting and losing it.
        return render_template("page_component_new.html", section="components",
                               cls=cls, type_name=type_name, values=request.form,
                               problems=problems, name=name,
                               siblings=components.types_in_group(cls.GROUP)), 400

    if request.form.get("deploy_now"):
        ok, output = component.deploy()
        state.record(name, component.spec.get("image", ""), "panel", ok, output,
                     actor=auth.current_user() or "")
        flash(f"Created {name} and deployed it." if ok
              else f"Created {name}, but the deploy failed: {output}",
              "ok" if ok else "bad")
    else:
        flash(f"Created {name}. It is not deployed yet.", "ok")
    return redirect(_component_href(name))


@app.get("/components/<name>")
@auth.login_required
def component_detail(name):
    component = _load(name)
    view = data.component_view(component)
    tabs = component.tabs()
    tab = request.args.get("tab", tabs[0][0])
    if tab not in [t[0] for t in tabs]:
        tab = tabs[0][0]

    # Only the open tab's expensive data is fetched. `logs` shells out with a
    # 30s timeout and the firewall probe is an SSH round trip with another;
    # doing both on every request made the Overview tab wait for two things it
    # does not render.
    extra = {}
    if tab == "logs":
        extra["logs"] = data.logs(component.service)
    if tab == "deployments":
        extra["webhook"] = webhook_for(name)
        extra["deployments"] = data.history(name)
        extra["registries"] = data.registry_logins()
    if tab == "credentials" and component.TYPE == "redis":
        extra["creds"] = _redis_credentials(component)
        extra["firewall"] = _firewall_state(component)

    return render_template("page_component_detail.html", section="components",
                           component=component, view=view, tabs=tabs, tab=tab,
                           fields=type(component).fields(),
                           env_pairs=component_store.read_env(name), **extra)


def _redis_credentials(component):
    """
    Built once, in Python, and rendered from that.

    The old page assembled the internal URL in Jinja and the external one here,
    so the two could disagree by construction — and one of them was a value
    computed and never used.
    """
    port = component.spec.get("external_port")
    master = data.master_ip()
    return {
        "password": component.password(),
        "internal_host": component.service,
        "internal_port": "6379",
        "internal_url": component.connection_url(),
        "external_port": port,
        "external_host": master,
        "external_url": component.connection_url(master, port) if port else "",
    }


def _firewall_state(component):
    """Whether the master's firewall currently lets the published port through."""
    port = component.spec.get("external_port")
    return {
        "available": True if PREVIEW else hostops.available(),
        "port": port,
        "open": data.port_is_open(port) if port else False,
    }


@app.post("/components/<name>/env")
@auth.login_required
def save_component_env(name):
    _require_csrf()
    _no_writes_in_preview()
    component = _load(name)

    # Two editors, one form. The client disables whichever one is not on screen,
    # so exactly one of them is submitted and there is no question of which view
    # wins — the one you were looking at when you pressed save does. With JS off
    # the textarea is disabled in the markup, so the rows are what arrives.
    if "bulk" in request.form:
        pairs, problems = component_store.parse_bulk(request.form.get("bulk", ""))
    else:
        pairs = [{"key": k.strip(), "value": v}
                 for k, v in zip(request.form.getlist("key"), request.form.getlist("value"))
                 if k.strip()]
        problems = []
    problems += component_store.validate_env(pairs)
    if problems:
        for problem in problems:
            flash(problem, "bad")
        return redirect(_component_href(name, "environment"))

    component_store.write_env(name, pairs, header=[f"# Environment for {name}.", ""])
    ok, output = component.deploy()
    state.record(name, component.live_image() or component.spec.get("image", ""),
                 "panel", ok, output, actor=auth.current_user() or "")
    flash("Environment saved and deployed." if ok
          else f"Saved, but the deploy failed: {output}", "ok" if ok else "bad")
    return redirect(_component_href(name, "environment"))


@app.post("/components/<name>/settings")
@auth.login_required
def save_component_spec(name):
    _require_csrf()
    _no_writes_in_preview()
    _load(name)
    component, problems = components.update(name, request.form)
    if problems:
        for problem in problems:
            flash(problem, "bad")
        return redirect(_component_href(name, "settings"))

    ok, output = component.deploy()
    flash("Settings saved and deployed." if ok
          else f"Saved, but the deploy failed: {output}", "ok" if ok else "bad")
    return redirect(_component_href(name, "settings"))


@app.post("/components/<name>/action")
@auth.login_required
def component_action(name):
    _require_csrf()
    _no_writes_in_preview()
    component = _load(name)
    verb = request.form.get("action", "")

    # Deploying a specific image is the one action that carries an argument, so
    # it is handled here rather than in the component's own verb table.
    if verb == "deploy-image":
        image = request.form.get("image", "").strip()
        problem = state.validate_image(image)
        if problem:
            flash(problem, "bad")
            return redirect(_component_href(name, "deployments"))
        ok, output = data.deploy_image(component.service, image)
        state.record(name, image, "panel", ok, output, actor=auth.current_user() or "")
        flash(output or ("Deployed." if ok else "Failed."), "ok" if ok else "bad")
        return redirect(_component_href(name, "deployments"))

    handler = component.actions().get(verb)
    if handler is None or handler[0] is None:
        abort(400, "Unknown action.")
    ok, output = handler[0]()
    flash(output or ("Done." if ok else "Failed."), "ok" if ok else "bad")
    return redirect(_component_href(name))


@app.post("/components/<name>/delete")
@auth.login_required
def delete_component(name):
    _require_csrf()
    _no_writes_in_preview()
    component = _load(name)
    # Typed confirmation, because this removes a stack and its spec, and the
    # difference between `api` and `api-staging` is eight characters.
    if request.form.get("confirm", "").strip() != name:
        flash(f"Type {name} exactly to confirm the delete.", "bad")
        return redirect(_component_href(name, "settings"))
    ok, output = component.remove()
    state.forget_token(name)
    flash(output or (f"Removed {name}." if ok else "Failed."), "ok" if ok else "bad")
    return redirect(url_for("components_index") if ok else _component_href(name))


@app.post("/components/<name>/token")
@auth.login_required
def rotate_token(name):
    _require_csrf()
    _no_writes_in_preview()
    _load(name)
    state.rotate_token(name)
    flash(f"New deploy token issued for {name}. Update your pipeline — the old "
          f"one stops working immediately.", "ok")
    return redirect(_component_href(name, "deployments"))


@app.post("/components/<name>/credentials")
@auth.login_required
def save_credentials(name):
    """
    Set or regenerate a component's own credentials.

    Blank means "generate", both here and on the create form, because a database
    with no password is not a state worth being able to reach by leaving a field
    empty. Nothing else in the cluster holds a copy, so the only thing this can
    break is your own client — which is the trade that makes it a form field
    rather than a versioned-secret dance.
    """
    _require_csrf()
    _no_writes_in_preview()
    component = _load(name)
    if not type(component).SECRETS:
        abort(404)

    if request.form.get("regenerate"):
        component.rotate_secrets()
    else:
        problems = component.apply_secrets(request.form)
        if problems:
            for problem in problems:
                flash(problem, "bad")
            return redirect(_component_href(name, "credentials"))

    ok, output = component.deploy()
    flash("Credentials saved and the service restarted with them." if ok
          else f"Saved, but the redeploy failed: {output}", "ok" if ok else "bad")
    return redirect(_component_href(name, "credentials"))


@app.post("/components/<name>/firewall")
@auth.login_required
def firewall(name):
    """Open or close a port on the master, over the restricted SSH channel."""
    _require_csrf()
    _no_writes_in_preview()
    _load(name)
    port = request.form.get("port", "").strip()
    action = request.form.get("firewall_action", "")
    if action == "open":
        ok, output = hostops.open_port(port)
    elif action == "close":
        ok, output = hostops.close_port(port)
    else:
        abort(400, "Unknown action.")
    flash(output, "ok" if ok else "bad")
    return redirect(_component_href(name, "credentials"))


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
        ok, output = registry.logout(request.form.get("registry", "").strip())
    else:
        ok, output = registry.login(
            request.form.get("registry", "").strip(),
            request.form.get("username", "").strip(),
            request.form.get("password", ""),
        )
        # Deliberately not written back to infra.env. The token can never be
        # synced there (we hold it only long enough to pipe into docker login),
        # so syncing just the username would leave a half-true pair that looks
        # authoritative. infra.env's GHCR_* are first-boot seeds; this is live.
    flash(output, "ok" if ok else "bad")
    back = request.form.get("component", "")
    return redirect(_component_href(back, "deployments") if back
                    else url_for("components_index"))


# --- cluster ---------------------------------------------------------------

@app.get("/cluster")
@auth.login_required
def cluster():
    return render_template("page_cluster.html", section="cluster",
                           nodes=data.nodes(), s=data.summary(),
                           system=data.system_view())


@app.post("/cluster/stack")
@auth.login_required
def deploy_system():
    """Redeploy an infrastructure stack. Components deploy themselves."""
    _require_csrf()
    _no_writes_in_preview()
    stack = request.form.get("stack", "")
    if stack not in catalog.SYSTEM_STACKS:
        abort(400, "Not an infrastructure stack.")
    ok, output = data.deploy_system_stack(stack)
    flash(output or (f"{stack} redeployed." if ok else "Failed."), "ok" if ok else "bad")
    return redirect(url_for("cluster"))


@app.get("/autoscaler")
@auth.login_required
def autoscaler():
    return render_template("page_autoscaler.html", section="autoscaler",
                           a=data.autoscaler_state(),
                           scaling_groups=_settings_groups(only=AUTOSCALER_GROUPS))


@app.get("/alerts")
@auth.login_required
def alerts():
    return render_template("page_alerts.html", section="alerts", alerts=data.alerts(),
                           destination=data.alert_destination())


# --- settings --------------------------------------------------------------

@app.get("/settings")
@auth.login_required
def settings():
    return render_template("page_settings.html", section="settings",
                           groups=_settings_groups(skip=AUTOSCALER_GROUPS),
                           elsewhere=AUTOSCALER_GROUPS)


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
        ok, output = envstore.deploy_stack(stack)
        all_ok = all_ok and ok
        messages.append(f"{stack}: {'redeployed' if ok else 'deploy failed — ' + output}")
    if not stacks:
        messages.append("No redeploy needed for these values.")
    flash(" ".join(messages), "ok" if all_ok else "bad")
    return redirect(url_for("settings"))


#: Groups the Autoscaler page owns. They are edited there, next to the live
#: signals they govern, and Settings links across instead of rendering a second
#: copy of the same form — two forms posting the same keys is a page where the
#: value you are looking at may already be stale.
AUTOSCALER_GROUPS = ["Fleet"]


def _settings_groups(only=None, skip=None):
    values = _infra_values()
    out = []
    for title, keys in settings_def.GROUPS:
        if only and title not in only:
            continue
        if skip and title in skip:
            continue
        rows = []
        for key in keys:
            if key not in values:
                continue
            mode, stack, why = settings_def.describe(key)
            rows.append({
                "key": key, "value": values[key], "mode": mode, "stack": stack, "why": why,
                "masked": any(m in key for m in settings_def.MASK_HINT),
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
    "NODE_PRESSURE_PCT": "80", "MIN_WORKERS": "0", "MAX_WORKERS": "5",
    "COOLDOWN_UP_SECONDS": "300", "COOLDOWN_DOWN_SECONDS": "900",
    "SCHEDULE_FLOOR": "", "DRY_RUN": "false",
    "ADMIN_USER": "admin", "ADMIN_PASSWORD": "hunter2hunter2",
    "GRAFANA_ADMIN_USER": "admin", "GRAFANA_ADMIN_PASSWORD": "s3cr3t-grafana",
    "CF_TUNNEL_TOKEN": "eyJhIjoiN2Y0MGQ5YTIi", "CI_SSH_PUBLIC_KEY": "ssh-ed25519 AAAAC3Nza...",
    "ALERT_TELEGRAM_BOT_TOKEN": "8140000000:AAF-preview-not-a-real-token",
    "ALERT_TELEGRAM_CHAT_ID": "-1002233445566",
}
# Nothing application-shaped here: no image, no port, no SLO, no replica counts.
# The fixture stands in for the real infra.env, so an extra key would make the
# preview show a Settings page the live panel cannot show — and a DIFFERENT
# VALUE would make it document a default the cloud-init does not ship. Both are
# pinned by test_preview_infra_matches_the_shipped_defaults.


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "3000")))
