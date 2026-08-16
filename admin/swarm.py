"""
Live cluster state, shaped for the panel.

Every function here returns plain dicts in the same shape as fixtures.py, so
the templates never know which one they were handed. That is what lets the
preview be generated from the real templates instead of a mock that drifts.
"""

import datetime as _dt
import os

import docker
import requests

import catalog

VM_URL = os.environ.get("VM_URL", "http://victoriametrics:8428")
# vmalert serves the rule list; string-surgery on VM_URL broke the moment
# either address was configured differently.
VMALERT_URL = os.environ.get("VMALERT_URL", "http://vmalert:8880")
_client = None


def client():
    global _client
    if _client is None:
        _client = docker.DockerClient(base_url="unix:///var/run/docker.sock")
    return _client


# --- helpers ---------------------------------------------------------------

def _age(iso):
    if not iso:
        return "—"
    try:
        then = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00").split(".")[0] + "+00:00")
    except ValueError:
        return "—"
    secs = (_dt.datetime.now(_dt.timezone.utc) - then).total_seconds()
    for limit, unit, div in ((60, "s", 1), (3600, "m", 60), (86400, "h", 3600)):
        if secs < limit:
            return f"{int(secs / div)}{unit} ago"
    return f"{int(secs / 86400)}d ago"


def _short_image(image):
    """`ghcr.io/you/app:sha-abc@sha256:...` -> `app:sha-abc`."""
    if not image:
        return "—"
    ref = image.split("@")[0]
    return ref.rsplit("/", 1)[-1]


def vm_query(expr):
    try:
        r = requests.get(f"{VM_URL}/api/v1/query", params={"query": expr}, timeout=8)
        r.raise_for_status()
        res = r.json().get("data", {}).get("result", [])
        return float(res[0]["value"][1]) if res else None
    except Exception:
        return None


# --- services --------------------------------------------------------------

def _task_rows(service_id):
    rows = []
    try:
        tasks = client().api.tasks(filters={"service": service_id})
    except Exception:
        return rows
    for t in sorted(tasks, key=lambda x: x.get("CreatedAt", ""), reverse=True)[:12]:
        status = t.get("Status", {})
        rows.append({
            "id": t.get("ID", "")[:12],
            "node": t.get("NodeID", "")[:12],
            "state": status.get("State", "unknown"),
            "desired": t.get("DesiredState", ""),
            "since": _age(status.get("Timestamp", "")),
            "error": status.get("Err", ""),
        })
    return rows


def _state_of(running, desired, tasks):
    if desired == 0:
        return "mute", "stopped"
    if running == 0:
        return "bad", "down"
    if running < desired:
        return "warn", "degraded"
    if any(t["state"] in ("starting", "preparing", "pending") for t in tasks):
        return "warn", "updating"
    return "ok", "healthy"


def service(name, with_tasks=True):
    try:
        svc = client().services.get(name)
    except Exception:
        return {"name": name, "exists": False, "tone": "mute", "state": "missing",
                "image": "—", "running": 0, "desired": 0, "tasks": [], "env": {},
                "updated": "—", "mode": "—", "resources": {}, "placement": [],
                "update_config": {}, "networks": []}

    spec = svc.attrs.get("Spec", {})
    task_tpl = spec.get("TaskTemplate", {})
    container = task_tpl.get("ContainerSpec", {})
    mode = spec.get("Mode", {})
    tasks = _task_rows(svc.id) if with_tasks else []
    running = sum(1 for t in tasks if t["state"] == "running")

    if "Replicated" in mode:
        desired = mode["Replicated"].get("Replicas", 0)
        mode_label = "replicated"
    else:
        desired = len([t for t in tasks if t["desired"] == "running"]) or running
        mode_label = "global"

    res = task_tpl.get("Resources", {}) or {}
    limits, reservations = res.get("Limits") or {}, res.get("Reservations") or {}
    upd = spec.get("UpdateConfig", {}) or {}
    tone, state = _state_of(running, desired, tasks)

    env = {}
    for item in container.get("Env", []) or []:
        if "=" in item:
            k, v = item.split("=", 1)
            env[k] = v

    return {
        "name": name,
        "exists": True,
        "tone": tone,
        "state": state,
        "image": container.get("Image", ""),
        "image_short": _short_image(container.get("Image", "")),
        "running": running,
        "desired": desired,
        "mode": mode_label,
        "updated": _age(svc.attrs.get("UpdatedAt", "")),
        "tasks": tasks,
        "env": env,
        "networks": [n.get("Target", "")[:12] for n in task_tpl.get("Networks", []) or []],
        "resources": {
            "cpu_limit": (limits.get("NanoCPUs", 0) or 0) / 1e9 or None,
            "mem_limit": (limits.get("MemoryBytes", 0) or 0) // (1024 * 1024) or None,
            "cpu_res": (reservations.get("NanoCPUs", 0) or 0) / 1e9 or None,
            "mem_res": (reservations.get("MemoryBytes", 0) or 0) // (1024 * 1024) or None,
        },
        "update_config": {
            "parallelism": upd.get("Parallelism"),
            "order": upd.get("Order"),
            "delay": f"{(upd.get('Delay') or 0) // 1_000_000_000}s" if upd.get("Delay") else None,
            "monitor": f"{(upd.get('Monitor') or 0) // 1_000_000_000}s" if upd.get("Monitor") else None,
            "failure_action": upd.get("FailureAction"),
        },
        "placement": (task_tpl.get("Placement", {}) or {}).get("Constraints", []) or [],
    }


