"""
Dummy cluster used by the preview build.

Same shapes as swarm.py, so the preview is rendered from the real templates and
cannot drift from the real UI. The numbers describe a plausible Tuesday
afternoon: one application busy but inside its SLO, a staging copy idle, one
worker mid-drain, and one alert firing so the warning states are visible.

**Components are real here.** `preview_build.py` points INFRA_DIR at a seeded
directory and this module only fakes the parts that talk to Docker. So the
preview exercises the actual Component classes, the actual renderer and the
actual field definitions — the one thing that used to be duplicated by hand
between here and the live path is not duplicated any more.
"""

import catalog
import shape

_IMG = "ghcr.io/acme/aichat-api"


def _svc(name, image, running, desired, state="healthy", tone="ok", mode="replicated",
         env=None, cpu=1.0, mem=768, cpu_res=0.5, mem_res=384, placement=None,
         updated="14m ago", tasks=None, networks=("edge", "monitoring")):
    return {
        "name": name, "exists": True, "tone": tone, "state": state,
        "image": image, "image_short": image.split("/")[-1],
        "running": running, "desired": desired, "mode": mode, "updated": updated,
        "tasks": tasks if tasks is not None else [
            {"id": f"t{i}kd93jf01x"[:12], "node": f"wkr-{(i % 3) + 1}0f2a9c",
             "state": "running", "desired": "running", "since": f"{9 + i * 4}m ago", "error": ""}
            for i in range(min(running, 6))
        ],
        "env": env or {},
        "networks": list(networks),
        "resources": {"cpu_limit": cpu, "mem_limit": mem, "cpu_res": cpu_res, "mem_res": mem_res},
        "update_config": {"parallelism": 1, "order": "start-first", "delay": "15s",
                          "monitor": "60s", "failure_action": "rollback"},
        "placement": placement if placement is not None else ["node.role == worker"],
    }


_SERVICES = {
    "api_app": _svc("api_app", f"{_IMG}:sha-9f3ac21", 6, 6),
    "api-staging_app": _svc("api-staging_app", f"{_IMG}:sha-c40e8b7", 1, 1,
                            cpu=0.5, mem=512, cpu_res=0.1, mem_res=128, updated="2h ago"),
    "cache_redis": _svc("cache_redis", "redis:7.4-alpine", 1, 1, cpu=None, mem=None,
                        cpu_res=0.2, mem_res=640, placement=["node.role == manager"],
                        networks=("edge",), updated="6d ago"),
    "cache_redis-exporter": _svc("cache_redis-exporter", "oliver006/redis_exporter:v1.66.0",
                                 1, 1, cpu=None, mem=None, cpu_res=0.05, mem_res=32,
                                 placement=["node.role == manager"], updated="6d ago"),
    "sessions_redis": _svc("sessions_redis", "redis:7.2-alpine", 1, 1, cpu=None, mem=None,
                           cpu_res=0.1, mem_res=256, placement=["node.role == manager"],
                           networks=("edge",), updated="9d ago"),
    "ingress_cloudflared": _svc("ingress_cloudflared", "cloudflare/cloudflared:2024.10.1",
                                4, 4, mode="global", cpu=None, mem=None, cpu_res=0.05,
                                mem_res=32, placement=[], updated="9d ago"),
    "admin_ui": _svc("admin_ui", "aichat/admin:latest", 1, 1, cpu=0.5, mem=256,
                     cpu_res=0.10, mem_res=128, placement=["node.role == manager"],
                     networks=("monitoring",), updated="3d ago"),
}
for _name, _image, _cpu_res, _mem_res in [
    ("monitoring_victoriametrics", "victoriametrics/victoria-metrics:v1.106.1", 0.5, 768),
    ("monitoring_vmagent", "victoriametrics/vmagent:v1.106.1", 0.15, 128),
    ("monitoring_vmalert", "victoriametrics/vmalert:v1.106.1", 0.1, 96),
    ("monitoring_alertmanager", "prom/alertmanager:v0.27.0", 0.1, 96),
    ("monitoring_loki", "grafana/loki:3.1.1", 0.3, 512),
    ("monitoring_grafana", "grafana/grafana:11.3.0", 0.2, 256),
    ("monitoring_autoscaler", "aichat/autoscaler:latest", 0.1, 96),
    ("monitoring_node-exporter", "prom/node-exporter:v1.8.2", 0.05, 64),
    ("monitoring_cadvisor", "gcr.io/cadvisor/cadvisor:v0.49.1", 0.10, 128),
]:
    _SERVICES[_name] = _svc(_name, _image, 1, 1, cpu=None, mem=None,
                            cpu_res=_cpu_res, mem_res=_mem_res,
                            placement=["node.role == manager"],
                            networks=("monitoring",), updated="9d ago")


