#!/usr/bin/env python3
"""
Two-tier autoscaler for Docker Swarm on Hetzner Cloud.

WHAT IT SCALES IS DISCOVERED, NOT CONFIGURED
--------------------------------------------
There is no APP_SERVICE. Every service carrying the deploy label
`infra.workload=app` is an application workload: this loop owns its placement,
counts it when sizing the fleet, and — if it also carries `autoscale.enabled` —
owns its replica count. Its whole policy (SLO, replica bounds, sustain windows,
thresholds) travels with it as `autoscale.*` labels on that same service, so a
second application is a component someone created, not an edit to this file, the
monitoring stack and infra.env.

Anything without that label is OVERHEAD: never pinned, never scaled, and its
reservations are simply subtracted from whatever node it sits on. That is the
correct default, and it is why creating a Redis needs no change here at all.
The failure modes are asymmetric on purpose — a forgotten label leaves an app on
the master, correctly accounted and visible; a label on a stateful service would
move a volume onto a worker that later gets deleted.

POLICY
------
Load is absorbed in two stages, cheapest first:

  1. REPLICAS. Adding a task to existing capacity takes seconds. Each service's
     desired count is driven by its own p95 latency against its own SLO, with
     CPU-per-replica as the secondary signal.
  2. NODES. Only when what we want will not fit on the capacity we have.
     Provisioning takes ~2 minutes including application warmup.

Coming down, the order reverses: shed replicas first, remove the node later and
slowly.

WANT, HOSTS, ADMITTED — three numbers, not one
----------------------------------------------
  want      what each service's signals ask for.
  hosts     derived from the UNCAPPED total of those wants. This is what makes
            the fleet grow toward real demand.
  admitted  what is actually written to Swarm this loop, capped at what the
            currently eligible nodes can hold.

Collapsing want and admitted into one variable deadlocks with several services:
each gets capped to fit, the total never exceeds capacity, and no worker is ever
bought. Admission caps GROWTH only — it never scales anything down. Shrinking is
tier 1's job and the node-removal path's job.

THE MASTER IS NOT A WORKER
--------------------------
`MIN_WORKERS`/`MAX_WORKERS` count Hetzner worker servers, and the master is not
one of them. `MIN_WORKERS=0` is therefore the free floor: no server is billed,
and the master carries the load itself. Applications live in one of two places,
and exactly one at a time:

  MANAGER MODE   zero workers. App services carry no role constraint and run on
                 the master alongside monitoring, the databases and the panel.
  WORKER MODE    one or more workers. Every app service carries
                 `node.role == worker` and the master goes back to being a pure
                 control plane carrying no application replicas at all.

So `MIN_WORKERS=1` means "always keep one worker", which also means the master
never runs application traffic — there is no separate switch for that.

CAPACITY IS MEASURED, NOT CONFIGURED
------------------------------------
No REPLICAS_PER_WORKER, no manager-capacity constant, no headroom constant. Every
quantity is a (cpu, memory) vector in nanocores and bytes:

  a node's free capacity  = what it advertises, minus the reservations of every
                            task on it that is NOT an app workload
  demand                  = sum over app services of replicas x that service's
                            own reservation
  a new worker            = the Hetzner catalogue entry for WORKER_TYPE, minus
                            the per-node reservations of the `mode: global`
                            services

Integers throughout: summing 0.05 + 0.10 + 0.05 as floats and then comparing
demand <= free is how a packing loop acquires a one-replica flap nobody can
reproduce.

The unit is the RESERVATION, not the limit, because reservations are what
Swarm's scheduler actually subtracts when placing a task.

ONE PACKER, USED THREE TIMES
----------------------------
`place()` — first-fit over a round-robin item stream — answers admission, fleet
sizing and node removal. Two algorithms that disagree by one replica are a loop
that buys a worker and immediately deletes it. Its output is a pure function of
(labels, live specs, node inventory, counts), which is the whole anti-oscillation
argument.

THE HANDOVER MUST NEVER LEAVE A GAP
-----------------------------------
Both directions are ordered so a healthy replica is serving at every instant:

  scaling out (manager -> workers)
    1. create workers, holding replicas at what the MASTER can serve while they
       boot. The master keeps serving throughout.
    2. wait until workers are `ready` AND can hold every app replica.
    3. only then add the constraint. start-first replaces the master's tasks one
       at a time, new-before-old.

  scaling in (workers -> manager)
    1. release the constraint FIRST, while the last worker is still serving.
    2. wait until a replica of EVERY app service is running on the master.
    3. only then drain and delete the last worker.

Reversing either order produces the outage. Every step is re-derived from live
state each loop, so a crash mid-handover resumes rather than half-applying.

cloudflared is the one exception: `mode: global`, no constraint, so the master
always has a registered connector and the tunnel never gaps mid-handover.

WHY THESE SIGNALS
-----------------
p95 latency is what your users feel; node CPU is not. Node CPU averages in the
log driver, the exporters, the tunnel connector and every other component, none
of which should influence one application's capacity. CPU-per-replica is the
resource-bound backstop, and raw node CPU is used only to decide whether another
replica can physically fit.

WHY THE COOLDOWNS ARE ASYMMETRIC
--------------------------------
Scaling up late costs user-visible latency. Scaling down late costs pennies.
COOLDOWN_UP must exceed provision time plus warmup, or the loop provisions again
while the last node is still warming and badly overshoots.

STATELESS BY DESIGN. Cooldowns derive from Hetzner creation timestamps and
sustain windows from VictoriaMetrics subqueries, so restarting this container
loses nothing. HORIZONTAL ONLY — no rescale calls; Hetzner rescale power-cycles
the server and a grown disk can never shrink.
"""

import logging
import math
import os
import re
import signal
import subprocess
import sys
import time
from collections import namedtuple
from datetime import datetime, timezone

import docker
import requests
from hcloud import Client
from hcloud.images import Image
from hcloud.locations import Location
from hcloud.networks import Network
from hcloud.server_types import ServerType
from hcloud.ssh_keys import SSHKey
from prometheus_client import Counter, Gauge, start_http_server

# ---------------------------------------------------------------------------
# config — cluster-wide only
# ---------------------------------------------------------------------------
# Nothing here names an application, and nothing here is a scaling threshold.
# Those belong to the service they apply to, as labels. What is left is the
# fleet: how many machines may exist, of what kind, and how eagerly.


def _env(key, default=None, cast=str):
    raw = os.environ.get(key, default)
    if raw is None:
        raise RuntimeError(f"missing required env var {key}")
    if cast is bool:
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    return cast(raw)


def _secret(name):
    path = f"/run/secrets/{name}"
    if os.path.exists(path):
        with open(path) as fh:
            return fh.read().strip()
    return _env(name)


CLUSTER = _env("APP_NAME", "app")
VM_URL = _env("VM_URL", "http://victoriametrics:8428")

# --- workers --------------------------------------------------------------
# Hetzner worker servers. The master is NOT one of them — it is the control
# plane, and it happens to carry the load while no worker exists.
#
#   MIN_WORKERS = 0  ->  no server billed; the master runs your components
#   MIN_WORKERS = 1  ->  always one worker, so the master runs no application
#   MAX_WORKERS = 5  ->  at most five workers. A BUDGET cap, not a capacity plan.
MIN_WORKERS = _env("MIN_WORKERS", "0", int)
MAX_WORKERS = _env("MAX_WORKERS", "5", int)

# Placement guard, not a trigger. If a node is this loaded on either CPU or
# memory, another replica will not fit on it, so a node is required.
NODE_PRESSURE_PCT = _env("NODE_PRESSURE_PCT", "80", float)

# COOLDOWN_UP >= node boot + image pull + app warmup, or you will overshoot.
COOLDOWN_UP = _env("COOLDOWN_UP_SECONDS", "300", int)
COOLDOWN_DOWN = _env("COOLDOWN_DOWN_SECONDS", "900", int)

SCHEDULE_FLOOR = _env("SCHEDULE_FLOOR", "")
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
ROTATE_TOKEN = _env("ROTATE_TOKEN_ON_SCALE_DOWN", "false", bool)

HCLOUD_TOKEN = _secret("HCLOUD_TOKEN")
LOCATION = _env("HCLOUD_LOCATION", "hel1")
NETWORK_NAME = _env("HCLOUD_NETWORK_NAME", "prod-net")
SSH_KEY_NAME = _env("HCLOUD_SSH_KEY_NAME", "")
WORKER_IMAGE = _env("WORKER_IMAGE", "ubuntu-24.04")
WORKER_TYPE = _env("WORKER_TYPE", "cpx21")
USERDATA_PATH = _env("WORKER_USERDATA_PATH", "/etc/infra/worker-cloud-init.yaml")

ORPHAN_GRACE_SECONDS = 900
POST_DRAIN_GRACE = _env("POST_DRAIN_GRACE_SECONDS", "45", int)
HANDOVER_STALL_SECONDS = 900

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("autoscaler")

if MIN_WORKERS < 0:
    log.warning("MIN_WORKERS=%d is negative; using 0", MIN_WORKERS)
    MIN_WORKERS = 0
if MAX_WORKERS < MIN_WORKERS:
    log.warning("MAX_WORKERS=%d is below MIN_WORKERS=%d; using %d",
                MAX_WORKERS, MIN_WORKERS, MIN_WORKERS)
    MAX_WORKERS = MIN_WORKERS


# ---------------------------------------------------------------------------
# labels: the contract with whatever created the component
# ---------------------------------------------------------------------------

WORKLOAD_LABEL = "infra.workload"
WORKLOAD_APP = "app"
COMPONENT_LABEL = "infra.component"

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


def _label_num(labels, key, default, cast, lo, hi, service):
    raw = labels.get(key)
    if raw is None:
        return default
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        M_ERRORS.labels(stage="policy").inc()
        warn_once((service, key, raw), "%s: %s=%r is not a number; using %s",
                  service, key, raw, default)
        return default
    if not (lo <= value <= hi):
        M_ERRORS.labels(stage="policy").inc()
        warn_once((service, key, raw), "%s: %s=%s is outside %s..%s; using %s",
                  service, key, value, lo, hi, default)
        return default
    return value


def _label_bool(labels, key, default=False):
    raw = labels.get(key)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


Policy = namedtuple("Policy", [
    "autoscale", "min_replicas", "max_replicas", "slo_ms", "up_ratio", "down_ratio",
    "up_cpu", "down_cpu", "sustain_up", "sustain_down", "up_factor", "cooldown",
    "priority", "histogram", "unit", "histogram_explicit",
])


