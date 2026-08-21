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
    }


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
        "cluster_cpu": vm_query("autoscaler_cluster_cpu_percent"),
        "cluster_mem": vm_query("autoscaler_cluster_mem_percent"),
    }