def apps():
    out = []
    for entry in catalog.CATALOG:
        envs = {k: service(v, with_tasks=False) for k, v in entry["environments"].items()}
        tones = [e["tone"] for e in envs.values()]
        worst = "bad" if "bad" in tones else "warn" if "warn" in tones else \
                "mute" if all(t == "mute" for t in tones) else "ok"
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


def logs(service_name, lines=200):
    """
    Read service logs through the CLI.

    Not the SDK: APIClient.service_logs returns either bytes or a generator
    depending on arguments and version, and the wrong branch raises rather than
    showing you your logs. The CLI has one behaviour.
    """
    import subprocess
    try:
        proc = subprocess.run(
            ["docker", "service", "logs", "--no-trunc", "--tail", str(lines), service_name],
            capture_output=True, text=True, timeout=30,
        )
        out = (proc.stdout + proc.stderr).strip()
        return out or f"No log output from {service_name} yet."
    except subprocess.TimeoutExpired:
        return f"Timed out reading logs for {service_name}."
    except OSError as exc:
        return f"Could not read logs for {service_name}: {exc}"


# --- cluster ---------------------------------------------------------------

def nodes():
    out = []
    try:
        for n in client().nodes.list():
            attrs, spec = n.attrs, n.attrs.get("Spec", {})
            desc = attrs.get("Description", {})
            resources = desc.get("Resources", {})
            state = attrs.get("Status", {}).get("State", "unknown")
            avail = spec.get("Availability", "")
            tone = "ok" if state == "ready" and avail == "active" else \
                   "warn" if state == "ready" else "bad"
            out.append({
                "id": n.id[:12],
                "hostname": desc.get("Hostname", "—"),
                "role": spec.get("Role", "—"),
                "state": state,
                "availability": avail,
                "tone": tone,
                "cpus": (resources.get("NanoCPUs", 0) or 0) // 1_000_000_000,
                "memory_gb": round((resources.get("MemoryBytes", 0) or 0) / 1024 ** 3, 1),
                "engine": desc.get("Engine", {}).get("EngineVersion", "—"),
                "addr": attrs.get("Status", {}).get("Addr", "—"),
            })
    except Exception:
        pass
    return sorted(out, key=lambda n: (n["role"] != "manager", n["hostname"]))


def summary():
    app_prod = service(catalog.BY_KEY["app"]["environments"]["prod"], with_tasks=False)
    node_list = nodes()
    workers = [n for n in node_list if n["role"] == "worker"]
    p95 = vm_query("autoscaler_app_p95_ms")
    slo = vm_query("autoscaler_slo_p95_ms") or 500.0
    degraded = [a for a in apps() if a["tone"] in ("bad", "warn")]
    return {
        "p95": p95,
        "slo": slo,
        "p95_tone": "mute" if p95 is None else
                    "bad" if p95 > slo else "warn" if p95 > slo * 0.8 else "ok",
        "replicas_running": app_prod["running"],
        "replicas_desired": app_prod["desired"],
        "workers": len(workers),
        "workers_ready": len([n for n in workers if n["tone"] == "ok"]),
        "max_workers": int(vm_query("autoscaler_max_workers") or 0),
        "hosts": int(vm_query("autoscaler_current_hosts") or 0),
        "degraded": degraded,
        "cpu_per_replica": vm_query("autoscaler_cpu_per_replica_percent"),
    }


