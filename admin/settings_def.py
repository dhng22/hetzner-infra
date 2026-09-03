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
    # --- panel -------------------------------------------------------------
    # Where this master updates ITSELF from. No stack to redeploy: nothing
    # holds a copy of these — `bin/infra-update` re-reads infra.env on every
    # tick of its timer, so a save takes effect on the next poll and no
    # container needs to be restarted to notice.
    "INFRA_REPO_URL": (EDIT, None,
                       "The repository the master polls, and the GHCR namespace its images "
                       "are pulled from. A PRIVATE-LOOKING URL IS USUALLY REQUIRED EVEN FOR "
                       "A PUBLIC REPO: GitHub serves the ref advertisement to anyone but "
                       "answers the fetch itself with 401 for an unauthenticated request "
                       "from a cloud IP, and roughly one attempt in six slips through — so "
                       "the cluster updates just often enough to look alive. Put the token "
                       "in as the PASSWORD: https://x-access-token:TOKEN@github.com/you/repo"
                       ".git. A bare https://TOKEN@github.com/... is userinfo with no "
                       "password, which git reads as a username and then asks for a password "
                       "no timer job can answer; that form is repaired on the way past, but "
                       "only once this master is running the commit that repairs it. Leave "
                       "it empty and the cluster simply stops self-updating, which is a "
                       "supported state. Whether the last poll worked is on the left rail, "
                       "under the theme button, on every page."),
    "INFRA_REPO_BRANCH": (EDIT, None,
                          "Which branch the master follows. Changing it moves the cluster to "
                          "that branch's head on the next poll, which is a deploy of "
                          "everything that differs — not a setting to try out on a whim."),

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

    # --- databases ------------------------------------------------------------
    # Dataguard's cluster-wide policy. Everything PER COMPONENT — how many
    # members, the lag budget, whether reads may go to secondaries, where the
    # backups go — is not here and never will be: it belongs to the component,
    # travels as deploy labels on its own services, and is edited on that
    # component's page. A copy here would be a copy that goes stale, which is the
    # mistake the whole component model exists to remove.
    "DB_MAX_CORES": (EDIT, "monitoring",
                     "The biggest machine a database member may end up on, in cores. This "
                     "is the ceiling the whole upgrade path is bounded by: under sustained "
                     "write or capacity pressure dataguard starts a member on the next plan "
                     "up, promotes it and drops the old one, and it stops when the next "
                     "plan up would exceed this. Without it that loop has no end."),
    "DB_MAX_MEMORY_GB": (EDIT, "monitoring",
                         "The other half of the database ceiling, in GB. Separate from the "
                         "worker ceiling because the mechanisms differ: a worker is grown by "
                         "power-cycling it, which is why that is opt-in, while a database "
                         "member is replaced by a bigger one that has already synced. A "
                         "capacity rather than a plan name, for the same reason as the "
                         "worker ceiling — the ladder is read from the Hetzner API."),
    "DB_MAX_STORAGE": (EDIT, "monitoring",
                       "IN MEGABYTES — the default of 655360 is 640 GB. The third "
                       "dimension of the database ceiling, and unlike the two above it "
                       "this one does not carry its unit in its name, so read the "
                       "number: a value in the low hundreds is a ceiling below the "
                       "smallest plan's own disk, which leaves dataguard nothing to "
                       "grow onto at all. Disk is not like CPU or memory either — too "
                       "little of those makes a database slow, too little disk stops "
                       "it, and on a plan ladder the disk arrives welded to the plan "
                       "and cannot be topped up afterwards. Dataguard will not move a "
                       "member onto a plan whose disk exceeds this. The default is the "
                       "disk of the largest plan the cores and memory ceilings already "
                       "allow, so out of the box it bounds nothing they did not: lower "
                       "it when storage is the cost you are watching."),
    "DATAGUARD_DRY_RUN": (EDIT, "monitoring",
                          "Every decision is logged and nothing is applied. A rehearsal, and "
                          "the honest way to find out what it wants to do to a live database."),
    "TOPOLOGY_COOLDOWN_SECONDS": (EDIT, "monitoring",
                                  "How long after one topology change before another may start. "
                                  "HOURS, not the autoscaler's ninety seconds: an initial sync "
                                  "reads the whole dataset off a live member, and a loop that "
                                  "reacts faster than the thing it controls oscillates."),
    "PRESSURE_SUSTAIN_SECONDS": (EDIT, "monitoring",
                                 "How long pressure must hold before it counts. Long, because "
                                 "the cheapest response to a database being busy costs an hour."),
    "BACKUP_MAX_AGE_SECONDS": (EDIT, "monitoring",
                               "How old a VERIFIED backup may be before dataguard refuses to "
                               "change a database's shape. A topology change can lose data."),
    "VIEWER_IDLE_SECONDS": (EDIT, "monitoring",
                            "How long a data visualiser stays up after the last time somebody "
                            "looked at it. It is full access to your data, so the shorter it "
                            "exists the smaller that surface is."),

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
    "ADMIN_USER": (SECRET, "admin", "Stored as a docker secret alongside the password."),
    "ADMIN_PASSWORD": (SECRET, "admin",
                       "The key to this panel, and to Grafana: the dashboards are served "
                       "through this panel behind this login, so they no longer keep an "
                       "admin password of their own."),
    # Editable, not a docker secret: the destination has to be changeable, and a
    # Swarm secret cannot be. Saving either of these re-renders
    # config/alertmanager.yml and redeploys monitoring, which is the whole
    # reason bin/stack-deploy does the rendering rather than bootstrap.
    "CF_TUNNEL_TOKEN": (SECRET, "ingress", "Rotating this means re-issuing the connector token in Cloudflare."),
    "HCLOUD_TOKEN": (SECRET, "monitoring", "Lets the autoscaler create and delete servers."),
    # MONGO_URI_* and REDIS_PASSWORD_* used to live here too, for the same reason
    # and with the same problem. They are gone from infra.env: config/app-<env>.env
    # is created at first boot with an empty MONGO_URI and a generated
    # REDIS_PASSWORD, and the panel is the only thing that edits it afterwards.
    # An unlisted key is refused by describe() anyway, so a hand-crafted POST
    # naming one still cannot write it.
}

