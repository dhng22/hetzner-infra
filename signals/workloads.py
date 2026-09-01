"""
What a workload IS, what it costs, and what policy it carries.

Extracted when the fleet moved out of the autoscaler and into the overseer, for
exactly the reason `signals/query.py` was extracted before it: two processes now
have to agree, and two copies of `policy_from_labels` in two images is a policy
fixed in one and not the other, with nothing failing while they disagree. The
overseer reads a workload's reservation to pack the fleet; the autoscaler reads
the same workload's bounds to choose a replica count. If those two ever parsed
`autoscale.max_replicas` differently, the overseer would buy a machine for
replicas the autoscaler was never going to create.

Stdlib only, and no docker client of its own. Each process lists services with
its own client and hands the raw label/spec dictionaries in — so this module has
no I/O, no registry and nothing to mock.

Errors and complaints leave through HOOKS rather than a counter declared here,
because each process owns its own Prometheus registry and a metric defined in
shared code would belong to neither. Both default to no-ops so this is usable
from a test or a script with no wiring at all.
"""

import math
import re
from collections import namedtuple

from . import classify

#: Called with a stage name when something is dropped or defaulted. Replaced by
#: the importing process with its own counter.
on_error = lambda stage: None                      # noqa: E731
#: Called with (key, message, *args) to log a complaint about operator input
#: once per distinct value. Replaced by the importing process with its own
#: `warn_once`; a no-op here.
on_warn = lambda key, message, *args: None         # noqa: E731


# ---------------------------------------------------------------------------
# labels: the contract with whatever created the component
# ---------------------------------------------------------------------------

WORKLOAD_LABEL = "infra.workload"
WORKLOAD_APP = "app"
COMPONENT_LABEL = "infra.component"

#: Set by a component whose placement was chosen by hand in the panel. Read off
#: the live service, because neither process ever sees a spec file.
PLACEMENT_PIN_LABEL = "infra.placement.pinned"

#: Set by the renderer of any component whose shape belongs to dataguard.
#:
#: This is a DATA-LOSS guard, not a tidiness one. A database member service is
#: one mongod with one volume; scaling it to two replicas starts a second mongod
#: on the same data directory, which is corruption rather than an outage. These
#: services already carry no `infra.workload=app` label and so are already
#: invisible to discovery — this exists so that a mislabelled one is refused
#: explicitly instead of relying on an absence.
MANAGED_BY_LABEL = "infra.managed_by"
MANAGED_BY_DATAGUARD = "dataguard"

WORKER_CONSTRAINT = "node.role==worker"
# Swarm normalises whitespace differently across versions, so recognise any
# spelling rather than only ours.
_WORKER_PIN = re.compile(r"^\s*node\.role\s*==\s*worker\s*$")

MODE_MANAGER = "manager"
MODE_WORKER = "worker"


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


def reservations(spec_resources):
    res = (spec_resources or {}).get("Reservations") or {}
    return Res(int(res.get("NanoCPUs", 0) or 0), int(res.get("MemoryBytes", 0) or 0))


def limits(spec_resources):
    lim = (spec_resources or {}).get("Limits") or {}
    return Res(int(lim.get("NanoCPUs", 0) or 0), int(lim.get("MemoryBytes", 0) or 0))


def node_resources(node):
    res = node.attrs.get("Description", {}).get("Resources", {}) or {}
    return Res(int(res.get("NanoCPUs", 0) or 0), int(res.get("MemoryBytes", 0) or 0))


# ---------------------------------------------------------------------------
# policy, read from the service's own deploy labels
# ---------------------------------------------------------------------------

