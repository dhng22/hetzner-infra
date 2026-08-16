#!/usr/bin/env python3
"""
Two-tier autoscaler for Docker Swarm on Hetzner Cloud.

POLICY
------
Load is absorbed in two stages, cheapest first:

  1. REPLICAS. Adding a task to existing capacity takes seconds. The desired
     replica count is driven by p95 latency against your SLO, with
     CPU-per-replica as the secondary signal.
  2. NODES. Only when the replicas we want will not fit on the capacity we
     have. Provisioning takes ~2 minutes including JVM warmup.

Coming down, the order reverses: shed replicas first, remove the node later
and slowly.

THE MASTER IS HOST #1
---------------------
Hosts are counted including the master, so the floor of MIN_WORKERS=1 is free:
one host means the master, and no Hetzner server is billed. The app therefore
lives in one of two places, and exactly one of them at a time:

  MANAGER MODE   one host. The app services carry no role constraint and run on
                 the master alongside monitoring, Redis and the panel. Nothing
                 is billed.
  WORKER MODE    two or more hosts. Every host is a Hetzner worker, the app
                 services carry `node.role == worker`, and the master goes back
                 to being a pure control plane carrying no replicas at all.

CAPACITY IS MEASURED, NOT CONFIGURED
------------------------------------
There is no REPLICAS_PER_WORKER and no manager-capacity constant. How many
replicas a node holds is computed from that node's advertised CPU and RAM minus
what Swarm has already reserved on it, divided by one replica's own reservation
read from the live service spec. A new worker's capacity comes from the Hetzner
server-type catalogue minus the per-node reservations of the `mode: global`
services. Resize the master, change a limit, switch WORKER_TYPE — the arithmetic
follows without a config edit, and there is no constant that can drift.

Existing nodes are filled before new ones are ordered: hosts_needed() sums what
the current workers can still take and turns only the shortfall into servers. A
half-empty 4 vCPU worker is used up before a second one is bought.

The autoscaler owns that constraint at runtime, the same way it owns the
replica count — stacks/app.yml sets neither. `_placement_mode()` reads the live
spec every loop and `_set_placement_mode()` moves it with `docker service
update --constraint-add/--constraint-rm`, which edits the constraint list and
nothing else while honouring the service's own update_config (start-first,
parallelism 1, monitor 90s, rollback on failure).

THE HANDOVER MUST NEVER LEAVE A GAP. Both directions are ordered so that a
healthy replica is serving at every instant:

  scaling out (manager -> workers)
    1. create workers, and hold the replica count at what the MANAGER can
       serve while they boot. The master keeps serving throughout.
    2. wait until the workers are `ready` AND their measured capacity covers
       every replica. Flipping early would strand tasks on nodes that cannot
       take them.
    3. only then add the constraint. start-first replaces the master's tasks
       one at a time, new-before-old, onto the workers.

  scaling in (workers -> manager)
    1. remove the constraint FIRST, while the last worker is still up and
       serving. The master becomes eligible and picks up tasks.
    2. wait until a replica is actually RUNNING on the master.
    3. only then drain and delete the last worker.

Reversing either order is what produces the outage: flip-then-provision leaves
tasks pending, and delete-then-flip leaves nothing serving. Every step is
re-derived from live state each loop, so a crash mid-handover is resumed rather
than left half-applied. If every worker disappears at once while in worker
mode, the next loop flips straight back to manager mode — the failure path and
the scale-down path are the same code.

cloudflared is the one exception: it stays `mode: global` with no constraint,
so the master always has a registered connector. That is deliberate. It is
ingress plumbing, not application load, and having it already live on both
sides of a handover is what stops the tunnel going dark while replicas move.

WHY THESE SIGNALS
-----------------
p95 latency is what your users experience; node CPU is not. Node CPU averages
in the log driver, the exporters, cloudflared and staging, none of which
should influence production capacity. CPU-per-replica (from cadvisor, scoped
to the production service) is the resource-bound backstop, and raw node CPU is
used only to decide whether another replica can physically fit.

WHY THE COOLDOWNS ARE ASYMMETRIC
--------------------------------
Scaling up late costs user-visible latency. Scaling down late costs pennies.
COOLDOWN_UP must exceed provision time plus JVM warmup, or the loop will
provision again while the last node is still warming and badly overshoot. That
is the most common way an autoscaler burns money.

STATELESS BY DESIGN. Cooldowns derive from Hetzner creation timestamps and
sustain windows from VictoriaMetrics subqueries, so restarting this container
loses nothing. HORIZONTAL ONLY — no rescale calls; Hetzner rescale power-cycles
the server and a grown disk can never shrink.
"""

import logging
import os
import re
import signal
import subprocess
import sys
import time
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
# config
# ---------------------------------------------------------------------------


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
APP_ENV = _env("APP_ENV", "prod")  # which app the latency signal reads from
VM_URL = _env("VM_URL", "http://victoriametrics:8428")

# --- the service we scale -------------------------------------------------
APP_SERVICE = _env("APP_SERVICE", "app_api-prod")
# Staging rides the same placement mode so that "the master runs no app" is
# true of the whole app stack, not just production. It is never scaled — one
# capped replica — so it follows, it does not drive. Blank disables the follow.
APP_SERVICE_STAGING = _env("APP_SERVICE_STAGING", "app_api-staging")
APP_CPU_LIMIT = _env("APP_CPU_LIMIT", "1.0", float)   # must match the stack file
MIN_REPLICAS = _env("MIN_REPLICAS", "2", int)
MAX_REPLICAS = _env("MAX_REPLICAS", "12", int)

# --- hosts ----------------------------------------------------------------
# HOSTS, not Hetzner servers. The master counts as host #1:
#
#   MIN_WORKERS = 1  ->  the master alone runs the app, zero servers billed
#   hosts     >= 2   ->  that many Hetzner workers exist and the master runs none
#
# so the floor of 1 is free. Set MIN_WORKERS = 2 if you would rather the master
# never ran application traffic; the master is then never a host at all.
#
# There is no REPLICAS_PER_WORKER and no manager capacity constant. How many
# replicas a node holds is measured from that node's real CPU and RAM minus what
# Swarm has already reserved on it — see node_app_capacity(). A constant would
# only ever be a guess about hardware the autoscaler can read directly.
MIN_WORKERS = _env("MIN_WORKERS", "1", int)
MAX_WORKERS = _env("MAX_WORKERS", "6", int)

