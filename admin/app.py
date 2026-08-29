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

import requests
from flask import (Flask, Response, abort, flash, jsonify, redirect, render_template,
                   request, session, url_for)
from werkzeug.utils import safe_join

import alerttargets
import auth
import catalog
import components
import envstore
import hostops
import registry
import settings_def
import shape
import state
import storage as storage_store
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
    {"key": "manager", "label": "Manager", "endpoint": "manager"},
    {"key": "alerts", "label": "Alerts", "endpoint": "alerts"},
    {"key": "grafana", "label": "Grafana", "endpoint": "grafana"},
    {"key": "storage", "label": "Storage", "endpoint": "storage"},
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
        "status_url": f"{base}/hooks/deploy/{name}/status",
        "token": token,
        # Two steps, because the deploy returns 202 the moment Swarm accepts the
        # spec — a rolling update takes longer than any proxy will hold a
        # request open. The poll is where the verdict comes from.
        "curl": (f"curl -fsS -X POST {base}/hooks/deploy/{name} \\\n"
                 f"  -H 'X-Deploy-Token: {token}' \\\n"
                 f"  -H 'Content-Type: application/json' \\\n"
                 f"  -d '{{\"image\": \"ghcr.io/you/app:sha-'\"$GITHUB_SHA\"'\"}}'\n"
                 f"\n"
                 f"# then poll until it is no longer 'pending'\n"
                 f"curl -fsS {base}/hooks/deploy/{name}/status \\\n"
                 f"  -H 'X-Deploy-Token: {token}'"),
    }


# Cloudflare rewrites the origin's `Cache-Control: no-cache` on static
# extensions into its own Browser Cache TTL — four hours by default — so after a
# self-update a browser keeps serving the previous app.js and style.css for
# hours. The rail reads the new commit because the HTML is dynamic and never
# cached, which is exactly the state that looks like the update did not happen.
# The response header is a dashboard setting we do not control from here, so the
# URL carries the version instead: a changed file is a different URL, and no
# cache keyed on the old one can answer it.
_STATIC_STAMPS = {}


@app.url_defaults
def _stamp_static(endpoint, values):
    if endpoint != "static" or "v" in values:
        return
    filename = values.get("filename")
    if not filename:
        return
    if filename not in _STATIC_STAMPS:
        path = safe_join(app.static_folder or "", filename)
        try:
            _STATIC_STAMPS[filename] = str(int(os.stat(path).st_mtime))
        except (OSError, TypeError):
            _STATIC_STAMPS[filename] = ""
    if _STATIC_STAMPS[filename]:
        values["v"] = _STATIC_STAMPS[filename]


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
        # Under the theme button on every page. It is a property of the master
        # you are looking at, not of the Cluster tab it used to have a panel on,
        # and it is the answer to "is what I am reading current" — which is a
        # question you have on every other page too.
        "infra": data.infra_version(),
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
        # `origin` is carried so the node page can send you back where you came
        # from — the fleet view on Overview and the Cluster tab both link here.
        "node_href": lambda node_id, origin="cluster": url_for(
            "node_detail", node_id=node_id, **({"from": origin} if origin != "cluster" else {})),
        "node_action_href": lambda node_id: url_for("node_action", node_id=node_id),
        "map_href": lambda name: url_for("component_map_fragment", name=name),
        "creds_href": lambda name: url_for("save_credentials", name=name),
        "new_href": lambda type_name: url_for("component_new", type=type_name),
        "create_href": lambda: url_for("component_create"),
        "settings_href": lambda: url_for("save_settings"),
        "storage_href": lambda: url_for("save_storage"),
        "alert_target_href": lambda: url_for("save_alert_target"),
        "alert_target_delete_href": lambda name: url_for("delete_alert_target", name=name),
        "storage_delete_href": lambda name: url_for("delete_storage", name=name),
        "viewer_href": lambda name: url_for("component_viewer", name=name, sub=""),
        "restore_href": lambda name: url_for("restore_component", name=name),
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
        wait = auth.retry_after(ip)
        if wait:
            # Named, not vague: the wait doubles every three failures, so "a few
            # minutes" stops being true on the fourth lock and a user who cannot
            # see the number cannot tell a lockout from a broken password.
            error = f"Too many failed attempts. Try again in {_duration(wait)}."
        elif auth.verify(request.form.get("username", ""),
                         request.form.get("password", ""), ip):
            auth.start_session(request.form.get("username", "").strip())
            nxt = request.args.get("next", "")
            # "//evil.com" and "/\\evil.com" both start with "/" and are
            # protocol-relative — a bare startswith("/") is an open redirect.
            safe = nxt.startswith("/") and not nxt.startswith(("//", "/\\"))
            return redirect(nxt if safe else url_for("overview"))
        else:
            wait = auth.retry_after(ip)
            error = "That username and password combination did not work."
            if wait:
                error += (f" That was the third failure in a row — this address is "
                          f"locked out for {_duration(wait)}, and each further three "
                          f"doubles it.")
    return render_template("login.html", error=error, configured=auth.configured())


