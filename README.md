# Self-managing Swarm on Hetzner

One master node created by hand. After that, workers and replicas scale
themselves against a latency SLO, CI deploys itself, and monitoring picks up
anything new without configuration.

## Architecture

```
                     Cloudflare — ONE tunnel, four hostnames
                                     │
   ┌─────────────────────────────────┴──────────────────────────────┐
   │  WORKER POOL — autoscaled 1..MAX, CPX21                        │
   │    cloudflared (global: one connector per worker)              │
   │    api-prod    (N replicas, autoscaled)                        │
   │    api-staging (1 replica, capped at 0.5 CPU)                  │
   │    node-exporter + cadvisor (global)                           │
   └─────────────────────────────────┬──────────────────────────────┘
            creates, drains          │        metrics, logs
   ┌─────────────────────────────────┴──────────────────────────────┐
   │  MASTER — manual, CPX31, OUTSIDE the request path              │
   │    admin panel · autoscaler                                    │
   │    VictoriaMetrics · Loki · Grafana · vmagent                  │
   │    redis-prod · redis-staging                                  │
   └────────────────────────────────────────────────────────────────┘
                                     │
                          MongoDB Atlas (prod + staging DBs)
```

Networks: `edge` (cloudflared + both api services), `data-prod`,
`data-staging` (each api to its own Redis), `monitoring`.

## Hostnames

Cloudflare's free Universal SSL covers `*.root` and nothing deeper, so every
name stays at the third level and extra services take a dash:

| Hostname | Tunnel target |
|---|---|
| `<app>.<root>` | `http://api-prod:8080` |
| `staging-<app>.<root>` | `http://api-staging:8080` |
| `grafana-<app>.<root>` | `http://grafana:3000` |
| `admin-<app>.<root>` | `http://<master-private-ip>:3000` |

Put the last two behind Cloudflare Access. They control your cluster.

## Scaling policy

Load is absorbed cheapest-first:

1. **Replicas** — seconds. Driven by p95 latency against `SLO_P95_MS`, with
   CPU-per-replica as the secondary signal.
2. **Nodes** — ~2 minutes. Only when the replicas we want will not fit.

Coming down, the order reverses: shed replicas first, remove the node later.

| Setting | Default | Why |
|---|---|---|
| `SLO_P95_MS` | 500 | **The** number. Everything derives from it. Set it from your real latency distribution. |
| `SCALE_UP_P95_RATIO` | 0.8 | Act at 80% of SLO, before users feel it |
| `SCALE_UP_CPU` | 70 | % of one replica's own limit — not node CPU |
| `NODE_PRESSURE_PCT` | 80 | Placement guard: another replica will not fit |
| `SUSTAIN_UP_SECONDS` | 90 | Up fast |
| `SUSTAIN_DOWN_SECONDS` | 900 | Down slow. Never symmetric. |
| `SCALE_UP_FACTOR` | 0.5 | +50% of current, min +1. One at a time cannot track a spike. |
| `COOLDOWN_UP_SECONDS` | 300 | Must exceed boot + pull + JVM warmup, or you overshoot |
| `MAX_WORKERS` | 6 | A **budget** cap, not a capacity plan |

Why CPU is still there when the policy is latency-led: `histogram_quantile`
over an empty bucket returns nothing. At low traffic the latency signal
disappears entirely, and CPU-per-replica is what tells the scaler it is safe
to come back down.

## Deploying an image

Your pipeline pushes an image and tells the panel to move the service to it:

```
POST https://admin-<app>.<root>/hooks/deploy/app/prod
     X-Deploy-Token: <from the panel, Apps -> API -> Deployments>
     {"image": "ghcr.io/you/app:prod-9f3ac21"}
```

A complete GitHub Actions workflow is in `docs/github-actions-deploy.yml`.
The panel's token, endpoint and a ready-made `curl` are on the Deployments tab.

Four properties you get, whatever pipeline you build:

- **Zero downtime** — `order: start-first` brings the new task to healthy
  before stopping the old one.
- **Real health gating** — `monitor: 90s` with `start_period: 60s`. Swarm's
  default monitor is 5 seconds, shorter than JVM startup, so it would call a
  task successful before your app finished booting.
