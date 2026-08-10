# Self-managing Swarm on Hetzner

Master node created once by hand. After that: worker nodes appear and disappear
on their own, monitoring picks them up with no configuration, and you go back to
writing Ktor code.

```
        ┌─────────────────────────────────────────────┐
        │  MASTER (manual, CPX31)                     │
        │  Dokploy · Redis ×2 · VictoriaMetrics        │
        │  Loki · Grafana · vmagent · autoscaler       │
        │  NO app tasks. NO cloudflared.               │
        └──────────────────────┬──────────────────────┘
                               │ creates + scrapes
   ┌───────────────────────────┴───────────────────────────┐
   │ WORKERS — one shared pool, 1..MAX, CPX21              │
   │ prod_api (N replicas) · staging_api (1 capped replica)│
   │ cloudflared ×2 tunnels · node-exporter · cadvisor     │
   └───────────────────────────┬───────────────────────────┘
                               ▲
              prod tunnel + staging tunnel
         (Cloudflare LB across every connector)
```

Ingress never touches the master. `cloudflared` runs `mode: global` on the
workers of its own environment, and Cloudflare load-balances across every
connector — so connectors scale with your workers and a dead master costs you
monitoring and scaling, not uptime.

## What changed versus your current setup

| Before | Now | Why |
|---|---|---|
| No monitoring | vmagent + Swarm service discovery | New nodes are scraped automatically; you never edit a scrape config again |
| Manual worker creation | Autoscaler + Hetzner API | Scales 1→MAX on sustained load, never below the floor |
| One environment | Two stacks, two tunnels, shared worker pool | Staging is one capped replica Swarm places wherever there is room |
| Swarm ports reachable publicly | Private network + ufw | Port 2377 exposed to the internet is a full cluster takeover |
| Single Redis / Mongo | Separate instance and Atlas database per env | Staging cannot corrupt production state |

Your existing `cloudflared` arrangement — `mode: global` on workers, Cloudflare
pointing at the Swarm service DNS name — is kept as-is. It is the correct
design and the reason the master can stay out of the data path.

## One-time manual setup

1. **Hetzner private network** — create `prod-net` with range `10.0.0.0/16`. Everything below depends on this existing first.
2. **Hetzner API token** — Console → Security → API tokens → **Read & Write**.
3. **Two Cloudflare Tunnels** — one for prod, one for staging. Copy both connector tokens. Don't add public hostnames yet.
3b. **GHCR token** — a GitHub PAT with `read:packages`, so the swarm can pull your images.
4. **Put this bundle where the master can fetch it.** Either push `stacks/`, `config/`, `autoscaler/` to a git repo and uncomment the `git clone` line in `master-cloud-init.yaml`, or paste them in and run `/opt/infra/bootstrap.sh` manually.
5. **Edit the VARIABLES block** at the top of `master-cloud-init.yaml`. Every `REPLACE_ME` must go.
6. **Create the master**: CPX31, Ubuntu 24.04, attached to `prod-net`, cloud-config = the edited file.
7. **Watch it**: `ssh root@<ip> tail -f /var/log/infra-bootstrap.log`
8. **Add tunnel hostnames** once the first worker of each env has joined:
   - prod tunnel: `app.<domain>` → `http://api:8080`
   - staging tunnel: `staging.<domain>` → `http://api:8080`
   - `dokploy.<domain>` and `grafana.<domain>` → `http://<master-private-ip>:3000` / `:3000`

   Both stacks use the service name `api`, but each lives on its own overlay
   network with its own tunnel, so there is no collision.
9. **Set `DRY_RUN=false`** only after watching the autoscalers log a few loops with it `true`. It ships as `true`.

## Wiring your Ktor app for metrics

The alerts and the latency-based scaling both read Micrometer's
`http_server_requests_seconds` histogram. Add:

```kotlin
// build.gradle.kts
implementation("io.ktor:ktor-server-metrics-micrometer:$ktorVersion")
implementation("io.micrometer:micrometer-registry-prometheus:1.13.6")
```

