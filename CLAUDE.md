# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Infrastructure-as-config for a self-managing Docker Swarm on Hetzner Cloud. It is **not** an application
repo: there is no build system, no test suite, and no CI. The deliverable is a bundle of files copied to
`/opt/infra` on a manually-created master node, where `bootstrap.sh` (embedded in `master-cloud-init.yaml`)
turns them into a running cluster.

Read `README.md` first — it holds the operational rationale (scaling policy table, hostname scheme,
"things that will bite you"). This file covers what the README doesn't: how the pieces are wired.

## Working on this repo

There is nothing to run locally that exercises the real system — the autoscaler needs a Docker Swarm
manager socket, a VictoriaMetrics endpoint, and a Hetzner token. But the alerting layer *is* testable, and
should be tested; it is the only part of this repo with real unit tests:

```bash
promtool test rules config/alerts_test.yml     # alert behaviour — run after any alerts.yml change
promtool check rules config/alerts.yml
amtool check-config config/alertmanager.yml
amtool config routes test --config.file=config/alertmanager.yml alertname=Watchdog   # -> heartbeat

python3 -m py_compile autoscaler/autoscaler.py
docker build -t autoscaler:test autoscaler/

# The admin panel runs locally against dummy data — no docker, no cluster.
PREVIEW=1 ADMIN_PASSWORD=dev python3 admin/app.py     # http://localhost:3000
python3 admin/preview_build.py                        # -> admin/preview/index.html

# Stack files use ${VAR} interpolation from /etc/infra/infra.env; render before validating.
set -a; source /path/to/infra.env; set +a
envsubst < stacks/app.yml | docker compose -f - config
```

Match tool versions to the deployed ones (`promtool` 2.x, `amtool` 0.27.0) — `api_url_file` and the
`matchers` route syntax are version-sensitive. The bootstrap script is embedded in `master-cloud-init.yaml`
as a `write_files` entry, so syntax-check it by extracting the YAML value and running `bash -n` on it;
editing it in place is easy to get subtly wrong.

The only real integration test is a deploy. On the master:

```bash
ssh root@<master-ip> tail -f /var/log/infra-bootstrap.log
docker service logs -f monitoring_autoscaler
docker service ls && docker node ls
```

Set `DRY_RUN=true` in `infra.env` to make the autoscaler log the Hetzner create/delete calls it would
make without issuing them. This is the safe way to exercise a scaling-logic change against live metrics.

## Deployment flow

1. Fill the `VARIABLES` block at the top of `master-cloud-init.yaml` (`write_files` → `/etc/infra/infra.env`).
   Every `REPLACE_ME` must be set. This single env file is the source of truth for both stacks and the
   autoscaler.
2. Create a Hetzner CPX31 on the private network with that cloud-config. `runcmd` expects `stacks/`,
   `config/`, and `autoscaler/` to already be in `/opt/infra` — the clone line is commented out on purpose.
3. `bootstrap.sh` then: preflight-checks every required variable, firewalls, installs Docker,
   `docker swarm init` on the private IP, creates the `docker secret`s, seeds `config/app-*.env` from
   `infra.env` (existing files win, so panel edits survive a re-run), renders the templates, builds the
   `autoscaler` and `admin` images, and deploys `monitoring` → `app` (via `bin/app-env`) → `admin`.
4. No workers exist yet. The autoscaler creates the first one on its first loop; that is the proof the
   Hetzner token and worker cloud-init both work.

## Architecture invariants

These constraints span multiple files. Breaking one of them fails silently rather than loudly.

**Master runs no traffic.** Everything stateful is pinned with `node.role == manager` (Redis, the whole
monitoring stack, the autoscaler). Everything in the request path is pinned to `node.role == worker`
(`api-prod`, `api-staging`, `cloudflared`). Losing the master costs scaling and observability, not uptime.

**`monitoring` is an external overlay network.** `stacks/monitoring.yml` creates it; `stacks/app.yml`
declares it `external: true`. Monitoring must be deployed first or the app stack fails to deploy.

