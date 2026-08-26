#!/usr/bin/env python3
"""
The performance dispatcher: works out WHY a service is slow, and hands the
answer to whoever owns it.

WHY THIS IS NOT IN THE AUTOSCALER
---------------------------------
It used to be, and that was wrong on three counts.

The autoscaler's job is capacity. Deciding that MongoDB is throttling an API is
not a capacity decision, and putting it there meant a process holding a Hetzner
token — one that creates and deletes servers — also had to know what a MongoDB
driver timer looks like. This process holds no token, mounts the docker socket
read-only and changes nothing.

Diagnosis is also the slowest work there is, and it runs precisely when things
are already going wrong. In the autoscaler it competed for a 60-second loop
budget with `AutoscalerStalled` firing at 300s.

And the answer has more than one consumer. A future dbmanager needs to know a
database is the bottleneck; if that lived in the autoscaler, dbmanager would
either re-derive it — two implementations that will disagree — or take a
dependency on the autoscaler, which is the wrong direction entirely.

WHAT STAYED BEHIND
------------------
The autoscaler still decides whether a latency breach is ITS problem, using
`signals.classify.saturated` from the same shared library. It has to: it acts on
that answer inside its own loop, and if it waited on this process, a dispatcher
outage would silently return it to scaling on latency alone — exactly when the
cluster is least able to afford it. The split is "is this mine?" (there, because
it acts) against "then whose is it?" (here, because nobody needs that in 60s).

HOW A MANAGER SUBSCRIBES
------------------------
By labelling its own service:

    infra.handles=database

Nothing here lists managers. A claimed cause is that manager's to report and
this process goes quiet about it; an unclaimed one raises
`dispatcher_signal_unowned`, which is what the alert fires on. A cause nobody
will ever fix — a third-party API you do not control — is muted per component
with `autoscale.mute_causes`, and mutes and claims are the same mechanism seen
from two ends.
"""

import json
import logging
import os
import signal
import sys
import time
from collections import namedtuple

import docker
import requests
from prometheus_client import Counter, Gauge, start_http_server

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from signals import classify, discovery, expressions, query  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("dispatcher")


def _env(key, default=None, cast=str):
    """Empty is absent — the same rule the autoscaler learned the hard way."""
    raw = os.environ.get(key)
    if raw is None or str(raw).strip() == "":
        raw = default
    if raw is None:
        raise RuntimeError(f"missing required env var {key}")
    if cast is bool:
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    return cast(raw)


LOOP_SECONDS = _env("LOOP_SECONDS", "60", int)
# Short. A manager that cannot answer in five seconds is one this loop must not
# wait on: the next delivery is a minute away and carries the same world.
DELIVER_TIMEOUT_SECONDS = _env("DELIVER_TIMEOUT_SECONDS", "5", int)
METRICS_PORT = _env("METRICS_PORT", "9210", int)

WORKLOAD_LABEL = "infra.workload"
WORKLOAD_APP = "app"
COMPONENT_LABEL = "infra.component"

_SVC = ["service"]
G_SIGNAL = Gauge("dispatcher_signal",
                 "1 when a service is slow for the named reason", _SVC + ["cause"])
G_UNOWNED = Gauge("dispatcher_signal_unowned",
                  "1 when a cause has no handler and is not muted", _SVC + ["cause"])
G_LATENCY = Gauge("dispatcher_service_latency_ms",
                  "Sustained request latency, as the autoscaler reads it", _SVC)
G_CLAIMS = Gauge("dispatcher_claimed_causes", "Causes some service claims", ["cause"])
G_LOOP = Gauge("dispatcher_last_loop_timestamp_seconds", "Unix time of the last loop")
G_SERVICES = Gauge("dispatcher_watched_services", "Application services being watched")
G_DIRECTION = Gauge("dispatcher_direction",
                    "1 on the direction currently dispatched for a service",
                    _SVC + ["direction"])
