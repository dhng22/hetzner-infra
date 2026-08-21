#!/usr/bin/env python3
"""
Build a self-contained, navigable preview of the panel.

    python3 admin/preview_build.py

Writes admin/preview/index.html: one file, dummy data, no server, no docker.
Open it in a browser and click through every screen.

It renders the SAME partials the live panel renders, with fixtures.py standing
in for the cluster and in-page anchors standing in for URLs. A hand-written mock
would drift from the real UI the first time a template changed; this cannot.

Components are not mocked at all: INFRA_DIR points at a temp directory seeded
with real component specs, so the preview drives the actual Component classes,
the actual field definitions and the actual renderer. Only the Docker-facing
half is fake.
"""

import os
import pathlib
import shutil
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

_SEED = tempfile.mkdtemp(prefix="panel-preview-")
os.environ.setdefault("PREVIEW", "1")
os.environ.setdefault("APP_NAME", "aichat")
os.environ.setdefault("ROOT_DOMAIN", "acme.dev")
os.environ.setdefault("ADMIN_PASSWORD", "preview")
os.environ.setdefault("MASTER_PRIVATE_IP", "10.0.0.2")
os.environ["INFRA_DIR"] = _SEED

import app as panel          # noqa: E402
import components            # noqa: E402
import fixtures              # noqa: E402


#: The components the preview cluster is running. Written through the real
#: `components.create`, so anything the create form would refuse is refused here
#: too — the preview cannot show a component the panel could not have made.
SEED = [
    ("app", "api", {
        "image": "ghcr.io/acme/aichat-api:sha-9f3ac21", "port": 8080,
        "replicas": 6, "autoscale": "true", "min_replicas": 2, "max_replicas": 12,
        "slo_p95_ms": 500,
    }),
    ("app", "api-staging", {
        "image": "ghcr.io/acme/aichat-api:sha-c40e8b7", "port": 8080,
        "replicas": 1, "cpu_reservation": 0.1, "memory_reservation_mb": 128,
        "cpu_limit": 0.5, "memory_limit_mb": 512, "slo_p95_ms": 800,
    }),
    ("redis", "cache", {"maxmemory_mb": 512, "external_port": 46379}),
    ("redis", "sessions", {"maxmemory_mb": 128, "memory_reservation_mb": 256,
                           "cpu_reservation": 0.1, "exporter": "false",
                           "version": "7.2-alpine"}),
]

SEED_ENV = {
    "api": [("LOG_LEVEL", "INFO"), ("FEATURE_NEW_CHECKOUT", "true"),
            ("OPENAI_TIMEOUT_MS", "30000"), ("MAX_UPLOAD_MB", "25"),
            ("REDIS_URL", "redis://default:8f2b91c4de77a0135be2@cache_redis:6379")],
    "api-staging": [("LOG_LEVEL", "DEBUG"), ("MAX_UPLOAD_MB", "5")],
}


def seed_components():
    for type_name, name, spec in SEED:
        component, problems = components.create(type_name, name, spec)
        if problems:
            raise SystemExit(f"preview seed for {name} is invalid: {problems}")
    for name, pairs in SEED_ENV.items():
        components.store.write_env(
            name, [{"key": k, "value": v} for k, v in pairs],
            header=[f"# Environment for {name}.", ""])


def detail_contexts():
    """One context per component, so every card in the list resolves."""
    out = []
    for component in components.all_components()[0]:
        view = fixtures.component_view(component)
        tabs = component.tabs()
        context = {
            "component": component,
            "view": view,
            "tabs": tabs,
            # Open each component on its most interesting tab, so the preview
            # shows the thing worth looking at rather than a task table.
            "tab": "environment" if component.TYPE == "app" else "credentials",
            "fields": type(component).fields(),
            "env_pairs": components.store.read_env(component.name),
            "logs": fixtures.logs(component.service),
            "webhook": panel.webhook_for(component.name),
            "deployments": fixtures.history(component.name),
            "registries": fixtures.registry_logins(),
        }
        if component.TYPE == "redis":
            context["creds"] = panel._redis_credentials(component)
            context["firewall"] = panel._firewall_state(component)
        out.append(context)
    return out


def main():
    seed_components()
    details = detail_contexts()

    labels = {"overview": "Overview", "components": "Components", "cluster": "Cluster",
              "autoscaler": "Autoscaler", "alerts": "Alerts", "settings": "Settings",
              "login": "Sign in"}
    for d in details:
        labels[f"component-{d['component'].name}"] = d["component"].name
    for type_name, cls in components.TYPES.items():
        labels[f"new-{type_name}"] = f"New {cls.LABEL.lower()}"

    views = fixtures.component_views()
    grouped = {}
    for view in views:
        grouped.setdefault(view["category"], []).append(view)
    order = ["Application", "Data"]
    ordered = ([(c, grouped[c]) for c in order if c in grouped]
               + [(c, v) for c, v in grouped.items() if c not in order])

    with panel.app.test_request_context("/"):
        html = panel.render_template(
            "preview.html",
            inline_css=((HERE / "static" / "fonts.css").read_text()
                        + (HERE / "static" / "style.css").read_text()),
            inline_js=(HERE / "static" / "app.js").read_text(),
            details=details,
            new_forms=[{"cls": cls, "type_name": key, "values": {}, "problems": [], "name": ""}
                       for key, cls in components.TYPES.items()],
            labels=labels,
            # page data
            s=fixtures.summary(),
            views=views,
            grouped=ordered,
            alerts=fixtures.alerts(),
            nodes=fixtures.nodes(),
            system=fixtures.system_view(),
            topo=fixtures.topology(),
            a=fixtures.autoscaler_state(),
            # Two disjoint sets, exactly as the live routes pass them — one
            # shared `groups` here would put the whole form on both pages.
            groups=panel._settings_groups(skip=panel.AUTOSCALER_GROUPS),
            scaling_groups=panel._settings_groups(only=panel.AUTOSCALER_GROUPS),
            elsewhere=panel.AUTOSCALER_GROUPS,
            # chrome
            nav=panel.NAV,
            types=components.TYPES,
            cluster_name=os.environ["APP_NAME"],
            root_domain=os.environ["ROOT_DOMAIN"],
            user="admin",
            preview=True,
            csrf_token=lambda: "preview",
            system_access=panel.system_access,
            section_href=lambda item: f"#view-{item['key']}",
            component_href=lambda name, tab=None: f"#view-component-{name}",
            new_href=lambda type_name: f"#view-new-{type_name}",
            # Every mutating URL is a dead anchor in the preview. All of them,
            # not most: two were missed last time and the generated file shipped
            # with live form actions pointing at a server that is not there.
            action_href=lambda name: "#",
            env_href=lambda name: "#",
            spec_href=lambda name: "#",
            delete_href=lambda name: "#",
            token_href=lambda name: "#",
            firewall_href=lambda name: "#",
            create_href=lambda: "#",
            settings_href=lambda: "#",
            registry_href=lambda: "#",
            stack_href=lambda: "#",
            logout_href=lambda: "#",
        )

    out_dir = HERE / "preview"
    out_dir.mkdir(exist_ok=True)
    target = out_dir / "index.html"
    target.write_text(html)
    print(f"wrote {target} ({len(html) / 1024:.0f} KB, {len(details)} components)")

    leaked = [u for u in ('action="/', "action='/") if u in html]
    if leaked:
        raise SystemExit("preview contains a live form action — a *_href was not stubbed")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(_SEED, ignore_errors=True)
