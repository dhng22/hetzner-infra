"""
Shapes shared by the live data module and the preview fixtures.

`swarm.py` and `fixtures.py` are swapped for each other at import time, so
anything computed in one and not the other is a drift waiting to happen — the
preview showed an all-missing component as healthy for exactly that reason, and
`history()` existed only in the fixtures, so the live panel 500'd on the page
you would use most.

Anything that is pure derivation from a service dict belongs here, taking the
`service()` lookup as an argument. Only the part that actually talks to Docker
stays split.
"""


#: How bad a tone is. Used to detect a component whose overall colour is worse
#: than its primary service's label admits.
_RANK = {"ok": 0, "mute": 0, "warn": 1, "bad": 2}


#: Managed spec field -> where its LIVE value actually lives.
#:
#: The spec's copy is a seed, not the truth. The autoscaler re-sizes
#: reservations on the running service and CI moves the image, so a panel that
#: reads the file shows whatever was last written there — which for a
#: right-sized component was 0.36 CPU against a service actually reserving 0.02.
#: A section headed "managed for you" showing the one number nobody manages is
#: worse than showing nothing.
_LIVE_OF = {
    "cpu_reservation":      lambda s: s["resources"].get("cpu_res"),
    "memory_reservation_mb": lambda s: s["resources"].get("mem_res"),
    "cpu_limit":            lambda s: s["resources"].get("cpu_limit"),
    "memory_limit_mb":      lambda s: s["resources"].get("mem_limit"),
    "replicas":             lambda s: s.get("desired"),
    "image":                lambda s: s.get("image_short"),
}


def managed_values(component, primary):
    """
    {field name: value as it IS} for every managed field.

    Falls back to the spec when the service does not exist yet — before a first
    deploy the file genuinely is the only thing that knows.
    """
    out = {}
    for field in type(component).fields():
        if not getattr(field, "managed", None):
            continue
        value = None
        if primary and primary.get("exists"):
            reader = _LIVE_OF.get(field.name)
            if reader:
                try:
                    value = reader(primary)
                except Exception:  # noqa: BLE001
                    value = None
        if value in (None, ""):
            value = component.spec.get(field.name)
        out[field.name] = value
    return out


def component_reserved(services):
    """
    What this component has promised across ALL of its services and replicas.

    The map on the Overview shows one chip per task, so the number on it is per
    REPLICA. That answers "how much of this node is this one task holding" and
    not "what is this component costing me", which is replicas x reservation
    summed over the app, its database and any sidecar.
    """
    cpu = mem = 0.0
    for svc in services:
        if not svc.get("exists"):
            continue
        count = max(svc.get("desired") or 0, 0)
        res = svc.get("resources") or {}
        cpu += (res.get("cpu_res") or 0) * count
        mem += (res.get("mem_res") or 0) * count
    return {"cpu": round(cpu, 3), "mem_mb": int(mem)}


def component_view(component, service_fn):
    """
    A component's live state, merged with its spec.

    The Component object stays the source of truth for what it IS; this adds
    what it is DOING. Templates get both, so nothing here needs to know the
    difference between an application and a database.
    """
    services = [service_fn(name, with_tasks=False) for name in component.services()]
    primary = services[0] if services else None
    tones = [s["tone"] for s in services]
    if "bad" in tones:
        worst = "bad"
    elif "warn" in tones:
        worst = "warn"
    elif tones and all(t == "mute" for t in tones):
        worst = "mute"
    else:
        worst = "ok"

    # A component whose services do not exist yet is not "healthy", it is not
    # deployed. This distinction used to be folded to "ok" in one function and
    # "mute" in another, so the same component read differently on two pages.
    if primary is not None and not primary["exists"]:
        worst, state = "mute", "not deployed"
    else:
        state = primary["state"] if primary else "unknown"
        # The colour comes from the WORST service, the label from the primary
        # one. When they disagree the pill contradicts itself — a red "healthy",
        # which is what a Redis whose exporter cannot be placed rendered as.
        # The label has to answer for the whole component, not just its head.
        if primary is not None and _RANK.get(worst, 0) > _RANK.get(primary["tone"], 0):
            state = "degraded"

    return {
        "name": component.name,
        "type": component.TYPE,
        "label": component.LABEL,
        "category": component.CATEGORY,
        "blurb": component.BLURB,
        "created_at": component.created_at,
        "summary": component.summary(),
        "access": component.access(),
        "services": services,
        "primary": primary,
        "tone": worst,
        "state": state,
        "managed": managed_values(component, primary),
        "reserved": component_reserved(services),
        "running": primary["running"] if primary else 0,
        "desired": primary["desired"] if primary else 0,
    }


def broken_view(name, problem):
    """A component whose spec will not parse. Listed, not hidden."""
    return {
        "name": name, "type": "?", "label": "Unreadable", "category": "Application",
        "blurb": problem, "created_at": None, "summary": problem, "access": None,
        "services": [], "primary": None, "tone": "bad", "state": "broken spec",
        "running": 0, "desired": 0,
        # Same keys as a healthy view: a template that reaches for these must
        # not have to know it is looking at a broken component.
        "managed": {}, "reserved": {"cpu": 0, "mem_mb": 0},
    }


def with_cluster_share(views, nodes):
    """
    Add each component's reservation as a percentage of total cluster capacity.

    Of the WHOLE cluster, not of one node: a component's replicas are spread
    across machines, so "12% of the cluster" is the honest answer and "12% of a
    node" would be true of no node in particular.
    """
    total_cpu = sum((n.get("cpus") or 0) for n in nodes)
    total_mem = sum((n.get("memory_gb") or 0) for n in nodes) * 1024
    for view in views:
        reserved = view.get("reserved") or {}
        view["reserved"] = {
            **reserved,
            "cpu_pct": round((reserved.get("cpu") or 0) / total_cpu * 100, 1) if total_cpu else 0,
            "mem_pct": round((reserved.get("mem_mb") or 0) / total_mem * 100, 1) if total_mem else 0,
        }
    return views


def component_views(service_fn):
    """Every component on disk. One broken spec does not hide the others."""
    try:
        import components
    except Exception:
        return []
    found, problems = components.all_components()
    return ([component_view(c, service_fn) for c in found]
            + [broken_view(name, problem) for name, problem in problems])


def summary(service_fn, nodes, vm_query):
    """
    The overview tiles.

    Cluster-wide, because there is no longer a distinguished application to be
    "the" p95 — that number is per component now, and the honest cluster-level
    version is how many components are unhealthy and how many are breaching.
    """
    workers = [n for n in nodes if n["role"] == "worker"]
    views = component_views(service_fn)
    return {
        "components": len(views),
        "components_ok": len([v for v in views if v["tone"] == "ok"]),
        "degraded": [v for v in views if v["tone"] in ("bad", "warn")],
        "undeployed": [v for v in views if v["state"] == "not deployed"],
        "slo_breaching": int(vm_query(
            "count(autoscaler_service_p95_ms > on (service) "
            "autoscaler_service_slo_p95_ms)") or 0),
        "replicas_running": sum(v["running"] for v in views),
        "replicas_desired": sum(v["desired"] for v in views),
        "workers": len(workers),
        "workers_ready": len([n for n in workers if n["tone"] == "ok"]),
        "max_workers": int(vm_query("autoscaler_max_workers") or 0),
        "hosts": int(vm_query("autoscaler_current_hosts") or 0),
        "min_workers": int(vm_query("autoscaler_effective_min_workers") or 0),
        "cluster_cpu": vm_query("autoscaler_cluster_cpu_percent"),
        "cluster_mem": vm_query("autoscaler_cluster_mem_percent"),
    }
