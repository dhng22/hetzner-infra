"""
What the panel calls an "app", and which Swarm services back it.

Swarm only knows a flat list of services with stack-prefixed names. That is a
bad mental model for an operator: `app_api-prod` and `app_api-staging` are one
application in two environments, not two unrelated things. This file is the
translation layer, and it is the only place that knowledge lives.

`editable` marks the apps whose configuration the panel may write. Everything
else is owned by a stack file, and the panel shows it read-only rather than
pretending otherwise — a UI that silently loses your edit on the next deploy is
worse than one that declines to take it.
"""

CATEGORIES = ["Application", "Data", "Ingress", "Observability", "Platform"]

# How a service's own web UI is reached. Being precise here matters: offering an
# "Open" button that leads nowhere is worse than saying it is not published.
#   tunnel   — a public hostname on the Cloudflare tunnel
#   host     — published on the master's private IP at `port`
#   internal — overlay network only. Not reachable from your laptop; we say so
#              rather than linking to an address that will time out.
UI_TUNNEL, UI_HOST, UI_INTERNAL = "tunnel", "host", "internal"

CATALOG = [
    {
        "key": "app",
        "display": "API",
        "category": "Application",
        "blurb": "The Ktor service behind the tunnel. Replica count is owned by the autoscaler.",
        "environments": {"prod": "app_api-prod", "staging": "app_api-staging"},
        "env_files": {"prod": "app-prod.env", "staging": "app-staging.env"},
        "editable": True,
        "deployable": True,
        "scalable": False,  # the autoscaler owns this; manual scaling would fight it
        "deployments": True,   # CI webhook + history tab
        "ui": {"kind": UI_TUNNEL, "prefix": {"prod": "", "staging": "staging-"}},
    },
    {
        "key": "redis",
        "display": "Redis",
        "category": "Data",
        "blurb": "Cache and sessions, one instance per environment, pinned to the master.",
        "environments": {"prod": "app_redis-prod", "staging": "app_redis-staging"},
        "editable": False,
        "deployable": True,
        "credentials": "redis",
        "note": "Password comes from the API's environment file — one value for server and clients.",
    },
    {
        "key": "cloudflared",
        "display": "Cloudflare Tunnel",
        "category": "Ingress",
        "blurb": "One connector per worker. All inbound traffic arrives here.",
        "environments": {"default": "app_cloudflared"},
        "editable": False,
        "deployable": True,
    },
    {
        "key": "grafana",
        "display": "Grafana",
        "category": "Observability",
        "blurb": "Dashboards over VictoriaMetrics and Loki.",
        "environments": {"default": "monitoring_grafana"},
        "editable": False,
        "ui": {"kind": UI_TUNNEL, "prefix": {"default": "grafana-"}},
    },
    {
        "key": "victoriametrics",
        "display": "VictoriaMetrics",
        "category": "Observability",
        "blurb": "Metric store, 30 day retention. The autoscaler reads its signals from here.",
        "environments": {"default": "monitoring_victoriametrics"},
        "editable": False,
        "ui": {"kind": UI_INTERNAL, "port": 8428, "host": "victoriametrics"},
    },
    {
        "key": "vmagent",
        "display": "vmagent",
        "category": "Observability",
        "blurb": "Scraper with Swarm service discovery. Picks up new workers unattended.",
        "environments": {"default": "monitoring_vmagent"},
        "editable": False,
        "ui": {"kind": UI_INTERNAL, "port": 8429, "host": "vmagent"},
    },
    {
        "key": "loki",
        "display": "Loki",
        "category": "Observability",
        "blurb": "Log store. Containers ship straight to it via the Docker log driver.",
        "environments": {"default": "monitoring_loki"},
        "editable": False,
        "ui": {"kind": UI_HOST, "port": 3100},
    },
    {
        "key": "alerting",
        "display": "Alerting",
        "category": "Observability",
        "blurb": "vmalert evaluates the rules; Alertmanager routes them to your webhook.",
        "environments": {"vmalert": "monitoring_vmalert", "alertmanager": "monitoring_alertmanager"},
        "editable": False,
        "ui": {"kind": UI_INTERNAL, "port": {"vmalert": 8880, "alertmanager": 9093},
               "host": {"vmalert": "vmalert", "alertmanager": "alertmanager"}},
    },
    {
        "key": "autoscaler",
        "display": "Autoscaler",
        "category": "Platform",
        "blurb": "Two-tier scaler. Owns the API's replica count and the worker pool.",
        "environments": {"default": "monitoring_autoscaler"},
        "editable": False,
        "ui": {"kind": UI_INTERNAL, "port": 9200, "host": "autoscaler", "path": "/metrics"},
    },
    {
        "key": "exporters",
        "display": "Exporters",
        "category": "Observability",
        "blurb": "node-exporter and cadvisor run on every node, present and future.",
        "environments": {"node": "monitoring_node-exporter", "container": "monitoring_cadvisor"},
        "editable": False,
        "ui": {"kind": UI_HOST, "port": {"node": 9100, "container": 8081}},
    },
    {
        "key": "admin",
        "display": "Admin Panel",
        "category": "Platform",
        "blurb": "This panel.",
        "environments": {"default": "admin_ui"},
        "editable": False,
        "ui": {"kind": UI_TUNNEL, "prefix": {"default": "admin-"}},
    },
]

BY_KEY = {a["key"]: a for a in CATALOG}


def service_names():
    names = []
    for app in CATALOG:
        names.extend(app["environments"].values())
    return names


def app_for_service(name):
    for app in CATALOG:
        for env, svc in app["environments"].items():
            if svc == name:
                return app, env
    return None, None