- **Automatic rollback** — `failure_action: rollback`.
- **A failed deploy fails your pipeline** — the webhook blocks until Swarm has
  converged and answers `502` when it rolled back. That only holds if your
  pipeline lets curl's failure propagate: `|| true` or `|| echo` turns every
  broken deploy into a green tick.

**Use immutable tags.** The panel rejects `latest` and the `prod-latest`
convention. `docker service update` normally resolves a tag to a digest and
notices when it moved, but if it cannot reach the registry Docker warns and
proceeds with the unchanged reference — a no-op reported as success. A tag that
never moves removes the question, and it is also the only way to know what is
running or roll back to a specific build. Push `prod-latest` for humans if you
like; deploy `prod-<sha>`.

Deploys are also possible by hand, on the master:

```bash
docker service update --with-registry-auth \
  --image ghcr.io/you/app:prod-9f3ac21 app_api-prod
docker service rollback app_api-prod                    # undo
```

`--with-registry-auth` is not optional: it ships the registry credential with
the service spec, so a worker created an hour from now can still pull.

## Before you start

Five things to have open in other tabs:

1. **Hetzner** — a private network `10.0.0.0/16`, and a **Read & Write** API token.
2. **Cloudflare** — one Tunnel; copy the connector token. Hostnames come later.
3. **GitHub** — a PAT with `read:packages`, for the cluster to pull your image.
4. **MongoDB Atlas** — two databases, prod and staging.
5. **Slack** — an incoming webhook (or a Discord one with `/slack` appended).
   Bootstrap refuses to start without it.

Optional: an SSH public key for `CI_SSH_PUBLIC_KEY` if you want a non-root
`deploy` user on the master. The panel does not need it.

## Admin panel

`admin-<app>.<root>` is a web console for the cluster: service health, node
state, autoscaler signals, alert rules, and an environment editor per app and
per environment. Username and password come from `master-cloud-init.yaml` —
still the only file you fill in.

It is deliberately narrow about what it owns:

| | Owner |
|---|---|
| Application configuration (`config/app-<env>.env`) | **the panel**, or `app-env` |
| Service spec: placement, resources, update policy | `stacks/app.yml` — panel shows it read-only |
| `api-prod` replica count | the autoscaler |
| Scaling policy, Hetzner, registry, alerting | `master-cloud-init.yaml` — panel shows it read-only |

That split is the whole point. A panel that lets you edit a value the next
`docker stack deploy` silently reverts is worse than one that declines.

**Treat it as a root console.** It mounts the docker socket, so it can redeploy
production and read every application credential. Put the hostname behind
Cloudflare Access; the login is one factor, Access is the second. Six failed
attempts locks the source address out for five minutes.

To preview the interface without deploying anything:

```bash
python3 admin/preview_build.py && open admin/preview/index.html
```

## Application configuration

Everything the app reads with `System.getenv` lives in `config/app-prod.env`
and `config/app-staging.env`, edited in the panel or on the box:

```bash
app-env list prod
app-env set FEATURE_NEW_CHECKOUT=true prod     # edits the file, then redeploys
app-env unset FEATURE_NEW_CHECKOUT prod
app-env deploy                                 # re-apply files, change nothing else
```

`REDIS_PASSWORD` is injected into the API, the matching Redis, and the exporter
from that one file, so the server and its clients can never drift apart.
`KTOR_ENV`, `REDIS_HOST` and `REDIS_PORT` come from the stack file and are
refused here.

> **Credentials are plain env vars, not docker secrets.** `MONGO_URI` and
> `REDIS_PASSWORD` are in these files so the panel can edit them. The cost:
> they appear in `docker service inspect`, in the service spec on every worker
> that runs a task, and in the container's environment. Swarm secrets cannot be
> updated in place, so making them editable meant giving that up. If you would
> rather have secrets back, move them to `docker secret` + the `*_FILE`
> convention and accept that rotation is a versioned-secret dance.

**Always deploy through `app-env`, never a bare `docker stack deploy`.** The
stack file is declarative over the whole stack, so a plain redeploy overwrites
two things it does not own:

- **replicas** — the file says `${MIN_REPLICAS}`, so a redeploy at peak cuts
  `api-prod` from N to 2. The autoscaler climbs back in +50% steps on a 60s
  cooldown, so you spend minutes under capacity because you changed a log level.
- **image** — the file says `${APP_IMAGE_PROD}`, the tag pinned at bootstrap.
  CI has moved on. A plain redeploy rolls production back to the first-boot
  image.