# 0 means a manager claimed a cause and could not be reached, so it is acting on
# nothing at all. That is a louder condition than any single service's latency.
G_DELIVERY = Gauge("dispatcher_delivery_ok",
                   "1 when the last dispatch to this manager succeeded", ["manager"])
C_ERRORS = Counter("dispatcher_errors_total", "Failures by stage", ["stage"])

query.on_error = lambda stage: C_ERRORS.labels(stage=stage).inc()

dkr = docker.DockerClient(base_url="unix:///var/run/docker.sock")

_said = set()


def say_once(key, message, *args):
    """
    A cause that persists for a week should not be a week of identical lines.
    Keyed on the verdict, so a CHANGE of verdict does speak up.
    """
    if key in _said:
        return
    _said.add(key)
    log.info(message, *args)


def forget(prefix_keys):
    _said.difference_update(prefix_keys)


class Watched:
    """One application service, with the policy bits this process needs."""

    def __init__(self, service):
        spec = service.attrs.get("Spec", {})
        labels = spec.get("Labels") or {}
        self.name = service.name
        self.component = labels.get(COMPONENT_LABEL, service.name.split("_")[0])
        self.enabled = str(labels.get("autoscale.enabled", "")).lower() in ("1", "true", "yes", "on")
        self.thresholds = classify.thresholds_from_labels(
            labels,
            on_bad=lambda key, raw: say_once(
                (self.name, "badthreshold", key, str(raw)),
                "%s: %s=%r is not usable; using the default", self.name, key, raw))
        self.muted = classify.parse_causes(
            labels.get(classify.MUTE_LABEL),
            on_bad=lambda bad: say_once(
                (self.name, "badmute", ",".join(bad)),
                "%s: %s names %s, which is not a cause; ignoring it",
                self.name, classify.MUTE_LABEL, ", ".join(bad)))
        resources = (spec.get("TaskTemplate") or {}).get("Resources") or {}
        limits = resources.get("Limits") or {}
        reservations = resources.get("Reservations") or {}
        self.cpu_limit = (limits.get("NanoCPUs") or reservations.get("NanoCPUs") or 0) / 1e9
        self.mem_limit = limits.get("MemoryBytes") or reservations.get("MemoryBytes") or 0

    @property
    def budget_ms(self):
        return self.thresholds.slo_ms * self.thresholds.up_ratio

    @property
    def sustain_up(self):
        return int(self.thresholds.sustain_up)

    @property
    def sustain_down(self):
        return int(self.thresholds.sustain_down)

    @property
    def busy_cpu(self):
        return self.thresholds.busy_cpu

    @property
    def busy_mem(self):
        return self.thresholds.busy_mem


def _num(labels, key, default):
    try:
        return float(labels[key])
    except (KeyError, TypeError, ValueError):
        return float(default)


def watched_services():
    """Application services, discovered by label exactly as the autoscaler does."""
    out = []
    for service in dkr.services.list():
        labels = (service.attrs.get("Spec", {}).get("Labels") or {})
        if labels.get(WORKLOAD_LABEL) == WORKLOAD_APP:
            out.append(Watched(service))
    return out


#: A manager declares WHERE to send it, alongside what it handles:
#:
#:     infra.handles=local
#:     infra.handles.port=9201
#:     infra.handles.path=/signal
#:
#: The address is the service's own name on the monitoring overlay. Nothing here
#: is configured with a manager's location, so a second manager is a label on a
#: second service and no edit at all.
HANDLER_PORT_LABEL = "infra.handles.port"
HANDLER_PATH_LABEL = "infra.handles.path"

Manager = namedtuple("Manager", ["name", "causes", "url"])


