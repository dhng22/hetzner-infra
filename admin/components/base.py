"""
The Component contract.

Everything the panel and the CLI do to a component goes through this class:
what fields it has, what services it owns, what YAML it renders to, what tabs
it shows, what buttons it offers. Adding a type means adding a subclass and
registering it — no route, template or script learns its name.

That is the difference from what this replaced, where the panel branched on
`a.get("credentials") == "redis"`, `env_name in ("prod", "staging")` and
`app_entry.key == 'app'` in a dozen places, and a second application meant
editing all of them.

Stdlib plus PyYAML only. `bin/component` imports this on the master.
"""

import os
import re
import secrets
import shlex
import subprocess

import yaml

from . import store

INFRA_DIR = store.INFRA_DIR

# Both are created before any component exists — `edge` by bootstrap.sh,
# `monitoring` by stacks/monitoring.yml — so every component declares them
# external. No component may own a network another component needs.
EDGE_NETWORK = "edge"
MONITORING_NETWORK = "monitoring"

DOCKER_TIMEOUT = 600


class Field:
    """
    One editable property, described once and rendered everywhere.

    The panel builds its create form and its settings tab from these; the CLI
    builds its `--flags` from the same list. A field that exists in one and not
    the other is not possible, which is what stops the two drifting.
    """

    def __init__(self, name, label, kind="text", default=None, help="",
                 required=False, choices=(), minimum=None, maximum=None,
                 placeholder="", secret=False, immutable=False, managed=None,
                 group=None, switch=False):
        self.name = name
        self.label = label
        self.kind = kind          # text | number | bool | choice | port | cpu | memory
        self.default = default
        self.help = help
        self.required = required
        self.choices = tuple(choices)
        self.minimum = minimum
        self.maximum = maximum
        self.placeholder = placeholder
        self.secret = secret
        self.immutable = immutable    # settable at create, read-only afterwards
        # Who owns this value once the component exists: "ci", "autoscaler", or
        # "convention". A managed field keeps its place in the spec — the
        # renderer still needs a number before anything has been measured, and a
        # reservation is mandatory — but it is not offered for editing, because
        # something else overwrites it and a form that silently loses your input
        # is worse than no form. Still shown at CREATE, where the value is the
        # only one anybody has.
        self.managed = managed
        # Which section of the settings form this belongs in. Everything
        # ungrouped is plain configuration; a group is rendered as its own
        # panel, which is how the scaling policy stopped being twelve more
        # inputs in a list of twenty.
        self.group = group
        # A bool rendered as a switch rather than a checkbox. Not decoration:
        # a checkbox reads as one of several things you might also tick, and a
        # switch reads as a thing that is either on or off right now. The
        # exporter and the data visualiser are the second kind — each one starts
        # or stops a container on its own.
        self.switch = switch

    def coerce(self, raw):
        """Text (from a form or argv) to the stored type. Raises ValueError."""
        if self.kind == "bool":
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in ("1", "true", "yes", "on")
        if raw is None:
            return None
        text = str(raw).strip()
        if text == "":
            return None
        if self.kind in ("number", "port", "memory"):
            value = int(text)
        elif self.kind == "cpu":
            value = float(text)
        else:
            return text
        return value

    def check(self, value):
        """Returns a problem string, or None."""
        if value in (None, ""):
            if self.required:
                return f"{self.label} is required."
            return None
        if self.kind == "choice" and value not in self.choices:
            return f"{self.label} must be one of: {', '.join(self.choices)}."
        if self.minimum is not None and value < self.minimum:
            return f"{self.label} must be at least {self.minimum}."
        if self.maximum is not None and value > self.maximum:
            return f"{self.label} must be at most {self.maximum}."
        return None