def service(name, with_tasks=True):
    svc = _SERVICES.get(name)
    if not svc:
        return {"name": name, "exists": False, "tone": "mute", "state": "missing",
                "image": "—", "image_short": "—", "running": 0, "desired": 0, "tasks": [],
                "env": {}, "updated": "—", "mode": "—", "resources": {}, "placement": [],
                "update_config": {}, "networks": []}
    return svc if with_tasks else {**svc, "tasks": []}


def component_view(component):
    return shape.component_view(component, service)


def component_views():
    return shape.component_views(service)


def system_view():
    grouped = {}
    for entry in catalog.SYSTEM:
        svc = service(entry["service"], with_tasks=False)
        grouped.setdefault(entry["category"], []).append({**entry, "svc": svc,
                                                          "tone": svc["tone"]})
    return [(c, grouped[c]) for c in catalog.CATEGORIES if c in grouped]


def vm_query(expr):
    return {
        "count(autoscaler_service_p95_ms > on (service) autoscaler_service_slo_p95_ms)": 1.0,
        "autoscaler_max_workers": 5.0,
        "autoscaler_current_hosts": 4.0,
        "autoscaler_current_workers": 3.0,
        "autoscaler_effective_min_workers": 0.0,
        "autoscaler_cluster_cpu_percent": 71.0,
        "autoscaler_cluster_mem_percent": 58.0,
    }.get(expr)


_LOG = """\
2026-08-12T14:02:11.881Z INFO  [main] com.acme.aichat.Application - Ktor started on 0.0.0.0:8080
2026-08-12T14:02:11.902Z INFO  [main] com.acme.aichat.Redis - connected to cache_redis:6379
2026-08-12T14:04:38.117Z INFO  [eventLoop-3] com.acme.aichat.Chat - completion in 812ms (model=gpt-4o-mini)
2026-08-12T14:05:02.559Z WARN  [eventLoop-1] com.acme.aichat.RateLimit - throttled tenant=acme-9931
2026-08-12T14:06:44.230Z INFO  [eventLoop-7] com.acme.aichat.Chat - completion in 1.4s (model=gpt-4o)
2026-08-12T14:07:19.004Z ERROR [eventLoop-2] com.acme.aichat.Upload - stream closed before EOF
2026-08-12T14:09:55.771Z INFO  [eventLoop-5] com.acme.aichat.Health - ok
"""


def logs(service_name, lines=200):
    return _LOG


def nodes():
    return [
        {"id": "k39dl2mzq018", "full_id": "k39dl2mzq018aa", "hostname": "aichat-master",
         "role": "manager", "state": "ready",
         "availability": "active", "tone": "ok", "cpus": 4, "memory_gb": 7.6,
         "engine": "27.3.1", "addr": "10.0.0.2"},
        {"id": "w1af02c9be47", "full_id": "w1af02c9be47aa",
         "hostname": "aichat-worker-1754812203", "role": "worker",
         "state": "ready", "availability": "active", "tone": "ok", "cpus": 3, "memory_gb": 3.8,
         "engine": "27.3.1", "addr": "10.0.0.5"},
        {"id": "w2bc71d0aa93", "full_id": "w2bc71d0aa93aa",
         "hostname": "aichat-worker-1754819114", "role": "worker",
         "state": "ready", "availability": "active", "tone": "ok", "cpus": 3, "memory_gb": 3.8,
         "engine": "27.3.1", "addr": "10.0.0.6"},
        {"id": "w3de92f1cc05", "full_id": "w3de92f1cc05aa",
         "hostname": "aichat-worker-1754823887", "role": "worker",
         "state": "ready", "availability": "drain", "tone": "warn", "cpus": 3, "memory_gb": 3.8,
         "engine": "27.3.1", "addr": "10.0.0.7"},
    ]


# Same shape as swarm.topology(): one entry per RUNNING task, named individually.
# A worker mid-drain with only a couple of tasks left is the state most worth
# being able to see at a glance, so the dummy data shows one.
_BANDS = [("applications", "prod"), ("data", "data"), ("ingress", "staging"),
          ("observability", "observe"), ("platform", "platform")]
_BAND_OF = {
    "api": ("applications", "prod"),
    "api-staging": ("applications", "prod"),
    "cache": ("data", "data"),
    "sessions": ("data", "data"),
    "cloudflared": ("ingress", "staging"),
    "ui": ("platform", "platform"),
    "autoscaler": ("platform", "platform"),
}
_SERVICE_OF = {
    "api": "api_app", "api-staging": "api-staging_app",
    "cache": "cache_redis", "sessions": "sessions_redis",
    "cloudflared": "ingress_cloudflared", "ui": "admin_ui",
}


