#!/usr/bin/env python3
"""
The overseer: what is happening, what it means, and what the fleet should be.

It measures every application, works out WHY a slow one is slow, hands that
answer to whoever owns it — and owns the Hetzner fleet outright: how many
machines exist, of what size, and which of them may be deleted.

WHY ONE PROCESS AND NOT TWO
---------------------------
This was two. A dispatcher decided that MongoDB was throttling an API; an
autoscaler decided that the cluster therefore needed another machine. That was
a good split right up until a SECOND manager wanted machines. Dataguard has to
buy a node to put a database replica on, and with the fleet living inside the
autoscaler there were only bad answers: give dataguard its own Hetzner token and
have two processes race on fleet size with two copies of the capacity
arithmetic, or have it ask the autoscaler — a service whose job is replica
counts — for a machine.

So the fleet came here instead. One process decides what the cluster should look
like; the autoscaler and dataguard are the two hands that make it so. There is
one capacity model, one thing that can delete a server, and one place to look
when the fleet is the wrong size.

WHAT THAT COST, STATED PLAINLY
------------------------------
The dispatcher's founding property was that the process parsing every
application's metrics held no token and changed nothing. That is over: this
process holds `HCLOUD_TOKEN`. The mitigations are the ones that were already
here — the `/signal` receiver is on the `monitoring` overlay and is never
published, `DRY_RUN` is a real switch, and every destructive path still checks
the `managedby=autoscaler` node label and refuses a node holding foreign state.

WHAT IT DECIDES, AND WHAT IT ONLY REPORTS
-----------------------------------------
  decides   how many workers exist, of what plan, and which one is deleted;
            whether applications run on the master or on workers;
            how many replicas of a service the current nodes can hold.
  reports   why a service is slow, and to whom that belongs.

Both leave through the SAME delivery. A manager subscribes by labelling its own
service — `infra.handles=local` for the autoscaler, `infra.handles=database` for
dataguard — and receives, every loop, the complete current world for the causes
it claims. Nothing here lists managers.

WANT, HOSTS, CEILING — three numbers, not one
---------------------------------------------
  want      what each service's signals ask for, from its own policy labels.
  hosts     derived from the UNCAPPED total of those wants. This is what makes
            the fleet grow toward real demand.
  ceiling   the most replicas of a service the CURRENTLY eligible nodes can
            hold. Dispatched per service; the autoscaler never exceeds it.

Collapsing want and ceiling into one number deadlocks with several services:
each gets capped to fit, the total never exceeds capacity, and no worker is ever
bought. The ceiling caps GROWTH only — it never scales anything down.

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

CAPACITY IS MEASURED, NOT CONFIGURED
------------------------------------
Every quantity is a (cpu, memory) vector in nanocores and bytes:

  a node's free capacity  = what it advertises, minus the reservations of every
                            task on it that is NOT an app workload
  demand                  = sum over app services of replicas x that service's
                            own reservation
  a new machine           = the catalogue entry for the smallest plan meeting
                            the base requirement, minus the per-node
                            reservations of the `mode: global` services

Integers throughout: summing 0.05 + 0.10 + 0.05 as floats and then comparing
demand <= free is how a packing loop acquires a one-replica flap nobody can
reproduce. The unit is the RESERVATION, not the limit, because reservations are
what Swarm's scheduler actually subtracts when placing a task.

ONE PACKER, USED FOUR TIMES
---------------------------
`place()` — first-fit over a round-robin item stream — answers the replica
ceiling, fleet sizing, node removal and worker resizing. Two algorithms that
disagree by one replica are a loop that buys a worker and immediately deletes
it.

THE HANDOVER MUST NEVER LEAVE A GAP
-----------------------------------
It now spans two processes, and it is still safe, because neither of them trusts
the other's intent — both re-derive from LIVE Swarm state every loop:

  scaling out (manager -> workers)
    1. buy workers. Replicas stay where they are; the master keeps serving.
    2. `pinned: true` is dispatched only once ready workers can hold EVERY
       replica. The autoscaler applies it with start-first, new before old.

  scaling in (workers -> manager)
    1. `pinned: false` is dispatched first, while the last worker still serves.
    2. this process refuses to delete the last worker until it can SEE a task
       of every service running on the master. Not until the autoscaler says
       so — until Swarm does.

Reversing either order produces the outage. A crash on either side resumes
rather than half-applying, because neither side stores intent.

cloudflared is the one exception: `mode: global`, no constraint, so the master
always has a registered connector and the tunnel never gaps mid-handover.

STATELESS BY DESIGN. Cooldowns derive from Hetzner creation timestamps and
sustain windows from VictoriaMetrics subqueries, so restarting this container
loses nothing.

THE FOUR WAYS THE CLUSTER SCALES, cheapest and least disruptive first
--------------------------------------------------------------------
  1. SOFT VERTICAL   right-size a replica's reservations from measured usage.
                     The autoscaler's job; it shows up here as demand changing.
  2. SOFT HORIZONTAL more replicas, onto capacity that exists. Seconds.
  3. HARD VERTICAL   grow ONE worker onto the next plan up. Minutes, and it
                     power-cycles that machine, so it happens only when another
                     ready worker can hold everything meanwhile.
  4. HARD HORIZONTAL buy another worker. Minutes, no disruption, but it pays
                     the per-node overhead tax again.

Scaling DOWN runs the other way, biggest saving first: shed replicas, then
delete a whole worker, and only shrink a worker's plan when no worker can be
deleted.
"""

import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import namedtuple
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import docker
import requests
from hcloud import Client
from hcloud.images import Image
from hcloud.locations import Location
from hcloud.networks import Network
from hcloud.server_types import ServerType
from hcloud.ssh_keys import SSHKey
from prometheus_client import Counter, Gauge, start_http_server

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from signals import classify, discovery, expressions, query, workloads  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    stream=sys.stdout)
log = logging.getLogger("overseer")


def _env(key, default=None, cast=str):
    """
    A setting, resolved default-first.

    AN EMPTY VALUE IS AN ABSENT ONE. `stacks/monitoring.yml` passes each setting
    as "${KEY}", and `docker stack deploy` substitutes a variable that infra.env
    does not carry with the empty string rather than leaving it unset — so the
    container always receives the key, and `os.environ.get` never sees its
    default. Every cluster built before a setting existed therefore delivers ""
    to a reader expecting a number: `int("")` raises at import, the process
    exits 1, and Swarm restarts it forever.
    """
    raw = os.environ.get(key)
    if raw is None or str(raw).strip() == "":
        raw = default
    if raw is None:
        raise RuntimeError(f"missing required env var {key}")
    if cast is bool:
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    return cast(raw)


CLUSTER = _env("APP_NAME", "app")
VM_URL = _env("VM_URL", "http://victoriametrics:8428")
LOOP_SECONDS = _env("LOOP_SECONDS", "60", int)
METRICS_PORT = _env("METRICS_PORT", "9210", int)
# Short. A manager that cannot answer in five seconds is one this loop must not
# wait on: the next delivery is a minute away and carries the same world.
DELIVER_TIMEOUT_SECONDS = _env("DELIVER_TIMEOUT_SECONDS", "5", int)

def _secret(name):
    path = f"/run/secrets/{name}"
    if os.path.exists(path):
        with open(path) as fh:
            return fh.read().strip()
    return _env(name)

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
DRY_RUN = _env("DRY_RUN", "false", bool)  # a real safety switch, not a rehearsal
ROTATE_TOKEN = _env("ROTATE_TOKEN_ON_SCALE_DOWN", "false", bool)

HCLOUD_TOKEN = _secret("HCLOUD_TOKEN")
LOCATION = _env("HCLOUD_LOCATION", "hel1")
NETWORK_NAME = _env("HCLOUD_NETWORK_NAME", "prod-net")
SSH_KEY_NAME = _env("HCLOUD_SSH_KEY_NAME", "")
WORKER_IMAGE = _env("WORKER_IMAGE", "ubuntu-24.04")
USERDATA_PATH = _env("WORKER_USERDATA_PATH", "/etc/infra/worker-cloud-init.yaml")

# --- vertical scaling ------------------------------------------------------
# A worker may be GROWN onto a bigger plan instead of a second worker being
# bought. It sits between "more replicas" and "more workers": one bigger box
# beats two small ones for a workload whose replicas are large, and it avoids
# paying the per-node overhead tax (`global_service_reservations`) a second
# time.
#
# OFF unless a ceiling is set, and the ceiling is a CAPACITY rather than a plan
# name. MAX_WORKERS counts servers, so it cannot cap a change that raises the
# bill without changing the count; and Hetzner adds and retires plans
# constantly, so "8 cores / 16 GB" survives a catalogue change that "cpx42"
# does not. Nothing here knows a plan name — the ladder is read from the API.
WORKER_MAX_CORES = _env("WORKER_MAX_CORES", "8", int)
WORKER_MAX_MEMORY_GB = _env("WORKER_MAX_MEMORY_GB", "16", float)
# Long, and separate from every other cooldown. A resize power-cycles a machine:
# it is a slow correction to the shape of the fleet, not a control loop chasing
# load — the replica count does that.
NODE_RESIZE_COOLDOWN = _env("NODE_RESIZE_COOLDOWN_SECONDS", "900", int)

ORPHAN_GRACE_SECONDS = 900
POST_DRAIN_GRACE = _env("POST_DRAIN_GRACE_SECONDS", "45", int)
HANDOVER_STALL_SECONDS = 900

#: Both halves of the ceiling are required. One without the other is an
#: operator who meant to cap something and capped nothing, so it is refused
#: rather than half-applied.
# --- the database ceiling --------------------------------------------------
# The SAME shape of setting, for a different mechanism, and the difference is
# why it is a second pair rather than a reuse of the one above.
#
# A worker is GROWN by power-cycling it onto a bigger plan, which is why that is
# opt-in: it is minutes of downtime for whatever was on it. A database member is
# never resized — dataguard starts a member on a bigger machine, lets it sync,
# promotes it and drops the old one — so the risk that makes vertical worker
# scaling opt-in does not exist here, and this ceiling is therefore always on.
#
# It has to exist at all because without it `bigger` had no meaning: the ladder
# is what turns "bigger" into a plan, and a database asking for a bigger machine
# with no ladder above it was handed another machine the same size, promoted it,
# dropped the old one, found itself under exactly the same pressure, and did the
# whole thing again next cooldown. Forever, and billed each time.
DB_MAX_CORES = _env("DB_MAX_CORES", "16", int)
DB_MAX_MEMORY_GB = _env("DB_MAX_MEMORY_GB", "32", float)
# MEGABYTES. The only setting here whose name does not carry its unit, and it
# sits directly under one that does — so read the number, not the name: 655360
# is 640 GB, and anything in the low hundreds here is a ceiling below the base
# plan's own disk, which empties the ladder rather than capping it.
#
# The third dimension, and the one that bites differently from the other two.
# CPU and memory being too small makes a database slow; disk being too small
# stops it, and on a plan ladder disk is not something you can add afterwards —
# it arrives welded to the plan. So this is a ceiling on which PLANS a member
# may be moved onto, not a quota on what it may store.
#
# The default is deliberately the disk of the largest plan the cores/memory
# ceiling already allows, so out of the box this bounds nothing the other two
# did not already bound. It is a cap to LOWER when storage is what you are
# watching, not a third gate to trip over on the way to the first two.
DB_MAX_STORAGE = _env("DB_MAX_STORAGE", "655360", int)

VERTICAL = WORKER_MAX_CORES > 0 and WORKER_MAX_MEMORY_GB > 0
if not VERTICAL and (WORKER_MAX_CORES > 0 or WORKER_MAX_MEMORY_GB > 0):
    log_pending_vertical = (
        "vertical scaling needs BOTH WORKER_MAX_CORES and WORKER_MAX_MEMORY_GB; "
        "only one is set, so workers will not be resized")
else:
    log_pending_vertical = ""

if MIN_WORKERS < 0:
    log.warning("MIN_WORKERS=%d is negative; using 0", MIN_WORKERS)
    MIN_WORKERS = 0
if MAX_WORKERS < MIN_WORKERS:
    log.warning("MAX_WORKERS=%d is below MIN_WORKERS=%d; using %d",
                MAX_WORKERS, MIN_WORKERS, MIN_WORKERS)
    MAX_WORKERS = MIN_WORKERS
if log_pending_vertical:
    log.warning("%s", log_pending_vertical)



