"""
An application: your image, your port, your environment.

The only thing this knows about your app is that it listens on a port and can
be replaced by a newer image. It sets no environment of its own — `KTOR_ENV`,
`REDIS_HOST` and `REDIS_PORT` used to be injected here and refused in the
editor, which meant the platform had an opinion about your framework. It does
not any more. Whatever you put in the environment file is exactly what the
container gets.
"""

from . import base, store
from .base import Component, Field


class AppComponent(Component):
    TYPE = "app"
    LABEL = "Application"
    BLURB = "A container image of yours, behind the tunnel."
    CATEGORY = "Application"
    GROUP = "Application"

    @classmethod
    def fields(cls):
        return [
            Field("image", "Image", "text", required=True,
                  placeholder="ghcr.io/you/app:sha-abc1234",
                  help="Pin a tag or digest. `latest` is refused: the next deploy "
                       "would be a different build wearing the same name."),
            Field("port", "Port", "port", 8080, required=True, minimum=1, maximum=65535,
                  help="The port your app listens on inside the container."),
            Field("health_path", "Health path", "text", "/health",
                  help="Checked with wget every 10s. Blank disables the healthcheck — "
                       "then a broken deploy rolls forward instead of rolling back."),
            Field("metrics_path", "Metrics path", "text", "/metrics",
                  help="Prometheus endpoint. Blank stops it being scraped, which also "
                       "removes the latency signal the autoscaler prefers."),
            Field("start_period", "Startup grace", "number", 60, minimum=0, maximum=900,
                  help="Seconds before a failing healthcheck counts. A JVM needs 60."),

            Field("replicas", "Replicas", "number", 2, required=True, minimum=0, maximum=100,
                  help="The count deployed now. Once autoscaling is on, the autoscaler "
                       "owns this number and a deploy preserves whatever is live."),
            Field("cpu_reservation", "CPU reserved", "cpu", 0.5, required=True,
                  minimum=0.01, maximum=32,
                  help="What Swarm subtracts when placing a task, and the unit the "
                       "autoscaler sizes nodes in. Not a limit — set it to what one "
                       "replica actually needs at rest."),
            Field("memory_reservation_mb", "Memory reserved (MB)", "memory", 384,
                  required=True, minimum=16, maximum=131072),
            Field("cpu_limit", "CPU limit", "cpu", 1.0, minimum=0.01, maximum=32,
                  help="The ceiling one replica may use. Also the denominator of the "
                       "CPU-per-replica scaling signal, read live from this service."),
            Field("memory_limit_mb", "Memory limit (MB)", "memory", 768,
                  minimum=16, maximum=131072,
                  help="Exceed it and the container is OOM-killed, so leave headroom."),
            Field("stop_grace", "Shutdown grace", "number", 30, minimum=1, maximum=600,
                  help="Seconds to finish in-flight requests on SIGTERM. Pair it with a "
                       "shutdown hook in your app; the Docker default of 10 cuts them."),

            Field("autoscale", "Autoscale", "bool", False,
                  help="Let the autoscaler own the replica count, using the policy below."),
            Field("min_replicas", "Minimum replicas", "number", 2, minimum=0, maximum=100,
                  help="Two is the usual floor: with one, a crash is an outage and a "
                       "rolling update has nowhere to shift traffic."),
            Field("max_replicas", "Maximum replicas", "number", 8, minimum=1, maximum=100,
                  help="A budget cap. The ReplicaCeiling alert compares against it."),
            Field("slo_p95_ms", "p95 SLO (ms)", "number", 500, minimum=1, maximum=600000,
                  help="THE number. Scale-up and scale-down thresholds are fractions of it."),
            Field("up_p95_ratio", "Scale up at", "cpu", 0.8, minimum=0.05, maximum=2.0,
                  help="Fraction of the SLO that triggers growth — act before users feel it."),
            Field("down_p95_ratio", "Scale down below", "cpu", 0.4, minimum=0.01, maximum=2.0),
            Field("up_cpu_pct", "Scale up above CPU %", "number", 70, minimum=1, maximum=200,
                  help="CPU of ONE replica against its own limit. This is the secondary "
                       "signal, and at low traffic it is the only one: p95 over an empty "
                       "histogram returns nothing at all."),
            Field("down_cpu_pct", "Scale down below CPU %", "number", 30, minimum=1, maximum=200),
            Field("sustain_up_seconds", "Sustain up (s)", "number", 90, minimum=30, maximum=3600,
                  help="How long a signal must stay high. Up fast, down slow — never "
                       "make these two symmetric."),
            Field("sustain_down_seconds", "Sustain down (s)", "number", 900,
                  minimum=60, maximum=86400),
            Field("up_factor", "Growth step", "cpu", 0.5, minimum=0.05, maximum=4.0,
                  help="+50% of current, minimum +1. One at a time cannot track a spike."),
            Field("cooldown_seconds", "Replica cooldown (s)", "number", 60,
                  minimum=0, maximum=3600),
            Field("priority", "Priority", "number", 100, minimum=0, maximum=1000,
                  help="Lower wins when several components compete for the same node "
                       "capacity. Leave it alone unless one app genuinely matters more."),
        ]

    # --- validation ---------------------------------------------------------

    def validate(self):
        problems = super().validate()
        problem = base.check_image(self.spec.get("image"))
        if problem:
            problems.append(problem)

        cpu_r, cpu_l = self.spec.get("cpu_reservation"), self.spec.get("cpu_limit")
        mem_r, mem_l = self.spec.get("memory_reservation_mb"), self.spec.get("memory_limit_mb")
        if cpu_r and cpu_l and cpu_r > cpu_l:
            problems.append("CPU reserved is above the CPU limit — the task could never be placed.")
        if mem_r and mem_l and mem_r > mem_l:
            problems.append("Memory reserved is above the memory limit.")

        if self.spec.get("autoscale"):
            lo, hi = self.spec.get("min_replicas"), self.spec.get("max_replicas")
            if lo is not None and hi is not None and lo > hi:
                problems.append("Minimum replicas is above maximum replicas.")
            if self.spec.get("down_p95_ratio", 0) >= self.spec.get("up_p95_ratio", 1):
                problems.append("Scale-down p95 must be below scale-up p95, or the two "
                                "thresholds cross and the count oscillates.")
            if self.spec.get("down_cpu_pct", 0) >= self.spec.get("up_cpu_pct", 1):
                problems.append("Scale-down CPU must be below scale-up CPU.")
            if not self.spec.get("metrics_path"):
                problems.append("Autoscaling with no metrics path leaves only the CPU "
                                "signal — set a metrics path, or turn autoscaling off.")
        return problems

    # --- rendering ----------------------------------------------------------

    def scaling_labels(self):
        """
        The scaling policy, carried on the service it applies to.

        The autoscaler discovers services by `infra.workload=app` and reads
        their policy from here. Nothing about this app exists in infra.env, in
        the monitoring stack, or in the autoscaler's own configuration — which
        is what makes a second application a create form rather than an edit of
        four files.
        """
        s = self.spec
        labels = {"infra.workload": "app"}
        if not s.get("autoscale"):
            # Still discovered, still pinned with the others, never scaled: a
            # fixed-size app has to move to the workers too.
            labels["autoscale.enabled"] = "false"
            return labels
        labels.update({
            "autoscale.enabled": "true",
            "autoscale.min_replicas": str(s["min_replicas"]),
            "autoscale.max_replicas": str(s["max_replicas"]),
            "autoscale.slo_p95_ms": str(s["slo_p95_ms"]),
            "autoscale.up_p95_ratio": str(s["up_p95_ratio"]),
            "autoscale.down_p95_ratio": str(s["down_p95_ratio"]),
            "autoscale.up_cpu_pct": str(s["up_cpu_pct"]),
            "autoscale.down_cpu_pct": str(s["down_cpu_pct"]),
            "autoscale.sustain_up_seconds": str(s["sustain_up_seconds"]),
            "autoscale.sustain_down_seconds": str(s["sustain_down_seconds"]),
            "autoscale.up_factor": str(s["up_factor"]),
            "autoscale.cooldown_seconds": str(s["cooldown_seconds"]),
            "autoscale.priority": str(s["priority"]),
        })
        return labels

    def render(self):
        s = self.spec
        labels = dict(self.base_labels())
        labels.update(self.scaling_labels())
        if s.get("metrics_path"):
            labels.update({
                "prometheus.scrape": "true",
                "prometheus.port": str(s["port"]),
                "prometheus.path": s["metrics_path"],
            })

        service = {
            "image": self.live_image() or s["image"],
            "networks": [base.EDGE_NETWORK, base.MONITORING_NETWORK],
            "logging": self.loki_logging(),
            "stop_grace_period": f"{s['stop_grace']}s",
            "deploy": {
                "replicas": self._deploy_replicas(),
                "labels": labels,
                "placement": self._placement(),
                "update_config": {
                    "parallelism": 1,
                    # start-first: the new task is healthy BEFORE the old one
                    # stops, so a deploy is not a gap. monitor must exceed your
                    # startup or Swarm calls success before the app has booted.
                    "order": "start-first",
                    "delay": "15s",
                    "monitor": f"{max(30, s['start_period'])}s",
                    "max_failure_ratio": 0,
                    "failure_action": "rollback",
                },
                "rollback_config": {"parallelism": 1, "order": "start-first", "delay": "5s"},
                "restart_policy": {"condition": "any", "delay": "5s"},
                "resources": self.resources(),
            },
        }

        env = store.env_map(self.name)
        if env:
            service["environment"] = env
        if s.get("health_path"):
            service["healthcheck"] = {
                "test": ["CMD", "wget", "-qO-",
                         f"http://localhost:{s['port']}{s['health_path']}"],
                "interval": "10s",
                "timeout": "5s",
                "retries": 3,
                "start_period": f"{s['start_period']}s",
            }

        return {
            "version": "3.8",
            "services": {self.service_key(): service},
            "networks": base.compose_networks(base.EDGE_NETWORK, base.MONITORING_NETWORK),
        }

    def _deploy_replicas(self):
        """
        Live count wins over the spec.

        The autoscaler owns this number at runtime. Applying the spec's copy
        would cut a service from N replicas back to its configured floor at
        whatever moment someone saved an unrelated setting, and the autoscaler
        would then climb back in +50% steps — minutes of reduced capacity caused
        by editing a log level.
        """
        live = self.live_replicas()
        return live if live is not None else self.spec["replicas"]

    def _placement(self):
        """
        Spread preference always; the worker pin only if it is already on.

        The autoscaler moves this service between the master and the workers by
        adding and removing `node.role == worker`. Writing our idea of it here
        would fight that: every deploy would slam placement back to whatever the
        file says, at whatever moment CI happened to ship. So the live constraint
        is read back and re-stated, and never invented.
        """
        placement = {"preferences": [{"spread": "node.id"}]}
        if self.live_worker_pinned():
            placement["constraints"] = ["node.role == worker"]
        return placement

    # --- panel surface ------------------------------------------------------

    def tabs(self):
        return [("overview", "Overview"), ("environment", "Environment"),
                ("deployments", "Deployments"), ("settings", "Settings"),
                ("logs", "Logs")]

    def actions(self):
        actions = super().actions()
        actions["deploy-image"] = (None, "Deploy an image now", None)  # handled by the route
        return actions

    def access(self):
        return {
            "target": f"http://{self.service}:{self.spec['port']}",
            "note": ("Add a hostname in the Cloudflare dashboard pointing at this "
                     "target. It is service DNS on the edge network, which is the "
                     "only name the tunnel connector can resolve."),
        }

    def summary(self):
        image = self.live_image() or self.spec.get("image") or ""
        return image.split("@")[0]