def _duration(seconds):
    if seconds < 90:
        return f"{seconds} seconds"
    if seconds < 5400:
        return f"{round(seconds / 60)} minutes"
    return f"{round(seconds / 3600, 1)} hours"


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

    Returns 202 as soon as Swarm accepts the spec. It does NOT wait for the
    rollout: that takes parallelism x (monitor + delay) x replicas, which is
    over two minutes for two replicas and ten for eight, and Cloudflare's origin
    timeout is 100 seconds. The waiting version could not answer in time through
    the tunnel, so the proxy returned 524 and the pipeline failed while the
    deploy it was reporting on went on to succeed. A green deploy that fails CI
    is worse than no check at all.

    The verdict has not been dropped, only moved: poll the `status` URL in the
    response until it stops saying `pending`. That is the same answer the
    attached call used to return, fetched instead of waited for.

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

    ok, output = data.deploy_image_async(component.service, image)
    if not PREVIEW:
        # Accepted, not finished — so PENDING, and the status endpoint settles
        # it. Recording DONE here would be the detached-exit-code lie this whole
        # status model exists to remove.
        state.record(name, image, "ci", ok, output,
                     status=None if ok else state.FAILED,
                     actor=request.headers.get("User-Agent", "")[:60])
    if not ok:
        return jsonify(ok=False, component=name, service=component.service,
                       image=image, status="failed", detail=output), 502
    return jsonify(ok=True, component=name, service=component.service,
                   image=image, status="pending", detail=output,
                   status_url=url_for("deploy_hook_status", name=name,
                                      _external=True)), 202


@app.get("/hooks/deploy/<name>/status")
def deploy_hook_status(name):
    """
    How the last rollout of this component ended. Token-authenticated, same
    token as the deploy itself.

    `pending` means Swarm is still working; `done` and `failed` are terminal.
    `image` is what is RUNNING, which is the field that matters after a
    rollback: the status is `failed` and the image is the previous one, so a
    pipeline can tell "my build is not live" from "my build is live".
    """
    try:
        component = components.load(name)
    except components.ComponentError:
        abort(404)
    if component.TYPE != "app":
        abort(404)

    presented = request.headers.get("X-Deploy-Token") or request.args.get("token", "")
    if not (PREVIEW or state.verify_token(name, presented)):
        return jsonify(error="unauthorized"), 401

    rollout = data.update_status(component.service)
    verdict = rollout.get("verdict")
    if not PREVIEW and verdict:
        state.reconcile(name, verdict, rollout.get("started_epoch"))

    return jsonify(component=name, service=component.service,
                   status=verdict or "pending",
                   state=rollout.get("state") or "",
                   image=rollout.get("image") or "",
                   detail=rollout.get("message") or ""), 200


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


@app.get("/components/<name>/map")
@auth.login_required
def component_map_fragment(name):
    """
    Just the Map panel, re-rendered.

    The Overview map has a JSON feed and a painter in `app.js` that rebuilds its
    blocks. The Map tab draws different blocks from the same data — one
    component's replicas, labelled by tag — so a second feed would need a second
    painter, and two painters over one dataset drift. Returning the rendered
    partial instead means the server stays the only thing that knows how a block
    is drawn, and a change to `_map.html` is live on both paths at once.
    """
    component = _load(name)
    return render_template("_map.html", component=component,
                           map=data.component_map(component.services()))


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
    # The infrastructure catalog lives here rather than on the Cluster tab.
    # Both are things running in this cluster, and the question "what is
    # deployed" has one answer; Cluster is about the machines underneath.
    return render_template("page_components.html", section="components",
                           grouped=ordered, views=views, new_groups=components.groups(),
                           system=data.system_view(), stacks=catalog.SYSTEM_STACKS)


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
        component, problems = components.create(type_name, name, _form_spec(request.form))
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
        flash(f"Created {name}. Deploying — watch the Deployments tab for the "
              f"result; Swarm rolls a failing deploy back." if ok
              else f"Created {name}, but the deploy failed: {output}",
              "ok" if ok else "bad")
    else:
        flash(f"Created {name}. It is not deployed yet.", "ok")
    return redirect(_component_href(name))