```kotlin
val registry = PrometheusMeterRegistry(PrometheusConfig.DEFAULT)

install(MicrometerMetrics) {
    this.registry = registry
    distributionStatisticConfig = DistributionStatisticConfig.Builder()
        .percentilesHistogram(true)   // required — vmalert uses histogram_quantile
        .build()
    meterBinders = listOf(
        JvmMemoryMetrics(), JvmGcMetrics(), ProcessorMetrics(),
    )
}

routing {
    get("/metrics") { call.respond(registry.scrape()) }
    get("/health")  { call.respondText("ok") }
}
```

`/health` is what the Swarm healthcheck hits — without it, rolling updates
can't tell a booting JVM from a broken one and `start-first` gives you no
protection.

Log as JSON to stdout; the Loki Docker driver ships it with no agent.

## Operating it

```bash
# is the production autoscaler thinking straight?
docker service logs -f monitoring_autoscaler

# deploy a new production image (immutable tag, registry auth)
docker service update --with-registry-auth \
  --image ghcr.io/you/your-ktor-app:sha-def5678 prod_api

# roll back
docker service rollback prod_api

# raise the floor without redeploying
docker service update --env-add MIN_WORKERS=3 monitoring_autoscaler

# emergency stop on all scaling
docker service update --env-add DRY_RUN=true monitoring_autoscaler
```

## Things that will bite you

- **The master is a single point of failure.** Dokploy, Redis, the metric store and the autoscaler all live there. The autoscaler is stateless on purpose, so rebuilding the master from this same cloud-init restores the cluster — but Redis AOF and VictoriaMetrics data are gone unless you enable Hetzner backups on the master. Turn them on. It's 20% of the server cost.
- **Redis has no HA.** Single instance, AOF `everysec`, so a hard crash loses up to a second of writes. Fine for cache and sessions; not fine as a source of truth. If it becomes one, move to Atlas or a managed Redis.
- **Staging shares the scaling average.** Its container is capped at 0.5 CPU, so one idle replica is noise. A real load test in staging is not — set `DRY_RUN=true` on the autoscaler before you run one, or accept that production may briefly add a worker and bill you for the hour.
- **Staging has no dedicated node.** If the worker holding it gets drained during a scale-down, the replica reschedules onto another worker — a few seconds of staging downtime. That is the trade for not paying for a dedicated box.
- **The autoscaler's queries depend on the `node_role="worker"` relabel in `vmagent.yml.tpl`.** Drop that rule and it goes blind, returns nothing, and sits at the floor forever. `AutoscalerStalled` will not catch this; it only checks the loop is running. Watch `autoscaler_cluster_cpu_percent` instead.
- **Never deploy `:latest`.** Swarm compares the image reference string; an unchanged tag means your deploy quietly does nothing. Tag with the commit SHA.
- **Always pass `--with-registry-auth`.** Without it, a worker created after the deploy has no GHCR credential and its tasks fail to pull.
- **Image size is scale-up latency.** Every new worker cold-pulls. Use a layered Gradle build so only a thin app layer changes between deploys.
- **Scale-down drains for at most 180s**, then removes the node regardless. If you have long-running requests, raise that deadline in `autoscaler.py` or you'll cut connections.
- **Cost ceiling is real.** `MAX_WORKERS=6` of CPX21 is roughly €50/month if pinned there all month. The `AutoscalerAtMax` alert fires after 15 minutes at the ceiling — treat it as a bug report about your app, not a signal to raise the ceiling.
- **Hourly billing rounds up.** Rapid up/down cycling costs real money, which is why `COOLDOWN_DOWN_SECONDS` defaults to 15 minutes. Don't lower it much.

## Adding a second app later

That's the point of the setup. Add a service to `stacks/app.yml` (or deploy it
through Dokploy), give it the three `prometheus.*` deploy labels, add a tunnel
hostname. Monitoring, scaling and log shipping all apply to it with no further
work.
