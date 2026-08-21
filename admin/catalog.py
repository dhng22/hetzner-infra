"""
The infrastructure — the part of the cluster you did not create and cannot edit.

Three stacks come up before anything else exists: monitoring, ingress, admin.
They are not components; they have no spec file, no environment editor and no
delete button, because they are the thing that runs components. The panel shows
them so you can see their state and open their UIs, and shows them read-only
because a UI that accepts an edit the next deploy reverts is worse than one that
declines.

Everything else — every application, every database — is a component, and none
of it is listed here. That is the whole point of the refactor: this file used to
hard-code `app_api-prod` and `app_api-staging`, so a second application meant
editing it.
"""

# How a service's own web UI is reached. Being precise matters: offering an
# "Open" button that leads nowhere is worse than saying it is not published.
#   tunnel   — a public hostname on the Cloudflare tunnel
#   host     — published on the master's private IP at `port`
#   internal — overlay network only. Not reachable from your laptop; we say so
#              rather than linking to an address that will time out.
UI_TUNNEL, UI_HOST, UI_INTERNAL = "tunnel", "host", "internal"

SYSTEM = [
    {
        "key": "cloudflared",
        "display": "Cloudflare Tunnel",
        "stack": "ingress",
        "service": "ingress_cloudflared",
        "category": "Ingress",
        "blurb": "One connector per node, master included. All inbound traffic arrives here. "
                 "Routes are configured in the Cloudflare dashboard, not here.",
    },
    {
        "key": "grafana",
        "display": "Grafana",
        "stack": "monitoring",
        "service": "monitoring_grafana",
        "category": "Observability",
        "blurb": "Dashboards over VictoriaMetrics and Loki.",
        "ui": {"kind": UI_TUNNEL, "prefix": "grafana-"},
    },
    {
        "key": "victoriametrics",
        "display": "VictoriaMetrics",
        "stack": "monitoring",
        "service": "monitoring_victoriametrics",
        "category": "Observability",
        "blurb": "Metric storage. Every scaling decision is read from here.",
        "ui": {"kind": UI_INTERNAL, "host": "victoriametrics", "port": 8428},
    },
    {
        "key": "vmagent",
        "display": "vmagent",
        "stack": "monitoring",
        "service": "monitoring_vmagent",
        "category": "Observability",
        "blurb": "Scrapes anything carrying the prometheus.* deploy labels. "
                 "Components get them automatically.",
        "ui": {"kind": UI_INTERNAL, "host": "vmagent", "port": 8429},
    },
    {
        "key": "vmalert",
        "display": "vmalert",
        "stack": "monitoring",
        "service": "monitoring_vmalert",
        "category": "Observability",
        "blurb": "Evaluates config/alerts.yml against VictoriaMetrics.",
        "ui": {"kind": UI_INTERNAL, "host": "vmalert", "port": 8880},
    },
    {
        "key": "alertmanager",
        "display": "Alertmanager",
        "stack": "monitoring",
        "service": "monitoring_alertmanager",
        "category": "Observability",
        "blurb": "Groups and dispatches alerts to your webhook.",
        "ui": {"kind": UI_INTERNAL, "host": "alertmanager", "port": 9093},
    },
    {
        "key": "loki",
        "display": "Loki",
        "stack": "monitoring",
        "service": "monitoring_loki",
        "category": "Observability",
        "blurb": "Log storage. Every container ships to it directly through the log driver.",
    },
    {
        "key": "autoscaler",
        "display": "Autoscaler",
        "stack": "monitoring",
        "service": "monitoring_autoscaler",
        "category": "Platform",
        "blurb": "Discovers components by label, scales their replicas, and buys and "
                 "sells Hetzner workers to fit them.",
    },
    {
        "key": "exporters",
        "display": "Node exporters",
        "stack": "monitoring",
        "service": "monitoring_node-exporter",
        "category": "Platform",
        "blurb": "node-exporter and cadvisor, one of each per node.",
    },
    {
        "key": "admin",
        "display": "Admin panel",
        "stack": "admin",
        "service": "admin_ui",
        "category": "Platform",
        "blurb": "This console. It holds the docker socket, so its password is the cluster.",
        "ui": {"kind": UI_TUNNEL, "prefix": "admin-"},
    },
]

BY_KEY = {entry["key"]: entry for entry in SYSTEM}

#: Stacks the panel deploys but never edits. `bin/stack-deploy` accepts exactly
#: these, and nothing else may be routed through it.
SYSTEM_STACKS = ["monitoring", "ingress", "admin"]

CATEGORIES = ["Ingress", "Observability", "Platform"]


def system_for_service(service_name):
    for entry in SYSTEM:
        if entry["service"] == service_name:
            return entry
    return None