GROUPS = [
    # First, because it is the one group that governs whether any of the others
    # can ever change: everything below is applied by a master that is only as
    # current as this section lets it be.
    ("Panel", ["INFRA_REPO_URL", "INFRA_REPO_BRANCH"]),
    ("Identity", ["APP_NAME", "ROOT_DOMAIN"]),
    ("Hetzner", ["HCLOUD_LOCATION", "HCLOUD_NETWORK_NAME", "HCLOUD_SSH_KEY_NAME",
                 "WORKER_IMAGE", "HCLOUD_TOKEN"]),
    ("Fleet", ["MIN_WORKERS", "MAX_WORKERS", "WORKER_MAX_CORES", "WORKER_MAX_MEMORY_GB",
               "NODE_RESIZE_COOLDOWN_SECONDS",
               "NODE_PRESSURE_PCT", "COOLDOWN_UP_SECONDS",
               "COOLDOWN_DOWN_SECONDS", "SCHEDULE_FLOOR", "DRY_RUN"]),
    ("Dataguard", ["DATAGUARD_DRY_RUN", "DB_MAX_CORES", "DB_MAX_MEMORY_GB",
                   "DB_MAX_STORAGE", "TOPOLOGY_COOLDOWN_SECONDS",
                   "PRESSURE_SUSTAIN_SECONDS", "BACKUP_MAX_AGE_SECONDS",
                   "VIEWER_IDLE_SECONDS"]),
    ("Access", ["ADMIN_USER", "ADMIN_PASSWORD", "CF_TUNNEL_TOKEN"]),
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

# What a setting is worth when infra.env does not carry it.
#
# `/etc/infra/infra.env` is written ONCE, by cloud-init, and nothing adds a key
# to it afterwards. Before this mapping existed the settings page skipped any
# key that file did not already have, so a knob introduced after a cluster was
# built was not merely unset on it — it was invisible, and there was no way to
# set it from the panel at all. The repo therefore carries the default and the
# file carries only this cluster's ANSWERS, which is the same split the reader
# side makes: `_env()` in the autoscaler resolves default-first and treats an
# empty value as an absent one.
#
# Only settings that HAVE a sane default belong here. A password or a token does
# not, and showing an invented value for one would be worse than leaving the row
# out, so a key with neither a live value nor a default is still skipped.
#
# Pinned against the cloud-init and against the autoscaler's own `_env` defaults
# by test_defaults_agree_everywhere — three readers of one number is exactly the
# drift this project keeps getting bitten by.
DEFAULTS = {
    "MIN_WORKERS": "0",
    "MAX_WORKERS": "5",
    "WORKER_MAX_CORES": "8",
    "WORKER_MAX_MEMORY_GB": "16",
    "NODE_RESIZE_COOLDOWN_SECONDS": "900",
    "NODE_PRESSURE_PCT": "80",
    "COOLDOWN_UP_SECONDS": "300",
    "COOLDOWN_DOWN_SECONDS": "900",
    "SCHEDULE_FLOOR": "",
    "DRY_RUN": "false",

    "DATAGUARD_DRY_RUN": "false",
    "DB_MAX_CORES": "16",
    "DB_MAX_MEMORY_GB": "32",
    "DB_MAX_STORAGE": "655360",
    "TOPOLOGY_COOLDOWN_SECONDS": "14400",
    "PRESSURE_SUSTAIN_SECONDS": "3600",
    "BACKUP_MAX_AGE_SECONDS": "86400",
    "VIEWER_IDLE_SECONDS": "900",
}

# Which settings the infrastructure containers are allowed to receive.
#
# `bin/render-fleet-env` turns this into /etc/infra/fleet.env and
# stacks/monitoring.yml hands that file to the OVERSEER and to DATAGUARD with
# `env_file`. That is why adding a fleet setting no longer means editing the
# stack: the stack names a FILE, and the file is generated from this list.
#
# One file, several readers. Each process ignores what it does not read, which
# is why a dataguard setting arriving at the overseer costs nothing — and why
# the test that pins this scans all three sources rather than one.
#
# Stated as GROUPS and a mode filter rather than as a list of key names, because
# a hand-kept list is one more place to forget. Everything in these groups goes
# except SECRET-mode keys — an ALLOW-list, so a credential added to Access later
# cannot reach the autoscaler by having been overlooked. HCLOUD_TOKEN is the one
# credential it does need and it arrives as a docker secret, not through here.
AUTOSCALER_ENV_GROUPS = ("Identity", "Hetzner", "Fleet", "Dataguard")


def autoscaler_env(values):
    """The settings the autoscaler gets, resolved default-first, in group order."""
    out = {}
    for title, keys in GROUPS:
        if title not in AUTOSCALER_ENV_GROUPS:
            continue
        for key in keys:
            if describe(key)[0] == SECRET:
                continue
            if key in values:
                out[key] = values[key]
            elif key in DEFAULTS:
                out[key] = DEFAULTS[key]
    return out


# Which keys are hidden behind a Reveal rather than printed on the page.
#
# Name-based, and "URL" earns its place the same way "URI" did: a URL that
# reaches this file is nearly always one carrying a credential inside it — a
# repo token, a webhook secret, a connection string. Masking one that does not
# costs a click; printing one that does puts a credential on a screen somebody
# is sharing.
MASK_HINT = ("TOKEN", "PASSWORD", "SECRET", "KEY", "URI", "URL")


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