def _tasks(spec):
    """spec: [(short_name, replica_count), ...] -> flat per-task list."""
    rank = {b: i for i, (b, _) in enumerate(_BANDS)}
    out = []
    for name, count in spec:
        band, key = _BAND_OF.get(name, ("observability", "observe"))
        full = _SERVICE_OF.get(name, f"monitoring_{name}")
        for i in range(count):
            out.append({"id": f"{name[:6]}{i}kd93jf01"[:12], "name": name,
                        "service": full, "band": band, "key": key})
    out.sort(key=lambda x: (rank.get(x["band"], 99), x["name"], x["id"]))
    return out


def topology():
    n = {x["hostname"]: x for x in nodes()}
    rows = [
        (n["aichat-master"], 78.0, 61.0, [
            ("victoriametrics", 1), ("vmagent", 1), ("vmalert", 1),
            ("alertmanager", 1), ("loki", 1), ("grafana", 1),
            ("node-exporter", 1), ("cadvisor", 1),
            ("cache", 1), ("sessions", 1), ("redis-exporter", 1),
            ("cloudflared", 1), ("autoscaler", 1), ("ui", 1),
        ]),
        (n["aichat-worker-1754812203"], 64.0, 47.0, [
            ("api", 3), ("node-exporter", 1), ("cadvisor", 1), ("cloudflared", 1),
        ]),
        (n["aichat-worker-1754819114"], 71.0, 52.0, [
            ("api", 2), ("api-staging", 1),
            ("node-exporter", 1), ("cadvisor", 1), ("cloudflared", 1),
        ]),
        (n["aichat-worker-1754823887"], 12.0, 19.0, [
            ("api", 1), ("node-exporter", 1), ("cadvisor", 1),
        ]),
    ]
    out = []
    for node, cpu, mem, spec in rows:
        items = _tasks(spec)
        counts = {}
        for it in items:
            counts[it["name"]] = counts.get(it["name"], 0) + 1
        out.append({**node, "tasks_total": len(items), "tasks": items,
                    "by_service": [{"name": k, "count": v} for k, v in sorted(counts.items())],
                    "cpu_pct": cpu, "mem_pct": mem})
    return {"nodes": out,
            "bands": [{"band": b, "key": k} for b, k in _BANDS],
            "max_tasks": max(x["tasks_total"] for x in out)}


def summary():
    return shape.summary(service, nodes(), vm_query)


def autoscaler_state():
    return {
        "services": [
            {"service": "api_app", "component": "api", "p95": 412.0, "slo": 500.0,
             "breaching": False, "current": 6, "desired": 8, "admitted": 6, "capped": True,
             "running": 6, "pending": 0, "min": 2, "max": 12, "cpu_per_replica": 64.0,
             "worker_pinned": 1.0},
            {"service": "api-staging_app", "component": "api-staging", "p95": 96.0,
             "slo": 800.0, "breaching": False, "current": 1, "desired": 1, "admitted": 1,
             "capped": False, "running": 1, "pending": 0, "min": 1, "max": 2,
             "cpu_per_replica": 8.0, "worker_pinned": 1.0},
        ],
        "signals": [
            {"key": "Node CPU", "value": 71.0, "unit": "%",
             "note": "Placement guard only, never a trigger. Reads the workers, "
                     "or the master when the fleet is empty."},
            {"key": "Node memory", "value": 58.0, "unit": "%",
             "note": "Placement guard only."},
            {"key": "Demand", "value": 3.6, "unit": "cores",
             "note": "Reservations of every application replica that has to be placed."},
            {"key": "Free", "value": 1.1, "unit": "cores",
             "note": "What the eligible nodes have left after everything else on them."},
        ],
        "worker_mode": 1.0, "mixed_placement": 0.0, "managed": 2.0,
        "demand_cpu": 3.6, "demand_mem": 2_684_354_560,
        "manager_free_cpu": 1.75, "manager_free_mem": 5_100_273_664,
        "worker_free_cpu": 1.1, "new_worker_cpu": 2.8,
        "current_workers": 3, "hosts": 4, "desired_workers": 4,
        "max_workers": 5, "min_workers": 0,
        "last_loop": 1_770_000_000.0,
    }


def alert_destination():
    return {"configured": True, "kind": "Telegram"}


