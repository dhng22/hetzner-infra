"""
Shapes shared by the live data module and the preview fixtures.

`swarm.py` and `fixtures.py` are swapped for each other at import time, so
anything computed in one and not the other is a drift waiting to happen — the
preview showed an all-missing component as healthy for exactly that reason, and
`history()` existed only in the fixtures, so the live panel 500'd on the page
you would use most.

Anything that is pure derivation from a service dict belongs here, taking the
`service()` lookup as an argument. Only the part that actually talks to Docker
stays split.
"""

import re


#: Which of the map's categorical hues an image tag gets on a component's Map
#: tab. The same five the cluster map already uses, in the same order — they are
#: validated against both surfaces and for colour-vision deficiency, and a
#: second palette invented for one tab would not be.
TAG_KEYS = ("prod", "staging", "data", "observe", "platform")

#: A Swarm node label. Docker accepts more than this, but a key with a space or
#: a comma in it cannot be written as a placement constraint, so it would be a
#: label you can set and never use.
_LABEL_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")


def short_image(image):
    """`ghcr.io/you/app:sha-abc@sha256:...` -> `app:sha-abc`."""
    if not image:
        return "—"
    return image.split("@")[0].rsplit("/", 1)[-1]


def same_image(a, b):
    """
    Whether two references name the same image, ignoring any pinned digest.

    Swarm PINS a digest onto every image it can resolve against a registry, so a
    running service reads `ghcr.io/you/app:main-9db4e08@sha256:ab1b…` while the
    deploy that asked for it recorded `ghcr.io/you/app:main-9db4e08`. Plain
    equality therefore calls an image that has been serving for days "not live
    yet", and does it only for registry-backed images — which is every real
    application. The two locally-built infrastructure images never get a digest
    (nothing pushes them), compared equal, and hid this.
    """
    if not a or not b:
        return False
    return a.split("@")[0] == b.split("@")[0]


def image_tag(image):
    """
    `ghcr.io/you/app:sha-abc@sha256:...` -> `sha-abc`.

    The tag alone, because on the Map tab every block belongs to the same
    component and the repository half is the same on all of them — the four
    characters that differ are the whole signal.
    """
    if not image:
        return "—"
    tail = image.split("@")[0].rsplit("/", 1)[-1]
    return tail.rsplit(":", 1)[1] if ":" in tail else "latest"


#: What a database member's block is coloured by, instead of an image tag.
#: PRIMARY is the one that takes writes and there is exactly one of it, so it is
#: the colour that has to stand out — losing it is the outage.
ROLE_KEYS = {"PRIMARY": "primary", "SECONDARY": "secondary"}
ROLE_ORDER = ("PRIMARY", "SECONDARY", "STARTUP2", "DOWN")


def component_map(topo, services, roles=None):
    """
    A topology narrowed to one component, coloured by image tag — or, for a
    database, by which member of the set each block IS.

    `roles` is {service name: replica-set state} and switches what a block MEANS
    without changing the picture, which is the point: the same tree, the same
    blocks, so the Overview map and this one still read as one fleet. For an
    application the interesting question is which replicas run which build; for
    a database it is which machine is taking writes, and neither is served by
    showing an image tag that is identical on every member.

    Same nodes and the same blocks as the Overview map, except every block is a
    replica of THIS component and what it says is the tag that replica runs.
    During a rolling update both tags sit on the fleet at once, so "is the new
    build everywhere yet" stops being a percentage in a log line: two colours
    while it rolls, one when it is done, and two that stay two when it is stuck.

    Tags are ranked by replica count, so the incumbent keeps its colour while a
    new one arrives beside it — a palette that reshuffled every tick would make
    two consecutive glances incomparable.
    """
    wanted = set(services)

    def label_of(task):
        if roles is None:
            return task["tag"]
        # A member with no reported state is UNKNOWN and says so. Calling it a
        # secondary would draw a set that looks healthier than it is.
        return roles.get(task["service"], "unknown")

    counts = {}
    for entry in topo["nodes"]:
        for task in entry["tasks"]:
            if task["service"] in wanted:
                label = label_of(task)
                counts[label] = counts.get(label, 0) + 1

    if roles is None:
        # Ranked by replica count, so the incumbent keeps its colour while a new
        # tag arrives beside it — a palette that reshuffled every tick would
        # make two consecutive glances incomparable.
        order = sorted(counts, key=lambda tag: (-counts[tag], tag))
        key_of = {tag: TAG_KEYS[i % len(TAG_KEYS)] for i, tag in enumerate(order)}
        fallback = "platform"
    else:
        # Ranked by ROLE, not by count: primary first however few there are of
        # it, because "which one is primary" is the whole question.
        order = ([r for r in ROLE_ORDER if r in counts]
                 + sorted(k for k in counts if k not in ROLE_ORDER))
        key_of = {role: ROLE_KEYS.get(role, "platform") for role in order}
        fallback = "platform"

    nodes = []
    for entry in topo["nodes"]:
        tasks = [dict(t, key=key_of.get(label_of(t), fallback), name=label_of(t))
                 for t in entry["tasks"] if t["service"] in wanted]
        nodes.append({**entry, "tasks": tasks, "tasks_total": len(tasks)})
    return {
        "nodes": nodes,
        "tags": [{"tag": tag, "key": key_of[tag], "count": counts[tag]} for tag in order],
        "total": sum(counts.values()),
        "by_role": roles is not None,
    }


