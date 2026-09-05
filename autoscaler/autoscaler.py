#!/usr/bin/env python3
"""
The autoscaler: it applies. It does not decide.

WHAT IT DOES
------------
Three things to application services, and nothing to anything else:

  1. REPLICA COUNTS. The overseer dispatches a direction and a ceiling; this
     turns them into a number within the service's own bounds and writes it.
  2. PLACEMENT. The overseer dispatches `pinned`; this adds or removes
     `node.role == worker` on each service, and forces a re-placement when a
     released pin has left the master with nothing running on it.
  3. RESERVATIONS. It measures what a replica actually uses and re-sizes the
     numbers Swarm schedules on. This is the one decision it still makes alone,
     because it is a property of the SERVICE rather than of the fleet.

WHAT IT NO LONGER DOES
----------------------
It holds no Hetzner token. It cannot create or delete a server, does not know
what a plan ladder is, and has no opinion about how many machines exist. All of
that moved to the overseer when a SECOND manager — dataguard — needed machines
too, and two processes buying servers from two copies of the capacity model was
never going to end well. See overseer/overseer.py for the argument.

WHAT IT SCALES IS DISCOVERED, NOT CONFIGURED
--------------------------------------------
There is no APP_SERVICE. Every service carrying `infra.workload=app` is an
application workload, and its whole policy travels with it as `autoscale.*`
labels on that same service. Anything without that label is invisible here.
Both halves of that contract live in `signals.workloads`, shared with the
overseer, so the two cannot come to disagree about what a service asked for.

NO SIGNAL MEANS NO ACTION
-------------------------
If the overseer is down nothing arrives, the verdicts go stale, and every
service holds exactly where it is. That is the failure mode this split was
chosen for: a fleet that stops changing, not one that falls back to a worse rule
at the moment it is least able to afford it. Right-sizing keeps running, because
it needs no verdict.

IT WILL NOT TOUCH A DATABASE
----------------------------
A service carrying `infra.managed_by=dataguard` is refused outright — no replica
change, no constraint change. Those services already carry no `infra.workload`
label and so are already invisible to discovery; the explicit refusal exists
because the accident it prevents is not an outage but corruption. Scaling a
mongod service to two replicas starts a second mongod on the same data
directory.

STATELESS BY DESIGN. Cooldowns are wall-clock since this process last acted, and
everything else is re-read from Swarm each loop, so restarting this container
loses nothing but a cooldown.
"""

import os
import re
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging

import docker
from prometheus_client import Counter, Gauge, start_http_server

from signals import classify, expressions, query, workloads


def _env(key, default=None, cast=str):
    """
    A setting, resolved default-first.

    AN EMPTY VALUE IS AN ABSENT ONE. `stacks/monitoring.yml` passes each setting
    as "${KEY}", and `docker stack deploy` substitutes a variable that infra.env
    does not carry with the empty string rather than leaving it unset — so the
    container always receives the key, and `os.environ.get` never sees its
    default. Every cluster built before a setting existed therefore delivers ""
    to a reader expecting a number: `int("")` raises at import, the process
    exits 1, and Swarm restarts it forever. That is exactly how adding the
    vertical-scaling ceilings crashlooped a running autoscaler, and it is why
    the repo's default has to win over an empty string rather than the other way
    round. `SCHEDULE_FLOOR` is deliberately blank and its default is blank too,
    so treating the two as the same thing costs nothing.
    """
    raw = os.environ.get(key)
    if raw is None or str(raw).strip() == "":
        raw = default
    if raw is None:
        raise RuntimeError(f"missing required env var {key}")
    if cast is bool:
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    return cast(raw)



# ---------------------------------------------------------------------------
# config — cluster-wide only
# ---------------------------------------------------------------------------
# Nothing here names an application, and nothing here is a scaling threshold.
# Those belong to the service they apply to, as labels. Nothing here is about
# the fleet either, any more — that is the overseer's.

CLUSTER = _env("APP_NAME", "app")
VM_URL = _env("VM_URL", "http://victoriametrics:8428")
LOOP_SECONDS = _env("LOOP_SECONDS", "60", int)
DRY_RUN = _env("DRY_RUN", "false", bool)  # a real safety switch, not a rehearsal

# Right-sizing: reservations measured from real usage rather than typed. On by
# default, because the typed number is a guess made before the component had
# ever run and is wrong by an order of magnitude in the direction that costs
# money. Turn it off and the spec's values are used unchanged.
RIGHT_SIZE = _env("RIGHT_SIZE", "true", bool)
# Long, because every resize restarts every replica of the service. Sizing is
# not a control loop chasing load — the replica count does that — it is a slow
# correction of a number that only changes when the application changes.
RESIZE_COOLDOWN_SECONDS = _env("RESIZE_COOLDOWN_SECONDS", "3600", int)
# How long a released pin may leave the master with no replica before this
# process forces a rolling re-placement. Swarm never rebalances a running task,
# so without the nudge it waits for an event that cannot happen on its own.
HANDOVER_STALL_SECONDS = _env("HANDOVER_STALL_SECONDS", "900", int)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("autoscaler")


# ---------------------------------------------------------------------------
# shared signal library
# ---------------------------------------------------------------------------
# What a workload IS, what it costs and what policy it carries are read by the
# overseer too, from `signals.workloads`. Two copies of `policy_from_labels` in
# two images is a policy fixed in one and not the other, with nothing failing
# while they disagree — so there is one copy and both import it.
vm_query = query.vm_query
vm_query_map = query.vm_query_map
sustained = query.sustained
_CPU_LABEL = expressions.CPU_LABEL
CPU_BY_SERVICE = expressions.CPU_BY_SERVICE
MEM_BY_SERVICE = expressions.MEM_BY_SERVICE


# ---------------------------------------------------------------------------
# metrics about ourselves
# ---------------------------------------------------------------------------
# THE RULE THAT KEEPS THE ALERTS WORKING: an unlabeled gauge is given the
# exporting SWARM SERVICE's name by vmagent, so an alert that joins two
# unlabeled gauges only works while both come from the SAME process. Every
# unlabeled fleet gauge therefore lives in the overseer, and everything here is
# either per-service — carrying its own `service` label, which `honor_labels`
# preserves across processes — or about this process itself.

