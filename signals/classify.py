"""
What a service's numbers MEAN: is it busy, and if it is slow, whose fault is it.

Shared because two processes must reach the same verdict about the same service.
The autoscaler asks "is this mine?" — it has to, because it acts on the answer
inside its own loop and cannot wait for anything else. The overseer asks "then
whose is it?" and routes that to whoever claims it. If they disagreed about what
"busy" means, the autoscaler would refuse to scale something the overseer said
was the service's own fault, and nobody would ever notice.
"""

from collections import namedtuple

#: The named causes a latency breach can be attributed to. `local` is the only
#: one more replicas can fix; the rest are somewhere else, and scaling for them
#: aims more concurrency at whatever is already struggling.
CAUSE_LOCAL = "local"
CAUSE_DATABASE = "database"
CAUSE_UPSTREAM = "upstream"
CAUSE_UNKNOWN = "unknown"
CAUSES = (CAUSE_LOCAL, CAUSE_DATABASE, CAUSE_UPSTREAM, CAUSE_UNKNOWN)

#: A service that carries this label CLAIMS the causes it names, and the
#: overseer hands them over instead of alerting. The whole extension point for
#: a future dbmanager is one label on its own service:
#:
#:     infra.handles=database
#:
#: Discovery by label, never by name — the same rule the autoscaler already
#: follows for workloads, so a second manager needs no edit anywhere here.
HANDLER_LABEL = "infra.handles"

#: Causes nobody will ever fix, named per component so the overseer stops
#: raising them. `upstream:vendor.example` mutes one target; `upstream` mutes all.
MUTE_LABEL = "autoscale.mute_causes"

#: How busy a replica has to be before a latency breach counts as ITS problem.
#: Deliberately well below the scale-up thresholds: this is not "is it
#: saturated", it is "is it doing enough work that the delay could plausibly be
#: its own". At 5% of its CPU limit and 7% of its memory, a replica is waiting.
BUSY_CPU = 25.0
BUSY_MEM = 60.0


def saturated(busy_cpu, busy_mem, cpu_held, mem_held):
    """
    Are the replicas themselves working hard enough to BE the delay?

    Unknown is NOT busy. A missing cadvisor series must not be readable as
    permission to scale on latency alone — that is precisely the state this
    guard exists to refuse.
    """
    return ((cpu_held is not None and cpu_held >= busy_cpu)
            or (mem_held is not None and mem_held >= busy_mem))


def parse_causes(raw, on_bad=None):
    """
    A set of cause names from a comma-separated label. Junk is dropped, and
    reported through `on_bad` rather than raised: this is operator input on the
    boundary of a control loop, and a typo must not take a component out.
    """
    out, bad = set(), []
    for token in (raw or "").replace(";", ",").split(","):
        token = token.strip().lower()
        if not token:
            continue
        # `upstream:vendor.example` names one target; `upstream` names the cause.
        if token.split(":", 1)[0] in CAUSES:
            out.add(token)
        else:
            bad.append(token)
    if bad and on_bad:
        on_bad(bad)
    return frozenset(out)


def verdict(cause, target, muted, claims):
    """
    (handled_by, alert) for an attributed cause.

    Three outcomes, and the middle one is the point of the whole mechanism:
      muted    — the operator has said nobody will ever fix this. Silent.
      claimed  — a manager handles it. Silent HERE; that manager reports.
      neither  — nothing in the cluster owns it, so it has to reach a human.
    """
    if cause in muted or (target and f"{cause}:{target}".lower() in muted):
        return "muted", False
    if cause in claims:
        return "claimed", False
    return None, True


# ---------------------------------------------------------------------------
# the performance verdict
# ---------------------------------------------------------------------------
# THE RULE LIVES HERE, ONCE. The overseer applies it and dispatches the
# result; a manager receives the result and decides what to do about it. Before
# the split the autoscaler applied the rule AND acted on it, which is why it had
# to know what a MongoDB driver timer looked like.

DIRECTION_UP = "up"
DIRECTION_DOWN = "down"
DIRECTION_HOLD = "hold"

#: One service's thresholds, read from its own deploy labels. Everything here is
#: a property of the SERVICE, so it travels with the component rather than being
#: configured anywhere central.
Thresholds = namedtuple("Thresholds", [
    "slo_ms", "up_ratio", "down_ratio",
    "up_cpu", "down_cpu", "up_mem", "down_mem",
    "busy_cpu", "busy_mem", "sustain_up", "sustain_down",
])

DEFAULTS = Thresholds(
    slo_ms=500.0, up_ratio=0.8, down_ratio=0.4,
    up_cpu=70.0, down_cpu=30.0, up_mem=85.0, down_mem=60.0,
    busy_cpu=BUSY_CPU, busy_mem=BUSY_MEM, sustain_up=90, sustain_down=900,
)