# ---------------------------------------------------------------------------
# metrics about ourselves
# ---------------------------------------------------------------------------
# THE RULE THAT KEEPS THE ALERTS WORKING: gauges an alert JOINS on stay
# unlabeled and cluster-scoped, and BOTH SIDES OF ANY JOIN ARE EXPORTED BY THE
# SAME PROCESS.
#
# The second half is new and it is load-bearing. vmagent scrapes with
# `honor_labels: true`, so a metric that carries its own `service` label keeps
# it — but an unlabeled one is given the SWARM SERVICE NAME of whatever exported
# it. `overseer_placement_worker_mode == 1 and overseer_current_workers < 1` is
# a set operation on the empty label signature, and it works only because both
# gauges are scraped from this process and therefore carry the same
# `service="monitoring_overseer"`. Moving one of them to the autoscaler would
# make the expression match nothing, silently, forever — which is exactly how
# NoHealthyReplicas was dead for months.
#
# Per-service gauges are safe to split across processes precisely because they
# DO carry their own `service` label, which honor_labels preserves.

M_CURRENT = Gauge("overseer_current_workers", "Hetzner worker servers in the swarm")
# Kept alongside _current_workers because "how many boxes am I running" is a
# different question from "how many am I paying for", and the panel shows both.
M_HOSTS = Gauge("overseer_current_hosts", "Boxes running applications, master included")
M_DESIRED = Gauge("overseer_desired_workers", "Host count the overseer wants")
M_WANTED_UNCAPPED = Gauge("overseer_wanted_workers_uncapped",
                          "Workers the demand asks for, before MIN/MAX are applied")
M_MAX = Gauge("overseer_max_workers", "Configured host ceiling")
M_MIN = Gauge("overseer_effective_min_workers", "Host floor in force right now")
M_CPU = Gauge("overseer_cluster_cpu_percent", "Mean worker CPU utilisation")
M_MEM = Gauge("overseer_cluster_mem_percent", "Mean worker memory utilisation")
M_MANAGED = Gauge("overseer_managed_services", "Services carrying infra.workload=app")
M_MODE = Gauge("overseer_placement_worker_mode",
               "1 when ANY application is pinned to worker nodes, 0 when none is")
M_MIXED = Gauge("overseer_placement_mixed",
                "1 when applications disagree about placement — a handover in flight")
M_DEMAND_CPU = Gauge("overseer_demand_cpu_cores", "CPU reserved by all application replicas")
M_DEMAND_MEM = Gauge("overseer_demand_memory_bytes", "Memory reserved by all application replicas")
M_MGR_CPU = Gauge("overseer_manager_free_cpu_cores", "CPU free for applications on the master")
M_MGR_MEM = Gauge("overseer_manager_free_memory_bytes", "Memory free for applications on the master")
M_POOL_CPU = Gauge("overseer_worker_pool_free_cpu_cores", "CPU free for applications across ready workers")
M_POOL_MEM = Gauge("overseer_worker_pool_free_memory_bytes", "Memory free for applications across ready workers")
M_NEW_CPU = Gauge("overseer_new_worker_free_cpu_cores", "CPU a new worker would offer applications")
M_NEW_MEM = Gauge("overseer_new_worker_free_memory_bytes", "Memory a new worker would offer applications")
M_EVENTS = Counter("overseer_scale_events_total", "Fleet actions taken", ["direction"])
M_RESIZING = Gauge("overseer_worker_resize_in_flight",
                   "1 while a worker is being power-cycled onto another plan")
M_LEASES = Gauge("overseer_node_leases", "Nodes a manager has asked this process to hold")
M_GRANTS_PENDING = Gauge("overseer_node_requests_pending",
                         "Node requests accepted and not yet satisfied")

_SVC = ["service"]
G_SIGNAL = Gauge("overseer_signal",
                 "1 when a service is slow for the named reason", _SVC + ["cause"])
G_UNOWNED = Gauge("overseer_signal_unowned",
                  "1 when a cause has no handler and is not muted", _SVC + ["cause"])
G_LATENCY = Gauge("overseer_service_latency_ms",
                  "Sustained request latency, as the autoscaler reads it", _SVC)
G_CLAIMS = Gauge("overseer_claimed_causes", "Causes some service claims", ["cause"])
G_LOOP = Gauge("overseer_last_loop_timestamp_seconds", "Unix time of the last loop")
G_SERVICES = Gauge("overseer_watched_services", "Application services being watched")
G_DIRECTION = Gauge("overseer_direction",
                    "1 on the direction currently dispatched for a service",
                    _SVC + ["direction"])
# 0 means a manager claimed a cause and could not be reached, so it is acting on
# nothing at all. That is a louder condition than any single service's latency.
G_DELIVERY = Gauge("overseer_delivery_ok",
                   "1 when the last dispatch to this manager succeeded", ["manager"])
# Per-service capacity verdicts. Safe to export from here rather than the
# autoscaler because they carry their own `service` label; see the note above.
S_CEILING = Gauge("overseer_service_replica_ceiling",
                  "Most replicas of this service the eligible nodes can hold", _SVC)
S_WANTED = Gauge("overseer_service_wanted_replicas",
                 "Replicas this service's signals ask for, before the ceiling", _SVC)
S_STARVED = Gauge("overseer_service_min_unsatisfied",
                  "1 when the cluster cannot host the minimum", _SVC)
S_UNPLACEABLE = Gauge("overseer_service_unplaceable",
                      "1 when one replica exceeds any possible node", _SVC)
S_COST_CPU = Gauge("overseer_service_replica_cost_cpu_cores", "CPU one replica reserves", _SVC)
S_COST_MEM = Gauge("overseer_service_replica_cost_memory_bytes", "Memory one replica reserves", _SVC)
S_PINNED = Gauge("overseer_service_worker_mode",
                 "1 when this service is pinned to workers", _SVC)
C_ERRORS = Counter("overseer_errors_total", "Failures by stage", ["stage"])

_PER_SERVICE = [G_LATENCY, S_CEILING, S_WANTED, S_STARVED, S_UNPLACEABLE,
                S_COST_CPU, S_COST_MEM, S_PINNED]
_exported_services = set()

query.on_error = lambda stage: C_ERRORS.labels(stage=stage).inc()
workloads.on_error = lambda stage: C_ERRORS.labels(stage=stage).inc()

hcloud = Client(token=HCLOUD_TOKEN)
dkr = docker.DockerClient(base_url="unix:///var/run/docker.sock")

_running = True
_last_scale_down = time.time()  # conservative: wait one cooldown after a restart
_capacity_note = None

_warned = set()


def warn_once(key, message, *args):
    """
    Log a complaint once per distinct value, not once per loop.

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


workloads.on_warn = warn_once


def discover_workloads():
    return workloads.discover_workloads(
        dkr,
        on_skip=lambda name, exc: log.warning(
            "skipping %s this loop: %s", name or "the service list", exc))


def forget_vanished(current_names):
    """
    Drop metric children for services that no longer exist.

    prometheus_client keeps a labeled child forever once created, so a deleted
    component would leave `overseer_service_replica_ceiling{service="gone"}=3`
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


def discover_latency(service_names):
    return discovery.discover_latency(
        service_names,
        on_missing=lambda svc: warn_once(
            (svc, "nolatency"),
            "%s publishes no recognisable request-latency metric; scaling it on "
            "CPU alone", svc))

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
    """
    One application service, in the shape the performance half reads it.

    A thin view over `signals.workloads.Workload` rather than a second parse of
    the same labels. It used to be the second parse, and that was one function
    away from the two halves of this process disagreeing about the same
    `autoscale.up_cpu_pct`.
    """

    def __init__(self, workload):
        self.workload = workload
        self.name = workload.name
        self.enabled = workload.policy.autoscale
        self.thresholds = workloads.thresholds(workload.policy)
        self.muted = workload.muted
        self.cpu_limit = workload.cpu_limit
        self.mem_limit = workload.mem_limit

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


#: (held, peak) for one service. `held` is the sustained MINIMUM over the
#: scale-up window — was it above the line for the whole time — and `peak` the
#: MAXIMUM over the scale-down one. Two windows, two aggregates: up fast, down
#: slow, and never symmetric.
Reading = namedtuple("Reading", ["held", "peak"])
EMPTY = Reading((None, None, None), (None, None, None))

#: The recommendation history behind a shrink. One per process, shared by every
#: service, pruned each loop against what still exists.
_stabilizer = workloads.Stabilizer()


def _held_of(verdict):
    """The scale-up triple as it travels in a verdict."""
    return (verdict.get("latency_ms"), verdict.get("cpu_pct"), verdict.get("mem_pct"))


def _peak_of(verdict):
    """The scale-down triple. Absent from an older verdict, which reads as
    "no measurement" and falls back to the single-replica step."""
    return (verdict.get("latency_peak_ms"), verdict.get("cpu_peak_pct"),
            verdict.get("mem_peak_pct"))


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