def _form_spec(form):
    """
    Form values as a plain dict, with an unchecked checkbox meaning False.

    components.update() merges over the stored spec, which is what makes it safe
    to leave managed fields off the form — but it also meant a bool that was
    simply absent kept its old value, so `autoscale` could be turned on and
    never off. Each bool renders a hidden `__bool__` marker naming itself; a
    name that is marked but not present was unchecked.
    """
    data = form.to_dict()
    data.pop("__bool__", None)
    for name in form.getlist("__bool__"):
        data[name] = name in form
    return data


#: Tabs whose content this route fetches only when they are the open tab. They
#: must be followed as links rather than switched in the browser — see the
#: handler in static/app.js.
LAZY_TABS = ("logs", "deployments", "credentials", "map", "backups")


@app.get("/components/<name>")
@auth.login_required
def component_detail(name):
    component = _load(name)
    view = data.component_view(component)
    # How much data it actually holds. Swarm advertises cores and memory and
    # says nothing about a volume, so without this the header could state a
    # component's CPU and memory footprint and be silent about the one resource
    # that cannot be recovered by shedding load. Absent for anything dataguard
    # is not measuring, which is honest — nothing else in the cluster knows.
    if component.MANAGER_FIELD == "dataguard":
        view["data_gb"] = data.component_data_gb(component.name)
    tabs = component.tabs()
    tab = request.args.get("tab", tabs[0][0])
    if tab not in [t[0] for t in tabs]:
        tab = tabs[0][0]

    # Only the open tab's expensive data is fetched. `logs` queries Loki and the
    # firewall probe is an SSH round trip; doing both on every request made the
    # Overview tab wait for two things it does not render.
    #
    # LAZY_TABS is passed to the template so the tab strip can mark these links
    # as needing a real navigation. Switching to one of them client-side reveals
    # a panel this route never filled in — which is exactly how the Logs and
    # Credentials tabs came to render blank while their data was one URL away.
    extra = {}
    if tab == "logs":
        extra["logs"] = data.logs(component.service)
    if tab == "deployments":
        extra["webhook"] = webhook_for(name)
        extra["deployments"], extra["rollout"] = data.deployments(name, component.service)
        extra["registries"] = data.registry_logins()
    if tab == "map":
        # The component NAME is passed as well as its services, because a
        # database's map is coloured by which member is primary and that answer
        # lives in dataguard's metrics rather than in the task list.
        extra["map"] = data.component_map(
            component.services(),
            component=component.name if component.MANAGER_FIELD == "dataguard" else None)
    # Any type that declares credentials gets the tab, and a type that does not
    # gets nothing. No branch here names a database.
    if tab == "credentials" and type(component).SECRETS:
        extra["creds"] = component.credentials(data.master_ip())
        extra["firewall"] = _firewall_state(component)
    # `pbm list` shells into a container, so it runs only when the tab is open.
    # Any type that can list snapshots gets the tab; no branch here names one.
    if tab == "backups" and hasattr(component, "snapshots"):
        found = component.snapshots()
        extra["snapshots"], extra["pitr"] = found if found else ([], {})

    return render_template("page_component_detail.html", section="components",
                           component=component, view=view, tabs=tabs, tab=tab,
                           lazy_tabs=LAZY_TABS, newest=_newest_deploy(name, view),
                           fields=type(component).fields(),
                           env_pairs=component_store.read_env(name), **extra)