class Secret:
    """
    A credential the component owns, stored in `secret.env` and never in the
    spec file.

    Kept off `fields()` on purpose: a Field's value is written to
    `component.json`, which is 0640 and is what you would paste into an issue
    when asking why something will not deploy. A password must not be in there.

    Either supply one or leave it blank and get a generated one — both at create
    time and afterwards. "Generated" is the good default, not the only option:
    plenty of people are moving an existing database and already have a password
    their clients know.

    NOT every credential can be generated, though. One that authenticates to
    somebody ELSE'S system — the Atlas connection string a migration reads — has
    no meaningful generated value: a random one is not a weak secret, it is a
    wrong one, and it would sit there looking configured while every use of it
    failed. Those are declared `generated=False`, where blank simply means unset.
    """

    def __init__(self, key, label, help="", minimum=8, maximum=128,
                 generated=True):
        self.key = key
        self.label = label
        self.help = help
        self.minimum = minimum
        self.maximum = maximum
        #: Whether blank means "make one up". True for a password this component
        #: owns; FALSE for a credential to somebody else's system, where a
        #: generated value is not a weak secret but a wrong one — it would look
        #: set, and every use of it would fail to authenticate.
        self.generated = generated

    def generate(self):
        return secrets.token_hex(24)

    def check(self, value):
        """Returns a problem string, or None. Blank is valid — it means generate."""
        if not value:
            return None
        if len(value) < self.minimum:
            return f"{self.label} must be at least {self.minimum} characters."
        if len(value) > self.maximum:
            return f"{self.label} must be at most {self.maximum} characters."
        if any(c in value for c in "\n\r\t"):
            return f"{self.label} cannot contain a line break or a tab."
        if value.strip() != value:
            return f"{self.label} cannot start or end with a space."
        return None


# An image reference we are willing to deploy. Rejects a bare name with no tag,
# because "whatever :latest means today" is not a deployment you can roll back.
IMAGE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._\-/:@]{0,510}$")


def check_image(ref, allow_floating=False):
    ref = (ref or "").strip()
    if not ref:
        return "An image is required."
    if not IMAGE_RE.match(ref):
        return "That does not look like an image reference."
    name = ref.split("@", 1)[0]
    tail = name.rsplit("/", 1)[-1]
    if ":" not in tail and "@" not in ref:
        return "Pin an explicit tag or digest — an untagged image is not a deployment you can roll back."
    if not allow_floating and name.rsplit(":", 1)[-1] in ("latest",):
        return "Refusing `latest`: the next deploy would be a different build with the same name."
    return None


