"""
Which values in infra.env the panel may change, and what has to be redeployed
afterwards.

Three honest categories, because the alternative — greying a field out with no
reason — is what made the first version of this panel useless:

  EDIT     safe to change at runtime; `stack` says what gets redeployed.
  BOOT     consumed once by cloud-init. Changing it here would look like it
           worked and change nothing, so we say what it would actually take.
  SECRET   backed by a docker secret. Swarm secrets are immutable and a
           service's reference is set at deploy time, so rotating one is a
           versioned-name dance, not an edit. Explained rather than hidden.
"""

EDIT, BOOT, SECRET = "edit", "boot", "secret"

# key -> (mode, stack to redeploy or None, explanation)
FIELDS = {
    # --- identity ----------------------------------------------------------
    "APP_NAME": (BOOT, None,
                 "Namespaces the Hetzner label the autoscaler is allowed to delete, the worker "
                 "hostname prefix and every hostname. Changing it would orphan the current "
                 "workers. Rebuild the cluster instead."),
    "ROOT_DOMAIN": (BOOT, None, "Baked into Grafana's root URL and your tunnel hostnames."),

    # --- hetzner -----------------------------------------------------------
    "HCLOUD_LOCATION": (EDIT, "monitoring", "Applies to workers created from now on."),
    "HCLOUD_NETWORK_NAME": (EDIT, "monitoring", "Must already exist in Hetzner."),
    "HCLOUD_SSH_KEY_NAME": (EDIT, "monitoring", "Installed on workers created from now on."),
    "WORKER_IMAGE": (EDIT, "monitoring", "Applies to workers created from now on."),
    "WORKER_TYPE": (EDIT, "monitoring",
                    "Applies to workers created from now on. How many replicas it holds is "
                    "read from the Hetzner catalogue, so there is nothing to keep in sync — "
                    "a bigger type simply means fewer, larger nodes."),

    # --- registry / first image ----------------------------------------------
    # All four are read exactly once, by bootstrap. Registry auth afterwards
    # lives in the docker config the panel manages (Apps > API > Deployments),
    # and the running image is whatever CI last deployed. Editing these later
    # changes nothing you can observe, so the panel does not offer to.
    "GHCR_USER": (BOOT, None, "First `docker login` only. Manage registry auth under Apps > API > Deployments."),
    "GHCR_TOKEN": (BOOT, None, "First `docker login` only."),
    "APP_IMAGE_PROD": (BOOT, None,
                       "Only the very first deploy reads this. The live image is shown on the "
                       "API page and changed from its Deployments tab."),
    "APP_IMAGE_STAGING": (BOOT, None, "Only the very first deploy reads this."),

    # --- scaling -----------------------------------------------------------
    "SLO_P95_MS": (EDIT, "monitoring", "The whole policy. Everything else derives from it."),
    "SCALE_UP_P95_RATIO": (EDIT, "monitoring", "Act at this fraction of the SLO, before users feel it."),
    "SCALE_DOWN_P95_RATIO": (EDIT, "monitoring", ""),
    "SCALE_UP_CPU": (EDIT, "monitoring", "Percent of one replica's own limit, not node CPU."),
    "SCALE_DOWN_CPU": (EDIT, "monitoring", ""),
    "NODE_PRESSURE_PCT": (EDIT, "monitoring", "Placement guard, never a trigger: above this the autoscaler asks for one host more than the capacity arithmetic alone would."),
    "MIN_REPLICAS": (EDIT, "monitoring",
                     "Two is the production floor: with one, a crash is an outage and a rolling "
                     "update has nowhere to shift traffic."),
    "MAX_REPLICAS": (EDIT, "monitoring", "The ReplicaCeiling alert compares against this."),
    "MIN_WORKERS": (EDIT, "monitoring",
                    "A HOST count, and the master is host #1. 1 means no Hetzner workers at "
                    "all — the master runs the app and nothing is billed. 2 or more means "
                    "the master never runs application traffic."),
    "MAX_WORKERS": (EDIT, "monitoring", "A budget cap, not a capacity plan. Also a host count, so 6 means the master plus up to 5 Hetzner workers."),
    "SUSTAIN_UP_SECONDS": (EDIT, "monitoring", "Up fast."),
    "SUSTAIN_DOWN_SECONDS": (EDIT, "monitoring", "Down slow. Never make these symmetric."),
    "SCALE_UP_FACTOR": (EDIT, "monitoring", "+50% of current, minimum +1. One at a time cannot track a spike."),
    "COOLDOWN_UP_SECONDS": (EDIT, "monitoring",
                            "Must exceed node boot + image pull + JVM warmup, or the loop provisions "
                            "again while the last node is still warming and badly overshoots."),
    "COOLDOWN_DOWN_SECONDS": (EDIT, "monitoring", ""),
    "REPLICA_COOLDOWN_SECONDS": (EDIT, "monitoring", ""),
    "SCHEDULE_FLOOR": (EDIT, "monitoring", "UTC, `HH:MM-HH:MM=N`, comma separated. Blank disables it."),
    "DRY_RUN": (EDIT, "monitoring", "`true` makes the autoscaler log every action without taking it."),
    "APP_CPU_LIMIT": (EDIT, "monitoring",
                      "The denominator for CPU-per-replica. It MUST match the cpus limit on api-prod "
                      "in stacks/app.yml or the signal silently misreports."),
    # --- application topology ----------------------------------------------
    # Both are rendered into stacks/app.yml by envsubst on every deploy, so a
    # change here really does take effect — after the app stack is redeployed.
    # They were previously unlisted, which made the panel fall back to "not
    # managed" and show them as unchangeable for no reason.
    "APP_PORT": (EDIT, "app",
                 "The port your app listens on. Feeds the healthcheck, the Prometheus "
                 "scrape label and both tunnel targets. Change it only alongside the "
                 "app itself — a wrong value fails the healthcheck and rolls back."),
    "APP_METRICS_PATH": (EDIT, "app",
                         "Where the app exposes Prometheus metrics. If this is wrong the "
                         "latency signal disappears and the autoscaler falls back to CPU."),
    "APP_SERVICE": (EDIT, "monitoring", "The service the autoscaler scales."),
    "APP_SERVICE_STAGING": (EDIT, "monitoring",
                            "Follows production between manager-only and worker-only "
                            "placement, so the master runs no app at all once workers "
                            "exist. It is never scaled. Blank to leave staging alone."),

    # --- access ------------------------------------------------------------
    "GRAFANA_ADMIN_USER": (EDIT, "monitoring", "Grafana only applies this on first start."),
    "ADMIN_USER": (SECRET, "admin", "Stored as a docker secret alongside the password."),
    "ADMIN_PASSWORD": (SECRET, "admin", "The key to this panel."),
    "GRAFANA_ADMIN_PASSWORD": (SECRET, "monitoring", ""),
    "ALERT_WEBHOOK_URL": (SECRET, "monitoring", "Where Alertmanager sends everything."),
    "CF_TUNNEL_TOKEN": (SECRET, "app", "Rotating this means re-issuing the connector token in Cloudflare."),
    "HCLOUD_TOKEN": (SECRET, "monitoring", "Lets the autoscaler create and delete servers."),
    "CI_SSH_PUBLIC_KEY": (BOOT, None, "The deploy user is created at boot. Edit authorized_keys on the master."),
    "MONGO_URI_PROD": (BOOT, None, "Seeds config/app-prod.env at first boot. Edit it under Apps → API → prod."),
    "MONGO_URI_STAGING": (BOOT, None, "Seeds config/app-staging.env at first boot."),
    "REDIS_PASSWORD_PROD": (BOOT, None, "Seeds config/app-prod.env. Edit it under Apps → Redis → prod."),
    "REDIS_PASSWORD_STAGING": (BOOT, None, "Seeds config/app-staging.env."),
}

