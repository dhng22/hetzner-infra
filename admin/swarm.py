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
import shape

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


def vm_query_by(expr, label="instance"):
    """Instant query returning {label_value: float} instead of a single number."""
    out = {}
    try:
        r = requests.get(f"{VM_URL}/api/v1/query", params={"query": expr}, timeout=8)
        r.raise_for_status()
        for row in r.json().get("data", {}).get("result", []):
            key = row.get("metric", {}).get(label)
            if key is None:
                continue
            try:
                out[key] = float(row["value"][1])
            except (KeyError, IndexError, ValueError):
                continue
    except Exception:
        pass
    return out


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


def component_view(component):
    return shape.component_view(component, service)


def system_view():
    """The three infrastructure stacks, read-only, grouped by category."""
    grouped = {}
    for entry in catalog.SYSTEM:
        svc = service(entry["service"], with_tasks=False)
        grouped.setdefault(entry["category"], []).append({**entry, "svc": svc,
                                                          "tone": svc["tone"]})
    return [(c, grouped[c]) for c in catalog.CATEGORIES if c in grouped]


def history(name=None, limit=25):
    """
    Deployment history. Lives in state.py; exposed here because the panel talks
    to exactly one data module and swaps it wholesale in preview mode.

    This function did not exist, while the app detail route called it on every
    request — so the live panel returned a 500 on the page you would use most.
    """
    import state
    return state.history(name, limit=limit)


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
                # Tasks reference the untruncated node id; topology() joins on it.
                "full_id": n.id,
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


# Which colour band a service belongs to on the topology chart.
#
# Derived from the stack the service belongs to, never enumerated. The previous
# version was a hard-coded prefix table listing `_api-prod`, `_redis` and eight
# `monitoring_*` names — a second taxonomy that had to be kept in step with the
# catalog by hand, and silently mis-coloured anything new.
#
# Four categorical hues plus a neutral tail, on purpose. The panel already
# reserves green/amber/red for status and teal for interaction, so a categorical
# ramp has to dodge all four; four hues is what survives the CVD checks against
# both surfaces. Everything past them folds into "platform" neutral grey rather
# than inventing a fifth hue nobody can distinguish.
_SYSTEM_BANDS = {
    "monitoring": ("observability", "observe"),
    "ingress": ("ingress", "staging"),
    "admin": ("platform", "platform"),
}
_TYPE_BANDS = {
    "app": ("applications", "prod"),
    "redis": ("data", "data"),
}
PLATFORM_BAND = ("platform", "platform")

#: band label -> colour key, for the legend. One dict so the chart and the
#: legend cannot disagree about which hue means what.
_BAND_KEYS = dict([*_SYSTEM_BANDS.values(), *_TYPE_BANDS.values(), PLATFORM_BAND])


def _band_of(service_name, component_types=None):
    """
    (label, colour key) for a Swarm service name.

    `component_types` maps a stack name to a component type, so a component's
    band follows what it IS rather than what it is called.
    """
    stack = service_name.split("_", 1)[0] if "_" in service_name else ""
    if stack in _SYSTEM_BANDS:
        return _SYSTEM_BANDS[stack]
    kind = (component_types or {}).get(stack)
    if kind in _TYPE_BANDS:
        return _TYPE_BANDS[kind]
    return PLATFORM_BAND


def _component_types():
    """{stack name: component type}, read from the specs on disk."""
    try:
        import components
        found, _ = components.all_components()
        return {c.name: c.TYPE for c in found}
    except Exception:
        return {}


def short_service(name):
    """
    `api_app` -> `api`, `monitoring_grafana` -> `grafana`.

    A component's stack IS its name, so `api_app` reads better as `api`; for the
    infrastructure stacks the stack prefix is the noise instead. Both are just
    "drop the redundant half".
    """
    stack, _, key = name.partition("_")
    if not key:
        return name
    if stack in _SYSTEM_BANDS:
        return key
    return stack if key in ("app", "redis") else f"{stack}·{key}"


def topology():
    """
    Per-node composition: every RUNNING task on every box, named individually.

    One entry per task, not per service — three replicas of api-prod are three
    entries, because "how many of this are on that node" is the whole question
    the view answers. Counts are of running tasks rather than desired replicas
    on purpose: "desired 6, running 4" is exactly the state worth seeing.
    """
    # Applications first, then data, then the infrastructure that carries them —
    # the order you read a node in when you are asking "what is on this box".
    band_order = ["applications", "data", "ingress", "observability", "platform"]
    band_rank = {b: i for i, b in enumerate(band_order)}
    types = _component_types()

    try:
        svc_names = {s.id: s.name for s in client().services.list()}
        tasks = client().api.tasks(filters={"desired-state": "running"})
    except Exception:
        svc_names, tasks = {}, []

    by_node = {}
    for t in tasks:
        if t.get("Status", {}).get("State") != "running":
            continue
        node_id = t.get("NodeID")
        if not node_id:
            continue
        full = svc_names.get(t.get("ServiceID"), "unknown")
        band, key = _band_of(full, types)
        by_node.setdefault(node_id, []).append({
            "id": (t.get("ID") or "")[:12],
            "name": short_service(full),
            "service": full,
            "band": band,
            "key": key,
        })

    cpu = vm_query_by('100 - (avg by (instance) '
                      '(rate(node_cpu_seconds_total{mode="idle"}[2m])) * 100)')
    mem = vm_query_by('100 * (1 - node_memory_MemAvailable_bytes '
                      '/ node_memory_MemTotal_bytes)')

    out = []
    for n in nodes():
        items = by_node.get(n["full_id"], [])
        # Grouped by band, then by name, so replicas of one service sit together
        # and the bands read as blocks without needing colour to do the work.
        items.sort(key=lambda x: (band_rank.get(x["band"], 99), x["name"], x["id"]))
        counts = {}
        for it in items:
            counts[it["name"]] = counts.get(it["name"], 0) + 1
        out.append({
            **n,
            "tasks_total": len(items),
            "tasks": items,
            "by_service": [{"name": k, "count": v} for k, v in sorted(counts.items())],
            "cpu_pct": cpu.get(n["hostname"]),
            "mem_pct": mem.get(n["hostname"]),
        })
    return {
        "nodes": out,
        "bands": [{"band": b, "key": _BAND_KEYS[b]} for b in band_order],
        "max_tasks": max([n["tasks_total"] for n in out], default=0),
    }


