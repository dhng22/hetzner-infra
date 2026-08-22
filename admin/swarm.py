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


#: A task on its way up. Swarm walks through these in order, and none of them
#: means anything is wrong.
STARTING_STATES = ("new", "allocated", "assigned", "accepted", "preparing",
                   "ready", "starting")
#: A task Swarm accepted but cannot place — no node has the resources, or none
#: satisfies its constraints. It will sit here indefinitely. Reporting it as
#: "down" hides the one fact that explains it and suggests the wrong fix.
BLOCKED_STATES = ("pending",)


def _task_counts():
    """
    {service_id: {running, starting, blocked}}, for every service, in ONE call.

    These are properties of the tasks, never of the service spec, so they
    cannot be read from the service alone. That is why callers asking for
    services WITHOUT their tasks used to report every one of them as down: the
    counts were derived from a task list they had deliberately not fetched, so
    they were always 0 — replicated services rendered "down", global ones
    rendered "0/0 stopped", and no page could show a healthy cluster at all.

    Fetching each service's tasks instead would be one round trip per row on
    the heaviest page in the panel. This is one for all of them.

    Counted by SLOT, not by task. `order: start-first` runs the replacement
    task alongside the one it replaces, so a service mid-rollout legitimately
    has two tasks per slot — and a rollout that wedges keeps them. Counting
    tasks reported `3/2 replicas` on a two-replica service, which then passed
    the `running < desired` check and rendered as healthy while every container
    was crash-looping. A slot is the thing a replica actually is. Global
    services have no slot, so their unit is the node.
    """
    slots = {}
    try:
        tasks = client().api.tasks(filters={"desired-state": "running"})
    except Exception:
        return {}
    for t in tasks:
        sid = t.get("ServiceID")
        if not sid:
            continue
        row = slots.setdefault(sid, {"running": set(), "starting": set(),
                                     "blocked": set()})
        # Slot is 1-based for replicated services and absent (0) for global.
        unit = t.get("Slot") or t.get("NodeID")
        if unit is None:
            continue
        state = (t.get("Status") or {}).get("State")
        if state == "running":
            row["running"].add(unit)
        elif state in STARTING_STATES:
            row["starting"].add(unit)
        elif state in BLOCKED_STATES:
            row["blocked"].add(unit)

    counts = {}
    for sid, row in slots.items():
        running = row["running"]
        counts[sid] = {
            "running": len(running),
            # Only slots with NOTHING running yet are still coming up. A slot
            # that has a running task and a starting one is being replaced,
            # which is `in_flight` below, not missing capacity.
            "starting": len(row["starting"] - running),
            "blocked": len(row["blocked"] - running),
            "in_flight": len((row["starting"] | row["blocked"]) & running),
        }
    return counts


def _state_of(running, desired, tasks, starting=0, blocked=0, in_flight=0):
    """
    (tone, label) for a service.

    `starting`, `blocked` and `in_flight` are counted separately from the task
    rows because the cheap path does not fetch rows at all. Without them a
    component being deployed reads "down" until the moment it reads "healthy",
    with nothing in between — so a normal rollout is indistinguishable from an
    outage for as long as it takes to pull the image.
    """
    if not starting:
        starting = sum(1 for t in tasks if t["state"] in STARTING_STATES)
    if not blocked:
        blocked = sum(1 for t in tasks if t["state"] in BLOCKED_STATES)

    if desired == 0:
        return "mute", "stopped"
    if running == 0:
        if starting:
            return "warn", "deploying"
        # Distinct from "down" on purpose: nothing is crashing, Swarm simply has
        # nowhere to put it. `docker service ps <name>` names the reason.
        if blocked:
            return "bad", "unschedulable"
        return "bad", "down"
    if running < desired:
        if blocked:
            return "bad", "unschedulable"
        return "warn", "deploying" if starting else "degraded"
    # Every slot is up, but some are being replaced. Never "healthy": this is
    # also what a wedged rollout looks like, and calling it healthy is how a
    # service whose new tasks keep dying reported green.
    if in_flight or starting:
        return "warn", "updating"
    return "ok", "healthy"


