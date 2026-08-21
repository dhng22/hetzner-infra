# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Infrastructure-as-config for a self-managing Docker Swarm on Hetzner Cloud. It is **not** an
application repo: the deliverable is a bundle of files copied to `/opt/infra` on a manually-created
master node, where `bootstrap.sh` (embedded in `master-cloud-init.yaml`) turns them into a running
cluster.

The master boots **infrastructure only** — swarm, monitoring, the tunnel connector, the admin panel.
No application, no database, nothing of yours. Those are **components**, created afterwards from the
panel or with `bin/component`, one independent Docker stack each.

Read `README.md` first — it holds the operational rationale. This file covers what the README
doesn't: how the pieces are wired, and which of the wires are load-bearing.

## Working on this repo

There are real tests now, and they cover the parts where a bug is silent:

```bash
# the component library: rendering, validation, isolation, credentials
python3 -m unittest discover -s admin/tests -v

# the autoscaler's decision logic: policy parsing, the packer, admission, fleet arithmetic
python3 -m unittest discover -s autoscaler/tests -v

# alerting — run after ANY alerts.yml change
promtool test rules config/alerts_test.yml
promtool check rules config/alerts.yml
amtool check-config config/alertmanager.yml

# syntax
python3 -m py_compile autoscaler/autoscaler.py admin/*.py admin/components/*.py
bash -n bin/*
docker build -t autoscaler:test autoscaler/

# the panel, offline, against dummy data — no docker, no cluster
PREVIEW=1 ADMIN_PASSWORD=dev python3 admin/app.py     # http://localhost:3000
python3 admin/preview_build.py                        # -> admin/preview/index.html

# a component's rendered stack, without a cluster
bin/component render api | docker compose -f - config
```

Match tool versions to the deployed ones (`promtool` 2.x, `amtool` 0.27.0) — `api_url_file` and the
`matchers` route syntax are version-sensitive. The bootstrap script is embedded in
`master-cloud-init.yaml` as a `write_files` entry, so syntax-check it by extracting the YAML value
and running `bash -n` on it; editing it in place is easy to get subtly wrong.

`DRY_RUN=true` in `infra.env` makes the autoscaler log the Hetzner create/delete calls it would make
without issuing them. That is the safe way to exercise a scaling change against live metrics.

## Deployment flow

1. Fill the `VARIABLES` block at the top of `master-cloud-init.yaml`. Every `REPLACE_ME` must be set.
   Nothing in it names an application.
2. Create a Hetzner CPX31 on the private network with that cloud-config. `runcmd` expects the bundle
   to already be in `/opt/infra` — the clone line is commented out on purpose.
3. `bootstrap.sh` preflight-checks the required variables, firewalls, installs Docker,
   `docker swarm init`s on the private IP, creates the docker secrets, creates
   `/opt/infra/components`, creates the `edge` overlay, renders the templates, builds the
   `autoscaler` and `admin` images, and deploys **monitoring → ingress → admin**.
4. `docker service ls` then shows infrastructure and nothing else. That is the finished state of a
   boot, not a half-finished one.
5. Components are created afterwards, and each deploys itself.

## Architecture invariants

These constraints span multiple files. Breaking one fails silently rather than loudly.

**One component, one stack, one directory, one writer.** A component owns
`/opt/infra/components/<name>/` (`component.json`, `env`, `secret.env`, `stack.yml`) and the Swarm
stack `<name>`, whose services are `<name>_app`, `<name>_redis`, `<name>_redis-exporter`. Nothing is
shared between components, which is the whole point: the previous design had one
`config/app-<env>.env` written by both the application's environment editor and the database's
credentials form, so a page rendered before someone else's save wrote their change back out. Two
writers on one file is the bug class this model exists to remove — do not reintroduce a shared file.

**Discovery is by label, never by name.** The autoscaler manages every service carrying
`infra.workload=app`, and reads that service's entire scaling policy from its own `autoscale.*`
deploy labels. There is no `APP_SERVICE`. Anything without the label is overhead: never pinned,
never scaled, its reservations simply subtracted from whatever node it sits on. The opt-in is
deliberately one-directional — a forgotten label leaves an app on the master, correctly accounted
and visible; a label on a stateful service moves a volume onto a worker that later gets deleted.
**Redis therefore carries no `infra.workload` label** and keeps `node.role == manager`.

