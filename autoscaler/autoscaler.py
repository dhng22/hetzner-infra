#!/usr/bin/env python3
"""
Two-tier autoscaler for Docker Swarm on Hetzner Cloud.

POLICY
------
Load is absorbed in two stages, cheapest first:

  1. REPLICAS. Adding a task to existing capacity takes seconds. The desired
     replica count is driven by p95 latency against your SLO, with
     CPU-per-replica as the secondary signal.
  2. NODES. Only when the replicas we want will not fit on the workers we
     have. Provisioning takes ~2 minutes including JVM warmup.

Coming down, the order reverses: shed replicas first, remove the node later
and slowly.

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
APP_CPU_LIMIT = _env("APP_CPU_LIMIT", "1.0", float)   # must match the stack file
MIN_REPLICAS = _env("MIN_REPLICAS", "2", int)
MAX_REPLICAS = _env("MAX_REPLICAS", "12", int)
REPLICAS_PER_WORKER = _env("REPLICAS_PER_WORKER", "2", int)

# --- nodes ----------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# metrics about ourselves
# ---------------------------------------------------------------------------

M_CURRENT = Gauge("autoscaler_current_workers", "Worker nodes currently in the swarm")
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


def pick_removal_candidate(ready):
    """Newest-first (LIFO): least likely to hold warm state, hourly billing."""
    def created(node):
        return node.attrs.get("CreatedAt", "")
    return sorted(ready, key=created, reverse=True)[0]


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

def read_signals():
    """Returns (p95_ms, cpu_per_replica_pct, node_pressure_pct). Any may be None."""
    p95 = vm_query(P95_EXPR)
    cpu_rep = vm_query(CPU_REPLICA_EXPR)
    node_mem = vm_query(MEM_EXPR)
    node_cpu = vm_query(CPU_EXPR)

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


def workers_needed(replicas, node_pressure):
    """
    How many workers must exist to hold this many replicas. Resource pressure
    forces an extra node even when the replica arithmetic says otherwise —
    Swarm will refuse to place a task that does not fit, and a queued task
    serves no traffic.
    """
    needed = -(-replicas // REPLICAS_PER_WORKER)  # ceil
    if node_pressure is not None and node_pressure > NODE_PRESSURE_PCT:
        needed += 1
        log.info("worker resource pressure at %.0f%%: requesting an extra worker",
                 node_pressure)
    return needed


def newest_worker_age():
    servers = hetzner_workers()
    if not servers:
        return None
    newest = max(servers, key=lambda s: s.created)
    return (datetime.now(timezone.utc) - newest.created).total_seconds()


def loop():
    global _last_scale_down, _last_replica_change

    reap_orphans()

    service = get_service()
    ready = swarm_ready_workers()
    current_workers = len(ready)
    floor = scheduled_floor()

    M_CURRENT.set(current_workers)
    M_MAX.set(MAX_WORKERS)
    M_MIN.set(floor)
    M_REPLICAS_MAX.set(MAX_REPLICAS)

    p95, cpu_rep, node_pressure = read_signals()

    if service is None:
        log.warning("service unavailable; holding at %d workers", current_workers)
        return

    replicas = current_replicas(service)
    M_REPLICAS.set(replicas)

    # --- tier 1: how many replicas do we want? ----------------------------
    want_replicas = desired_replicas(replicas, p95, cpu_rep)
    want_replicas = max(MIN_REPLICAS, min(MAX_REPLICAS, want_replicas))
    M_REPLICAS_WANT.set(want_replicas)

    # --- tier 2: how many workers must exist to hold them? ----------------
    want_workers = workers_needed(want_replicas, node_pressure)
    want_workers = max(floor, min(MAX_WORKERS, want_workers))
    M_DESIRED.set(want_workers)

    # If the ceiling blocks the nodes we need, cap replicas to what will
    # actually fit rather than queueing tasks Swarm can never place.
    placeable = want_workers * REPLICAS_PER_WORKER
    if want_replicas > placeable:
        log.warning(
            "want %d replicas but only %d will fit on %d workers (ceiling %d)",
            want_replicas, placeable, want_workers, MAX_WORKERS,
        )
        want_replicas = max(MIN_REPLICAS, placeable)

    # ---------------------------------------------------------------------
    # SCALE UP: nodes first, then replicas onto them.
    # ---------------------------------------------------------------------
    if want_workers > current_workers:
        age = newest_worker_age()
        if current_workers >= floor and age is not None and age < COOLDOWN_UP:
            log.info(
                "worker scale-up suppressed: newest is %.0fs old, cooldown %ds",
                age, COOLDOWN_UP,
            )
        elif current_workers >= MAX_WORKERS:
            log.warning("at worker ceiling %d — this is a budget cap, not capacity",
                        MAX_WORKERS)
        else:
            for _ in range(min(want_workers - current_workers,
                               MAX_WORKERS - current_workers)):
                create_worker()

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

    # ---------------------------------------------------------------------
    # SCALE DOWN: only after replicas have already been shed, and slowly.
    # ---------------------------------------------------------------------
    if want_workers < current_workers and want_replicas <= replicas:
        since = time.time() - _last_scale_down
        if since < COOLDOWN_DOWN:
            log.info("worker scale-down suppressed: %.0fs since last", since)
        elif current_workers <= floor:
            pass
        else:
            remove_worker(pick_removal_candidate(ready))
            _last_scale_down = time.time()

    log.info(
        "workers %d/%d (floor %d) · replicas %d/%d · p95 %s · cpu/replica %s",
        current_workers, MAX_WORKERS, floor, replicas, want_replicas,
        f"{p95:.0f}ms" if p95 is not None else "n/a",
        f"{cpu_rep:.0f}%" if cpu_rep is not None else "n/a",
    )


def main():
    start_http_server(9200)
    log.info(
        "autoscaler up — cluster=%s service=%s slo_p95=%.0fms "
        "workers=%d..%d replicas=%d..%d dry_run=%s",
        CLUSTER, APP_SERVICE, SLO_P95_MS,
        MIN_WORKERS, MAX_WORKERS, MIN_REPLICAS, MAX_REPLICAS, DRY_RUN,
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