def find_node(topo, node_id):
    """One node out of a topology, by short or full id. None when absent."""
    for entry in topo["nodes"]:
        if node_id in (entry["id"], entry["full_id"]):
            return entry
    return None


#: The one node label a user may not write.
#:
#: The autoscaler stamps `managedby=autoscaler` on the workers it created, and
#: drains, deletes and reaps ONLY those. That makes this key a permission rather
#: than a note: setting it by hand on a node someone else owns hands that node to
#: the reaper, and dropping it from one of ours leaks a server nothing will ever
#: remove. So the form does not offer it, `validate_labels` refuses it, and
#: `merge_labels` carries the live value through whatever was submitted.
#:
#: The value is an owner NAME, not a flag, so a node stamped `managedby=dbmanager`
#: later is protected by exactly this rule with nothing here to change.
OWNER_LABEL = "managedby"


def node_owner(labels):
    """Who manages this node, or "" when nobody does."""
    return (labels or {}).get(OWNER_LABEL, "")


def merge_labels(current, pairs):
    """
    The label set to write: everything submitted, plus the reserved owner label
    exactly as it already is.

    `update_node` REPLACES the label map, so a form that simply does not render
    the reserved key would delete it on every save.
    """
    out = {p["key"]: p["value"] for p in pairs if p["key"] != OWNER_LABEL}
    owner = node_owner(current)
    if owner:
        out[OWNER_LABEL] = owner
    return out


def validate_labels(pairs):
    """Problems with a set of {key, value} node labels. Empty list when fine."""
    problems, seen = [], set()
    for pair in pairs:
        key, value = pair["key"], pair["value"]
        if key == OWNER_LABEL:
            problems.append(f"{key!r} says which manager owns this node and is set "
                            f"by that manager, not here.")
        elif not _LABEL_KEY.match(key):
            problems.append(f"{key!r} is not a usable label name — letters, digits, "
                            f"dot, dash and underscore only, starting with a letter "
                            f"or digit.")
        elif key in seen:
            problems.append(f"{key!r} is listed twice.")
        seen.add(key)
        if len(value) > 255 or any(c in value for c in "\n\r\t"):
            problems.append(f"The value of {key!r} must be one line of at most 255 "
                            f"characters.")
    return problems


#: How bad a tone is. Used to detect a component whose overall colour is worse
#: than its primary service's label admits.
_RANK = {"ok": 0, "mute": 0, "warn": 1, "bad": 2}


#: Managed spec field -> where its LIVE value actually lives.
#:
#: The spec's copy is a seed, not the truth. The autoscaler re-sizes
#: reservations on the running service and CI moves the image, so a panel that
#: reads the file shows whatever was last written there — which for a
#: right-sized component was 0.36 CPU against a service actually reserving 0.02.
#: A section headed "managed for you" showing the one number nobody manages is
#: worse than showing nothing.
_LIVE_OF = {
    "cpu_reservation":      lambda s: s["resources"].get("cpu_res"),
    "memory_reservation_mb": lambda s: s["resources"].get("mem_res"),
    "cpu_limit":            lambda s: s["resources"].get("cpu_limit"),
    "memory_limit_mb":      lambda s: s["resources"].get("mem_limit"),
    "replicas":             lambda s: s.get("desired"),
    "image":                lambda s: s.get("image_short"),
}