def alerts():
    return [
        {"name": "Watchdog", "group": "meta", "state": "firing", "tone": "ok",
         "severity": "none", "summary": "Alerting pipeline is alive"},
        {"name": "SLOBreach", "group": "app", "state": "inactive", "tone": "ok",
         "severity": "critical", "summary": "p95 on a component is above its SLO"},
        {"name": "HighErrorRate", "group": "app", "state": "pending", "tone": "warn",
         "severity": "critical", "summary": "Over 5% of requests to api_app are returning 5xx"},
        {"name": "NoHealthyReplicas", "group": "app", "state": "inactive", "tone": "ok",
         "severity": "critical", "summary": "A component wants replicas but none are running"},
        {"name": "ReplicaCeiling", "group": "app", "state": "inactive", "tone": "ok",
         "severity": "warning", "summary": "A component is at its replica ceiling"},
        {"name": "AppMinReplicasUnsatisfiable", "group": "app", "state": "inactive", "tone": "ok",
         "severity": "critical", "summary": "The cluster cannot host a component's minimum"},
        {"name": "NodeDown", "group": "infra", "state": "inactive", "tone": "ok",
         "severity": "critical", "summary": "Node is not reporting"},
        {"name": "NodeDiskFilling", "group": "infra", "state": "inactive", "tone": "ok",
         "severity": "warning", "summary": "Disk over 85%"},
        {"name": "AutoscalerAtMax", "group": "infra", "state": "inactive", "tone": "ok",
         "severity": "warning", "summary": "Autoscaler pinned at MAX_WORKERS"},
        {"name": "AutoscalerStalled", "group": "infra", "state": "inactive", "tone": "ok",
         "severity": "critical", "summary": "Autoscaler has not completed a loop in 5 minutes"},
    ]


def deploy_system_stack(name):
    return True, f"{name}: services converged"


def restart(service_name):
    return True, f"Rolling restart started for {service_name}."


def rollback(service_name):
    return True, f"Rolled {service_name} back to its previous spec."


def master_ip():
    return "10.0.0.2"


def deploy_image(service_name, image):
    return True, f"{service_name}: image updated to {image}\nverify: Service converged"


_HISTORY = [
    {"at": "2026-08-10T13:41:58+00:00", "epoch": 1786714918, "component": "api",
     "image": "ghcr.io/acme/aichat-api:sha-9f3ac21", "source": "ci", "actor": "github-actions",
     "ok": True, "detail": "api_app: image updated\nverify: Service converged"},
    {"at": "2026-08-10T11:02:11+00:00", "epoch": 1786705331, "component": "api-staging",
     "image": "ghcr.io/acme/aichat-api:sha-c40e8b7", "source": "ci", "actor": "github-actions",
     "ok": True, "detail": "api-staging_app: image updated\nverify: Service converged"},
    {"at": "2026-08-09T16:20:44+00:00", "epoch": 1786652444, "component": "api",
     "image": "ghcr.io/acme/aichat-api:sha-77b1e05", "source": "ci", "actor": "github-actions",
     "ok": False, "detail": "update paused due to failure; rolled back to sha-4c9920a\n"
                            "task health check failed after start_period"},
    {"at": "2026-08-09T09:14:02+00:00", "epoch": 1786626842, "component": "api",
     "image": "ghcr.io/acme/aichat-api:sha-4c9920a", "source": "panel", "actor": "admin",
     "ok": True, "detail": "configuration change: LOG_LEVEL, OPENAI_TIMEOUT_MS"},
    {"at": "2026-08-08T18:47:30+00:00", "epoch": 1786574850, "component": "api",
     "image": "ghcr.io/acme/aichat-api:sha-4c9920a", "source": "ci", "actor": "github-actions",
     "ok": True, "detail": "api_app: image updated\nverify: Service converged"},
]


def history(name=None, limit=25):
    rows = [h for h in _HISTORY if not name or h["component"] == name]
    for row in rows:
        row.setdefault("status", "done" if row.get("ok") else "failed")
    return rows[:limit]


def update_status(service_name):
    """Swarm's verdict on the last rollout. Mirrors swarm.update_status()."""
    return {"state": "completed", "verdict": "done", "started_epoch": None,
            "message": "update completed", "at": "6m ago"}


def deployments(name, service_name, limit=25):
    """Mirrors swarm.deployments(). Nothing to reconcile against fixtures."""
    return history(name, limit=limit), update_status(service_name)


def port_is_open(port):
    return str(port) == "46379"


def registry_logins():
    return [
        {"registry": "ghcr.io", "username": "acme-bot"},
        {"registry": "registry.gitlab.com", "username": "deploy-token-91"},
    ]
