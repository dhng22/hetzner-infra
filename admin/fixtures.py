"""
Dummy cluster used by the preview build.

Same shapes as swarm.py, so the preview is rendered from the real templates
and cannot drift from the real UI. The numbers describe a plausible Tuesday
afternoon: production busy but inside SLO, staging fine, one worker mid-drain,
and one alert firing so the warning states are actually visible.
"""

import catalog

_IMG = "ghcr.io/acme/aichat-api"


def _svc(name, image, running, desired, state="healthy", tone="ok", mode="replicated",
         env=None, cpu=1.0, mem=768, cpu_res=0.5, mem_res=384, placement=None,
         updated="14m ago", tasks=None, networks=("edge", "data-prod", "monitoring")):
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
                          "monitor": "90s", "failure_action": "rollback"},
        "placement": placement if placement is not None else ["node.role == worker"],
    }


PROD_ENV = {
    "KTOR_ENV": "production", "REDIS_HOST": "redis-prod", "REDIS_PORT": "6379",
    "MONGO_URI": "mongodb+srv://appuser:s3cr3t@cluster0.mongodb.net/appdb?retryWrites=true&w=majority",
    "REDIS_PASSWORD": "8f2b91c4de77a0135be2",
    "LOG_LEVEL": "INFO", "FEATURE_NEW_CHECKOUT": "true",
    "OPENAI_TIMEOUT_MS": "30000", "MAX_UPLOAD_MB": "25",
}
STAGING_ENV = {
    "KTOR_ENV": "staging", "REDIS_HOST": "redis-staging", "REDIS_PORT": "6379",
    "MONGO_URI": "mongodb+srv://appuser:s3cr3t@cluster0.mongodb.net/appdb_staging?retryWrites=true&w=majority",
    "REDIS_PASSWORD": "b71ce0aa2f9384d51c60",
    "LOG_LEVEL": "DEBUG", "FEATURE_NEW_CHECKOUT": "true", "MAX_UPLOAD_MB": "5",
}

_SERVICES = {
    "app_api-prod": _svc("app_api-prod", f"{_IMG}:sha-9f3ac21", 6, 6, env=PROD_ENV),
    "app_api-staging": _svc("app_api-staging", f"{_IMG}:sha-c40e8b7", 1, 1, env=STAGING_ENV,
                            cpu=0.5, mem=512, cpu_res=0.1, mem_res=128,
                            networks=("edge", "data-staging", "monitoring"), updated="2h ago"),
    "app_redis-prod": _svc("app_redis-prod", "redis:7.4-alpine", 1, 1, cpu=None, mem=None,
                           cpu_res=None, mem_res=None, placement=["node.role == manager"],
                           networks=("data-prod",), updated="6d ago"),
    "app_redis-staging": _svc("app_redis-staging", "redis:7.4-alpine", 1, 1, cpu=None, mem=None,
                              cpu_res=None, mem_res=None, placement=["node.role == manager"],
                              networks=("data-staging",), updated="6d ago"),
    "app_cloudflared": _svc("app_cloudflared", "cloudflare/cloudflared:2024.10.1", 3, 4,
                            state="updating", tone="warn", mode="global", cpu=None, mem=None,
                            cpu_res=None, mem_res=None, networks=("edge", "monitoring"),
                            updated="3m ago"),
    "monitoring_grafana": _svc("monitoring_grafana", "grafana/grafana:11.3.0", 1, 1,
                               cpu=None, mem=None, cpu_res=None, mem_res=None,
                               placement=["node.role == manager"], networks=("monitoring",),
                               updated="6d ago"),
    "monitoring_victoriametrics": _svc("monitoring_victoriametrics",
                                       "victoriametrics/victoria-metrics:v1.106.1", 1, 1,
                                       cpu=None, mem=1500, cpu_res=None, mem_res=None,
                                       placement=["node.role == manager"],
                                       networks=("monitoring",), updated="6d ago"),
    "monitoring_vmagent": _svc("monitoring_vmagent", "victoriametrics/vmagent:v1.106.1", 1, 1,
                               cpu=None, mem=None, cpu_res=None, mem_res=None,
                               placement=["node.role == manager"], networks=("monitoring",),
                               updated="6d ago"),
    "monitoring_loki": _svc("monitoring_loki", "grafana/loki:3.1.1", 1, 1, cpu=None, mem=None,
                            cpu_res=None, mem_res=None, placement=["node.role == manager"],
                            networks=("monitoring",), updated="6d ago"),
    "monitoring_vmalert": _svc("monitoring_vmalert", "victoriametrics/vmalert:v1.106.1", 1, 1,
                               cpu=None, mem=None, cpu_res=None, mem_res=None,
                               placement=["node.role == manager"], networks=("monitoring",),
                               updated="6d ago"),
    "monitoring_alertmanager": _svc("monitoring_alertmanager", "prom/alertmanager:v0.27.0", 1, 1,
                                    cpu=None, mem=None, cpu_res=None, mem_res=None,
                                    placement=["node.role == manager"], networks=("monitoring",),
                                    updated="6d ago"),
    "monitoring_autoscaler": _svc("monitoring_autoscaler", "aichat/autoscaler:latest", 1, 1,
                                  cpu=None, mem=None, cpu_res=None, mem_res=None,
                                  placement=["node.role == manager"], networks=("monitoring",),
                                  updated="6d ago"),
    "monitoring_node-exporter": _svc("monitoring_node-exporter", "prom/node-exporter:v1.8.2",
                                     4, 4, mode="global", cpu=None, mem=None, cpu_res=None,
                                     mem_res=None, placement=[], networks=("monitoring",),
                                     updated="6d ago"),
    "monitoring_cadvisor": _svc("monitoring_cadvisor", "gcr.io/cadvisor/cadvisor:v0.49.1", 4, 4,
                                mode="global", cpu=None, mem=None, cpu_res=None, mem_res=None,
                                placement=[], networks=("monitoring",), updated="6d ago"),
    "admin_ui": _svc("admin_ui", "aichat/admin:latest", 1, 1, cpu=None, mem=256,
                     cpu_res=None, mem_res=None, placement=["node.role == manager"],
                     networks=("monitoring",), updated="21m ago"),
}