def policy_from_labels(service_name, labels, spec_replicas):
    """
    A service's scaling policy, read from its own deploy labels.

    NEVER RAISES. This is the boundary between operator input and the loop, so
    every field falls back to a default and complains rather than taking the
    cluster down with a typo.

    A service with `infra.workload=app` and no `autoscale.enabled` is a
    fixed-replica application: still discovered, still pinned with the others,
    still counted in demand, never scaled. Its bounds are its live replica count
    read fresh each loop, so scaling it by hand is respected rather than fought.
    """
    try:
        enabled = _label_bool(labels, "autoscale.enabled", False)
        if not enabled:
            fixed = max(0, int(spec_replicas or 0))
            return Policy(False, fixed, fixed, 500.0, 0.8, 0.4, 70.0, 30.0,
                          90, 900, 0.5, 60, 100, "", "seconds", False)

        lo = _label_num(labels, "autoscale.min_replicas", 1, int, 0, 100, service_name)
        hi = _label_num(labels, "autoscale.max_replicas", lo, int, 0, 100, service_name)
        if hi < lo:
            warn_once((service_name, "bounds", f"{lo}-{hi}"),
                      "%s: max_replicas %d is below min_replicas %d; using %d for both",
                      service_name, hi, lo, lo)
            hi = lo
        if hi == lo and enabled:
            warn_once((service_name, "noop", f"{lo}"),
                      "%s: autoscaling is on but min == max == %d, so nothing can "
                      "move. Set a range, or turn autoscaling off.", service_name, lo)

        up_ratio = _label_num(labels, "autoscale.up_p95_ratio", 0.8, float, 0.01, 2.0, service_name)
        down_ratio = _label_num(labels, "autoscale.down_p95_ratio", 0.4, float, 0.01, 2.0, service_name)
        up_cpu = _label_num(labels, "autoscale.up_cpu_pct", 70.0, float, 1.0, 200.0, service_name)
        down_cpu = _label_num(labels, "autoscale.down_cpu_pct", 30.0, float, 1.0, 200.0, service_name)
        # Repairing only one side would produce a config nobody wrote, so a
        # crossed pair reverts both.
        if down_ratio >= up_ratio:
            warn_once((service_name, "ratios", f"{up_ratio}/{down_ratio}"),
                      "%s: scale-down p95 ratio %.2f is not below scale-up %.2f; "
                      "using the defaults for both", service_name, down_ratio, up_ratio)
            up_ratio, down_ratio = 0.8, 0.4
        if down_cpu >= up_cpu:
            warn_once((service_name, "cpus", f"{up_cpu}/{down_cpu}"),
                      "%s: scale-down CPU %.0f%% is not below scale-up %.0f%%; "
                      "using the defaults for both", service_name, down_cpu, up_cpu)
            up_cpu, down_cpu = 70.0, 30.0

        sustain_up = _label_num(labels, "autoscale.sustain_up_seconds", 90, int, 30, 3600, service_name)
        sustain_down = _label_num(labels, "autoscale.sustain_down_seconds", 900, int, 60, 86400, service_name)
        if sustain_down < sustain_up:
            warn_once((service_name, "sustain", f"{sustain_up}/{sustain_down}"),
                      "%s: sustain_down %ds is shorter than sustain_up %ds, which "
                      "inverts the up-fast/down-slow asymmetry this loop relies on",
                      service_name, sustain_down, sustain_up)

        # No default metric name any more. The old one was
        # `http_server_requests_seconds_bucket`, a Spring convention that
        # silently matched nothing for every other framework — and an empty
        # histogram_quantile is indistinguishable from an idle service, so it
        # never looked wrong. Absent this label the metric is DISCOVERED from
        # what the service actually publishes; see discover_latency().
        histogram = labels.get("autoscale.p95_histogram") or ""
        histogram_explicit = bool(histogram)
        if histogram and not re.match(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$", histogram):
            warn_once((service_name, "metric", histogram),
                      "%s: %r is not a metric name; discovering one instead",
                      service_name, histogram)
            histogram, histogram_explicit = "", False
        unit = labels.get("autoscale.p95_unit", "seconds")
        if unit not in ("seconds", "milliseconds"):
            unit = "seconds"

        return Policy(
            True, lo, hi,
            _label_num(labels, "autoscale.slo_p95_ms", 500.0, float, 1.0, 600000.0, service_name),
            up_ratio, down_ratio, up_cpu, down_cpu, sustain_up, sustain_down,
            _label_num(labels, "autoscale.up_factor", 0.5, float, 0.01, 4.0, service_name),
            _label_num(labels, "autoscale.cooldown_seconds", 60, int, 0, 3600, service_name),
            _label_num(labels, "autoscale.priority", 100, int, 0, 1000, service_name),
            histogram, unit, histogram_explicit,
        )
    except Exception as exc:  # noqa: BLE001
        M_ERRORS.labels(stage="policy").inc()
        log.warning("%s: could not read the scaling policy (%s); treating it as fixed",
                    service_name, exc)
        fixed = max(0, int(spec_replicas or 0))
        return Policy(False, fixed, fixed, 500.0, 0.8, 0.4, 70.0, 30.0, 90, 900,
                      0.5, 60, 100, "", "seconds", False)


# ---------------------------------------------------------------------------
# resources — integers, never floats
# ---------------------------------------------------------------------------

class Res(namedtuple("Res", ["cpu", "mem"])):
    """(nanocores, bytes). Integers so the packer cannot acquire a rounding flap."""

    __slots__ = ()

    def __add__(self, other):
        return Res(self.cpu + other.cpu, self.mem + other.mem)

    def __sub__(self, other):
        """Clamped at zero: negative free capacity is not a thing."""
        return Res(max(0, self.cpu - other.cpu), max(0, self.mem - other.mem))

    def fits_in(self, free):
        return self.cpu <= free.cpu and self.mem <= free.mem

    @property
    def cores(self):
        return self.cpu / 1e9

    @property
    def mb(self):
        return self.mem // (1024 * 1024)

    def __str__(self):
        return f"{self.cores:.2f} CPU / {self.mb}MB"


ZERO = Res(0, 0)
#: What a service with no reservations at all is charged. It is a lie either
#: way — Swarm will pack it anywhere — but a documented floor keeps it visible
#: in the arithmetic instead of making the node look free.
UNRESERVED_FLOOR = Res(int(0.1 * 1e9), 128 * 1024 * 1024)


def _reservations(spec_resources):
    res = (spec_resources or {}).get("Reservations") or {}
    return Res(int(res.get("NanoCPUs", 0) or 0), int(res.get("MemoryBytes", 0) or 0))


def _limits(spec_resources):
    lim = (spec_resources or {}).get("Limits") or {}
    return Res(int(lim.get("NanoCPUs", 0) or 0), int(lim.get("MemoryBytes", 0) or 0))


# ---------------------------------------------------------------------------
# metrics about ourselves
# ---------------------------------------------------------------------------
# THE RULE THAT KEEPS THE ALERTS WORKING: gauges an alert JOINS on stay
# unlabeled and cluster-scoped; only genuinely per-service quantities gain a
# `service` label.
#
# AppStrandedWithoutWorkers is `autoscaler_placement_worker_mode == 1 and
# autoscaler_current_workers < 1` — a set operation on the empty label
# signature. The moment one side gains a label it matches nothing and goes
# silent, which is exactly the class of failure that made NoHealthyReplicas
# dead for months. So the cluster gauge stays, and a labeled twin is added
# alongside it for per-service visibility.

M_CURRENT = Gauge("autoscaler_current_workers", "Hetzner worker servers in the swarm")
# Kept alongside _current_workers because "how many boxes am I running" is a
# different question from "how many am I paying for", and the panel shows both.
# Nothing keys a THRESHOLD on it any more: the ceiling is a worker count now, so
# AutoscalerAtMax compares workers to workers.
M_HOSTS = Gauge("autoscaler_current_hosts", "Boxes running applications, master included")
M_DESIRED = Gauge("autoscaler_desired_workers", "Host count the autoscaler wants")
M_MAX = Gauge("autoscaler_max_workers", "Configured host ceiling")
M_MIN = Gauge("autoscaler_effective_min_workers", "Host floor in force right now")
M_CPU = Gauge("autoscaler_cluster_cpu_percent", "Mean worker CPU utilisation")
M_MEM = Gauge("autoscaler_cluster_mem_percent", "Mean worker memory utilisation")
M_MANAGED = Gauge("autoscaler_managed_services", "Services carrying infra.workload=app")
M_MODE = Gauge("autoscaler_placement_worker_mode",
               "1 when ANY application is pinned to worker nodes, 0 when none is")
M_MIXED = Gauge("autoscaler_placement_mixed",
                "1 when applications disagree about placement — a handover in flight")
M_DEMAND_CPU = Gauge("autoscaler_demand_cpu_cores", "CPU reserved by all application replicas")
M_DEMAND_MEM = Gauge("autoscaler_demand_memory_bytes", "Memory reserved by all application replicas")
M_MGR_CPU = Gauge("autoscaler_manager_free_cpu_cores", "CPU free for applications on the master")
M_MGR_MEM = Gauge("autoscaler_manager_free_memory_bytes", "Memory free for applications on the master")
M_POOL_CPU = Gauge("autoscaler_worker_pool_free_cpu_cores", "CPU free for applications across ready workers")
M_POOL_MEM = Gauge("autoscaler_worker_pool_free_memory_bytes", "Memory free for applications across ready workers")
M_NEW_CPU = Gauge("autoscaler_new_worker_free_cpu_cores", "CPU a new worker would offer applications")
M_NEW_MEM = Gauge("autoscaler_new_worker_free_memory_bytes", "Memory a new worker would offer applications")
M_LOOP = Gauge("autoscaler_last_loop_timestamp_seconds", "Unix time of last completed loop")
M_EVENTS = Counter("autoscaler_scale_events_total", "Scaling actions taken", ["direction"])
M_ERRORS = Counter("autoscaler_errors_total", "Errors encountered", ["stage"])

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
#: nothing said so: the autoscaler logged "cpu/replica n/a" once a minute into a
#: log nobody reads, held every service at its current size, and kept a worker
#: alive against zero traffic for as long as it ran. The gauge makes that state
#: alertable instead of merely true.
S_CPU_SIGNAL = Gauge("autoscaler_service_cpu_signal_present",
                     "1 when CPU-per-replica is readable for this service", _SVC)
S_PENDING = Gauge("autoscaler_service_pending_replicas", "Tasks wanted but not placed", _SVC)
S_MIN = Gauge("autoscaler_service_min_replicas", "Configured replica floor", _SVC)
S_MAX = Gauge("autoscaler_service_max_replicas", "Configured replica ceiling", _SVC)
S_AUTO = Gauge("autoscaler_service_autoscale_enabled", "1 when the replica count is driven by signals", _SVC)
S_CPU = Gauge("autoscaler_service_cpu_per_replica_percent", "Mean CPU per replica, % of limit", _SVC)
S_PINNED = Gauge("autoscaler_service_worker_mode", "1 when this service is pinned to workers", _SVC)
S_STARVED = Gauge("autoscaler_service_min_unsatisfied", "1 when the cluster cannot host the minimum", _SVC)
S_UNPLACEABLE = Gauge("autoscaler_service_unplaceable", "1 when one replica exceeds any possible node", _SVC)
S_COST_CPU = Gauge("autoscaler_service_replica_cost_cpu_cores", "CPU one replica reserves", _SVC)
S_COST_MEM = Gauge("autoscaler_service_replica_cost_memory_bytes", "Memory one replica reserves", _SVC)

_PER_SERVICE = [S_P95, S_SLO, S_CURRENT, S_DESIRED, S_ADMITTED, S_RUNNING, S_PENDING,
                S_MIN, S_MAX, S_AUTO, S_CPU, S_PINNED, S_STARVED, S_UNPLACEABLE,
                S_COST_CPU, S_COST_MEM]
_exported_services = set()

hcloud = Client(token=HCLOUD_TOKEN)
dkr = docker.DockerClient(base_url="unix:///var/run/docker.sock")

_running = True
_last_scale_down = time.time()  # conservative: wait one cooldown after a restart
_last_replica_change = {}       # service name -> unix time
_manager_wait_since = None
_capacity_note = None


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
    _exported_services.intersection_update(current_names)
    _exported_services.update(current_names)


def _stop(signum, _frame):
    global _running
    log.info("received signal %s, finishing current loop then exiting", signum)
    _running = False


signal.signal(signal.SIGTERM, _stop)
signal.signal(signal.SIGINT, _stop)


# ---------------------------------------------------------------------------
# metric queries
# ---------------------------------------------------------------------------

def vm_query(expr):
    """Instant query against VictoriaMetrics. Returns float or None."""
    try:
        resp = requests.get(f"{VM_URL}/api/v1/query", params={"query": expr}, timeout=15)
        resp.raise_for_status()
        results = resp.json().get("data", {}).get("result", [])
        if not results:
            return None
        return float(results[0]["value"][1])
    except Exception as exc:  # noqa: BLE001
        M_ERRORS.labels(stage="query").inc()
        log.warning("query failed (%s): %s", expr[:60], exc)
        return None


def vm_query_map(expr, label="service"):
    """
    One query, many series. Returns {label value: float}.

    Every per-service signal is aggregated `by (service)` and read through this
    rather than issued once per service. Ten components x six queries at a 15s
    timeout does not fit in a 60s loop, and AutoscalerStalled fires at 300s.
    """
    out = {}
    try:
        resp = requests.get(f"{VM_URL}/api/v1/query", params={"query": expr}, timeout=15)
        resp.raise_for_status()
        for row in resp.json().get("data", {}).get("result", []):
            key = row.get("metric", {}).get(label)
            if key:
                out[key] = float(row["value"][1])
    except Exception as exc:  # noqa: BLE001
        M_ERRORS.labels(stage="query").inc()
        log.warning("grouped query failed (%s): %s", expr[:60], exc)
    return out


_SEL = 'node_role="worker"'
_MGR = 'node_role="manager"'

# PLACEMENT GUARD ONLY: is there physical room for another replica? Never a
# scaling trigger — it averages in everything running on the box.
CPU_EXPR = f'100 - (avg(rate(node_cpu_seconds_total{{mode="idle",{_SEL}}}[5m])) * 100)'
MEM_EXPR = (f'avg(100 * (1 - node_memory_MemAvailable_bytes{{{_SEL}}}'
            f' / node_memory_MemTotal_bytes{{{_SEL}}}))')
# The same guard for the manager, used ONLY when the worker fleet is empty: with
# zero workers the worker-scoped queries return no series at all, so without
# this there is nothing to notice that the box carrying the replicas is full.
MGR_CPU_EXPR = f'100 - (avg(rate(node_cpu_seconds_total{{mode="idle",{_MGR}}}[5m])) * 100)'
MGR_MEM_EXPR = (f'avg(100 * (1 - node_memory_MemAvailable_bytes{{{_MGR}}}'
                f' / node_memory_MemTotal_bytes{{{_MGR}}}))')

# CPU per replica, for every scraped service at once. Divided by each service's
# own limit locally, which is what finally removes APP_CPU_LIMIT.
_CPU_LABEL = "container_label_com_docker_swarm_service_name"
CPU_BY_SERVICE = (f'avg by ({_CPU_LABEL}) (rate(container_cpu_usage_seconds_total'
                  f'{{{_CPU_LABEL}!=""}}[3m]))')


def p95_expr(histogram, unit):
    """p95 by service, in milliseconds. `service` is written by vmagent."""
    scale = 1000 if unit == "seconds" else 1
    return (f"histogram_quantile(0.95, sum by (service, le) "
            f"(rate({histogram}[2m]))) * {scale}")


def mean_expr(base, unit):
    """
    Mean request latency by service, in milliseconds.

    Used when a service publishes a timer but no buckets. It is NOT a p95 and
    is not pretended to be one: the mean sits below the tail, so a service
    compared against a p95 SLO through this will scale up later than one with a
    real histogram. That is still enormously better than the alternative, which
    is no latency signal at all — and it is the common case, because a
    Micrometer/Prometheus timer publishes _sum and _count by default and
    publishes buckets only when someone explicitly enables them.
    """
    scale = 1000 if unit == "seconds" else 1
    return (f"(sum by (service) (rate({base}_sum[2m])) "
            f"/ sum by (service) (rate({base}_count[2m]))) * {scale}")


#: Name fragments that make a metric family look like HTTP server latency,
#: best first. Discovery ranks candidates by the first fragment they contain, so
#: an app publishing both client and server timers picks the server one.
_LATENCY_HINTS = ("http_server_requests", "http_server_request",
                  "http_server_duration", "http_request_duration",
                  "http_requests_duration", "request_duration",
                  "requests_seconds", "request_seconds")

#: Discovery is a metadata query, not a signal — the answer changes only when
#: someone ships a new framework, so it is cached and refreshed rarely.
_LATENCY_TTL_SECONDS = 900
_latency_cache = {}          # service -> (expr, unit, kind) or None
_latency_checked_at = 0.0


def _rank_latency(name):
    """Lower is better. None means it does not look like request latency."""
    lowered = name.lower()
    for i, hint in enumerate(_LATENCY_HINTS):
        if hint in lowered:
            return i
    return None


def _unit_of(name):
    return "milliseconds" if ("millis" in name or name.endswith("_ms")) else "seconds"


def discover_latency(service_names):
    """
    {service: (expr, kind)} for services whose latency metric we can find.

    Two metadata queries for the whole cluster, cached. This exists so a
    component does not have to be told the name of its own latency metric: the
    default was `http_server_requests_seconds_bucket`, a Spring convention, and
    a Ktor app publishing `ktor_http_server_requests_seconds` matched nothing —
    so p95 read n/a forever and only CPU could ever move the replica count.
    Nothing warned, because an empty histogram_quantile is also what an idle
    service looks like.
    """
    global _latency_checked_at
    now = time.time()
    if _latency_cache and now - _latency_checked_at < _LATENCY_TTL_SECONDS:
        return {k: v for k, v in _latency_cache.items() if v}

    wanted = set(service_names)
    found = {}

    # Real histograms first — a true p95 always beats a mean.
    for suffix, kind in (("_bucket", "p95"), ("_count", "mean")):
        rows = vm_series_names(f'{{__name__=~".+{suffix}", service!=""}}')
        for metric, svc in rows:
            if svc not in wanted or svc in found:
                continue
            base = metric[: -len(suffix)]
            rank = _rank_latency(base)
            if rank is None:
                continue
            best = found.get(svc)
            if best and best[0] <= rank:
                continue
            unit = _unit_of(base)
            expr = (p95_expr(f"{base}_bucket", unit) if kind == "p95"
                    else mean_expr(base, unit))
            found[svc] = (rank, expr, kind, base)

    _latency_cache.clear()
    for svc in wanted:
        hit = found.get(svc)
        _latency_cache[svc] = (hit[1], hit[2], hit[3]) if hit else None
        if hit:
            log.info("%s: latency signal is %s from %s", svc, hit[2], hit[3])
        else:
            warn_once((svc, "nolatency"),
                      "%s publishes no recognisable request-latency metric; "
                      "scaling it on CPU alone", svc)
    _latency_checked_at = now
    return {k: v for k, v in _latency_cache.items() if v}


def vm_series_names(selector):
    """
    [(metric name, service)] for a selector, via the /series metadata endpoint.

    /series rather than an instant query: it returns label sets without values,
    so asking "which metrics does this cluster publish" does not also drag every
    sample back through the loop.
    """
    out = []
    try:
        resp = requests.get(f"{VM_URL}/api/v1/series",
                            params={"match[]": selector,
                                    "start": int(time.time()) - 3600},
                            timeout=15)
        resp.raise_for_status()
        for row in resp.json().get("data", []):
            name, svc = row.get("__name__"), row.get("service")
            if name and svc:
                out.append((name, svc))
    except Exception as exc:  # noqa: BLE001
        M_ERRORS.labels(stage="query").inc()
        log.warning("series lookup failed (%s): %s", selector[:60], exc)
    return out


def sustained(expr, window, aggregate):
    """
    Was `expr` continuously above (min_over_time) or below (max_over_time) for
    the whole window? A subquery, so no local state is needed.
    """
    step = max(15, window // 12)
    return f"{aggregate}(({expr})[{window}s:{step}s])"


# ---------------------------------------------------------------------------
# swarm + hetzner inventory
# ---------------------------------------------------------------------------

def swarm_workers():
    return [n for n in dkr.nodes.list()
            if n.attrs.get("Spec", {}).get("Role") == "worker"]


def swarm_ready_workers():
    return [n for n in swarm_workers()
            if n.attrs.get("Status", {}).get("State") == "ready"
            and n.attrs.get("Spec", {}).get("Availability") == "active"]


def hetzner_workers():
    return hcloud.servers.get_all(label_selector=f"cluster=={CLUSTER},role==swarm-worker")


def provisioning_workers():
    """
    Servers that exist and are being paid for but have not joined the swarm as
    ready yet — roughly two minutes of boot, cloud-init, docker and image pull.

    These MUST count towards the fleet. Sizing off swarm_ready_workers() alone
    makes a booting worker invisible, so every loop during that window sees the
    same shortfall and orders more capacity for it, sailing past the ceiling.
    """
    ready = {n.attrs["Description"]["Hostname"] for n in swarm_ready_workers()}
    return [s for s in hetzner_workers() if s.name not in ready]


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
# discovery
# ---------------------------------------------------------------------------

Workload = namedtuple("Workload", [
    "name", "id", "policy", "spec_replicas", "cost", "cpu_limit", "pinned",
    "rolling", "component", "rolled_back", "placement_pinned",
])

#: Set by a component whose placement was chosen by hand in the panel. The
#: autoscaler reads it and stops moving that service between master and workers.
PLACEMENT_PIN_LABEL = "infra.placement.pinned"

WORKER_CONSTRAINT = "node.role==worker"
# Swarm normalises whitespace differently across versions, so recognise any
# spelling rather than only ours.
_WORKER_PIN = re.compile(r"^\s*node\.role\s*==\s*worker\s*$")

MODE_MANAGER = "manager"
MODE_WORKER = "worker"


def _constraints(service):
    try:
        return (service.attrs["Spec"]["TaskTemplate"].get("Placement", {})
                .get("Constraints") or [])
    except (KeyError, TypeError):
        return []


def is_pinned(service):
    return any(_WORKER_PIN.match(c) for c in _constraints(service))


def update_in_progress(service):
    """
    Is Swarm still rolling this service? Issuing a second constraint change on
    top of an in-flight one restarts the rollout from the beginning.
    """
    state = (service.attrs.get("UpdateStatus") or {}).get("State")
    return state in ("updating", "rollback_started")


#: Swarm's terminal verdicts on a failed rollout. `paused` is included because
#: it is what a service with no rollback configured does instead — it stops
#: mid-update and waits, which looks identical to "deployed" from the outside.
ROLLBACK_STATES = ("paused", "rollback_started", "rollback_completed")


def update_rolled_back(service):
    """
    Did this service's last deploy fail and get reverted?

    Nothing else in the cluster reports this. `docker stack deploy` is detached
    by default, so it exits 0 the moment Swarm accepts the spec — long before
    the tasks fail and the rollback undoes them. The panel then records a
    successful deploy, the spec on disk still shows the change, and the running
    service quietly holds the previous one. A deploy that silently un-happened
    is exactly the class of failure this repo alerts on everywhere else.
    """
    return (service.attrs.get("UpdateStatus") or {}).get("State") in ROLLBACK_STATES


def discover_workloads():
    """
    ([Workload], all_services, ok).

    `ok` is False when the API call itself failed, and the difference matters
    enormously: an error must never be read as "there is no demand" and delete
    the fleet, while an honest empty result IS the normal state of a cluster
    nobody has created a component on yet, and must scale to the floor cleanly.
    """
    try:
        services = dkr.services.list()
    except Exception as exc:  # noqa: BLE001
        M_ERRORS.labels(stage="discovery").inc()
        log.error("cannot list services: %s", exc)
        return [], [], False

    workloads = []
    for service in services:
        spec = service.attrs.get("Spec", {})
        labels = spec.get("Labels") or {}
        if labels.get(WORKLOAD_LABEL) != WORKLOAD_APP:
            continue
        try:
            task_tpl = spec.get("TaskTemplate", {}) or {}
            resources = task_tpl.get("Resources")
            replicas = (spec.get("Mode", {}).get("Replicated") or {}).get("Replicas", 0)
            cost = _reservations(resources)
            if cost == ZERO:
                M_ERRORS.labels(stage="policy").inc()
                warn_once((service.name, "noreservation"),
                          "%s carries no resource reservation. Swarm will pack it "
                          "anywhere and the capacity model cannot see it; charging "
                          "%s so it is at least visible.", service.name, UNRESERVED_FLOOR)
                cost = UNRESERVED_FLOOR
            mounts = (task_tpl.get("ContainerSpec") or {}).get("Mounts") or []
            if any(m.get("Type") == "volume" for m in mounts):
                M_ERRORS.labels(stage="policy").inc()
                warn_once((service.name, "volume"),
                          "%s is labelled %s=%s but mounts a volume. It will be "
                          "moved onto workers that are later deleted, and the data "
                          "goes with them. Drop the label, or drop the volume.",
                          service.name, WORKLOAD_LABEL, WORKLOAD_APP)
            limit = _limits(resources)
            cpu_limit = limit.cores or cost.cores or 1.0
            workloads.append(Workload(
                name=service.name, id=service.id,
                policy=policy_from_labels(service.name, labels, replicas),
                spec_replicas=replicas, cost=cost, cpu_limit=cpu_limit,
                pinned=is_pinned(service), rolling=update_in_progress(service),
                component=labels.get(COMPONENT_LABEL, service.name.split("_")[0]),
                rolled_back=update_rolled_back(service),
                placement_pinned=labels.get(PLACEMENT_PIN_LABEL) == "true",
            ))
        except Exception as exc:  # noqa: BLE001
            M_ERRORS.labels(stage="discovery").inc()
            log.warning("skipping %s this loop: %s", service.name, exc)

    workloads.sort(key=lambda w: (w.policy.priority, w.name))
    return workloads, services, True


# ---------------------------------------------------------------------------
# CAPACITY — measured, never configured
# ---------------------------------------------------------------------------

def node_resources(node):
    res = node.attrs.get("Description", {}).get("Resources", {}) or {}
    return Res(int(res.get("NanoCPUs", 0) or 0), int(res.get("MemoryBytes", 0) or 0))


def node_free_for_apps(node, tasks, app_ids):
    """
    Room on this node for application workload.

    Overhead is defined NEGATIVELY — everything that is not an app task — which
    is both simpler than the old exclude-one-service-id and more correct,
    because it absorbs whatever new kind of component gets invented next.

    App tasks already running here are deliberately NOT subtracted: this is
    "total room for apps", and the demand side counts every replica including
    the ones already placed. The two sides never double-count, and because
    demand comes from desired counts rather than live tasks, a start-first
    rollout briefly doubling a footprint cannot inflate it.
    """
    total = node_resources(node)
    if total.cpu <= 0:
        return ZERO
    overhead = ZERO
    for task in tasks:
        if task.get("ServiceID") in app_ids:
            continue
        overhead = overhead + _reservations(task.get("Spec", {}).get("Resources"))
    return total - overhead


def global_service_reservations(services):
    """
    (cpu, mem) that lands on EVERY node just for being in the cluster.

    `mode: global` services get one task per node, so their reservations are a
    per-node tax that comes off a new worker's advertised size before any of it
    counts as application capacity.
    """
    total = ZERO
    for svc in services:
        spec = svc.attrs.get("Spec", {})
        if "Global" not in (spec.get("Mode") or {}):
            continue
        total = total + _reservations(spec.get("TaskTemplate", {}).get("Resources"))
    return total


def new_worker_free(services):
    """
    What one NEW worker of WORKER_TYPE would offer applications, or None.

    None means "cannot size a new worker", and callers refuse to buy rather than
    guessing — with heterogeneous costs, an assumed capacity means nothing.
    """
    try:
        st = hcloud.server_types.get_by_name(WORKER_TYPE)
    except Exception as exc:  # noqa: BLE001
        log.warning("cannot look up server type %s: %s", WORKER_TYPE, exc)
        return None
    if st is None:
        log.warning("unknown WORKER_TYPE %s", WORKER_TYPE)
        return None
    total = Res(int(st.cores * 1e9), int(st.memory * 1024 ** 3))   # hcloud reports GB
    return total - global_service_reservations(services)


# --- the packer ------------------------------------------------------------
# One algorithm, used by admission, by fleet sizing and by node removal. Two
# algorithms that disagree by one replica are a loop that buys a worker and
# immediately deletes it.

Item = namedtuple("Item", ["service", "cost", "workers_only"])


def demand_items(workloads, counts, pinned_names=None):
    """
    One item per replica, round-robin across services.

    Round-robin rather than service-by-service so a large application cannot
    starve a small one, and `workers_only` per item because mid-handover
    services legitimately disagree about where they may be placed.
    """
    per_service = []
    for w in workloads:
        n = counts.get(w.name, 0)
        only = w.name in pinned_names if pinned_names is not None else w.pinned
        per_service.append([Item(w.name, w.cost, only) for _ in range(n)])
    items = []
    for row in zip(*per_service) if per_service else []:
        items.extend(row)
    # zip() stops at the shortest; append the remainder in the same service
    # order so the stream stays deterministic.
    depth = min((len(p) for p in per_service), default=0)
    for row in per_service:
        items.extend(row[depth:])
    # Constrained items first within the stream: placing an unpinned item on a
    # worker when it could have used the master, and thereby starving a pinned
    # item, is the one ordering mistake that produces pending tasks.
    items.sort(key=lambda i: not i.workers_only)
    return items


Bin = namedtuple("Bin", ["key", "free", "is_manager"])


def place(items, bins):
    """
    First-fit over the item stream, bins in a fixed order. Returns
    (assignment {item index: bin key}, unplaced [item index]).

    Bins are ordered largest-CPU-first with the master last, so existing
    capacity is filled before anything new is opened — which is what "fill the
    workers before buying another" means, expressed as an ordering rather than
    as a special case.
    """
    order = sorted(bins, key=lambda b: (b.is_manager, -b.free.cpu, str(b.key)))
    free = {b.key: b.free for b in order}
    assignment, unplaced = {}, []
    for index, item in enumerate(items):
        for b in order:
            if item.workers_only and b.is_manager:
                continue
            if item.cost.fits_in(free[b.key]):
                free[b.key] = free[b.key] - item.cost
                assignment[index] = b.key
                break
        else:
            unplaced.append(index)
    return assignment, unplaced


def admit(workloads, wants, bins, live_replicas):
    """
    How many replicas of each service may actually be set this loop.

    Two round-robin passes over shared bin state: minimums first, then growth.
    Deterministic and monotone — more capacity never reduces anyone's allocation
    — which is what stops the loop oscillating between services.

    Ranking by "worst SLO breach first" is tempting and wrong: it couples the
    allocation to a noisy signal and hands a replica back and forth every 60
    seconds. `autoscale.priority` is the escape hatch when one app genuinely
    matters more.
    """
    order = sorted(workloads, key=lambda w: (w.policy.priority, w.name))
    granted = {w.name: 0 for w in order}
    starved, capped = set(), {}
    free = {b.key: b.free for b in
            sorted(bins, key=lambda b: (b.is_manager, -b.free.cpu, str(b.key)))}
    bin_order = [b for b in sorted(bins, key=lambda b: (b.is_manager, -b.free.cpu, str(b.key)))]

    def try_place(w):
        for b in bin_order:
            if w.pinned and b.is_manager:
                continue
            if w.cost.fits_in(free[b.key]):
                free[b.key] = free[b.key] - w.cost
                return True
        return False

    def rounds(target_of, on_fail):
        active = list(order)
        while active:
            progressed = []
            for w in active:
                if granted[w.name] >= target_of(w):
                    continue
                if try_place(w):
                    granted[w.name] += 1
                    progressed.append(w)
                else:
                    on_fail(w)
            active = progressed

    rounds(lambda w: w.policy.min_replicas, lambda w: starved.add(w.name))
    rounds(lambda w: max(wants.get(w.name, 0), w.policy.min_replicas),
           lambda w: capped.setdefault(w.name, granted[w.name]))

    # Admission caps GROWTH; it never scales anything down. A transient overhead
    # spike (an infrastructure rollout doubling a reservation for 90 seconds)
    # must not become a scale-down followed by a scale-up next loop.
    admitted = {}
    for w in order:
        floor = min(live_replicas.get(w.name, 0), wants.get(w.name, 0))
        admitted[w.name] = max(floor, granted[w.name])
    return admitted, capped, starved


def servers_needed(items, worker_bins, new_free, pressured):
    """
    How many Hetzner servers the workers must amount to. Master excluded.

    Counts the existing bins that RECEIVE something rather than however many
    exist: "we have enough, keep what we have" reads as harmless and quietly
    never scales down — a fleet grown to three under load would hold all three
    until traffic fell below what the MASTER alone can take, having paid for two
    idle servers all the way down.
    """
    assignment, unplaced = place(items, worker_bins)
    used = len(set(assignment.values()))
    if unplaced:
        if new_free is None:
            log.warning("cannot size a new %s; holding the fleet at %d",
                        WORKER_TYPE, len(worker_bins))
            return len(worker_bins)
        remaining = [items[i] for i in unplaced]
        extra = 0
        while remaining:
            extra += 1
            _, still = place(remaining, [Bin(("new", extra), new_free, False)])
            if len(still) == len(remaining):
                # Nothing fit at all: a single replica is larger than any node
                # this cluster can buy. No number of servers will ever hold it.
                for i in still:
                    M_ERRORS.labels(stage="capacity").inc()
                    warn_once((remaining[i].service, "unplaceable"),
                              "%s reserves %s, which exceeds what a whole %s offers "
                              "(%s). No number of workers will ever place it.",
                              remaining[i].service, remaining[i].cost, WORKER_TYPE, new_free)
                    S_UNPLACEABLE.labels(remaining[i].service).set(1)
                break
            remaining = [remaining[i] for i in still]
            if extra > MAX_WORKERS + 2:      # belt and braces against a bad cost
                break
        used += extra
    if pressured:
        used += 1
    return used


def workers_needed(workloads, wants, node_pressure, manager_free, worker_bins, new_free):
    """
    How many Hetzner WORKERS the wanted replicas require. The master is not one.

    Returns 0 when the master alone can hold everything — the free floor,
    nothing billed. Otherwise the master stops being a placement target and the
    workers must cover the WHOLE demand, so the answer is never 0 and never 1
    just because "one more would do": one worker has to hold what the master was
    holding as well.
    """
    pressured = node_pressure is not None and node_pressure > NODE_PRESSURE_PCT

    # "Could the master hold everything IF we unpinned?" — a hypothetical about
    # the target state, which is why the masks are ignored here.
    if not pressured:
        hypothetical = demand_items(workloads, wants, pinned_names=set())
        _, unplaced = place(hypothetical, [Bin("master", manager_free, True)])
        if not unplaced:
            return 0

    items = demand_items(workloads, wants, pinned_names=set(w.name for w in workloads))
    servers = servers_needed(items, worker_bins, new_free, pressured)
    if pressured:
        log.info("node resource pressure at %.0f%%: requesting an extra worker",
                 node_pressure)
    # Past the master's capacity, so at least one real worker is required.
    return max(1, servers)


def note_capacity(manager_free, worker_bins, new_free, workloads):
    """Explain the numbers when they CHANGE, not every 60 seconds."""
    global _capacity_note
    shape = (manager_free, tuple(b.free for b in worker_bins), new_free,
             tuple((w.name, w.cost) for w in workloads))
    if shape == _capacity_note:
        return
    _capacity_note = shape
    log.info(
        "measured capacity: master offers %s · %d worker(s) offer %s · a new %s "
        "would offer %s · replicas cost %s",
        manager_free, len(worker_bins),
        " + ".join(str(b.free) for b in worker_bins) or "nothing",
        WORKER_TYPE, new_free if new_free else "unknown",
        ", ".join(f"{w.name} {w.cost}" for w in workloads) or "nothing",
    )
    if MIN_WORKERS == 0 and workloads:
        floor_items = demand_items(
            workloads, {w.name: w.policy.min_replicas for w in workloads},
            pinned_names=set())
        _, unplaced = place(floor_items, [Bin("master", manager_free, True)])
        if unplaced:
            log.warning(
                "the master cannot hold every component's minimum (%s), so the "
                "cluster can never return to the free zero-worker state. Give it "
                "more CPU/RAM, lower a minimum, or shrink a reservation.",
                ", ".join(f"{w.name}x{w.policy.min_replicas}" for w in workloads),
            )


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
        cmd += ["--constraint-add", WORKER_CONSTRAINT]
    else:
        # --constraint-rm matches the stored string EXACTLY, and the stored
        # string is not ours. The renderer writes `node.role == worker` with
        # spaces; WORKER_CONSTRAINT is written without them. Removing the
        # spaced constraint by its unspaced name silently removed nothing, so
        # the pin could never be released: every loop issued an update that
        # changed the service version and nothing else, the applications stayed
        # bound to workers forever, and the fleet could never scale to zero no
        # matter how idle the cluster was. _WORKER_PIN already tolerates every
        # spelling when READING; this is the same tolerance when writing.
        live = [c for c in _constraints(dkr.services.get(name))
                if _WORKER_PIN.match(c)]
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
                 MODE_WORKER if pinned else MODE_MANAGER, reason)
        return
    if not _service_update_constraint(name, add=pinned):
        # Nothing to remove. Saying "moving to manager mode" once a minute for
        # a service that has been in manager mode all along is how a loop that
        # was achieving nothing still looked busy.
        return False
    M_EVENTS.labels(direction=f"placement-{MODE_WORKER if pinned else MODE_MANAGER}").inc()
    log.info("moving %s to %s mode: %s", name,
             MODE_WORKER if pinned else MODE_MANAGER, reason)
    return True


def reconcile_placement(workloads, want_pinned, reason, skip_rolling=True):
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
    for w in workloads:
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


def running_and_pending(tasks_by_node, workloads):
    """(running, pending) per service name, across the whole cluster."""
    by_id = {w.id: w.name for w in workloads}
    running = {w.name: 0 for w in workloads}
    pending = {w.name: 0 for w in workloads}
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
# signals
# ---------------------------------------------------------------------------

Signals = namedtuple("Signals", ["p95", "cpu", "p95_held", "cpu_held", "p95_peak", "cpu_peak"])


def read_signals_batch(workloads, worker_count):
    """
    Every service's signals, in a handful of grouped queries.

    Returns ({service name: Signals}, node_pressure). Services are grouped by
    (histogram, unit, window) so in practice this is one or two round trips
    rather than six per component.
    """
    signals = {w.name: Signals(None, None, None, None, None, None) for w in workloads}
    scalers = [w for w in workloads if w.policy.autoscale]

    cpu_raw = vm_query_map(CPU_BY_SERVICE, label=_CPU_LABEL) if scalers else {}

    # A latency metric named explicitly on the service wins; anything else is
    # discovered. Discovery is what makes a component work without being told
    # its own framework's metric name, and the explicit label is the escape
    # hatch for an app that publishes several and wants a specific one.
    discovered = discover_latency([w.name for w in scalers]) if scalers else {}

    groups = {}
    for w in scalers:
        explicit = w.policy.histogram_explicit
        if explicit:
            expr = p95_expr(w.policy.histogram, w.policy.unit)
        elif w.name in discovered:
            expr = discovered[w.name][0]
        else:
            continue          # no latency signal for this one; CPU still applies
        groups.setdefault(expr, []).append(w)

    p95_now, p95_held, p95_peak = {}, {}, {}
    for expr, members in groups.items():
        p95_now.update(vm_query_map(expr))
        for window in {w.policy.sustain_up for w in members}:
            p95_held.update({k: v for k, v in
                             vm_query_map(sustained(expr, window, "min_over_time")).items()
                             if any(w.policy.sustain_up == window and w.name == k
                                    for w in members)})
        for window in {w.policy.sustain_down for w in members}:
            p95_peak.update({k: v for k, v in
                             vm_query_map(sustained(expr, window, "max_over_time")).items()
                             if any(w.policy.sustain_down == window and w.name == k
                                    for w in members)})

    cpu_windows_up, cpu_windows_down = {}, {}
    for window in {w.policy.sustain_up for w in scalers}:
        cpu_windows_up[window] = vm_query_map(
            sustained(CPU_BY_SERVICE, window, "min_over_time"), label=_CPU_LABEL)
    for window in {w.policy.sustain_down for w in scalers}:
        cpu_windows_down[window] = vm_query_map(
            sustained(CPU_BY_SERVICE, window, "max_over_time"), label=_CPU_LABEL)

    for w in scalers:
        def pct(value):
            # Each service against its OWN limit, read from its own live spec.
            return None if value is None else value / max(w.cpu_limit, 0.01) * 100

        signals[w.name] = Signals(
            p95=p95_now.get(w.name),
            cpu=pct(cpu_raw.get(w.name)),
            p95_held=p95_held.get(w.name),
            cpu_held=pct(cpu_windows_up.get(w.policy.sustain_up, {}).get(w.name)),
            p95_peak=p95_peak.get(w.name),
            cpu_peak=pct(cpu_windows_down.get(w.policy.sustain_down, {}).get(w.name)),
        )

    # With an empty fleet the worker-scoped guards have no series to return, so
    # measure the box that is actually holding the replicas instead.
    if worker_count:
        node_cpu, node_mem = vm_query(CPU_EXPR), vm_query(MEM_EXPR)
    else:
        node_cpu, node_mem = vm_query(MGR_CPU_EXPR), vm_query(MGR_MEM_EXPR)
    if node_cpu is not None:
        M_CPU.set(node_cpu)
    if node_mem is not None:
        M_MEM.set(node_mem)

    # Whichever resource runs out first is the one that blocks placement.
    pressures = [v for v in (node_cpu, node_mem) if v is not None]
    return signals, (max(pressures) if pressures else None)


def desired_replicas(workload, signals, current):
    """
    Latency first, CPU-per-replica second. Both must be SUSTAINED — a single
    scrape above threshold is noise, not a trend.
    """
    policy = workload.policy
    if not policy.autoscale:
        return current

    up_p95 = policy.slo_ms * policy.up_ratio
    down_p95 = policy.slo_ms * policy.down_ratio

    reasons = []
    if signals.p95_held is not None and signals.p95_held > up_p95:
        reasons.append(f"p95 held above {up_p95:.0f}ms ({signals.p95_held:.0f}ms)")
    if signals.cpu_held is not None and signals.cpu_held > policy.up_cpu:
        reasons.append(f"cpu/replica held above {policy.up_cpu:.0f}% ({signals.cpu_held:.0f}%)")

    if reasons:
        step = max(1, int(current * policy.up_factor))
        want = min(policy.max_replicas, current + step)
        if want != current:
            log.info("%s: replicas %d -> %d: %s", workload.name, current, want,
                     "; ".join(reasons))
        return want

    # Scale down only when BOTH signals have stayed low for the full window.
    # `quiet_cpu` requires a non-None peak on purpose: a missing cadvisor series
    # must hold the count, not authorise shrinking it.
    quiet_latency = signals.p95_peak is None or signals.p95_peak < down_p95
    quiet_cpu = signals.cpu_peak is not None and signals.cpu_peak < policy.down_cpu
    if quiet_latency and quiet_cpu and current > policy.min_replicas:
        log.info("%s: replicas %d -> %d: quiet for %ds (p95 peak %s, cpu/replica peak %.0f%%)",
                 workload.name, current, current - 1, policy.sustain_down,
                 f"{signals.p95_peak:.0f}ms" if signals.p95_peak is not None else "no traffic",
                 signals.cpu_peak)
        return current - 1

    return current


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


def right_size(cpu_q, mem_max_bytes, node_cpu, node_mem):
    """
    (cpu_reservation, memory_reservation_mb, cpu_limit, memory_limit_mb).

    Clamped to a fraction of the SMALLEST node, because a reservation larger
    than any node can satisfy is not a sizing decision, it is an unschedulable
    task — the failure mode is a replica that sits Pending forever while the
    panel reports the component as down for no visible reason.
    """
    cpu_res = max(CPU_RESERVE_FLOOR, cpu_q * CPU_RESERVE_HEADROOM)
    mem_res_mb = max(MEM_RESERVE_FLOOR_MB,
                     mem_max_bytes * MEM_RESERVE_HEADROOM / (1024 * 1024))

    cpu_cap = max(CPU_RESERVE_FLOOR, node_cpu / 1e9 * 0.5) if node_cpu else cpu_res
    mem_cap = (max(MEM_RESERVE_FLOOR_MB, node_mem / (1024 * 1024) * 0.5)
               if node_mem else mem_res_mb)
    cpu_res = min(cpu_res, cpu_cap)
    mem_res_mb = min(mem_res_mb, mem_cap)

    return (round(cpu_res, 3), int(mem_res_mb),
            round(min(cpu_res * CPU_LIMIT_MULTIPLE, cpu_cap * 2), 3),
            int(min(mem_res_mb * MEM_LIMIT_MULTIPLE, mem_cap * 2)))


def _changed_enough(old, new):
    if not old:
        return True
    return abs(new - old) / old >= RESIZE_MIN_CHANGE


def apply_right_sizing(workloads, usage, node_cpu, node_mem):
    """
    Resize reservations to match measured usage. Returns the number applied.

    Deliberately does NOT touch a service whose rollout is in flight: a resize
    is itself an update, and stacking one on an in-progress rollout restarts it
    from the beginning.
    """
    applied = 0
    for w in workloads:
        if w.rolling or w.name not in usage:
            continue
        cpu_q, mem_max = usage[w.name]
        cpu_res, mem_res, cpu_lim, mem_lim = right_size(
            cpu_q, mem_max, node_cpu, node_mem)

        now = time.time()
        if now - _last_resize.get(w.name, 0) < RESIZE_COOLDOWN_SECONDS:
            continue
        if not (_changed_enough(w.cost.cores, cpu_res)
                or _changed_enough(w.cost.mb, mem_res)):
            continue

        if DRY_RUN:
            log.info("[dry-run] would resize %s to %.3f CPU / %dMB reserved "
                     "(measured %.3f CPU / %dMB)", w.name, cpu_res, mem_res,
                     cpu_q, int(mem_max / (1024 * 1024)))
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
        log.info("resized %s: %.2f -> %.3f CPU, %d -> %dMB reserved "
                 "(measured q%d %.3f CPU, peak %dMB)",
                 w.name, w.cost.cores, cpu_res, int(w.cost.mb), mem_res,
                 int(USAGE_CPU_Q * 100), cpu_q, int(mem_max / (1024 * 1024)))
    return applied


#: How long to leave a forced re-placement alone before trying another. Long
#: enough that a rollout which is simply slow is never nudged twice.
HANDOVER_NUDGE_COOLDOWN = 600
_last_handover_nudge = {}


def handover_nudge(names, workloads):
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
    by_name = {w.name: w for w in workloads}
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
# scheduling floor
# ---------------------------------------------------------------------------

def scheduled_floor():
    """
    The worker floor in force right now.

    Parses SCHEDULE_FLOOR like '08:00-20:00=2,20:00-23:00=1' (UTC). The numbers
    are worker counts, so `=0` is a valid entry meaning "the master alone is
    enough during this window".
    """
    if not SCHEDULE_FLOOR.strip():
        return MIN_WORKERS
    now = datetime.now(timezone.utc)
    minutes_now = now.hour * 60 + now.minute
    floor = MIN_WORKERS
    for chunk in SCHEDULE_FLOOR.split(","):
        m = re.match(r"^\s*(\d{2}):(\d{2})-(\d{2}):(\d{2})=(\d+)\s*$", chunk)
        if not m:
            log.warning("ignoring malformed SCHEDULE_FLOOR entry: %s", chunk)
            continue
        h1, m1, h2, m2, count = (int(x) for x in m.groups())
        start, end = h1 * 60 + m1, h2 * 60 + m2
        active = start <= minutes_now < end if start <= end else (
            minutes_now >= start or minutes_now < end)
        if active:
            floor = max(floor, count)
    return floor


# ---------------------------------------------------------------------------
# node actions
# ---------------------------------------------------------------------------

def worker_join_token():
    return dkr.swarm.attrs["JoinTokens"]["Worker"]


def manager_private_ip():
    addr = dkr.info().get("Swarm", {}).get("NodeAddr")
    if not addr:
        raise RuntimeError("cannot determine manager advertise address")
    return addr


def create_worker():
    token = worker_join_token()
    manager_ip = manager_private_ip()
    with open(USERDATA_PATH) as fh:
        user_data = fh.read()
    user_data = user_data.replace("__SWARM_TOKEN__", token)
    user_data = user_data.replace("__MANAGER_IP__", manager_ip)

    name = f"{CLUSTER}-worker-{int(time.time())}"
    if DRY_RUN:
        log.info("[dry-run] would create %s (%s)", name, WORKER_TYPE)
        return

    network = hcloud.networks.get_by_name(NETWORK_NAME)
    if network is None:
        raise RuntimeError(f"private network {NETWORK_NAME} not found")
    ssh_keys = []
    if SSH_KEY_NAME:
        key = hcloud.ssh_keys.get_by_name(SSH_KEY_NAME)
        if key:
            ssh_keys.append(SSHKey(id=key.id))

    hcloud.servers.create(
        name=name,
        server_type=ServerType(name=WORKER_TYPE),
        image=Image(name=WORKER_IMAGE),
        location=Location(name=LOCATION),
        networks=[Network(id=network.id)],
        ssh_keys=ssh_keys,
        user_data=user_data,
        labels={"cluster": CLUSTER, "role": "swarm-worker"},
        start_after_create=True,
    )
    M_EVENTS.labels(direction="up").inc()
    log.info("created worker %s (%s in %s)", name, WORKER_TYPE, LOCATION)


def pick_removal_candidate(ready, node_free, items, manager_free, tasks_by_node, app_ids):
    """
    Which worker to drop, or None if dropping any would leave demand unplaceable.

    Newest-first (LIFO): the newest node is least likely to hold warm state and
    Hetzner bills by the hour. But each candidate is TESTED, not assumed — with
    heterogeneous costs a sum is meaningless, since 1.0 CPU spread over four
    nodes holds no 0.5 CPU replica if each has 0.25 free.

    `manager_free` is supplied only when every app is unpinned. Without it the
    LAST worker is never removable — the remaining workers total nothing, that
    never covers the demand, and the cluster sticks one server above the floor
    forever.
    """
    def created(node):
        return node.attrs.get("CreatedAt", "")

    for node in sorted(ready, key=created, reverse=True):
        # A node carrying something this loop does not understand — a replicated,
        # non-global service that is not an app workload — may be holding state.
        # Deleting it is data loss, so skip it and say so.
        foreign = [t for t in tasks_by_node.get(node.id, [])
                   if t.get("ServiceID") not in app_ids
                   and t.get("Status", {}).get("State") == "running"
                   and not _is_global(t)]
        if foreign:
            warn_once((node.id, "foreign"),
                      "not removing %s: it runs %d task(s) this autoscaler does not "
                      "manage, which may be holding state",
                      node.attrs["Description"]["Hostname"], len(foreign))
            continue
        bins = [Bin(n.id, node_free.get(n.id, ZERO), False) for n in ready if n.id != node.id]
        if manager_free is not None:
            bins.append(Bin("master", manager_free, True))
        _, unplaced = place(items, bins)
        if not unplaced:
            return node
    return None


_GLOBAL_SERVICE_IDS = set()


def _is_global(task):
    return task.get("ServiceID") in _GLOBAL_SERVICE_IDS


def tasks_on_node(node_id):
    return list(dkr.api.tasks(filters={"node": node_id, "desired-state": "running"}))


def remove_worker(node):
    hostname = node.attrs["Description"]["Hostname"]
    node_id = node.id

    if DRY_RUN:
        log.info("[dry-run] would drain and delete %s", hostname)
        return

    log.info("draining %s", hostname)
    spec = node.attrs["Spec"]
    spec["Availability"] = "drain"
    node.update(spec)

    deadline = time.time() + 180
    while time.time() < deadline:
        remaining = tasks_on_node(node_id)
        if not remaining:
            break
        log.info("waiting for %d task(s) to leave %s", len(remaining), hostname)
        time.sleep(10)
    else:
        log.warning("drain timed out on %s; removing anyway", hostname)

    # cloudflared runs global on every worker and needs ~30s to drain its edge
    # connections on SIGTERM. Cutting this short drops live requests.
    time.sleep(POST_DRAIN_GRACE)

    try:
        dkr.api.remove_node(node_id, force=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("swarm node removal failed for %s: %s", hostname, exc)

    server = hcloud.servers.get_by_name(hostname)
    if server:
        server.delete()
        log.info("deleted hetzner server %s", hostname)
    else:
        log.warning("no hetzner server named %s; swarm entry removed only", hostname)

    M_EVENTS.labels(direction="down").inc()

    if ROTATE_TOKEN:
        try:
            dkr.api.update_swarm(
                version=dkr.api.inspect_swarm()["Version"]["Index"],
                swarm_spec=dkr.api.inspect_swarm()["Spec"],
                rotate_worker_token=True,
            )
            log.info("rotated worker join token")
        except Exception as exc:  # noqa: BLE001
            log.warning("token rotation failed: %s", exc)


def reap_orphans():
    """Hetzner servers that never joined, and swarm nodes that went away."""
    swarm_hostnames = {n.attrs["Description"]["Hostname"] for n in swarm_workers()}
    servers = hetzner_workers()
    for server in servers:
        if server.name in swarm_hostnames:
            continue
        age = (datetime.now(timezone.utc) - server.created).total_seconds()
        if age < ORPHAN_GRACE_SECONDS:
            continue
        log.warning("orphan server %s never joined the swarm after %.0fs; deleting",
                    server.name, age)
        if not DRY_RUN:
            server.delete()

    hetzner_names = {s.name for s in servers}
    for node in swarm_workers():
        hostname = node.attrs["Description"]["Hostname"]
        state = node.attrs.get("Status", {}).get("State")
        if state == "down" and hostname not in hetzner_names:
            log.warning("swarm node %s is down and its server is gone; removing", hostname)
            if DRY_RUN:
                continue
            try:
                dkr.api.remove_node(node.id, force=True)
            except Exception as exc:  # noqa: BLE001
                log.warning("could not remove %s: %s", hostname, exc)


def newest_worker_age():
    servers = hetzner_workers()
    if not servers:
        return None
    newest = max(servers, key=lambda s: s.created)
    return (datetime.now(timezone.utc) - newest.created).total_seconds()


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------

def export_service_metrics(workloads, wants, admitted, signals, running, pending):
    names = {w.name for w in workloads}
    forget_vanished(names)
    for w in workloads:
        s = signals.get(w.name, Signals(None, None, None, None, None, None))
        if s.p95 is not None:
            S_P95.labels(w.name).set(s.p95)
        if s.cpu is not None:
            S_CPU.labels(w.name).set(s.cpu)
        # Reported for every scaler, every loop, present or not — an absent
        # series cannot alert, which is the whole point.
        S_CPU_SIGNAL.labels(w.name).set(
            1 if (w.policy.autoscale and s.cpu_peak is not None) else 0)
        S_SLO.labels(w.name).set(w.policy.slo_ms)
        S_CURRENT.labels(w.name).set(w.spec_replicas)
        S_DESIRED.labels(w.name).set(wants.get(w.name, w.spec_replicas))
        S_ADMITTED.labels(w.name).set(admitted.get(w.name, w.spec_replicas))
        S_RUNNING.labels(w.name).set(running.get(w.name, 0))
        S_ROLLBACK.labels(w.name).set(1 if w.rolled_back else 0)
        S_PENDING.labels(w.name).set(pending.get(w.name, 0))
        S_MIN.labels(w.name).set(w.policy.min_replicas)
        S_MAX.labels(w.name).set(w.policy.max_replicas)
        S_AUTO.labels(w.name).set(1 if w.policy.autoscale else 0)
        S_PINNED.labels(w.name).set(1 if w.pinned else 0)
        S_COST_CPU.labels(w.name).set(w.cost.cores)
        S_COST_MEM.labels(w.name).set(w.cost.mem)


def loop():
    global _last_scale_down, _manager_wait_since

    # 1. reaping is independent of everything else and runs first.
    try:
        reap_orphans()
    except Exception as exc:  # noqa: BLE001
        M_ERRORS.labels(stage="reap").inc()
        log.warning("reaping failed: %s", exc)

    # 2. INVENTORY. Failing here is a HOLD: without it every later number is a
    #    guess, and acting on a guess is how a cluster deletes itself.
    try:
        ready = swarm_ready_workers()
        manager_node = get_manager_node()
        manager_id = manager_node.id if manager_node else manager_node_id()
        tasks_by_node = index_tasks()
    except Exception as exc:  # noqa: BLE001
        M_ERRORS.labels(stage="inventory").inc()
        log.error("cannot read the cluster inventory; holding: %s", exc)
        return

    current_workers = len(ready)
    current_hosts = 1 + current_workers
    floor = scheduled_floor()
    M_CURRENT.set(current_workers)
    M_HOSTS.set(current_hosts)
    M_MAX.set(MAX_WORKERS)
    M_MIN.set(floor)

    # 3. DISCOVERY. An API error must never be read as "no demand".
    workloads, services, ok = discover_workloads()
    if not ok:
        log.warning("holding at %d worker(s): the service list is unreadable", current_workers)
        return
    _GLOBAL_SERVICE_IDS.clear()
    _GLOBAL_SERVICE_IDS.update(
        s.id for s in services if "Global" in (s.attrs.get("Spec", {}).get("Mode") or {}))
    M_MANAGED.set(len(workloads))
    app_ids = {w.id for w in workloads}
    pinned = {w.name for w in workloads if w.pinned}
    any_pinned = bool(pinned)
    all_unpinned = not any_pinned
    M_MODE.set(1 if any_pinned else 0)
    M_MIXED.set(1 if pinned and len(pinned) != len(workloads) else 0)

    # 4. EMERGENCY, before anything that can fail. Worker mode with an empty
    #    fleet means every task is unplaceable and the site is down; this is the
    #    one path that must survive VictoriaMetrics or Hetzner being unreachable,
    #    and in the old code it sat behind both.
    if any_pinned and current_workers == 0:
        M_ERRORS.labels(stage="stranded").inc()
        log.error("applications are pinned to workers and no worker is left: %s",
                  ", ".join(sorted(pinned)))
        # Deliberately ignores update_in_progress: a rollout with nowhere to
        # place tasks is not a reason to wait, it is the thing to unwedge.
        reconcile_placement(workloads, False,
                            "no worker is left in the swarm; failing back to the master",
                            skip_rolling=False)
        return

    # 5. CAPACITY.
    node_free = {}
    for node in ([manager_node] if manager_node else []) + ready:
        try:
            node_free[node.id] = node_free_for_apps(
                node, tasks_by_node.get(node.id, []), app_ids)
        except Exception as exc:  # noqa: BLE001
            M_ERRORS.labels(stage="capacity").inc()
            log.warning("cannot measure %s; treating it as full: %s", node.id[:12], exc)
            # ZERO free, not zero used. Believing in room that does not exist is
            # how tasks end up pending.
            node_free[node.id] = ZERO
    manager_free = node_free.get(manager_id, ZERO)
    worker_bins = [Bin(n.id, node_free.get(n.id, ZERO), False) for n in ready]
    try:
        new_free = new_worker_free(services)
    except Exception as exc:  # noqa: BLE001
        log.warning("cannot size a new worker: %s", exc)
        new_free = None

    M_MGR_CPU.set(manager_free.cores)
    M_MGR_MEM.set(manager_free.mem)
    pool = ZERO
    for b in worker_bins:
        pool = pool + b.free
    M_POOL_CPU.set(pool.cores)
    M_POOL_MEM.set(pool.mem)
    M_NEW_CPU.set(new_free.cores if new_free else 0)
    M_NEW_MEM.set(new_free.mem if new_free else 0)
    note_capacity(manager_free, worker_bins, new_free, workloads)

    running, pending = running_and_pending(tasks_by_node, workloads)

    # 6. SIGNALS + 7. TIER 1, per service, each isolated.
    try:
        signals, node_pressure = read_signals_batch(workloads, current_workers)
    except Exception as exc:  # noqa: BLE001
        M_ERRORS.labels(stage="signals").inc()
        log.warning("signal read failed; every service holds this loop: %s", exc)
        signals = {w.name: Signals(None, None, None, None, None, None) for w in workloads}
        node_pressure = None

    live = {w.name: w.spec_replicas for w in workloads}
    wants = {}
    for w in workloads:
        try:
            want = desired_replicas(w, signals.get(w.name), w.spec_replicas)
        except Exception as exc:  # noqa: BLE001
            M_ERRORS.labels(stage="signals").inc()
            log.warning("%s holds at %d: %s", w.name, w.spec_replicas, exc)
            want = w.spec_replicas
        wants[w.name] = max(w.policy.min_replicas, min(w.policy.max_replicas, want))

    demand = ZERO
    for w in workloads:
        for _ in range(wants[w.name]):
            demand = demand + w.cost
    M_DEMAND_CPU.set(demand.cores)
    M_DEMAND_MEM.set(demand.mem)

    # 8. SIZING, from the UNCAPPED want.
    want_servers = workers_needed(workloads, wants, node_pressure, manager_free,
                                  worker_bins, new_free) if workloads else 0
    want_servers = max(floor, min(MAX_WORKERS, want_servers))
    # One worker means the master is out of the request path entirely: there is
    # no half state where both carry replicas.
    want_pinned = want_servers >= 1
    M_DESIRED.set(want_servers)

    # 9. ADMISSION, against the CURRENTLY eligible nodes — which is what the
    #    services are pinned to RIGHT NOW, not where they are heading. Mid
    #    scale-out the master is still serving, and capping against the workers
    #    alone would shed the replicas it is holding.
    bins = list(worker_bins)
    if any(not w.pinned for w in workloads) or not workloads:
        bins.append(Bin("master", manager_free, True))
    admitted, capped, starved = admit(workloads, wants, bins, live) if workloads else ({}, {}, set())

    for w in workloads:
        S_STARVED.labels(w.name).set(1 if w.name in starved else 0)
        if w.name in starved:
            M_ERRORS.labels(stage="admission").inc()
            warn_once((w.name, "starved", admitted.get(w.name)),
                      "%s cannot reach its minimum of %d replica(s): only %d fit on "
                      "the current nodes. The fleet is being grown; if it is already "
                      "at MAX_WORKERS this will not resolve on its own.",
                      w.name, w.policy.min_replicas, admitted.get(w.name, 0))
        elif w.name in capped:
            warn_once((w.name, "capped", capped[w.name], wants[w.name]),
                      "capping %s at %d replica(s) (wanted %d): the eligible nodes "
                      "have %s left and one replica needs %s",
                      w.name, capped[w.name], wants[w.name], pool, w.cost)

    # 10. HANDOVER 1 — release the pin BEFORE shrinking the fleet.
    if not want_pinned and any_pinned:
        changed = reconcile_placement(
            workloads, False,
            f"scaling in to {want_servers} worker(s); the master takes the replicas back")
        if changed:
            pinned -= set(changed)
            any_pinned = bool(pinned)
            all_unpinned = not any_pinned

    # 11. SCALE UP nodes.
    booting = len(provisioning_workers())
    owned = current_workers + booting
    if want_servers > owned:
        age = newest_worker_age()
        if age is not None and age < COOLDOWN_UP:
            log.info("worker scale-up suppressed: newest is %.0fs old, cooldown %ds",
                     age, COOLDOWN_UP)
        elif owned >= MAX_WORKERS:
            log.warning("at the worker ceiling of %d — this is a budget cap, not "
                        "capacity", MAX_WORKERS)
        else:
            for _ in range(min(want_servers - owned, MAX_WORKERS - owned)):
                try:
                    create_worker()
                except Exception as exc:  # noqa: BLE001
                    M_ERRORS.labels(stage="create").inc()
                    log.error("could not create a worker: %s", exc)
                    break
    elif booting:
        log.info("%d worker(s) still booting; not ordering more", booting)

    # 12. APPLY replicas, per service, each with its own cooldown.
    for w in workloads:
        target = admitted.get(w.name, w.spec_replicas)
        if target == w.spec_replicas:
            continue
        since = time.time() - _last_replica_change.get(w.name, 0.0)
        if since < w.policy.cooldown:
            log.info("%s: replica change suppressed, %.0fs since last", w.name, since)
            continue
        try:
            set_replicas(w.name, target)
            _last_replica_change[w.name] = time.time()
        except Exception as exc:  # noqa: BLE001
            M_ERRORS.labels(stage="scale").inc()
            log.error("could not scale %s: %s", w.name, exc)

    # 13. HANDOVER 2 — pin to workers only once they can hold EVERYTHING.
    if want_pinned and not all(w.pinned for w in workloads) and workloads:
        room_items = demand_items(
            workloads, {w.name: max(admitted.get(w.name, 0), w.spec_replicas) for w in workloads},
            pinned_names={w.name for w in workloads})
        _, unplaced = place(room_items, worker_bins)
        if current_workers == 0:
            log.info("deferring the move to worker mode: no worker is ready yet")
        elif unplaced:
            log.info("deferring the move to worker mode: %d worker(s) cannot yet hold "
                     "%d replica(s) — the master keeps serving until they can",
                     current_workers, len(room_items))
        else:
            changed = reconcile_placement(
                workloads, True,
                f"{current_workers} worker(s) ready with room for every replica; "
                f"the master drops to zero")
            if changed:
                pinned |= set(changed)
                any_pinned = True
                all_unpinned = False

    # 14. SCALE DOWN nodes: only after replicas have been shed, and slowly.
    if want_servers < current_workers:
        since = time.time() - _last_scale_down
        last_one = current_workers == 1
        items = demand_items(workloads,
                             {w.name: admitted.get(w.name, w.spec_replicas) for w in workloads},
                             pinned_names=pinned)
        candidate = pick_removal_candidate(
            ready, node_free, items, manager_free if all_unpinned else None,
            tasks_by_node, app_ids)
        on_manager = manager_running_by_service(tasks_by_node, manager_id)
        missing = [w.name for w in workloads
                   if admitted.get(w.name, w.spec_replicas) >= 1
                   and on_manager.get(w.id, 0) < 1]
        rolling = [w.name for w in workloads if w.rolling]

        if rolling:
            # Draining a node kills the tasks a rollout is in the middle of
            # creating, and `max_failure_ratio: 0` reads those deaths as the
            # update failing — so Swarm rolls the whole thing back. That is not
            # hypothetical: a right-sizing update went out detached, the next
            # loop drained the last worker, and the resize was reverted 90
            # seconds later. The rollback alert fired for a deploy nobody made.
            #
            # Removal is never urgent. Waiting a loop for the rollout to settle
            # costs one minute of one server.
            log.info("worker scale-down deferred: %s still rolling", ", ".join(rolling))
        elif since < COOLDOWN_DOWN:
            log.info("worker scale-down suppressed: %.0fs since last", since)
        elif current_workers <= floor:
            pass
        elif candidate is None:
            log.info("no worker can be removed without leaving replicas unplaceable "
                     "(%d worker(s), demand %s)", current_workers, demand)
        elif last_one and not all_unpinned:
            # Handover 1 should already have released every pin; if it could not,
            # deleting this worker takes the site down rather than scaling to zero.
            log.warning("holding the last worker: still pinned — %s", ", ".join(sorted(pinned)))
        elif last_one and missing:
            # The pins are off and the master is eligible, but Swarm has not
            # started a task there yet. Deleting the worker now is the one move
            # that produces a gap, so we wait — indefinitely if it comes to that.
            # Keeping one worker costs a few euros a month; removing it blind
            # costs the site, and that is never the better trade.
            if _manager_wait_since is None:
                _manager_wait_since = time.time()
            waited = time.time() - _manager_wait_since
            if waited > HANDOVER_STALL_SECONDS:
                # Nudge, then report. Releasing the pin does NOT move anything:
                # Swarm places a task when it is created and never rebalances a
                # running one, so a replica whose constraint was lifted stays
                # exactly where it is. The wait above was therefore waiting for
                # an event that could not happen on its own — 37 minutes of it,
                # until an unrelated resize recreated the tasks and they landed
                # on the master by accident.
                #
                # A forced rolling update is the one thing that redeploys tasks
                # onto the now-eligible master, and it is graceful: start-first,
                # health-gated, one replica at a time, exactly like a deploy.
                nudged = handover_nudge(missing, workloads)

                M_ERRORS.labels(stage="handover").inc()
                detail = ""
                for w in workloads:
                    if w.name not in missing:
                        continue
                    for task in tasks_by_node.get(manager_id, []) or []:
                        if task.get("ServiceID") == w.id:
                            detail = (task.get("Status", {}) or {}).get("Err", "")
                            break
                log.error("handover stalled: %.0fs with every pin released and still no "
                          "replica on the master for %s. %s%s", waited, ", ".join(missing),
                          "Forcing a rolling re-placement onto the master."
                          if nudged else "The last worker stays until there is one.",
                          f" Swarm says: {detail}" if detail else
                          " Check `docker service ps` for a reservation that no longer fits.")
            else:
                log.info("holding the last worker: no replica on the master yet for %s (%.0fs)",
                         ", ".join(missing), waited)
        else:
            _manager_wait_since = None
            if last_one:
                log.info("removing the last worker: the master is already serving every "
                         "component, the fleet goes to zero")
            try:
                remove_worker(candidate)
                _last_scale_down = time.time()
            except Exception as exc:  # noqa: BLE001
                M_ERRORS.labels(stage="remove").inc()
                log.error("could not remove a worker: %s", exc)

    # 15. RIGHT-SIZE, last. A resize is itself a rolling update of every replica,
    #     so it runs after the replica count and the placement have settled —
    #     stacking it on either restarts that rollout from the beginning. The
    #     numbers it writes are picked up by the NEXT loop's discovery, which is
    #     what makes the correction visible in demand and fleet sizing.
    if RIGHT_SIZE and workloads:
        try:
            smallest_cpu = min((node_resources(n).cpu
                                for n in ([manager_node] if manager_node else []) + ready),
                               default=0)
            smallest_mem = min((node_resources(n).mem
                                for n in ([manager_node] if manager_node else []) + ready),
                               default=0)
            apply_right_sizing(workloads, measure_usage([w.name for w in workloads]),
                               smallest_cpu, smallest_mem)
        except Exception as exc:  # noqa: BLE001
            M_ERRORS.labels(stage="resize").inc()
            log.warning("right-sizing skipped this loop: %s", exc)

    export_service_metrics(workloads, wants, admitted, signals, running, pending)

    log.info(
        "workers %d/%d (floor %d, ceiling %d) · %d component(s) · demand %s · master %s · "
        "worker room %s · new %s · placement %s%s",
        current_workers, want_servers, floor, MAX_WORKERS, len(workloads), demand, manager_free,
        " + ".join(str(b.free) for b in worker_bins) or "none",
        new_free if new_free else "unknown",
        "workers" if any_pinned else "master",
        " -> workers" if want_pinned and not any_pinned else
        " -> master" if not want_pinned and any_pinned else "",
    )
    for w in workloads:
        s = signals.get(w.name)
        log.info("  %-28s %d/%d replicas (want %d) · p95 %s · cpu/replica %s · %s",
                 w.name, running.get(w.name, 0), admitted.get(w.name, w.spec_replicas),
                 wants.get(w.name, w.spec_replicas),
                 f"{s.p95:.0f}ms" if s and s.p95 is not None else "n/a",
                 f"{s.cpu:.0f}%" if s and s.cpu is not None else "n/a",
                 "workers" if w.name in pinned else "master")


def main():
    start_http_server(9200)
    log.info("autoscaler up — cluster=%s workers=%d..%d worker_type=%s dry_run=%s",
             CLUSTER, MIN_WORKERS, MAX_WORKERS, WORKER_TYPE, DRY_RUN)
    log.info("scaling targets are DISCOVERED: any service labelled %s=%s is managed, "
             "and its policy comes from its own autoscale.* labels. Nothing here "
             "names an application.", WORKLOAD_LABEL, WORKLOAD_APP)
    if MIN_WORKERS == 0:
        log.info("worker floor is 0: an idle cluster bills no Hetzner servers at all "
                 "and the master carries the load. Capacity is measured, not "
                 "configured — see the 'demand' and 'master' figures in each loop line.")
    else:
        log.info("worker floor is %d: at least that many Hetzner workers always run, so "
                 "the master never carries application traffic", MIN_WORKERS)
    while _running:
        started = time.time()
        try:
            loop()
        except Exception as exc:  # noqa: BLE001
            M_ERRORS.labels(stage="loop").inc()
            log.exception("loop failed: %s", exc)
        M_LOOP.set(time.time())
        elapsed = time.time() - started
        time.sleep(max(1, LOOP_SECONDS - elapsed))
    log.info("exiting cleanly")


if __name__ == "__main__":
    main()