def _label_num(labels, key, default, cast, lo, hi, service):
    raw = labels.get(key)
    if raw is None:
        return default
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        on_error("policy")
        on_warn((service, key, raw), "%s: %s=%r is not a number; using %s",
                service, key, raw, default)
        return default
    if not (lo <= value <= hi):
        on_error("policy")
        on_warn((service, key, raw), "%s: %s=%s is outside %s..%s; using %s",
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
    # Latency is a SYMPTOM, and these decide whether it is this service's
    # symptom. A replica that is not working hard is not the reason a request
    # was slow — something it called was — and adding replicas then aims more
    # concurrency at the thing that is already struggling.
    "busy_cpu", "busy_mem",
    # Memory as a first-class trigger, not just a gate. CPU is compressible and
    # memory is not: a replica near its limit is one allocation from an OOM
    # kill, which no amount of latency headroom warns you about.
    "up_mem", "down_mem",
    # The most of itself a service may shed in one step, as `up_factor` is the
    # most it may add. Scaling down used to be a flat -1, which took eighteen
    # cooldowns to walk 20 replicas back to 2 and cost money the whole way.
    "down_factor",
    # How long a SMALLER count has to keep being the answer before it is acted
    # on. Kubernetes calls this `behavior.scaleDown.stabilizationWindowSeconds`;
    # it damps the recommendation, where `sustain_down` damps the signal.
    "stabilize_down",
])


#: The most of itself a service may shed in one step. Symmetric with
#: `up_factor`'s default on purpose — the ASYMMETRY that keeps scaling
#: conservative lives in the windows (90s up, 900s down) and in the stabilizer
#: below, not in the size of the step. Halving is also provably safe against
#: CPU: a service may only shrink when its peak per-replica CPU is under
#: `down_cpu` (30%), so after halving it is near 60%, still under `up_cpu` (70%).
DOWN_FACTOR = 0.5

#: How long a smaller count has to keep being the answer. Kubernetes' default
#: for the same idea is 300s and this is the same number for the same reason:
#: long enough that one quiet minute does not shed capacity, short enough that
#: an idle evening is not paid for. 0 turns it off.
STABILIZE_DOWN = 300

#: How busy a replica has to be before a latency breach counts as ITS problem.
#: Imported rather than restated: the overseer reads the same two numbers off
#: the same service.
_BUSY_CPU = classify.BUSY_CPU
_BUSY_MEM = classify.BUSY_MEM
#: Memory's own thresholds. Higher than CPU's on purpose — a JVM sits near its
#: heap ceiling by design, so only the top of the range means trouble.
_UP_MEM = 85.0
_DOWN_MEM = 60.0