def service(name, with_tasks=True, running_counts=None):
    """
    `running_counts` is the map from _task_counts(), and is how a caller that
    skips `with_tasks` still gets truthful counts. Callers that render many
    services pass it once; anything else can leave it None.
    """
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
    starting = blocked = in_flight = 0
    # Always slot-counted, even on the with_tasks path: `docker service ls`
    # itself will say 3/2 during a start-first rollout, and a panel repeating
    # that is telling you a service has more replicas than it was asked for.
    counts = running_counts if running_counts is not None else _task_counts()
    row = counts.get(svc.id) or {}
    running = row.get("running", 0)
    starting = row.get("starting", 0)
    blocked = row.get("blocked", 0)
    in_flight = row.get("in_flight", 0)

    if "Replicated" in mode:
        desired = mode["Replicated"].get("Replicas", 0)
        mode_label = "replicated"
    else:
        desired = (len([t for t in tasks if t["desired"] == "running"])
                   or (running + starting + blocked))
        mode_label = "global"

    res = task_tpl.get("Resources", {}) or {}
    limits, reservations = res.get("Limits") or {}, res.get("Reservations") or {}
    upd = spec.get("UpdateConfig", {}) or {}
    tone, state = _state_of(running, desired, tasks, starting, blocked, in_flight)

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


def _service_fn_with_counts():
    """
    `service` with one task query pre-bound, for callers that shape several
    services at once.

    Without this each service does its own full task listing, so rendering the
    components page cost two API round trips per component and grew with the
    cluster. The counts are a single snapshot, which is also more correct: every
    row on a page then describes the same instant.
    """
    counts = _task_counts()

    def fn(name, with_tasks=True, running_counts=None):
        return service(name, with_tasks=with_tasks,
                       running_counts=running_counts or counts)
    return fn


def component_view(component):
    return shape.component_view(component, _service_fn_with_counts())


def system_view():
    """The three infrastructure stacks, read-only, grouped by category."""
    grouped = {}
    counts = _task_counts()
    for entry in catalog.SYSTEM:
        svc = service(entry["service"], with_tasks=False, running_counts=counts)
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


LOKI_URL = os.environ.get("LOKI_URL", "http://loki:3100")

#: How far back the Logs tab looks. A crash-looping container writes its whole
#: life in a few seconds and then goes quiet, so a short window shows an empty
#: page for the exact service you opened the tab to debug.
LOG_WINDOW_SECONDS = 24 * 3600


#: Swarm's UpdateStatus.State -> what it means for the deploy that caused it.
#: `paused` is a failure: it is where a rollout stops when it breached its
#: failure threshold and has no rollback configured. None means "still running".
_UPDATE_VERDICT = {
    "updating": None,
    "completed": "done",
    "paused": "failed",
    "rollback_started": "failed",
    "rollback_paused": "failed",
    "rollback_completed": "failed",
}


def update_status(service_name):
    """
    Swarm's own account of the service's last rollout.

    This is the only honest source for "did my deploy work". The deploy command
    cannot answer it — `docker stack deploy` returns before the tasks even
    start — and the component's files on disk always show the new spec whether
    or not it survived. UpdateStatus is what Swarm actually did.
    """
    blank = {"state": "", "verdict": None, "started_epoch": None,
             "message": "", "at": "—"}
    try:
        svc = client().services.get(service_name)
    except Exception:  # noqa: BLE001
        return blank

    status = svc.attrs.get("UpdateStatus") or {}
    state = status.get("State") or ""

    if not state:
        # Swarm writes UpdateStatus only when it UPDATES a service. A service
        # that has just been CREATED has no such key at all — so relying on it
        # alone left every first deploy of every component sitting on
        # "deploying" forever, which is what this whole path was added to stop.
        #
        # Convergence is the honest substitute: the deploy did what it was asked
        # to when every slot Swarm was asked for is running and none is being
        # replaced. It stays unresolved rather than claiming success early.
        counts = _task_counts().get(svc.id) or {}
        mode = svc.attrs.get("Spec", {}).get("Mode", {})
        if "Replicated" in mode:
            want = mode["Replicated"].get("Replicas", 0)
        else:
            want = (counts.get("running", 0) + counts.get("starting", 0)
                    + counts.get("blocked", 0))
        converged = (want > 0
                     and counts.get("running", 0) >= want
                     and not counts.get("starting", 0)
                     and not counts.get("in_flight", 0))
        return {
            "state": "converged" if converged else "converging",
            "verdict": "done" if converged else None,
            "started_epoch": None,
            "message": "" if converged else
                       (f"{counts.get('running', 0)}/{want} running"
                        + (", unschedulable" if counts.get("blocked") else "")),
            "at": _age(svc.attrs.get("UpdatedAt", "")),
        }

    started = status.get("StartedAt") or ""
    epoch = None
    if started:
        try:
            epoch = _dt.datetime.fromisoformat(
                started.replace("Z", "+00:00").split(".")[0] + "+00:00").timestamp()
        except ValueError:
            epoch = None
    return {
        "state": state,
        "verdict": _UPDATE_VERDICT.get(state),
        "started_epoch": epoch,
        "message": status.get("Message") or "",
        "at": _age(status.get("CompletedAt") or started),
    }