**The renderer never invents placement, replicas or the image.** `Component.render()` reads the live
image (CI owns it), the live replica count (the autoscaler owns it) and the live `node.role`
constraint (the autoscaler owns it) back from the running service and re-states them. Emitting the
spec's copy is how a redeploy triggered by an unrelated setting rolls production back
([docker/cli#2235](https://github.com/docker/cli/issues/2235)). Tests pin all three.

**Reservations are mandatory and load-bearing twice.** Swarm's scheduler subtracts them when placing
a task, *and* they are the entire input to the autoscaler's capacity arithmetic.
`Component.resources()` refuses to render a service without them. A component with none makes its
node look idle, and app replicas get packed on top of VictoriaMetrics.

**A component's credentials are `Secret` declarations, not `Field`s.** A Field's value is written to
`component.json`, which is 0640 and is the file someone pastes into an issue; a password must never
be in there. `SECRETS` are written to `secret.env` at 0600 by `apply_secrets()`, which treats blank
as "generate" both at create time and afterwards — a database reachable with no password by leaving
a form field empty is not a state worth having. The panel renders a Credentials tab for any type
that declares them and none for a type that does not, so a future Postgres needs no route change.
The displayed connection URL percent-encodes the password, because a user-chosen one containing `@`
turns `redis://default:p@ss@host` into a different host and an error that blames DNS.

**Capacity is measured in CPU/memory, never in replicas.** Every quantity is a `Res(nanocores, bytes)`
integer vector: a node's free capacity is what it advertises minus the reservations of every task on
it that is *not* an app; demand is the sum over services of replicas × that service's own
reservation. Replica-unit capacity cannot express two applications of different sizes at all, and
floats in the sum produce a one-replica flap nobody can reproduce. There is no
`REPLICAS_PER_WORKER`, no manager-capacity constant and no headroom constant — all three existed,
all three were wrong, and all three were guesses about hardware the autoscaler can read directly.

**`edge` and `monitoring` are external overlays created before any component exists.** `edge` by
`bootstrap.sh` with `docker network create`, `monitoring` by `stacks/monitoring.yml`. No component
may own a network another component needs — when `edge` belonged to the app stack, removing that
stack took the tunnel's route to everything with it.

**Service discovery for metrics is label-driven, never enumerated.** `config/vmagent.yml.tpl` keeps
any Swarm task carrying `prometheus.scrape/port/path` deploy labels, and `node-exporter`/`cadvisor`
run `mode: global`. The component renderer emits those three labels, so a new component is scraped
with no edit to the scrape config. The `service` label vmagent writes is what every per-component
alert and query joins on.

**Logs bypass any agent.** Every stack uses the `loki` Docker log driver pointed at
`http://${MASTER_PRIVATE_IP}:3100`, which is why Loki publishes port 3100 in `mode: host` and why
the plugin is installed in both cloud-inits.

**The master is not a worker.** `MIN_WORKERS`/`MAX_WORKERS` count Hetzner worker servers, and the
master is the control plane that happens to carry the load while none exists. `MIN_WORKERS=0` is the
free floor — nothing billed, master serving; `MIN_WORKERS=1` keeps one worker up, which is also the
only way to say "the master runs no application traffic". `AutoscalerAtMax` compares workers to
workers, so `autoscaler_current_hosts` is now purely informational and no threshold keys on it. At
the floor, losing the master costs uptime and not just scaling — the deliberate price of it.

**`cloudflared` is `mode: global` with no constraint**, in its own `ingress` stack. Global so the
master always has a registered connector and the tunnel does not gap during a handover; its own
stack so an application you have not built yet cannot take ingress — and with it the panel's own
hostname — down with it.

**Routing is manual and that is a choice.** The tunnel runs in token mode, so its ingress rules live
in the Cloudflare dashboard. The panel's job is to show you the exact local target
(`http://<name>_app:<port>`, service DNS on `edge`) to paste. Nothing here calls the Cloudflare API.

## Autoscaler control flow

`autoscaler/autoscaler.py` is a single-file loop (`loop()`, every `LOOP_SECONDS`) in a strict order.
The ordering is the whole gaplessness argument; reversing any of it causes an outage.

1. `reap_orphans()` — delete Hetzner servers that never joined, and swarm nodes whose server is gone.
2. **Inventory** (nodes + one `index_tasks()` call). Failing here is a HOLD: without it every later
   number is a guess.
3. `discover_workloads()` — services labelled `infra.workload=app`, with their policies.
   **Discovery raising and discovery returning zero services are opposite events**: an API error
   must never be read as "no demand" and delete the fleet, while an honest empty result is the
   normal state of a fresh cluster and must scale to the floor cleanly.
4. **Emergency**, before anything that can fail: pinned to workers with an empty fleet means every
   task is unplaceable, so release every pin immediately. This is the one path that must survive
   VictoriaMetrics or Hetzner being down, which is why it is at the top and ignores
   `update_in_progress`.
5. Capacity, per node. A node whose tasks cannot be listed contributes **zero free**, not zero used —
   believing in room that does not exist is how tasks end up pending.
6. `read_signals_batch()` — every service's p95 and CPU-per-replica in a handful of `by (service)`
   queries. Per-service queries do not fit in a 60s loop and `AutoscalerStalled` fires at 300s.
7. Tier 1 per service, each isolated: `desired_replicas()` → **want**.
8. `workers_needed()` from the **uncapped** want. Returns 0 when the master alone can hold
   everything, and never 1-because-one-more-would-do: past the master, the workers cover the whole
   demand including what the master was carrying.
9. `admit()` against the currently eligible nodes → **admitted**.
10. Apply in order: **release the pin (scaling in) → create nodes → set replicas → add the pin
    (scaling out) → remove nodes**.

**Want, hosts and admitted are three different numbers.** Collapsing want and admitted — which the
previous version did, mutating one variable in place — deadlocks with several services: each gets
capped to fit, the total never exceeds capacity, and no worker is ever bought. **Admission caps
growth and never scales anything down**; shrinking belongs to tier 1 and to node removal.

**One packer, used three times.** `place()` answers admission, fleet sizing and removal. Two
algorithms that disagree by one replica are a loop that buys a worker and immediately deletes it.
Allocation is round-robin in `(priority, name)` order — deterministic and monotone, so more capacity
never reduces anyone's allocation, which is what stops oscillation. Ranking by "worst breach first"
couples allocation to a noisy signal and hands a replica back and forth every 60 seconds.

**App tasks already on a node are not subtracted from its free capacity.** `node_free_for_apps()` is
"total room for apps here" and demand counts every replica including the ones already placed, so the
two sides never double-count. Demand comes from desired counts, never live tasks, so a start-first
rollout briefly doubling a footprint cannot inflate it.

**Placement is reconciled every loop, for every service** — not only on transitions. That fixes two
real bugs: a second service that had drifted out of the pin was never reconciled, and a service
created while the cluster was already in worker mode was never pinned at all.

- The pin is released *before* the last worker is deleted, and removal additionally blocks until a
  replica of **every** app service is running on the master — waiting indefinitely rather than
  trading uptime for a few euros.
- The pin is added only once workers are `ready` *and* can hold every replica.
- `pick_removal_candidate()` tests each candidate with the packer rather than comparing sums; with
  heterogeneous costs a sum is meaningless. It takes the master's free capacity as a bin only when
  every app is unpinned — without that the LAST worker is never removable.
- `provisioning_workers()` counts booting servers towards the fleet. Sizing off ready workers alone
  re-orders the same capacity every loop for two minutes and sails past `MAX_WORKERS`.

Node CPU is deliberately never a scaling trigger — it averages in exporters, the tunnel connector
and every other component. CPU-per-replica exists because `histogram_quantile` over an empty bucket
returns nothing, so at low traffic the latency signal vanishes and something must authorise scaling
back down. Its denominator is each service's own CPU limit read from the live spec, which is what
finally deleted `APP_CPU_LIMIT` and its hand-synchronisation warning.

Removal is LIFO: newest node first, least likely to hold warm state, and Hetzner bills hourly.

**The autoscaler's blast radius is the Hetzner label selector** `cluster==<APP_NAME>,role==swarm-worker`.
`reap_orphans()` deletes servers matching it that never joined the swarm. Anything else in the same
Hetzner project is protected only by that label.

## Deploy paths

There are exactly two, and they must not be confused:

- **`bin/stack-deploy <monitoring|ingress|admin>`** — the infrastructure. Re-applying the file is
  always safe because nothing else owns state in it. It refuses anything else.
- **`bin/component deploy <name>`** — one component. Renders its stack from its spec, with the live
  image, replica count and placement pinned, and deploys only that stack.

The panel shells out to both and calls `docker stack deploy` itself nowhere.

## The admin panel is a root console

`admin/` is a Flask app pinned to the manager with the docker socket mounted. It can deploy anything
and read every credential in the cluster, so changes to it are security changes:

- Every mutating route is POST + CSRF-checked, **including `/logout`**.
- Credentials arrive as docker secrets, never from the image or compose file. Login comparison is
  constant-time with a per-process PBKDF2 salt, and six failures lock the source address out.
- Gunicorn runs **one worker on purpose** — the lockout counter lives in memory, so a second worker
  would hand an attacker a second lockout budget.
- **The routes are generic.** `/components/<name>` renders whatever tabs the component declares,
  `/components/<name>/action` dispatches whatever verbs it offers, and both forms are built from
  `fields()`. Adding a type is a class plus one line in `TYPES` — if you find yourself adding
  `if component.TYPE == ...` to a route or template, the abstraction is in the wrong place.
- `admin/components/` is **stdlib + PyYAML only**, so `bin/component` can import it on the host
  where Flask and docker-py are not installed. Live status lives in `swarm.py`, which is panel-only.
- `admin/shape.py` holds anything derived from a service dict, because `swarm.py` and `fixtures.py`
  are swapped for each other and anything computed in one and not the other is a drift waiting to
  happen — that is exactly how `history()` came to exist only in the fixtures while the live panel
  500'd on it.
- Only the open tab's expensive data is fetched. Logs shell out with a 30s timeout and the firewall
  probe is an SSH round trip with another; doing both on every request made the Overview tab wait
  for two things it does not render.

`admin/preview_build.py` renders the real templates against `fixtures.py` into a single
self-contained `admin/preview/index.html`, and seeds **real components** into a temp `INFRA_DIR` so
the preview drives the actual classes and renderer. It fails the build if any live form action
leaks into the output. Rebuild it after template changes.

## Alerting must fail loudly

The alerting path was rebuilt after all three of its parts turned out to be inert. Keep these
properties:

- **`ALERT_WEBHOOK_URL` is required.** Bootstrap rejects unset/`REPLACE_ME` values before installing
  anything, and Alertmanager reads the URL from a docker secret via `api_url_file`, so it refuses to
  start rather than dropping alerts. Never reintroduce a placeholder receiver.
- **Absent series must alert.** With components created at runtime, `or vector(0)` cannot do this —
  it can only name one service. The anchor is `autoscaler_service_running_replicas`, a gauge the
  autoscaler reads from the Docker API, which exists for as long as the component does. So "every
  replica is gone" is the value 0 rather than a missing series.
- **Cluster gauges stay unlabeled; only per-service quantities gain a `service` label.**
  `AppStrandedWithoutWorkers` is a plain `and` on the empty label signature — give
  `autoscaler_placement_worker_mode` a label and it matches nothing and goes permanently silent.
  Its four test cases are unchanged from before the refactor precisely as that assertion.
- **`ReplicaCeiling` needs its guards.** A fixed-replica component sits at `current == max == min`
  forever and would page continuously without `autoscale_enabled == 1` and `max > min`; the rule
  then gets muted and means nothing.
- **`Watchdog` fires permanently by design** and is routed to its own 24h receiver. Its silence is
  the signal; do not "fix" it.

**`autoscaler_current_workers` and `autoscaler_current_hosts` are different numbers.** The first
counts Hetzner servers and reaches 0 at the floor; the second adds the master and is never below 1.
Collapsing them made the stranded alert unfireable.

Guard all of it with `promtool test rules config/alerts_test.yml`. The suite is mutation-tested:
dropping the `ReplicaCeiling` guards or labelling the stranded gauge both fail it.

## Remaining known gap: secret rotation

There is no way to change a **docker** secret. `mk_secret` in `bootstrap.sh` is
`docker secret create ... || true`, so re-running bootstrap with a new `CF_TUNNEL_TOKEN` silently
does nothing — and Swarm secrets are immutable, so it could not update in place anyway. Rotating one
means creating a versioned secret (`CF_TUNNEL_TOKEN_v2`), editing the stack file, and redeploying.

This applies to the infrastructure credentials only (tunnel token, Hetzner token, Grafana, alert
webhook, admin login). A component's own credentials are not docker secrets: a Redis password lives
in that component's `secret.env`, and rotating it is a button that regenerates the value and
redeploys one stack. Nothing else holds a copy, so nothing else breaks — which is what made the
button possible.