def managed_values(component, primary):
    """
    {field name: value as it IS} for every managed field.

    Falls back to the spec when the service does not exist yet — before a first
    deploy the file genuinely is the only thing that knows.
    """
    out = {}
    for field in type(component).fields():
        if not getattr(field, "managed", None):
            continue
        value = None
        if primary and primary.get("exists"):
            reader = _LIVE_OF.get(field.name)
            if reader:
                try:
                    value = reader(primary)
                except Exception:  # noqa: BLE001
                    value = None
        if value in (None, ""):
            value = component.spec.get(field.name)
        out[field.name] = value
    return out


def component_reserved(services):
    """
    What this component has promised across ALL of its services and replicas.

    The map on the Overview shows one chip per task, so the number on it is per
    REPLICA. That answers "how much of this node is this one task holding" and
    not "what is this component costing me", which is replicas x reservation
    summed over the app, its database and any sidecar.
    """
    cpu = mem = 0.0
    for svc in services:
        if not svc.get("exists"):
            continue
        count = max(svc.get("desired") or 0, 0)
        res = svc.get("resources") or {}
        cpu += (res.get("cpu_res") or 0) * count
        mem += (res.get("mem_res") or 0) * count
    return {"cpu": round(cpu, 3), "mem_mb": int(mem)}


def component_view(component, service_fn):
    """
    A component's live state, merged with its spec.

    The Component object stays the source of truth for what it IS; this adds
    what it is DOING. Templates get both, so nothing here needs to know the
    difference between an application and a database.
    """
    services = [service_fn(name, with_tasks=False) for name in component.services()]
    primary = services[0] if services else None
    tones = [s["tone"] for s in services]
    if "bad" in tones:
        worst = "bad"
    elif "warn" in tones:
        worst = "warn"
    elif tones and all(t == "mute" for t in tones):
        worst = "mute"
    else:
        worst = "ok"

    # A component whose services do not exist yet is not "healthy", it is not
    # deployed. This distinction used to be folded to "ok" in one function and
    # "mute" in another, so the same component read differently on two pages.
    if primary is not None and not primary["exists"]:
        worst, state = "mute", "not deployed"
    else:
        state = primary["state"] if primary else "unknown"
        # The colour comes from the WORST service, the label from the primary
        # one. When they disagree the pill contradicts itself — a red "healthy",
        # which is what a Redis whose exporter cannot be placed rendered as.
        # The label has to answer for the whole component, not just its head.
        if primary is not None and _RANK.get(worst, 0) > _RANK.get(primary["tone"], 0):
            state = "degraded"

    return {
        "name": component.name,
        "type": component.TYPE,
        "label": component.LABEL,
        "category": component.CATEGORY,
        "blurb": component.BLURB,
        "created_at": component.created_at,
        "summary": component.summary(),
        "access": component.access(),
        "services": services,
        "primary": primary,
        "tone": worst,
        "state": state,
        "managed": managed_values(component, primary),
        "reserved": component_reserved(services),
        "running": primary["running"] if primary else 0,
        "desired": primary["desired"] if primary else 0,
    }


def broken_view(name, problem):
    """A component whose spec will not parse. Listed, not hidden."""
    return {
        "name": name, "type": "?", "label": "Unreadable", "category": "Application",
        "blurb": problem, "created_at": None, "summary": problem, "access": None,
        "services": [], "primary": None, "tone": "bad", "state": "broken spec",
        "running": 0, "desired": 0,
        # Same keys as a healthy view: a template that reaches for these must
        # not have to know it is looking at a broken component.
        "managed": {}, "reserved": {"cpu": 0, "mem_mb": 0},
    }