**Env-file values are duplicated by hand into three places.** A new autoscaler tunable must be added to
(a) the `infra.env` block in `master-cloud-init.yaml`, (b) `x-autoscaler-env` in `stacks/monitoring.yml`,
and (c) an `_env(...)` call in `autoscaler.py`. Missing (b) means the container silently uses the default
in (c).

**`APP_CPU_LIMIT` must equal the `cpus` limit on `api-prod` in `stacks/app.yml`.** It is the denominator
of `CPU_REPLICA_EXPR`. Change one without the other and the secondary scaling signal misreports with no
error.

**`REPLICAS_PER_WORKER` encodes the worker shape.** It assumes CPX21 (3 vCPU) with a 1.0 CPU limit per
replica. Changing `WORKER_TYPE` without changing it produces either wasted nodes or permanently unplaceable
tasks.

**Service discovery is label-driven, never enumerated.** `config/vmagent.yml.tpl` keeps any Swarm task
carrying `prometheus.scrape/port/path` deploy labels, and `node-exporter`/`cadvisor` run `mode: global`.
Adding a scraped service means adding those three labels, not editing the scrape config. The
`app.env` deploy label becomes the `env` metric label the autoscaler's `P95_EXPR` filters on.

**Logs bypass any agent.** Both stacks use the `loki` Docker log driver plugin pointed at
`http://${MASTER_PRIVATE_IP}:3100`, which is why Loki publishes port 3100 in `mode: host` and why the
plugin is installed in both cloud-inits.

**Service names are distinct on purpose.** `api-prod` and `api-staging` share the `edge` network with
cloudflared; identically-named services would make overlay DNS ambiguous and round-robin between
environments. Stack-qualified names (`app_api-prod`) are what `APP_SERVICE` and `docker service update`
take.

**The autoscaler is stateless and horizontal-only.** Cooldowns come from Hetzner server creation
timestamps; sustain windows come from VictoriaMetrics subqueries (`sustained()`). Restarting the container
loses nothing. It never rescales a server — Hetzner rescale power-cycles the box and a grown disk cannot
shrink.

**The autoscaler's blast radius is the Hetzner label selector** `cluster==<APP_NAME>,role==swarm-worker`.
`reap_orphans()` deletes servers matching it that never joined the swarm. Anything else in the same Hetzner
project is protected only by that label.

## Autoscaler control flow

`autoscaler/autoscaler.py` is a single-file loop (`loop()`, every `LOOP_SECONDS`) with a strict order:

1. `reap_orphans()` — delete Hetzner servers that never joined, and swarm nodes whose server is gone.
2. `read_signals()` — p95 latency (primary), CPU-per-replica (secondary), node CPU/mem (placement guard only).
3. `desired_replicas()` — tier 1. Scale up if *either* signal is sustained high over `SUSTAIN_UP`; scale
   down only if *both* stayed low over `SUSTAIN_DOWN`.
4. `workers_needed()` — tier 2. `ceil(replicas / REPLICAS_PER_WORKER)`, plus one if node pressure exceeds
   `NODE_PRESSURE_PCT`.
5. Apply in order: **create nodes → adjust replicas → remove nodes**. Scale-down of a node requires that
   replicas have already been shed, drains for up to 180s, then waits `POST_DRAIN_GRACE` for cloudflared to
   release its edge connections.

Node CPU is deliberately never a scaling trigger — it averages in exporters, cloudflared and staging.
CPU-per-replica exists because `histogram_quantile` over an empty bucket returns nothing, so at low traffic
the latency signal vanishes entirely and something must authorise scaling back down.

Removal is LIFO (`pick_removal_candidate`): newest node first, since it is least likely to hold warm state
and Hetzner bills hourly.

## There is exactly one deploy path: `bin/app-env`