def _newest_deploy(name, view=None):
    """
    The last image anybody ASKED for, whatever became of it.

    The header already shows what is running, which is read off the service and
    is therefore always a success — a failed deploy is invisible there, because
    the thing it failed to replace is still up and looks fine. This is the other
    half: newest equal to running means the latest request is live; newest
    different means it is still rolling, or it was rolled back and nobody
    noticed. Source is the history file, so a CI push and a button press are the
    same event.
    """
    recent = data.history(name, limit=1)
    if not recent:
        return None
    entry = recent[0]
    image = entry.get("image") or ""
    running = getattr(getattr(view, "primary", None), "image", None) if view else None
    if running is None and isinstance(view, dict):
        running = (view.get("primary") or {}).get("image")
    return {"image": image,
            "image_short": shape.short_image(image),
            # Computed HERE, not in the template, because the comparison is not
            # string equality — see shape.same_image — and the template is
            # rendered by two builders that would each need their own copy of it.
            "live": shape.same_image(running, image),
            "status": entry.get("status") or "",
            "source": entry.get("source") or "",
            "at": entry.get("at") or ""}


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
    flash("Environment saved. Deploying — the Deployments tab shows whether it "
          "converged or was rolled back." if ok
          else f"Saved, but the deploy failed: {output}", "ok" if ok else "bad")
    return redirect(_component_href(name, "environment"))


@app.post("/components/<name>/settings")
@auth.login_required
def save_component_spec(name):
    _require_csrf()
    _no_writes_in_preview()
    _load(name)
    component, problems = components.update(name, _form_spec(request.form))
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
        state.record(name, image, "panel", ok, output,
                     status=state.DONE if ok else state.FAILED,
                     actor=auth.current_user() or "")
        flash(output or ("Deployed." if ok else "Failed."), "ok" if ok else "bad")
        return redirect(_component_href(name, "deployments"))

    chosen = component.actions().get(verb)
    if chosen is None or chosen["run"] is None:
        abort(400, "Unknown action.")
    ok, output = chosen["run"]()
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
    # Nodes only. The infrastructure services moved to the Components tab, and
    # the infrastructure version moved to the rail, under the theme button.
    return render_template("page_cluster.html", section="cluster",
                           nodes=data.topology()["nodes"], s=data.summary())


@app.get("/cluster/nodes/<node_id>")
@auth.login_required
def node_detail(node_id):
    entry = data.node(node_id)
    if entry is None:
        abort(404)
    back = request.args.get("from", "cluster")
    if back not in ("cluster", "overview"):
        back = "cluster"
    return render_template("page_node.html", section="cluster", n=entry,
                           back_section=back)


@app.post("/cluster/nodes/<node_id>")
@auth.login_required
def node_action(node_id):
    """
    Availability, labels, and forgetting a node that is already gone.

    Labels are here rather than in Settings because they are a property of one
    machine, and because they are the other half of a component's placement
    constraints — `node.labels.disk == ssd` matches nothing until something sets
    `disk` on a node, and until now there was no way to do that from the panel
    at all.
    """
    _require_csrf()
    _no_writes_in_preview()
    if data.node(node_id) is None:
        abort(404)
    verb = request.form.get("node_action", "")

    if verb == "availability":
        value = request.form.get("availability", "")
        if value not in ("active", "pause", "drain"):
            abort(400, "Unknown availability.")
        ok, output = data.update_node(node_id, availability=value)
    elif verb == "labels":
        pairs = [{"key": k.strip(), "value": v.strip()}
                 for k, v in zip(request.form.getlist("key"), request.form.getlist("value"))
                 if k.strip()]
        problems = shape.validate_labels(pairs)
        if problems:
            for problem in problems:
                flash(problem, "bad")
            return redirect(url_for("node_detail", node_id=node_id))
        ok, output = data.update_node(node_id, labels=pairs)
    elif verb == "remove":
        ok, output = data.remove_node(node_id)
        flash(output, "ok" if ok else "bad")
        return redirect(url_for("cluster") if ok
                        else url_for("node_detail", node_id=node_id))
    else:
        abort(400, "Unknown action.")

    flash(output, "ok" if ok else "bad")
    return redirect(url_for("node_detail", node_id=node_id))


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
    return redirect(url_for("components_index"))


@app.get("/manager")
@auth.login_required
def manager():
    """
    Two tabs over one page: the fleet, and the databases.

    They share a page because they are the same kind of thing — cluster-wide
    policy for a process that changes the cluster on its own — and because the
    alternative was a second nav entry for a handful of settings. Both post to
    /settings and both redeploy `monitoring`, so "change it and deploy" is one
    button in either.
    """
    return render_template("page_manager.html", section="manager",
                           a=data.autoscaler_state(),
                           tab=request.args.get("tab", "fleet"),
                           scaling_groups=_settings_groups(only=AUTOSCALER_GROUPS),
                           dataguard_groups=_settings_groups(only=DATAGUARD_GROUPS),
                           dg=data.dataguard_state())


