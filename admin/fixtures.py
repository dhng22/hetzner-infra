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
import charts
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


#: Swarm PINS a digest onto any image it can resolve against a registry, so a
#: live service spec carries `tag@sha256:...` while the deploy that asked for it
#: recorded the bare tag. The fixtures used to hold the bare tag on both sides,
#: which is precisely why "is the newest image live?" compared equal here and
#: reported "not live yet" against a real cluster for days.
_DIGEST = "@sha256:ab1b76aeca1837e20a816bfea14687d41d86942bac091969dcf5384b514c95c0"

_SERVICES = {
    "api_app": _svc("api_app", f"{_IMG}:sha-9f3ac21{_DIGEST}", 6, 6),
    "api-staging_app": _svc("api-staging_app", f"{_IMG}:sha-c40e8b7", 1, 1,
                            cpu=0.5, mem=512, cpu_res=0.1, mem_res=128, updated="2h ago"),
    # A managed Redis: the replica on the master plus three that exist as
    # services and DNS names and are not running. That IS the resting state —
    # the sentinel URL names all of them from the day it was created, so the
    # address never changes when dataguard starts one.
    "cache_redis-1": _svc("cache_redis-1", "redis:7.4-alpine", 1, 1, cpu=None, mem=None,
                          cpu_res=0.2, mem_res=640, placement=["node.role == manager"],
                          networks=("edge",), updated="6d ago"),
    "cache_redis-2": _svc("cache_redis-2", "redis:7.4-alpine", 0, 0, cpu=None, mem=None,
                          cpu_res=0.2, mem_res=640, networks=("edge",), updated="6d ago"),
    "cache_redis-3": _svc("cache_redis-3", "redis:7.4-alpine", 0, 0, cpu=None, mem=None,
                          cpu_res=0.2, mem_res=640, networks=("edge",), updated="6d ago"),
    "cache_redis-4": _svc("cache_redis-4", "redis:7.4-alpine", 0, 0, cpu=None, mem=None,
                          cpu_res=0.2, mem_res=640, networks=("edge",), updated="6d ago"),
    "cache_sentinel-1": _svc("cache_sentinel-1", "redis:7.4-alpine", 1, 1, cpu=None,
                             mem=None, cpu_res=0.01, mem_res=24, networks=("edge",),
                             updated="6d ago"),
    "cache_sentinel-2": _svc("cache_sentinel-2", "redis:7.4-alpine", 1, 1, cpu=None,
                             mem=None, cpu_res=0.01, mem_res=24, networks=("edge",),
                             updated="6d ago"),
    "cache_sentinel-3": _svc("cache_sentinel-3", "redis:7.4-alpine", 1, 1, cpu=None,
                             mem=None, cpu_res=0.01, mem_res=24, networks=("edge",),
                             updated="6d ago"),
    "cache_redis-exporter": _svc("cache_redis-exporter", "oliver006/redis_exporter:v1.66.0",
                                 1, 1, cpu=None, mem=None, cpu_res=0.05, mem_res=32,
                                 placement=["node.role == manager"], updated="6d ago"),
    # A managed Mongo that has already grown: member 1 on the master, member 2
    # on a machine of its own, members 3 and 4 named and not yet running.
    "documents_mongo-1": _svc("documents_mongo-1", "mongo:7.0", 1, 1, cpu=None, mem=None,
                              cpu_res=0.3, mem_res=768,
                              placement=["node.role == manager"],
                              networks=("edge",), updated="4d ago"),
    "documents_mongo-2": _svc("documents_mongo-2", "mongo:7.0", 1, 1, cpu=None, mem=None,
                              cpu_res=0.3, mem_res=768,
                              placement=["node.hostname == aichat-db-1756"],
                              networks=("edge",), updated="1d ago"),
    "documents_mongo-3": _svc("documents_mongo-3", "mongo:7.0", 0, 0, cpu=None, mem=None,
                              cpu_res=0.3, mem_res=768, networks=("edge",), updated="4d ago"),
    "documents_mongo-4": _svc("documents_mongo-4", "mongo:7.0", 0, 0, cpu=None, mem=None,
                              cpu_res=0.3, mem_res=768, networks=("edge",), updated="4d ago"),
    "documents_mongo-exporter": _svc("documents_mongo-exporter",
                                     "percona/mongodb_exporter:0.43.1", 1, 1, cpu=None,
                                     mem=None, cpu_res=0.05, mem_res=64,
                                     placement=["node.role == manager"], updated="4d ago"),
    "documents_pbm-ctl": _svc("documents_pbm-ctl",
                              "percona/percona-backup-mongodb:2.8.0", 1, 1, cpu=None,
                              mem=None, cpu_res=0.02, mem_res=48,
                              placement=["node.role == manager"], updated="4d ago"),
    "ingress_cloudflared": _svc("ingress_cloudflared", "cloudflare/cloudflared:2024.10.1",
                                4, 4, mode="global", cpu=None, mem=None, cpu_res=0.05,
                                mem_res=32, placement=[], updated="9d ago"),
    "admin_ui": _svc("admin_ui", "ghcr.io/dhng22/hetzner-infra/admin:73887dec9c22", 1, 1, cpu=0.5, mem=256,
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
    ("monitoring_autoscaler", "ghcr.io/dhng22/hetzner-infra/autoscaler:73887dec9c22", 0.1, 96),
    ("monitoring_overseer", "ghcr.io/dhng22/hetzner-infra/overseer:73887dec9c22", 0.04, 96),
    ("monitoring_dataguard", "ghcr.io/dhng22/hetzner-infra/dataguard:73887dec9c22", 0.03, 64),
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
    # Same shape as the live panel, cluster share included — the preview exists
    # to catch a template reaching for a key one of them does not set.
    return shape.with_cluster_share(shape.component_views(service), nodes())


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
        "overseer_max_workers": 5.0,
        "overseer_current_hosts": 4.0,
        "overseer_current_workers": 3.0,
        "overseer_effective_min_workers": 0.0,
        "overseer_cluster_cpu_percent": 71.0,
        "overseer_cluster_mem_percent": 58.0,
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


def log_events(service_name, lines=200, since_ns=None):
    """
    Mirrors swarm.log_events(). A poll after the first returns nothing new,
    which is the state the preview should show: a quiet service, following.
    """
    if since_ns:
        return [], since_ns, ""
    rows = [(1755000000000000000 + i * 10**9, line)
            for i, line in enumerate(_LOG.strip().splitlines())]
    return shape.log_rows(rows), rows[-1][0], ""


def nodes():
    return [
        {"id": "k39dl2mzq018", "full_id": "k39dl2mzq018aa", "hostname": "aichat-master",
         "role": "manager", "state": "ready",
         "availability": "active", "tone": "ok", "cpus": 4, "memory_gb": 7.6,
         "engine": "27.3.1", "cpu_reserved": 1.2, "mem_reserved_mb": 1400,
         "cpu_reserved_pct": 62.0, "mem_reserved_pct": 48.0, "addr": "10.0.0.2",
         "labels": {"disk": "ssd"}, "os": "linux", "arch": "x86_64",
         "leader": True, "reachability": "reachable",
         "created_at": "2025-06-01T09:14:02", "updated_at": "2025-08-09T11:02:44"},
        {"id": "w1af02c9be47", "full_id": "w1af02c9be47aa",
         "hostname": "aichat-worker-1754812203", "role": "worker",
         "state": "ready", "availability": "active", "tone": "ok", "cpus": 3, "memory_gb": 3.8,
         "engine": "27.3.1", "cpu_reserved": 1.2, "mem_reserved_mb": 1400,
         "cpu_reserved_pct": 62.0, "mem_reserved_pct": 48.0, "addr": "10.0.0.5",
         "labels": {"managedby": "autoscaler"}, "os": "linux", "arch": "x86_64",
         "leader": False, "reachability": "",
         "created_at": "2025-08-10T07:10:03", "updated_at": "2025-08-10T07:11:19"},
        {"id": "w2bc71d0aa93", "full_id": "w2bc71d0aa93aa",
         "hostname": "aichat-worker-1754819114", "role": "worker",
         "state": "ready", "availability": "active", "tone": "ok", "cpus": 3, "memory_gb": 3.8,
         "engine": "27.3.1", "cpu_reserved": 1.2, "mem_reserved_mb": 1400,
         "cpu_reserved_pct": 62.0, "mem_reserved_pct": 48.0, "addr": "10.0.0.6",
         "labels": {"zone": "eu", "managedby": "autoscaler"}, "os": "linux", "arch": "x86_64",
         "leader": False, "reachability": "",
         "created_at": "2025-08-10T09:05:14", "updated_at": "2025-08-10T09:06:31"},
        {"id": "w3de92f1cc05", "full_id": "w3de92f1cc05aa",
         "hostname": "aichat-worker-1754823887", "role": "worker",
         "state": "ready", "availability": "drain", "tone": "warn", "cpus": 3, "memory_gb": 3.8,
         "engine": "27.3.1", "cpu_reserved": 1.2, "mem_reserved_mb": 1400,
         "cpu_reserved_pct": 62.0, "mem_reserved_pct": 48.0, "addr": "10.0.0.7",
         "labels": {"managedby": "autoscaler"}, "os": "linux", "arch": "x86_64",
         "leader": False, "reachability": "",
         "created_at": "2025-08-10T10:24:47", "updated_at": "2025-08-10T12:41:02"},
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
    "documents": ("data", "data"),
    "cloudflared": ("ingress", "staging"),
    "ui": ("platform", "platform"),
    "autoscaler": ("platform", "platform"),
}
_SERVICE_OF = {
    "api": "api_app", "api-staging": "api-staging_app",
    "cache": "cache_redis-1",
    "documents": "documents_mongo-1",
    "cloudflared": "ingress_cloudflared", "ui": "admin_ui",
}


_IMAGE_OF = {
    "api": "ghcr.io/acme/aichat-api:sha-9f3ac21",
    "api-staging": "ghcr.io/acme/aichat-api:sha-c40e8b7",
    "cache": "redis:7.4-alpine",
    "documents": "mongo:7.0",
    "cloudflared": "cloudflare/cloudflared:2024.8.3",
    "ui": "aichat-admin:local",
}


def _tasks(spec):
    """spec: [(short_name, replica_count), ...] -> flat per-task list."""
    rank = {b: i for i, (b, _) in enumerate(_BANDS)}
    out = []
    for name, count in spec:
        band, key = _BAND_OF.get(name, ("observability", "observe"))
        full = _SERVICE_OF.get(name, f"monitoring_{name}")
        # Varied so the preview exercises every branch the live panel can hit:
        # a failing task, a starting one, and a range of reservation sizes.
        states = ["running"] * count
        if name == "api" and count > 2:
            states[-1] = "starting"
        if name == "api-staging":
            states[0] = "failed"
        for i in range(count):
            state = states[i]
            share = {"victoriametrics": (7.5, 10.1), "loki": (5.0, 8.4),
                     "api": (18.0, 12.0), "grafana": (2.5, 3.4),
                     "cache": (10.5, 16.8)}.get(name, (1.5, 2.5))
            # Two tags on `api`, one of them on a single replica: a rolling
            # update caught halfway is the state the Map tab exists for, so the
            # preview shows one rather than a uniform fleet.
            image = _IMAGE_OF.get(name, f"{name}:v1.0.0")
            if name == "api" and i == 0:
                image = "ghcr.io/acme/aichat-api:sha-c40e8b7"
            out.append({"id": f"{name[:6]}{i}kd93jf01"[:12], "name": name,
                        "service": full, "band": band, "key": key,
                        "image": image, "tag": shape.image_tag(image),
                        "state": state,
                        "tone": {"running": "ok", "starting": "warn",
                                 "failed": "bad"}.get(state, "mute"),
                        "cpu_res": int(share[0] / 100 * 4e9),
                        "mem_res": int(share[1] / 100 * 7.6 * 1024 ** 3),
                        "cpu_share": share[0], "mem_share": share[1]})
    out.sort(key=lambda x: (rank.get(x["band"], 99), x["name"], x["id"]))
    return out


def topology():
    n = {x["hostname"]: x for x in nodes()}
    rows = [
        (n["aichat-master"], 78.0, 61.0, [
            ("victoriametrics", 1), ("vmagent", 1), ("vmalert", 1),
            ("alertmanager", 1), ("loki", 1), ("grafana", 1),
            ("node-exporter", 1), ("cadvisor", 1),
            ("cache", 1), ("redis-exporter", 1), ("documents", 1),
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
    for index, (node, cpu, mem, spec) in enumerate(rows):
        items = _tasks(spec)
        counts = {}
        for it in items:
            counts[it["name"]] = counts.get(it["name"], 0) + 1
        # A different disk figure per node, including one close to the line, so
        # the preview shows the warn tone rather than only the calm case.
        total = 160.0 if index == 0 else 80.0
        used_pct = (88.0, 41.0, 33.0, 12.0)[index % 4]
        out.append({**node, "tasks_total": len(items), "tasks": items,
                    "by_service": [{"name": k, "count": v} for k, v in sorted(counts.items())],
                    "cpu_pct": cpu, "mem_pct": mem,
                    "disk_pct": used_pct,
                    "disk_total_gb": total,
                    "disk_free_gb": round(total * (100 - used_pct) / 100, 1)})
    return {"nodes": out,
            "bands": [{"band": b, "key": k} for b, k in _BANDS],
            "max_tasks": max(x["tasks_total"] for x in out)}


def node(node_id):
    return shape.find_node(topology(), node_id)


def update_node(node_id, availability=None, labels=None):
    return True, "Preview build — nothing was changed."


def remove_node(node_id):
    return True, "Preview build — nothing was changed."


def component_map(services, component=None):
    return shape.component_map(topology(), services,
                               roles=member_roles(component) if component else None)


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
        "live": True,
        "fleet_live": True,
    }


def dataguard_state():
    """A cluster with one managed database, mid-way up the ladder."""
    import time
    now = time.time()
    return {
        "components": [
            {"component": "docs", "state": 2,
             "state_label": "Master + one machine", "lag": 0.4,
             "backup_at": now - 3600, "verified_at": now - 90000,
             "verified": True, "changing": False},
            {"component": "cache", "state": 1, "state_label": "On the master",
             "lag": None, "backup_at": None, "verified_at": None,
             "verified": False, "changing": False},
        ],
        # The interesting column: what is being held, and by which gate.
        "refused": [("cooldown", 14.0), ("no_verified_backup", 3.0)],
        "leases": 1.0,
        "managed": 2.0,
        "restore_in_flight": 0.0,
        "last_loop": now - 12,
        "live": True,
    }


def by_labels(expr, label):
    return {}


def component_data_gb(component):
    return 12.4 if component == "documents" else None


def member_roles(component):
    if component != "documents":
        return None
    return {"documents_mongo-1": "SECONDARY", "documents_mongo-2": "PRIMARY"}


def ensure_viewer(service):
    return True, ""


def viewer_running(service):
    return True


def touch_viewer(component):
    pass


def autoscaler_state_silent(_healthy=autoscaler_state):
    """
    The same page with nothing reporting — the autoscaler down, or its metrics
    not scraped yet.

    Every gauge is None, which is the state the live panel is in at the exact
    moment you go looking, and the state the fixtures never had: the tiles are
    individually guarded, one comparison between two of them was not, and the
    page 500'd in production while the preview of the same template looked
    perfect. A fixture that only ever describes a healthy cluster cannot catch
    that, so this one describes the other half.
    """
    # Bound at definition, because the caller that wants this state gets it by
    # replacing the module attribute — reading the name here would recurse.
    state = _healthy()
    for key, value in list(state.items()):
        if key in ("services", "signals"):
            continue
        state[key] = None
    state["services"] = []
    state["signals"] = [dict(s, value=None) for s in state["signals"]]
    state["live"] = False
    return state


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


def deploy_image_async(service_name, image):
    return True, f"preview: would start updating {service_name} to {image}"


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
            "message": "update completed", "at": "6m ago",
            "image": "ghcr.io/acme/aichat-api:sha-4c9920a",
            "image_short": "aichat-api:sha-4c9920a"}


def expire_pending(name, now=None):
    """No-op: fixtures have no clock and nothing to expire."""


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


def infra_version():
    """Mirrors swarm.infra_version(). A cluster that is current and healthy."""
    return {
        "configured": True,
        "commit": "9f2c1ab77e40c3d5b81aa0e6f4c92db3e77a1c40",
        "short": "9f2c1ab77e40",
        "branch": "master",
        "updated_at": "2026-08-08T18:41:02+00:00",
        "checked_at": "2026-08-08T19:12:44+00:00",
        "previous": "3ab90ff21c77",
        "status": "ok",
        "detail": "",
        "behind": False,
        "remote_short": "9f2c1ab77e40",
    }


def _wave(base, swing, points=61, step=60, phase=0.0):
    """
    A series that looks like a Tuesday afternoon rather than a sine wave.

    The preview exists to prove the templates and the chart code, so the numbers
    have to exercise the interesting cases — a rise, a plateau, a dip — without
    being so regular that a broken axis still looks plausible.
    """
    import math
    import time
    now = int(time.time())
    return [(now - (points - 1 - i) * step,
             max(0.0, base + swing * math.sin(i / 6.0 + phase)
                 + swing * 0.35 * math.sin(i / 2.3 + phase)))
            for i in range(points)]


#: What each preview expression answers with. Keyed by the constants in
#: `shape.py` rather than by copied strings, so renaming a query here is a
#: NameError instead of a silently empty chart.
_RANGES = {
    shape.Q_LATENCY: {"api_app": _wave(310, 120), "web_app": _wave(140, 40, phase=2)},
    shape.Q_STATUS_CLASS: {"2xx": _wave(11, 3), "3xx": _wave(1.2, 0.4),
                           "4xx": _wave(0.6, 0.3, phase=1), "5xx": _wave(0.15, 0.12)},
    shape.Q_ERROR_RATIO: {"5xx": [(t, v / 100.0) for t, v in _wave(1.4, 1.1)]},
    shape.Q_REQUEST_RATE: {"served": _wave(13, 4)},
}
for _name, _expr in shape.Q_UTILISATION:
    _RANGES[_expr] = {"master": _wave(58, 12), "wkr-1": _wave(71, 9),
                      "wkr-2": _wave(24, 8, phase=3)}
for _name, _expr in shape.Q_PRESSURE:
    _RANGES[_expr] = {"master": _wave(0.05, 0.04), "wkr-1": _wave(0.22, 0.15)}


def vm_query_range(expr, minutes=60, step=60, label=None):
    return _RANGES.get(expr, {})


def observability():
    """Mirrors swarm.observability() — the same function, canned series."""
    return shape.observability(vm_query_range, _obs_instant, charts)


def _obs_instant(expr):
    """The instant readings the column asks for, beside its range queries."""
    if expr == shape.Q_SLO:
        return 500.0
    for name, resource_expr in shape.Q_RESOURCE_ERRORS:
        if expr == resource_expr:
            return {"OOM kills": 0.0, "container OOM": 1.0, "tx errors": 0.0,
                    "rx errors": 0.0, "tx drops": 2.0}[name]
    return vm_query(expr)