def _thresholds_from_labels(labels, service_name):
    """
    The performance half of a policy: what "slow" and "busy" mean here.

    Read for EVERY service, autoscaled or not. A fixed-replica application is
    still measured, still has a verdict published about it, and still has an
    opinion about its own SLO — turning autoscaling off means "do not change the
    count", not "stop believing my thresholds". Parsing these only in the
    enabled branch quietly gave every fixed component the defaults.
    """
    up_ratio = _label_num(labels, "autoscale.up_p95_ratio", 0.8, float, 0.01, 2.0, service_name)
    down_ratio = _label_num(labels, "autoscale.down_p95_ratio", 0.4, float, 0.01, 2.0, service_name)
    up_cpu = _label_num(labels, "autoscale.up_cpu_pct", 70.0, float, 1.0, 200.0, service_name)
    down_cpu = _label_num(labels, "autoscale.down_cpu_pct", 30.0, float, 1.0, 200.0, service_name)
    # Repairing only one side would produce a config nobody wrote, so a crossed
    # pair reverts both.
    if down_ratio >= up_ratio:
        on_warn((service_name, "ratios", f"{up_ratio}/{down_ratio}"),
                "%s: scale-down p95 ratio %.2f is not below scale-up %.2f; "
                "using the defaults for both", service_name, down_ratio, up_ratio)
        up_ratio, down_ratio = 0.8, 0.4
    if down_cpu >= up_cpu:
        on_warn((service_name, "cpus", f"{up_cpu}/{down_cpu}"),
                "%s: scale-down CPU %.0f%% is not below scale-up %.0f%%; "
                "using the defaults for both", service_name, down_cpu, up_cpu)
        up_cpu, down_cpu = 70.0, 30.0

    up_mem = _label_num(labels, "autoscale.up_mem_pct", _UP_MEM, float, 1.0, 100.0, service_name)
    down_mem = _label_num(labels, "autoscale.down_mem_pct", _DOWN_MEM, float, 1.0, 100.0, service_name)
    if down_mem >= up_mem:
        on_warn((service_name, "mems", f"{up_mem}/{down_mem}"),
                "%s: scale-down memory %.0f%% is not below scale-up %.0f%%; "
                "using the defaults for both", service_name, down_mem, up_mem)
        up_mem, down_mem = _UP_MEM, _DOWN_MEM

    # The saturation floors. Capped at the scale-up thresholds, because a floor
    # ABOVE the trigger is a service that can never scale on latency at all — a
    # footgun that reads as "stricter" and means "off".
    busy_cpu = _label_num(labels, "autoscale.busy_cpu_pct", _BUSY_CPU, float, 0.0, 200.0, service_name)
    busy_mem = _label_num(labels, "autoscale.busy_mem_pct", _BUSY_MEM, float, 0.0, 100.0, service_name)
    busy_cpu, busy_mem = min(busy_cpu, up_cpu), min(busy_mem, up_mem)

    sustain_up = _label_num(labels, "autoscale.sustain_up_seconds", 90, int, 30, 3600, service_name)
    sustain_down = _label_num(labels, "autoscale.sustain_down_seconds", 900, int, 60, 86400, service_name)
    if sustain_down < sustain_up:
        on_warn((service_name, "sustain", f"{sustain_up}/{sustain_down}"),
                "%s: sustain_down %ds is shorter than sustain_up %ds, which "
                "inverts the up-fast/down-slow asymmetry this loop relies on",
                service_name, sustain_down, sustain_up)

    slo = _label_num(labels, "autoscale.slo_p95_ms", 500.0, float, 1.0, 600000.0, service_name)

    # No default metric name any more. The old one was
    # `http_server_requests_seconds_bucket`, a Spring convention that silently
    # matched nothing for every other framework — and an empty
    # histogram_quantile is indistinguishable from an idle service, so it never
    # looked wrong. Absent this label the metric is DISCOVERED from what the
    # service actually publishes; see signals.discovery.
    histogram = labels.get("autoscale.p95_histogram") or ""
    histogram_explicit = bool(histogram)
    if histogram and not re.match(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$", histogram):
        on_warn((service_name, "metric", histogram),
                "%s: %r is not a metric name; discovering one instead",
                service_name, histogram)
        histogram, histogram_explicit = "", False
    unit = labels.get("autoscale.p95_unit", "seconds")
    if unit not in ("seconds", "milliseconds"):
        unit = "seconds"

    return dict(slo_ms=slo, up_ratio=up_ratio, down_ratio=down_ratio,
                up_cpu=up_cpu, down_cpu=down_cpu, up_mem=up_mem, down_mem=down_mem,
                busy_cpu=busy_cpu, busy_mem=busy_mem,
                sustain_up=sustain_up, sustain_down=sustain_down,
                histogram=histogram, unit=unit,
                histogram_explicit=histogram_explicit)


def fixed_policy(spec_replicas, labels=None, service_name=""):
    """A service that is discovered and placed but never scaled."""
    fixed = max(0, int(spec_replicas or 0))
    t = _thresholds_from_labels(labels or {}, service_name)
    return Policy(False, fixed, fixed, t["slo_ms"], t["up_ratio"], t["down_ratio"],
                  t["up_cpu"], t["down_cpu"], t["sustain_up"], t["sustain_down"],
                  0.5, 60, 100, t["histogram"], t["unit"], t["histogram_explicit"],
                  t["busy_cpu"], t["busy_mem"], t["up_mem"], t["down_mem"],
                  DOWN_FACTOR, STABILIZE_DOWN)


def policy_from_labels(service_name, labels, spec_replicas):
    """
    A service's scaling policy, read from its own deploy labels.

    NEVER RAISES. This is the boundary between operator input and two control
    loops, so every field falls back to a default and complains rather than
    taking the cluster down with a typo.

    A service with `infra.workload=app` and no `autoscale.enabled` is a
    fixed-replica application: still discovered, still pinned with the others,
    still counted in demand, never scaled. Its bounds are its live replica count
    read fresh each loop, so scaling it by hand is respected rather than fought.
    """
    try:
        t = _thresholds_from_labels(labels, service_name)
        if not _label_bool(labels, "autoscale.enabled", False):
            return fixed_policy(spec_replicas, labels, service_name)

        lo = _label_num(labels, "autoscale.min_replicas", 1, int, 0, 100, service_name)
        hi = _label_num(labels, "autoscale.max_replicas", lo, int, 0, 100, service_name)
        if hi < lo:
            on_warn((service_name, "bounds", f"{lo}-{hi}"),
                    "%s: max_replicas %d is below min_replicas %d; using %d for both",
                    service_name, hi, lo, lo)
            hi = lo
        if hi == lo:
            on_warn((service_name, "noop", f"{lo}"),
                    "%s: autoscaling is on but min == max == %d, so nothing can "
                    "move. Set a range, or turn autoscaling off.", service_name, lo)

        return Policy(
            True, lo, hi, t["slo_ms"], t["up_ratio"], t["down_ratio"],
            t["up_cpu"], t["down_cpu"], t["sustain_up"], t["sustain_down"],
            _label_num(labels, "autoscale.up_factor", 0.5, float, 0.01, 4.0, service_name),
            _label_num(labels, "autoscale.cooldown_seconds", 60, int, 0, 3600, service_name),
            _label_num(labels, "autoscale.priority", 100, int, 0, 1000, service_name),
            t["histogram"], t["unit"], t["histogram_explicit"],
            t["busy_cpu"], t["busy_mem"], t["up_mem"], t["down_mem"],
            _label_num(labels, "autoscale.down_factor", DOWN_FACTOR, float,
                       0.01, 1.0, service_name),
            _label_num(labels, "autoscale.stabilize_down_seconds", STABILIZE_DOWN,
                       int, 0, 3600, service_name),
        )
    except Exception as exc:  # noqa: BLE001
        on_error("policy")
        on_warn((service_name, "unreadable", str(exc)),
                "%s: could not read the scaling policy (%s); treating it as fixed",
                service_name, exc)
        return fixed_policy(spec_replicas)


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------

Workload = namedtuple("Workload", [
    "name", "id", "policy", "spec_replicas", "cost", "cpu_limit", "mem_limit",
    "pinned", "rolling", "component", "rolled_back", "placement_pinned",
    # Causes the operator has said nobody will ever fix, parsed from the same
    # label the overseer mutes on. Carried here so a service's whole contract
    # with the platform is read once, in one place.
    "muted",
])


def thresholds(policy):
    """
    The same service's numbers, in the shape `signals.classify` reads.

    `Policy` and `classify.Thresholds` describe overlapping halves of one label
    set, and for a while each was parsed by its own function — which is two
    readers of `autoscale.up_cpu_pct` that can be fixed separately. This is the
    projection instead: parsed once as a Policy, viewed as Thresholds.
    """
    return classify.Thresholds(
        slo_ms=policy.slo_ms, up_ratio=policy.up_ratio, down_ratio=policy.down_ratio,
        up_cpu=policy.up_cpu, down_cpu=policy.down_cpu,
        up_mem=policy.up_mem, down_mem=policy.down_mem,
        busy_cpu=policy.busy_cpu, busy_mem=policy.busy_mem,
        sustain_up=policy.sustain_up, sustain_down=policy.sustain_down,
    )


def constraints(service):
    try:
        return (service.attrs["Spec"]["TaskTemplate"].get("Placement", {})
                .get("Constraints") or [])
    except (KeyError, TypeError):
        return []


def is_pinned(service):
    return any(_WORKER_PIN.match(c) for c in constraints(service))


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


def managed_by_dataguard(service):
    """
    True for a service whose shape belongs to dataguard.

    Checked by the autoscaler before it changes a replica count or a placement
    constraint. See MANAGED_BY_LABEL for why that is not paranoia.
    """
    labels = (service.attrs.get("Spec", {}) or {}).get("Labels") or {}
    return labels.get(MANAGED_BY_LABEL) == MANAGED_BY_DATAGUARD


def workload_from_service(service):
    """
    One Workload, or None if this service is not an application.

    Raises nothing the caller has to handle beyond its own guard: a service that
    cannot be read is the caller's decision to skip, because "skip one" and
    "give up on the loop" are different answers and only the caller knows which
    it is.
    """
    spec = service.attrs.get("Spec", {})
    labels = spec.get("Labels") or {}
    if labels.get(WORKLOAD_LABEL) != WORKLOAD_APP:
        return None
    if labels.get(MANAGED_BY_LABEL) == MANAGED_BY_DATAGUARD:
        # BOTH labels is a contradiction, and it is resolved here — at the point
        # where a service becomes something the autoscaler may act on — rather
        # than at each of the three places that act on one.
        #
        # It used to be resolved at one of them. The replica path refused, and
        # placement and right-sizing did not: a mislabelled member kept its
        # replica count and had its node constraint rewritten, which moves a
        # mongod to a machine where its volume does not exist, and had its
        # reservations rewritten, which restarts it. The first of those loses
        # the data. Being absent from the list is the only guard that covers
        # every path including the next one somebody adds.
        on_error("policy")
        on_warn((service.name, "managed"),
                "%s carries %s=%s AND %s=%s, which cannot both be true. Treating "
                "it as a database and leaving it alone: this loop scales by "
                "replica count and moves services between machines, and a "
                "database survives neither. Remove one of the two labels.",
                service.name, MANAGED_BY_LABEL, MANAGED_BY_DATAGUARD,
                WORKLOAD_LABEL, WORKLOAD_APP)
        return None

    task_tpl = spec.get("TaskTemplate", {}) or {}
    resources = task_tpl.get("Resources")
    replicas = (spec.get("Mode", {}).get("Replicated") or {}).get("Replicas", 0)
    cost = reservations(resources)
    if cost == ZERO:
        on_error("policy")
        on_warn((service.name, "noreservation"),
                "%s carries no resource reservation. Swarm will pack it "
                "anywhere and the capacity model cannot see it; charging "
                "%s so it is at least visible.", service.name, UNRESERVED_FLOOR)
        cost = UNRESERVED_FLOOR
    mounts = (task_tpl.get("ContainerSpec") or {}).get("Mounts") or []
    if any(m.get("Type") == "volume" for m in mounts):
        on_error("policy")
        on_warn((service.name, "volume"),
                "%s is labelled %s=%s but mounts a volume. It will be "
                "moved onto workers that are later deleted, and the data "
                "goes with them. Drop the label, or drop the volume.",
                service.name, WORKLOAD_LABEL, WORKLOAD_APP)
    limit = limits(resources)
    cpu_limit = limit.cores or cost.cores or 1.0
    # Falling back to the RESERVATION is what makes the percentage mean
    # something for a service with no limit set: without it the divisor
    # would be zero and memory would read as absent rather than as
    # "measured against what it asked for".
    mem_limit = limit.mem or cost.mem or 0
    return Workload(
        name=service.name, id=service.id,
        policy=policy_from_labels(service.name, labels, replicas),
        spec_replicas=replicas, cost=cost, cpu_limit=cpu_limit,
        mem_limit=mem_limit,
        pinned=is_pinned(service), rolling=update_in_progress(service),
        component=labels.get(COMPONENT_LABEL, service.name.split("_")[0]),
        rolled_back=update_rolled_back(service),
        placement_pinned=labels.get(PLACEMENT_PIN_LABEL) == "true",
        muted=classify.parse_causes(
            labels.get(classify.MUTE_LABEL),
            on_bad=lambda bad: on_warn(
                (service.name, "badmute", ",".join(bad)),
                "%s: %s names %s, which is not a cause; ignoring it",
                service.name, classify.MUTE_LABEL, ", ".join(bad))),
    )


def discover_workloads(dkr, on_skip=None):
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
        on_error("discovery")
        if on_skip:
            on_skip(None, exc)
        return [], [], False

    workloads = []
    for service in services:
        try:
            workload = workload_from_service(service)
        except Exception as exc:  # noqa: BLE001
            on_error("discovery")
            if on_skip:
                on_skip(service.name, exc)
            continue
        if workload is not None:
            workloads.append(workload)

    workloads.sort(key=lambda w: (w.policy.priority, w.name))
    return workloads, services, True


# ---------------------------------------------------------------------------
# a direction, turned into a count
# ---------------------------------------------------------------------------

def aim(low, high):
    """
    The load a replica should be carrying: the middle of its own band.

    Not the scale-up line and not the scale-down one. A count that lands exactly
    on the up threshold scales up again on the next loop, and one that lands on
    the down threshold scales down again — the only resting point is between
    them, so that is what the arithmetic below aims at. It is derived from the
    service's OWN thresholds rather than being a constant, so a component that
    moves its band moves its target with it and nothing here has to know.
    """
    return (low + high) / 2.0


def _by_ratio(current, value, target):
    """
    How many replicas it would take to bring `value` down to `target`.

    Kubernetes' HPA arithmetic — `ceil(current × metric / target)` — and it is
    here for the reason HPA has it: a fixed step converges at a rate that has
    nothing to do with how far over the line the service actually is. A service
    at four times its target crawled there in several loops of ×1.5.

    Only CPU and memory are ever passed in. LATENCY DELIBERATELY IS NOT: it is
    not linear in replica count and it is not even necessarily caused by this
    service (see `classify.decide`), so a request that took five times the SLO
    is not a request for five times the replicas. Latency still triggers a
    scale-up; it just gets the conservative step rather than a multiplier.
    """
    if value is None or current <= 0 or target <= 0:
        return None
    return int(math.ceil(current * value / target))


def desired_replicas(policy, direction, current, held=None, peak=None):
    """
    A DIRECTION turned into a replica count, within this service's own bounds.

    Pure, and shared, because two processes now need the same answer for
    different reasons: the overseer sizes the fleet from the total of these
    numbers, and the autoscaler writes each of them to Swarm. If the two
    disagreed, the overseer would buy a machine for replicas the autoscaler was
    never going to create — a worker that boots, sits empty, and is deleted
    fifteen minutes later.

    `direction` is None when nothing fresh has been dispatched, and that means
    HOLD. A missing verdict is not "nothing is wrong", it is "nobody has told us
    anything", and the two are only the same when the fleet is allowed to drift
    on a guess.

    `held` and `peak` are the same `(latency_ms, cpu_pct, mem_pct)` triples
    `classify.decide` judged — the sustained minimum over the scale-up window
    and the maximum over the scale-down one. They are OPTIONAL, and when they
    are absent this falls back to the fixed step it used to always take. That
    matters: a missing cadvisor series must produce a cautious number, never an
    arbitrary one computed from a gap.

    The factors are now CAPS rather than the step itself. Nothing scales up
    faster than `up_factor` ever allowed; what changed is that it can scale up
    by less when less is called for, and down by more than one replica when the
    service is plainly idle.
    """
    if not policy.autoscale or not direction:
        return current

    cpu_aim = aim(policy.down_cpu, policy.up_cpu)
    mem_aim = aim(policy.down_mem, policy.up_mem)

    def ratio(reading):
        """
        The count the measurements ask for, or None when there are none.

        `None` and `0` are different answers and the callers below test for the
        first explicitly: a service measured at zero load legitimately wants as
        few replicas as its floor allows, and reading that as "no measurement"
        would quietly give it the one-replica step instead of the shrink it
        earned.
        """
        _lat, cpu, mem = reading or (None, None, None)
        wanted = [n for n in (_by_ratio(current, cpu, cpu_aim),
                              _by_ratio(current, mem, mem_aim)) if n is not None]
        # The MAX, as HPA does across its metrics: the resource that needs the
        # most replicas is the one that decides, because satisfying it satisfies
        # the others and satisfying any other one leaves it still over the line.
        return max(wanted) if wanted else None

    if direction == classify.DIRECTION_UP:
        ceiling = current + max(1, int(current * policy.up_factor))
        wanted = ratio(held)
        # `current + 1` because up means up: a latency breach with modest CPU
        # produces a ratio BELOW the current count, and answering a scale-up
        # with a scale-down would be a control loop arguing with itself.
        step = (max(current + 1, min(ceiling, wanted))
                if wanted is not None else ceiling)
        return min(policy.max_replicas, step)

    if direction == classify.DIRECTION_DOWN and current > policy.min_replicas:
        floor = current - max(1, int(current * policy.down_factor))
        wanted = ratio(peak)
        step = (min(current - 1, max(floor, wanted))
                if wanted is not None else current - 1)
        return max(policy.min_replicas, step)

    return current


class Stabilizer:
    """
    A shrink has to keep being the answer before it is acted on.

    `sustain_down` already damps the SIGNAL — a service must be quiet for the
    whole 900-second window before `decide` will say down at all. This damps the
    RECOMMENDATION, which is a different thing and only became worth having once
    the count above stopped being a flat -1: a metric that oscillates either side
    of the window boundary now produces a count that oscillates with it, and the
    replica count would follow.

    Kubernetes does exactly this (`behavior.scaleDown.stabilizationWindowSeconds`,
    default 300s) and takes the MAXIMUM recommendation over the window when
    scaling down. So does this.

    It is the only state in either control loop that is not re-derived every
    pass, which is a property worth protecting — it is why a restart mid-decision
    is harmless. Two things keep the cost bounded: what is remembered is the RAW
    recommendation and never the damped one, so nothing feeds back on itself;
    and losing the history means shrinking sooner, which is exactly the
    behaviour this replaced. A restart is therefore safe, not merely survivable.
    """

    def __init__(self):
        self._seen = {}                       # name -> [(at, raw_want), ...]

    def stabilise(self, name, want, current, window, now):
        """
        The count to act on, having recorded `want` as the latest raw answer.

        Growing is never delayed — Kubernetes' own scale-up stabilization window
        defaults to zero for the same reason. Being slow to add capacity is an
        outage; being slow to remove it is a bill.
        """
        history = self._seen.setdefault(name, [])
        history.append((now, want))
        if window > 0:
            cutoff = now - window
            del history[:next((i for i, (at, _) in enumerate(history)
                               if at >= cutoff), len(history))]
        else:
            del history[:-1]
        if want >= current:
            return want
        return max(w for _, w in history)

    def forget(self, live_names):
        """Drop services that no longer exist, so this cannot grow forever."""
        for name in [n for n in self._seen if n not in live_names]:
            del self._seen[name]


def bounded(policy, count):
    """A count clamped into the service's own floor and ceiling."""
    return max(policy.min_replicas, min(policy.max_replicas, count))


def worker_pin_constraints(service):
    """
    Every spelling of the worker pin currently on this service.

    `docker service update --constraint-rm` matches the STORED STRING EXACTLY,
    and the stored string is not necessarily ours: the component renderer writes
    `node.role == worker` with spaces, WORKER_CONSTRAINT has none. Removing the
    spaced constraint by its unspaced name silently removed nothing, so the pin
    could never be released and the fleet could never reach zero. Reading the
    live spelling back is what fixes that, and it belongs next to the pattern
    that recognises it.
    """
    return [c for c in constraints(service) if _WORKER_PIN.match(c)]