Never suggest a bare `docker stack deploy` for the app stack, and never add one to a script. It is
declarative over the whole stack and silently overwrites live state owned by other things
([docker/cli#2235](https://github.com/docker/cli/issues/2235)): `deploy.replicas: ${MIN_REPLICAS}` resets
the autoscaler's replica count to 2, and `image: ${APP_IMAGE_PROD}` rolls back whatever CI last deployed.
`bin/app-env deploy` reads both from the live service and pins them for the deploy; bootstrap uses it too,
falling back to `infra.env` when nothing is running yet.

Application config lives in `config/app-{prod,staging}.env` and is layered on as a second `-c` override
file using plain `environment:` mappings, which
[merge key-by-key](https://docs.docker.com/reference/compose-file/merge/) with the base — so `KTOR_ENV`
and the other stack-set values survive. `env_file:` is deliberately **not** used: it has regressed in
`docker stack deploy` before ([docker/cli#4952](https://github.com/docker/cli/issues/4952)) and this host
installs whatever Docker `get.docker.com` currently ships.

One file feeds several services: `MAP_PROD`/`MAP_STAGING` in `bin/app-env` send every key to the API but
only `REDIS_PASSWORD` to that environment's Redis and exporter, so the server and its clients cannot
drift apart. `MONGO_URI` and `REDIS_PASSWORD` are plain env vars rather than docker secrets — a
deliberate trade so the panel can edit them, documented in the README. Infrastructure credentials
(tunnel token, Hetzner token, Grafana, alert webhook, admin login) are still docker secrets.

## The admin panel is a root console

`admin/` is a Flask app pinned to the manager with the docker socket mounted. It can redeploy
production and read every application credential, so changes to it are security changes:

- Every mutating route is POST + CSRF-checked (`auth.check_csrf`). Never add a GET that mutates.
- Credentials arrive as docker secrets, never from the image or compose file. Login comparison is
  constant-time with a per-process PBKDF2 salt, and six failures lock the source address out.
- Gunicorn runs **one worker on purpose** — the lockout counter lives in memory, so a second worker
  would hand an attacker a second lockout budget.
- The panel writes `config/app-*.env` and then shells out to `bin/app-env`. It must never call
  `docker stack deploy` itself.
- It shows stack-owned and cloud-init-owned values **read-only**. A UI that accepts an edit the next
  deploy reverts is worse than one that declines; keep that boundary.

`admin/preview_build.py` renders the real templates against `fixtures.py` into a single
self-contained `admin/preview/index.html`. Keep fixtures in the same shape as `swarm.py` — that
shared shape is what stops the preview from drifting from the live UI. Rebuild it after template
changes.

## Alerting must fail loudly

The alerting path was rebuilt after all three of its parts turned out to be inert. Keep these properties:

- **`ALERT_WEBHOOK_URL` is required.** The bootstrap preflight rejects unset/`REPLACE_ME` values before
  installing anything, and Alertmanager reads the URL from a docker secret via `api_url_file`, so it
  refuses to start rather than dropping alerts. Never reintroduce a placeholder receiver.
- **Absent series must alert.** `sum(...) < 1` returns *no series* when every replica is gone, and no
  series never fires — that is the outage the rule exists for. Use `(sum(...) or vector(0)) < 1`.
- **Label selectors in `alerts.yml` drift.** `service=` is the Swarm name (`app_api-prod`), which must
  track `APP_SERVICE`. This is what made `NoHealthyReplicas` dead for months.
- **`Watchdog` fires permanently by design** and is routed to its own 24h receiver. Its silence is the
  signal; do not "fix" it.

Guard all of this with `promtool test rules config/alerts_test.yml` before changing `alerts.yml`.

**Alert thresholds are never literals.** Every threshold that mirrors a config value compares against an
exported gauge (`autoscaler_max_replicas`, `autoscaler_max_workers`). Adding a tunable that an alert keys
on means adding a gauge for it in `autoscaler.py` too.

## Remaining known gap: secret rotation

There is no way to change a secret. `mk_secret` in `bootstrap.sh` is `docker secret create ... || true`,
so re-running bootstrap with a new `REDIS_PASSWORD_PROD` in `infra.env` silently does nothing — and Swarm
secrets are immutable, so it could not update in place anyway. Rotating one today means creating a
versioned secret (`REDIS_PASSWORD_prod_v2`), editing `stacks/app.yml` to reference it, and redeploying.

This is worth knowing before assuming any UI can manage app secrets: the constraint is Swarm's, not the
tool's.