def with_cluster_share(views, nodes):
    """
    Add each component's reservation as a percentage of total cluster capacity.

    Of the WHOLE cluster, not of one node: a component's replicas are spread
    across machines, so "12% of the cluster" is the honest answer and "12% of a
    node" would be true of no node in particular.
    """
    total_cpu = sum((n.get("cpus") or 0) for n in nodes)
    total_mem = sum((n.get("memory_gb") or 0) for n in nodes) * 1024
    for view in views:
        reserved = view.get("reserved") or {}
        view["reserved"] = {
            **reserved,
            "cpu_pct": round((reserved.get("cpu") or 0) / total_cpu * 100, 1) if total_cpu else 0,
            "mem_pct": round((reserved.get("mem_mb") or 0) / total_mem * 100, 1) if total_mem else 0,
        }
    return views


def component_views(service_fn):
    """Every component on disk. One broken spec does not hide the others."""
    try:
        import components
    except Exception:
        return []
    found, problems = components.all_components()
    return ([component_view(c, service_fn) for c in found]
            + [broken_view(name, problem) for name, problem in problems])


def summary(service_fn, nodes, vm_query):
    """
    The overview tiles.

    Cluster-wide, because there is no longer a distinguished application to be
    "the" p95 — that number is per component now, and the honest cluster-level
    version is how many components are unhealthy and how many are breaching.
    """
    workers = [n for n in nodes if n["role"] == "worker"]
    views = component_views(service_fn)
    return {
        "components": len(views),
        "components_ok": len([v for v in views if v["tone"] == "ok"]),
        "degraded": [v for v in views if v["tone"] in ("bad", "warn")],
        "undeployed": [v for v in views if v["state"] == "not deployed"],
        "slo_breaching": int(vm_query(
            "count(autoscaler_service_p95_ms > on (service) "
            "autoscaler_service_slo_p95_ms)") or 0),
        "replicas_running": sum(v["running"] for v in views),
        "replicas_desired": sum(v["desired"] for v in views),
        "workers": len(workers),
        "workers_ready": len([n for n in workers if n["tone"] == "ok"]),
        # `overseer_`, not `autoscaler_`. The fleet moved to the overseer and
        # took its gauges with it, but these five names did not follow, so
        # every one of them read None against the live cluster and the tiles
        # showed 0 workers, 0 hosts and no cluster load at all. The two p95
        # names above are NOT part of that move — replica counts are still the
        # autoscaler's, and they were verified present before this was changed.
        "max_workers": int(vm_query("overseer_max_workers") or 0),
        "hosts": int(vm_query("overseer_current_hosts") or 0),
        "min_workers": int(vm_query("overseer_effective_min_workers") or 0),
        "cluster_cpu": vm_query("overseer_cluster_cpu_percent"),
        "cluster_mem": vm_query("overseer_cluster_mem_percent"),
    }


#: What makes a log line worth a colour. Matched against the line as written,
#: so it catches both `ERROR` from a JVM logger and `level=error` from a Go one.
#: Deliberately only two levels above "normal": a page where a third of the
#: lines are coloured tells you nothing, and the reason anyone opens this tab is
#: to find the one line that is not fine.
#:
#: Kept as SOURCE rather than compiled patterns because the same expressions are
#: handed to Loki to filter with. If the colour and the filter used different
#: definitions of "an error", filtering to errors could hide a line the page had
#: just drawn in red. Both are RE2-compatible, which is what Loki parses.
LEVEL_PATTERNS = (
    ("err", r"(?i)\b(ERROR|FATAL|PANIC|SEVERE)\b|level=(error|fatal)"),
    ("warn", r"(?i)\bWARN(ING)?\b|level=warn(ing)?"),
)

_LOG_LEVELS = tuple((level, re.compile(source))
                    for level, source in LEVEL_PATTERNS)


def log_level(line):
    """`err`, `warn`, or "" — the class the line is drawn with."""
    for level, pattern in _LOG_LEVELS:
        if pattern.search(line):
            return level
    return ""


#: What the Logs tab may be narrowed to. `warn` means "warnings AND errors" —
#: a severity filter that excluded the more severe thing would be a trap.
LEVEL_CHOICES = (("", "any level"), ("warn", "warnings and errors"),
                 ("err", "errors only"))