def summary():
    return shape.summary(service, nodes(), vm_query)


def component_views():
    return shape.component_views(service)


def autoscaler_state():
    """
    What the autoscaler is thinking, cluster-wide and per component.

    Cluster gauges stay unlabeled and per-service gauges carry a `service`
    label — the same split the alert rules depend on, so reading them the same
    way here keeps the panel and the alerts describing one reality.
    """
    def g(name):
        return vm_query(name)

    def by_service(name):
        return vm_query_by(name, label="service")

    p95 = by_service("autoscaler_service_p95_ms")
    slo = by_service("autoscaler_service_slo_p95_ms")
    current = by_service("autoscaler_service_current_replicas")
    desired = by_service("autoscaler_service_desired_replicas")
    admitted = by_service("autoscaler_service_admitted_replicas")
    running = by_service("autoscaler_service_running_replicas")
    pending = by_service("autoscaler_service_pending_replicas")
    lo = by_service("autoscaler_service_min_replicas")
    hi = by_service("autoscaler_service_max_replicas")
    cpu = by_service("autoscaler_service_cpu_per_replica_percent")
    pinned = by_service("autoscaler_service_worker_mode")

    services = []
    for name in sorted(set().union(current, running, p95, admitted)):
        want, got = desired.get(name), admitted.get(name)
        services.append({
            "service": name,
            "component": name.split("_", 1)[0],
            "p95": p95.get(name),
            "slo": slo.get(name),
            "breaching": (p95.get(name) is not None and slo.get(name) is not None
                          and p95[name] > slo[name]),
            "current": current.get(name),
            "desired": want,
            "admitted": got,
            # want > admitted means the cluster could not place what the signals
            # asked for. That is the number to look at when scaling "did nothing".
            "capped": want is not None and got is not None and want > got,
            "running": running.get(name),
            "pending": pending.get(name),
            "min": lo.get(name),
            "max": hi.get(name),
            "cpu_per_replica": cpu.get(name),
            "worker_pinned": pinned.get(name),
        })

    return {
        "services": services,
        "signals": [
            {"key": "Node CPU", "value": g("autoscaler_cluster_cpu_percent"), "unit": "%",
             "note": "Placement guard only, never a trigger. Reads the workers, "
                     "or the master when the fleet is empty."},
            {"key": "Node memory", "value": g("autoscaler_cluster_mem_percent"), "unit": "%",
             "note": "Placement guard only."},
            {"key": "Demand", "value": g("autoscaler_demand_cpu_cores"), "unit": "cores",
             "note": "Reservations of every application replica that has to be placed."},
            {"key": "Free", "value": g("autoscaler_worker_pool_free_cpu_cores"), "unit": "cores",
             "note": "What the eligible nodes have left after everything else on them."},
        ],
        # 1 = at least one application is pinned to the workers.
        "worker_mode": g("autoscaler_placement_worker_mode"),
        "mixed_placement": g("autoscaler_placement_mixed"),
        "managed": g("autoscaler_managed_services"),
        "demand_cpu": g("autoscaler_demand_cpu_cores"),
        "demand_mem": g("autoscaler_demand_memory_bytes"),
        "manager_free_cpu": g("autoscaler_manager_free_cpu_cores"),
        "manager_free_mem": g("autoscaler_manager_free_memory_bytes"),
        "worker_free_cpu": g("autoscaler_worker_pool_free_cpu_cores"),
        "new_worker_cpu": g("autoscaler_new_worker_free_cpu_cores"),
        # Workers are Hetzner servers; the master is not one. `hosts` is kept
        # only as "boxes in the swarm", and nothing keys a threshold on it.
        "current_workers": g("autoscaler_current_workers"),
        "hosts": g("autoscaler_current_hosts"),
        "desired_workers": g("autoscaler_desired_workers"),
        "max_workers": g("autoscaler_max_workers"),
        "min_workers": g("autoscaler_effective_min_workers"),
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

def deploy_system_stack(name):
    """
    Redeploy one of the three infrastructure stacks.

    Components do NOT come through here — each one deploys itself, reading its
    live image and replica count back first. bin/stack-deploy refuses anything
    that is not monitoring, ingress or admin for the same reason.
    """
    import envstore
    return envstore.deploy_stack(name)


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