@app.get("/alerts")
@auth.login_required
def alerts():
    return render_template("page_alerts.html", section="alerts", alerts=data.alerts(),
                           destination=data.alert_destination(),
                           alert_targets=alerttargets.described(),
                           alert_kinds=alerttargets.KINDS)


@app.post("/alerts/targets")
@auth.login_required
def save_alert_target():
    """
    Add a destination, prove it works, then regenerate and redeploy.

    All three, in that order: the file the panel writes is not what Alertmanager
    reads — `bin/render-alertmanager` turns the list into a config on the next
    monitoring deploy — so saving without deploying would leave the panel
    showing a target that receives nothing.

    The proof is in the middle because it is the part with a real answer. The
    deploy tells you the stack accepted a config; only a message that ARRIVED
    tells you the credential is right, and Alertmanager's own Watchdog would not
    tell you that for up to a day. A failed probe does not undo the save — a
    target somebody meant to create is kept, and the failure is said out loud.
    """
    _require_csrf()
    _no_writes_in_preview()
    target = {
        "name": (request.form.get("name") or "").strip(),
        "kind": request.form.get("kind") or alerttargets.KIND_TELEGRAM,
        "bot_token": (request.form.get("bot_token") or "").strip(),
        "chat_id": (request.form.get("chat_id") or "").strip(),
    }
    problems = alerttargets.save(target)
    if problems:
        for problem in problems:
            flash(problem, "bad")
        return redirect(url_for("alerts"))
    sent, detail = alerttargets.probe(target)
    if sent:
        flash(f"Test message sent to {target['name']} — if it did not arrive, "
              "the token and chat id are right but the chat is not the one you "
              "are watching.", "ok")
    else:
        flash(f"{target['name']} was saved, but the test message did NOT go "
              f"out: {detail}", "bad")
    ok, output = envstore.deploy_stack("monitoring")
    flash(f"{target['name']} added. " + (output or
          ("Monitoring redeployed with it." if ok else "The redeploy failed.")),
          "ok" if ok else "bad")
    return redirect(url_for("alerts"))


@app.post("/alerts/targets/<name>/delete")
@auth.login_required
def delete_alert_target(name):
    _require_csrf()
    _no_writes_in_preview()
    alerttargets.remove(name)
    remaining = alerttargets.names()
    ok, _output = envstore.deploy_stack("monitoring")
    message = f"{name} removed."
    if not remaining:
        # The state that looks healthy from the inside, said out loud rather
        # than left for somebody to notice when an alert did not arrive.
        message += " Nothing is left — every alert is now generated and dropped."
    flash(message, "ok" if ok else "bad")
    return redirect(url_for("alerts"))


# --- restore ---------------------------------------------------------------

@app.post("/components/<name>/restore")
@auth.login_required
def restore_component(name):
    """
    Put a database back to a moment in the past. The most destructive verb here.

    Everything written after the target is GONE — that is what restoring means,
    and it is why this needs the component's name typed rather than a confirm
    dialog. `docker stack rm` has the same guard for the same reason: a button
    whose worst outcome is unrecoverable should be harder to press by accident
    than one whose worst outcome is a redeploy.
    """
    _require_csrf()
    _no_writes_in_preview()
    try:
        component = components.load(name)
    except components.ComponentError as exc:
        abort(404, str(exc))
    if not hasattr(component, "restore"):
        abort(404, "This component type has no restore.")
    if request.form.get("confirm") != name:
        flash("Type the component's name to confirm the restore.", "bad")
        return redirect(_component_href(name, "backups"))

    ok, output = component.restore(
        snapshot=request.form.get("snapshot") or None,
        point_in_time=request.form.get("point_in_time") or None)
    flash(output, "ok" if ok else "bad")
    return redirect(_component_href(name, "backups"))


# --- the data visualiser ---------------------------------------------------
# A browser console over a database, reached only through this panel.
#
# THE SERVICE HAS NO PUBLISHED PORT AND NO TUNNEL HOSTNAME. It is full access to
# your data with no password of its own, so the only door is the session you are
# already signed in to — which is also the only door that already has a lockout,
# a Strict SameSite cookie and a CSRF token behind it. Giving it a hostname of
# its own would be a second front door with a different auth story, and giving it
# its own login would be a second password nobody rotates.
#
# It starts at zero replicas and dataguard stops it again once nobody has looked
# at it, so the surface exists only while somebody is using it.