# --- signals: latency first, CPU-per-replica second -----------------------
SLO_P95_MS = _env("SLO_P95_MS", "500", float)      # your target. THE number.
SCALE_UP_P95_RATIO = _env("SCALE_UP_P95_RATIO", "0.8", float)   # act at 80% of SLO
SCALE_DOWN_P95_RATIO = _env("SCALE_DOWN_P95_RATIO", "0.4", float)
SCALE_UP_CPU = _env("SCALE_UP_CPU", "70", float)   # % of a replica's CPU limit
SCALE_DOWN_CPU = _env("SCALE_DOWN_CPU", "30", float)
# Placement guard, not a trigger. If a worker is this loaded on either CPU or
# memory, another replica will not fit on it, so a node is required.
NODE_PRESSURE_PCT = _env("NODE_PRESSURE_PCT", "80", float)

# Up fast, down slow. Never make these symmetric.
SUSTAIN_UP = _env("SUSTAIN_UP_SECONDS", "90", int)
SUSTAIN_DOWN = _env("SUSTAIN_DOWN_SECONDS", "900", int)
# COOLDOWN_UP >= node boot + image pull + JVM warmup, or you will overshoot.
COOLDOWN_UP = _env("COOLDOWN_UP_SECONDS", "300", int)
COOLDOWN_DOWN = _env("COOLDOWN_DOWN_SECONDS", "900", int)
REPLICA_COOLDOWN = _env("REPLICA_COOLDOWN_SECONDS", "60", int)
# Step up proportionally: +1 at a time cannot keep pace with a real spike.
SCALE_UP_FACTOR = _env("SCALE_UP_FACTOR", "0.5", float)

SCHEDULE_FLOOR = _env("SCHEDULE_FLOOR", "")
LOOP_SECONDS = _env("LOOP_SECONDS", "60", int)
DRY_RUN = _env("DRY_RUN", "false", bool)  # a real safety switch, not a rehearsal
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("autoscaler")

# The master is host #1, so a floor below 1 is meaningless and a floor of 0
# would ask for a cluster with nowhere to run.
if MIN_WORKERS < 1:
    log.warning("MIN_WORKERS=%d is below the floor of 1 (the master is host #1); using 1",
                MIN_WORKERS)
    MIN_WORKERS = 1
if MAX_WORKERS < MIN_WORKERS:
    log.warning("MAX_WORKERS=%d is below MIN_WORKERS=%d; using %d",
                MAX_WORKERS, MIN_WORKERS, MIN_WORKERS)
    MAX_WORKERS = MIN_WORKERS

# ---------------------------------------------------------------------------
# metrics about ourselves
# ---------------------------------------------------------------------------

# TWO DIFFERENT NUMBERS, and alerts depend on the difference.
#   ..._current_workers  Hetzner servers only. Goes to 0 at the free floor, which
#                        is what AppStrandedWithoutWorkers keys on.
#   ..._current_hosts    those plus the master as host #1, so never below 1. This
#                        is what the MAX_WORKERS ceiling is expressed in.
# Collapsing them made the stranded alert unfireable, because hosts is never < 1.
M_CURRENT = Gauge("autoscaler_current_workers", "Hetzner worker servers in the swarm")
M_HOSTS = Gauge("autoscaler_current_hosts", "Hosts running the app, master included")
M_DESIRED = Gauge("autoscaler_desired_workers", "Worker count the autoscaler wants")
M_MAX = Gauge("autoscaler_max_workers", "Configured ceiling")
M_MIN = Gauge("autoscaler_effective_min_workers", "Floor in force right now")
M_CPU = Gauge("autoscaler_cluster_cpu_percent", "Mean worker CPU utilisation")
M_MEM = Gauge("autoscaler_cluster_mem_percent", "Mean worker memory utilisation")
M_P95 = Gauge("autoscaler_app_p95_ms", "Production p95 latency in milliseconds")
M_REPLICAS = Gauge("autoscaler_current_replicas", "Replicas of the scaled service")
M_REPLICAS_WANT = Gauge("autoscaler_desired_replicas", "Replicas the autoscaler wants")
# Exported so alerts compare against the running config instead of a literal.
# ReplicaCeiling used to hardcode 12; raising MAX_REPLICAS left it firing at the
# old number with nothing to indicate the threshold had gone stale.
M_REPLICAS_MAX = Gauge("autoscaler_max_replicas", "Configured replica ceiling")
M_MGR_CAPACITY = Gauge("autoscaler_manager_replica_capacity",
                       "Replicas the master can hold, measured from its free resources")
M_WORKER_CAPACITY = Gauge("autoscaler_worker_replica_capacity",
                          "Replicas one new worker of WORKER_TYPE would hold")
# 0 = manager mode (app runs on the master, zero workers)
# 1 = worker mode  (app pinned to workers, master carries none)
M_MODE = Gauge("autoscaler_placement_worker_mode",
               "1 when the app is pinned to worker nodes, 0 when it runs on the manager")
M_CPU_REPLICA = Gauge("autoscaler_cpu_per_replica_percent", "Mean CPU per replica, % of limit")
M_SLO = Gauge("autoscaler_slo_p95_ms", "Configured p95 SLO")
M_LOOP = Gauge("autoscaler_last_loop_timestamp_seconds", "Unix time of last completed loop")
M_EVENTS = Counter("autoscaler_scale_events_total", "Scaling actions taken", ["direction"])
M_ERRORS = Counter("autoscaler_errors_total", "Errors encountered", ["stage"])

hcloud = Client(token=HCLOUD_TOKEN)
dkr = docker.DockerClient(base_url="unix:///var/run/docker.sock")

_running = True
_last_scale_down = time.time()  # conservative: wait one cooldown after a restart
_last_replica_change = 0.0
# When we first started waiting for the master to pick up a replica so the last
# worker can go. Only used to escalate a wait that has become a stall.
_manager_wait_since = None
HANDOVER_STALL_SECONDS = 900
# Remembered so the measured capacity is explained when it CHANGES, not every
# 60 seconds. It is the number every scaling decision rests on, so it belongs in
# the log — but only once per shape of cluster.
_capacity_note = None


