#!/usr/bin/env python3
"""
Dataguard: the autoscaler's counterpart for the components that hold your data.

WHAT IT IS
----------
The autoscaler owns replica counts for stateless services. Dataguard owns the
SHAPE of a stateful one: how many members a replica set has, which machines they
are on, which of them is primary, whether they are backed up, and whether the
backup can actually be restored. Same philosophy, and deliberately the same
shape: discovered by label, policy carried on the service it applies to, no
component named anywhere in this file.

It subscribes to the overseer exactly the way the autoscaler does — three deploy
labels on its own service, `infra.handles=database` — which is the extension
point `signals/classify.py` has documented since before this existed.

WHAT IT NEVER DOES
------------------
It does not create or delete Swarm services, and it does not buy machines.

Not creating services is the important one. A component's `stack.yml` is
rendered by the panel and deployed with `--prune`, so a service created outside
that stack is deleted by the next unrelated save. Instead the renderer emits ALL
n+1 members up front, most of them at `replicas: 0`, and dataguard moves a member
between 0 and 1 and sets its node constraint. The renderer reads both back off
the live service, the same way it already reads an application's image and
replica count back — live wins over spec, because something other than the file
owns those values at runtime (docker/cli#2235).

Machines come from the overseer. Dataguard POSTs its complete set of node leases
every loop and reads back which machines exist; it holds no Hetzner token. A
lease is a promise not to delete, never a promise to keep paying: when dataguard
stops asking, the machine becomes an ordinary worker that the overseer will only
remove once nothing is running on it. The failure mode of a dataguard outage is
therefore a server that costs money, never one that takes a database with it.

THE CONNECTION STRING NEVER CHANGES
-----------------------------------
This is the property everything else is arranged around. A component is created
with a seed list naming all n+1 members, and the replica set config contains only
the members that exist. A driver tolerates seeds that do not resolve — it
discovers the real topology from whichever one answers — so member 4 can be a
name that means nothing for six months and then suddenly mean a machine in
Helsinki, with no redeploy of anything that talks to it.

Configuring all n+1 in `rs.config()` up front would be the exact opposite: a set
with no majority, and therefore no primary, and therefore no writes.

WHAT IT WILL NOT PRETEND
------------------------
Read scaling is an application contract, not an infrastructure setting. Adding
replicas only helps reads that are allowed to go to a secondary, and a read that
goes to a secondary can be behind the write that produced it. When a component
has secondary reads turned off, dataguard says so and reaches for a bigger
machine instead of adding a replica that could not have helped.
"""

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import docker
import requests
from prometheus_client import Counter, Gauge, start_http_server

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from signals import classify, query, workloads  # noqa: E402

import engines  # noqa: E402
import pki  # noqa: E402
import plan  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    stream=sys.stdout)
log = logging.getLogger("dataguard")