def managers():
    """
    Every service that claims a cause, with where to deliver to.

    A claim is on the CAUSE, never on one target: a manager that handles
    databases handles all of them, and `database:documents` would be a claim
    nothing could satisfy for the next database somebody creates.
    """
    out = []
    for service in dkr.services.list():
        labels = (service.attrs.get("Spec", {}).get("Labels") or {})
        causes = {c for c in classify.parse_causes(labels.get(classify.HANDLER_LABEL))
                  if c in classify.CAUSES}
        if not causes:
            continue
        port = labels.get(HANDLER_PORT_LABEL)
        if not port:
            say_once((service.name, "noport"),
                     "%s claims %s but declares no %s, so nothing can be delivered "
                     "to it", service.name, "/".join(sorted(causes)), HANDLER_PORT_LABEL)
            continue
        path = labels.get(HANDLER_PATH_LABEL) or "/signal"
        out.append(Manager(service.name, frozenset(causes),
                           f"http://{service.name}:{port}{path}"))
    return out


def claimed_causes(known=None):
    """Causes some manager in this cluster says it will act on."""
    claimed = set()
    for m in (managers() if known is None else known):
        claimed |= m.causes
    return claimed


def busy_components():
    """
    Non-application components working hard right now.

    Correlation, not proof, which is why it ranks below a timer the application
    itself publishes. It is still the difference between "your API is slow, good
    luck" and "your API is slow and your Mongo is pinned".
    """
    busy = set()
    usage = query.vm_query_map(expressions.CPU_BY_SERVICE, label=expressions.CPU_LABEL)
    for service in dkr.services.list():
        spec = service.attrs.get("Spec", {})
        labels = spec.get("Labels") or {}
        if COMPONENT_LABEL not in labels or labels.get(WORKLOAD_LABEL) == WORKLOAD_APP:
            continue
        resources = (spec.get("TaskTemplate") or {}).get("Resources") or {}
        limit = ((resources.get("Limits") or {}).get("NanoCPUs")
                 or (resources.get("Reservations") or {}).get("NanoCPUs") or 0) / 1e9
        used = usage.get(service.name)
        if used is not None and limit and used / limit * 100 >= classify.BUSY_CPU:
            busy.add(labels.get(COMPONENT_LABEL, service.name))
    return busy


#: (held, peak) for one service. `held` is the sustained MINIMUM over the
#: scale-up window — was it above the line for the whole time — and `peak` the
#: MAXIMUM over the scale-down one. Two windows, two aggregates: up fast, down
#: slow, and never symmetric.
Reading = namedtuple("Reading", ["held", "peak"])
EMPTY = Reading((None, None, None), (None, None, None))


def measure(services):
    """
    {name: Reading} for every watched service, in a handful of grouped queries.

    Ten components x six queries at a 15s timeout does not fit in a 60s loop, so
    everything is aggregated `by (service)` and read once per (expression,
    window) rather than once per service.
    """
    out = {s.name: EMPTY for s in services}
    if not services:
        return out
    found = discovery.discover_latency([s.name for s in services])

    util = {}
    for aggregate, windows in (("min_over_time", {s.sustain_up for s in services}),
                               ("max_over_time", {s.sustain_down for s in services})):
        for window in windows:
            for key, expr in (("cpu", expressions.CPU_BY_SERVICE),
                              ("mem", expressions.MEM_BY_SERVICE)):
                util[(key, aggregate, window)] = query.vm_query_map(
                    query.sustained(expr, window, aggregate), label=expressions.CPU_LABEL)

    latency = {}
    for s in services:
        if s.name not in found:
            continue
        expr = found[s.name][0]
        for aggregate, window in (("min_over_time", s.sustain_up),
                                  ("max_over_time", s.sustain_down)):
            if (expr, aggregate, window) in latency:
                continue
            latency[(expr, aggregate, window)] = query.vm_query_map(
                query.sustained(expr, aggregate=aggregate, window=window))

    def pct(value, divisor):
        return None if value is None or divisor <= 0 else value / divisor * 100

    for s in services:
        expr = found.get(s.name, (None,))[0]

        def side(aggregate, window):
            lat = latency.get((expr, aggregate, window), {}).get(s.name) if expr else None
            return (lat,
                    pct(util[("cpu", aggregate, window)].get(s.name), max(s.cpu_limit, 0.01)),
                    pct(util[("mem", aggregate, window)].get(s.name), s.mem_limit))

        out[s.name] = Reading(side("min_over_time", s.sustain_up),
                              side("max_over_time", s.sustain_down))
    return out