def note_capacity(manager_capacity, worker_caps, new_worker_cap, cost):
    global _capacity_note
    shape = (manager_capacity, tuple(worker_caps), new_worker_cap)
    if shape == _capacity_note:
        return
    _capacity_note = shape
    cpu, mem = cost
    log.info(
        "measured capacity: one replica reserves %.2f CPU / %dMB · master holds "
        "%d · existing workers hold %s · a new %s would hold %s",
        cpu, mem // (1024 * 1024) if mem else 0, manager_capacity,
        "+".join(str(c) for c in worker_caps) or "none",
        WORKER_TYPE,
        new_worker_cap if new_worker_cap is not None else "unknown (assuming 1)",
    )
    if manager_capacity < MIN_REPLICAS and MIN_WORKERS == 1:
        log.warning(
            "the master can only hold %d replica(s) but MIN_REPLICAS=%d, so the "
            "cluster can never return to the free host-1 state. Give the master "
            "more CPU/RAM, lower MIN_REPLICAS, or shrink the app's reservations.",
            manager_capacity, MIN_REPLICAS,
        )


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
        resp = requests.get(
            f"{VM_URL}/api/v1/query", params={"query": expr}, timeout=15
        )
        resp.raise_for_status()
        payload = resp.json()
        results = payload.get("data", {}).get("result", [])
        if not results:
            return None
        return float(results[0]["value"][1])
    except Exception as exc:  # noqa: BLE001
        M_ERRORS.labels(stage="query").inc()
        log.warning("query failed (%s): %s", expr[:60], exc)
        return None


_SEL = 'node_role="worker"'
_SVC = f'container_label_com_docker_swarm_service_name="{APP_SERVICE}"'

# PRIMARY: what users actually feel. Production only — staging latency is
# irrelevant to production capacity.
P95_EXPR = (
    "histogram_quantile(0.95, sum(rate(http_server_requests_seconds_bucket"
    f'{{env="{APP_ENV}"}}[2m])) by (le)) * 1000'
)

# SECONDARY: mean CPU of one production replica as a percentage of the limit
# it was given. Scoped to the service, so exporters, cloudflared and staging
# cannot contaminate it.
CPU_REPLICA_EXPR = (
    f'avg(rate(container_cpu_usage_seconds_total{{{_SVC}}}[3m]))'
    f' / {APP_CPU_LIMIT} * 100'
)

# PLACEMENT GUARD ONLY: is there physical room for another replica? Never used
# as a scaling trigger — it averages in everything running on the box.
CPU_EXPR = (
    f'100 - (avg(rate(node_cpu_seconds_total{{mode="idle",{_SEL}}}[5m])) * 100)'
)
MEM_EXPR = (
    f'avg(100 * (1 - node_memory_MemAvailable_bytes{{{_SEL}}}'
    f' / node_memory_MemTotal_bytes{{{_SEL}}}))'
)

# The same guard for the manager, used ONLY when the worker fleet is empty.
# With zero workers the worker-scoped queries above return no series at all, so
# without this there is nothing to notice that the box carrying the replicas is
# full. It stays a guard, never a trigger: the manager's baseline includes
# VictoriaMetrics, Loki, Grafana, Redis and the panel, so its absolute number
# is meaningless as a measure of app load — only its headroom matters.
_MGR = 'node_role="manager"'
MGR_CPU_EXPR = (
    f'100 - (avg(rate(node_cpu_seconds_total{{mode="idle",{_MGR}}}[5m])) * 100)'
)
MGR_MEM_EXPR = (
    f'avg(100 * (1 - node_memory_MemAvailable_bytes{{{_MGR}}}'
    f' / node_memory_MemTotal_bytes{{{_MGR}}}))'
)