def attribute(service, cpu_pct, mem_pct, dependencies):
    """
    Why is this service slow? Returns (cause, target or None).

    Evidence, in order of how much it is worth:

      1. The replicas are busy      -> it is us. More replicas is the fix, and
                                       the autoscaler has already applied it.
      2. An outbound timer this service publishes is over the same budget
                                    -> it is whatever that timer measures,
                                       named by its own label.
      3. Nothing                    -> `unknown`, which is what it is. Guessing
                                       here sends somebody to read the wrong log.

    There used to be a rule between 2 and 3: if some component in the cluster
    looked busy, blame that one. It read as a cheap upgrade over "good luck" —
    "your API is slow and your Mongo is pinned" — and it was wrong twice over.

    It is not evidence. "Busy" was CPU against the service's own limit, and a
    component with no CPU limit is measured against its RESERVATION instead; a
    Redis reserving 0.06 cores crosses the 25% line doing essentially nothing.
    Every managed database in a small cluster is permanently "busy" by that
    arithmetic, and the set was then sorted alphabetically to pick a winner.

    And a cause is not a hint. `dispatch` routes each verdict to the manager
    that claims its cause, so naming `database` on a correlation handed the
    guess to dataguard as an instruction: it grew the set, asked the overseer
    for a machine, and waited for one. That happened here — an application
    hanging on a third-party HTTP call, with no outbound timer to say so, put
    an idle Redis on the shopping list once a minute.

    So an uninstrumented service now gets `unknown`, and `unknown` is claimed by
    nobody, which raises the alert that says exactly that. Which component is
    hot at the same moment is a question the Overview page already answers, per
    node and per service, without spending anything to ask it.
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
    body = json.dumps({"at": time.time(), "from": "overseer",
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
# ---------------------------------------------------------------------------
# metric queries
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# swarm + hetzner inventory
# ---------------------------------------------------------------------------

def swarm_workers():
    return [n for n in dkr.nodes.list()
            if n.attrs.get("Spec", {}).get("Role") == "worker"]


def is_dedicated(node):
    """
    Held under a lease, so invisible to the application capacity model.

    A dedicated node is not packed, not counted as room for replicas, and never
    picked for removal by the app scale-down path. Application services keep off
    it by carrying `node.labels.dedicated != true`, which their renderer writes
    unconditionally — a constraint rather than a taint, because Swarm has no
    taints and an unenforced convention would put an API replica on a database's
    machine the first time somebody unpinned it.
    """
    labels = (node.attrs.get("Spec", {}) or {}).get("Labels") or {}
    return labels.get(NODE_DEDICATED_LABEL) == "true"


def swarm_ready_workers():
    """Workers available to APPLICATIONS. Leased machines are not."""
    return [n for n in swarm_workers()
            if n.attrs.get("Status", {}).get("State") == "ready"
            and n.attrs.get("Spec", {}).get("Availability") == "active"
            and not is_dedicated(n)]


def hetzner_servers():
    """Every server this cluster owns, leased or not."""
    return hcloud.servers.get_all(label_selector=f"cluster=={CLUSTER},role==swarm-worker")


def hetzner_workers():
    return hetzner_servers()


# Who is allowed to drain and delete a node.
#
# The autoscaler's blast radius has always been the Hetzner label selector. This
# is that ownership written onto the SWARM node, where the panel can read it and
# where a node that is not ours is visibly not ours. `adopt_workers()` derives it
# from the selector rather than anyone configuring it, so a node someone joined
# by hand carries no owner and is never drained, never deleted, never reaped.
#
# The key holds an owner NAME rather than a boolean on purpose: a node stamped
# `managedby=dbmanager` later is refused by this same check, with no code here to
# change. It is also why the panel treats the whole key as reserved rather than
# blacklisting one value.
NODE_OWNER_LABEL = "managedby"
OWNER_AUTOSCALER = "autoscaler"


def node_owner(node):
    return (node.attrs.get("Spec", {}).get("Labels") or {}).get(NODE_OWNER_LABEL, "")


def owned_by_autoscaler(node):
    return node_owner(node) == OWNER_AUTOSCALER


def adopt_workers(nodes, server_names):
    """
    Stamp `managedby=autoscaler` on every worker whose Hetzner server carries our
    selector and that is not stamped yet.

    Failing on a node leaves it unowned for a loop, which is the safe direction:
    unowned costs a server we keep paying for, never a node we should not have
    touched. Docker REPLACES a NodeSpec rather than merging it, so the whole spec
    is read, one key added, and the whole thing written back — a partial payload
    drops every other label.
    """
    for node in nodes:
        hostname = node.attrs["Description"]["Hostname"]
        if hostname not in server_names or node_owner(node):
            continue
        if DRY_RUN:
            log.info("[dry-run] would mark %s as %s=%s",
                     hostname, NODE_OWNER_LABEL, OWNER_AUTOSCALER)
            continue
        try:
            spec = node.attrs["Spec"]
            spec.setdefault("Role", "worker")
            spec.setdefault("Availability", "active")
            spec.setdefault("Labels", {})[NODE_OWNER_LABEL] = OWNER_AUTOSCALER
            node.update(spec)
            log.info("marked %s as managed by the autoscaler", hostname)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not mark ownership on %s: %s", hostname, exc)


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
# CAPACITY — measured, never configured
# ---------------------------------------------------------------------------


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
    total = workloads.node_resources(node)
    if total.cpu <= 0:
        return workloads.ZERO
    overhead = workloads.ZERO
    for task in tasks:
        if task.get("ServiceID") in app_ids:
            continue
        overhead = overhead + workloads.reservations(task.get("Spec", {}).get("Resources"))
    return total - overhead


def global_service_reservations(services):
    """
    (cpu, mem) that lands on EVERY node just for being in the cluster.

    `mode: global` services get one task per node, so their reservations are a
    per-node tax that comes off a new worker's advertised size before any of it
    counts as application capacity.
    """
    total = workloads.ZERO
    for svc in services:
        spec = svc.attrs.get("Spec", {})
        if "Global" not in (spec.get("Mode") or {}):
            continue
        total = total + workloads.reservations(spec.get("TaskTemplate", {}).get("Resources"))
    return total


def new_worker_free(services):
    """
    What one NEW machine on the base plan would offer applications, or None.

    None means "cannot size a new worker", and callers refuse to buy rather than
    guessing — with heterogeneous costs, an assumed capacity means nothing.
    """
    try:
        st = base_server_type()
    except Exception as exc:  # noqa: BLE001
        log.warning("cannot size a machine on the base plan: %s", exc)
        return None
    if st is None:
        log.warning("no plan meets the base requirement in %s", LOCATION)
        return None
    total = workloads.Res(int(st.cores * 1e9), int(st.memory * 1024 ** 3))   # hcloud reports GB
    return total - global_service_reservations(services)

# --- the plan ladder -------------------------------------------------------
# Which server types a worker may sit on, and which rung is next.
#
# Nothing here contains a plan name. Hetzner adds plans, retires plans, and does
# not offer the same ones everywhere — two plans one digit apart can be sold in
# different locations — so a hardcoded ladder would be wrong on the day it was
# written and wronger every year after.

def _family(name):
    """`abc22` -> `abc`. The letters that start a plan name ARE its family."""
    match = re.match(r"^[a-z]+", name or "")
    return match.group(0) if match else ""


def type_res(server_type):
    """A plan as the same integer vector everything else in here is measured in."""
    return workloads.Res(int(server_type.cores * 1e9),
               int(server_type.memory * 1024 ** 3))     # hcloud reports GB


#: The catalogue changes on Hetzner's timescale, not ours, and `plan_resize`
#: asks for it once per candidate per loop. Uncached that is a handful of calls
#: a minute against a 3600/hour budget shared with everything else in here.
_catalogue = {"at": 0.0, "types": None, "dc": {}}
CATALOGUE_TTL = 900


def _catalogue_types():
    if _catalogue["types"] is None or time.time() - _catalogue["at"] > CATALOGUE_TTL:
        _catalogue["types"] = list(hcloud.server_types.get_all())
        _catalogue["at"] = time.time()
        _catalogue["dc"] = {}
    return _catalogue["types"]


#: What a base plan has to be, before anything is grown from it.
#:
#: NOT A PLAN NAME. This was a `WORKER_TYPE` setting for a while and it was the wrong
#: shape twice over: Hetzner adds and retires plans constantly and does not sell
#: the same family in every location, so a name typed into infra.env is a name
#: that stops existing — and it stopped existing silently, leaving a cluster that
#: could not buy a worker at all. What is actually wanted is a REQUIREMENT, and
#: the smallest thing meeting it.
#:
#: 2 cores / 4 GB / 80 GB, shared x86, not deprecated, orderable in this
#: location. Whatever that resolves to is an OUTPUT, not a configuration, and it
#: will quietly become something else the day a line is renamed.
#:
#: The disk floor is what does most of the work. The cheap Intel tier is sold at
#: this core and memory size with half the disk, so requiring the disk excludes
#: it without needing a "cost optimised" field the API does not have.
BASE_MIN_CORES = 2
BASE_MIN_MEMORY_GB = 4
BASE_MIN_DISK_GB = 80


def base_server_type():
    """
    The smallest plan worth building on, read from the catalogue every time.

    Returns the server type or None. None means "the catalogue could not be
    read", and every caller treats that as "do not act" rather than falling back
    to a guess — a guessed plan name is how the old setting failed.
    """
    try:
        catalogue = _catalogue_types()
    except Exception as exc:                                     # noqa: BLE001
        log.warning("cannot read the server type catalogue: %s", exc)
        return None
    available = location_types()
    candidates = [
        t for t in catalogue
        if not t.deprecated
        and t.cpu_type == "shared" and t.architecture == "x86"
        and t.cores >= BASE_MIN_CORES
        and t.memory >= BASE_MIN_MEMORY_GB
        and t.disk >= BASE_MIN_DISK_GB
        and (available is None or t.name in available)
    ]
    if not candidates:
        warn_once(("nobase", LOCATION),
                  "no plan in %s is shared x86 with at least %d cores, %d GB and "
                  "%d GB of disk. Nothing can be bought until that is true.",
                  LOCATION, BASE_MIN_CORES, BASE_MIN_MEMORY_GB, BASE_MIN_DISK_GB)
        return None
    # Smallest first, and disk last: two plans with the same cores and memory
    # differ only in disk, and the smaller disk is the one a downgrade can
    # return to — `upgrade_disk=False` makes a grown disk a one-way door.
    return min(candidates, key=lambda t: (t.cores, t.memory, t.disk, t.name))


def base_type_name():
    base = base_server_type()
    return base.name if base else ""


def type_by_name(name):
    """The FULL plan record. A server's own `server_type` may be a stub with
    nothing but a name on it, and sizing arithmetic off a stub is silently zero."""
    for t in _catalogue_types():
        if t.name == name:
            return t
    return None


def location_types():
    """
    Plan names actually orderable in LOCATION, or None if that cannot be read.

    Keyed on the LOCATION rather than on the server's own datacenter, because a
    server object comes back with `datacenter=None` — so a per-datacenter lookup
    silently never filtered anything, and the ladder then offered plans this
    location does not sell. The very next rung the ladder picked was a plan that
    exists in the catalogue and cannot be bought in that location at all.

    INTERSECTION across the location's datacenters, not union: servers are
    created with a location and Hetzner chooses the datacenter, so a plan sold in
    only one of them is a plan that may not be there when it is wanted.

    None means "could not read it" and callers then do not filter — letting
    Hetzner reject one `change_type`, which the abort path already handles, beats
    disabling the feature because a listing call failed.
    """
    if "types" in _catalogue["dc"]:
        return _catalogue["dc"]["types"]
    found = None
    try:
        for dc in hcloud.datacenters.get_all():
            if (dc.location.name if dc.location else None) != LOCATION:
                continue
            here = {t.name for t in (dc.server_types.available or [])}
            found = here if found is None else (found & here)
    except Exception as exc:                                     # noqa: BLE001
        log.warning("cannot list what %s sells: %s", LOCATION, exc)
        return None
    if found is None:
        log.warning("no datacenter found in %s; not filtering the ladder", LOCATION)
    _catalogue["dc"]["types"] = found
    return found


def worker_ladder(available=None):
    """
    Plans a worker may run on, smallest first, the base plan at the bottom.

    Four filters, and each one is load-bearing:

    * SAME FAMILY, cpu type and architecture. The prefix is what separates `cpx`
      from `cx` — both are shared/x86 — while cpu type and architecture are what
      stop a shared x86 worker being "grown" onto dedicated ARM, where the image
      would not boot at all.
    * NEVER BELOW THE BASE PLAN in either dimension. That is the floor a
      downgrade returns to, and it is also what new machines are created as, so
      the two cannot drift apart — which they could when the floor was a name in
      a settings file and the ladder was read from the API.
    * DISK NEVER BELOW the floor's. Resizes are done with `upgrade_disk=False`
      so the disk stays whatever it was created with; Hetzner then refuses any
      plan whose disk is smaller than the one the server has. Growing the disk
      would be a one-way door — a server whose disk has grown can never be
      downgraded again — and this feature is worthless if it only goes up.
    * WITHIN THE CEILING, which is the operator's budget and the only reason
      this is not unbounded.
    """
    if not VERTICAL:
        return []
    return _ladder(WORKER_MAX_CORES, WORKER_MAX_MEMORY_GB, available)



def db_ladder(available=None):
    """
    Plans a database MEMBER may run on. Same floor as a worker, its own ceiling.

    Not gated on `VERTICAL`. That switch exists because growing a worker
    power-cycles it, and an operator should get to say no to that; a database
    member is replaced rather than resized, so the same switch would be turning
    off a mechanism whose risk it was never describing. Turning off vertical
    worker scaling and silently capping every database at the base plan is a
    surprise, and it is the expensive kind: `bigger` still gets asked for, still
    gets granted, and still buys a machine.
    """
    return _ladder(DB_MAX_CORES, DB_MAX_MEMORY_GB, available,
                   max_disk_gb=DB_MAX_STORAGE / 1024.0)


def _ladder(max_cores, max_memory_gb, available=None, max_disk_gb=None):
    """
    The plans between the inferred floor and the given ceiling, smallest first.

    `max_disk_gb` is optional because only databases have one. A worker's disk
    holds images and logs and is sized by whatever plan the CPU and memory
    ceilings landed on; a database's disk holds the data, so it is the dimension
    an operator most wants a hard cap on and the one they are most likely to
    exceed by accident.
    """
    if max_cores <= 0 or max_memory_gb <= 0:
        return []
    if max_disk_gb is not None and max_disk_gb <= 0:
        return []
    try:
        catalogue = _catalogue_types()
    except Exception as exc:                                     # noqa: BLE001
        log.warning("cannot read the server type catalogue: %s", exc)
        return []
    base = base_server_type()
    if base is None:
        return []
    family = _family(base.name)
    out = []
    for t in catalogue:
        if t.deprecated:
            continue
        if _family(t.name) != family:
            continue
        if t.cpu_type != base.cpu_type or t.architecture != base.architecture:
            continue
        if t.cores < base.cores or t.memory < base.memory or t.disk < base.disk:
            continue
        if t.cores > max_cores or t.memory > max_memory_gb:
            continue
        if max_disk_gb is not None and t.disk > max_disk_gb:
            continue
        if available is not None and t.name not in available:
            continue
        out.append(t)
    # By size, never by name: `cpx12` is 1 core and `cpx11` is 2, so the names
    # do not order the ladder and sorting by them silently inverts two rungs.
    out.sort(key=lambda t: (t.cores, t.memory, t.disk))
    return out


def next_rung(current_name, ladder, direction):
    """
    The next plan up (or down) from `current_name`, or None at the end.

    A current plan that is not ON the ladder — someone resized by hand, or the
    ceiling was lowered under a worker that had already grown — can still come
    DOWN to the largest rung below it, and can never go up. Stranding an
    oversized worker with no way back is the one outcome worth avoiding here.
    """
    names = [t.name for t in ladder]
    if current_name in names:
        i = names.index(current_name)
        j = i + (1 if direction > 0 else -1)
        return ladder[j] if 0 <= j < len(ladder) else None
    if direction > 0:
        return None
    try:
        current = type_by_name(current_name)
    except Exception:                                            # noqa: BLE001
        return None
    if current is None:
        return None
    smaller = [t for t in ladder
               if t.cores <= current.cores and t.memory <= current.memory
               and (t.cores, t.memory) != (current.cores, current.memory)]
    return smaller[-1] if smaller else None

# --- the packer ------------------------------------------------------------
# One algorithm, used by admission, by fleet sizing and by node removal. Two
# algorithms that disagree by one replica are a loop that buys a worker and
# immediately deletes it.

Item = namedtuple("Item", ["service", "cost", "workers_only"])


def demand_items(found, counts, pinned_names=None):
    """
    One item per replica, round-robin across services.

    Round-robin rather than service-by-service so a large application cannot
    starve a small one, and `workers_only` per item because mid-handover
    services legitimately disagree about where they may be placed.
    """
    per_service = []
    for w in found:
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


def admit(found, wants, bins, live_replicas):
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
    order = sorted(found, key=lambda w: (w.policy.priority, w.name))
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
                        base_type_name() or "the base plan", len(worker_bins))
            return len(worker_bins)
        remaining = [items[i] for i in unplaced]
        extra = 0
        while remaining:
            extra += 1
            _, still = place(remaining, [Bin(("new", extra), new_free, False)])
            if len(still) == len(remaining):
                # Nothing fit at all: a single replica is larger than any node
                # this cluster can buy. No number of servers will ever hold it.
                #
                # UNCOUNT THE RUNG. `extra` was incremented on the way into this
                # iteration as a PROPOSAL, and this branch is the proof that the
                # proposed machine holds nothing. Counting it anyway bought one
                # worker for a replica no worker can ever take: the log said "no
                # number of servers will ever place it" and the very same return
                # value ordered a server. It then stayed forever, because the
                # want never fell back to zero, so it was billed and empty and
                # the scale-down path had nothing to react to.
                extra -= 1
                for i in still:
                    C_ERRORS.labels(stage="capacity").inc()
                    warn_once((remaining[i].service, "unplaceable"),
                              "%s reserves %s, which exceeds what a whole %s offers "
                              "(%s). No number of workers will ever place it.",
                              remaining[i].service, remaining[i].cost,
                              base_type_name() or "the base plan", new_free)
                    S_UNPLACEABLE.labels(remaining[i].service).set(1)
                break
            remaining = [remaining[i] for i in still]
            if extra > MAX_WORKERS + 2:      # belt and braces against a bad cost
                break
        used += extra
    if pressured:
        used += 1
    return used


def workers_needed(found, wants, node_pressure, manager_free, worker_bins, new_free):
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
        hypothetical = demand_items(found, wants, pinned_names=set())
        _, unplaced = place(hypothetical, [Bin("master", manager_free, True)])
        if not unplaced:
            return 0

    items = demand_items(found, wants, pinned_names=set(w.name for w in found))
    servers = servers_needed(items, worker_bins, new_free, pressured)
    if pressured:
        log.info("node resource pressure at %.0f%%: requesting an extra worker",
                 node_pressure)
    # Past the master's capacity, so at least one real worker is required —
    # UNLESS the answer is genuinely zero, which happens for exactly one reason:
    # every replica that did not fit the master does not fit a whole worker
    # either, and `servers_needed` has already said so. Flooring THAT at one
    # buys a machine for a replica no machine can take, and the want never falls
    # back to zero afterwards, so it is billed and empty until someone notices.
    return max(1, servers) if servers else 0


def note_capacity(manager_free, worker_bins, new_free, found):
    """Explain the numbers when they CHANGE, not every 60 seconds."""
    global _capacity_note
    shape = (manager_free, tuple(b.free for b in worker_bins), new_free,
             tuple((w.name, w.cost) for w in found))
    if shape == _capacity_note:
        return
    _capacity_note = shape
    log.info(
        "measured capacity: master offers %s · %d worker(s) offer %s · a new %s "
        "would offer %s · replicas cost %s",
        manager_free, len(worker_bins),
        " + ".join(str(b.free) for b in worker_bins) or "nothing",
        base_type_name() or "the base plan", new_free if new_free else "unknown",
        ", ".join(f"{w.name} {w.cost}" for w in found) or "nothing",
    )
    if MIN_WORKERS == 0 and found:
        floor_items = demand_items(
            found, {w.name: w.policy.min_replicas for w in found},
            pinned_names=set())
        _, unplaced = place(floor_items, [Bin("master", manager_free, True)])
        if unplaced:
            log.warning(
                "the master cannot hold every component's minimum (%s), so the "
                "cluster can never return to the free zero-worker state. Give it "
                "more CPU/RAM, lower a minimum, or shrink a reservation.",
                ", ".join(f"{w.name}x{w.policy.min_replicas}" for w in found),
            )

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
# signals
# ---------------------------------------------------------------------------

def read_node_pressure(worker_count):
    """
    Whichever of CPU or memory is fullest on the boxes that hold the replicas.

    A PLACEMENT GUARD, never a scaling trigger — it averages in the exporters,
    the tunnel connector and everything else on the machine. It is the last
    per-signal query this process makes: everything about how a SERVICE is
    performing now arrives from the overseer.
    """
    # With an empty fleet the worker-scoped guards have no series to return, so
    # measure the box that is actually holding the replicas instead.
    if worker_count:
        node_cpu, node_mem = query.vm_query(CPU_EXPR), query.vm_query(MEM_EXPR)
    else:
        node_cpu, node_mem = query.vm_query(MGR_CPU_EXPR), query.vm_query(MGR_MEM_EXPR)
    if node_cpu is not None:
        M_CPU.set(node_cpu)
    if node_mem is not None:
        M_MEM.set(node_mem)
    pressures = [v for v in (node_cpu, node_mem) if v is not None]
    return max(pressures) if pressures else None

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


def create_worker(server_type=None, labels=None, purpose="app"):
    """
    Buy one machine and let cloud-init join it to the swarm.

    `labels` carries a `lease` when the machine is being bought FOR a manager
    rather than for the application pool. That one Hetzner label is what makes
    the node findable again after a restart of this process, and the matching
    swarm node label — written by cloud-init from the same value — is what keeps
    application replicas off it.
    """
    server_type = server_type or base_type_name()
    if not server_type:
        raise RuntimeError(
            f"no plan in {LOCATION} is shared x86 with at least {BASE_MIN_CORES} "
            f"cores, {BASE_MIN_MEMORY_GB} GB and {BASE_MIN_DISK_GB} GB of disk")
    extra = dict(labels or {})
    lease = extra.get("lease", "")
    token = worker_join_token()
    manager_ip = manager_private_ip()
    with open(USERDATA_PATH) as fh:
        user_data = fh.read()
    user_data = user_data.replace("__SWARM_TOKEN__", token)
    user_data = user_data.replace("__MANAGER_IP__", manager_ip)
    # A leased machine joins PAUSED. `docker swarm join` cannot set node labels,
    # so there is a window between joining and being labelled in which nothing
    # keeps application replicas off a database's machine — and a paused node
    # accepts no new tasks at all, which closes it. `adopt_leases` labels it and
    # then sets it active, in that order.
    user_data = user_data.replace("__JOIN_AVAILABILITY__",
                                  "pause" if lease else "active")

    prefix = "db" if lease else "worker"
    name = f"{CLUSTER}-{prefix}-{int(time.time())}"
    if DRY_RUN:
        log.info("[dry-run] would create %s (%s, %s)", name, server_type, purpose)
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
        server_type=ServerType(name=server_type),
        image=Image(name=WORKER_IMAGE),
        location=Location(name=LOCATION),
        networks=[Network(id=network.id)],
        ssh_keys=ssh_keys,
        user_data=user_data,
        labels=dict({"cluster": CLUSTER, "role": "swarm-worker"}, **extra),
        start_after_create=True,
    )
    M_EVENTS.labels(direction="up" if not lease else "lease").inc()
    log.info("created %s %s (%s in %s)%s", purpose, name, server_type, LOCATION,
             f" for lease {lease}" if lease else "")


def fits_without(node_id, ready, node_free, items, manager_free):
    """
    Would every wanted replica still be placeable if this node vanished?

    "Can I delete it" and "can I take it offline for four minutes to resize it"
    are the SAME question, so they are the same code. Two nearly-identical
    capacity tests that disagree by one replica is how you get a loop that
    drains a node it should not have touched.

    `manager_free` is passed only when every app is unpinned; without it the
    master is not a placement target and must not be counted as one.
    """
    bins = [Bin(n.id, node_free.get(n.id, workloads.ZERO), False)
            for n in ready if n.id != node_id]
    if manager_free is not None:
        bins.append(Bin("master", manager_free, True))
    _, unplaced = place(items, bins)
    return not unplaced


def would_lose_last_replica(node_id, tasks_by_node, found):
    """
    Services whose ONLY running replica is on this node.

    Capacity is not availability. `fits_without` proves the replicas would fit
    somewhere else; it says nothing about the gap while they are being recreated,
    because a drain STOPS a task and Swarm starts its replacement afterwards —
    there is no start-first for rescheduling, only for updates.

    So a service with two replicas spread over two nodes rides a drain out on the
    surviving one, and a service whose single replica sits here goes down for as
    long as it takes to pull and start elsewhere. That is the difference between
    "reduced capacity" and "an outage", and it is the question this asks.
    """
    elsewhere = {w.id: 0 for w in found if w.spec_replicas >= 1}
    for nid, tasks in tasks_by_node.items():
        if nid == node_id:
            continue
        for task in tasks:
            sid = task.get("ServiceID")
            if sid in elsewhere and task.get("Status", {}).get("State") == "running":
                elsewhere[sid] += 1
    by_id = {w.id: w for w in found}
    return [by_id[sid].name for sid, n in elsewhere.items() if n < 1]


def holds_foreign_state(node, tasks_by_node, app_ids):
    """
    Tasks on this node that this autoscaler does not manage and that are not
    global — something replicated that may be holding state.

    Deleting such a node is data loss. Draining one to resize it is not, but it
    IS an outage for whatever that is, so both paths refuse the node and say so.
    """
    return [t for t in tasks_by_node.get(node.id, [])
            if t.get("ServiceID") not in app_ids
            and t.get("Status", {}).get("State") == "running"
            and not _is_global(t)]


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

    # A worker the autoscaler did not create is capacity it may use and must not
    # remove — someone else joined it and owns its lifecycle.
    mine = [n for n in ready if owned_by_autoscaler(n)]

    for node in sorted(mine, key=created, reverse=True):
        # A node carrying something this loop does not understand — a replicated,
        # non-global service that is not an app workload — may be holding state.
        # Deleting it is data loss, so skip it and say so.
        foreign = holds_foreign_state(node, tasks_by_node, app_ids)
        if foreign:
            warn_once((node.id, "foreign"),
                      "not removing %s: it runs %d task(s) this autoscaler does not "
                      "manage, which may be holding state",
                      node.attrs["Description"]["Hostname"], len(foreign))
            continue
        if fits_without(node.id, ready, node_free, items, manager_free):
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

    # The last gate before a drain. Candidate selection already filters to owned
    # nodes; this repeats the check because the cost of the two disagreeing is a
    # node someone else's manager depends on, drained and deleted.
    if not owned_by_autoscaler(node):
        log.warning("refusing to drain %s: it is managed by %r, not the autoscaler",
                    hostname, node_owner(node) or "nobody")
        return

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

# ---------------------------------------------------------------------------
# vertical scaling — growing a worker instead of buying one
# ---------------------------------------------------------------------------
# A resize power-cycles a machine, so it takes minutes. It is therefore a STATE
# MACHINE advanced one step per loop, never a blocking call: `remove_worker`
# already blocks for up to four minutes and `AutoscalerStalled` fires at five,
# so a drain-then-poweroff-then-change-then-boot done inline would trip the
# alert on every single resize.
#
# Exactly one runs at a time, cluster-wide, and while one is in flight no node
# is created and none is removed. That is the whole conflict story: three things
# that change the size of the fleet, and only ever one of them moving.

#: Per-phase deadline. Past it the resize ABORTS, and aborting always means
#: putting the node back into service — a node left drained and powered off is
#: capacity you are paying for and not using, which is worse than never having
#: tried.
RESIZE_DEADLINES = {
    "draining": 300, "verifying": 300, "powering_off": 240, "changing": 600,
    "powering_on": 300, "rejoining": 420,
}

_resize = None
_last_node_resize = 0.0


def resize_busy():
    return _resize is not None


def set_availability(node, value):
    """
    One field of a NodeSpec, with the rest re-stated.

    Docker REPLACES a node spec rather than merging it, so posting a partial
    payload drops every label — including `managedby`, which is the only thing
    that makes this node ours to touch at all.
    """
    spec = dict(node.attrs.get("Spec") or {})
    if spec.get("Availability") == value:
        return
    spec.setdefault("Role", "worker")
    spec["Labels"] = dict(spec.get("Labels") or {})
    spec["Availability"] = value
    node.update(spec)


def running_elsewhere(service_ids, node_id):
    """
    Running task count per service, excluding one node. {} if it cannot be read.

    Asked of Swarm directly rather than of the packer. `place()` models CPU and
    memory and nothing else — not published ports, not volume affinity, not
    `max_replicas_per_node`, not a placement constraint that happens to match
    only the node being drained. So "it fits" and "it actually restarted over
    there" are different claims, and only the second one is availability.
    """
    try:
        tasks = dkr.api.tasks(filters={"desired-state": "running"})
    except Exception as exc:                                     # noqa: BLE001
        log.warning("cannot read tasks while verifying a drain: %s", exc)
        return {}
    counts = {sid: 0 for sid in service_ids}
    for task in tasks:
        sid = task.get("ServiceID")
        if (sid in counts and task.get("NodeID") != node_id
                and task.get("Status", {}).get("State") == "running"):
            counts[sid] += 1
    return counts


def begin_resize(node, server, target, direction, found=()):
    global _resize
    hostname = node.attrs["Description"]["Hostname"]
    if DRY_RUN:
        log.info("[dry-run] would resize %s %s -> %s", hostname,
                 server.server_type.name, target.name)
        # True, so the caller does NOT also report buying a server: the two are
        # alternatives, and a rehearsal that prints both describes a loop that
        # cannot happen.
        return True
    _resize = {
        "node_id": node.id,
        "hostname": hostname,
        # Snapshotted here because the machine advances above discovery and has
        # no workload list of its own.
        "services": {w.id: w.name for w in found if w.spec_replicas >= 1},
        "from": server.server_type.name,
        "target": target.name,
        "direction": direction,
        "phase": "draining",
        "since": time.time(),
        "grace_until": 0.0,
    }
    log.info("resizing %s: %s -> %s (%s)", hostname, server.server_type.name,
             target.name, "up" if direction > 0 else "down")
    return True


def _resize_phase(name):
    _resize["phase"] = name
    _resize["since"] = time.time()
    log.info("resize %s: %s", _resize["hostname"], name)


def end_resize(ok, why=""):
    """
    Put the node back into service, whatever happened.

    Called on success AND on every failure path. The node is left `active` and
    the server left running even when the resize did not happen, because the
    alternative — a drained, powered-off worker nobody notices — costs money and
    capacity silently.
    """
    global _resize, _last_node_resize
    state = _resize
    _resize = None
    if state is None:
        return
    _last_node_resize = time.time()
    try:
        server = hcloud.servers.get_by_name(state["hostname"])
        if server is not None and server.status == "off":
            server.power_on()
            log.info("resize %s: powered back on", state["hostname"])
    except Exception as exc:                                     # noqa: BLE001
        log.error("resize %s: could not power the server back on: %s",
                  state["hostname"], exc)
    try:
        node = dkr.nodes.get(state["node_id"])
        set_availability(node, "active")
    except Exception as exc:                                     # noqa: BLE001
        log.error("resize %s: could not return the node to active: %s",
                  state["hostname"], exc)
    if ok:
        M_EVENTS.labels(direction="resize-up" if state["direction"] > 0
                        else "resize-down").inc()
        log.info("resize %s complete: %s -> %s", state["hostname"],
                 state["from"], state["target"])
        return

    # Say what the machine ACTUALLY ended up as, not what was attempted. "The
    # node is being returned to service" is a hope; the restore above can fail
    # too, and a resize that left a server switched off has to be findable by
    # reading the log rather than by noticing the bill.
    C_ERRORS.labels(stage="resize").inc()
    plan = status = avail = "unknown"
    try:
        server = hcloud.servers.get_by_name(state["hostname"])
        if server is not None:
            plan, status = server.server_type.name, server.status
        else:
            plan = status = "server gone"
    except Exception:                                            # noqa: BLE001
        pass
    try:
        avail = (dkr.nodes.get(state["node_id"]).attrs.get("Spec") or {}).get(
            "Availability", "unknown")
    except Exception:                                            # noqa: BLE001
        avail = "node gone"
    log.error("resize %s abandoned in %s: %s. Now on %s, power %s, availability "
              "%s%s", state["hostname"], state["phase"], why, plan, status, avail,
              "" if (status == "running" and avail == "active")
              else " — NEEDS ATTENTION: this node is not serving")


def advance_resize():
    """
    Move the in-flight resize on by one step. Returns True while it is busy.

    Every step is idempotent and non-blocking: it looks at where things are and
    acts only if that phase's action has not already taken effect, so a loop
    that dies mid-resize resumes from the real state rather than from a memory
    of it.
    """
    if _resize is None:
        return False
    phase = _resize["phase"]
    late = time.time() - _resize["since"] > RESIZE_DEADLINES.get(phase, 300)

    try:
        node = dkr.nodes.get(_resize["node_id"])
    except Exception as exc:                                     # noqa: BLE001
        end_resize(False, f"the swarm node is gone ({exc})")
        return False

    if phase == "draining":
        try:
            set_availability(node, "drain")
            remaining = tasks_on_node(_resize["node_id"])
        except Exception as exc:                                 # noqa: BLE001
            if late:
                end_resize(False, f"cannot drain ({exc})")
            return True
        if remaining and not late:
            log.info("resize %s: waiting for %d task(s) to leave",
                     _resize["hostname"], len(remaining))
            return True
        if remaining:
            # ABANDON, never force. `remove_worker` powers through a stuck drain
            # because removal has to complete for the fleet to reach its floor;
            # a resize does not have to happen at all, so cutting live tasks off
            # a machine to save a few euros is the wrong trade. Put it back.
            end_resize(False, f"{len(remaining)} task(s) would not leave")
            return False
        # cloudflared runs global on every worker and needs ~30s to let its edge
        # connections go on SIGTERM. Cutting this short drops live requests —
        # the same grace `remove_worker` waits out, for the same reason.
        if not _resize["grace_until"]:
            _resize["grace_until"] = time.time() + POST_DRAIN_GRACE
            return True
        if time.time() < _resize["grace_until"]:
            return True
        _resize_phase("verifying")
        return True

    if phase == "verifying":
        # The tasks LEFT this node. That is not the same as them having started
        # somewhere else, and the difference is the whole outage. Swarm may be
        # unable to place them for reasons the packer never models — a published
        # port already taken, a volume bound to this host, max_replicas_per_node,
        # a constraint that only this node satisfies.
        #
        # So the node stays UP and drained until every service is serving
        # elsewhere. If that never happens, un-drain and abandon: the machine is
        # still here and can take its tasks straight back, which is the cheapest
        # recovery available and stops existing once it is powered off.
        counts = running_elsewhere(_resize["services"], _resize["node_id"])
        missing = sorted(_resize["services"][sid] for sid, n in counts.items() if n < 1)
        if not counts and _resize["services"]:
            if late:
                end_resize(False, "could not confirm the drained tasks restarted")
                return False
            return True
        if missing and not late:
            log.info("resize %s: waiting for %s to come back elsewhere",
                     _resize["hostname"], ", ".join(missing))
            return True
        if missing:
            end_resize(False, f"{', '.join(missing)} did not restart elsewhere")
            return False
        _resize_phase("powering_off")
        return True

    try:
        server = hcloud.servers.get_by_name(_resize["hostname"])
    except Exception as exc:                                     # noqa: BLE001
        if late:
            end_resize(False, f"cannot read the server ({exc})")
        return True
    if server is None:
        end_resize(False, "the Hetzner server is gone")
        return False

    if phase == "powering_off":
        if server.status == "off":
            target = hcloud.server_types.get_by_name(_resize["target"])
            if target is None:
                end_resize(False, f"plan {_resize['target']} vanished from the catalogue")
                return False
            # upgrade_disk=False, ALWAYS. A grown disk can never shrink, so an
            # upgraded one turns every future downscale into a permanent no.
            server.change_type(target, upgrade_disk=False)
            _resize_phase("changing")
        elif late:
            end_resize(False, "the server would not power off")
        elif server.status == "running":
            server.power_off()
        return True

    if phase == "changing":
        if server.server_type.name == _resize["target"]:
            if server.status == "off":
                server.power_on()
            _resize_phase("powering_on")
        elif late:
            end_resize(False, "the plan change did not take effect")
        return True

    if phase == "powering_on":
        if server.status == "running":
            _resize_phase("rejoining")
        elif server.status == "off":
            server.power_on()
        elif late:
            end_resize(False, "the server would not come back up")
        return True

    if phase == "rejoining":
        if node.attrs.get("Status", {}).get("State") == "ready":
            end_resize(True)
            return False
        if late:
            end_resize(False, "the node did not rejoin the swarm")
            return False
        return True

    end_resize(False, f"unknown phase {phase}")
    return False


def _resize_candidates(ready, tasks_by_node, app_ids):
    """
    Workers this autoscaler may power-cycle, with their live plan.

    Owned, ready, active, and carrying nothing it does not manage. A worker
    someone joined by hand is capacity the packer may use and a machine it must
    never reboot.
    """
    out = []
    for node in ready:
        if not owned_by_autoscaler(node):
            continue
        if holds_foreign_state(node, tasks_by_node, app_ids):
            continue
        hostname = node.attrs["Description"]["Hostname"]
        try:
            server = hcloud.servers.get_by_name(hostname)
        except Exception as exc:                                 # noqa: BLE001
            log.warning("cannot read %s: %s", hostname, exc)
            continue
        if server is None or server.status != "running":
            continue
        out.append((node, server))
    return out


def plan_resize(ready, node_free, live_items, want_items, manager_free,
                tasks_by_node, app_ids, found, direction):
    """
    Which worker to grow (or shrink), and onto which plan. None if none is safe.

    THE HA RULE, and it is not negotiable: a second ready worker must exist, and
    everything must still fit without this one. The node is drained and powered
    off for minutes, so "there is another worker" alone is not enough — that
    worker has to actually have room, which is the packer's question and not a
    counting question. `fits_without` is the same test node REMOVAL uses,
    deliberately: taking a node away for four minutes and taking it away forever
    need the same guarantee.

    TWO demand sets, and confusing them makes the feature dead code. The offline
    window has to hold what is RUNNING (`live_items`); whether growing was worth
    it is judged against what is WANTED (`want_items`). Test the drain against
    the want and it can never pass — the want not fitting is the entire reason
    we are here, so every candidate would be refused and no worker would ever
    grow.

    Growing is tried smallest-worker-first so a fleet levels up evenly instead
    of growing one giant beside a row of small ones; shrinking is
    largest-worker-first for the same reason from the other end. Both orders are
    deterministic, which is what stops two loops disagreeing and flapping.
    """
    if len(ready) < 2:
        return None
    sized = []
    for node, server in _resize_candidates(ready, tasks_by_node, app_ids):
        # The full plan record, never the stub hanging off the server: a stub
        # carries a name and nothing else, and sizing arithmetic on it is a
        # silent zero that makes every delta look free.
        current = type_by_name(server.server_type.name)
        if current is None:
            log.warning("%s runs unknown plan %s; not resizing it",
                        server.name, server.server_type.name)
            continue
        sized.append((node, server, current))
    sized.sort(key=lambda row: (row[2].cores, row[2].memory, row[2].name),
               reverse=direction < 0)

    for node, server, current in sized:
        ladder = worker_ladder(location_types())
        if not ladder:
            continue
        # The ACTUAL disk on this machine, which is not the same as its type's
        # disk. A resize done with `upgrade_disk=False` leaves an 80 GB disk on a
        # plan whose nominal disk is 160, and Hetzner refuses any plan whose disk
        # is smaller than the one the server really has. Reading it from the
        # server rather than assuming it is what turns "downgrades are allowed
        # because we never upgraded the disk" from a claim about our own code
        # into a fact checked against the machine — including when somebody
        # upgraded that disk by hand in the console, which is a one-way door this
        # cannot undo and must not keep retrying.
        # Falls back to the ladder FLOOR, not to the current type's disk: this
        # code only ever resizes with `upgrade_disk=False`, so a worker it made
        # still carries the disk the base plan was created with. Falling back to
        # the current type's nominal disk would read a grown worker as having a
        # grown disk and silently disable every downscale.
        min_disk = getattr(server, "primary_disk_size", None) or ladder[0].disk
        usable = [t for t in ladder if t.disk >= min_disk]
        target = next_rung(current.name, usable, direction)
        if target is None:
            if direction < 0 and any(t.disk < min_disk for t in ladder):
                warn_once((server.name, "disk-locked"),
                          "%s cannot be downgraded: its disk is %d GB, and every "
                          "smaller plan offers less. A disk upgrade is permanent, "
                          "so this worker stays on %s until it is replaced.",
                          server.name, min_disk, current.name)
            continue
        # Can the rest of the fleet carry what is running while this node is
        # off? Same test node REMOVAL uses, on the same live demand.
        if not fits_without(node.id, ready, node_free, live_items, manager_free):
            continue
        # Capacity is not availability. Fitting somewhere else is not the same as
        # staying up while you get there, and a resize is optional — so it holds
        # itself to the stricter bar that node removal, which sometimes has to
        # happen, cannot.
        losing = would_lose_last_replica(node.id, tasks_by_node, found)
        if losing:
            warn_once((node.id, "sole-replica", tuple(sorted(losing))),
                      "not resizing %s: it holds the only running replica of %s, "
                      "which would be down for the drain. Raise that service's "
                      "minimum to 2 and this becomes free.",
                      node.attrs["Description"]["Hostname"], ", ".join(sorted(losing)))
            continue
        delta = type_res(target) - type_res(current)
        after = [Bin(n.id, node_free.get(n.id, workloads.ZERO) + (delta if n.id == node.id else workloads.ZERO),
                     False) for n in ready]
        if direction > 0:
            # Worth a power cycle only if the whole want then FITS — asked of the
            # packer directly rather than of `servers_needed`, which cannot
            # price a new server when Hetzner is unreachable and returns the
            # current fleet size, a value that reads as "growing was enough"
            # when it means "cannot tell". Growth that does not close the gap is
            # an outage for nothing; buy a server instead.
            _, unplaced = place(want_items, after)
            if unplaced:
                continue
        else:
            # Shrink only if the smaller fleet still holds what is running.
            _, unplaced = place(live_items, after)
            if unplaced:
                continue
        return node, server, target
    return None


def reap_orphans():
    """Hetzner servers that never joined, and swarm nodes that went away."""
    workers = swarm_workers()
    swarm_hostnames = {n.attrs["Description"]["Hostname"] for n in workers}
    servers = hetzner_workers()
    hetzner_names = {s.name for s in servers}

    # Before anything is judged removable. A node that joins and dies inside one
    # loop is the only window where ours goes unstamped, and the cost of that is
    # a leaked swarm entry rather than a deleted node.
    adopt_workers(workers, hetzner_names)

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

    for node in workers:
        hostname = node.attrs["Description"]["Hostname"]
        state = node.attrs.get("Status", {}).get("State")
        if not owned_by_autoscaler(node):
            continue
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
# node leases — how a manager asks for a machine
# ---------------------------------------------------------------------------
# Dataguard needs a dedicated machine to put a database replica on. It does not
# get a Hetzner token to buy one: it POSTs its COMPLETE current set of leases
# here, every loop, and this process reconciles the fleet against them. Level
# triggered, exactly like the dispatch going the other way — a request that
# fails to arrive is corrected by the next one a minute later, and there is no
# queue to get stuck.
#
# A LEASE IS A PROMISE NOT TO DELETE, not a promise to keep paying. A node under
# lease is invisible to the application capacity model: it is not packed, not
# counted as app room, and never chosen for removal by the app scale-down path.
# When the lease disappears the node becomes ORDINARY, not doomed — it is then
# removable by the normal rules, which still refuse a node holding foreign
# state. That is deliberate: the failure mode of a dataguard outage must be a
# machine that costs money, never one that takes a database with it.

REQUEST_PORT = _env("REQUEST_PORT", "9211", int)
#: A lease older than this with nothing renewing it is stale. Long, because the
#: only cost of holding one too long is a server, and the cost of dropping one
#: too early is a database.
LEASE_TTL_SECONDS = _env("LEASE_TTL_SECONDS", "900", int)

#: Swarm node labels. `dedicated` is what keeps application replicas off the
#: machine — every app service carries `node.labels.dedicated != true`, written
#: by the component renderer — and `lease` is what a database member service
#: constrains itself to.
NODE_LEASE_LABEL = "lease"
NODE_DEDICATED_LABEL = "dedicated"

_leases_lock = threading.Lock()
_leases = {}                 # lease name -> (received at, request dict)


def record_leases(payload):
    """Replace one manager's whole lease set. Returns how many it holds."""
    now = time.time()
    holder = payload.get("from") or "unknown"
    received = payload.get("leases") or []
    with _leases_lock:
        for name in [k for k, (_, v) in _leases.items() if v.get("holder") == holder]:
            del _leases[name]
        for request in received:
            name = request.get("lease")
            if not name:
                continue
            _leases[name] = (now, dict(request, holder=holder))
    return len(received)


