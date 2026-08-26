"""
Finding out what a service publishes, so nobody has to be told.

Both discoveries are METADATA queries against /series, cached for fifteen
minutes: the answer changes when somebody ships a new library, not between
loops. Neither returns a signal — they return the expression to read one with.
"""

import logging
import time

from .classify import CAUSE_DATABASE, CAUSE_UPSTREAM
from .expressions import mean_expr, p95_expr, unit_of
from .query import vm_series_rows

log = logging.getLogger("signals")

#: Name fragments that make a metric family look like HTTP SERVER latency, best
#: first. Discovery ranks candidates by the first fragment they contain, so an
#: app publishing both client and server timers picks the server one.
LATENCY_HINTS = ("http_server_requests", "http_server_request",
                 "http_server_duration", "http_request_duration",
                 "http_requests_duration", "request_duration",
                 "requests_seconds", "request_seconds")

#: What a metric family name says about WHO is slow. Ordered: the first family a
#: name matches wins, so a driver-specific timer beats a generic client one.
#: Micrometer, OpenTelemetry and the Prometheus client libraries all converge on
#: these, and an app that publishes none simply yields `unknown` — an honest
#: answer rather than a wrong one.
DEPENDENCY_HINTS = (
    (CAUSE_DATABASE, ("mongodb_driver_commands", "mongodb_driver_command",
                      "hikaricp_connections_acquire", "jdbc_connections",
                      "db_client_operation", "db_client_commands",
                      "redis_commands_duration", "sql_client_processing")),
    (CAUSE_UPSTREAM, ("http_client_requests", "http_client_request",
                      "http_client_duration", "ktor_http_client",
                      "rpc_client_duration", "grpc_client_handled")),
)

#: Labels that name WHICH dependency, best first. Whichever one the series
#: actually carries becomes the target, so `upstream:tikdrama` can be muted
#: without muting every outbound call the service makes.
TARGET_LABELS = ("host", "server_address", "net_peer_name", "client_name",
                 "target", "pool", "database", "uri")

TTL_SECONDS = 900


def rank_latency(name):
    """Lower is better. None means it does not look like request latency."""
    lowered = name.lower()
    for i, hint in enumerate(LATENCY_HINTS):
        if hint in lowered:
            return i
    return None


class _Cache:
    def __init__(self):
        self.value = {}
        self.at = 0.0

    def fresh(self):
        return self.value and time.time() - self.at < TTL_SECONDS

    def store(self, value):
        self.value, self.at = value, time.time()
        return value


_latency = _Cache()
_dependencies = _Cache()


def discover_latency(service_names, on_missing=None):
    """
    {service: (expr, kind, base)} for services whose latency metric we can find.

    This exists so a component does not have to be told the name of its own
    latency metric. The old default was `http_server_requests_seconds_bucket`, a
    Spring convention, and a Ktor app publishing
    `ktor_http_server_requests_seconds` matched nothing — so p95 read n/a
    forever and only CPU could ever move the replica count. Nothing warned,
    because an empty histogram_quantile is also what an idle service looks like.
    """
    if _latency.fresh():
        return {k: v for k, v in _latency.value.items() if v}

    wanted = set(service_names)
    found = {}
    # Real histograms first — a true p95 always beats a mean.
    for suffix, kind in (("_bucket", "p95"), ("_count", "mean")):
        for metric, svc, _labels in vm_series_rows(f'{{__name__=~".+{suffix}", service!=""}}'):
            if svc not in wanted:
                continue
            base = metric[: -len(suffix)]
            rank = rank_latency(base)
            if rank is None:
                continue
            best = found.get(svc)
            if best and best[0] <= rank:
                continue
            unit = unit_of(base)
            expr = (p95_expr(f"{base}_bucket", unit) if kind == "p95"
                    else mean_expr(base, unit))
            found[svc] = (rank, expr, kind, base)

    out = {}
    for svc in wanted:
        hit = found.get(svc)
        out[svc] = (hit[1], hit[2], hit[3]) if hit else None
        if hit:
            log.info("%s: latency signal is %s from %s", svc, hit[2], hit[3])
        elif on_missing:
            on_missing(svc)
    _latency.store(out)
    return {k: v for k, v in out.items() if v}


def discover_dependencies(service_names):
    """
    {service: [(cause, expr, base, target_label)]} — the outbound timers a
    service publishes, which is the only evidence in the cluster about what it
    is WAITING ON.
    """
    if _dependencies.fresh():
        return {k: v for k, v in _dependencies.value.items() if v}

    wanted = set(service_names)
    found = {}
    for metric, svc, labels in vm_series_rows('{__name__=~".+_count", service!=""}'):
        if svc not in wanted:
            continue
        base = metric[: -len("_count")]
        for cause, hints in DEPENDENCY_HINTS:
            if not any(h in base for h in hints):
                continue
            target = next((lab for lab in TARGET_LABELS if labels.get(lab)), None)
            entry = (cause, mean_expr(base, unit_of(base)), base, target)
            if entry not in found.setdefault(svc, []):
                found[svc].append(entry)
            break

    out = {svc: found.get(svc, []) for svc in wanted}
    for svc, entries in out.items():
        for cause, _expr, base, target in entries:
            log.info("%s: %s dependency timer %s%s", svc, cause, base,
                     f" (target label {target})" if target else "")
    _dependencies.store(out)
    return {k: v for k, v in out.items() if v}


def reset_caches():
    """For tests, and for a process that wants a fresh look after a deploy."""
    _latency.value, _latency.at = {}, 0.0
    _dependencies.value, _dependencies.at = {}, 0.0