_LABELS = {
    "slo_ms": "autoscale.slo_p95_ms", "up_ratio": "autoscale.up_p95_ratio",
    "down_ratio": "autoscale.down_p95_ratio", "up_cpu": "autoscale.up_cpu_pct",
    "down_cpu": "autoscale.down_cpu_pct", "up_mem": "autoscale.up_mem_pct",
    "down_mem": "autoscale.down_mem_pct", "busy_cpu": "autoscale.busy_cpu_pct",
    "busy_mem": "autoscale.busy_mem_pct",
    "sustain_up": "autoscale.sustain_up_seconds",
    "sustain_down": "autoscale.sustain_down_seconds",
}


def thresholds_from_labels(labels, on_bad=None):
    """
    NEVER RAISES. This is the boundary between operator input and a control
    loop, so every field falls back to its default and complains rather than
    taking a component out with a typo.
    """
    values = {}
    for field, key in _LABELS.items():
        default = getattr(DEFAULTS, field)
        raw = labels.get(key)
        try:
            values[field] = type(default)(raw) if raw not in (None, "") else default
        except (TypeError, ValueError):
            values[field] = default
            if on_bad:
                on_bad(key, raw)
    t = Thresholds(**values)

    # Repairing one side of a crossed pair produces a configuration nobody
    # wrote, so a crossed pair reverts BOTH to the defaults.
    fixes = {}
    if t.down_ratio >= t.up_ratio:
        fixes.update(up_ratio=DEFAULTS.up_ratio, down_ratio=DEFAULTS.down_ratio)
    if t.down_cpu >= t.up_cpu:
        fixes.update(up_cpu=DEFAULTS.up_cpu, down_cpu=DEFAULTS.down_cpu)
    if t.down_mem >= t.up_mem:
        fixes.update(up_mem=DEFAULTS.up_mem, down_mem=DEFAULTS.down_mem)
    if fixes:
        if on_bad:
            on_bad("crossed thresholds", ", ".join(sorted(fixes)))
        t = t._replace(**fixes)

    # A busy floor ABOVE the scale-up trigger reads as "stricter" and means
    # "latency can never scale this service at all".
    return t._replace(busy_cpu=min(t.busy_cpu, t.up_cpu),
                      busy_mem=min(t.busy_mem, t.up_mem))


def decide(t, held, peak):
    """
    (direction, reason) from one service's numbers. `held` is the sustained
    minimum over the scale-up window, `peak` the maximum over the scale-down
    one; each is (latency_ms, cpu_pct, mem_pct) and any of them may be None.

    Latency is a SYMPTOM, and more replicas fix exactly one cause of it — these
    replicas being the bottleneck. A slow third-party API, a throttled database
    or a cold JVM all raise latency while the replicas sit idle, and scaling
    then aims more concurrency at whatever is already struggling. That cost a
    real scale-up: one person testing an Android client made four calls, one
    took 904ms, and with no other traffic in the 2-minute rate window that ONE
    request was the service's mean latency for long enough to satisfy the
    90-second sustain check. CPU was 11%. The cluster grew 2 -> 3 -> 4 replicas
    for a single user and drained back half an hour later.

    So latency may only push UP when the replicas are busy. CPU and memory each
    remain triggers in their own right, because those are local by construction.
    """
    lat_held, cpu_held, mem_held = held
    lat_peak, cpu_peak, mem_peak = peak
    up_ms, down_ms = t.slo_ms * t.up_ratio, t.slo_ms * t.down_ratio

    breaching = lat_held is not None and lat_held > up_ms
    local = saturated(t.busy_cpu, t.busy_mem, cpu_held, mem_held)

    reasons = []
    if breaching and local:
        reasons.append(f"latency held above {up_ms:.0f}ms ({lat_held:.0f}ms) with replicas busy")
    if cpu_held is not None and cpu_held > t.up_cpu:
        reasons.append(f"cpu/replica held above {t.up_cpu:.0f}% ({cpu_held:.0f}%)")
    if mem_held is not None and mem_held > t.up_mem:
        reasons.append(f"memory/replica held above {t.up_mem:.0f}% ({mem_held:.0f}%)")
    if reasons:
        return DIRECTION_UP, "; ".join(reasons)

    # Down only when EVERY signal has stayed low for the whole window.
    # `quiet_cpu` requires a non-None peak on purpose: a missing cadvisor series
    # must hold the count, not authorise shrinking it. Memory may be unknown,
    # because a service with no memory limit would otherwise never shrink.
    quiet_latency = lat_peak is None or lat_peak < down_ms
    quiet_cpu = cpu_peak is not None and cpu_peak < t.down_cpu
    quiet_mem = mem_peak is None or mem_peak < t.down_mem
    if quiet_latency and quiet_cpu and quiet_mem:
        seen = f"{lat_peak:.0f}ms" if lat_peak is not None else "no traffic"
        return DIRECTION_DOWN, (f"quiet for the whole window (latency peak {seen}, "
                                f"cpu/replica peak {cpu_peak:.0f}%)")

    if breaching and not local:
        return DIRECTION_HOLD, (f"latency held at {lat_held:.0f}ms but the replicas are "
                                f"idle — more of them cannot fix this")
    return DIRECTION_HOLD, ""