GROUPS = [
    ("Identity", ["APP_NAME", "ROOT_DOMAIN"]),
    ("Hetzner", ["HCLOUD_LOCATION", "HCLOUD_NETWORK_NAME", "HCLOUD_SSH_KEY_NAME",
                 "WORKER_IMAGE", "WORKER_TYPE", "HCLOUD_TOKEN"]),
    ("Scaling", ["SLO_P95_MS", "SCALE_UP_P95_RATIO", "SCALE_DOWN_P95_RATIO", "SCALE_UP_CPU",
                 "SCALE_DOWN_CPU", "NODE_PRESSURE_PCT", "MIN_REPLICAS", "MAX_REPLICAS",
                 "MIN_WORKERS", "MAX_WORKERS", "SUSTAIN_UP_SECONDS",
                 "SUSTAIN_DOWN_SECONDS", "SCALE_UP_FACTOR", "COOLDOWN_UP_SECONDS",
                 "COOLDOWN_DOWN_SECONDS", "REPLICA_COOLDOWN_SECONDS", "SCHEDULE_FLOOR",
                 "DRY_RUN", "APP_CPU_LIMIT", "APP_SERVICE", "APP_SERVICE_STAGING"]),
    ("Application", ["APP_PORT", "APP_METRICS_PATH"]),
    ("Access", ["ADMIN_USER", "ADMIN_PASSWORD", "GRAFANA_ADMIN_USER", "GRAFANA_ADMIN_PASSWORD",
                "ALERT_WEBHOOK_URL", "CF_TUNNEL_TOKEN", "CI_SSH_PUBLIC_KEY"]),
    # Not shown, on purpose: GHCR_USER, GHCR_TOKEN, APP_IMAGE_PROD,
    # APP_IMAGE_STAGING, MONGO_URI_*, REDIS_PASSWORD_*.
    #
    # Every one of them is read once by bootstrap and never again, so the value
    # here stops matching reality the moment you change the real thing:
    #   registry auth   -> Apps > API > Deployments
    #   running image   -> Apps > API (shown live) / Deployments to change it
    #   Mongo URI       -> Apps > API > <env> > Environment
    #   Redis password  -> Apps > Redis > <env> > Credentials
    #
    # MONGO_URI_* and REDIS_PASSWORD_* are deliberately NOT shown. They exist in
    # infra.env only to seed config/app-<env>.env on first boot, and go stale the
    # moment you change either one in the panel. Displaying a value that no
    # longer matches what is running is worse than not displaying it.
    #   Mongo URI       -> Apps > API > <env> > Environment
    #   Redis password  -> Apps > Redis > <env> > Credentials
    # They keep their FIELDS entries below so that a hand-crafted POST is still
    # refused rather than silently written.
]

MASK_HINT = ("TOKEN", "PASSWORD", "SECRET", "KEY", "URI")


def describe(key):
    return FIELDS.get(key, (BOOT, None, "Not managed by the panel."))


def editable(key):
    return describe(key)[0] == EDIT


def stacks_for(keys):
    """Which stacks need redeploying for this set of changed keys."""
    out = []
    for k in keys:
        stack = describe(k)[1]
        if stack and stack not in out:
            out.append(stack)
    return out