M_LOOP = Gauge("autoscaler_last_loop_timestamp_seconds", "Unix time of last completed loop")
M_MANAGED = Gauge("autoscaler_managed_services", "Services carrying infra.workload=app")
M_EVENTS = Counter("autoscaler_scale_events_total", "Replica actions taken", ["direction"])
M_ERRORS = Counter("autoscaler_errors_total", "Errors encountered", ["stage"])
# The overseer owns the performance loop and the fleet, so these two say whether
# this process is being told anything at all. A cluster that has stopped
# changing looks identical to a quiet one until you can see that no verdict has
# arrived.
M_SIGNAL_AT = Gauge("autoscaler_last_dispatch_timestamp_seconds",
                    "Unix time of the last verdict delivered by the overseer")
M_DISPATCH_WAITING = Gauge("autoscaler_services_awaiting_dispatch",
                           "Autoscaled services with no fresh verdict, therefore holding")
M_REFUSED = Counter("autoscaler_refused_total",
                    "Changes refused because something else owns the service", ["reason"])

_SVC = ["service"]
S_P95 = Gauge("autoscaler_service_p95_ms", "p95 latency in milliseconds", _SVC)
S_SLO = Gauge("autoscaler_service_slo_p95_ms", "Configured p95 SLO", _SVC)
S_CURRENT = Gauge("autoscaler_service_current_replicas", "Replicas in the live spec", _SVC)
S_DESIRED = Gauge("autoscaler_service_desired_replicas", "Replicas the signals asked for", _SVC)
S_ADMITTED = Gauge("autoscaler_service_admitted_replicas", "Replicas that fit and were applied", _SVC)
S_RUNNING = Gauge("autoscaler_service_running_replicas", "Tasks actually running", _SVC)
S_ROLLBACK = Gauge("autoscaler_service_deploy_rolled_back",
                   "1 when Swarm reverted this service's last update", _SVC)
#: 1 when this service has a usable CPU-per-replica reading this loop.
#:
#: Scaling DOWN requires it — a missing series holds the replica count rather
#: than shrinking it, which is the safe choice but a silent one. cadvisor
#: stopped reporting per-container metrics entirely after a Docker upgrade and
#: nothing said so: the loop logged "cpu/replica n/a" once a minute into a log
#: nobody reads, held every service at its current size, and kept a worker alive
#: against zero traffic for as long as it ran.
S_CPU_SIGNAL = Gauge("autoscaler_service_cpu_signal_present",
                     "1 when CPU-per-replica is readable for this service", _SVC)
S_PENDING = Gauge("autoscaler_service_pending_replicas", "Tasks wanted but not placed", _SVC)
S_MIN = Gauge("autoscaler_service_min_replicas", "Configured replica floor", _SVC)
S_MAX = Gauge("autoscaler_service_max_replicas", "Configured replica ceiling", _SVC)
S_AUTO = Gauge("autoscaler_service_autoscale_enabled", "1 when the replica count is driven by signals", _SVC)
S_CPU = Gauge("autoscaler_service_cpu_per_replica_percent", "Mean CPU per replica, % of limit", _SVC)
S_PINNED = Gauge("autoscaler_service_worker_mode", "1 when this service is pinned to workers", _SVC)
S_COST_CPU = Gauge("autoscaler_service_replica_cost_cpu_cores", "CPU one replica reserves", _SVC)
S_COST_MEM = Gauge("autoscaler_service_replica_cost_memory_bytes", "Memory one replica reserves", _SVC)
S_MEM = Gauge("autoscaler_service_memory_per_replica_percent",
              "Mean working set per replica, % of limit", _SVC)
_PER_SERVICE = [S_P95, S_SLO, S_CURRENT, S_DESIRED, S_ADMITTED, S_RUNNING, S_PENDING,
                S_MIN, S_MAX, S_AUTO, S_CPU, S_MEM, S_PINNED, S_CPU_SIGNAL, S_ROLLBACK,
                S_COST_CPU, S_COST_MEM]
_exported_services = set()

query.on_error = lambda stage: M_ERRORS.labels(stage=stage).inc()
workloads.on_error = lambda stage: M_ERRORS.labels(stage=stage).inc()

dkr = docker.DockerClient(base_url="unix:///var/run/docker.sock")

_running = True
#: What the SIGNALS asked for, before the overseer's ceiling capped it — the
#: number `autoscaler_service_desired_replicas` is named after. Recorded here
#: rather than recomputed, because the stabiliser that produces it keeps state
#: and asking it twice in a loop is asking it a different question the second
#: time.
_wanted = {}
_last_replica_change = {}       # service name -> unix time
_unpinned_since = {}            # service name -> when its pin was released

_warned = set()


def warn_once(key, message, *args):
    """
    Log a policy complaint once per distinct value, not once per loop.

    Keyed on the offending value rather than the field, so a bad label is
    reported once a day instead of 1440 times — but CHANGING it to a different
    bad value reports again.
    """
    if key in _warned:
        return
    if len(_warned) > 5000:            # a pathological spec must not leak memory
        _warned.clear()
    _warned.add(key)
    log.warning(message, *args)



def discover_workloads():
    return workloads.discover_workloads(
        dkr,
        on_skip=lambda name, exc: log.warning(
            "skipping %s this loop: %s", name or "the service list", exc))

def forget_vanished(current_names):
    """
    Drop metric children for services that no longer exist.

    prometheus_client keeps a labeled child forever once created, so a deleted
    component would leave `autoscaler_service_running_replicas{service="gone"}=3`
    sitting at its last value, alerting or — worse — suppressing an alert
    indefinitely. Removed one child at a time rather than clear()-and-re-set,
    because a scrape landing between those two sees the series missing, and for
    these rules a missing series is a page.
    """
    for name in _exported_services - current_names:
        for gauge in _PER_SERVICE:
            try:
                gauge.remove(name)
            except KeyError:
                pass
        # Same reason, one dict along: a deleted component leaving its last
        # wanted count behind would keep publishing it under the next service to
        # be given its name.
        _wanted.pop(name, None)
    _exported_services.intersection_update(current_names)
    _exported_services.update(current_names)