class LogFilter:
    """
    A narrowing of the Logs tab, expressed once and applied in two places.

    It becomes a LogQL line filter wherever Loki is answering, so the search
    runs over the WHOLE retained window on Loki's side rather than over the two
    hundred lines that happen to be on screen — which is the difference between
    a filter and a highlighter. The same object also filters in Python, because
    the `docker service logs` fallback cannot be asked to filter anything.

    Never raises on operator input. A regex that does not compile is reported as
    `problem` and the filter reports itself inactive, so the tab shows an
    explanation instead of either a stack trace or a silently empty page.
    """

    def __init__(self, contains="", excludes="", level="", regex=False):
        self.contains = (contains or "").strip()
        self.excludes = (excludes or "").strip()
        self.level = level if level in dict(LEVEL_CHOICES) else ""
        self.regex = bool(regex)
        self.problem = ""
        self._match = None
        self._drop = None

        # LogQL raw strings are backtick-quoted and cannot contain a backtick.
        # Refusing is better than escaping: there is no escape for it, and
        # quietly dropping the character would search for something else.
        for value in (self.contains, self.excludes):
            if "`" in value:
                self.problem = "A backtick cannot be searched for."
                return
        if self.regex:
            for value, where in ((self.contains, "match"), (self.excludes, "exclude")):
                if not value:
                    continue
                try:
                    re.compile(value)
                except re.error as exc:
                    self.problem = f"The {where} pattern is not a valid regex: {exc}"
                    return
            self._match = re.compile(self.contains) if self.contains else None
            self._drop = re.compile(self.excludes) if self.excludes else None

    @property
    def active(self):
        return not self.problem and bool(self.contains or self.excludes or self.level)

    def logql(self):
        """
        The line filters to append to a stream selector, or "".

        Order matters for cost, not for correctness: the cheapest and most
        selective filter first, so Loki discards most lines before running a
        regular expression over them.
        """
        if not self.active:
            return ""
        parts = []
        operator = "|~" if self.regex else "|="
        negated = "!~" if self.regex else "!="
        if self.contains:
            parts.append(f"{operator} `{self.contains}`")
        if self.excludes:
            parts.append(f"{negated} `{self.excludes}`")
        if self.level:
            wanted = [source for name, source in LEVEL_PATTERNS
                      if self.level == "warn" or name == self.level]
            parts.append("|~ `" + "|".join(wanted) + "`")
        return " " + " ".join(parts)

    def matches(self, text):
        """The same decision, in Python, for the CLI fallback."""
        if not self.active:
            return True
        if self.level:
            found = log_level(text)
            if self.level == "err" and found != "err":
                return False
            if self.level == "warn" and found not in ("warn", "err"):
                return False
        if self.contains:
            if self.regex:
                if not self._match.search(text):
                    return False
            elif self.contains not in text:
                return False
        if self.excludes:
            if self.regex:
                if self._drop.search(text):
                    return False
            elif self.excludes in text:
                return False
        return True


def log_rows(rows):
    """
    `[(timestamp_ns_or_None, text)]` -> the dicts the log pane renders.

    The timestamp is split out of the text rather than pasted in front of it,
    which is what lets it be dimmed separately — the styles for that
    (`.logs .ts`, `.lvl-warn`, `.lvl-err`) have existed in the stylesheet since
    the tab was written and nothing has ever emitted them.

    A row with no timestamp is a CLI fallback line, which carries its own; it
    keeps an empty stamp rather than being given a made-up one.
    """
    import datetime as dt

    out = []
    for ts, text in rows:
        stamp = ""
        if ts:
            stamp = f"{dt.datetime.fromtimestamp(ts / 1e9, dt.timezone.utc):%H:%M:%S}"
        out.append({"at": stamp, "text": text, "level": log_level(text)})
    return out


# --- observability ----------------------------------------------------------
#
# Three frameworks, on purpose, and they overlap: RED is a subset of the Golden
# Signals, and USE shares "errors" with both. What separates them is SCOPE, and
# each section says so on the page:
#
#   RED     per service    what a request meets
#   USE     per node       what a machine is doing with itself
#   GOLDEN  cluster-wide   the four numbers you wake somebody for
#
# Every expression below was run against this cluster's VictoriaMetrics before
# it was written down. The one real gap is named on the card rather than drawn
# as a mysteriously empty chart: no application here publishes an HTTP timer,
# so per-service rate and errors have no source, and the card says which metric
# would give it one.