def service(name, with_tasks=True):
    svc = _SERVICES.get(name)
    if not svc:
        return {"name": name, "exists": False, "tone": "mute", "state": "missing",
                "image": "—", "image_short": "—", "running": 0, "desired": 0, "tasks": [],
                "env": {}, "updated": "—", "mode": "—", "resources": {}, "placement": [],
                "update_config": {}, "networks": []}
    return svc


def apps():
    out = []
    for entry in catalog.CATALOG:
        envs = {k: service(v) for k, v in entry["environments"].items()}
        tones = [e["tone"] for e in envs.values()]
        worst = "bad" if "bad" in tones else "warn" if "warn" in tones else "ok"
        out.append({**entry, "envs": envs, "tone": worst})
    return out


def app(key):
    entry = catalog.BY_KEY.get(key)
    if not entry:
        return None
    envs = {k: service(v) for k, v in entry["environments"].items()}
    tones = [e["tone"] for e in envs.values()]
    worst = "bad" if "bad" in tones else "warn" if "warn" in tones else "ok"
    return {**entry, "envs": envs, "tone": worst}


_LOG = """2026-08-10T13:42:01Z INFO  [main] Ktor server started on 0.0.0.0:8080 (JVM 1284ms)
2026-08-10T13:42:01Z INFO  [main] Mongo connection pool ready (min=5 max=50)
2026-08-10T13:42:02Z INFO  [main] Redis connected redis-prod:6379
2026-08-10T13:44:17Z INFO  [req] POST /v1/chat 200 143ms user=u_8812
2026-08-10T13:44:19Z INFO  [req] POST /v1/chat 200 208ms user=u_4410
2026-08-10T13:46:55Z WARN  [pool] Mongo checkout wait 812ms, pool saturated
2026-08-10T13:47:02Z INFO  [req] GET  /v1/threads 200 41ms user=u_8812
2026-08-10T13:48:30Z ERROR [req] POST /v1/chat 502 upstream timeout after 30000ms
2026-08-10T13:48:31Z INFO  [req] POST /v1/chat 200 388ms user=u_9931
2026-08-10T13:51:10Z INFO  [health] /health ok, uptime 9m
"""


def logs(service_name, lines=200):
    return _LOG


def nodes():
    return [
        {"id": "k39dl2mzq018", "hostname": "aichat-master", "role": "manager", "state": "ready",
         "availability": "active", "tone": "ok", "cpus": 4, "memory_gb": 7.6,
         "engine": "27.3.1", "addr": "10.0.0.2"},
        {"id": "w1af02c9be47", "hostname": "aichat-worker-1754812203", "role": "worker",
         "state": "ready", "availability": "active", "tone": "ok", "cpus": 3, "memory_gb": 3.8,
         "engine": "27.3.1", "addr": "10.0.0.5"},
        {"id": "w2bc71d0aa93", "hostname": "aichat-worker-1754819114", "role": "worker",
         "state": "ready", "availability": "active", "tone": "ok", "cpus": 3, "memory_gb": 3.8,
         "engine": "27.3.1", "addr": "10.0.0.6"},
        {"id": "w3de92f1cc05", "hostname": "aichat-worker-1754823887", "role": "worker",
         "state": "ready", "availability": "drain", "tone": "warn", "cpus": 3, "memory_gb": 3.8,
         "engine": "27.3.1", "addr": "10.0.0.7"},
    ]


def summary():
    return {
        "p95": 412.0, "slo": 500.0, "p95_tone": "warn",
        "replicas_running": 6, "replicas_desired": 6,
        "workers": 3, "workers_ready": 2, "max_workers": 6,
        "degraded": [a for a in apps() if a["tone"] in ("bad", "warn")],
        "cpu_per_replica": 64.0,
    }