def _stop(signum, _frame):
    global _running
    log.info("received signal %s, finishing current loop then exiting", signum)
    _running = False


signal.signal(signal.SIGTERM, _stop)
signal.signal(signal.SIGINT, _stop)


def manager_node_id():
    return dkr.info().get("Swarm", {}).get("NodeID") or ""


def get_manager_node():
    try:
        for node in dkr.nodes.list():
            if node.attrs.get("Spec", {}).get("Role") == "manager":
                return node
    except Exception as exc:  # noqa: BLE001
        log.warning("cannot list nodes: %s", exc)
    return None


def index_tasks():
    """
    {node id: [task]} in ONE API call.

    node_reserved() used to be called per node, so a six-worker fleet issued
    seven task listings every loop. Everything below reads from this index.
    """
    by_node = {}
    for task in dkr.api.tasks(filters={"desired-state": "running"}):
        node_id = task.get("NodeID")
        if node_id:
            by_node.setdefault(node_id, []).append(task)
    return by_node

# ---------------------------------------------------------------------------
# placement
# ---------------------------------------------------------------------------

def _service_update_constraint(name, add):
    """
    `docker service update --constraint-add/rm` on one service, detached.

    Detached on purpose. Blocking would hold the loop for parallelism x monitor
    x replicas — long enough for AutoscalerStalled to fire — and there is
    nothing to wait for: the next loop reads live state and carries on.
    """
    cmd = ["docker", "service", "update", "--detach=true"]
    if add:
        cmd += ["--constraint-add", workloads.WORKER_CONSTRAINT]
    else:
        # --constraint-rm matches the stored string EXACTLY, and the stored
        # string is not ours. The renderer writes `node.role == worker` with
        # spaces; workloads.WORKER_CONSTRAINT is written without them. Removing the
        # spaced constraint by its unspaced name silently removed nothing, so
        # the pin could never be released: every loop issued an update that
        # changed the service version and nothing else, the applications stayed
        # bound to workers forever, and the fleet could never scale to zero no
        # matter how idle the cluster was. _WORKER_PIN already tolerates every
        # spelling when READING; this is the same tolerance when writing.
        live = workloads.worker_pin_constraints(dkr.services.get(name))
        if not live:
            return False          # already unpinned; issuing a no-op update is
                                  # what produced version 13629 on a service
                                  # nobody had deployed in an hour
        for constraint in live:
            cmd += ["--constraint-rm", constraint]
    cmd.append(name)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed ({proc.returncode}): "
                           f"{(proc.stderr or proc.stdout).strip()[:400]}")
    return True


def set_service_placement(name, pinned, reason):
    if DRY_RUN:
        log.info("[dry-run] would move %s to %s mode (%s)", name,
                 workloads.MODE_WORKER if pinned else workloads.MODE_MANAGER, reason)
        return
    if not _service_update_constraint(name, add=pinned):
        # Nothing to remove. Saying "moving to manager mode" once a minute for
        # a service that has been in manager mode all along is how a loop that
        # was achieving nothing still looked busy.
        return False
    M_EVENTS.labels(direction=f"placement-{workloads.MODE_WORKER if pinned else workloads.MODE_MANAGER}").inc()
    log.info("moving %s to %s mode: %s", name,
             workloads.MODE_WORKER if pinned else workloads.MODE_MANAGER, reason)
    return True


def reconcile_placement(found, want_pinned, reason, skip_rolling=True):
    """
    Bring EVERY app service to the target placement, every loop.

    Not just on transitions. That is the fix for the drift the old code had: it
    read the mode from one distinguished service, so a second service that had
    somehow lost its constraint was never reconciled — and a service created
    while the cluster was already in worker mode was never pinned at all. Here,
    a component that appeared thirty seconds ago is pinned on the next loop, and
    each service is deferred or fails on its own without blocking the rest.
    """
    changed = []
    for w in found:
        if w.placement_pinned:
            # Somebody chose this placement deliberately. Moving it anyway would
            # make the setting in the panel a lie, and for a component pinned to
            # the master that is the difference between "stays put" and "gets
            # migrated onto a worker that is about to be deleted".
            continue
        if w.pinned == want_pinned:
            continue
        if skip_rolling and w.rolling:
            log.info("deferring the placement change on %s: a rollout is in flight", w.name)
            continue
        try:
            if set_service_placement(w.name, want_pinned, reason):
                changed.append(w.name)
        except Exception as exc:  # noqa: BLE001
            M_ERRORS.labels(stage="placement").inc()
            log.error("could not change placement on %s: %s", w.name, exc)
    return changed


def manager_running_by_service(tasks_by_node, manager_id):
    """{service id: running task count} on the master, from the single index."""
    counts = {}
    for task in tasks_by_node.get(manager_id, []):
        if task.get("Status", {}).get("State") == "running":
            sid = task.get("ServiceID")
            counts[sid] = counts.get(sid, 0) + 1
    return counts


def running_and_pending(tasks_by_node, found):
    """(running, pending) per service name, across the whole cluster."""
    by_id = {w.id: w.name for w in found}
    running = {w.name: 0 for w in found}
    pending = {w.name: 0 for w in found}
    for tasks in tasks_by_node.values():
        for task in tasks:
            name = by_id.get(task.get("ServiceID"))
            if not name:
                continue
            state = task.get("Status", {}).get("State")
            if state == "running":
                running[name] += 1
            elif state in ("pending", "rejected", "assigned", "accepted", "preparing"):
                pending[name] += 1
    return running, pending

# ---------------------------------------------------------------------------
# the dispatch receiver
# ---------------------------------------------------------------------------
# This process no longer reads latency, and no longer decides whether a service
# is slow. The OVERSEER owns the performance loop and pushes a verdict here;
# what is left is the half that is genuinely capacity work — turning a direction
# into a replica count within this service's bounds, then placing it.
#
# NO SIGNAL MEANS NO ACTION. If the overseer is down, nothing arrives, the
# verdicts go stale and every service holds exactly where it is. That is the
# whole reason the loop lives over there: the failure mode is a fleet that stops
# changing, not one that falls back to a worse rule at the moment it is least
# able to afford it. Everything else this loop does — placement, node lifecycle,
# reaping, resizing — is driven by the replica counts that already exist and
# keeps running normally.

