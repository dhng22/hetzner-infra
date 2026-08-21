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
                 placeholder="", secret=False, immutable=False):
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


class Component:
    """Subclasses set TYPE/LABEL/BLURB and implement fields() and render()."""

    TYPE = "component"
    LABEL = "Component"
    BLURB = ""
    CATEGORY = "Application"

    def __init__(self, name, data=None):
        self.name = store.check_name(name)
        data = dict(data or {})
        self.created_at = data.get("created_at")
        self.spec = dict(self.defaults(), **(data.get("spec") or {}))

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

    def validate(self):
        """Problems with the spec as it stands. Subclasses extend this."""
        problems = []
        for field in self.fields():
            problem = field.check(self.spec.get(field.name))
            if problem:
                problems.append(problem)
        return problems

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
        cpu_r = self.spec.get("cpu_reservation")
        mem_r = self.spec.get("memory_reservation_mb")
        if not cpu_r or not mem_r:
            raise store.ComponentError(
                f"{self.name} has no CPU or memory reservation. Every component "
                "must declare one — it is what Swarm schedules on and what the "
                "autoscaler measures capacity with."
            )
        out = {"reservations": {"cpus": str(cpu_r), "memory": f"{mem_r}M"}}
        cpu_l = self.spec.get("cpu_limit")
        mem_l = self.spec.get("memory_limit_mb")
        if cpu_l or mem_l:
            limits = {}
            if cpu_l:
                limits["cpus"] = str(cpu_l)
            if mem_l:
                limits["memory"] = f"{mem_l}M"
            out["limits"] = limits
        return out

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
        """
        try:
            path = self.write_stack()
        except store.ComponentError as exc:
            return False, str(exc)
        return run(["docker", "stack", "deploy", "--with-registry-auth",
                    "--resolve-image", "changed", "-c", path, self.stack])

    def remove(self):
        ok, out = run(["docker", "stack", "rm", self.stack], timeout=120)
        # `stack rm` on something that was never deployed is not an error worth
        # blocking a delete on — the files are the component, not the stack.
        if not ok and "not found" not in out.lower():
            return False, out
        store.delete_dir(self.name)
        return True, out or f"removed {self.name}"

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
        """verb -> (callable, button label, confirm text or None)."""
        return {
            "redeploy": (self.deploy, "Redeploy", None),
            "restart": (self.restart, "Rolling restart", None),
            "rollback": (self.rollback, "Roll back",
                         "Return this service to its previous spec?"),
        }

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