`app-env` reads both from the running service and pins them for the deploy.

## Things that will bite you

- **`SLO_P95_MS` is the whole policy.** If your real p95 already exceeds it,
  the scaler runs to the ceiling on day one and `ReplicaCeiling` fires. Set it
  before first boot.
- **`APP_CPU_LIMIT` must match `stacks/app.yml`.** It is the denominator for
  CPU-per-replica. Change the limit in one place only and the signal silently
  misreports.
- **`REPLICAS_PER_WORKER=2` assumes CPX21 and a 1.0 CPU limit.** Change the
  worker type without changing this and you will either waste nodes or queue
  unplaceable tasks.
- **Staging shares the pool.** Capped at 0.5 CPU, so idle staging is noise. A
  real load test is not — it will move production. Cap it or expect a worker.
- **The master is a single point of failure for control, not traffic.** Enable
  Hetzner backups; Redis AOF and metrics history live only there.
- **Redis has no HA.** `everysec` AOF loses up to a second on a hard crash.
  Fine for cache and sessions, not as a source of truth.
- **Scale-down drains for at most 180s** before removing the node anyway. Long
  requests need that raised in `autoscaler.py`.
- **A silent alert rule is worse than no alert rule.** `NoHealthyReplicas` once
  matched a service name that no longer existed, so it could never fire and
  looked identical to "nothing is wrong". `config/alerts_test.yml` now pins that
  behaviour; run `promtool test rules config/alerts_test.yml` after touching
  `alerts.yml`. The `Watchdog` rule fires permanently on purpose — if the daily
  heartbeat stops arriving, the pipeline is broken, not quiet.

## Bring it up, in order

1. **Put this repo somewhere private** and uncomment the clone line in
   `master-cloud-init.yaml` (`runcmd`), filling in your token. See the comment
   there — it strips `.git` before copying so your token is not left on disk.
2. **Fill every `REPLACE_ME`** in the `VARIABLES` block. Two matter more than
   the rest: **`SLO_P95_MS`**, which the entire scaling policy derives from, and
   **`ADMIN_PASSWORD`**, which is the key to the cluster. Bootstrap refuses to
   run while any of them is unset.
3. **Create the master**: Hetzner CPX31, Ubuntu 24.04, **attached to the private
   network**, with the cloud-config pasted in.
4. **Watch it build**:
   ```bash
   ssh root@<ip> tail -f /var/log/infra-bootstrap.log
   ```
5. **Wait for the first worker.** No workers exist until the autoscaler's first
   loop — that moment is your proof the Hetzner token and the worker cloud-init
   both work:
   ```bash
   docker service logs -f monitoring_autoscaler
   docker node ls
   ```
6. **Add the four tunnel hostnames** in Cloudflare, once a worker has joined:

   | Hostname | Target |
   |---|---|
   | `<app>.<root>` | `http://api-prod:8080` |
   | `staging-<app>.<root>` | `http://api-staging:8080` |
   | `grafana-<app>.<root>` | `http://grafana:3000` |
   | `admin-<app>.<root>` | `http://<master-private-ip>:3000` |

7. **Put `admin-` and `grafana-` behind Cloudflare Access.** The panel holds the
   docker socket and can read your Hetzner token, so its password is one factor
   and Access is the second. **Exempt `/hooks/deploy/*`** or give CI an Access
   service token, or your pipeline will be blocked before it reaches the panel.
8. **Prove it works** — do not skip this:
   ```bash
   /opt/infra/bin/smoke-test --deep
   ```
   It checks the things that fail silently: whether the panel can actually reach
   docker, whether it has registry credentials, whether the Redis password the
   server enforces matches the one the app was given, whether your alert rules
   loaded. Fix anything red before trusting the cluster.
9. **Wire up CI.** Open the panel → **Apps → API → Deployments**, copy the
   endpoint and token into your app repo's secrets (`PANEL_URL`,
   `DEPLOY_TOKEN_PROD`, `DEPLOY_TOKEN_STAGING`), and copy
   `docs/github-actions-deploy.yml` into `.github/workflows/`.
10. **Push a commit** and watch it deploy.

From here on you should not need to SSH in. Configuration, credentials,
redeploys, the firewall and the scaling policy are all in the panel; the
`bin/` scripts are the same paths it uses, if you prefer a terminal.
