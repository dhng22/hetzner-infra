#!/usr/bin/env python3
"""
Build a self-contained, navigable preview of the panel.

    python3 admin/preview_build.py

Writes admin/preview/index.html: one file, dummy data, no server, no docker.
Open it in a browser and click through every screen.

It renders the SAME partials the live panel renders, with fixtures.py standing
in for the cluster and in-page anchors standing in for URLs. A hand-written
mock would drift from the real UI the first time a template changed; this
cannot.
"""

import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ.setdefault("PREVIEW", "1")
os.environ.setdefault("APP_NAME", "aichat")
os.environ.setdefault("ROOT_DOMAIN", "acme.dev")
os.environ.setdefault("ADMIN_PASSWORD", "preview")

import app as panel          # noqa: E402
import catalog               # noqa: E402
import envstore              # noqa: E402
import fixtures              # noqa: E402


def detail_contexts():
    """One context per app/environment pair, so every link in Apps resolves."""
    out = []
    for entry in catalog.CATALOG:
        for env_name in entry["environments"]:
            svc = fixtures.service(entry["environments"][env_name])
            editable = bool(entry.get("editable")) and env_name in ("prod", "staging")
            pairs = None
            if editable:
                pairs = [
                    {"key": k, "value": v, "sensitive": bool(envstore.SENSITIVE.search(k))}
                    for k, v in svc["env"].items() if k not in envstore.RESERVED
                ]
            has_creds = entry.get("credentials") == "redis"
            deployable = bool(entry.get("deployments"))
            app_entry = {**entry, "envs": {}}
            out.append({
                "app_entry": app_entry,
                "env_name": env_name,
                "svc": svc,
                # Open each app on its most interesting tab so the preview shows
                # the thing worth looking at rather than a task table.
                "tab": ("environment" if editable else
                        "credentials" if has_creds else "overview"),
                "editable": editable,
                "pairs": pairs,
                "creds": panel.redis_credentials(env_name) if has_creds else None,
                "access": panel.ui_access(app_entry, env_name),
                "webhook": panel.webhook_for(entry["key"], env_name) if deployable else None,
                "deployments": fixtures.history(entry["key"], env_name) if deployable else None,
                "registries": fixtures.registry_logins() if deployable else None,
                "firewall": ({"available": True, "port": "46379",
                              "open": fixtures.port_is_open("46379")}
                             if has_creds and env_name == "prod" else
                             {"available": True, "port": "", "open": False}
                             if has_creds else None),
                "logs": fixtures.logs(svc["name"]),
            })
    return out


def main():
    details = detail_contexts()
    first_env = {e["key"]: next(iter(e["environments"])) for e in catalog.CATALOG}
    labels = {"overview": "Overview", "apps": "Apps", "cluster": "Cluster",
              "autoscaler": "Autoscaler", "alerts": "Alerts", "settings": "Settings",
              "login": "Sign in"}
    for d in details:
        labels[f"app-{d['app_entry']['key']}-{d['env_name']}"] = (
            f"{d['app_entry']['display']} · {d['env_name']}"
        )

    grouped = {}
    for a in fixtures.apps():
        grouped.setdefault(a["category"], []).append(a)

    with panel.app.test_request_context("/"):
        html = panel.render_template(
            "preview.html",
            inline_css=((HERE / "static" / "fonts.css").read_text()
                        + (HERE / "static" / "style.css").read_text()),
            inline_js=(HERE / "static" / "app.js").read_text(),
            details=details,
            labels=labels,
            # page data
            s=fixtures.summary(),
            apps=fixtures.apps(),
            alerts=fixtures.alerts(),
            grouped=[(c, grouped[c]) for c in catalog.CATEGORIES if c in grouped],
            nodes=fixtures.nodes(),
            a=fixtures.autoscaler_state(),
            groups=panel._settings_groups(),
            # chrome
            nav=panel.NAV,
            cluster_name=os.environ["APP_NAME"],
            root_domain=os.environ["ROOT_DOMAIN"],
            user="admin",
            preview=True,
            csrf_token=lambda: "preview",
            section_href=lambda item: f"#view-{item['key']}",
            # Overview links to an app without naming an environment; resolve
            # that to whichever environment the catalog lists first.
            app_href=lambda key, env=None, tab=None: (
                f"#view-app-{key}-{env or first_env[key]}"
            ),
            action_href=lambda key: "#",
            env_href=lambda key: "#",
            creds_href=lambda key: "#",
            token_href=lambda key: "#",
            settings_href=lambda: "#",
        )

    out_dir = HERE / "preview"
    out_dir.mkdir(exist_ok=True)
    target = out_dir / "index.html"
    target.write_text(html)
    print(f"wrote {target} ({len(html) / 1024:.0f} KB, {len(details)} app views)")


if __name__ == "__main__":
    main()