def live_leases(now=None):
    """{lease: request} for every lease still inside its TTL."""
    now = now or time.time()
    with _leases_lock:
        return {name: req for name, (at, req) in _leases.items()
                if now - at <= LEASE_TTL_SECONDS}


class _RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):                                   # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 1_000_000:
            self.send_error(400, "empty or oversized body")
            return
        try:
            payload = json.loads(self.rfile.read(length))
            count = record_leases(payload)
        except Exception as exc:  # noqa: BLE001
            C_ERRORS.labels(stage="lease").inc()
            self.send_error(400, f"unreadable lease set: {exc}")
            return
        body = json.dumps({"accepted": count,
                           "nodes": lease_nodes()}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        """One line per request would be 1440 a day saying nothing."""


def serve_requests():
    """
    Listen on the monitoring overlay only.

    Not published to a host port and not on `edge`: the only things that can
    reach this are the infrastructure services on that network. An application
    that could POST here could order servers.
    """
    server = ThreadingHTTPServer(("0.0.0.0", REQUEST_PORT), _RequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name="lease-receiver")
    thread.start()
    log.info("listening for node leases on :%d (a lease expires after %ds)",
             REQUEST_PORT, LEASE_TTL_SECONDS)
    return server


def dedicated_nodes():
    """Swarm nodes held under a lease, by lease name."""
    out = {}
    for node in dkr.nodes.list():
        labels = (node.attrs.get("Spec", {}) or {}).get("Labels") or {}
        name = labels.get(NODE_LEASE_LABEL)
        if name:
            out[name] = node
    return out


def lease_nodes():
    """
    {lease: {node, hostname, state}} — what each lease actually has right now.

    This is the answer dataguard acts on, and it is read from Swarm rather than
    remembered, so a restart of either process changes nothing.
    """
    out = {}
    # Computed once per call, not per lease: every one of these is a catalogue
    # read behind a cache, and a cluster with six database members would take
    # the same answer six times.
    topped = {}
    for name, node in dedicated_nodes().items():
        desc = node.attrs.get("Description", {}) or {}
        owner = _lease_owner(name)
        if owner not in topped:
            try:
                topped[owner] = at_max_plan(name)
            except Exception as exc:                             # noqa: BLE001
                # Unknown is reported as NOT topped out. The consequence of
                # guessing wrong in this direction is one machine; guessing
                # wrong the other way stops a database growing and says nothing.
                log.warning("cannot tell whether %s is at its plan ceiling: %s",
                            owner, exc)
                topped[owner] = False
        out[name] = {
            "node": node.id,
            "hostname": desc.get("Hostname", ""),
            "state": (node.attrs.get("Status", {}) or {}).get("State", "unknown"),
            "availability": (node.attrs.get("Spec", {}) or {}).get("Availability", ""),
            "cpu": workloads.node_resources(node).cores,
            "memory_bytes": workloads.node_resources(node).mem,
            "at_max_plan": topped[owner],
        }
    return out


def adopt_leases():
    """
    Label a leased machine, then let it schedule. In that order, always.

    A machine bought for a database joins paused, because `docker swarm join`
    cannot set node labels and an unlabelled node has nothing for
    `node.labels.dedicated != true` to match — so for the seconds between
    joining and being labelled, Swarm would happily put an application replica
    on a database's disk. Labelling first and activating second removes the
    window entirely rather than making it small.
    """
    wanted = live_leases()
    if not wanted:
        return
    by_host = {}
    for server in hetzner_servers():
        name = server.labels.get("lease")
        if name in wanted:
            by_host[server.name] = name
    for node in swarm_workers():
        hostname = (node.attrs.get("Description") or {}).get("Hostname", "")
        lease = by_host.get(hostname)
        if not lease:
            continue
        spec = dict(node.attrs.get("Spec") or {})
        labels = dict(spec.get("Labels") or {})
        if labels.get(NODE_LEASE_LABEL) == lease and spec.get("Availability") == "active":
            continue
        if DRY_RUN:
            log.info("[dry-run] would mark %s as dedicated to %s", hostname, lease)
            continue
        # Docker REPLACES a NodeSpec rather than merging it, so the whole spec is
        # read, two keys added, and the whole thing written back — a partial
        # payload drops every other label, including `managedby`.
        labels[NODE_DEDICATED_LABEL] = "true"
        labels[NODE_LEASE_LABEL] = lease
        spec["Labels"] = labels
        spec["Availability"] = "active"
        try:
            node.update(spec)
            log.info("%s is now dedicated to %s and scheduling", hostname, lease)
        except Exception as exc:                                 # noqa: BLE001
            C_ERRORS.labels(stage="lease").inc()
            log.error("could not mark %s as dedicated: %s", hostname, exc)


def _lease_owner(lease):
    """The component a lease belongs to. Leases are named `<component>/<index>`."""
    return (lease or "").split("/", 1)[0]


def held_plan(lease):
    """
    The biggest plan this lease's COMPONENT is already running on, or None.

    An upgrade replaces a member rather than resizing one, so the machine being
    replaced is still there when the replacement is ordered, and it is the only
    record of which rung this database has reached. Reading it back is what makes
    `bigger` mean the next rung up rather than, forever, the second rung: asking
    from the base every time is why a database could never get past one upgrade,
    and why it kept buying the same machine and calling it growth.
    """
    owner = _lease_owner(lease)
    if not owner:
        return None
    best = None
    for server in hetzner_servers():
        other = server.labels.get("lease")
        if not other or _lease_owner(other) != owner:
            continue
        try:
            found = type_by_name(server.server_type.name)
        except Exception:                                        # noqa: BLE001
            continue
        if found is None:
            continue
        if best is None or (found.cores, found.memory) > (best.cores, best.memory):
            best = found
    return best


def lease_plan(request):
    """
    Which plan to buy for a lease. Never a name the requester chose.

    A manager asks for the BASE plan or for something `bigger`; only this
    process can read the catalogue, and a plan name crossing the wire is a name
    that stops existing the day Hetzner retires the line. `bigger` walks one rung
    up the DATABASE ladder from whatever that component is already on.
    """
    base = base_server_type()
    if base is None:
        return ""
    if not request.get("bigger"):
        return base.name
    ladder = db_ladder(location_types())
    current = held_plan(request.get("lease")) or base
    up = next_rung(current.name, ladder, 1)
    if up is None:
        # At the top of the ladder. The machine is still bought — a member was
        # asked for and refusing to provide one strands the transition half done
        # — but `lease_nodes` reports the ceiling, so the NEXT decision is
        # `at_ceiling` rather than another identical purchase.
        return current.name
    return up.name


def at_max_plan(lease):
    """Is this lease's component already on the biggest plan the ceiling allows?"""
    ladder = db_ladder(location_types())
    if not ladder:
        return True
    current = held_plan(lease)
    if current is None:
        return False
    return next_rung(current.name, ladder, 1) is None


def reconcile_leases():
    """
    Buy a machine for every lease that has none. Never deletes one.

    A lease with no node and no server already provisioning is the only thing
    that causes a purchase here. Releasing is the absence of a lease, and its
    only effect is that the node stops being invisible to the ordinary removal
    path — which still refuses to delete a node with foreign state on it.
    """
    wanted = live_leases()
    M_LEASES.set(len(wanted))
    have = dedicated_nodes()
    booting = {s.labels.get("lease") for s in hetzner_servers()
               if s.labels.get("lease")} - set(have)
    missing = [name for name in wanted if name not in have and name not in booting]
    M_GRANTS_PENDING.set(len(missing))
    for name in missing:
        request = wanted[name]
        try:
            create_worker(server_type=lease_plan(request),
                          labels={"lease": name},
                          purpose=request.get("purpose") or "lease")
        except Exception as exc:  # noqa: BLE001
            C_ERRORS.labels(stage="create").inc()
            log.error("could not provision the node for lease %s: %s", name, exc)
            break
    # A node whose lease is gone is not deleted here. It stops being dedicated
    # so the ordinary path can consider it, and that path has every guard.
    for name, node in have.items():
        if name in wanted:
            continue
        warn_once(("leasegone", name),
                  "the lease %s is no longer held, so %s is now an ordinary "
                  "worker. It will only be removed once nothing is running on "
                  "it.", name, (node.attrs.get("Description") or {}).get("Hostname", node.id[:12]))


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------

def _fleet_hold(reason, *args):
    """
    Everything fleet-shaped is skipped this loop; the performance half is not.

    Losing the Hetzner API must not also stop telling the autoscaler which
    services are slow — those replicas fit on machines that already exist.
    """
    C_ERRORS.labels(stage="inventory").inc()
    log.error(reason, *args)


def loop():
    global _last_scale_down

    # 1. Reaping is independent of everything else and runs first.
    try:
        reap_orphans()
    except Exception as exc:  # noqa: BLE001
        C_ERRORS.labels(stage="reap").inc()
        log.warning("reaping failed: %s", exc)

    # 1a. A RESIZE IN FLIGHT advances one step, and does so BEFORE inventory.
    #     It needs only its own node and its own server, and a node left drained
    #     and powered off through a discovery outage is capacity being paid for
    #     and not used.
    try:
        advance_resize()
    except Exception as exc:                                     # noqa: BLE001
        C_ERRORS.labels(stage="resize").inc()
        log.error("resize step failed: %s", exc)
        end_resize(False, str(exc))
    resizing = resize_busy()
    M_RESIZING.set(1 if resizing else 0)

    # 2. DISCOVERY. An API error must never be read as "no demand".
    found, services, ok = discover_workloads()
    if not ok:
        log.warning("holding: the service list is unreadable")
        return {}
    watched = [Watched(w) for w in found]
    by_name = {w.name: w for w in found}
    G_SERVICES.set(len(watched))
    M_MANAGED.set(len(found))
    forget_vanished({w.name for w in found})
    _GLOBAL_SERVICE_IDS.clear()
    _GLOBAL_SERVICE_IDS.update(
        s.id for s in services if "Global" in (s.attrs.get("Spec", {}).get("Mode") or {}))
    app_ids = {w.id for w in found}
    pinned = {w.name for w in found if w.pinned}
    any_pinned = bool(pinned)
    all_unpinned = not any_pinned
    M_MODE.set(1 if any_pinned else 0)
    M_MIXED.set(1 if pinned and len(pinned) != len(found) else 0)
    for w in found:
        S_PINNED.labels(w.name).set(1 if w.pinned else 0)
        S_COST_CPU.labels(w.name).set(w.cost.cores)
        S_COST_MEM.labels(w.name).set(w.cost.mem)

    # 3. PERFORMANCE, and it runs whatever the fleet is doing. This is the half
    #    that has to keep working when Hetzner does not.
    decided = judge(watched)

    # 4. INVENTORY. Failing here holds the FLEET only — the verdicts above are
    #    still delivered at the end, because a replica count that fits on the
    #    machines already running does not need Hetzner to be reachable.
    fleet_ok = True
    ready, manager_node, manager_id, tasks_by_node = [], None, "", {}
    try:
        ready = swarm_ready_workers()
        manager_node = get_manager_node()
        manager_id = manager_node.id if manager_node else manager_node_id()
        tasks_by_node = index_tasks()
    except Exception as exc:  # noqa: BLE001
        _fleet_hold("cannot read the cluster inventory; holding the fleet: %s", exc)
        fleet_ok = False

    current_workers = len(ready)
    floor = scheduled_floor() if fleet_ok else 0
    if fleet_ok:
        M_CURRENT.set(current_workers)
        M_HOSTS.set(1 + current_workers)
        M_MAX.set(MAX_WORKERS)
        M_MIN.set(floor)

    # 5. EMERGENCY, before anything that can fail. Worker mode with an empty
    #    fleet means every task is unplaceable and the site is down. Dispatching
    #    `pinned: false` is all this process can do about it — the autoscaler
    #    holds the constraint — so it is dispatched immediately and nothing else
    #    is attempted this loop.
    if fleet_ok and any_pinned and current_workers == 0:
        C_ERRORS.labels(stage="stranded").inc()
        log.error("applications are pinned to workers and no worker is left: %s. "
                  "Dispatching a release so the master takes them back.",
                  ", ".join(sorted(pinned)))
        for verdict in decided.values():
            verdict["pinned"] = False
            verdict["replica_ceiling"] = None
        dispatch(decided)
        return decided

    # 6. CAPACITY.
    node_free, worker_bins, new_free = {}, [], None
    manager_free = workloads.ZERO
    if fleet_ok:
        for node in ([manager_node] if manager_node else []) + ready:
            try:
                node_free[node.id] = node_free_for_apps(
                    node, tasks_by_node.get(node.id, []), app_ids)
            except Exception as exc:  # noqa: BLE001
                C_ERRORS.labels(stage="capacity").inc()
                log.warning("cannot measure %s; treating it as full: %s", node.id[:12], exc)
                # ZERO free, not zero used. Believing in room that does not
                # exist is how tasks end up pending.
                node_free[node.id] = workloads.ZERO
        manager_free = node_free.get(manager_id, workloads.ZERO)
        worker_bins = [Bin(n.id, node_free.get(n.id, workloads.ZERO), False) for n in ready]
        try:
            new_free = new_worker_free(services)
        except Exception as exc:  # noqa: BLE001
            log.warning("cannot size a new worker: %s", exc)
            new_free = None

        M_MGR_CPU.set(manager_free.cores)
        M_MGR_MEM.set(manager_free.mem)
        pool = workloads.ZERO
        for b in worker_bins:
            pool = pool + b.free
        M_POOL_CPU.set(pool.cores)
        M_POOL_MEM.set(pool.mem)
        M_NEW_CPU.set(new_free.cores if new_free else 0)
        M_NEW_MEM.set(new_free.mem if new_free else 0)
        note_capacity(manager_free, worker_bins, new_free, found)

    # 7. PLACEMENT GUARD.
    pressure = None
    if fleet_ok:
        try:
            pressure = read_node_pressure(current_workers)
        except Exception as exc:  # noqa: BLE001
            C_ERRORS.labels(stage="signals").inc()
            log.warning("node pressure unreadable this loop: %s", exc)

    # 8. WANT. The same pure function the autoscaler applies, from the direction
    #    this process just decided — so the fleet is sized for exactly the
    #    replicas that will be created, never for a number nobody asked for.
    live = {w.name: w.spec_replicas for w in found}
    wants = {}
    now = time.time()
    for w in found:
        verdict = decided.get(w.name, {})
        raw = workloads.bounded(w.policy, workloads.desired_replicas(
            w.policy, verdict.get("direction"), w.spec_replicas,
            held=_held_of(verdict), peak=_peak_of(verdict)))
        # Stabilised HERE as well as in the autoscaler, from the same function
        # and the same numbers, because this total is what the fleet is sized
        # from. Sizing for a shrink the autoscaler is going to damp would delete
        # a worker whose replicas are still running on it.
        wants[w.name] = _stabilizer.stabilise(
            w.name, raw, w.spec_replicas, w.policy.stabilize_down, now)
        S_WANTED.labels(w.name).set(wants[w.name])
    _stabilizer.forget({w.name for w in found})

    demand = workloads.ZERO
    for w in found:
        for _ in range(wants[w.name]):
            demand = demand + w.cost
    M_DEMAND_CPU.set(demand.cores)
    M_DEMAND_MEM.set(demand.mem)

    want_servers, want_pinned = 0, any_pinned
    if fleet_ok:
        # 9. SIZING, from the UNCAPPED want.
        want_servers = workers_needed(found, wants, pressure, manager_free,
                                      worker_bins, new_free) if found else 0
        # Exported BEFORE the clamp: a fleet pinned at MAX_WORKERS reported the
        # same number whether demand wanted one more worker or nine.
        M_WANTED_UNCAPPED.set(want_servers)
        want_servers = max(floor, min(MAX_WORKERS, want_servers))
        # One worker means the master is out of the request path entirely.
        want_pinned = want_servers >= 1
        M_DESIRED.set(want_servers)

    # 10. THE CEILING, against the CURRENTLY eligible nodes — which is where the
    #     services are pinned RIGHT NOW, not where they are heading. Mid
    #     scale-out the master is still serving, and capping against the workers
    #     alone would shed the replicas it is holding.
    #
    #     The packer is asked what would fit if every service asked for its
    #     maximum, which is exactly the definition of a ceiling: it caps growth
    #     and can never ask for a shrink.
    ceilings = {}
    if fleet_ok and found:
        bins = list(worker_bins)
        if any(not w.pinned for w in found) or not found:
            bins.append(Bin("master", manager_free, True))
        ceilings, capped, starved = admit(
            found, {w.name: w.policy.max_replicas for w in found}, bins, live)
        for w in found:
            S_CEILING.labels(w.name).set(ceilings.get(w.name, w.spec_replicas))
            S_STARVED.labels(w.name).set(1 if w.name in starved else 0)
            if w.name in starved:
                C_ERRORS.labels(stage="admission").inc()
                warn_once((w.name, "starved", ceilings.get(w.name)),
                          "%s cannot reach its minimum of %d replica(s): only %d fit "
                          "on the current nodes. The fleet is being grown; if it is "
                          "already at MAX_WORKERS this will not resolve on its own.",
                          w.name, w.policy.min_replicas, ceilings.get(w.name, 0))
            elif wants[w.name] > ceilings.get(w.name, 0):
                warn_once((w.name, "capped", ceilings.get(w.name), wants[w.name]),
                          "capping %s at %d replica(s) (wanted %d): one replica needs %s",
                          w.name, ceilings.get(w.name, 0), wants[w.name], w.cost)

    # 11. Everything that changes the FLEET.
    if fleet_ok:
        try:
            adopt_leases()
            reconcile_leases()
        except Exception as exc:  # noqa: BLE001
            C_ERRORS.labels(stage="lease").inc()
            log.error("cannot reconcile node leases: %s", exc)
        try:
            resizing = grow_or_buy(found, ready, node_free, tasks_by_node, app_ids,
                                   wants, ceilings, live, pinned, manager_free,
                                   all_unpinned, want_servers, current_workers, resizing)
            _last_scale_down = shrink(found, ready, node_free, tasks_by_node, app_ids,
                                      ceilings, live, pinned, manager_free, manager_id,
                                      all_unpinned, want_servers, current_workers,
                                      floor, demand, _last_scale_down)
        except Exception as exc:  # noqa: BLE001
            C_ERRORS.labels(stage="fleet").inc()
            log.error("fleet step failed: %s", exc)

    # 12. DISPATCH. The whole current world, to every manager that claims a
    #     cause in it — including the services that are fine, because a manager
    #     that only heard about problems could never learn that one had ended.
    for name, verdict in decided.items():
        w = by_name.get(name)
        # HANDOVER 2 lives here: `pinned: true` is only ever dispatched once
        # ready workers can hold every replica. Until then the master keeps
        # serving and the autoscaler is told nothing has changed.
        verdict["pinned"] = want_pinned and (workers_can_hold(found, worker_bins,
                                                              ceilings, live)
                                             if want_pinned else False)
        verdict["replica_ceiling"] = ceilings.get(name) if fleet_ok else None
        if w is not None and verdict["replica_ceiling"] is None:
            verdict["replica_ceiling"] = None
    dispatch(decided)

    if fleet_ok:
        log.info(
            "workers %d/%d (floor %d, ceiling %d) · %d component(s) · demand %s · "
            "master %s · worker room %s · new %s · placement %s%s",
            current_workers, want_servers, floor, MAX_WORKERS, len(found), demand,
            manager_free, " + ".join(str(b.free) for b in worker_bins) or "none",
            new_free if new_free else "unknown",
            "workers" if any_pinned else "master",
            " -> workers" if want_pinned and not any_pinned else
            " -> master" if not want_pinned and any_pinned else "")
    for w in found:
        v = decided.get(w.name) or {}
        log.info("  %-28s %d replicas (want %d, ceiling %s) · latency %s · cpu/replica %s · %s · %s",
                 w.name, w.spec_replicas, wants.get(w.name, w.spec_replicas),
                 ceilings.get(w.name, "n/a"),
                 f"{v['latency_ms']:.0f}ms" if v.get("latency_ms") is not None else "n/a",
                 f"{v['cpu_pct']:.0f}%" if v.get("cpu_pct") is not None else "n/a",
                 "workers" if w.name in pinned else "master",
                 v.get("direction") or "?")
    return decided


def workers_can_hold(found, worker_bins, ceilings, live):
    """
    Could the ready workers hold every replica, right now?

    This is HANDOVER 2's gate, and it is why `pinned: true` is not simply
    `want_servers >= 1`. Pinning before the workers can take the load moves
    replicas onto machines with no room for them, and start-first then stalls
    with the old task still on the master and the new one pending — which looks
    exactly like a healthy handover until you count.
    """
    if not found or not worker_bins:
        return False
    items = demand_items(
        found, {w.name: max(ceilings.get(w.name, 0), live.get(w.name, 0)) for w in found},
        pinned_names={w.name for w in found})
    _, unplaced = place(items, worker_bins)
    return not unplaced


def judge(watched):
    """
    The performance verdict for every application, and who owns it.

    Unchanged in substance from when this was the whole process: measure, decide
    a direction, and — only for a service that is slow while its replicas are
    idle — work out what it is waiting on.
    """
    readings = measure(watched)
    known = managers()
    claims = claimed_causes(known)
    for cause in classify.CAUSES:
        G_CLAIMS.labels(cause=cause).set(1 if cause in claims else 0)

    needs_cause, decided = [], {}
    for s in watched:
        reading = readings.get(s.name, EMPTY)
        direction, reason = classify.decide(s.thresholds, reading.held, reading.peak)
        lat, cpu, mem = reading.held
        if lat is not None:
            G_LATENCY.labels(s.name).set(lat)
        peak_lat, peak_cpu, peak_mem = reading.peak
        decided[s.name] = {"service": s.name, "direction": direction, "reason": reason,
                           "cause": classify.CAUSE_LOCAL, "target": None,
                           "latency_ms": lat, "cpu_pct": cpu, "mem_pct": mem,
                           # The scale-DOWN side of the same measurement. Only
                           # the held values used to cross the wire, so the
                           # autoscaler could see that a service was busy and
                           # never how idle it had been — which is the number a
                           # proportional shrink is computed from. Unknown keys
                           # are ignored by both ends, so an old autoscaler
                           # reading a new verdict simply keeps its old -1 step.
                           "latency_peak_ms": peak_lat, "cpu_peak_pct": peak_cpu,
                           "mem_peak_pct": peak_mem,
                           "enabled": s.enabled}
        if direction == classify.DIRECTION_HOLD and reason:
            needs_cause.append((s, cpu, mem))

    if needs_cause:
        dependencies = discovery.discover_dependencies([s.name for s, _, _ in needs_cause])
        for s, cpu, mem in needs_cause:
            cause, target = attribute(s, cpu, mem, dependencies)
            decided[s.name].update(cause=cause, target=target)

    for s in watched:
        verdict = decided[s.name]
        if verdict["direction"] == classify.DIRECTION_HOLD and not verdict["reason"]:
            quiet(s)
            continue
        handled, alert = classify.verdict(verdict["cause"], verdict["target"],
                                          s.muted, claims)
        publish(s, verdict, handled, alert)
    return decided


def dispatch(decided):
    known = managers()
    for m in known:
        payload = [v for v in decided.values() if v["cause"] in m.causes]
        if payload:
            deliver(m, payload)


_last_node_buy_note = None


def grow_or_buy(found, ready, node_free, tasks_by_node, app_ids, wants, ceilings,
                live, pinned, manager_free, all_unpinned, want_servers,
                current_workers, resizing):
    """
    Grow one worker onto a bigger plan, or buy another. Returns `resizing`.

    Vertical sits before horizontal on purpose: one bigger worker beats two
    small ones when a replica is large, and it pays the per-node overhead tax
    once instead of twice.
    """
    booting = len(provisioning_workers())
    owned = current_workers + booting
    grew = False
    # `w.rolling` for the same reason node REMOVAL checks it, and it is not
    # hypothetical: draining kills the tasks a rollout is midway through
    # creating, `max_failure_ratio: 0` reads those deaths as the update failing,
    # and Swarm reverts the whole deploy. Growing a worker is never urgent.
    if (VERTICAL and not resizing and found and want_servers > owned
            and not any(w.rolling for w in found)
            and time.time() - _last_node_resize >= NODE_RESIZE_COOLDOWN):
        live_items = demand_items(
            found, {w.name: ceilings.get(w.name, w.spec_replicas) for w in found},
            pinned_names=pinned)
        want_items = demand_items(found, wants,
                                  pinned_names={w.name for w in found})
        plan = plan_resize(ready, node_free, live_items, want_items,
                           manager_free if all_unpinned else None,
                           tasks_by_node, app_ids, found, 1)
        if plan:
            grew = begin_resize(plan[0], plan[1], plan[2], 1, found)
            resizing = resizing or grew

    if grew or resizing:
        # One fleet-changing action at a time. Buying now would order capacity
        # for a shortfall that is already being answered.
        return resizing
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
                    C_ERRORS.labels(stage="create").inc()
                    log.error("could not create a worker: %s", exc)
                    break
    elif booting:
        log.info("%d worker(s) still booting; not ordering more", booting)
    return resizing


def shrink(found, ready, node_free, tasks_by_node, app_ids, ceilings, live, pinned,
           manager_free, manager_id, all_unpinned, want_servers, current_workers,
           floor, demand, last_scale_down):
    """
    Delete a worker, or failing that shrink one. Returns the new scale-down clock.

    THE LAST WORKER IS THE DANGEROUS ONE, and both guards on it read LIVE Swarm
    state rather than anything this process decided: every application must
    already be unpinned, and a task of every service must already be RUNNING on
    the master. The autoscaler is the one that releases the pin, so waiting for
    Swarm to show it is what makes the handover safe across two processes.
    """
    removed = False
    if resize_busy():
        log.info("worker scale-down held: %s is being resized", _resize["hostname"])
        return last_scale_down
    if want_servers >= current_workers:
        return _shrink_plan(found, ready, node_free, tasks_by_node, app_ids, ceilings,
                            pinned, manager_free, all_unpinned, want_servers,
                            current_workers, removed, last_scale_down)

    since = time.time() - last_scale_down
    last_one = current_workers == 1
    items = demand_items(found, {w.name: ceilings.get(w.name, w.spec_replicas)
                                 for w in found}, pinned_names=pinned)
    candidate = pick_removal_candidate(
        ready, node_free, items, manager_free if all_unpinned else None,
        tasks_by_node, app_ids)
    on_manager = manager_running_by_service(tasks_by_node, manager_id)
    missing = [w.name for w in found
               if ceilings.get(w.name, w.spec_replicas) >= 1
               and on_manager.get(w.id, 0) < 1]
    rolling = [w.name for w in found if w.rolling]

    if rolling:
        # Draining a node kills the tasks a rollout is in the middle of
        # creating, and `max_failure_ratio: 0` reads those deaths as the update
        # failing — so Swarm rolls the whole thing back. Removal is never
        # urgent; waiting a loop costs one minute of one server.
        log.info("worker scale-down deferred: %s still rolling", ", ".join(rolling))
    elif since < COOLDOWN_DOWN:
        log.info("worker scale-down suppressed: %.0fs since last", since)
    elif current_workers <= floor:
        pass
    elif candidate is None:
        log.info("no worker can be removed without leaving replicas unplaceable "
                 "(%d worker(s), demand %s)", current_workers, demand)
    elif last_one and not all_unpinned:
        # The release has been dispatched; Swarm has not applied it yet.
        # Deleting this worker now takes the site down rather than scaling to
        # zero, so it waits — indefinitely if it comes to that. Keeping one
        # worker costs a few euros a month; removing it blind costs the site.
        log.info("holding the last worker: applications are still pinned — %s",
                 ", ".join(sorted(pinned)))
    elif last_one and missing:
        # The pins are off and the master is eligible, but Swarm has not started
        # a task there yet. The autoscaler forces a re-placement when this
        # persists; all that is needed here is not to delete the worker.
        log.info("holding the last worker: no replica on the master yet for %s",
                 ", ".join(missing))
    else:
        if last_one:
            log.info("removing the last worker: the master is already serving every "
                     "component, the fleet goes to zero")
        try:
            remove_worker(candidate)
            last_scale_down = time.time()
            removed = True
        except Exception as exc:  # noqa: BLE001
            C_ERRORS.labels(stage="remove").inc()
            log.error("could not remove a worker: %s", exc)

    return _shrink_plan(found, ready, node_free, tasks_by_node, app_ids, ceilings,
                        pinned, manager_free, all_unpinned, want_servers,
                        current_workers, removed, last_scale_down)


def _shrink_plan(found, ready, node_free, tasks_by_node, app_ids, ceilings, pinned,
                 manager_free, all_unpinned, want_servers, current_workers, removed,
                 last_scale_down):
    """
    VERTICAL DOWN, and only once removal has had its turn and declined.

    Deleting a whole server saves its whole cost; shrinking one rung saves a
    fraction of one. Trying the smaller saving first would keep servers alive
    that the fleet no longer needs, and would break the free zero-worker floor
    by leaving a shrunken worker where none is wanted.
    """
    if (VERTICAL and not resize_busy() and not removed and found
            and want_servers == current_workers
            and time.time() - _last_node_resize >= NODE_RESIZE_COOLDOWN
            and not any(w.rolling for w in found)):
        down_items = demand_items(
            found, {w.name: ceilings.get(w.name, w.spec_replicas) for w in found},
            pinned_names=pinned)
        plan = plan_resize(ready, node_free, down_items, down_items,
                           manager_free if all_unpinned else None,
                           tasks_by_node, app_ids, found, -1)
        if plan:
            begin_resize(plan[0], plan[1], plan[2], -1, found)
    return last_scale_down


def _stop(signum, _frame):
    global _running
    log.info("received signal %s, finishing", signum)
    _running = False


def main():
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    start_http_server(METRICS_PORT)
    serve_requests()
    log.info("overseer up — cluster=%s workers=%d..%d dry_run=%s",
             CLUSTER, MIN_WORKERS, MAX_WORKERS, DRY_RUN)
    log.info("the base plan is chosen from the catalogue, not configured: the "
             "smallest shared x86 type in %s with at least %d cores, %d GB and "
             "%d GB of disk. Right now that is %s.", LOCATION, BASE_MIN_CORES,
             BASE_MIN_MEMORY_GB, BASE_MIN_DISK_GB, base_type_name() or "nothing")
    log.info("what it manages is DISCOVERED: any service labelled %s=%s is an "
             "application, and its policy comes from its own autoscale.* labels.",
             workloads.WORKLOAD_LABEL, workloads.WORKLOAD_APP)
    log.info("a manager subscribes by labelling its own service %s=<cause>; "
             "causes are %s", classify.HANDLER_LABEL, "/".join(classify.CAUSES))
    if MIN_WORKERS == 0:
        log.info("worker floor is 0: an idle cluster bills no Hetzner servers at all "
                 "and the master carries the load.")
    while _running:
        started = time.time()
        try:
            loop()
            G_LOOP.set(time.time())
        except Exception as exc:  # noqa: BLE001
            C_ERRORS.labels(stage="loop").inc()
            log.exception("loop failed: %s", exc)
        time.sleep(max(1.0, LOOP_SECONDS - (time.time() - started)))
    log.info("exiting cleanly")


if __name__ == "__main__":
    main()