VIEWER_PORT = {"mongo": 8081, "redis": 5540}
#: Everything a proxy must not forward. `Upgrade` in particular: a websocket
#: through here would escape the request/response model this route is built on,
#: and both consoles fall back to long polling without it.
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
              "te", "trailer", "transfer-encoding", "upgrade", "host",
              "content-length", "content-encoding"}
#: Never forwarded upstream, and separate from the hop-by-hop list because the
#: reason is different. These are OUR credentials, not transport plumbing.
#:
#: The visualiser is a third-party image with unrestricted access to a database,
#: reached through a page the operator is signed in to — so the browser attaches
#: the panel's session cookie to every request through this route, exactly as it
#: would to any other path on this origin. Passing that straight through hands a
#: console the bearer token for the console that runs the cluster. `Cookie` is
#: rebuilt below with the panel's own cookie removed and the visualiser's kept,
#: because the visualiser does need its own session back.
NEVER_FORWARD = {"cookie", "authorization"}
PROXY_MAX_BYTES = 32 * 1024 * 1024

#: Grafana, reached the same way and for the same reason. It is on `monitoring`
#: and nothing else, so this name resolves for the panel and for nobody outside
#: the cluster. See `grafana()` below for what this buys.
GRAFANA_ORIGIN = "http://grafana:3000"


def _forward(origin):
    """
    Hand this request to `origin` unchanged, at its own path, and stream it back.

    THE PATH IS NOT OURS TO STRIP. Everything proxied here is told it lives at
    the prefix the panel serves it under — RedisInsight by `RI_PROXY_PATH`,
    mongo-express by `ME_CONFIG_SITE_BASEURL`, Grafana by `root_url` plus
    `serve_from_sub_path` — and a program told that SERVES there: asking
    RedisInsight for `/` returned `{"message":"Cannot GET /"}` while the full
    path returned the application. The prefix is also the only thing keeping
    their asset URLs from colliding with the panel's own `/static`.

    Raises `requests.RequestException` rather than rendering its own failure:
    a database console that is still waking up and a Grafana that is missing
    are different problems and deserve different pages.
    """
    body = request.get_data(cache=False, as_text=False)
    if len(body) > PROXY_MAX_BYTES:
        abort(413, "That is larger than this proxy will forward.")
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in HOP_BY_HOP and k.lower() not in NEVER_FORWARD}
    # Their own cookies, and only those. `SESSION_COOKIE_NAME` rather than the
    # literal "session" so this keeps working if that is ever changed.
    theirs = "; ".join(f"{k}={v}" for k, v in request.cookies.items()
                       if k != app.config["SESSION_COOKIE_NAME"])
    if theirs:
        headers["Cookie"] = theirs
    upstream = requests.request(
        request.method, f"{origin}{request.path}",
        params=request.args, data=body, headers=headers,
        allow_redirects=False, stream=True, timeout=60)

    out = Response(upstream.iter_content(chunk_size=64 * 1024),
                   status=upstream.status_code)
    for key, value in upstream.headers.items():
        if key.lower() in HOP_BY_HOP:
            continue
        if key.lower() == "location":
            # Drop the INTERNAL origin and keep the path — the upstream path is
            # already ours, so re-adding the prefix here would send the browser
            # to `/components/x/viewer/components/x/viewer`. A redirect to the
            # public URL is left exactly as it is: that one is already correct.
            value = value.replace(origin, "")
        out.headers[key] = value
    return out


def _seed_viewer(component, service, port):
    """
    Give the console the component's own connection, so it opens on the data.

    Best effort, and deliberately: a console you have to add a database to by
    hand is a poor experience, and a 502 because this failed is a worse one.

    The check is what makes it idempotent — the console is asked what it already
    has before anything is added, so reopening it does not accumulate duplicate
    entries of the same server.
    """
    wanted = component.viewer_databases()
    if not wanted:
        return
    base = f"http://{service}:{port}/components/{component.name}/viewer"
    try:
        if requests.get(f"{base}/api/databases", timeout=10).json():
            return
        for database in wanted:
            requests.post(f"{base}/api/databases", json=database, timeout=20)
    except Exception as exc:                                     # noqa: BLE001
        # Never the body — it holds the database password.
        app.logger.warning("could not set up %s: %s", service, exc)