def autoscaler_state():
    def g(name):
        return vm_query(name)
    return {
        "signals": [
            {"key": "p95 latency", "value": g("autoscaler_app_p95_ms"), "unit": "ms",
             "note": "Primary signal. What users feel."},
            {"key": "CPU per replica", "value": g("autoscaler_cpu_per_replica_percent"), "unit": "%",
             "note": "Secondary. Keeps scale-down working when traffic is near zero."},
            {"key": "Node CPU", "value": g("autoscaler_cluster_cpu_percent"), "unit": "%",
             "note": "Placement guard only, never a trigger. Reads the workers, "
                     "or the master when the fleet is empty."},
            {"key": "Node memory", "value": g("autoscaler_cluster_mem_percent"), "unit": "%",
             "note": "Placement guard only."},
        ],
        # 1 = app pinned to workers (master carries none), 0 = master runs it.
        "worker_mode": g("autoscaler_placement_worker_mode"),
        "manager_capacity": g("autoscaler_manager_replica_capacity"),
        "worker_capacity": g("autoscaler_worker_replica_capacity"),
        "current_replicas": g("autoscaler_current_replicas"),
        "desired_replicas": g("autoscaler_desired_replicas"),
        "max_replicas": g("autoscaler_max_replicas"),
        "current_workers": g("autoscaler_current_hosts"),
        "current_servers": g("autoscaler_current_workers"),
        "desired_workers": g("autoscaler_desired_workers"),
        "max_workers": g("autoscaler_max_workers"),
        "min_workers": g("autoscaler_effective_min_workers"),
        "slo": g("autoscaler_slo_p95_ms"),
        "last_loop": g("autoscaler_last_loop_timestamp_seconds"),
    }


def alerts():
    """Rules and their firing state, straight from vmalert."""
    out = []
    try:
        r = requests.get(f"{VMALERT_URL}/api/v1/rules", timeout=8)
        r.raise_for_status()
        for group in r.json().get("data", {}).get("groups", []):
            for rule in group.get("rules", []):
                state = rule.get("state", "inactive")
                out.append({
                    "name": rule.get("name", "—"),
                    "group": group.get("name", "—"),
                    "state": state,
                    "tone": "bad" if state == "firing" else "warn" if state == "pending" else "ok",
                    "severity": (rule.get("labels") or {}).get("severity", "—"),
                    "summary": (rule.get("annotations") or {}).get("summary", ""),
                })
    except Exception:
        pass
    return out


# --- actions ---------------------------------------------------------------

def redeploy_stack():
    import envstore
    return envstore.deploy()


def restart(service_name):
    """Force a rolling restart without changing the spec."""
    try:
        svc = client().services.get(service_name)
        svc.force_update()
        return True, f"Rolling restart started for {service_name}."
    except Exception as exc:
        return False, f"Could not restart {service_name}: {exc}"


def rollback(service_name):
    """
    Roll a service back to its previous spec.

    `docker service rollback` rather than the SDK: docker-py's update_service
    has no rollback flag, so the previous implementation would have raised
    TypeError the first time anyone pressed the button.
    """
    import subprocess
    try:
        proc = subprocess.run(
            ["docker", "service", "rollback", service_name],
            capture_output=True, text=True, timeout=600,
        )
        out = (proc.stdout + proc.stderr).strip()
        if proc.returncode != 0:
            return False, out or f"Could not roll back {service_name}."
        return True, out or f"Rolled {service_name} back to its previous spec."
    except subprocess.TimeoutExpired:
        return False, f"Timed out rolling back {service_name}."
    except OSError as exc:
        return False, f"Could not run docker: {exc}"


def master_ip():
    """Private IP of the manager, used to build host-published URLs."""
    try:
        addr = client().info().get("Swarm", {}).get("NodeAddr")
        if addr:
            return addr
    except Exception:
        pass
    return os.environ.get("MASTER_PRIVATE_IP", "10.0.0.2")


def deploy_image(service_name, image):
    """
    Point a service at a new image, gracefully.

    Shells out rather than using the SDK so the update inherits exactly the
    contract the README documents to CI:
      --with-registry-auth  ships the GHCR credential with the spec, so a worker
                            created an hour from now can still pull it
      the service's own update_config applies: start-first, monitor 90s,
                            failure_action rollback
      the command waits for convergence and exits non-zero if the update failed
                            or rolled back, so a broken deploy fails loudly
    """
    import subprocess
    try:
        proc = subprocess.run(
            ["docker", "service", "update", "--with-registry-auth",
             "--image", image, service_name],
            capture_output=True, text=True, timeout=600,
        )
        out = (proc.stdout + proc.stderr).strip()
        if proc.returncode != 0:
            return False, out or "docker service update failed."
        return True, out
    except subprocess.TimeoutExpired:
        return False, ("Timed out after 10 minutes. Swarm may still be converging; "
                       "check `docker service ps " + service_name + "`.")
    except OSError as exc:
        return False, f"Could not run docker: {exc}"


def port_is_open(port):
    import hostops
    return hostops.port_is_open(port)


def registry_logins():
    import registry
    return registry.logins()
