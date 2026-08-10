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
   │    Dokploy · autoscaler                                        │
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
| `dokploy-<app>.<root>` | `http://<master-private-ip>:3000` |

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

CI is per-app and out of scope here. What this infra guarantees is the
contract your pipeline targets:

```bash
docker service update --with-registry-auth \
  --image ghcr.io/you/app:sha-abc1234 app_api-prod     # or app_api-staging

docker service rollback app_api-prod                    # undo
```

Set `CI_SSH_PUBLIC_KEY` in the cloud-init and you get a non-root `deploy` user
in the docker group to run that as. Leave it blank and nothing is created.

Three properties you get for free, whatever pipeline you build:

- **Zero downtime** — `order: start-first` brings the new task to healthy
  before stopping the old one.
- **Real health gating** — `monitor: 90s` with `start_period: 60s`. Swarm's
  default monitor is 5 seconds, shorter than JVM startup, so it would call a
  task successful before your app finished booting.
- **Automatic rollback** — `failure_action: rollback`, and the update command
  exits non-zero, so a failed deploy fails your pipeline.

Two rules your pipeline must follow: **never `:latest`** (Swarm compares the
reference string and silently no-ops an unchanged tag), and **always
`--with-registry-auth`** (or a worker created later has no GHCR credential and
cannot pull).

## One-time setup

1. Hetzner private network `10.0.0.0/16`, and a Read & Write API token.
2. **One** Cloudflare Tunnel. Copy the connector token. Add hostnames later.
3. GitHub PAT with `read:packages`.
4. Two Atlas databases.
5. Optional: an SSH public key for `CI_SSH_PUBLIC_KEY`, if you want the
   non-root deploy user.
6. Push `stacks/`, `config/`, `autoscaler/` to a private repo; uncomment the
   clone line in the cloud-init.
7. Fill every `REPLACE_ME`. **Set `SLO_P95_MS` from your real p95.**
8. Create a CPX31, Ubuntu 24.04, on the private network, cloud-config pasted.

```bash
ssh root@<ip> tail -f /var/log/infra-bootstrap.log
docker service ls
docker service logs -f monitoring_autoscaler
```

No workers exist until the autoscaler's first loop — that moment is your proof
the Hetzner token and the worker cloud-init both work. Add the tunnel
hostnames once one has joined.

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