@app.route("/components/<name>/viewer/", defaults={"sub": ""},
           methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
@app.route("/components/<name>/viewer/<path:sub>",
           methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
@auth.login_required
def component_viewer(name, sub):        # noqa: ARG001 — `sub` is the URL capture
    """
    Proxy one request to a component's visualiser, starting it if it is asleep.

    `sub` exists to make the route match; the request is forwarded at its FULL
    path, for the reason spelled out where it is built.

    `_require_csrf` is deliberately NOT applied. A request through here carries
    the UPSTREAM application's CSRF token, not the panel's, and demanding ours
    would break every form the console has. What guards this instead is the
    session plus `SESSION_COOKIE_SAMESITE="Strict"`, which is what stops another
    site driving it from your browser — and the fact that the thing behind it is
    not reachable any other way. This is the only mutating path in the panel
    without a CSRF check, and it is why the note is here rather than in a commit
    message.
    """
    if PREVIEW:
        abort(400, "This is a preview build with dummy data — nothing is proxied.")
    try:
        component = components.load(name)
    except components.ComponentError as exc:
        abort(404, str(exc))
    if not component.spec.get("visualizer"):
        abort(404, "This component has no data visualiser.")
    port = VIEWER_PORT.get(component.TYPE)
    if not port:
        abort(404, "This component type has no visualiser.")

    service = f"{component.stack}_viewer"
    started, detail = data.ensure_viewer(service)
    if not started:
        return render_template("page_viewer_wait.html", section="components",
                               component=component, detail=detail), 503
    data.touch_viewer(component.name)
    if not sub:
        # The landing request, and only that one. This is the request the View
        # button makes; every asset and API call the console then fires has a
        # non-empty `sub`, so the cost is one round trip each time you open it
        # rather than one per resource. Doing it here rather than once at wake
        # also means it re-asserts itself if the console was restarted
        # underneath us — its database list lives in the container and nowhere
        # else, so it does not survive being put away for idleness.
        _seed_viewer(component, service, port)

    try:
        return _forward(f"http://{service}:{port}")
    except requests.RequestException as exc:
        return render_template("page_viewer_wait.html", section="components",
                               component=component, detail=str(exc)), 502


@app.route("/grafana/", defaults={"sub": ""},
           methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
@app.route("/grafana/<path:sub>",
           methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
@auth.login_required
def grafana(sub):                       # noqa: ARG001 — `sub` is the URL capture
    """
    Grafana, behind the panel session instead of behind a public hostname.

    It used to be reachable one way only: a `grafana-<app>.<root>` hostname on
    the Cloudflare tunnel, which is a second login page on the public internet
    guarding every metric this cluster has ever recorded. The database consoles
    already solved this — no published port, no tunnel entry, reached only
    through a session the operator already holds — and there was no reason
    Grafana should be the exception.

    Grafana is told it lives here (`root_url` plus `serve_from_sub_path` in
    `stacks/monitoring.yml`), so it serves under this prefix and its own asset
    URLs land back on this route. Its `<base href>` is the prefix and everything
    below it is relative, which is why nothing has to be rewritten on the way
    out.

    IT STILL ASKS FOR ITS OWN LOGIN, once. Injecting the admin credential here
    would mean mounting Grafana's docker secret into the panel to read a
    password the panel can only write — a real widening, to save typing a
    password a browser remembers. Its session cookie is forwarded like any
    other, so this is once per browser, not once per visit.

    `_require_csrf` is deliberately not applied, for the reason spelled out on
    `component_viewer`: the token on a request through here is Grafana's, not
    ours. Live-streaming panels do not work, because `Upgrade` is not forwarded
    by anything on this route; dashboards poll and are unaffected.
    """
    if PREVIEW:
        abort(400, "This is a preview build with dummy data — nothing is proxied.")
    try:
        return _forward(GRAFANA_ORIGIN)
    except requests.RequestException as exc:
        return render_template("page_grafana_down.html", section="grafana",
                               detail=str(exc)), 502


# --- storage ---------------------------------------------------------------
# Where backups go, defined once for the cluster. A component names a target;
# this is what a name means. See admin/storage.py for why the credentials are
# Swarm secrets rather than rows in the file.

def _storage_users(name):
    """Which components would break if this target went away."""
    found = []
    everything, _problems = components.all_components()
    for component in everything:
        if component.spec.get("backup_target") == name:
            found.append(component.name)
    return found


@app.get("/storage")
@auth.login_required
def storage():
    targets = storage_store.described()
    return render_template("page_storage.html", section="storage",
                           targets=[dict(t, used_by=_storage_users(t["name"]))
                                    for t in targets])


@app.post("/storage")
@auth.login_required
def save_storage():
    _require_csrf()
    _no_writes_in_preview()
    form = request.form
    target = {
        "name": (form.get("name") or "").strip(),
        "kind": storage_store.KIND_S3,
        "endpoint": (form.get("endpoint") or "").strip(),
        "region": (form.get("region") or "").strip(),
        "bucket": (form.get("bucket") or "").strip(),
        "prefix": (form.get("prefix") or "").strip(),
        "path_style": form.get("path_style") == "on",
        # Server-side encryption, on by default. It costs nothing on any S3
        # implementation that supports it, and what is in the bucket is every
        # row of your database.
        "sse": form.get("sse", "on") == "on",
    }
    problems = storage_store.save(target, form.get("access_key", ""),
                                  form.get("secret_key", ""))
    if problems:
        for problem in problems:
            flash(problem, "bad")
    else:
        flash(f"{target['name']} saved.", "ok")
    return redirect(url_for("storage"))


@app.post("/storage/<name>/delete")
@auth.login_required
def delete_storage(name):
    _require_csrf()
    _no_writes_in_preview()
    if request.form.get("confirm") != name:
        flash("Type the target's name to confirm.", "bad")
        return redirect(url_for("storage"))
    problems = storage_store.remove(name, _storage_users(name))
    for problem in problems:
        flash(problem, "bad")
    if not problems:
        flash(f"{name} removed. Nothing already in it was deleted.", "ok")
    return redirect(url_for("storage"))


# --- settings --------------------------------------------------------------

@app.get("/settings")
@auth.login_required
def settings():
    return render_template("page_settings.html", section="settings",
                           groups=_settings_groups(skip=AUTOSCALER_GROUPS + DATAGUARD_GROUPS),
                           elsewhere=AUTOSCALER_GROUPS + DATAGUARD_GROUPS)


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
DATAGUARD_GROUPS = ["Dataguard"]


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
            # infra.env carries this cluster's ANSWERS; the repo carries the
            # defaults. Skipping a key the file happens not to have is what made
            # every setting added after a cluster was built invisible on it —
            # and unsettable, since the form is built from these rows.
            if key in values:
                value = values[key]
            elif key in settings_def.DEFAULTS:
                value = settings_def.DEFAULTS[key]
            else:
                continue
            mode, stack, why = settings_def.describe(key)
            rows.append({
                "key": key, "value": value, "mode": mode, "stack": stack, "why": why,
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
    "WORKER_IMAGE": "ubuntu-24.04",
    "HCLOUD_TOKEN": "hcl_9f2bc41d77aa0e35", "GHCR_USER": "acme-bot",
    "GHCR_TOKEN": "ghp_a71ccf20e9bb14d0",
    "NODE_PRESSURE_PCT": "80", "MIN_WORKERS": "0", "MAX_WORKERS": "5",
    "WORKER_MAX_CORES": "8", "WORKER_MAX_MEMORY_GB": "16",
    "NODE_RESIZE_COOLDOWN_SECONDS": "900",
    "COOLDOWN_UP_SECONDS": "300", "COOLDOWN_DOWN_SECONDS": "900",
    "SCHEDULE_FLOOR": "", "DRY_RUN": "false",
    "ADMIN_USER": "admin", "ADMIN_PASSWORD": "hunter2hunter2",
    "GRAFANA_ADMIN_USER": "admin", "GRAFANA_ADMIN_PASSWORD": "s3cr3t-grafana",
    "CF_TUNNEL_TOKEN": "eyJhIjoiN2Y0MGQ5YTIi", "CI_SSH_PUBLIC_KEY": "ssh-ed25519 AAAAC3Nza...",
}
# Nothing application-shaped here: no image, no port, no SLO, no replica counts.
# The fixture stands in for the real infra.env, so an extra key would make the
# preview show a Settings page the live panel cannot show — and a DIFFERENT
# VALUE would make it document a default the cloud-init does not ship. Both are
# pinned by test_preview_infra_matches_the_shipped_defaults.


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "3000")))