def _env(key, default=None, cast=str):
    """
    A setting, resolved default-first. An EMPTY value is an ABSENT one.

    The same rule as the other two processes, and for the same reason: compose
    substitutes an unknown `${KEY}` with the empty string, so the container
    always receives the key and `os.environ.get` never sees its default.
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
INFRA_DIR = _env("INFRA_DIR", "/opt/infra")
STATE_DIR = os.path.join(INFRA_DIR, "state")
LOOP_SECONDS = _env("LOOP_SECONDS", "60", int)
SIGNAL_PORT = _env("SIGNAL_PORT", "9202", int)
METRICS_PORT = _env("METRICS_PORT", "9203", int)
OVERSEER_URL = _env("OVERSEER_URL", "http://monitoring_overseer:9211")
SIGNAL_TTL_SECONDS = _env("SIGNAL_TTL_SECONDS", str(LOOP_SECONDS * 3), int)

# Whether a given database is managed is a switch on THAT database, not here.
# What is here is the rehearsal switch, which is a property of the process.
ENABLED = True
DRY_RUN = _env("DATAGUARD_DRY_RUN", "false", bool)
# HOURS, not the autoscaler's ninety seconds. An initial sync is not a replica
# start: it reads the whole dataset off a live member and can take hours on a
# database of any size. A control loop that reacts faster than the thing it is
# controlling oscillates.
TOPOLOGY_COOLDOWN_SECONDS = _env("TOPOLOGY_COOLDOWN_SECONDS", "14400", int)
PRESSURE_SUSTAIN_SECONDS = _env("PRESSURE_SUSTAIN_SECONDS", "3600", int)
LAG_BUDGET_SECONDS = _env("LAG_BUDGET_SECONDS", "10", float)
BACKUP_MAX_AGE_SECONDS = _env("BACKUP_MAX_AGE_SECONDS", "86400", int)
VIEWER_IDLE_SECONDS = _env("VIEWER_IDLE_SECONDS", "900", int)
DISK_HEADROOM = _env("DB_DISK_HEADROOM", "2.5", float)

# --- labels: the contract with the component renderer ----------------------
DG = "dataguard."
L_ENABLED = DG + "enabled"
L_MEMBER = DG + "member"
L_POOL = DG + "pool"
L_MAX_MEMBERS = DG + "max_members"
L_LAG_BUDGET = DG + "lag_budget_seconds"
L_SECONDARY_READS = DG + "secondary_reads"
L_BACKUP_TARGET = DG + "backup_target"
L_MAX_SNAPSHOTS = DG + "max_snapshots"
L_SET_NAME = DG + "set"
L_VIEWER = DG + "viewer"

TYPE_LABEL = "infra.type"


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------
# Every gauge carries `component`, so no alert has to name one — the rule the
# whole alert file is built on, because component names are created at runtime.

_C = ["component"]
G_STATE = Gauge("dataguard_component_state", "1 on the state this component is in",
                _C + ["state"])
G_MEMBERS = Gauge("dataguard_members", "Members by replica-set state", _C + ["state"])
G_LAG = Gauge("dataguard_replication_lag_seconds", "Seconds a secondary is behind",
              _C + ["member"])
#: Per MEMBER, keyed by its Swarm service name so the panel can join it against
#: a task without knowing anything about replica sets. `dataguard_members` is
#: the count by state and answers "is there a primary"; this answers "which one",
#: which is what the map draws.
G_MEMBER = Gauge("dataguard_member_state", "1 on the state this member is in",
                 _C + ["member", "state"])
G_CHANGE = Gauge("dataguard_topology_change_in_flight",
                 "1 while this component is mid-transition", _C)
G_BACKUP_OK = Gauge("dataguard_backup_last_success_timestamp",
                    "Unix time of the last completed backup", _C)
G_BACKUP_VERIFIED = Gauge("dataguard_backup_last_verified_timestamp",
                          "Unix time of the last backup proven to RESTORE", _C)
#: How much data this database actually holds. Swarm advertises cores and memory
#: and says nothing about a volume, so without this the panel could show a
#: component's CPU and memory footprint and be silent about the only resource
#: that cannot be recovered by shedding load.
G_DATA = Gauge("dataguard_data_bytes", "Bytes this database holds on disk", _C)
G_TLS_DAYS = Gauge("dataguard_tls_days_remaining",
                   "Days before this member's certificate expires", _C + ["member"])
G_RESTORE = Gauge("dataguard_restore_in_flight", "1 while a restore is running")
G_LEASES = Gauge("dataguard_node_leases", "Machines held for database members")
G_LOOP = Gauge("dataguard_last_loop_timestamp_seconds", "Unix time of the last loop")
G_MANAGED = Gauge("dataguard_managed_components", "Components under management")
G_SIGNAL_AT = Gauge("dataguard_last_dispatch_timestamp_seconds",
                    "Unix time of the last verdict delivered by the overseer")
C_ACTIONS = Counter("dataguard_actions_total", "Topology actions taken", ["action"])
#: The one that gets read most. When nothing is happening, the only question
#: anybody has is WHICH GATE is holding it — and a log line nobody tails is not
#: an answer.
C_REFUSED = Counter("dataguard_refused_total", "Actions declined, by gate", ["reason"])
C_ERRORS = Counter("dataguard_errors_total", "Failures by stage", ["stage"])

workloads.on_error = lambda stage: C_ERRORS.labels(stage=stage).inc()
query.on_error = lambda stage: C_ERRORS.labels(stage=stage).inc()

dkr = docker.DockerClient(base_url="unix:///var/run/docker.sock")

_running = True
_said = set()


def say_once(key, message, *args):
    if key in _said:
        return
    if len(_said) > 5000:
        _said.clear()
    _said.add(key)
    log.info(message, *args)


workloads.on_warn = lambda key, message, *args: say_once(key, message, *args)


# ---------------------------------------------------------------------------
# the dispatch receiver
# ---------------------------------------------------------------------------

_signals_lock = threading.Lock()
_dispatched = {}


def record_dispatch(payload):
    now = time.time()
    received = payload.get("signals") or []
    with _signals_lock:
        for verdict in received:
            name = verdict.get("service")
            if name:
                _dispatched[name] = (now, verdict)
    G_SIGNAL_AT.set(now)
    return len(received)


def dispatched_targets(now=None):
    """
    {component: reason} for every fresh verdict blaming a database.

    The overseer names the target as the busy COMPONENT, so this is already the
    right key. A stale verdict is dropped rather than believed: it is a statement
    about a cluster that has since changed.
    """
    now = now or time.time()
    out = {}
    with _signals_lock:
        entries = list(_dispatched.items())
    for _service, (at, verdict) in entries:
        if now - at > SIGNAL_TTL_SECONDS:
            continue
        if verdict.get("cause") != classify.CAUSE_DATABASE:
            continue
        target = verdict.get("target")
        if target:
            out[target] = verdict.get("reason") or "attributed by the overseer"
    return out


class _SignalHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):                                   # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 1_000_000:
            self.send_error(400, "empty or oversized body")
            return
        try:
            count = record_dispatch(json.loads(self.rfile.read(length)))
        except Exception as exc:  # noqa: BLE001
            C_ERRORS.labels(stage="signal").inc()
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


def monitoring_address():
    """
    This container's address on the `monitoring` overlay.

    THE ONE INTERFACE THIS PROCESS MAY LISTEN ON. Dataguard is the only
    infrastructure service attached to `edge` as well — it has to be, because
    mongod and redis live there — and binding 0.0.0.0 would put an endpoint that
    rewrites database topology on the same network as every application in the
    cluster. The autoscaler can bind wide safely because it is on one network;
    this cannot, and the difference is easy to miss when copying its code.

    Refuses to start rather than falling back to 0.0.0.0. A security property
    that silently degrades is not one.
    """
    # ASKED OF THE ROUTING TABLE, not of DNS. The obvious version of this looks
    # up `tasks.monitoring_dataguard` and keeps whichever of the container's own
    # addresses comes back — but Docker's embedded DNS answers a `tasks.` query
    # with the task's addresses on EVERY network the asker shares with it, and
    # this container shares both. That version therefore returns the edge
    # address about as often as the monitoring one, and when it does it binds
    # the topology endpoint to the network it exists to stay off, reports
    # success, and looks exactly like the correct outcome in the log.
    #
    # A UDP socket connected to a peer that is only on `monitoring` gives the
    # source address the kernel would use to reach it. No packet is sent, the
    # peer does not need to be up, and the answer is the interface — which is
    # the actual question.
    # VictoriaMetrics is on `monitoring` and nothing else, and dataguard
    # already queries it every loop — so if this peer is wrong, nothing here
    # works anyway. Taken from the same setting rather than a second copy.
    peer = urlparse(query.VM_URL).hostname or "victoriametrics"
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect((peer, 9))
            addr = probe.getsockname()[0]
        finally:
            probe.close()
        if addr and addr != "0.0.0.0":
            return addr
    except OSError:
        pass
    # Single-address container: whatever it has IS the monitoring address.
    addrs = {i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None,
                                                 socket.AF_INET)}
    if len(addrs) == 1:
        return addrs.pop()
    raise RuntimeError(
        "cannot tell which of this container's addresses is the monitoring "
        f"overlay ({', '.join(sorted(addrs))}). Refusing to listen on all of "
        "them: the /signal endpoint changes database topology and `edge` is "
        "where every application lives.")


def serve_signals(address=None):
    address = address or monitoring_address()
    server = ThreadingHTTPServer((address, SIGNAL_PORT), _SignalHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name="signal-receiver")
    thread.start()
    log.info("listening for dispatch on %s:%d — the monitoring overlay only, never "
             "edge (verdicts expire after %ds)", address, SIGNAL_PORT,
             SIGNAL_TTL_SECONDS)
    return server


# ---------------------------------------------------------------------------
# discovery — by label, like everything else here
# ---------------------------------------------------------------------------

def _num(labels, key, default, cast=float):
    try:
        return cast(labels[key])
    except (KeyError, TypeError, ValueError):
        return default


def _flag(labels, key, default=False):
    raw = labels.get(key)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


class Component:
    """
    One managed database, assembled from the labels on its member services.

    Nothing is read from disk. The panel writes `component.json`, but this
    process is a different image on a different schedule, and a file is a copy
    that goes stale — the deploy labels are on the objects Swarm is actually
    running, which is the same reason the autoscaler reads policy from there.
    """

    def __init__(self, name, kind, services):
        self.name = name
        self.kind = kind                    # "mongo" | "redis"
        self.services = services            # {member index: docker service}
        first = next(iter(services.values()))
        labels = (first.attrs.get("Spec", {}) or {}).get("Labels") or {}
        self.labels = labels
        self.enabled = ENABLED and _flag(labels, L_ENABLED, True)
        self.dry_run = DRY_RUN
        self.pool = int(_num(labels, L_POOL, len(services), int))
        self.max_members = int(_num(labels, L_MAX_MEMBERS, self.pool, int))
        self.lag_budget_seconds = _num(labels, L_LAG_BUDGET, LAG_BUDGET_SECONDS)
        self.secondary_reads = _flag(labels, L_SECONDARY_READS, True)
        self.backup_target = labels.get(L_BACKUP_TARGET) or ""
        self.max_snapshots = int(_num(labels, L_MAX_SNAPSHOTS, 7, int))
        # NOT A PLAN NAME, on either side. The overseer picks the smallest
        # shared x86 type in the location that meets the base requirement, and
        # `bigger` asks it for the next rung up from there — because Hetzner
        # renames and retires plans, and a name typed into a settings file is a
        # name that stops existing silently.
        self.node_type = ""
        self.bigger_node_type = "bigger"
        self.set_name = labels.get(L_SET_NAME) or name
        self.viewer = _flag(labels, L_VIEWER, False)
        self.cooldown_seconds = TOPOLOGY_COOLDOWN_SECONDS
        self.backup_max_age_seconds = BACKUP_MAX_AGE_SECONDS
        self.disk_headroom = DISK_HEADROOM
        self.last_change_at = 0.0

    # Member 1 is the one on the manager, by construction: the renderer pins it
    # there and dataguard never moves it. Everything that has to reason about
    # "the copy on the master" asks for it by that name rather than by looking
    # at where things happen to be, because where they happen to be is exactly
    # what is being changed.
    @property
    def master_member(self):
        return f"{self.name}_{self.kind}-1"

    @property
    def master_host(self):
        return f"{self.master_member}:{self.port}"

    @property
    def port(self):
        return 27017 if self.kind == "mongo" else 6379

    def member_host(self, index):
        return f"{self.name}_{self.kind}-{index}:{self.port}"

    def seed_hosts(self):
        return [self.member_host(i) for i in range(1, self.pool + 1)]

    def lease(self, index):
        return f"{self.name}/{index}"

    def live_members(self):
        """Member indices whose service is scaled above zero."""
        out = []
        for index, service in self.services.items():
            mode = (service.attrs.get("Spec", {}).get("Mode") or {})
            if (mode.get("Replicated") or {}).get("Replicas", 0) >= 1:
                out.append(index)
        return sorted(out)

    def env(self, key):
        """
        One environment variable off a member's container spec.

        This is where the database password comes from. Dataguard already holds
        the docker socket, which is root-equivalent on this box, so reading a
        service's own environment adds no privilege — and it means there is no
        second copy of the credential to rotate, and none of it ever reaches a
        command line.
        """
        for service in self.services.values():
            spec = (service.attrs.get("Spec", {}).get("TaskTemplate") or {})
            for entry in (spec.get("ContainerSpec") or {}).get("Env") or []:
                if entry.startswith(f"{key}="):
                    return entry.split("=", 1)[1]
        return ""


def managed_components():
    """
    {name: Component} for everything carrying `infra.managed_by=dataguard`.

    A component with no member services at all is not returned — there is
    nothing to manage and nothing to report, and inventing an entry for it would
    put a component that no longer exists into the metrics forever.
    """
    found = {}
    try:
        services = dkr.services.list()
    except Exception as exc:                                     # noqa: BLE001
        C_ERRORS.labels(stage="discovery").inc()
        log.error("cannot list services: %s", exc)
        return {}
    for service in services:
        labels = (service.attrs.get("Spec", {}) or {}).get("Labels") or {}
        if labels.get(workloads.MANAGED_BY_LABEL) != workloads.MANAGED_BY_DATAGUARD:
            continue
        name = labels.get(workloads.COMPONENT_LABEL)
        kind = labels.get(TYPE_LABEL)
        index = labels.get(L_MEMBER)
        if not (name and kind and index):
            continue
        try:
            found.setdefault((name, kind), {})[int(index)] = service
        except ValueError:
            continue
    return {name: Component(name, kind, members)
            for (name, kind), members in found.items()}


# ---------------------------------------------------------------------------
# machines — asked for, never bought
# ---------------------------------------------------------------------------

_leases = {}            # lease name -> {"type": ..., "purpose": ...}
_lease_nodes = {}       # lease name -> {"hostname": ..., "state": ...}


def hold_lease(name, node_type, purpose):
    """
    Ask for a machine. `node_type` is "" for the base plan or "bigger" for the
    next rung up — never a plan name, because the overseer is the thing that can
    read the catalogue and a name in a request is a name that stops existing.
    """
    request = {"lease": name, "purpose": purpose}
    if node_type == "bigger":
        request["bigger"] = True
    _leases[name] = request


def release_lease(name):
    _leases.pop(name, None)


def sync_leases():
    """
    Tell the overseer the complete current set, and read back what exists.

    Level-triggered, whole world every time, no queue: a request that fails to
    arrive is corrected by the next one a minute later. That also means a
    dataguard restart re-asserts every lease within one loop, which is why the
    overseer's lease TTL is longer than this interval by a wide margin.
    """
    global _lease_nodes
    G_LEASES.set(len(_leases))
    body = json.dumps({"from": "dataguard", "leases": list(_leases.values())}).encode()
    try:
        resp = requests.post(f"{OVERSEER_URL}/nodes", data=body, timeout=10,
                             headers={"Content-Type": "application/json"})
        resp.raise_for_status()
        _lease_nodes = resp.json().get("nodes") or {}
    except Exception as exc:                                     # noqa: BLE001
        C_ERRORS.labels(stage="lease").inc()
        say_once(("lease", str(exc)[:60]),
                 "cannot reach the overseer at %s (%s). No machine will be "
                 "provisioned until it comes back; nothing already running is "
                 "affected.", OVERSEER_URL, exc)


def lease_ready(name):
    """
    Ready AND schedulable. Both, because a leased machine joins PAUSED.

    `docker swarm join` cannot set node labels, so there is a window between a
    machine joining and the overseer stamping `dedicated=true` on it in which
    nothing keeps application replicas off a database's disk. Joining paused
    closes that window; the cost is that "ready" alone is not enough here, and
    reading it as enough would scale a member onto a machine that cannot run it
    — leaving the task pending while everything involved reported success.
    """
    node = _lease_nodes.get(name)
    return bool(node and node.get("state") == "ready"
                and node.get("availability") == "active")


# ---------------------------------------------------------------------------
# the infrastructure verbs
# ---------------------------------------------------------------------------
# Dataguard changes exactly two things about a member service: how many replicas
# it has (0 or 1) and which node it is constrained to. Both are read back by the
# component renderer, so a panel save does not undo them — the same contract the
# autoscaler has for an application's replica count and worker pin.

def _service_update(name, *args):
    if DRY_RUN:
        log.info("[dry-run] would run docker service update %s %s", " ".join(args), name)
        return True
    cmd = ["docker", "service", "update", "--detach=true", *args, name]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise engines.Refused(
            f"{' '.join(cmd)} failed: {(proc.stderr or proc.stdout).strip()[:300]}")
    return True


def start_member(component, index, node_hostname):
    """
    Pin a member to its machine, THEN start it. Never the other way round.

    Scaling first would let Swarm place the task wherever it likes, and a mongod
    that starts on the wrong node creates its data directory there — so the
    constraint added a second later cannot move it without an initial sync onto
    a machine that already had one.
    """
    service = f"{component.name}_{component.kind}-{index}"
    if node_hostname:
        _service_update(service, "--constraint-add", f"node.hostname=={node_hostname}")
    _service_update(service, "--replicas", "1")
    C_ACTIONS.labels(action="start_member").inc()
    log.info("%s: member %d starting on %s", component.name, index,
             node_hostname or "the master")


def stop_member(component, index):
    """
    Scale a member to zero. The VOLUME is kept, always.

    Removing the data is never automatic and never a side effect of shrinking.
    A member that comes back finds its data directory and rejoins with an
    incremental catch-up instead of a full initial sync, which is the difference
    between seconds and hours.
    """
    service = f"{component.name}_{component.kind}-{index}"
    _service_update(service, "--replicas", "0")
    C_ACTIONS.labels(action="stop_member").inc()
    log.info("%s: member %d stopped; its volume is kept", component.name, index)


def index_of_host(component, host):
    name = host.split(":")[0]
    prefix = f"{component.name}_{component.kind}-"
    if not name.startswith(prefix):
        return None
    try:
        return int(name[len(prefix):])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# engines, built from what the service itself says
# ---------------------------------------------------------------------------

def engine_for(component):
    if component.kind == "mongo":
        import pymongo

        user = component.env("MONGO_INITDB_ROOT_USERNAME") or "root"
        password = component.env("MONGO_INITDB_ROOT_PASSWORD")
        ca = os.path.join(STATE_DIR, "dataguard", component.name, "ca.crt")

        def client():
            return pymongo.MongoClient(
                host=component.seed_hosts(), username=user, password=password,
                authSource="admin", replicaSet=None, directConnection=False,
                tls=True, tlsCAFile=ca if os.path.exists(ca) else None,
                serverSelectionTimeoutMS=10000, connectTimeoutMS=10000)
        return engines.MongoEngine(client, component.set_name)

    import redis
    from redis.sentinel import Sentinel

    password = component.env("REDIS_PASSWORD")
    sentinels = [(f"{component.name}_sentinel-{i}", 26379) for i in range(1, 4)]

    def sentinel():
        return Sentinel(sentinels, socket_timeout=5.0,
                        sentinel_kwargs={"password": password},
                        password=password)
    return engines.RedisEngine(sentinel, component.set_name)


# ---------------------------------------------------------------------------
# pressure — what the world is saying about this component
# ---------------------------------------------------------------------------

_pressure_since = {}        # (component, kind) -> first seen


#: How much longer QUIET has to hold than pressure does before it counts.
#:
#: The two windows were the same, and symmetry is exactly wrong here. Growing is
#: a response to something happening now; shrinking is a cost saving that can
#: always wait, and the two thresholds sit next to each other with nothing
#: between them — so any load that alternates on a period near the cooldown
#: walks a database up and down the ladder indefinitely, and every rung is an
#: election, an initial sync and a machine. A daily traffic cycle is exactly
#: that shape. Making the way down slower than the way up is the cheapest
#: hysteresis there is: it costs a few hours of a machine nobody needed and it
#: turns an oscillation into a step.
#:
#: A FULL DAY, because the cycle it has to outlast is a day. At 4 this was still
#: 4h against a 16h overnight lull, so a database busy 09:00-17:00 bought a
#: machine every morning, ran a whole initial sync into it, and dropped it again
#: every evening — thirty machines and thirty full-dataset copies over a month,
#: to end each night exactly where it started. Anything shorter than the quiet
#: half of a daily cycle reproduces that, and every human traffic pattern has a
#: daily cycle, so the window has to be the period itself rather than a fraction
#: of it. The cost of being wrong in this direction is one machine for one extra
#: day; in the other it is a full initial sync every morning, forever.
QUIET_SUSTAIN_MULTIPLE = 24


def _sustained(component, kind, present, now, window=None):
    """
    True once a condition has held for the whole window.

    A database transition costs hours and cannot be undone cheaply, so nothing
    here reacts to a spike. The autoscaler's ninety seconds is right for a
    replica that starts in five; it would be catastrophic for an initial sync.
    """
    key = (component, kind)
    if not present:
        _pressure_since.pop(key, None)
        return False
    first = _pressure_since.setdefault(key, now)
    return now - first >= (PRESSURE_SUSTAIN_SECONDS if window is None else window)


def read_pressure(component, topology, dispatched, node_usage, now, engine=None):
    """
    {capacity, latency, read, write, quiet} — each already sustained.

    `quiet` is held to a window several times longer than the others. See
    QUIET_SUSTAIN_MULTIPLE: equal windows make the way down as fast as the way
    up, and a database on a daily traffic cycle then spends its life mid-move.

    Two independent sources, and they answer different questions. The dispatched
    verdict is the APPLICATION's view: somebody is waiting on this database. The
    node usage is the MACHINE's view: it is running out of room. Either one is
    enough to grow; `quiet` requires both to be absent, for the whole window.
    """
    slow = component.name in dispatched
    usage = node_usage.get(component.name) or {}
    tight = max(usage.get("cpu", 0.0), usage.get("memory", 0.0),
                usage.get("disk", 0.0)) >= 80.0

    capacity = _sustained(component.name, "capacity", tight, now)
    latency = _sustained(component.name, "latency", slow, now)
    # Quiet is the absence of the SUSTAINED signals, not of the raw samples, and
    # over a window this long that distinction is the whole difference between a
    # window and a coin toss. `tight` is one instantaneous reading of one node:
    # a two-minute 80% blip at 03:00 — a backup, a log rotation, a cron — would
    # otherwise reset a day-long clock to zero, and a database that blips once a
    # night could then never shrink at all. What should cancel "we can let one
    # go" is "we actually need to grow", and that is exactly what these two
    # already mean. A blip cannot make either of them true, so it no longer
    # makes quiet false.
    quiet = _sustained(component.name, "quiet", not capacity and not latency, now,
                       window=PRESSURE_SUSTAIN_SECONDS * QUIET_SUSTAIN_MULTIPLE)

    reads, writes = False, False
    if latency and engine is not None:
        reads, writes = _read_write_split(component, engine)

    return {"capacity": capacity, "latency": latency, "quiet": quiet,
            "read": reads, "write": writes,
            "reason": dispatched.get(component.name, "node pressure" if tight else "")}


def _read_write_split(component, engine):
    """
    Is the slowness reads or writes? Asked of the SERVER, not the client.

    Unknown is neither. Returning "reads" for a database whose accounting could
    not be read would have the loop add a replica for a write-bound set, which
    cannot help and costs a machine.
    """
    if not hasattr(engine, "op_latencies"):
        return False, False
    read_us, write_us = engine.op_latencies()
    if read_us is None and write_us is None:
        return False, False
    read_us, write_us = read_us or 0.0, write_us or 0.0
    if read_us == write_us == 0:
        return False, False
    return read_us >= write_us, write_us > read_us


# ---------------------------------------------------------------------------
# TLS material
# ---------------------------------------------------------------------------

def tls_dir(component):
    """
    The component's own directory, which the panel wrote into first.

    The SAME directory, not a copy: `/opt/infra` is one bind mount and both
    processes are on the master. The panel issues this material at create time,
    because the stack references the Swarm secrets and they have to exist before
    the first deploy; renewal is this process's, because it is the one with a
    loop. Two directories would be two answers to "what certificate does member
    2 have".
    """
    return os.path.join(INFRA_DIR, "components", component.name, "tls")


def ensure_tls(component):
    """
    Renew any member certificate that is close to expiring. Never issues a CA.

    An EXPIRED member certificate does not degrade a replica set, it stops it:
    members refuse each other, there is no primary, and every write fails. So
    this starts a month out, does one member per loop — applying a renewal is a
    restart of that member — and exports the days remaining so the state is
    alertable rather than merely true.

    A component whose authority is missing is REPORTED, not repaired. The panel
    creates it; inventing one here would silently replace the material every
    member is already using and take the set down.
    """
    directory = tls_dir(component)
    ca_key = os.path.join(directory, "ca.key")
    if not os.path.exists(ca_key):
        say_once((component.name, "notls"),
                 "%s has no certificate authority in %s. It is created when the "
                 "component is deployed from the panel; redeploy it.",
                 component.name, directory)
        return False
    try:
        key_pem, crt_pem = pki.ensure_ca(directory, CLUSTER, component.name)
    except Exception as exc:                                     # noqa: BLE001
        C_ERRORS.labels(stage="tls").inc()
        log.error("%s: cannot open its certificate authority: %s", component.name, exc)
        return False

    for index in range(1, component.pool + 1):
        path = os.path.join(directory, f"member-{index}.pem")
        existing = _read(path)
        left = pki.days_remaining(existing) if existing else None
        if left is not None:
            G_TLS_DAYS.labels(component=component.name, member=str(index)).set(left)
        if existing is None or not pki.needs_renewal(existing):
            continue
        host = f"{component.name}_{component.kind}-{index}"
        pem = pki.issue_member(key_pem, crt_pem, CLUSTER, component.name,
                               [host, f"tasks.{host}", "localhost", "127.0.0.1"])
        if DRY_RUN:
            log.info("[dry-run] would renew the certificate for %s member %d",
                     component.name, index)
            continue
        _write_local(path, pem)
        name = _ensure_secret(f"{component.name}-tls-{index}", pem)
        _swap_secret(f"{component.name}_{component.kind}-{index}",
                     f"{component.name}-tls-{index}", name, "tls-member.pem")
        G_TLS_DAYS.labels(component=component.name, member=str(index)).set(pki.LEAF_DAYS)
        log.info("%s: renewed the certificate for member %d and restarted it",
                 component.name, index)
        # One per loop. Each renewal restarts a member, and restarting two at
        # once in a three-member set is an election with no majority.
        break
    return True


def _read(path):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _write_local(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(payload)
    os.replace(path + ".tmp", path)


def _swap_secret(service, base, new_name, target):
    """
    Point a service at the new version of a secret it already mounts.

    Swarm secrets are immutable, so renewal is a new NAME — and the service has
    to be told, or it keeps the expiring one until something else redeploys it.
    The component renderer reads the live secret reference back for exactly this
    reason: a later save from the panel must not undo a renewal.
    """
    old = ""
    out = _docker(["service", "inspect", service, "--format",
                   "{{range .Spec.TaskTemplate.ContainerSpec.Secrets}}{{println .SecretName}}{{end}}"])
    for line in out.splitlines():
        if line.strip().startswith(base + "-v"):
            old = line.strip()
    args = ["--secret-add", f"source={new_name},target={target},uid={MONGO_UID},mode=0400"]
    if old and old != new_name:
        args = ["--secret-rm", old] + args
    _service_update(service, *args)


def _secret_versions(base):
    out = []
    for secret in dkr.secrets.list():
        name = secret.name
        if name.startswith(f"{base}-v"):
            try:
                out.append((int(name[len(base) + 2:]), secret))
            except ValueError:
                continue
    return sorted(out)


def _ensure_secret(base, payload):
    """
    Create the NEXT version of a secret and return its name.

    Swarm secrets are immutable, so "update" is a new name and a redeploy of
    whatever mounts it — the versioned dance `admin/settings_def.py` documents.
    The old version is left in place: removing one a running task still
    references fails, and a stale secret costs nothing.
    """
    versions = _secret_versions(base)
    version = (versions[-1][0] + 1) if versions else 1
    name = f"{base}-v{version}"
    dkr.secrets.create(name=name, data=payload,
                       labels={"infra.component": base.split("-tls")[0],
                               "infra.managed_by": workloads.MANAGED_BY_DATAGUARD})
    return name


MONGO_UID = "999"


def _docker(argv):
    try:
        proc = subprocess.run(["docker"] + argv, capture_output=True, text=True,
                              timeout=30)
        return proc.stdout if proc.returncode == 0 else ""
    except (subprocess.TimeoutExpired, OSError):
        return ""


# ---------------------------------------------------------------------------
# backups
# ---------------------------------------------------------------------------

_backup_state = {}      # component -> {"last": ts, "verified": ts}


def backup_age(component, now=None):
    """
    Seconds since the last COMPLETED backup, or None if there has never been one.

    Note what this is not. The gate it feeds wants a backup that has been proven
    to RESTORE, and proving that means restoring into a scratch instance and
    reading the data back — which this does not do yet. So the gate is the
    weaker one it can actually enforce, and
    `dataguard_backup_last_verified_timestamp` stays absent rather than being
    quietly set to the time the backup was taken.

    That absence is deliberate and it is alerted on: BackupNeverVerified fires
    and keeps firing, which is the honest state of affairs. Setting the gauge
    from `last` would make the panel show a green tick for a hypothesis, and
    the whole point of having two timestamps is that they are different claims.
    """
    state = _backup_state.get(component.name) or {}
    taken = state.get("last")
    if not taken:
        return None
    return (now or time.time()) - taken


def run_backup(component):
    """
    Ask this component's own backup controller to take one.

    For Mongo that is `pbm backup` inside the component's pbm-ctl sidecar, which
    is pinned to the master so the panel and this process can both reach it.
    Nothing here reimplements PBM's control protocol: it writes commands into
    the database and the agents pick them up, and a second implementation of
    that is a second thing that can be subtly wrong about a restore.
    """
    if not component.backup_target:
        return
    container = _local_container(f"{component.name}_pbm-ctl"
                                 if component.kind == "mongo"
                                 else f"{component.name}_backup")
    if not container:
        say_once((component.name, "nobackupctl"),
                 "%s has a backup target but no backup controller is running on "
                 "this node; nothing is being backed up.", component.name)
        return
    if DRY_RUN:
        log.info("[dry-run] would take a backup of %s", component.name)
        return
    ok, out = _exec(container, ["pbm", "backup", "--wait"] if component.kind == "mongo"
                    else ["/backup.sh", "once"])
    if ok:
        _backup_state.setdefault(component.name, {})["last"] = time.time()
        G_BACKUP_OK.labels(component=component.name).set(time.time())
        C_ACTIONS.labels(action="backup").inc()
        # ONLY on success, and only after. Evicting first would mean a failed
        # backup leaves fewer snapshots than there were before it ran, which is
        # the one moment you least want to be short of them.
        prune_snapshots(component, container)
    else:
        C_ERRORS.labels(stage="backup").inc()
        log.error("%s: backup failed: %s", component.name, out[:300])


def list_snapshots(component, container):
    """
    [(name, taken_at)] oldest first, or None if the controller cannot answer.

    None and [] mean different things and the caller acts on the difference: []
    is "there are no snapshots", None is "nobody knows", and deleting on the
    strength of a list you failed to read is how retention removes the only copy.
    """
    ok, out = _exec(container, ["pbm", "list", "--out=json"])
    if not ok:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    found = []
    for snap in data.get("snapshots") or []:
        name = snap.get("name")
        if not name:
            continue
        # `restoreTo` is the authority; the name is an ISO timestamp and sorts
        # the same way, so it is a safe fallback when a build omits the field.
        found.append((name, snap.get("restoreTo") or name))
    found.sort(key=lambda row: (row[1], row[0]))
    return found


def prune_snapshots(component, container):
    """
    Delete the oldest snapshots once there are more than the component allows.

    Storage is billed by the gigabyte-month, so a daily snapshot kept forever is
    a bill with no ceiling — but retention is also the only thing here that
    deletes a backup, so every step refuses rather than guesses: an unreadable
    list prunes nothing, a limit of zero or less prunes nothing, and a failed
    delete stops the run instead of moving on to the next one.

    Mongo only. Redis's backup controller is not rendered yet, and inventing a
    prune for a service that does not exist would be inventing its interface too.
    """
    keep = getattr(component, "max_snapshots", 0)
    if component.kind != "mongo" or keep <= 0:
        return
    snapshots = list_snapshots(component, container)
    if snapshots is None:
        say_once((component.name, "nosnaplist"),
                 "%s: cannot read the snapshot list, so nothing is being pruned. "
                 "Retention is not running for this component.", component.name)
        return
    excess = len(snapshots) - keep
    if excess <= 0:
        return
    for name, _when in snapshots[:excess]:
        if DRY_RUN:
            log.info("[dry-run] would delete snapshot %s of %s", name, component.name)
            continue
        ok, out = _exec(container, ["pbm", "delete-backup", "--force", name])
        if not ok:
            C_ERRORS.labels(stage="prune").inc()
            log.error("%s: could not delete snapshot %s: %s",
                      component.name, name, out[:300])
            return
        C_ACTIONS.labels(action="prune").inc()
        log.info("%s: deleted snapshot %s (keeping %d)",
                 component.name, name, keep)


def _local_container(service_name):
    try:
        out = dkr.containers.list(filters={
            "label": f"com.docker.swarm.service.name={service_name}",
            "status": "running"})
        return out[0] if out else None
    except Exception:                                            # noqa: BLE001
        return None


def _exec(container, argv, timeout=1800):
    try:
        result = container.exec_run(argv, demux=False)
        return result.exit_code == 0, (result.output or b"").decode(errors="replace")
    except Exception as exc:                                     # noqa: BLE001
        return False, str(exc)


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------

_syncing = None         # the ONE component allowed an initial sync right now


def apply(component, engine, action, topology):
    """
    Carry out one action. Returns True if the cluster changed.

    Every branch that touches the replica set re-checks the arithmetic against
    the topology it was handed, because the topology can change between deciding
    and acting — a member can go down in those seconds, and the removal that was
    safe when it was chosen is not safe now.
    """
    global _syncing
    verb = action.verb

    if verb == "hold":
        return False

    if verb in ("at_ceiling", "cannot_help"):
        C_REFUSED.labels(reason=verb).inc()
        say_once((component.name, verb, getattr(action, "reason", "")),
                 "%s: %s", component.name,
                 getattr(action, "reason", None)
                 or f"at the {action.limit}-member ceiling; nothing more will be added")
        return False

    if verb == "enfranchise":
        engine.enfranchise(action.host)
        C_ACTIONS.labels(action="enfranchise").inc()
        _syncing = None
        return True

    if verb in ("hide", "unhide"):
        if not component.secondary_reads:
            return False
        engine.set_hidden(action.host, verb == "hide")
        C_ACTIONS.labels(action=verb).inc()
        log.info("%s: %s is %s reads (lag %.1fs, budget %.1fs)", component.name,
                 action.host, "out of" if verb == "hide" else "back in",
                 getattr(action, "lag", 0.0) or 0.0, component.lag_budget_seconds)
        return True

    if verb == "stepdown":
        engine.step_down(action.host)
        C_ACTIONS.labels(action="stepdown").inc()
        return True

    if verb == "promote":
        engine.promote(action.host)
        C_ACTIONS.labels(action="promote").inc()
        log.info("%s: %s should take over as primary — %s", component.name,
                 action.host, getattr(action, "reason", ""))
        return True

    if verb == "provision":
        index = action.index
        on_master = getattr(action, "on_master", False)
        lease = component.lease(index)
        if on_master:
            start_member(component, index, None)
        else:
            hold_lease(lease, action.node_type, f"{component.name} member {index}")
            sync_leases()
            if not lease_ready(lease):
                say_once((component.name, "waiting", lease),
                         "%s: waiting for a machine for member %d (%s)",
                         component.name, index, action.reason)
                return False
            start_member(component, index, _lease_nodes[lease]["hostname"])
        # The member is running but not in the set yet. Adding it is what starts
        # the initial sync, and only one of those may run in the whole cluster.
        _syncing = component.name
        engine.add_member(component.member_host(index))
        C_ACTIONS.labels(action="provision").inc()
        return True

    if verb == "remove":
        if plan.would_break_majority(topology, action.host, engine.VOTING_MEMBERS):
            C_REFUSED.labels(reason="would_break_majority").inc()
            log.warning("%s: refusing to remove %s — the set would lose its majority",
                        component.name, action.host)
            return False
        if topology.primary and topology.primary.host == action.host:
            # Never remove the primary. Step it down and come back next loop:
            # the election is seconds, and doing both in one go means removing a
            # member whose replacement has not been agreed yet.
            engine.step_down(action.host)
            C_ACTIONS.labels(action="stepdown").inc()
            return True
        engine.remove_member(action.host)
        index = index_of_host(component, action.host)
        if index is not None:
            stop_member(component, index)
            release_lease(component.lease(index))
        C_ACTIONS.labels(action="remove").inc()
        return True

    log.warning("%s: no idea how to %s", component.name, verb)
    return False


#: Node pressure, read from node-exporter by hostname. DISK is the one that
#: matters most here and the one a container's own metrics cannot see: cgroup
#: accounting says nothing about the filesystem underneath it, and a full disk
#: on a database is not a recoverable mistake the way CPU is.
_NODE_CPU = ('100 - (avg by (instance) (rate(node_cpu_seconds_total'
             '{{mode="idle", instance="{host}"}}[5m])) * 100)')
_NODE_MEM = ('100 * (1 - node_memory_MemAvailable_bytes{{instance="{host}"}}'
             ' / node_memory_MemTotal_bytes{{instance="{host}"}})')
_NODE_DISK = ('100 * (1 - node_filesystem_avail_bytes'
              '{{instance="{host}", mountpoint="/"}}'
              ' / node_filesystem_size_bytes{{instance="{host}", mountpoint="/"}})')


def node_usage_by_component(components):
    """
    How full each component's own machine is, as a percentage of each resource.

    Read from the NODE rather than from the container. A member that has been
    moved onto a machine of its own is the only thing on it, so the node's
    numbers are the member's — and they include the disk, which is the reading
    the whole down-scale path is gated on.

    An unmeasured node is ABSENT from the result, never zero. Reading a missing
    series as "empty" would tell the loop a machine has room it does not have.
    """
    out = {}
    for component in components.values():
        hosts = [node.get("hostname") for lease, node in _lease_nodes.items()
                 if lease.startswith(f"{component.name}/") and node.get("hostname")]
        if not hosts:
            continue
        usage = {}
        for key, expr in (("cpu", _NODE_CPU), ("memory", _NODE_MEM),
                          ("disk", _NODE_DISK)):
            readings = [query.vm_query(expr.format(host=host)) for host in hosts]
            readings = [r for r in readings if r is not None]
            if readings:
                # The WORST machine, not the average. One member out of disk is
                # the problem whatever the others are doing.
                usage[key] = max(readings)
        if usage:
            out[component.name] = usage
    return out


def member_sizes(component):
    """
    {member host: (cores, memory bytes)} for every member on a leased machine.

    The member on the MASTER is deliberately absent rather than measured. Its
    machine runs the control plane, the panel and every unmanaged component, so
    its size says nothing about how much of it this database may have — and an
    upgrade comparison that treated it as a candidate would try to "promote"
    its way onto the box the whole cluster is run from.
    """
    out = {}
    prefix = f"{component.name}/"
    for lease, node in _lease_nodes.items():
        if not lease.startswith(prefix):
            continue
        try:
            index = int(lease[len(prefix):])
        except ValueError:
            continue
        cores, memory = node.get("cpu"), node.get("memory_bytes")
        if cores and memory:
            out[component.member_host(index)] = (cores, memory)
    return out


def component_at_max_plan(component):
    """
    Has the overseer already given this database the biggest machine it may have?

    Reported per lease and identical across a component's leases, so any one of
    them answers. A component with no leases at all is on the master and has the
    whole ladder ahead of it, which is not the same as being at the top of it.
    """
    prefix = f"{component.name}/"
    flags = [node.get("at_max_plan") for lease, node in _lease_nodes.items()
             if lease.startswith(prefix) and "at_max_plan" in node]
    return bool(flags) and all(flags)


def loop():
    global _syncing
    components = managed_components()
    G_MANAGED.set(len(components))
    if not components:
        sync_leases()
        return {}

    dispatched = dispatched_targets()
    sync_leases()
    usage = node_usage_by_component(components)
    now = time.time()
    results = {}

    for name, component in sorted(components.items()):
        try:
            ensure_tls(component)
            engine = engine_for(component)
            topology = engine.topology()
        except Exception as exc:                                 # noqa: BLE001
            C_ERRORS.labels(stage="engine").inc()
            log.warning("%s: cannot read its topology: %s", name, exc)
            continue

        state = plan.current_state(component, topology)
        _publish(component, topology, state)
        if hasattr(engine, "collection_stats"):
            data_bytes, _storage = engine.collection_stats()
            if data_bytes is not None:
                G_DATA.labels(component=name).set(data_bytes)

        gates = plan.refusals(
            component, topology, now=now, backup_age=backup_age(component, now),
            syncing_elsewhere=_syncing not in (None, name))
        pressure = read_pressure(component, topology, dispatched, usage, now,
                                 engine=engine)
        action = plan.next_action(component, state, topology, pressure,
                                  engine.VOTING_MEMBERS,
                                  sizes=member_sizes(component),
                                  at_max_plan=component_at_max_plan(component))
        results[name] = (state, action, gates)

        # The lag actions are corrections, not transitions: they take a stale
        # member out of read rotation and put it back. Holding them behind a
        # four-hour cooldown would mean serving stale reads for four hours.
        transition = action.verb not in ("hold", "hide", "unhide", "at_ceiling",
                                         "cannot_help")
        if transition and gates:
            for reason in gates:
                C_REFUSED.labels(reason=reason).inc()
            say_once((name, "gated", ",".join(gates), action.verb),
                     "%s: would %s (%s) but %s", name, action.verb,
                     getattr(action, "reason", ""), ", ".join(gates))
            continue

        try:
            if apply(component, engine, action, topology) and transition:
                component.last_change_at = now
                _last_change[name] = now
        except (engines.Unavailable, engines.Refused) as exc:
            C_ERRORS.labels(stage="apply").inc()
            log.error("%s: %s was refused: %s", name, action.verb, exc)
        except Exception as exc:                                 # noqa: BLE001
            C_ERRORS.labels(stage="apply").inc()
            log.exception("%s: %s failed: %s", name, action.verb, exc)

    _stop_idle_viewers(components)
    return results


_last_change = {}


def _publish(component, topology, state):
    for value in (plan.STATE_MASTER, plan.STATE_PAIRED, plan.STATE_DEDICATED):
        G_STATE.labels(component=component.name, state=str(value)).set(
            1 if state == value else 0)
    counts = {}
    states = (engines.PRIMARY, engines.SECONDARY, engines.STARTUP, engines.DOWN)
    for member in topology.members:
        counts[member.state] = counts.get(member.state, 0) + 1
        name = member.host.split(":")[0]
        if member.lag_seconds is not None:
            G_LAG.labels(component=component.name, member=name).set(member.lag_seconds)
        for label in states:
            G_MEMBER.labels(component=component.name, member=name,
                            state=label).set(1 if member.state == label else 0)
    for label in states:
        G_MEMBERS.labels(component=component.name, state=label).set(counts.get(label, 0))
    G_CHANGE.labels(component=component.name).set(
        1 if any(m.state == engines.STARTUP for m in topology.members) else 0)


def _stop_idle_viewers(components):
    """
    Scale a data visualiser back to zero once nobody is looking at it.

    The viewer is full access to the database with no password of its own. It is
    never published, only proxied through the panel session — and the shortest
    time it exists, the smaller that surface is. The panel records the last time
    somebody opened it; this is the half that puts it away.
    """
    for component in components.values():
        if not component.viewer:
            continue
        stamp = os.path.join(STATE_DIR, "viewer", f"{component.name}.seen")
        try:
            last = os.path.getmtime(stamp)
        except OSError:
            continue
        if time.time() - last < VIEWER_IDLE_SECONDS:
            continue
        service = f"{component.name}_viewer"
        try:
            live = dkr.services.get(service)
        except Exception:                                        # noqa: BLE001
            continue
        mode = (live.attrs.get("Spec", {}).get("Mode") or {}).get("Replicated") or {}
        if mode.get("Replicas", 0) == 0:
            continue
        try:
            _service_update(service, "--replicas", "0")
            log.info("%s: the data visualiser has been idle for %ds; stopped",
                     component.name, VIEWER_IDLE_SECONDS)
        except Exception as exc:                                 # noqa: BLE001
            log.warning("could not stop %s: %s", service, exc)


def _stop_signal(signum, _frame):
    global _running
    log.info("received signal %s, finishing", signum)
    _running = False


def main():
    signal.signal(signal.SIGTERM, _stop_signal)
    signal.signal(signal.SIGINT, _stop_signal)
    # BOTH listeners on the monitoring address, for the same reason and resolved
    # once. The careful argument in `monitoring_address` was being made about
    # :9202 while :9203 sat on 0.0.0.0 one line above it — so every application
    # container on `edge` could read which databases exist, how far behind their
    # replicas are, when each was last backed up and whether a restore is
    # running. Read-only, and a map of exactly which database is worth attacking
    # and when it is least able to cope.
    address = monitoring_address()
    start_http_server(METRICS_PORT, addr=address)
    serve_signals(address)
    log.info("dataguard up — cluster=%s enabled=%s dry_run=%s", CLUSTER, ENABLED, DRY_RUN)
    log.info("it manages what is DISCOVERED: any service labelled %s=%s, with its "
             "policy on its own dataguard.* labels. Nothing here names a component.",
             workloads.MANAGED_BY_LABEL, workloads.MANAGED_BY_DATAGUARD)
    log.info("topology changes are gated on a verified backup, a healthy majority, "
             "an odd voting set, disk headroom and a %dh cooldown — and every "
             "refusal is counted as dataguard_refused_total{reason}.",
             TOPOLOGY_COOLDOWN_SECONDS // 3600)
    while _running:
        started = time.time()
        try:
            loop()
            G_LOOP.set(time.time())
        except Exception as exc:                                 # noqa: BLE001
            C_ERRORS.labels(stage="loop").inc()
            log.exception("loop failed: %s", exc)
        time.sleep(max(1.0, LOOP_SECONDS - (time.time() - started)))
    log.info("exiting cleanly")


if __name__ == "__main__":
    main()