def run(argv, timeout=DOCKER_TIMEOUT, stdin=None):
    """(ok, combined output). Never raises for a non-zero exit."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, input=stdin)
        return proc.returncode == 0, (proc.stdout + proc.stderr).strip()
    except subprocess.TimeoutExpired:
        return False, (f"Timed out after {timeout}s. The operation may still be "
                       f"converging — check `docker service ls` on the master.")
    except OSError as exc:
        return False, f"Could not run {argv[0]}: {exc}"


def docker_out(argv):
    """stdout of a docker query, or '' — for reading live state, never writing."""
    ok, out = run(["docker"] + argv, timeout=30)
    return out if ok else ""


def action(run, label, confirm=None, tone="", when=None):
    """
    One button on a component's page.

    `tone` is the button's weight and `when` is the live state it applies to —
    "running" for anything that needs the stack up, "stopped" for the one that
    brings it back, None for always. Both live here rather than in the template
    because the template used to decide them by matching verb names, which is
    the same `if TYPE == ...` in a different costume: a type adding a verb had
    to edit the markup to make its button red.
    """
    return {"run": run, "label": label, "confirm": confirm, "tone": tone, "when": when}


def _names(items, limit=4):
    """A readable list of service names, cut off before it becomes a wall."""
    if len(items) <= limit:
        return ", ".join(items)
    return ", ".join(items[:limit]) + f" and {len(items) - limit} more"


def describe_changes(diff):
    """
    What a deploy is about to do, in one sentence, or "" if it will do nothing.

    Removals come FIRST and are never abbreviated away. `--prune` deletes a
    service the render no longer emits, and that is the one line here worth
    reading twice — everything else is a container restarting.
    """
    if not diff:
        return ""
    parts = []
    if diff["removed"]:
        parts.append("Removing " + ", ".join(diff["removed"]))
    if diff["added"]:
        parts.append("Adding " + _names(diff["added"]))
    if diff["changed"]:
        parts.append("Rolling " + _names(diff["changed"]))
    if not parts:
        return ""
    sentence = "; ".join(parts) + "."
    if diff["unchanged"]:
        sentence += f" Leaving {_names(diff['unchanged'])} alone."
    return sentence


def nothing_to_do(diff):
    """
    True when a deploy provably has no work. `None` means we could not tell —
    an unreadable previous render, or a first deploy — and an unknown is not a
    licence to skip: deploying costs a few seconds, and NOT deploying when
    something did change is a component that silently ignores your edit.
    """
    return bool(diff) and not (diff["added"] or diff["removed"] or diff["changed"])


#: A component's certificate authority, as Swarm names it. Anything matching
#: this IS a component CA — the name is the registry, so nothing has to keep a
#: second list of which components issue certificates.
_CA_SECRET_RE = re.compile(r"^(?P<component>.+)-tls-ca-v(?P<version>\d+)$")


def tls_ca_secrets():
    """
    Every component authority currently in the cluster: {component: secret}.

    Read from Docker rather than from disk, for the same reason `secret_name`
    is: dataguard can issue a new version between two deploys, and a stack that
    referenced the remembered name would fail to deploy against a secret that no
    longer exists.
    """
    newest = {}
    for line in docker_out(["secret", "ls", "--format", "{{.Name}}"]).splitlines():
        found = _CA_SECRET_RE.match(line.strip())
        if not found:
            continue
        component = found.group("component")
        version = int(found.group("version"))
        if version >= newest.get(component, (0, ""))[0]:
            newest[component] = (version, found.group(0))
    return {name: secret for name, (_, secret) in newest.items()}


def ca_file_for(component_name):
    """
    Where a CLIENT finds that component's authority, in any container that
    mounts it. One path, chosen once: it is written into the connection string
    the panel publishes, so every consumer — the component's own exporter, its
    console, and an application on the edge network — satisfies it the same way.

    Named after the component rather than a bare `ca.crt` because an application
    talking to two databases mounts two of these, and two files cannot share one
    target.
    """
    return f"/run/secrets/{component_name}-ca.crt"


class Component:
    """Subclasses set TYPE/LABEL/BLURB and implement fields() and render()."""

    TYPE = "component"
    LABEL = "Component"
    BLURB = ""
    CATEGORY = "Application"      # which section of the list it appears in
    #: Which entry of the "+ New" menu creates it. Several types can share a
    #: group — a second database type is a new class and nothing else, and the
    #: create page grows a picker on its own.
    GROUP = "Application"
    #: The field whose switch means "something else owns this component's
    #: shape". `autoscale` for an application, `dataguard` for a database, None
    #: for a type nothing manages. See `normalize`.
    MANAGER_FIELD = None

    #: (key, title, master field or None, note) for each grouped section of the
    #: settings form, in order. Everything ungrouped renders as Configuration
    #: above these. A group naming a master field gets a switch in its title and
    #: its body is disabled when that switch is off.
    GROUPS = ()

    #: True when this type owns a volume named `<component>-data`.
    #:
    #: `docker stack rm` does not delete volumes and neither do we — a mistyped
    #: delete should cost you a redeploy, not your data. It is a flag rather than
    #: two identical `remove()` overrides and an `if TYPE == 'redis'` in the
    #: delete form, which is what it was until a second database made both wrong.
    KEEPS_VOLUME = False

    def __init__(self, name, data=None):
        self.name = store.check_name(name)
        data = dict(data or {})
        self.created_at = data.get("created_at")
        stored = data.get("spec") or {}
        self.spec = dict(self.defaults(), **stored)
        # A COMPONENT THAT PREDATES THE MANAGER SWITCH IS NOT MANAGED.
        #
        # Every other field can take its class default when an old spec does not
        # mention it: the default is what a sensible new component would have
        # said, and applying it changes a number. This one changes the SHAPE.
        # `dataguard` defaults to on, which is right for something created
        # through the form, where the switch and its help text are in front of
        # you. Inherited by a spec written before the field existed, it silently
        # reclassifies a running single-instance database as a replica set — and
        # the next redeploy of any kind, a password rotation included, renders
        # members and sentinels in place of the one service that is actually
        # holding the data. Components deploy with `--prune`, so the old service
        # is not left behind alongside the new ones; it is deleted. The
        # connection string every application uses changes at the same moment.
        #
        # `create()` always writes the key, so ABSENT means "older than the
        # feature", never "a new component that declined it". Turning it on is
        # then something an operator does deliberately, on a component whose
        # migration they have read about, which is exactly what the plan
        # promised for MongoDB and what nothing was enforcing for either engine.
        if self.MANAGER_FIELD and self.MANAGER_FIELD not in stored and stored:
            self.spec[self.MANAGER_FIELD] = False

    # --- schema -------------------------------------------------------------

    @classmethod
    def fields(cls):
        raise NotImplementedError

    @classmethod
    def defaults(cls):
        return {f.name: f.default for f in cls.fields()}

    @classmethod
    def coerce_spec(cls, raw):
        """
        {field: text} -> (spec, problems). Unknown keys are ignored rather than
        rejected: the form and the CLI both post extras (csrf, name, type), and
        a spec that silently drops what it does not know is easier to extend.
        """
        spec, problems = {}, []
        for field in cls.fields():
            if field.name not in raw:
                spec[field.name] = field.default
                continue
            try:
                value = field.coerce(raw[field.name])
            except (TypeError, ValueError):
                problems.append(f"{field.label} must be a number.")
                spec[field.name] = field.default
                continue
            # An explicit empty value CLEARS an optional field; only an absent
            # key falls back to the default. Without this you could never turn
            # off a healthcheck once it had one.
            if value is None and raw[field.name] is None:
                value = field.default
            problem = field.check(value)
            if problem:
                problems.append(problem)
            spec[field.name] = value
        return spec, problems

    def normalize(self, changed=()):
        """
        Repair a spec that says two contradictory things. Returns what it fixed.

        There is exactly one such pair today, and it is one decision wearing two
        hats: whether a manager owns this component, and where this component is
        allowed to run. A manager that may not choose the machine cannot do its
        job — dataguard's entire first move is putting a replica somewhere else
        — and a placement somebody pinned by hand is a promise the manager would
        break on its next loop.

        So they are kept in step HERE, once. The create form, the settings tab
        and `bin/component` all pass through this, which is what stops the three
        of them disagreeing; the form's JavaScript mirrors it for feel, and this
        is the authority.

        `changed` names the fields this save actually touched, because the two
        directions are not symmetric. Turning the switch ON is a request to be
        managed, and the placement follows it. Pinning the placement is a request
        to be left alone, and the switch follows THAT.
        """
        field = self.MANAGER_FIELD
        if not field:
            return []
        managed = bool(self.spec.get(field))
        mode = (self.spec.get("placement_mode") or "auto").strip()
        changed = set(changed or ())
        fixed = []
        if managed and mode != "auto":
            if field in changed and "placement_mode" not in changed:
                self.spec["placement_mode"] = "auto"
                fixed.append(f"Placement was set to auto: {field} owns where this runs.")
            else:
                self.spec[field] = False
                fixed.append(f"{field.title()} was turned off: it cannot manage a "
                             f"component pinned to {mode}.")
        return fixed

    def validate(self):
        """Problems with the spec as it stands. Subclasses extend this."""
        problems = []
        for field in self.fields():
            problem = field.check(self.spec.get(field.name))
            if problem:
                problems.append(problem)
        return problems

    # --- credentials --------------------------------------------------------
    # Declared, not hard-coded, so the panel renders a credentials tab for any
    # type that has one and none for a type that does not.

    SECRETS = ()

    def secret_values(self):
        return store.env_map(self.name, "secret.env")

    def secret(self, key):
        return self.secret_values().get(key, "")

    def apply_secrets(self, raw, generate_missing=True):
        """
        Set this component's credentials from a form, generating any left blank.

        Returns a list of problems; nothing is written when there are any. Also
        used at create time, which is why blank has to mean "generate" rather
        than "unset" — a database with no password is not a state worth being
        able to reach by leaving a field empty.
        """
        if not self.SECRETS:
            return []
        current = self.secret_values()
        problems, values = [], {}
        for spec in self.SECRETS:
            supplied = (raw.get(spec.key) or raw.get(spec.key.lower()) or "").strip()
            problem = spec.check(supplied)
            if problem:
                problems.append(problem)
                continue
            if supplied:
                values[spec.key] = supplied
            elif current.get(spec.key):
                values[spec.key] = current[spec.key]        # unchanged
            elif generate_missing and spec.generated:
                values[spec.key] = spec.generate()
        if problems:
            return problems
        self._write_secrets(values)
        return []

    def rotate_secrets(self):
        """
        Regenerate every credential this component OWNS, ignoring what is there.

        A credential to somebody else's system is carried through untouched:
        rotating it here would not change anything at their end, it would just
        replace a working connection string with a random one.
        """
        current = self.secret_values()
        self._write_secrets({
            s.key: (s.generate() if s.generated else current.get(s.key, ""))
            for s in self.SECRETS})

    def _write_secrets(self, values):
        store.write_env(
            self.name, [{"key": k, "value": v} for k, v in values.items()],
            filename="secret.env",
            header=["# Generated or set through the panel. Edit it there rather than",
                    "# here, so the running service is updated with it.", ""])

    # --- identity -----------------------------------------------------------

    @property
    def stack(self):
        return self.name

    def service_key(self):
        """The compose key of the component's main service."""
        return self.TYPE

    @property
    def service(self):
        """Its Swarm name: stack_key, e.g. `api_app`."""
        return f"{self.stack}_{self.service_key()}"

    def services(self):
        return [self.service]

    def as_dict(self):
        return {"name": self.name, "type": self.TYPE,
                "created_at": self.created_at, "spec": dict(self.spec)}

    # --- rendering ----------------------------------------------------------

    def secret_versions(self, suffix):
        prefix = f"{self.name}-{suffix}-v"
        out = []
        for line in docker_out(["secret", "ls", "--format", "{{.Name}}"]).splitlines():
            line = line.strip()
            if line.startswith(prefix):
                try:
                    out.append((int(line[len(prefix):]), line))
                except ValueError:
                    continue
        return sorted(out)

    def secret_name(self, suffix):
        """
        The newest existing version of one of this component's Swarm secrets.

        Read from Docker rather than remembered, because dataguard creates new
        versions during a renewal and this file must render whatever is current —
        the same reason the running image and replica count are read back.
        """
        versions = self.secret_versions(suffix)
        return versions[-1][1] if versions else f"{self.name}-{suffix}-v1"

    def render(self):
        raise NotImplementedError

    def loki_logging(self):
        master_ip = os.environ.get("MASTER_PRIVATE_IP", "")
        return {
            "driver": "loki",
            "options": {
                "loki-url": f"http://{master_ip}:3100/loki/api/v1/push",
                "loki-retries": "3",
                "loki-batch-size": "400",
                "mode": "non-blocking",
                "max-buffer-size": "4m",
            },
        }

    def resources(self):
        """
        Reservations are mandatory and the renderer enforces it.

        They are load-bearing twice: Swarm subtracts them when placing a task,
        and they are the only input the autoscaler's capacity arithmetic has.
        A service with none makes the master look idle, so replicas get packed
        on top of VictoriaMetrics until something is OOM-killed.
        """
        # The live sizing wins where it exists; the spec is the seed used before
        # anything has been measured, and the fallback when the service is gone.
        live = self.live_resources()
        cpu_r = live.get("cpu_reservation") or self.spec.get("cpu_reservation")
        mem_r = live.get("memory_reservation_mb") or self.spec.get("memory_reservation_mb")
        if not cpu_r or not mem_r:
            raise store.ComponentError(
                f"{self.name} has no CPU or memory reservation. Every component "
                "must declare one — it is what Swarm schedules on and what the "
                "autoscaler measures capacity with."
            )
        out = {"reservations": {"cpus": str(cpu_r), "memory": f"{mem_r}M"}}
        cpu_l = live.get("cpu_limit") or self.spec.get("cpu_limit")
        mem_l = live.get("memory_limit_mb") or self.spec.get("memory_limit_mb")
        if cpu_l or mem_l:
            limits = {}
            if cpu_l:
                limits["cpus"] = str(cpu_l)
            if mem_l:
                limits["memory"] = f"{mem_l}M"
            out["limits"] = limits
        return out

    def viewer_databases(self):
        """
        Connections to register in the visualiser once it has started, if any.

        Most consoles need none: mongo-express is handed a URL in an environment
        variable and connects itself. RedisInsight has no equivalent — see the
        override on the Redis component — so it gets told through its own API
        instead. Returning a list rather than a flag because "which server" is a
        decision the component owns and the proxy route should not be making.
        """
        return []

    def base_labels(self):
        """Labels every component's services carry, whatever the type."""
        return {"infra.component": self.name, "infra.type": self.TYPE}

    def stack_yaml(self):
        return yaml.safe_dump(self.render(), sort_keys=False, width=100,
                              default_flow_style=False)

    def write_stack(self):
        path = store.path_for(self.name, "stack.yml")
        store._write_atomic(path, self.stack_yaml(), 0o600)
        return path

    def pending_changes(self):
        """
        Which of this component's services a deploy would actually touch:
        `{"added": [...], "removed": [...], "changed": [...], "unchanged": [...]}`.

        Read-only. It renders the stack and compares it with the one on disk,
        which is the render that was last applied — `write_stack()` writes it
        immediately before `deploy()` uses it, so it is the only record of what
        Swarm was last told.

        This is NOT a second change-detector bolted on beside Swarm's. Swarm
        already leaves a service whose spec is byte-identical completely alone,
        which is why saving an unrelated field has never restarted a database.
        What was missing is that nobody could SEE that, so every save looked
        like a whole-stack event and a save that changed nothing was still a
        deploy. This answers both: the caller names what will move, and skips
        the deploy entirely when the answer is nothing.

        Deliberately not a parameter on `deploy()`. The panel tests replace
        `deploy` with a fixed-arity recorder, and widening that signature would
        break every one of them to express something that reads better as its
        own question anyway.

        Two things it cannot promise, and the caller must not claim otherwise:
        `--resolve-image changed` can still roll a service whose TAG moved
        under it since the last deploy, and a rotated secret legitimately shows
        up as a change because secret names carry their version.

        Not free: rendering a database issues its certificates and its password
        if they do not exist yet. Both are idempotent by construction — that is
        what makes rendering safe to do twice per save — but this is the reason
        it is a method on the component rather than a pure function over two
        files.
        """
        def services_of(text):
            try:
                loaded = yaml.safe_load(text) or {}
            except yaml.YAMLError:
                return None
            found = loaded.get("services")
            return found if isinstance(found, dict) else None

        def full(key):
            return f"{self.stack}_{key}"

        fresh = services_of(self.stack_yaml())
        if fresh is None:                      # our own render is unparseable
            return None
        try:
            with open(store.path_for(self.name, "stack.yml")) as fh:
                previous = services_of(fh.read())
        except OSError:
            previous = None
        if previous is None:
            # Never deployed from this panel, or the file is gone. Everything is
            # new as far as we can prove, and claiming otherwise would be a
            # guess presented as a fact.
            return {"added": sorted(full(k) for k in fresh), "removed": [],
                    "changed": [], "unchanged": []}

        return {
            "added": sorted(full(k) for k in fresh if k not in previous),
            "removed": sorted(full(k) for k in previous if k not in fresh),
            "changed": sorted(full(k) for k in fresh
                              if k in previous and fresh[k] != previous[k]),
            "unchanged": sorted(full(k) for k in fresh
                                if k in previous and fresh[k] == previous[k]),
        }

    # --- live state ---------------------------------------------------------

    def live_replicas(self, service=None):
        out = docker_out(["service", "inspect", service or self.service,
                          "--format", "{{.Spec.Mode.Replicated.Replicas}}"])
        try:
            return int(out)
        except (TypeError, ValueError):
            return None

    def live_image(self, service=None):
        out = docker_out(["service", "inspect", service or self.service,
                          "--format", "{{.Spec.TaskTemplate.ContainerSpec.Image}}"])
        # Swarm appends @sha256:… once it has resolved the digest. Keep it: it
        # is what is actually running, and re-deploying the digest is a no-op
        # rather than a silent re-pull of a moved tag.
        return out or None

    def live_resources(self, service=None):
        """
        {cpu_reservation, memory_reservation_mb, cpu_limit, memory_limit_mb} as
        the RUNNING service has them, or {} if it is not deployed yet.

        Read back for the same reason as the image, the replica count and the
        worker pin: something other than this file owns the value at runtime.
        The autoscaler re-sizes reservations from what a component actually
        uses, so emitting the spec's copy would undo that on the next unrelated
        save — and undoing it means re-reserving a third of a core for something
        that needs a fortieth, which is what kept a worker alive against no
        traffic (docker/cli#2235 is the same trap, one field over).
        """
        out = docker_out([
            "service", "inspect", service or self.service, "--format",
            "{{with .Spec.TaskTemplate.Resources}}"
            "{{with .Reservations}}{{.NanoCPUs}} {{.MemoryBytes}}{{else}}0 0{{end}} "
            "{{with .Limits}}{{.NanoCPUs}} {{.MemoryBytes}}{{else}}0 0{{end}}"
            "{{end}}"])
        parts = out.split()
        if len(parts) != 4:
            return {}
        try:
            cpu_r, mem_r, cpu_l, mem_l = (int(p or 0) for p in parts)
        except ValueError:
            return {}
        live = {}
        if cpu_r:
            live["cpu_reservation"] = round(cpu_r / 1e9, 3)
        if mem_r:
            live["memory_reservation_mb"] = mem_r // (1024 * 1024)
        if cpu_l:
            live["cpu_limit"] = round(cpu_l / 1e9, 3)
        if mem_l:
            live["memory_limit_mb"] = mem_l // (1024 * 1024)
        return live

    def live_worker_pinned(self, service=None):
        out = docker_out(["service", "inspect", service or self.service,
                          "--format",
                          "{{range .Spec.TaskTemplate.Placement.Constraints}}{{println .}}{{end}}"])
        return any(re.match(r"^\s*node\.role\s*==\s*worker\s*$", line)
                   for line in out.splitlines())

    # --- actions ------------------------------------------------------------

    def deploy(self):
        """
        Render and apply. Returns (ok, output).

        The render reads image, replica count and worker pin back from the
        running service first, because those three belong to CI, the autoscaler
        and the autoscaler respectively — not to this file. Applying the spec's
        idea of them would roll production back to whatever was last typed into
        a form (docker/cli#2235).

        `--prune` removes services in this stack that the render no longer
        emits. Without it, turning off an optional sub-service in the panel is a
        no-op that looks like it worked: `docker stack deploy` only ever adds
        and updates, so disabling a Redis exporter dropped it from the spec, the
        stack file and the component's own service list — while the container
        kept running, kept its CPU reservation, and kept being scraped. Nothing
        in the panel would ever mention it again.

        It is safe here for the reason the whole component model exists: one
        component owns one stack and nothing else writes to it, so everything
        `--prune` can reach is this component's own.
        """
        try:
            path = self.write_stack()
        except store.ComponentError as exc:
            return False, str(exc)
        return run(["docker", "stack", "deploy", "--with-registry-auth",
                    "--resolve-image", "changed", "--prune",
                    "-c", path, self.stack])

    def stop(self):
        """
        Take the stack down without forgetting it.

        `docker stack rm`, not `--replicas 0`: a service scaled to zero is still
        discovered by the autoscaler, which reads its own floor off the policy
        labels and puts every replica straight back — a stop button that undoes
        itself sixty seconds later is worse than none. Removing the services
        removes them from discovery, and nothing that makes this component what
        it is lives in Swarm: the spec, the environment, the credentials and the
        volumes are all on disk and all still there.
        """
        ok, out = run(["docker", "stack", "rm", self.stack], timeout=120)
        if not ok and "not found" not in out.lower():
            return False, out
        return True, (f"{self.name} is stopped. Its files, environment, credentials "
                      f"and volumes are kept — press Deploy to bring it back.")

    def remove(self):
        ok, out = run(["docker", "stack", "rm", self.stack], timeout=120)
        # `stack rm` on something that was never deployed is not an error worth
        # blocking a delete on — the files are the component, not the stack.
        if not ok and "not found" not in out.lower():
            return False, out
        store.delete_dir(self.name)
        out = out or f"removed {self.name}"
        if self.KEEPS_VOLUME:
            out += (f"\nThe volume {self.name}-data was kept. Delete it with "
                    f"`docker volume rm {self.name}-data` once you are sure.")
        return True, out

    def restart(self):
        return run(["docker", "service", "update", "--force", "--detach=false",
                    self.service])

    def rollback(self):
        return run(["docker", "service", "rollback", self.service])

    def logs(self, lines=200, service=None):
        ok, out = run(["docker", "service", "logs", "--no-trunc",
                       "--tail", str(lines), service or self.service], timeout=30)
        return out if ok else f"(no logs: {out})"

    def tabs(self):
        return [("overview", "Overview"), ("logs", "Logs")]

    def actions(self):
        """
        verb -> action(), in the order the panel renders them.

        Stop and Deploy are one pair of mutually exclusive buttons rather than a
        toggle, because a toggle has to guess which state it is in before it can
        label itself; `when` lets the panel show whichever one applies to what
        is actually running.
        """
        return {
            "stop": action(self.stop, "Stop",
                           f"Stop {self.name}? Its services leave the cluster; its "
                           f"files, environment, credentials and volumes stay, and "
                           f"Deploy brings it back.",
                           tone="danger", when="running"),
            "start": action(self.deploy, "Deploy", tone="primary", when="stopped"),
            "redeploy": action(self.deploy, "Redeploy", tone="primary", when="running"),
            "restart": action(self.restart, "Rolling restart", when="running"),
            "rollback": action(self.rollback, "Roll back",
                               "Return this service to its previous spec?",
                               tone="danger", when="running"),
        }

    def credentials(self, master_ip=""):
        """
        What the Credentials tab shows, or None when the type has none.

        Assembled here, once, rather than half in a route and half in Jinja —
        which is how the internal and the external connection URL came to be
        built by two different pieces of code that could disagree. The panel
        renders this tab for any type whose `SECRETS` is non-empty, so a second
        database is a class and not a route change.
        """
        return None

    def access(self):
        """
        Where this component is reachable, for the panel to display.

        Routing is not automated on purpose: you add the hostname in the
        Cloudflare dashboard once, when you create the component. What the
        panel owes you is the exact local target to paste, and that is service
        DNS on the edge network — the only name cloudflared can resolve.
        """
        return None

    def summary(self):
        """One line for the card. Subclasses override."""
        return self.LABEL


def compose_networks(*names):
    return {n: {"external": True} for n in names}


def shell_command(parts):
    """
    An explicit `sh -c` wrapper for a command that must expand an env var.

    Compose does NOT run a shell for `command:`, so `--requirepass "$PASSWORD"`
    is passed through as the eleven literal characters `$PASSWORD`. That is not
    a hypothetical — it is what the stack file this replaced actually did, so
    the database enforced the string `$REDIS_PASSWORD` while every client was
    handed the real one. Wrapping in `sh -c` is what makes the expansion happen,
    and `$$` is what stops compose interpolating the variable away before the
    shell ever sees it.
    """
    return ["sh", "-c", "exec " + " ".join(parts)]


def quote(value):
    return shlex.quote(str(value))