#: How much history the column draws, and how coarsely.
OBS_MINUTES = 60
OBS_STEP = 60

#: Where cluster CPU and memory stop being comfortable. NOT invented for the
#: chart — these are the numbers that actually act. `up_cpu` in
#: `signals/classify.py` is what scales a service out; `NODE_PRESSURE_PCT` in
#: `overseer/overseer.py` is what makes the fleet buy a machine. A saturation
#: reading shown without them is a number with no consequence attached.
SATURATION_WARN = 70.0
SATURATION_DANGER = 80.0

#: The ratio `HighErrorRate` fires at, drawn on the errors charts so the line
#: and the alert rule cannot drift apart without somebody seeing it.
ERROR_BUDGET_PCT = 5.0

Q_STATUS_CLASS = (
    'sum by (class) (label_replace(rate(cloudflared_tunnel_response_by_code[5m]),'
    ' "class", "${1}xx", "status_code", "(.).*"))')
Q_ERROR_RATIO = (
    'sum(rate(cloudflared_tunnel_response_by_code{status_code=~"5.."}[5m]))'
    ' / clamp_min(sum(rate(cloudflared_tunnel_response_by_code[5m])), 0.001)')
Q_REQUEST_RATE = 'sum(rate(cloudflared_tunnel_response_by_code[5m]))'
Q_LATENCY = 'overseer_service_latency_ms'
Q_SLO = 'max(autoscaler_service_slo_p95_ms)'

#: Utilisation, one row per node per resource.
Q_UTILISATION = (
    ("cpu", '100 - (avg by (instance) '
            '(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)'),
    ("memory", '100 * (1 - sum by (instance) (node_memory_MemAvailable_bytes)'
               ' / sum by (instance) (node_memory_MemTotal_bytes))'),
    ("disk", '100 * (1 - sum by (instance) '
             '(node_filesystem_avail_bytes{mountpoint="/"})'
             ' / sum by (instance) (node_filesystem_size_bytes{mountpoint="/"}))'),
)

#: Saturation is PSI — seconds per second of work STALLED waiting for a
#: resource. Utilisation says a resource is busy; pressure says something is
#: queueing behind it, which is the difference between fast and full.
Q_PRESSURE = (
    ("cpu stall", 'sum by (instance) '
                  '(rate(node_pressure_cpu_waiting_seconds_total[5m]))'),
    ("memory stall", 'sum by (instance) '
                     '(rate(node_pressure_memory_waiting_seconds_total[5m]))'),
    ("io stall", 'sum by (instance) '
                 '(rate(node_pressure_io_waiting_seconds_total[5m]))'),
)

#: What counts as a resource error, over an hour rather than five minutes:
#: these are rare by nature, and a RATE of "one OOM kill" is noise where a
#: COUNT of "one OOM kill" is the answer.
Q_RESOURCE_ERRORS = (
    ("OOM kills", "sum(increase(node_vmstat_oom_kill[1h]))"),
    ("container OOM", "sum(increase(container_oom_events_total[1h]))"),
    ("tx errors", "sum(increase(node_network_transmit_errs_total[1h]))"),
    ("rx errors", "sum(increase(node_network_receive_errs_total[1h]))"),
    ("tx drops", "sum(increase(node_network_transmit_drop_total[1h]))"),
)

NO_TIMER_NOTE = (
    "Measured at the tunnel, not per service: no application in this cluster "
    "publishes an HTTP timer. A Ktor MicrometerMetrics plugin exporting "
    "http_server_requests_seconds would split this per service — and would give "
    "the HighErrorRate rule in config/alerts.yml, which already reads that "
    "metric, something to fire on.")


def _card(title, note, body, warning=""):
    return {"title": title, "note": note, "body": body, "warning": warning}


def _tone_for(value, warn, danger):
    if value is None:
        return ""
    if value >= danger:
        return "bad"
    if value >= warn:
        return "warn"
    return ""


def _as_percent(series):
    """A 0..1 ratio series redrawn as 0..100, so it shares an axis with a %."""
    return {name: [(t, v * 100.0) for t, v in points]
            for name, points in series.items()}


def _worst_series(series):
    """The single series with the highest peak — a cluster-level rollup."""
    if not series:
        return {}
    name, points = max(series.items(),
                       key=lambda kv: max(v for _, v in kv[1]))
    return {f"worst: {name}": points}