SIGNAL_PORT = _env("SIGNAL_PORT", "9201", int)
#: A verdict older than this is not a verdict. Three loops of slack, so a single
#: missed delivery is absorbed and a dead overseer is noticed.
SIGNAL_TTL_SECONDS = _env("SIGNAL_TTL_SECONDS", str(LOOP_SECONDS * 3), int)

_signals_lock = threading.Lock()
_dispatched = {}            # service name -> (received at, verdict dict)


def record_dispatch(payload):
    """Store a delivery. Returns how many verdicts it carried."""
    now = time.time()
    received = payload.get("signals") or []
    with _signals_lock:
        for verdict in received:
            name = verdict.get("service")
            if name:
                _dispatched[name] = (now, verdict)
    M_SIGNAL_AT.set(now)
    return len(received)


def dispatched_for(name, now=None):
    """
    The current verdict for a service, or None if nothing fresh has arrived.

    None is the safe answer and the caller must treat it as "hold": a stale
    verdict is a decision made about a cluster that has since changed.
    """
    now = now or time.time()
    with _signals_lock:
        entry = _dispatched.get(name)
    if not entry:
        return None
    at, verdict = entry
    if now - at > SIGNAL_TTL_SECONDS:
        return None
    return verdict


class _SignalHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):                                   # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 1_000_000:
            self.send_error(400, "empty or oversized body")
            return
        try:
            payload = json.loads(self.rfile.read(length))
            count = record_dispatch(payload)
        except Exception as exc:  # noqa: BLE001
            M_ERRORS.labels(stage="signal").inc()
            self.send_error(400, f"unreadable dispatch: {exc}")
            return
        body = json.dumps({"accepted": count}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        """One line per delivery would be 1440 a day saying nothing."""


def serve_signals():
    """
    Listen on the monitoring overlay only.

    Not published to a host port and not on `edge`: the only things that can
    reach this are the infrastructure services on that network. Components live
    on `edge`, so an application cannot reach the endpoint that changes its own
    replica count.
    """
    server = ThreadingHTTPServer(("0.0.0.0", SIGNAL_PORT), _SignalHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name="signal-receiver")
    thread.start()
    log.info("listening for dispatch on :%d (verdicts expire after %ds)",
             SIGNAL_PORT, SIGNAL_TTL_SECONDS)
    return server


# ---------------------------------------------------------------------------
# right-sizing — reservations measured, not typed
# ---------------------------------------------------------------------------
#
# A reservation is a promise about what one replica needs, and nobody can make
# that promise accurately by typing it into a form on day one. The default here
# was 0.5 CPU / 384MB; the first real app to run on it used 0.008 cores at rest
# and peaked at 151MB. Reserving 0.36 of a core for a fortieth of one is not a
# rounding error — it is the difference between the master carrying the whole
# cluster and a worker being billed around the clock to hold one idle replica.
#
# So the numbers are measured. CPU and memory are sized differently on purpose:
#
#   CPU is compressible. A replica that wants more than its reservation is
#   throttled, not killed, so the reservation is sized from a high quantile and
#   the LIMIT absorbs bursts. A JVM's startup spike belongs in the limit; paying
#   for it in the reservation forever is how you end up here.
#
#   Memory is not. Exceeding a limit is an OOM kill, so memory is sized from the
#   true maximum with real headroom, never from a quantile.

#: Multipliers applied to measured usage. Generous rather than tight: the cost
#: of over-reserving is money, and the cost of under-reserving memory is a dead
#: container.
CPU_RESERVE_HEADROOM = 2.0
MEM_RESERVE_HEADROOM = 1.5
CPU_LIMIT_MULTIPLE = 4.0
MEM_LIMIT_MULTIPLE = 2.0

#: Floors. Below these the numbers stop meaning anything and Swarm's scheduler
#: starts treating the service as free.
CPU_RESERVE_FLOOR = 0.02
MEM_RESERVE_FLOOR_MB = 32

#: THROTTLING, and why the CPU limit cannot be derived from usage alone.
#:
#: Everything above reads `container_cpu_usage_seconds_total` — CPU actually
#: CONSUMED. A container at its CPU limit cannot consume more than the limit, so
#: the measurement that sets the cap is suppressed BY the cap. Size a cap from
#: it and the result is self-confirming: the service looks like it wants exactly
#: what it was allowed, and stays there forever.
#:
#: That is not theoretical. An I/O-bound API measured 0.003 cores because it
#: spends its life waiting on the network, floored to a 0.02 reservation and a
#: 0.08 limit — 8ms of CPU per 100ms scheduling period. Requests needing 30ms of
#: CPU could not be served in one period, so the kernel chopped them across four
#: or five, and the shape of the latency histogram gave it away: 70% of requests
#: under 5ms, NOTHING between 100 and 250ms, then a hard cluster at 250-500ms.
#: Not a tail — a second population, quantised by the 100ms period. The service
#: was two orders of magnitude slower than its work, and every averaged CPU
#: reading said it was nearly idle, which it was, because it was waiting.
#:
#: `container_cpu_cfs_throttled_periods_total` is the one signal that escapes
#: the trap: it counts periods where the container had work to run and was not
#: allowed to. Any throttling at all means the cap is too low, so the cap is
#: raised until throttling stops rather than inferred from what got through.
#:
#: Raising it is close to free. Swarm PACKS on reservations, not limits, so an
#: unused ceiling occupies nothing and costs nothing; contention is settled by
#: CPU shares, which come from the reservation and are untouched here. The
#: asymmetry is total — a ceiling set too high costs nothing at all, and one set
#: too low costs half a second a request.
THROTTLE_TARGET_PCT = 1.0     # any sustained throttling is too much
CPU_LIMIT_RELIEF = 4.0        # one decisive step, not a crawl up an hourly cooldown
CPU_LIMIT_RELIEF_FLOOR = 0.5  # below ~half a core, CFS quantisation dominates latency

#: Don't touch a service for a change smaller than this. Every resize is a
#: rolling restart of every replica, so chasing a 5% drift would restart the
#: cluster all day and never converge.
RESIZE_MIN_CHANGE = 0.25

#: How much history a resize needs. Shorter than this and the first sample after
#: a deploy — a cold JVM compiling everything it owns — becomes the reservation.
RESIZE_MIN_HISTORY = "2h"

USAGE_CPU_Q = 0.90
_last_resize = {}


def measure_usage(service_names):
    """
    {service: (cpu_cores_q90, memory_bytes_max)} over RESIZE_MIN_HISTORY.

    Both are per replica: cadvisor reports per container and these aggregate
    with max/quantile across the replicas of a service, so the answer is "what
    does ONE of these need", which is the unit a reservation is in.
    """
    cpu = vm_query_map(
        f'quantile_over_time({USAGE_CPU_Q}, '
        f'max by ({_CPU_LABEL}) (rate(container_cpu_usage_seconds_total'
        f'{{{_CPU_LABEL}!=""}}[5m]))[{RESIZE_MIN_HISTORY}:1m])',
        label=_CPU_LABEL)
    mem = vm_query_map(
        f'max_over_time('
        f'max by ({_CPU_LABEL}) (container_memory_working_set_bytes'
        f'{{{_CPU_LABEL}!=""}})[{RESIZE_MIN_HISTORY}:1m])',
        label=_CPU_LABEL)
    return {name: (cpu.get(name), mem.get(name))
            for name in service_names
            if cpu.get(name) is not None and mem.get(name) is not None}


def measure_throttling(service_names):
    """
    {service: % of scheduling periods in which the CPU cap stopped it}.

    The worst replica, not the mean: one throttled replica serves slow requests
    to whoever lands on it, and averaging it against two idle ones hides that.
    """
    pct = vm_query_map(
        f'max by ({_CPU_LABEL}) ('
        f'rate(container_cpu_cfs_throttled_periods_total{{{_CPU_LABEL}!=""}}[5m])'
        f' / clamp_min(rate(container_cpu_cfs_periods_total{{{_CPU_LABEL}!=""}}[5m]), 1)'
        f') * 100',
        label=_CPU_LABEL)
    return {name: pct.get(name) or 0.0 for name in service_names}


def right_size(cpu_q, mem_max_bytes, node_cpu, node_mem,
               throttled_pct=0.0, cpu_limit_now=0.0):
    """
    (cpu_reservation, memory_reservation_mb, cpu_limit, memory_limit_mb).

    Clamped to a fraction of the SMALLEST node, because a reservation larger
    than any node can satisfy is not a sizing decision, it is an unschedulable
    task — the failure mode is a replica that sits Pending forever while the
    panel reports the component as down for no visible reason.

    The RESERVATION is what the service typically uses, and it decides packing
    and CPU shares. The LIMIT is what it may burst to, and it decides latency.
    They are different questions, and deriving the second from the first is what
    strangled an API for weeks — see THROTTLE_TARGET_PCT above. So when the
    kernel reports throttling, the limit is raised on that evidence instead, and
    it is never lowered while it is still the ceiling something is hitting.
    """
    cpu_res = max(CPU_RESERVE_FLOOR, cpu_q * CPU_RESERVE_HEADROOM)
    mem_res_mb = max(MEM_RESERVE_FLOOR_MB,
                     mem_max_bytes * MEM_RESERVE_HEADROOM / (1024 * 1024))

    cpu_cap = max(CPU_RESERVE_FLOOR, node_cpu / 1e9 * 0.5) if node_cpu else cpu_res
    mem_cap = (max(MEM_RESERVE_FLOOR_MB, node_mem / (1024 * 1024) * 0.5)
               if node_mem else mem_res_mb)
    cpu_res = min(cpu_res, cpu_cap)
    mem_res_mb = min(mem_res_mb, mem_cap)

    cpu_lim = cpu_res * CPU_LIMIT_MULTIPLE
    if throttled_pct > THROTTLE_TARGET_PCT:
        # One decisive step. Every resize restarts the service's replicas and
        # is gated behind an hour's cooldown, so crawling up 20% at a time would
        # spend most of a day at a cap we already know is too low.
        cpu_lim = max(cpu_lim, cpu_limit_now * CPU_LIMIT_RELIEF,
                      CPU_LIMIT_RELIEF_FLOOR)
    else:
        # Never walk a ceiling back down on quiet alone: throttling stopping is
        # what the raise was FOR, and treating it as proof the raise was
        # unnecessary would drop the cap, re-throttle the service, and oscillate
        # on an hourly cycle. An unused ceiling is free; a wrong one is not.
        cpu_lim = max(cpu_lim, cpu_limit_now)

    return (round(cpu_res, 3), int(mem_res_mb),
            round(min(cpu_lim, cpu_cap * 2), 3),
            int(min(mem_res_mb * MEM_LIMIT_MULTIPLE, mem_cap * 2)))


def _changed_enough(old, new):
    if not old:
        return True
    return abs(new - old) / old >= RESIZE_MIN_CHANGE


def apply_right_sizing(found, usage, node_cpu, node_mem):
    """
    Resize reservations to match measured usage. Returns the number applied.

    Deliberately does NOT touch a service whose rollout is in flight: a resize
    is itself an update, and stacking one on an in-progress rollout restarts it
    from the beginning.
    """
    applied = 0
    throttling = measure_throttling([w.name for w in found])
    for w in found:
        if w.rolling or w.name not in usage:
            continue
        cpu_q, mem_max = usage[w.name]
        cpu_res, mem_res, cpu_lim, mem_lim = right_size(
            cpu_q, mem_max, node_cpu, node_mem,
            throttling.get(w.name, 0.0), w.cpu_limit)

        now = time.time()
        if now - _last_resize.get(w.name, 0) < RESIZE_COOLDOWN_SECONDS:
            continue
        # The LIMIT counts as a change in its own right. It used to be a pure
        # function of the reservation, so testing the reservation alone was the
        # same test; now a throttled service can need a bigger ceiling while its
        # measured usage — capped by that very ceiling — has not moved at all.
        if not (_changed_enough(w.cost.cores, cpu_res)
                or _changed_enough(w.cost.mb, mem_res)
                or _changed_enough(w.cpu_limit, cpu_lim)):
            continue

        if DRY_RUN:
            log.info("[dry-run] would resize %s to %.3f CPU / %dMB reserved, "
                     "cap %.3f -> %.3f CPU (measured %.3f CPU / %dMB, "
                     "throttled %.1f%% of periods)",
                     w.name, cpu_res, mem_res, w.cpu_limit, cpu_lim,
                     cpu_q, int(mem_max / (1024 * 1024)),
                     throttling.get(w.name, 0.0))
            _last_resize[w.name] = now
            continue

        cmd = ["docker", "service", "update", "--detach=true",
               "--reserve-cpu", str(cpu_res), "--reserve-memory", f"{mem_res}M",
               "--limit-cpu", str(cpu_lim), "--limit-memory", f"{mem_lim}M",
               w.name]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            M_ERRORS.labels(stage="resize").inc()
            log.warning("could not resize %s: %s", w.name,
                        (proc.stderr or proc.stdout).strip()[:200])
            continue
        _last_resize[w.name] = now
        applied += 1
        M_EVENTS.labels(direction="resize").inc()
        # The CAP is in this line because its absence is how the original
        # problem stayed hidden: the reservation moved around plausibly for
        # weeks while the ceiling that was costing half a second a request was
        # never printed anywhere.
        log.info("resized %s: %.2f -> %.3f CPU, %d -> %dMB reserved, "
                 "cap %.3f -> %.3f CPU (measured q%d %.3f CPU, peak %dMB, "
                 "throttled %.1f%% of periods)",
                 w.name, w.cost.cores, cpu_res, int(w.cost.mb), mem_res,
                 w.cpu_limit, cpu_lim,
                 int(USAGE_CPU_Q * 100), cpu_q, int(mem_max / (1024 * 1024)),
                 throttling.get(w.name, 0.0))
    return applied


#: How long to leave a forced re-placement alone before trying another. Long
#: enough that a rollout which is simply slow is never nudged twice.
HANDOVER_NUDGE_COOLDOWN = 600
_last_handover_nudge = {}


def handover_nudge(names, found):
    """
    Force a rolling re-placement of services that should have moved but did not.

    Returns the names actually nudged.

    `docker service update --force` recreates every task with an unchanged
    spec, which is the only way to make Swarm reconsider placement — there is no
    "rebalance" verb, and a constraint that has already been removed will not
    move a task that is already running. Detached, because the rollout takes
    longer than a loop and the next loop reads live state anyway; the scale-down
    step refuses to drain a node while a rollout is in flight, so the two cannot
    collide.
    """
    if DRY_RUN:
        log.info("[dry-run] would force a re-placement of %s", ", ".join(names))
        return list(names)

    now = time.time()
    by_name = {w.name: w for w in found}
    nudged = []
    for name in names:
        if now - _last_handover_nudge.get(name, 0) < HANDOVER_NUDGE_COOLDOWN:
            continue
        workload = by_name.get(name)
        if workload is not None and workload.rolling:
            continue          # already moving; nudging again restarts it
        proc = subprocess.run(
            ["docker", "service", "update", "--detach=true", "--force", name],
            capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            M_ERRORS.labels(stage="handover").inc()
            log.warning("could not force a re-placement of %s: %s", name,
                        (proc.stderr or proc.stdout).strip()[:200])
            continue
        _last_handover_nudge[name] = now
        nudged.append(name)
        M_EVENTS.labels(direction="handover-nudge").inc()
    return nudged


def set_replicas(name, count):
    if DRY_RUN:
        log.info("[dry-run] would set %s to %d replicas", name, count)
        return
    dkr.services.get(name).scale(count)
    M_EVENTS.labels(direction=f"replicas-{'up' if count else 'down'}").inc()
    log.info("scaled %s to %d replicas", name, count)

# ---------------------------------------------------------------------------
# what this loop publishes
# ---------------------------------------------------------------------------

def export_service_metrics(found, targets, verdicts, running, pending,
                           uncapped=None):
    """
    The per-service series, unchanged in name and meaning.

    The NUMBERS now arrive in the dispatched verdict rather than being queried
    here — which is why the verdict carries them at all. Keeping the metric
    names is deliberate: the panel and every alert join on them, and moving a
    control loop is not a reason to break a dashboard.
    """
    forget_vanished({w.name for w in found})
    uncapped = uncapped or {}
    for w in found:
        v = verdicts.get(w.name) or {}
        if v.get("latency_ms") is not None:
            S_P95.labels(w.name).set(v["latency_ms"])
        if v.get("cpu_pct") is not None:
            S_CPU.labels(w.name).set(v["cpu_pct"])
        if v.get("mem_pct") is not None:
            S_MEM.labels(w.name).set(v["mem_pct"])
        # Reported for every scaler, every loop, present or not — an absent
        # series cannot alert, which is the whole point.
        S_CPU_SIGNAL.labels(w.name).set(
            1 if (w.policy.autoscale and v.get("cpu_pct") is not None) else 0)
        S_SLO.labels(w.name).set(w.policy.slo_ms)
        S_CURRENT.labels(w.name).set(w.spec_replicas)
        # WHAT THE SIGNALS ASKED FOR, which is what this gauge is named for and
        # is not the ceiling. It used to report `replica_ceiling` — the number
        # of replicas the NODES could hold — so a service sitting calmly at 2 on
        # a master with room for 3 published "desired 3" and read, to anyone
        # looking at the panel, as an autoscaler about to spawn another
        # container. Nothing wanted a third: the overseer's verdict was `hold`,
        # its own wanted-replicas gauge said 2, and admitted, running and
        # current all said 2.
        #
        # The ceiling is already published by the overseer as
        # `overseer_service_replica_ceiling`, and next to it as
        # `overseer_service_rollout_ceiling`. Repeating it here under a name
        # that means something else made the panel disagree with itself.
        S_DESIRED.labels(w.name).set(uncapped.get(w.name, w.spec_replicas))
        S_ADMITTED.labels(w.name).set(targets.get(w.name, w.spec_replicas))
        S_RUNNING.labels(w.name).set(running.get(w.name, 0))
        S_ROLLBACK.labels(w.name).set(1 if w.rolled_back else 0)
        S_PENDING.labels(w.name).set(pending.get(w.name, 0))
        S_MIN.labels(w.name).set(w.policy.min_replicas)
        S_MAX.labels(w.name).set(w.policy.max_replicas)
        S_AUTO.labels(w.name).set(1 if w.policy.autoscale else 0)
        S_PINNED.labels(w.name).set(1 if w.pinned else 0)
        S_COST_CPU.labels(w.name).set(w.cost.cores)
        S_COST_MEM.labels(w.name).set(w.cost.mem)


# ---------------------------------------------------------------------------
# what a verdict becomes
# ---------------------------------------------------------------------------

#: The recommendation history behind a shrink, mirroring the overseer's. Both
#: run the same function over the same numbers, so they agree about when a
#: smaller count has been the answer for long enough; and because the record is
#: the RAW recommendation, neither can feed its own damping back into itself.
_stabilizer = workloads.Stabilizer()


def target_replicas(workload, verdict):
    """
    The count to write, from the dispatched direction and ceiling.

    Two clamps, in this order, and the order is the whole thing:

      1. the SERVICE's own bounds, because a policy is a promise to the person
         who wrote it;
      2. the overseer's CEILING, because a replica that does not fit is a task
         that sits pending forever and looks like a healthy scale-up.

    The ceiling caps GROWTH only. It is never allowed below what is already
    running: a node going away must shed replicas through the removal path,
    which drains gracefully, and never by this loop discovering that four no
    longer fit and cutting to two in one step.
    """
    policy = workload.policy
    current = workload.spec_replicas
    if not verdict:
        return current
    raw = workloads.bounded(policy, workloads.desired_replicas(
        policy, verdict.get("direction"), current,
        held=(verdict.get("latency_ms"), verdict.get("cpu_pct"),
              verdict.get("mem_pct")),
        # Absent from a verdict an older overseer sent, which reads as "no
        # measurement" and falls back to the single-replica step this used to
        # always take. That is what makes the two deployable in either order.
        peak=(verdict.get("latency_peak_ms"), verdict.get("cpu_peak_pct"),
              verdict.get("mem_peak_pct"))))
    # A smaller count has to keep being the answer before it is written. Growth
    # is never delayed — being slow to add capacity is an outage, being slow to
    # remove it is a bill.
    want = _stabilizer.stabilise(workload.name, raw, current,
                                 policy.stabilize_down, time.time())
    _wanted[workload.name] = want
    ceiling = verdict.get("replica_ceiling")
    if ceiling is None:
        return want
    return max(min(want, int(ceiling)), min(current, want))


def _say_why(workload, verdict, target):
    current = workload.spec_replicas
    reason = (verdict or {}).get("reason") or "dispatched"
    if target != current:
        log.info("%s: replicas %d -> %d: %s", workload.name, current, target, reason)
        return
    direction = (verdict or {}).get("direction")
    if direction == classify.DIRECTION_HOLD and reason != "dispatched":
        # The overseer said slow-but-not-ours. Said once rather than every
        # loop; WHICH dependency is its answer, published as `overseer_signal`.
        warn_once((workload.name, "throttled"),
                  "%s: %s. Cause: %s%s.", workload.name, reason,
                  (verdict or {}).get("cause") or "unknown",
                  f" ({verdict['target']})" if (verdict or {}).get("target") else "")


def refuse_if_managed(service_name):
    """
    True when something else owns this service's shape.

    Read from the LIVE service rather than from the workload list, because the
    list only contains `infra.workload=app` services and this guard exists for
    the case where one is mislabelled. See workloads.MANAGED_BY_LABEL for what
    that costs if it is wrong.
    """
    try:
        service = dkr.services.get(service_name)
    except Exception:                                            # noqa: BLE001
        return False
    if not workloads.managed_by_dataguard(service):
        return False
    M_REFUSED.labels(reason="dataguard").inc()
    warn_once((service_name, "managed"),
              "%s carries %s=%s AND %s=%s. Refusing to change it: this loop scales "
              "by replica count, and a second replica of a database is a second "
              "server writing to one data directory. Remove one of the two labels.",
              service_name, workloads.MANAGED_BY_LABEL, workloads.MANAGED_BY_DATAGUARD,
              workloads.WORKLOAD_LABEL, workloads.WORKLOAD_APP)
    return True


def nudge_stalled_handovers(found, tasks_by_node, manager_id):
    """
    Force a re-placement for a service that was unpinned and never moved.

    RELEASING A PIN MOVES NOTHING. Swarm places a task when it is created and
    never rebalances a running one, so a replica whose constraint was lifted
    stays exactly where it is — and the overseer, which will not delete the last
    worker until it sees a task on the master, waits for an event that cannot
    happen on its own. It waited 37 minutes once, until an unrelated resize
    recreated the tasks and they landed on the master by accident.

    A forced rolling update is the one thing that redeploys tasks onto the
    now-eligible master, and it is graceful: start-first, health-gated, one
    replica at a time, exactly like a deploy.
    """
    on_manager = manager_running_by_service(tasks_by_node, manager_id)
    now = time.time()
    stalled = []
    for w in found:
        if w.pinned or w.rolling or w.spec_replicas < 1:
            _unpinned_since.pop(w.name, None)
            continue
        if on_manager.get(w.id, 0) >= 1:
            _unpinned_since.pop(w.name, None)
            continue
        since = _unpinned_since.setdefault(w.name, now)
        if now - since >= HANDOVER_STALL_SECONDS:
            stalled.append(w.name)
    if not stalled:
        return []
    M_ERRORS.labels(stage="handover").inc()
    nudged = handover_nudge(stalled, found)
    log.error("handover stalled: every pin is released and still no replica on the "
              "master for %s. %s", ", ".join(stalled),
              "Forcing a rolling re-placement onto the master."
              if nudged else "Check `docker service ps` for a reservation that no "
                             "longer fits.")
    for name in nudged:
        _unpinned_since[name] = now
    return nudged


def loop():
    # 1. DISCOVERY. An API error must never be read as "no work to do".
    found, _services, ok = discover_workloads()
    if not ok:
        log.warning("holding: the service list is unreadable")
        return
    M_MANAGED.set(len(found))
    # Bounded: a component that is deleted must not keep a shrink history alive
    # for the lifetime of the process.
    _stabilizer.forget({w.name for w in found})

    # 2. INVENTORY, for the two things this loop reads Swarm placement for:
    #    the stalled-handover nudge and the running/pending counts.
    try:
        manager_node = get_manager_node()
        manager_id = manager_node.id if manager_node else manager_node_id()
        tasks_by_node = index_tasks()
    except Exception as exc:  # noqa: BLE001
        M_ERRORS.labels(stage="inventory").inc()
        log.error("cannot read the task inventory; holding: %s", exc)
        return
    running, pending = running_and_pending(tasks_by_node, found)

    # 3. THE DISPATCHED WORLD. A service with no fresh verdict HOLDS — that is
    #    the whole shape of the split.
    verdicts = {w.name: dispatched_for(w.name) for w in found}
    waiting = [w.name for w in found if w.policy.autoscale and not verdicts[w.name]]
    if waiting:
        warn_once(("nodispatch", ",".join(sorted(waiting))),
                  "no fresh verdict for %s; holding their replica counts. Is the "
                  "overseer running, and does this service carry %s=%s?",
                  ", ".join(sorted(waiting)), classify.HANDLER_LABEL, classify.CAUSE_LOCAL)
    M_DISPATCH_WAITING.set(len(waiting))

    # 4. REPLICAS, per service, each with its own cooldown.
    targets = {}
    for w in found:
        verdict = verdicts[w.name]
        try:
            target = target_replicas(w, verdict)
        except Exception as exc:  # noqa: BLE001
            M_ERRORS.labels(stage="signals").inc()
            log.warning("%s holds at %d: %s", w.name, w.spec_replicas, exc)
            target = w.spec_replicas
        targets[w.name] = target
        _say_why(w, verdict, target)
        if target == w.spec_replicas:
            continue
        since = time.time() - _last_replica_change.get(w.name, 0.0)
        if since < w.policy.cooldown:
            log.info("%s: replica change suppressed, %.0fs since last", w.name, since)
            targets[w.name] = w.spec_replicas
            continue
        if refuse_if_managed(w.name):
            targets[w.name] = w.spec_replicas
            continue
        try:
            set_replicas(w.name, target)
            _last_replica_change[w.name] = time.time()
        except Exception as exc:  # noqa: BLE001
            M_ERRORS.labels(stage="scale").inc()
            log.error("could not scale %s: %s", w.name, exc)
            targets[w.name] = w.spec_replicas

    # 5. PLACEMENT, from the dispatched `pinned`. The DECISION is the overseer's
    #    — it is the one that knows whether the workers can hold everything —
    #    and applying it is this loop's, because it owns service specs.
    #
    #    A mixed dispatch is not possible by construction (the overseer sends
    #    one fleet-wide answer), but it is read per service anyway so a partial
    #    delivery cannot half-apply a handover.
    wanted_pins = {w.name: verdicts[w.name].get("pinned") for w in found
                   if verdicts[w.name] is not None}
    if wanted_pins:
        for want in (True, False):
            names = [n for n, p in wanted_pins.items() if p is want]
            if not names:
                continue
            group = [w for w in found if w.name in names]
            reconcile_placement(
                group, want,
                "the overseer says the workers can hold every replica" if want
                else "the overseer is scaling the fleet in; the master takes them back")

    # 6. The nudge, for a pin released long ago that Swarm never acted on.
    try:
        nudge_stalled_handovers(found, tasks_by_node, manager_id)
    except Exception as exc:  # noqa: BLE001
        M_ERRORS.labels(stage="handover").inc()
        log.warning("handover check skipped this loop: %s", exc)

    # 7. RIGHT-SIZE, last. A resize is itself a rolling update of every replica,
    #    so it runs after the replica count and the placement have settled —
    #    stacking it on either restarts that rollout from the beginning. The
    #    numbers it writes are picked up by the overseer's NEXT loop, which is
    #    what makes the correction visible in demand and fleet sizing.
    if RIGHT_SIZE and found:
        try:
            nodes = dkr.nodes.list()
            sizes = [workloads.node_resources(n) for n in nodes
                     if n.attrs.get("Status", {}).get("State") == "ready"]
            smallest_cpu = min((s.cpu for s in sizes), default=0)
            smallest_mem = min((s.mem for s in sizes), default=0)
            apply_right_sizing(found, measure_usage([w.name for w in found]),
                               smallest_cpu, smallest_mem)
        except Exception as exc:  # noqa: BLE001
            M_ERRORS.labels(stage="resize").inc()
            log.warning("right-sizing skipped this loop: %s", exc)

    export_service_metrics(found, targets, verdicts, running, pending, _wanted)

    for w in found:
        v = verdicts.get(w.name) or {}
        log.info("  %-28s %d/%d replicas (ceiling %s) · latency %s · cpu/replica %s · %s · %s",
                 w.name, running.get(w.name, 0), targets.get(w.name, w.spec_replicas),
                 v.get("replica_ceiling", "n/a"),
                 f"{v['latency_ms']:.0f}ms" if v.get("latency_ms") is not None else "n/a",
                 f"{v['cpu_pct']:.0f}%" if v.get("cpu_pct") is not None else "n/a",
                 "workers" if w.pinned else "master",
                 v.get("direction") or "no verdict")


def main():
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    start_http_server(9200)
    serve_signals()
    log.info("autoscaler up — cluster=%s dry_run=%s", CLUSTER, DRY_RUN)
    log.info("it applies what the overseer decides: replica counts, placement and "
             "measured reservations. It holds no Hetzner token and cannot change "
             "the fleet.")
    log.info("targets are DISCOVERED: any service labelled %s=%s is managed, and its "
             "policy comes from its own autoscale.* labels.",
             workloads.WORKLOAD_LABEL, workloads.WORKLOAD_APP)
    while _running:
        started = time.time()
        try:
            loop()
        except Exception as exc:  # noqa: BLE001
            M_ERRORS.labels(stage="loop").inc()
            log.exception("loop failed: %s", exc)
        M_LOOP.set(time.time())
        time.sleep(max(1, LOOP_SECONDS - (time.time() - started)))
    log.info("exiting cleanly")


if __name__ == "__main__":
    main()