def attribute(service, cpu_pct, mem_pct, dependencies, busy):
    """
    Why is this service slow? Returns (cause, target or None).

    Evidence, in order of how much it is worth:

      1. The replicas are busy      -> it is us. More replicas is the fix, and
                                       the autoscaler has already applied it.
      2. An outbound timer this service publishes is over the same budget
                                    -> it is whatever that timer measures,
                                       named by its own label.
      3. A component in this cluster is busy -> probably that. Weaker than 2
                                       because it is correlation: two things can
                                       be busy at the same time.
      4. Nothing                    -> `unknown`, which is what it is. Guessing
                                       here sends somebody to read the wrong log.
    """
    if classify.saturated(service.busy_cpu, service.busy_mem, cpu_pct, mem_pct):
        return classify.CAUSE_LOCAL, None

    worst = None
    for cause, expr, base, target_label in dependencies.get(service.name, ()):
        if target_label:
            readings = query.vm_query_map(f"topk(1, {expr}) by ({target_label})",
                                          label=target_label)
        else:
            readings = {base: query.vm_query(expr)}
        for key, value in readings.items():
            if value is None or value <= service.budget_ms:
                continue
            if worst is None or value > worst[2]:
                worst = (cause, key, value)
    if worst:
        return worst[0], worst[1]

    if busy:
        return classify.CAUSE_DATABASE, sorted(busy)[0]
    return classify.CAUSE_UNKNOWN, None


def publish(service, verdict, handled, alert):
    """Publish one service's verdict, and say it once in the log."""
    for name in classify.CAUSES:
        G_SIGNAL.labels(service=service.name, cause=name).set(
            1 if name == verdict["cause"] else 0)
        # The TARGET is deliberately not a label. A third-party hostname is
        # unbounded cardinality, and one runaway series takes the metrics store
        # with it. It goes in the log line and the alert's description instead.
        G_UNOWNED.labels(service=service.name, cause=name).set(
            1 if (name == verdict["cause"] and alert) else 0)
    for name in (classify.DIRECTION_UP, classify.DIRECTION_DOWN, classify.DIRECTION_HOLD):
        G_DIRECTION.labels(service=service.name, direction=name).set(
            1 if name == verdict["direction"] else 0)

    cause, target = verdict["cause"], verdict["target"]
    where = f" ({target})" if target else ""
    key = (service.name, cause, target or "", handled or "unowned", verdict["direction"])
    forget({k for k in _said if len(k) == 5 and k[0] == service.name and k != key})
    if verdict["direction"] != classify.DIRECTION_HOLD:
        say_once(key, "%s: %s — %s", service.name, verdict["direction"], verdict["reason"])
    elif handled == "muted":
        say_once(key, "%s: %s%s is throttling it and is muted; not alerting.",
                 service.name, cause, where)
    elif handled == "claimed":
        say_once(key, "%s: %s%s is throttling it; %s claims it, dispatching there.",
                 service.name, cause, where, cause)
    elif cause != classify.CAUSE_LOCAL:
        say_once(key, "%s: %s%s is throttling it and NOTHING claims it. More "
                      "replicas cannot fix this. Either give something "
                      "infra.handles=%s, or mute it with %s.",
                 service.name, cause, where, cause, classify.MUTE_LABEL)


def quiet(service):
    for name in classify.CAUSES:
        G_SIGNAL.labels(service=service.name, cause=name).set(0)
        G_UNOWNED.labels(service=service.name, cause=name).set(0)
    forget({k for k in _said if k[0] == service.name})