def observability(vm_range, vm_query, charts):
    """
    The RED / USE / Golden column, already drawn.

    `charts` is a parameter rather than an import for the same reason
    `summary()` takes `vm_query`: this stays a pure function of its arguments,
    so the fixtures can render the identical column from canned series and a
    test can pin the shape without a cluster.

    Returns `[{key, title, scope, cards: [{title, note, body, warning}]}]`.
    `body` is markup; the template prints it and decides nothing, so this
    function and `charts.py` are the only two places a chart decision is made.
    """
    def rng(expr, label=None):
        return vm_range(expr, OBS_MINUTES, OBS_STEP, label)

    latency = rng(Q_LATENCY, "service")
    slo = vm_query(Q_SLO)
    errors = _as_percent(rng(Q_ERROR_RATIO))

    red = [
        _card("Duration", "p95 per service, against the SLO it is judged by",
              charts.line(latency, "ms", reference=slo, band=slo,
                          empty="no service is publishing a timer yet")),
        _card("Rate", "responses per second, by status class",
              charts.stack(rng(Q_STATUS_CLASS, "class"), "/s"),
              warning=NO_TIMER_NOTE),
        _card("Errors", "share of responses that are 5xx",
              charts.line(errors, "%", reference=ERROR_BUDGET_PCT,
                          band=ERROR_BUDGET_PCT,
                          empty="no responses recorded in this window")),
    ]

    utilisation = []
    for resource, expr in Q_UTILISATION:
        for node, points in rng(expr, "instance").items():
            value = points[-1][1]
            utilisation.append({
                "name": f"{node} · {resource}", "value": value, "max": 100.0,
                "tone": _tone_for(value, SATURATION_WARN, SATURATION_DANGER)})

    pressure = {}
    for resource, expr in Q_PRESSURE:
        for node, points in rng(expr, "instance").items():
            pressure[f"{node} · {resource}"] = points

    resource_errors = []
    for name, expr in Q_RESOURCE_ERRORS:
        value = vm_query(expr)
        resource_errors.append({"name": name,
                                "value": 0.0 if value is None else value,
                                "tone": "bad" if (value or 0) > 0 else ""})

    use = [
        _card("Utilisation", "how much of each machine is in use right now",
              charts.bars(utilisation, "%", empty="no node is reporting")),
        _card("Saturation",
              "seconds per second of work stalled waiting for a resource — "
              "queueing, which is what utilisation alone cannot tell you",
              charts.line(pressure, "s/s",
                          empty="nothing has queued in this window")),
        _card("Errors", "counted over the last hour, not averaged",
              charts.columns(resource_errors)),
    ]

    golden = [
        _card("Latency", "the slowest service in the cluster",
              charts.line(_worst_series(latency), "ms", reference=slo, band=slo,
                          empty="no service is publishing a timer yet")),
        _card("Traffic", "everything the tunnel served, per second",
              charts.line(rng(Q_REQUEST_RATE), "/s",
                          empty="no responses recorded in this window")),
        _card("Errors", f"5xx share against the {ERROR_BUDGET_PCT:.0f}% the "
                        f"alert fires at",
              charts.line(errors, "%", reference=ERROR_BUDGET_PCT,
                          band=ERROR_BUDGET_PCT,
                          empty="no responses recorded in this window")),
        _card("Saturation",
              f"cluster CPU then memory, against {SATURATION_WARN:.0f}% "
              f"(scales a service out) and {SATURATION_DANGER:.0f}% (buys a "
              f"machine)",
              charts.bullet(vm_query("overseer_cluster_cpu_percent"),
                            SATURATION_WARN, SATURATION_DANGER)
              + charts.bullet(vm_query("overseer_cluster_mem_percent"),
                              SATURATION_WARN, SATURATION_DANGER)),
    ]

    return [
        {"key": "red", "title": "RED",
         "scope": "per service — what a request meets", "cards": red},
        {"key": "use", "title": "USE",
         "scope": "per node — what a machine is doing with itself", "cards": use},
        {"key": "golden", "title": "Golden signals",
         "scope": "cluster-wide — the four you wake somebody for", "cards": golden},
    ]