def sustained(expr, window, aggregate):
    """
    Was `expr` continuously above (min_over_time) or below (max_over_time)
    for the whole window? Uses a subquery so no local state is needed.
    """
    step = max(15, window // 12)
    return vm_query(f"{aggregate}(({expr})[{window}s:{step}s])")


# ---------------------------------------------------------------------------
# swarm + hetzner inventory
# ---------------------------------------------------------------------------

def swarm_workers():
    return [
        n for n in dkr.nodes.list()
        if n.attrs.get("Spec", {}).get("Role") == "worker"
    ]


def swarm_ready_workers():
    return [
        n for n in swarm_workers()
        if n.attrs.get("Status", {}).get("State") == "ready"
        and n.attrs.get("Spec", {}).get("Availability") == "active"
    ]


def hetzner_workers():
    return hcloud.servers.get_all(
        label_selector=f"cluster=={CLUSTER},role==swarm-worker"
    )


def provisioning_workers():
    """
    Servers that exist and are being paid for but have not joined the swarm as
    ready yet — roughly two minutes of boot, cloud-init, docker install and
    image pull.

    These MUST count towards the fleet size. Sizing decisions off
    swarm_ready_workers() alone means a booting worker is invisible, so every
    loop during that window sees the same shortfall and orders more capacity
    for it. That overshoots the ceiling outright: the ceiling arithmetic is
    also written against the ready count, so `MAX_WORKERS - current` stays
    generous while servers pile up. COOLDOWN_UP used to paper over this by
    accident, and stopped doing so once an empty fleet became the normal
    resting state — there is no longer a first-worker case that skips the
    cooldown, so every scale-out starts from zero servers.
    """
    ready = {n.attrs["Description"]["Hostname"] for n in swarm_ready_workers()}
    return [s for s in hetzner_workers() if s.name not in ready]


def get_service():
    try:
        return dkr.services.get(APP_SERVICE)
    except Exception as exc:  # noqa: BLE001
        M_ERRORS.labels(stage="service").inc()
        log.error("cannot find service %s: %s", APP_SERVICE, exc)
        return None


def current_replicas(service):
    return (
        service.attrs["Spec"]["Mode"].get("Replicated", {}).get("Replicas", 0)
    )


# ---------------------------------------------------------------------------
# placement mode: who is allowed to run the app right now
# ---------------------------------------------------------------------------

WORKER_CONSTRAINT = "node.role==worker"
# Swarm normalises whitespace differently across versions and the stack file
# once wrote it spaced out, so recognise any spelling rather than only ours.
_WORKER_PIN = re.compile(r"^\s*node\.role\s*==\s*worker\s*$")

MODE_MANAGER = "manager"
MODE_WORKER = "worker"


def managed_services():
    """The app services whose placement this autoscaler owns, prod first."""
    names = [APP_SERVICE]
    if APP_SERVICE_STAGING and APP_SERVICE_STAGING != APP_SERVICE:
        names.append(APP_SERVICE_STAGING)
    return names


def _constraints(service):
    try:
        return (
            service.attrs["Spec"]["TaskTemplate"].get("Placement", {})
            .get("Constraints") or []
        )
    except (KeyError, TypeError):
        return []


def placement_mode(service):
    """MODE_WORKER if the live spec pins tasks to workers, else MODE_MANAGER."""
    if service is None:
        return None
    return MODE_WORKER if any(
        _WORKER_PIN.match(c) for c in _constraints(service)
    ) else MODE_MANAGER


def update_in_progress(service):
    """
    Is Swarm still rolling this service? Issuing a second constraint change on
    top of an in-flight one restarts the rollout from the beginning, which with
    monitor: 90s per task means it may never finish.
    """
    state = (service.attrs.get("UpdateStatus") or {}).get("State")
    return state in ("updating", "rollback_started")


def _service_update_constraint(name, add):
    """`docker service update --constraint-add/rm` on one service, detached.

    Detached on purpose. Blocking would hold the loop for parallelism x
    monitor (90s) x replicas — long enough for AutoscalerStalled to fire — and
    there is nothing to wait for synchronously: the next loop reads the live
    state and carries on from wherever the rollout got to.
    """
    flag = "--constraint-add" if add else "--constraint-rm"
    cmd = ["docker", "service", "update", "--detach=true",
           flag, WORKER_CONSTRAINT, name]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{' '.join(cmd)} failed ({proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()[:400]}"
        )


def set_placement_mode(mode, reason):
    """
    Move every managed app service into `mode`. Idempotent: services already in
    the target mode are skipped, so a crash mid-handover is simply resumed.
    """
    add = mode is MODE_WORKER
    for name in managed_services():
        try:
            service = dkr.services.get(name)
        except Exception as exc:  # noqa: BLE001
            # Staging may legitimately not exist; production not existing is
            # already reported by get_service().
            log.warning("cannot read %s for placement change: %s", name, exc)
            continue
        if placement_mode(service) == mode:
            continue
        if DRY_RUN:
            log.info("[dry-run] would move %s to %s mode (%s)", name, mode, reason)
            continue
        _service_update_constraint(name, add=add)
        M_EVENTS.labels(direction=f"placement-{mode}").inc()
        log.info("moving %s to %s mode: %s", name, mode, reason)


def manager_node_id():
    return dkr.info().get("Swarm", {}).get("NodeID") or ""


def get_manager_node():
    """The swarm node object for this master, or None."""
    try:
        for node in dkr.nodes.list():
            if node.attrs.get("Spec", {}).get("Role") == "manager":
                return node
    except Exception as exc:  # noqa: BLE001
        log.warning("cannot list nodes: %s", exc)
    return None


# ---------------------------------------------------------------------------
# CAPACITY — measured, never configured
# ---------------------------------------------------------------------------
# Everything below answers one question: how many more app replicas will
# actually fit? It is derived from what Swarm already knows, so there is no
# REPLICAS_PER_WORKER to keep in sync with WORKER_TYPE and no headroom constant
# to keep in sync with the monitoring stack. Resize the master, add a service,
# change a reservation, switch worker type — the arithmetic follows on its own.
#
# The unit is one app replica's RESERVATION, not its limit. Reservations are
# what Swarm's scheduler actually subtracts when placing a task, so they are the
# only number that predicts whether placement succeeds. Limits are a ceiling on
# what a running task may burst to and would over-count every node.


def _reservations(spec_resources):
    """(cpu_cores, mem_bytes) reserved by a TaskTemplate's Resources block."""
    res = (spec_resources or {}).get("Reservations") or {}
    return (res.get("NanoCPUs", 0) / 1e9, res.get("MemoryBytes", 0))


def app_replica_cost(service):
    """
    What one replica of the scaled service reserves, read from the live spec.

    Deliberately not configured. stacks/app.yml already states it, and a second
    copy in infra.env would be one more pair of numbers that silently drift
    apart — the exact failure APP_CPU_LIMIT has to be warned about.
    """
    cpu, mem = _reservations(
        service.attrs.get("Spec", {}).get("TaskTemplate", {}).get("Resources"))
    if cpu <= 0 and mem <= 0:
        # No reservations on the service: Swarm will pack it anywhere, so the
        # honest answer is that we cannot predict placement. Fall back to the
        # CPU limit, which at least bounds it.
        return max(APP_CPU_LIMIT, 0.01), 0
    return max(cpu, 0.01), mem


def node_resources(node):
    """(cpu_cores, mem_bytes) this node advertises to the swarm."""
    res = node.attrs.get("Description", {}).get("Resources", {}) or {}
    return (res.get("NanoCPUs", 0) / 1e9, res.get("MemoryBytes", 0))


def node_reserved(node_id, exclude_service=None):
    """
    (cpu, mem) already reserved on a node by running tasks.

    `exclude_service` leaves the scaled service out, which is what turns this
    into "how much room is there for the app in total" rather than "how much is
    left over right now".
    """
    cpu = mem = 0.0
    try:
        tasks = dkr.api.tasks(filters={"node": node_id, "desired-state": "running"})
    except Exception as exc:  # noqa: BLE001
        log.warning("cannot list tasks on node %s: %s", node_id[:12], exc)
        return 0.0, 0.0
    for task in tasks:
        if exclude_service and task.get("ServiceID") == exclude_service:
            continue
        c, m = _reservations(task.get("Spec", {}).get("Resources"))
        cpu += c
        mem += m
    return cpu, mem


def node_app_capacity(node, cost, app_service_id=None):
    """
    How many app replicas this node could hold in total.

    Total resources minus everything that is NOT the app, divided by one
    replica's reservation. On the master that subtraction is the whole
    monitoring stack, both Redis instances and the panel, which is exactly why
    no MANAGER_HEADROOM_CPU constant is needed: the reservations in
    stacks/monitoring.yml already say it, and they are the same numbers Swarm
    schedules against.
    """
    total_cpu, total_mem = node_resources(node)
    if total_cpu <= 0:
        return 0
    used_cpu, used_mem = node_reserved(node.id, exclude_service=app_service_id)
    cost_cpu, cost_mem = cost
    by_cpu = int(max(0.0, total_cpu - used_cpu) // cost_cpu)
    if cost_mem > 0 and total_mem > 0:
        return min(by_cpu, int(max(0, total_mem - used_mem) // cost_mem))
    return by_cpu


def worker_type_capacity(cost):
    """
    How many app replicas one NEW worker of WORKER_TYPE would hold.

    Read from the Hetzner catalogue rather than declared, then reduced by what
    every node has to run regardless (node-exporter, cadvisor, cloudflared).
    Returns None when the type cannot be looked up, and callers treat that as
    "assume one replica" so a lookup failure under-provisions rather than
    ordering a fleet.
    """
    try:
        st = hcloud.server_types.get_by_name(WORKER_TYPE)
    except Exception as exc:  # noqa: BLE001
        log.warning("cannot look up server type %s: %s", WORKER_TYPE, exc)
        return None
    if st is None:
        log.warning("unknown WORKER_TYPE %s", WORKER_TYPE)
        return None
    overhead_cpu, overhead_mem = global_service_reservations()
    cost_cpu, cost_mem = cost
    by_cpu = int(max(0.0, st.cores - overhead_cpu) // cost_cpu)
    if cost_mem > 0:
        total_mem = st.memory * 1024 ** 3          # hcloud reports GB
        by_mem = int(max(0, total_mem - overhead_mem) // cost_mem)
        return max(0, min(by_cpu, by_mem))
    return max(0, by_cpu)


def global_service_reservations():
    """
    (cpu, mem) that lands on EVERY node just for being in the cluster.

    `mode: global` services get one task per node, so their reservations are a
    per-node tax that has to come off a new worker's advertised size before it
    is treated as app capacity.
    """
    cpu = mem = 0.0
    try:
        for svc in dkr.services.list():
            spec = svc.attrs.get("Spec", {})
            if "Global" not in (spec.get("Mode") or {}):
                continue
            c, m = _reservations(spec.get("TaskTemplate", {}).get("Resources"))
            cpu += c
            mem += m
    except Exception as exc:  # noqa: BLE001
        log.warning("cannot read global service reservations: %s", exc)
    return cpu, mem


def running_replicas_on_manager():
    """
    Replicas of the scaled service actually RUNNING on the master — not merely
    assigned to it. This is the gate that makes scale-in safe: the last worker
    is not deleted until the master is already serving traffic.
    """
    node_id = manager_node_id()
    if not node_id:
        return 0
    try:
        tasks = dkr.api.tasks(filters={
            "service": APP_SERVICE, "node": node_id, "desired-state": "running",
        })
    except Exception as exc:  # noqa: BLE001
        log.warning("cannot list manager tasks: %s", exc)
        return 0
    return sum(1 for t in tasks if t.get("Status", {}).get("State") == "running")


def set_replicas(service, count):
    """Scale the service. Swarm applies the stack's rolling-update policy."""
    if DRY_RUN:
        log.info("[dry-run] would set %s to %d replicas", APP_SERVICE, count)
        return
    service.scale(count)
    M_EVENTS.labels(direction=f"replicas-{'up' if count else 'down'}").inc()
    log.info("scaled %s to %d replicas", APP_SERVICE, count)


def worker_join_token():
    return dkr.swarm.attrs["JoinTokens"]["Worker"]


def manager_private_ip():
    info = dkr.info()
    addr = info.get("Swarm", {}).get("NodeAddr")
    if not addr:
        raise RuntimeError("cannot determine manager advertise address")
    return addr


# ---------------------------------------------------------------------------
# scheduling floor
# ---------------------------------------------------------------------------

def scheduled_floor():
    """Parse SCHEDULE_FLOOR like '08:00-20:00=2,20:00-23:00=1' (UTC)."""
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
            minutes_now >= start or minutes_now < end
        )
        if active:
            floor = max(floor, count)
    return floor


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------

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


def pick_removal_candidate(ready, worker_caps, need_replicas, spare=0):
    """
    Which worker to drop, or None if dropping any would leave too little room.

    Newest-first (LIFO) is still the preference: the newest node is least likely
    to hold warm state and Hetzner bills by the hour. But with a mixed fleet the
    newest node may also be the biggest, and removing it could leave the rest
    unable to hold the replicas — so each candidate is checked against what the
    OTHER nodes can take, newest first, and the first one that still leaves
    enough room wins.

    `spare` is capacity outside the worker fleet, which in practice means the
    master once the worker pin has been released. Without it the LAST worker can
    never be removed: the remaining workers total zero, that is never >= the
    replica count, and the cluster sticks one server short of the free floor
    forever.
    """
    caps = dict(zip((n.id for n in ready), worker_caps))
    total = sum(worker_caps) + spare

    def created(node):
        return node.attrs.get("CreatedAt", "")

    for node in sorted(ready, key=created, reverse=True):
        if total - caps.get(node.id, 0) >= need_replicas:
            return node
    return None


def tasks_on_node(node_id):
    return [
        t for t in dkr.api.tasks(filters={"node": node_id, "desired-state": "running"})
    ]


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

    # cloudflared runs global on every worker and needs ~30s to drain its
    # edge connections on SIGTERM. Cutting this short drops live requests.
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
    swarm_hostnames = {
        n.attrs["Description"]["Hostname"] for n in swarm_workers()
    }
    for server in hetzner_workers():
        if server.name in swarm_hostnames:
            continue
        age = (datetime.now(timezone.utc) - server.created).total_seconds()
        if age < ORPHAN_GRACE_SECONDS:
            continue
        log.warning("reaping orphan server %s (never joined, age %ds)", server.name, age)
        if not DRY_RUN:
            server.delete()

    hetzner_names = {s.name for s in hetzner_workers()}
    for node in swarm_workers():
        hostname = node.attrs["Description"]["Hostname"]
        state = node.attrs.get("Status", {}).get("State")
        if state == "down" and hostname not in hetzner_names:
            log.warning("removing dead swarm node %s", hostname)
            if not DRY_RUN:
                try:
                    dkr.api.remove_node(node.id, force=True)
                except Exception as exc:  # noqa: BLE001
                    log.warning("could not remove %s: %s", hostname, exc)


# ---------------------------------------------------------------------------
# decision
# ---------------------------------------------------------------------------

def read_signals(worker_count):
    """Returns (p95_ms, cpu_per_replica_pct, node_pressure_pct). Any may be None."""
    p95 = vm_query(P95_EXPR)
    cpu_rep = vm_query(CPU_REPLICA_EXPR)

    # With an empty fleet the worker-scoped guards have no series to return, so
    # measure the box that is actually holding the replicas instead.
    if worker_count:
        node_mem = vm_query(MEM_EXPR)
        node_cpu = vm_query(CPU_EXPR)
    else:
        node_mem = vm_query(MGR_MEM_EXPR)
        node_cpu = vm_query(MGR_CPU_EXPR)

    if p95 is not None:
        M_P95.set(p95)
    if cpu_rep is not None:
        M_CPU_REPLICA.set(cpu_rep)
    if node_mem is not None:
        M_MEM.set(node_mem)
    if node_cpu is not None:
        M_CPU.set(node_cpu)
    M_SLO.set(SLO_P95_MS)

    # Whichever resource runs out first is the one that blocks placement.
    pressures = [v for v in (node_cpu, node_mem) if v is not None]
    node_pressure = max(pressures) if pressures else None
    return p95, cpu_rep, node_pressure


def desired_replicas(current, p95, cpu_rep):
    """
    Latency first, CPU-per-replica second. Both must be sustained — a single
    scrape above threshold is noise, not a trend.
    """
    up_p95 = SLO_P95_MS * SCALE_UP_P95_RATIO
    down_p95 = SLO_P95_MS * SCALE_DOWN_P95_RATIO

    reasons = []
    if p95 is not None:
        held = sustained(P95_EXPR, SUSTAIN_UP, "min_over_time")
        if held is not None and held > up_p95:
            reasons.append(f"p95 held above {up_p95:.0f}ms ({held:.0f}ms)")
    if cpu_rep is not None:
        held = sustained(CPU_REPLICA_EXPR, SUSTAIN_UP, "min_over_time")
        if held is not None and held > SCALE_UP_CPU:
            reasons.append(f"cpu/replica held above {SCALE_UP_CPU:.0f}% ({held:.0f}%)")

    if reasons:
        step = max(1, int(current * SCALE_UP_FACTOR))
        want = min(MAX_REPLICAS, current + step)
        if want != current:
            log.info("replicas %d -> %d: %s", current, want, "; ".join(reasons))
        return want

    # Scale down only when BOTH signals have stayed low for the full window.
    p95_peak = sustained(P95_EXPR, SUSTAIN_DOWN, "max_over_time")
    cpu_peak = sustained(CPU_REPLICA_EXPR, SUSTAIN_DOWN, "max_over_time")
    quiet_latency = p95_peak is None or p95_peak < down_p95
    quiet_cpu = cpu_peak is not None and cpu_peak < SCALE_DOWN_CPU
    if quiet_latency and quiet_cpu and current > MIN_REPLICAS:
        log.info(
            "replicas %d -> %d: quiet for %ds (p95 peak %s, cpu/replica peak %.0f%%)",
            current, current - 1, SUSTAIN_DOWN,
            f"{p95_peak:.0f}ms" if p95_peak is not None else "no traffic",
            cpu_peak,
        )
        return current - 1

    return current


def hosts_needed(replicas, node_pressure, manager_capacity, worker_caps, new_worker_cap):
    """
    How many HOSTS this many replicas require. The master is host #1 and
    Hetzner workers stack on top, so `servers = hosts - 1`.

    Returns 1 when the master alone can hold them — the free floor, nothing
    billed. Otherwise the master stops being a placement target entirely, the
    workers must cover EVERY replica, and the answer is 1 + however many workers
    that takes. A single-worker fleet is reachable: 5 replicas that fit on one
    worker buy one worker, not two.

    EXISTING WORKERS ARE FILLED BEFORE NEW ONES ARE ORDERED. `worker_caps` is
    the per-node capacity of the workers that already exist, measured from their
    real CPU and RAM. They are summed first, and only the shortfall is turned
    into new servers. That is what stops the loop buying a 4 vCPU worker while 2
    vCPU sits idle on the ones already running, and it is why heterogeneous
    fleets work: a node is worth what it can actually hold, not an average.

    Resource pressure forces one more host than the arithmetic asks for. Swarm
    refuses to place a task that does not fit, and a queued task serves no
    traffic.
    """
    pressured = node_pressure is not None and node_pressure > NODE_PRESSURE_PCT

    if replicas <= manager_capacity and not pressured:
        return 1                                   # master only, nothing billed

    # Past the master's capacity: workers hold everything. Work out the FEWEST
    # existing workers that cover the replicas, largest first, and only then
    # buy for whatever is still short.
    #
    # Counting `len(worker_caps)` instead — "we have enough, keep what we have"
    # — reads as harmless and quietly never scales down. A fleet that grew to
    # three workers under load would hold all three until traffic fell below
    # what the MASTER alone can take, then drop straight to zero, having paid
    # for two idle servers all the way down.
    servers, covered = 0, 0
    for cap in sorted(worker_caps, reverse=True):
        if covered >= replicas:
            break
        covered += cap
        servers += 1
    if covered < replicas:
        per_new = new_worker_cap if new_worker_cap and new_worker_cap > 0 else 1
        servers += -(-(replicas - covered) // per_new)   # ceil
    if pressured:
        servers += 1
        log.info("node resource pressure at %.0f%%: requesting an extra host",
                 node_pressure)
    # We are past what the master can hold, so at least one real worker exists.
    return 1 + max(1, servers)


def newest_worker_age():
    servers = hetzner_workers()
    if not servers:
        return None
    newest = max(servers, key=lambda s: s.created)
    return (datetime.now(timezone.utc) - newest.created).total_seconds()


def loop():
    global _last_scale_down, _last_replica_change, _manager_wait_since

    reap_orphans()

    service = get_service()
    ready = swarm_ready_workers()
    current_workers = len(ready)
    # HOSTS = the master (always #1) plus however many workers exist. So a
    # floor of 1 means "master only, nothing billed", and MAX_WORKERS=6 means
    # the master plus up to 5 Hetzner workers.
    current_hosts = 1 + current_workers
    floor = scheduled_floor()

    M_CURRENT.set(current_workers)
    M_HOSTS.set(current_hosts)
    M_MAX.set(MAX_WORKERS)
    M_MIN.set(floor)
    M_REPLICAS_MAX.set(MAX_REPLICAS)

    p95, cpu_rep, node_pressure = read_signals(current_workers)

    if service is None:
        log.warning("service unavailable; holding at %d host(s)", current_hosts)
        return

    replicas = current_replicas(service)
    M_REPLICAS.set(replicas)

    # --- measured capacity, not configured --------------------------------
    cost = app_replica_cost(service)
    manager_node = get_manager_node()
    manager_capacity = (node_app_capacity(manager_node, cost, service.id)
                        if manager_node else 0)
    worker_caps = [node_app_capacity(n, cost, service.id) for n in ready]
    new_worker_cap = worker_type_capacity(cost)
    M_MGR_CAPACITY.set(manager_capacity)
    M_WORKER_CAPACITY.set(new_worker_cap or 0)
    note_capacity(manager_capacity, worker_caps, new_worker_cap, cost)

    # --- tier 1: how many replicas do we want? ----------------------------
    want_replicas = desired_replicas(replicas, p95, cpu_rep)
    want_replicas = max(MIN_REPLICAS, min(MAX_REPLICAS, want_replicas))
    M_REPLICAS_WANT.set(want_replicas)

    # --- tier 2: how many hosts must exist to hold them? ------------------
    want_hosts = hosts_needed(want_replicas, node_pressure, manager_capacity,
                              worker_caps, new_worker_cap)
    want_hosts = max(floor, min(MAX_WORKERS, want_hosts))
    M_DESIRED.set(want_hosts)

    # --- placement mode ---------------------------------------------------
    # Where the app is allowed to run right now, read from the live spec, and
    # where it should be. `want_hosts`, not the current count, decides the
    # target: deriving it from what exists today would flap, because scaling in
    # deliberately spends a few loops with the manager eligible AND a worker
    # still up.
    mode = placement_mode(service)
    rolling = update_in_progress(service)
    want_mode = MODE_MANAGER if want_hosts <= 1 else MODE_WORKER
    M_MODE.set(1 if mode == MODE_WORKER else 0)

    # Emergency: worker mode with an empty fleet means every task is pending
    # and the site is down. Whatever the desired state says, the master is the
    # only node left that can serve. Same code path as an ordinary scale-in.
    stranded = mode == MODE_WORKER and current_workers == 0
    if stranded:
        want_mode = MODE_MANAGER

    # Cap replicas at what the CURRENTLY eligible nodes can actually hold. This
    # is what keeps the scale-out handover gapless: while workers are booting
    # the app is still manager-only, so the count stays at what the master can
    # serve and rises after the flip, instead of queueing tasks that have
    # nowhere to run.
    if mode == MODE_MANAGER:
        placeable = manager_capacity + sum(worker_caps)
        where = f"master ({manager_capacity}) + {current_workers} worker(s)"
    else:
        placeable = sum(worker_caps)
        where = f"{current_workers} worker(s)"
    if want_replicas > placeable:
        log.info(
            "holding at %d replicas: %d wanted, %d fit on %s right now",
            max(MIN_REPLICAS, placeable), want_replicas, placeable, where,
        )
        want_replicas = max(MIN_REPLICAS, placeable)

    # ---------------------------------------------------------------------
    # HANDOVER, PART 1 — hand BACK to the manager before shrinking the fleet.
    #
    # This runs before any node is removed, on purpose. Removing the last
    # worker first would leave the app pinned to a role with no nodes in it.
    # Flipping first means the master picks up tasks while that worker is
    # still serving, and the removal below then waits for proof of it.
    # ---------------------------------------------------------------------
    if want_mode == MODE_MANAGER and mode == MODE_WORKER and not rolling:
        if stranded:
            reason = ("no worker is left in the swarm and the app is pinned to "
                      "workers — every task is unplaceable; failing back to the master")
            log.error("app stranded with an empty worker fleet: %s", APP_SERVICE)
            M_ERRORS.labels(stage="stranded").inc()
        else:
            reason = (f"scaling in to {want_hosts} host(s); master takes the "
                      f"replicas back (it holds {manager_capacity})")
        try:
            set_placement_mode(MODE_MANAGER, reason)
            mode = MODE_MANAGER
        except Exception as exc:  # noqa: BLE001
            M_ERRORS.labels(stage="placement").inc()
            log.error("could not release the worker pin: %s", exc)

    # ---------------------------------------------------------------------
    # SCALE UP: nodes first, then replicas onto them.
    # ---------------------------------------------------------------------
    # `owned` is what we are already paying for: ready workers plus the ones
    # still booting. Ordering against the ready count alone re-orders the same
    # capacity every loop for two minutes and sails past MAX_WORKERS.
    booting = len(provisioning_workers())
    owned = current_workers + booting
    # Only real servers are bought; the master is host #1 and already exists.
    want_servers = max(0, want_hosts - 1)
    if want_servers > owned:
        age = newest_worker_age()
        if age is not None and age < COOLDOWN_UP:
            log.info(
                "worker scale-up suppressed: newest is %.0fs old, cooldown %ds",
                age, COOLDOWN_UP,
            )
        elif owned >= MAX_WORKERS - 1:
            log.warning("at host ceiling %d (master + %d workers) — this is a "
                        "budget cap, not capacity", MAX_WORKERS, MAX_WORKERS - 1)
        else:
            for _ in range(min(want_servers - owned, (MAX_WORKERS - 1) - owned)):
                create_worker()
    elif booting:
        log.info("%d worker(s) still booting; not ordering more", booting)

    # ---------------------------------------------------------------------
    # REPLICAS: applied every loop, subject to their own short cooldown.
    # ---------------------------------------------------------------------
    if want_replicas != replicas:
        since = time.time() - _last_replica_change
        if since < REPLICA_COOLDOWN:
            log.info("replica change suppressed: %.0fs since last", since)
        else:
            set_replicas(service, want_replicas)
            _last_replica_change = time.time()
            replicas = want_replicas

    # ---------------------------------------------------------------------
    # HANDOVER, PART 2 — take the app OFF the manager, once and only once the
    # workers can hold all of it.
    #
    # Both conditions matter. Workers that exist but are not `ready` cannot be
    # scheduled onto, and workers that are ready but too few would leave the
    # remainder pending the moment the master stops being eligible. Until both
    # hold, the master keeps serving and we simply try again next loop.
    # ---------------------------------------------------------------------
    if want_mode == MODE_WORKER and mode == MODE_MANAGER:
        worker_room = sum(worker_caps)
        if rolling:
            log.info("deferring the move to worker mode: a rollout is in flight")
        elif current_workers == 0:
            log.info("deferring the move to worker mode: no worker is ready yet")
        elif worker_room < max(replicas, want_replicas):
            log.info(
                "deferring the move to worker mode: %d worker(s) hold %d, "
                "need room for %d — master keeps serving until then",
                current_workers, worker_room, max(replicas, want_replicas),
            )
        else:
            try:
                set_placement_mode(
                    MODE_WORKER,
                    f"{current_workers} worker(s) ready with room for "
                    f"{worker_room}; master drops to zero replicas",
                )
                mode = MODE_WORKER
            except Exception as exc:  # noqa: BLE001
                M_ERRORS.labels(stage="placement").inc()
                log.error("could not pin the app to workers: %s", exc)

    # ---------------------------------------------------------------------
    # SCALE DOWN: only after replicas have been shed, and slowly. Removing the
    # LAST worker additionally waits for the master to be serving already.
    # ---------------------------------------------------------------------
    if want_servers < current_workers and want_replicas <= replicas:
        since = time.time() - _last_scale_down
        last_one = current_workers == 1
        on_manager = running_replicas_on_manager() if last_one else 0
        # The master counts as room only once the pin is off and it is a legal
        # placement target again — which HANDOVER PART 1 above has already done
        # by the time a last-worker removal is on the table.
        spare = manager_capacity if mode == MODE_MANAGER else 0
        candidate = pick_removal_candidate(ready, worker_caps, want_replicas, spare)
        if since < COOLDOWN_DOWN:
            log.info("worker scale-down suppressed: %.0fs since last", since)
        elif current_hosts <= floor:
            pass
        elif candidate is None:
            # Heterogeneous fleets: dropping the newest node is not always safe
            # when nodes differ in size. If no single node can leave without the
            # rest falling short of the replica count, keep them all.
            log.info(
                "no worker can be removed without dropping below %d replica(s) "
                "of capacity (have %d across %d worker(s))",
                want_replicas, sum(worker_caps), current_workers,
            )
        elif last_one and mode != MODE_MANAGER:
            # Part 1 above should already have released the pin; if it could
            # not, deleting this worker would take the site down rather than
            # scale it to zero. Wait instead.
            log.warning(
                "holding the last worker: %s is still pinned to worker nodes",
                APP_SERVICE,
            )
        elif last_one and on_manager < 1:
            # The pin is off and the master is eligible, but Swarm has not
            # started a task there yet. Deleting the worker now is the one move
            # that produces a gap, so we wait — indefinitely if it comes to
            # that. Keeping one worker costs a few euros a month; removing it
            # blind costs the site, and that is never the better trade.
            #
            # It normally resolves within a loop or two: `spread: node.id`
            # counts tasks of this service per node, the master has none, so it
            # wins the next placement. If it does not resolve, something is
            # wrong that an operator has to see.
            if _manager_wait_since is None:
                _manager_wait_since = time.time()
            waited = time.time() - _manager_wait_since
            if waited > HANDOVER_STALL_SECONDS:
                M_ERRORS.labels(stage="handover").inc()
                log.error(
                    "handover stalled: %.0fs with the worker pin released and "
                    "still no replica running on the master. The last worker "
                    "stays until there is one. Check `docker service ps %s` for "
                    "a task the master cannot place — usually a resource "
                    "reservation that no longer fits.",
                    waited, APP_SERVICE,
                )
            else:
                log.info(
                    "holding the last worker: no replica on the master yet "
                    "(%.0fs, rollout %s)",
                    waited, "in flight" if rolling else "settling",
                )
        else:
            _manager_wait_since = None
            if last_one:
                log.info(
                    "removing the last worker: master is already serving %d "
                    "replica(s), fleet goes to zero",
                    on_manager,
                )
            remove_worker(candidate)
            _last_scale_down = time.time()

    log.info(
        "hosts %d/%d (floor %d) · replicas %d/%d · room %d (master %d, workers %s) · "
        "placement %s%s · p95 %s · cpu/replica %s",
        current_hosts, MAX_WORKERS, floor, replicas, want_replicas,
        placeable, manager_capacity,
        "+".join(str(c) for c in worker_caps) or "-",
        mode, f" -> {want_mode}" if want_mode != mode else "",
        f"{p95:.0f}ms" if p95 is not None else "n/a",
        f"{cpu_rep:.0f}%" if cpu_rep is not None else "n/a",
    )


def main():
    start_http_server(9200)
    log.info(
        "autoscaler up — cluster=%s service=%s slo_p95=%.0fms "
        "hosts=%d..%d replicas=%d..%d dry_run=%s",
        CLUSTER, APP_SERVICE, SLO_P95_MS,
        MIN_WORKERS, MAX_WORKERS, MIN_REPLICAS, MAX_REPLICAS, DRY_RUN,
    )
    if MIN_WORKERS == 1:
        log.info(
            "host floor is 1: the master is host #1, so an idle cluster bills "
            "no Hetzner servers at all. Per-node capacity is measured, not "
            "configured — see the 'room' figure in each loop line."
        )
    else:
        log.info(
            "host floor is %d: the master is never a host, so at least %d "
            "Hetzner worker(s) always run", MIN_WORKERS, MIN_WORKERS,
        )
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