def deployments(name, service_name, limit=25):
    """
    (history, live rollout status) for one component, reconciled first.

    Reconciling on read rather than on a timer is deliberate: the panel has one
    worker and no scheduler, and the only moment the answer matters is when
    somebody is looking at the page.
    """
    import state
    live = update_status(service_name)
    if live["verdict"]:
        state.reconcile(name, live["verdict"], live["started_epoch"])
    else:
        # Still converging. A deploy that never converges is a failure too, and
        # without this it would read "deploying" for the rest of the cluster's
        # life — a component that cannot be placed at all is the common case.
        state.expire_pending(name)
    return state.history(name, limit=limit), live


def _loki_logs(service_name, lines):
    """
    Lines from Loki, or None if Loki could not answer.

    None and "" mean different things and the caller depends on it: None is
    "ask the CLI instead", "" is "Loki answered and this service has said
    nothing". Collapsing them would hide a broken Loki behind an empty page.
    """
    now = _dt.datetime.now(_dt.timezone.utc).timestamp()
    try:
        resp = requests.get(
            f"{LOKI_URL}/loki/api/v1/query_range",
            params={
                "query": '{swarm_service="%s"}' % service_name.replace('"', ''),
                "start": int((now - LOG_WINDOW_SECONDS) * 1e9),
                "end": int(now * 1e9),
                "limit": lines,
                "direction": "backward",
            },
            timeout=15,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception:  # noqa: BLE001 — any failure means "fall back to the CLI"
        return None

    rows = []
    for stream in (payload.get("data") or {}).get("result") or []:
        for ts, line in stream.get("values") or []:
            rows.append((int(ts), line.rstrip("\n")))
    rows.sort()
    out = []
    for ts, line in rows:
        stamp = _dt.datetime.fromtimestamp(ts / 1e9, _dt.timezone.utc)
        out.append(f"{stamp:%H:%M:%S} {line}")
    return "\n".join(out)


def logs(service_name, lines=200):
    """
    Read a service's logs from Loki, falling back to the CLI.

    NOT `docker service logs` first. Every stack here sets the `loki` log
    driver, and Docker cannot read back from a non-local driver — so the CLI
    returns absolutely nothing for every component in the cluster, and this tab
    rendered "No log output yet" no matter how much the container was screaming.
    The logs were never missing; the panel was asking the one component that
    does not have them.

    The CLI fallback stays for anything that somehow logs locally, and so that
    a Loki outage degrades to "no logs" rather than to a stack trace.
    """
    from_loki = _loki_logs(service_name, lines)
    if from_loki:
        return from_loki

    import subprocess
    try:
        proc = subprocess.run(
            ["docker", "service", "logs", "--no-trunc", "--tail", str(lines), service_name],
            capture_output=True, text=True, timeout=30,
        )
        out = (proc.stdout + proc.stderr).strip()
    except subprocess.TimeoutExpired:
        return f"Timed out reading logs for {service_name}."
    except OSError as exc:
        return f"Could not read logs for {service_name}: {exc}"

    if out:
        return out
    if from_loki is None:
        return (f"Could not reach Loki at {LOKI_URL}, and the Docker CLI has no "
                f"local logs for {service_name}. Check `docker service ls | grep loki`.")
    return f"No log output from {service_name} in the last 24h."


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
    # summary() shapes every component too, so it gets the same single snapshot.
    return shape.summary(_service_fn_with_counts(), nodes(), vm_query)


def component_views():
    return shape.component_views(_service_fn_with_counts())


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


def alert_destination():
    """
    Whether alerts have anywhere to go, read from the rendered config.

    Bootstrap no longer refuses to boot without one, so something has to say
    when the last hop is missing — otherwise the pipeline looks perfect right up
    until the moment you needed it, which is the failure this path was rebuilt
    to remove.
    """
    path = os.path.join(os.environ.get("INFRA_DIR", "/opt/infra"),
                        "config", "alertmanager.yml")
    try:
        with open(path) as fh:
            body = fh.read()
    except OSError:
        return {"configured": False, "kind": "unreadable"}
    for marker, kind in (("telegram_configs", "Telegram"),
                         ("slack_configs", "Slack"),
                         ("webhook_configs", "a webhook")):
        if marker in body:
            return {"configured": True, "kind": kind}
    return {"configured": False, "kind": "none"}


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