def deliver(manager, verdicts):
    """
    POST the manager's whole current world, every loop.

    LEVEL-TRIGGERED, NOT EVENT-TRIGGERED. Each delivery carries the complete
    current verdict for every service the manager handles, never a delta — so a
    delivery that fails is corrected by the next one a minute later rather than
    lost. That is also why nothing is queued or retried here: a retry would
    deliver a stale world, which is worse than delivering nothing.
    """
    body = json.dumps({"at": time.time(), "from": "dispatcher",
                       "signals": verdicts}).encode()
    try:
        resp = requests.post(manager.url, data=body, timeout=DELIVER_TIMEOUT_SECONDS,
                             headers={"Content-Type": "application/json"})
        resp.raise_for_status()
        G_DELIVERY.labels(manager=manager.name).set(1)
        return True
    except Exception as exc:  # noqa: BLE001
        C_ERRORS.labels(stage="deliver").inc()
        G_DELIVERY.labels(manager=manager.name).set(0)
        say_once((manager.name, "undeliverable"),
                 "cannot deliver to %s at %s (%s). It will be retried every loop; "
                 "until it succeeds that manager is acting on nothing.",
                 manager.name, manager.url, exc)
        return False


def loop():
    services = [s for s in watched_services()]
    G_SERVICES.set(len(services))
    readings = measure(services)

    known = managers()
    claims = claimed_causes(known)
    for cause in classify.CAUSES:
        G_CLAIMS.labels(cause=cause).set(1 if cause in claims else 0)

    # Attribution is only needed for a service that is slow WITHOUT its replicas
    # being busy — the state where the count must not move and somebody still
    # has to be told why. Everything else skips the dependency queries entirely.
    needs_cause = []
    decided = {}
    for s in services:
        reading = readings.get(s.name, EMPTY)
        direction, reason = classify.decide(s.thresholds, reading.held, reading.peak)
        lat, cpu, mem = reading.held
        if lat is not None:
            G_LATENCY.labels(s.name).set(lat)
        decided[s.name] = {"service": s.name, "direction": direction, "reason": reason,
                           "cause": classify.CAUSE_LOCAL, "target": None,
                           "latency_ms": lat, "cpu_pct": cpu, "mem_pct": mem,
                           "enabled": s.enabled}
        if direction == classify.DIRECTION_HOLD and reason:
            needs_cause.append((s, cpu, mem))

    if needs_cause:
        dependencies = discovery.discover_dependencies([s.name for s, _, _ in needs_cause])
        busy = busy_components()
        for s, cpu, mem in needs_cause:
            cause, target = attribute(s, cpu, mem, dependencies, busy)
            decided[s.name].update(cause=cause, target=target)

    for s in services:
        verdict = decided[s.name]
        if verdict["direction"] == classify.DIRECTION_HOLD and not verdict["reason"]:
            quiet(s)
            continue
        handled, alert = classify.verdict(verdict["cause"], verdict["target"],
                                          s.muted, claims)
        publish(s, verdict, handled, alert)

    # Every manager gets the services whose cause it claims — including the ones
    # that are fine. A manager that only heard about problems could never learn
    # that a problem had ended.
    for m in known:
        payload = [v for v in decided.values() if v["cause"] in m.causes]
        if payload:
            deliver(m, payload)
    return decided


_running = True


def _stop(signum, _frame):
    global _running
    log.info("received signal %s, finishing", signum)
    _running = False


def main():
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    start_http_server(METRICS_PORT)
    log.info("dispatcher up — watching infra.workload=%s, every %ds",
             WORKLOAD_APP, LOOP_SECONDS)
    log.info("a manager subscribes by labelling its own service %s=<cause>; "
             "causes are %s", classify.HANDLER_LABEL, "/".join(classify.CAUSES))
    while _running:
        started = time.time()
        try:
            loop()
            G_LOOP.set(time.time())
        except Exception as exc:  # noqa: BLE001
            C_ERRORS.labels(stage="loop").inc()
            log.warning("loop failed: %s", exc)
        # Nothing here changes the cluster, so a slow loop is a stale reading
        # rather than a stuck fleet — sleep the remainder and carry on.
        time.sleep(max(1.0, LOOP_SECONDS - (time.time() - started)))


if __name__ == "__main__":
    main()