def autoscaler_state():
    return {
        "signals": [
            {"key": "p95 latency", "value": 412.0, "unit": "ms",
             "note": "Primary signal. What users feel."},
            {"key": "CPU per replica", "value": 64.0, "unit": "%",
             "note": "Secondary. Keeps scale-down working when traffic is near zero."},
            {"key": "Worker CPU", "value": 71.0, "unit": "%",
             "note": "Placement guard only, never a trigger."},
            {"key": "Worker memory", "value": 58.0, "unit": "%",
             "note": "Placement guard only."},
        ],
        "current_replicas": 6, "desired_replicas": 8, "max_replicas": 12,
        "current_workers": 3, "desired_workers": 4, "max_workers": 6, "min_workers": 2,
        "slo": 500.0, "last_loop": 1_770_000_000.0,
    }


def alerts():
    return [
        {"name": "Watchdog", "group": "meta", "state": "firing", "tone": "ok",
         "severity": "none", "summary": "Alerting pipeline is alive"},
        {"name": "SLOBreach", "group": "app", "state": "inactive", "tone": "ok",
         "severity": "critical", "summary": "p95 is above the SLO and the autoscaler has not recovered it"},
        {"name": "HighErrorRate", "group": "app", "state": "pending", "tone": "warn",
         "severity": "critical", "summary": "Over 5% of requests are returning 5xx"},
        {"name": "NoHealthyReplicas", "group": "app", "state": "inactive", "tone": "ok",
         "severity": "critical", "summary": "No healthy app replicas are being scraped"},
        {"name": "ReplicaCeiling", "group": "app", "state": "inactive", "tone": "ok",
         "severity": "warning", "summary": "Replica ceiling reached — capacity plan or app efficiency issue"},
        {"name": "NodeDown", "group": "infra", "state": "inactive", "tone": "ok",
         "severity": "critical", "summary": "Node is not reporting"},
        {"name": "NodeDiskFilling", "group": "infra", "state": "inactive", "tone": "ok",
         "severity": "warning", "summary": "Disk over 85%"},
        {"name": "AutoscalerAtMax", "group": "infra", "state": "inactive", "tone": "ok",
         "severity": "warning", "summary": "Autoscaler pinned at MAX_WORKERS"},
        {"name": "AutoscalerStalled", "group": "infra", "state": "inactive", "tone": "ok",
         "severity": "critical", "summary": "Autoscaler has not completed a loop in 5 minutes"},
    ]


def redeploy_stack():
    return True, "preserving live replica count: 6\npreserving deployed prod image: ghcr.io/acme/aichat-api:sha-9f3ac21\nUpdating service app_api-prod (id: 8xk2)"


def restart(service_name):
    return True, f"Rolling restart started for {service_name}."


def rollback(service_name):
    return True, f"Rolled {service_name} back to its previous spec."


def master_ip():
    return "10.0.0.2"


def deploy_image(service_name, image):
    return True, f"{service_name}: image updated to {image}\nverify: Service converged"


_HISTORY = [
    {"at": "2026-08-10T13:41:58+00:00", "epoch": 1786714918, "app": "app", "env": "prod",
     "image": "ghcr.io/acme/aichat-api:sha-9f3ac21", "source": "ci", "actor": "github-actions",
     "ok": True, "detail": "app_api-prod: image updated\nverify: Service converged"},
    {"at": "2026-08-10T11:02:11+00:00", "epoch": 1786705331, "app": "app", "env": "staging",
     "image": "ghcr.io/acme/aichat-api:sha-c40e8b7", "source": "ci", "actor": "github-actions",
     "ok": True, "detail": "app_api-staging: image updated\nverify: Service converged"},
    {"at": "2026-08-09T16:20:44+00:00", "epoch": 1786652444, "app": "app", "env": "prod",
     "image": "ghcr.io/acme/aichat-api:sha-77b1e05", "source": "ci", "actor": "github-actions",
     "ok": False, "detail": "update paused due to failure; rolled back to sha-4c9920a\n"
                            "task health check failed after start_period"},
    {"at": "2026-08-09T09:14:02+00:00", "epoch": 1786626842, "app": "app", "env": "prod",
     "image": "ghcr.io/acme/aichat-api:sha-4c9920a", "source": "panel", "actor": "admin",
     "ok": True, "detail": "configuration change: LOG_LEVEL, OPENAI_TIMEOUT_MS"},
    {"at": "2026-08-08T18:47:30+00:00", "epoch": 1786574850, "app": "app", "env": "prod",
     "image": "ghcr.io/acme/aichat-api:sha-4c9920a", "source": "ci", "actor": "github-actions",
     "ok": True, "detail": "app_api-prod: image updated\nverify: Service converged"},
]


def history(app_key=None, env=None, limit=25):
    rows = [h for h in _HISTORY
            if (not app_key or h["app"] == app_key) and (not env or h["env"] == env)]
    return rows[:limit]


def port_is_open(port):
    return str(port) == "46379"


def registry_logins():
    return [
        {"registry": "ghcr.io", "username": "acme-bot"},
        {"registry": "registry.gitlab.com", "username": "deploy-token-91"},
    ]
