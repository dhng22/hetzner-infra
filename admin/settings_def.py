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
    "WORKER_MAX_CORES": (EDIT, "monitoring",
                         "Vertical scaling ceiling. A worker may be grown onto a bigger plan "
                         "instead of a second worker being bought — up to this many cores. "
                         "0 turns vertical scaling off. Both this and the memory ceiling must "
                         "be set, because half a ceiling is no ceiling."),
    "WORKER_MAX_MEMORY_GB": (EDIT, "monitoring",
                             "The other half of the vertical ceiling, in GB. Stated as a "
                             "CAPACITY rather than a plan name on purpose: Hetzner adds and "
                             "retires plans, and the same family is not sold in every "
                             "location, so the ladder is read from the API and simply cut "
                             "off here."),
    "NODE_RESIZE_COOLDOWN_SECONDS": (EDIT, "monitoring",
                                     "How long after one worker resize before another may "
                                     "start. Long on purpose: a resize power-cycles a machine, "
                                     "so it corrects the shape of the fleet slowly while the "
                                     "replica count handles traffic."),

    # --- registry -------------------------------------------------------------
    # Both are read exactly once, by bootstrap. Registry auth afterwards lives in
    # the docker config the panel manages (Apps > API > Deployments). Editing
    # these later changes nothing you can observe, so the panel does not offer to.
    "GHCR_USER": (BOOT, None, "First `docker login` only. Manage registry auth under Apps > API > Deployments."),
    "GHCR_TOKEN": (BOOT, None, "First `docker login` only."),
    # APP_IMAGE_PROD / APP_IMAGE_STAGING used to live here. They are no longer in
    # infra.env at all: the services are created on a locally built placeholder
    # and the first real deploy happens under Apps > API > Deployments, so the
    # running image has exactly one home and it is the live service spec.

    # --- fleet ---------------------------------------------------------------
    # Cluster-wide only. Per-application policy (SLO, replica bounds, sustain
    # windows, CPU thresholds) is not here and never will be: it belongs to the
    # component, is carried as deploy labels on its own service, and is edited
    # on that component's page. A copy in this file would be a copy that goes
    # stale, which is the mistake this refactor exists to remove.
    "NODE_PRESSURE_PCT": (EDIT, "monitoring",
                          "Placement guard, never a trigger: above this the autoscaler asks for "
                          "one host more than the capacity arithmetic alone would."),
    "MIN_WORKERS": (EDIT, "monitoring",
                    "Hetzner workers, and the master is not one of them. 0 means none are "
                    "billed and the master carries the load itself. 1 or more means the "
                    "master never runs application traffic at all."),
    "MAX_WORKERS": (EDIT, "monitoring",
                    "A budget cap, not a capacity plan. The most Hetzner workers that may "
                    "exist at once, on top of the master."),
    "COOLDOWN_UP_SECONDS": (EDIT, "monitoring",
                            "Must exceed node boot + image pull + app warmup, or the loop "
                            "provisions again while the last node is still warming and badly "
                            "overshoots."),
    "COOLDOWN_DOWN_SECONDS": (EDIT, "monitoring",
                              "Slow on purpose: a deleted node costs a full boot to get back."),
    "SCHEDULE_FLOOR": (EDIT, "monitoring",
                       "UTC, `HH:MM-HH:MM=N`, comma separated. Blank disables it."),
    "DRY_RUN": (EDIT, "monitoring",
                "`true` makes the autoscaler log every action without taking it."),

    # --- access ------------------------------------------------------------
    "GRAFANA_ADMIN_USER": (EDIT, "monitoring", "Grafana only applies this on first start."),
    "ADMIN_USER": (SECRET, "admin", "Stored as a docker secret alongside the password."),
    "ADMIN_PASSWORD": (SECRET, "admin", "The key to this panel."),
    "GRAFANA_ADMIN_PASSWORD": (SECRET, "monitoring", ""),
    # Editable, not a docker secret: the destination has to be changeable, and a
    # Swarm secret cannot be. Saving either of these re-renders
    # config/alertmanager.yml and redeploys monitoring, which is the whole
    # reason bin/stack-deploy does the rendering rather than bootstrap.
    "ALERT_TELEGRAM_BOT_TOKEN": (EDIT, "monitoring",
                                 "From @BotFather. Rendered into a root-only config file on "
                                 "the master, never into a container's environment."),
    "ALERT_TELEGRAM_CHAT_ID": (EDIT, "monitoring",
                               "The group or channel to post in. Negative for groups; get it "
                               "from /getUpdates after sending one message there. Leave both "
                               "blank and alerts are generated and dropped."),
    "CF_TUNNEL_TOKEN": (SECRET, "ingress", "Rotating this means re-issuing the connector token in Cloudflare."),
    "HCLOUD_TOKEN": (SECRET, "monitoring", "Lets the autoscaler create and delete servers."),
    "CI_SSH_PUBLIC_KEY": (BOOT, None, "The deploy user is created at boot. Edit authorized_keys on the master."),
    # MONGO_URI_* and REDIS_PASSWORD_* used to live here too, for the same reason
    # and with the same problem. They are gone from infra.env: config/app-<env>.env
    # is created at first boot with an empty MONGO_URI and a generated
    # REDIS_PASSWORD, and the panel is the only thing that edits it afterwards.
    # An unlisted key is refused by describe() anyway, so a hand-crafted POST
    # naming one still cannot write it.
}

GROUPS = [
    ("Identity", ["APP_NAME", "ROOT_DOMAIN"]),
    ("Hetzner", ["HCLOUD_LOCATION", "HCLOUD_NETWORK_NAME", "HCLOUD_SSH_KEY_NAME",
                 "WORKER_IMAGE", "WORKER_TYPE", "HCLOUD_TOKEN"]),
    ("Fleet", ["MIN_WORKERS", "MAX_WORKERS", "WORKER_MAX_CORES", "WORKER_MAX_MEMORY_GB",
               "NODE_RESIZE_COOLDOWN_SECONDS",
               "NODE_PRESSURE_PCT", "COOLDOWN_UP_SECONDS",
               "COOLDOWN_DOWN_SECONDS", "SCHEDULE_FLOOR", "DRY_RUN"]),
    ("Access", ["ADMIN_USER", "ADMIN_PASSWORD", "GRAFANA_ADMIN_USER", "GRAFANA_ADMIN_PASSWORD",
                "ALERT_TELEGRAM_BOT_TOKEN", "ALERT_TELEGRAM_CHAT_ID",
                "CF_TUNNEL_TOKEN", "CI_SSH_PUBLIC_KEY"]),
    # Not shown, on purpose: GHCR_USER and GHCR_TOKEN.
    #
    # Both are read once by bootstrap and never again, so the value here stops
    # matching reality the moment you change the real thing:
    #   registry auth   -> Apps > API > Deployments
    #
    # The other four that used to be hidden here — the first image, the Mongo URI
    # and the two Redis passwords — are not in infra.env any more at all, which
    # is a better answer than showing a copy that goes stale:
    #   running image   -> Apps > API (shown live) / Deployments to change it
    #   Mongo URI       -> Apps > API > <env> > Environment
    #   Redis password  -> Apps > Redis > <env> > Credentials
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
