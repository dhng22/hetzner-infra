# Self-managing Swarm on Hetzner

One master node created by hand. It boots with monitoring, ingress and a control
panel — and nothing else. Applications and databases are components you create
afterwards, each its own independent stack. After that, workers and replicas
scale themselves against each component's own latency SLO, CI deploys itself,
and monitoring picks up anything new without configuration.

## Architecture

```
                     Cloudflare — ONE tunnel, hostnames you add
                                     │
   ┌─────────────────────────────────┴──────────────────────────────┐
   │  WORKER POOL — 0 servers at rest, autoscaled up to MAX-1        │
   │    cloudflared   (global: one connector per node)              │
   │    <your apps>   (N replicas each, autoscaled independently)   │
   │    node-exporter + cadvisor (global)                           │
   ├────────────────────────────────────────────────────────────────┤
   │  DATABASE MACHINES — leased, one per member, never packed       │
   │    <one database member>  + its backup agent                   │
   │    labelled `dedicated=true`, so no app replica lands here     │
   └─────────────────────────────────┬──────────────────────────────┘
            creates, drains          │        metrics, logs
   ┌─────────────────────────────────┴──────────────────────────────┐
   │  MASTER — manual. HOST #1.                                     │
   │    admin panel                                                 │
   │    overseer     decides: what is slow, and how big the fleet is│
   │    autoscaler   applies it to services                         │
   │    dataguard    applies it to databases                        │
   │    VictoriaMetrics · Loki · Grafana · vmagent · Alertmanager   │
   │    cloudflared                                                 │
   │    <your databases>  (member 1 always; the rest grow outward)  │
   │    ...and your apps ONLY while no worker exists                │
   └────────────────────────────────────────────────────────────────┘
```

**One process decides; two apply.** The overseer measures every application,
works out why a slow one is slow, and owns the Hetzner fleet: how many machines
exist, of what size, and which may be deleted. It holds the token. The
autoscaler turns its verdicts into replica counts and placement constraints;
dataguard turns them into replica-set members, failovers and backups. Neither
can buy a machine, and neither decides anything — which is why a manager that
wants a machine asks rather than racing another one for it.

The master is **not a worker** — it is the control plane, and while no worker
exists it carries your components itself. So the resting state is one box and no
Hetzner servers. When load outgrows what the master can hold, workers are
provisioned for *all* the replicas and the master goes back to carrying none.
How much each node holds is measured from its real CPU and memory, not
configured. See [Scaling policy](#scaling-policy).

Two networks, both created before any component exists: `edge` (every component
plus cloudflared — this is how your app reaches your database, and how the
tunnel reaches your app) and `monitoring` (the observability stack, plus
anything being scraped). Dataguard is the only infrastructure service on both,
because it has to talk to a database rather than about one — which is why its
receiver binds the monitoring address only and never `0.0.0.0`.

## Components

Nothing of yours is baked into the cluster. A component is a name, a spec file
and a Docker stack:

```
/opt/infra/components/api/
  component.json   the spec — image, port, resources, scaling policy
  env              your environment variables
  stack.yml        what was last rendered and deployed
```

Three types ship today:

| Group | Type | What it is | Owns |
|---|---|---|---|
| Application | `app` | a container image of yours, behind the tunnel | its environment, its replica count, its scaling policy |
| Database | `redis` | Redis with a Sentinel quorum in front of it | its own password, yours to set or generate |
| Database | `mongo` | a MongoDB replica set, TLS end to end | its own password, its own certificate authority |

In the panel that is one **+ New** button with two entries, and Database offers
both engines. A third is a new class and one line in `components/__init__.py`;
no route, template or script learns its name.

Both databases are created at their FINAL SHAPE and grow into it. A `mongo` with
a pool of three is four member services and a connection string that names all
four on the day you make it, while only the one on the master is running. The
driver ignores a seed it cannot resolve and discovers the set from the ones it
can — which is what lets the database move onto its own machines later without
anything that talks to it changing. A `redis` does the same trick with a
sentinel URL: three sentinel services are declared and ONE of them runs, because
a quorum watching a server that has no replica to promote is three containers
buying nothing — and on a single-node cluster all three would sit on the machine
they are watching. Dataguard starts the other two when it starts the second
server, immediately before it, and stops them again when the set shrinks back.
**Raising the pool afterwards is the one change that does alter the string, so
pick the ceiling when you create it.**

Create them in the panel (**Components → New app**) or on the master:

```bash
component create app api --image ghcr.io/you/app:sha-abc1234 --port 8080
component deploy api

component create redis cache                       # generates a password
component create redis cache --REDIS_PASSWORD ...  # or bring your own
component secret cache show
component rotate cache            # new password, redeploys only this component
component env api set LOG_LEVEL=debug
component remove api
```

Each becomes the Swarm stack `<name>`, with services `<name>_app` or
`<name>_redis`. **They share nothing.** Deleting an app cannot touch a database,
a broken image cannot take down the tunnel, and no two things write to the same
file — which is exactly what went wrong in the design this replaced.

**Databases keep their credentials to themselves.** A Redis component's
password is injected into its own service and nowhere else, and the panel shows
you the connection URL. Nothing is injected into your application for you; how
you use the URL is your business.

**You choose the password, or leave it blank and get a generated one** — on the
create form and afterwards on the Credentials tab, plus a one-click regenerate.
Setting your own matters when you are moving an existing database whose clients
already know it. Rotating is safe for the same reason nothing is injected:
nothing else in the cluster holds a copy to go stale.

## Hostnames

Routing is configured once, by hand, in the Cloudflare dashboard — the tunnel
runs in token mode, so its ingress rules live there rather than here. The panel
shows you the exact local target to paste for each component.

Cloudflare's free Universal SSL covers `*.root` and nothing deeper, so every
name stays at the third level and extra services take a dash:

| Hostname | Tunnel target |
|---|---|
| `<app>.<root>` | `http://api_app:8080` — an `app` component named `api` |
| `staging-<app>.<root>` | `http://api-staging_app:8080` — a second component |
| `admin-<app>.<root>` | `http://<master-private-ip>:3000` |

Grafana is not on that list. It is served through the panel at `/grafana/`,
behind the session you are already signed in to, the same way the database
consoles are — so it needs no hostname, no second login page on the public
internet, and no Access policy of its own.

Put the panel behind Cloudflare Access. It controls your cluster.

## Scaling policy

Load is absorbed cheapest-first:

1. **Replicas** — seconds. Driven by each component's own p95 latency against
   its own SLO, with CPU-per-replica as the secondary signal.
2. **Nodes** — ~2 minutes. Only when the replicas we want will not fit.

Coming down, the order reverses: shed replicas first, remove the node later.

### The autoscaler is told nothing; it discovers everything

There is no `APP_SERVICE` and no list of applications anywhere. Every service
carrying the deploy label `infra.workload=app` is managed, and its policy travels
with it on that same service:

| Label | Meaning |
|---|---|
| `infra.workload=app` | manage this: pin it, count it, scale it |
| `autoscale.enabled` | drive the replica count from signals (off = fixed size) |
| `autoscale.min_replicas` / `.max_replicas` | the range it may move in |
| `autoscale.slo_p95_ms` | THE number. Up and down thresholds are fractions of it |
| `autoscale.up_p95_ratio` / `.down_p95_ratio` | act at 80% of the SLO; relax below 40% |
| `autoscale.up_cpu_pct` / `.down_cpu_pct` | the secondary signal |
| `autoscale.sustain_up_seconds` / `.sustain_down_seconds` | up fast, down slow |
| `autoscale.priority` | who wins when two components want the same node |

The panel writes them when you create or edit a component. Anything **without**
`infra.workload=app` is overhead: never moved, never scaled, its reservations
simply subtracted from whatever node it sits on. That is why a database needs no
autoscaler change at all — and why a database must never carry the label, since
it would then be moved onto a worker that later gets deleted.

### Where your apps run — the master is not a worker

`MIN_WORKERS` and `MAX_WORKERS` count **Hetzner workers**. The master is not one
of them, so a floor of zero costs nothing:

| Workers | Constraint on app services | Who runs them |
|---|---|---|
| 0 | *(none)* | the master |
| 1+ | `node.role == worker` | the workers only; the master carries zero |

`MIN_WORKERS=0` therefore means "nothing billed, the master serves" — the
default. `MIN_WORKERS=1` keeps one worker up permanently, which also means the
master never runs application traffic; there is no separate switch for that.
`MAX_WORKERS=5` is the most that may ever exist.

The autoscaler adds and removes that constraint itself, and re-states whatever is
live on every deploy — the component spec deliberately never sets it.

### Capacity is measured in CPU and memory, not in replicas

There is no `REPLICAS_PER_WORKER`. Everything is a `(CPU, memory)` vector:

```
a node's free capacity = what it advertises
                       - the reservations of every task on it that is not an app
demand                 = for each component, replicas x its own reservation
a new machine          = the catalogue entry for the smallest plan that fits
                       - the per-node tax of the global services
```

Worked example, with the shipped reservations: a CPX31 master running only
infrastructure has about **2.25 CPU / 5.9 GB** free, and a new CPX21 worker
offers **2.8 CPU / 3.9 GB** after node-exporter, cadvisor and cloudflared take
theirs. An app reserving 0.5 CPU / 384 MB and a second reserving 0.25 / 256 MB,
at 3 and 5 replicas, come to 2.75 CPU — past the master, so one worker is bought
and holds both.

"Replicas per node" cannot express that at all once two components are different
sizes, and memory becomes the binding constraint long before CPU does on a
four-component cluster.

**Existing nodes are filled before new ones are bought**, and a node is worth
what it can actually hold — so mixed fleets work, and a half-empty 4 vCPU worker
is used up before another is ordered.

> The unit is the **reservation**, not the limit, because reservations are what
> Swarm's scheduler actually subtracts when placing a task. The limit is burst
> headroom on top. If replicas do contend, p95 rises and the autoscaler adds
> capacity — which is the signal it is built on anyway.

### Several components share the fleet without starving each other

When demand exceeds what the eligible nodes can hold, the fleet is sized from
the **uncapped** demand — so it grows — while what is actually applied this
minute is capped. Minimums are satisfied first, then growth is handed out one
replica at a time, round-robin. That is deterministic and monotone: more
capacity never reduces anyone's allocation, which is what stops two components
trading a replica back and forth every loop. `autoscale.priority` is the escape
hatch when one genuinely matters more.

Capping never scales anything **down** — shrinking is the signals' job, not the
packer's.

### Both handovers are ordered so something is always serving

- **Out:** provision workers → wait until they are `ready` *and* their measured
  capacity covers every replica → only then add the pin. The master serves the
  whole time.
- **In:** remove the pin *first*, while the last worker is still up → wait until
  a replica is actually running on the master → only then drain and delete it.

If every worker disappears at once, the next loop puts everything back on the
master by the same path — and it runs at the *top* of the loop, before anything
that can fail, so a VictoriaMetrics or Hetzner outage cannot block the recovery.
`AppStrandedWithoutWorkers` fires if it does not happen within two minutes. The
emergency path applies even at `MIN_WORKERS=1`: an outage is worse than
temporary co-tenancy.

Removing the last worker additionally waits for a replica of **every** component
to be running on the master — indefinitely if it comes to that. Keeping one
worker costs a few euros a month; removing it blind costs the site.

Where each setting lives now — and the split is the point:

| Setting | Default | Where | Why |
|---|---|---|---|
| `autoscale.slo_p95_ms` | 500 | the component | **The** number. Everything derives from it. Set it from your real latency distribution. |
| `autoscale.min_replicas` / `.max_replicas` | 1 / 4 | the component | Two is the usual production floor: with one, a crash is an outage. |
| `autoscale.up_p95_ratio` | 0.8 | the component | Act at 80% of the SLO, before users feel it |
| `autoscale.up_cpu_pct` | 70 | the component | % of that replica's own limit — not node CPU |
| `autoscale.sustain_up_seconds` | 90 | the component | Up fast |
| `autoscale.sustain_down_seconds` | 900 | the component | Down slow. Never symmetric. |
| `autoscale.up_factor` | 0.5 | the component | +50% of current, min +1. One at a time cannot track a spike. |
| `MIN_WORKERS` | 0 | `infra.env` | Hetzner workers; the master is not one. `0` = nothing billed. |
| `MAX_WORKERS` | 5 | `infra.env` | The most workers that may exist. A **budget** cap. |
| `NODE_PRESSURE_PCT` | 80 | `infra.env` | Placement guard: another replica will not fit |
| `COOLDOWN_UP_SECONDS` | 300 | `infra.env` | Must exceed boot + pull + app warmup, or you overshoot |

Application policy belongs to the application; fleet policy belongs to the
cluster. A copy of an SLO in `infra.env` would be a copy that goes stale, and
there would be no right answer once there are two applications.

Why CPU is still there when the policy is latency-led: `histogram_quantile`
over an empty bucket returns nothing. At low traffic the latency signal
disappears entirely, and CPU-per-replica is what tells the scaler it is safe
to come back down.

## Deploying an image

Your pipeline pushes an image and tells the panel to move the service to it:

```
POST https://admin-<app>.<root>/hooks/deploy/<component>
     X-Deploy-Token: <from that component's Deployments tab>
     {"image": "ghcr.io/you/app:prod-9f3ac21"}
```

A complete GitHub Actions workflow is in `docs/github-actions-deploy.yml`.
The panel's token, endpoint and a ready-made `curl` are on the Deployments tab.
**One token per component**, so a leaked staging token cannot reach production.

Four properties you get, whatever pipeline you build:

- **Zero downtime** — `order: start-first` brings the new task to healthy
  before stopping the old one.
- **Real health gating** — `monitor` tracks the component's own startup grace.
  Swarm's default monitor is 5 seconds, shorter than most application startups,
  so it would call a task successful before your app finished booting.
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
  --image ghcr.io/you/app:prod-9f3ac21 api_app
docker service rollback api_app                         # undo
```

`--with-registry-auth` is not optional: it ships the registry credential with
the service spec, so a worker created an hour from now can still pull.

## Updating the infrastructure itself

Push to `master`. That is the whole procedure.

GitHub Actions builds the three images this repository owns — the autoscaler,
the overseer and the admin panel — runs all four test suites **inside** the
images it just built, and only then publishes them to GHCR, tagged with the
commit:

```
ghcr.io/<owner>/<repo>/autoscaler:<sha>
ghcr.io/<owner>/<repo>/overseer:<sha>
ghcr.io/<owner>/<repo>/admin:<sha>
```

Every master polls the branch head every five minutes. When it moves, the master
pulls those three exact references, copies the new tree into `/opt/infra`, and
re-applies the three stacks. It builds nothing and it tests nothing — both of
those jobs belong to CI, and doing them on the box whose actual job is running
the cluster is what made updates slow and fragile.

**The tag is a commit sha and never `latest`, and that is the whole design.**
These images used to be built on the master and tagged `:latest`. A tag that
exists only in one box's image store cannot be resolved to a digest, so Swarm
wrote the bare string into the service spec — and rebuilding the tag then
produced a spec byte-identical to the running one. Swarm compared, found no
difference, and kept the **old** container serving while the new image sat
unused. The updater reported success, the panel showed the new commit, and the
code was the old code. With an immutable per-commit reference the spec genuinely
changes, so Swarm rolls the service because that is what it does; nothing has to
be forced, and "what is this cluster running" is answerable by reading the
service.

Two things follow from CI owning the build:

- **A commit with no images is a commit no cluster moves to.** A failing test
  publishes nothing, so the master pulls a 404 and stays where it is. It waits
  quietly for the first 25 minutes — CI takes a couple of minutes and a master
  should not panic about that — and after the deadline the panel says *update
  failing* and names the missing image.
- **The GHCR packages must be readable by the master.** Making them public is
  simplest. If you keep them private, `GHCR_USER` / `GHCR_TOKEN` in
  `master-cloud-init.yaml` need `read:packages` on them — the same PAT the
  cluster already uses to pull your application image.

`.github/workflows/publish.yml` and `bin/infra-images` have to agree on the
image names down to the character, and neither can import the other, so
`test_ci_publishes_exactly_the_images_this_cluster_pulls` runs both derivations
and compares them. A disagreement would not crash anything — the master would
pull a tag that was never published, treat the 404 as "CI has not finished yet",
and sit at *update pending* forever.

## Before you start

Four things to have open in other tabs:

1. **Hetzner** — a private network `10.0.0.0/16`, and a **Read & Write** API token.
2. **Cloudflare** — one Tunnel; copy the connector token. Hostnames come later.
3. **GitHub** — a PAT with `read:packages`, for the cluster to pull your
   application image, and this repository pushed somewhere Actions can build
   it. See *Updating the infrastructure itself*.
4. **Telegram** — a bot from @BotFather, added to a group. The chat id comes
   from `curl https://api.telegram.org/bot<TOKEN>/getUpdates` after you send one
   message there; group ids are negative. Optional at boot — the cluster comes
   up without it and says loudly that alerts are being dropped.

Anything your application itself needs — a managed database, an API key, a
bucket — is not on this list. Those go in that component's environment after the
cluster is up, and the cluster has no opinion about them.

Optional: an SSH public key for `CI_SSH_PUBLIC_KEY` if you want a non-root
`deploy` user on the master. The panel does not need it.

## Admin panel

`admin-<app>.<root>` is a web console for the cluster: create and delete
components, edit their environment and their settings, deploy an image, rotate a
database password, and watch node state, autoscaler signals and alert rules.
Username and password come from `master-cloud-init.yaml` — still the only file
you fill in.

It is deliberately narrow about what it owns:

| | Owner |
|---|---|
| Components: create, configure, deploy, delete | **the panel**, or `bin/component` |
| A component's environment and its own credentials | **the panel** |
| Replica count at runtime, and placement | the autoscaler |
| The running image | CI, through the deploy webhook |
| Fleet policy, Hetzner, registry, alerting | `infra.env` — the panel edits what is safe and shows the rest read-only |
| The three infrastructure stacks | their files; the panel can only re-apply them |

That split is the whole point. A panel that lets you edit a value the next
deploy silently reverts is worse than one that declines — which is why a
component's page shows its live image and replica count rather than offering to
set them.

**Treat it as a root console.** It mounts the docker socket, so it can deploy
anything in the cluster and read every credential in it. Put the hostname behind
Cloudflare Access; the login is one factor, Access is the second. Six failed
attempts locks the source address out for five minutes.

To preview the interface without deploying anything:

```bash
python3 admin/preview_build.py && open admin/preview/index.html
```

## Component configuration

Everything an app reads from its environment lives in that component's own file,
edited in the panel or on the box:

```bash
component env api                       # list
component env api set LOG_LEVEL=debug   # edits the file
component env api unset LOG_LEVEL
component env api edit                  # $EDITOR, whole file at once
component deploy api                    # apply it
```

**The platform injects nothing.** Whatever is in that file is exactly what the
container gets — no framework variables, no reserved names, no database URL
helpfully filled in. If your app needs Redis, you copy the connection URL from
that Redis component's Credentials page into this component's environment, and
you decide what to call it.

In the panel the file has two views, switched with **Rows / Text** above the
editor: a row per variable, or the whole thing as text for pasting a `.env` in
one go. They are the same form — switching converts what you have edited, and
only the view on screen is submitted, so they cannot disagree about what gets
saved. The text view takes `KEY=VALUE` per line, comments and blank lines
dropped, a leading `export` allowed, quotes kept as part of the value, and a
line without `=` is an error rather than a silent drop.

> **A component's credentials are plain files, not docker secrets.** A Redis
> password lives in `components/<name>/secret.env` at 0600 and is injected into
> that one service, which means it appears in `docker service inspect` — readable
> by anyone who is already root on the master. The trade buys one-click rotation:
> Swarm secrets are immutable, so a rotate button on top of them is a
> versioned-secret dance instead of a button.

**Deploy paths, and there are exactly two:**

- `bin/stack-deploy <monitoring|ingress|admin>` — the infrastructure. Always safe
  to re-apply; nothing else owns state in those files.
- `bin/component deploy <name>` — one component, one stack.

A component's deploy reads three things back from the running service before
rendering, because applying the spec's copy of them is how an unrelated edit
rolls production back:

- **replicas** — the autoscaler owns the count at runtime. Applying the spec's
  number would cut a service from N back to its configured floor at whatever
  moment someone saved a memory limit.
- **image** — CI moved it on with `docker service update`. The spec's copy is
  whatever was last typed into a form.
- **placement** — the autoscaler moves the app between master and workers by
  adding and removing `node.role == worker`. Writing our idea of it would slam
  placement back on every deploy.

## Things that will bite you

- **A component's SLO is the whole policy for that component.** If its real p95
  already exceeds it, the scaler runs to the ceiling on day one and
  `ReplicaCeiling` fires. Set it from a real latency distribution, not a wish.
- **Reservations are mandatory, and load-bearing twice.** Swarm schedules
  against them, *and* the autoscaler derives every capacity number from them.
  The renderer refuses to emit a service without them, because one that looks
  free gets packed on top of VictoriaMetrics until something is OOM-killed.
  Understate one and the autoscaler believes in room that is not there.
- **Never label a stateful service `infra.workload=app`.** It would be moved
  onto a worker that is later drained and deleted, and its volume with it. The
  autoscaler warns loudly if it sees a volume on a service it manages, and
  `smoke-test` fails on it — but the label is yours to get right.
- **At the floor the master IS the request path.** That is the deal for not
  paying for an idle worker: while no workers exist, losing the master costs
  uptime and not just monitoring. Set `MIN_WORKERS=1` if that trade is wrong —
  one worker always up means the master carries nothing.
- **Do not put a `node.role` constraint on a component.** The autoscaler owns
  it. Pinning to workers makes the free floor an outage instead of a saving;
  pinning to the manager keeps production on the master forever.
- **A small master never reaches the free floor.** If the infrastructure's
  reservations leave no room for every component's minimum, one worker always
  stays up. The autoscaler says so on startup — look for `measured capacity` in
  its log.
- **Components share the fleet.** A staging copy is a component like any other:
  it reserves resources, and a real load test against it will buy a worker.
  Give it small reservations and a low `max_replicas`, or expect the bill.
- **Deleting a database keeps its volumes.** `docker stack rm` does not remove
  volumes and neither do we — a mistyped delete should cost you a redeploy, not
  your data. Remove `<name>-<n>-data` by hand once you are sure.
- **Rotating a database password breaks your clients, by design.** Nothing else
  in the cluster holds a copy, so nothing else needs updating; your application
  needs the new URL, and that is your move to make.
- **The master is a single point of failure for control, not traffic.** Enable
  Hetzner backups; the metrics history and every unmanaged volume live only there.
- **Secondary reads are a contract with your application, not a setting.** A
  managed Mongo ships with `readPreference=secondaryPreferred`, because read
  scaling is the point of a replica set. A secondary can be behind the write
  that produced what you are reading, and the only fix is on your side: one
  causally consistent session per request chain, with its `operationTime`
  carried forward. The Credentials tab has the code. Turn the switch off and
  every read goes to the primary — correct by construction, and then only a
  bigger machine can help read latency, which dataguard will say rather than
  adding a replica that could not have helped.
- **A client that cannot speak Sentinel will not fail over.** `redis://host:6379`
  is a promise that one server is the database, and no amount of infrastructure
  keeps that promise while the server changes. redis-py, ioredis, Lettuce and
  go-redis all speak it. If yours does not, that component is HA for the data
  and not for that client.
- **Redis backup is not MongoDB backup.** Mongo gets real point-in-time
  recovery — a continuous oplog between snapshots, so you can restore to a
  second. Redis gets an RDB snapshot plus the append-only file, replayed to the
  last `everysec` fsync: up to a second of writes gone. Both are called
  "backup"; only one of them is arbitrary-timestamp recovery.
- **A backup nobody has restored is a hypothesis.** Dataguard proves one
  restores on a schedule and refuses to change a database's shape without a
  recent VERIFIED backup, because a topology change can lose data. `BackupNeverVerified`
  is the alert; the Dataguard tab is where you see which gate is holding
  something back.
- **A database is never power-cycled onto a bigger plan.** The overseer can
  resize a worker; for a member it provisions a bigger machine, syncs onto it,
  promotes it and drops the old one. Same sequence as every other transition,
  interruptible at every step, and the old machine serves until it is not needed.
- **The data visualiser is full access with no password of its own.** It is
  never published and never on the tunnel: the View button proxies it through
  your panel session, it starts on the click, and dataguard stops it once
  nobody is looking. That proxy is the only mutating path in the panel without
  a CSRF check — a request through it carries the console's token, not ours —
  and what guards it is the session plus a Strict SameSite cookie.
- **Scale-down drains for at most 180s** before removing the node anyway. Long
  requests need that raised in `autoscaler.py`.
- **A silent alert rule is worse than no alert rule.** `NoHealthyReplicas` once
  matched a service name that no longer existed, so it could never fire and
  looked identical to "nothing is wrong". Now that component names are created
  at runtime, no rule may name one: they key on gauges the three infrastructure
  processes export,
  and `config/alerts_test.yml` pins that — including that the cluster-level
  gauges stay unlabelled — and, since the fleet moved out of the autoscaler,
  that BOTH SIDES OF A JOIN COME FROM ONE PROCESS. vmagent gives an unlabelled
  gauge the exporting service's name, so `X == 1 and Y < 1` across two exporters
  matches nothing, silently, forever. That is why every fleet gauge lives in the
  overseer rather than being split with the half that applies it. Run `promtool test rules config/alerts_test.yml` after
  touching `alerts.yml`. The `Watchdog` rule fires permanently on purpose — if
  the daily heartbeat stops arriving, the pipeline is broken, not quiet.

## Bring it up, in order

1. **Push this repo to GitHub and let the workflow run once.** The master
   pulls its three infrastructure images from GHCR rather than building them,
   so the commit you boot from has to have been published — check the Actions
   tab is green before creating the server. A public repository with public
   packages needs nothing else; a private one needs `GHCR_TOKEN` to have
   `read:packages`.

   There is nothing to configure for this. The image path is derived from
   `INFRA_REPO_URL` and the tag is the commit, so both halves of every name
   come from facts the cluster already has.
2. **Fill every `REPLACE_ME`** in the `VARIABLES` block. The one that matters
   most is **`ADMIN_PASSWORD`**: it is the key to the cluster. Bootstrap refuses
   to run while any of them is unset.

   Nothing about an application is in that file — no image, no port, no SLO, no
   replica counts, no service names. Those are properties of a component, and
   components are created after the cluster is up. Nothing here can go stale,
   because nothing here is a copy of something else.
3. **Create the master**: Hetzner CPX31, Ubuntu 24.04, **attached to the private
   network**, with the cloud-config pasted in.
4. **Watch it build**:
   ```bash
   ssh root@<ip> tail -f /var/log/infra-bootstrap.log
   ```
5. **Expect an empty cluster.** When it finishes, `docker service ls` shows
   monitoring, ingress and admin — and nothing else. That is the finished state
   of a boot, not a half-finished one:
   ```bash
   docker service ls                              # infrastructure only
   docker node ls                                 # just the master
   docker service logs -f monitoring_autoscaler   # "0 component(s)", 0 workers
   ```
6. **Add the panel's hostname** in Cloudflare:

   | Hostname | Target |
   |---|---|
   | `admin-<app>.<root>` | `http://<master-private-ip>:3000` |

   Grafana does not need one — it is reached through the panel at `/grafana/`.

7. **Put it behind Cloudflare Access.** The panel holds the docker socket and
   can read your Hetzner token, so its password is one factor and Access is the
   second. **Exempt `/hooks/deploy/*`** or give CI an Access service token, or
   your pipeline will be blocked before it reaches the panel.
8. **Create your components.** In the panel: **Components → New app**, or on the
   master:
   ```bash
   component create redis cache
   component deploy cache
   component create app api --image ghcr.io/you/app:sha-abc1234 --port 8080
   component env api set REDIS_URL="$(component show cache | grep target)"   # your call
   component deploy api
   ```
   Then add the app's hostname in Cloudflare, pointing at the local target the
   panel shows on its page (`http://api_app:8080`).
9. **Prove it works** — do not skip this:
   ```bash
   /opt/infra/bin/smoke-test --deep
   ```
   It checks the things that fail silently: whether the panel can actually reach
   docker, whether it has registry credentials, whether the password a Redis
   enforces is the one the panel shows you, whether a redeploy preserved the
   image and the replica count, whether your alert rules loaded. Fix anything red
   before trusting the cluster.
10. **Turn on autoscaling** for the components that should have it, on their
    Settings tab. Set the SLO from a real latency distribution.
11. **Wire up CI.** Open the component → **Deployments**, copy the endpoint and
    token into your app repo's secrets, and copy `docs/github-actions-deploy.yml`
    into `.github/workflows/`. One token per component.
12. **Push a commit** and watch it deploy.

To prove the Hetzner token and the worker cloud-init before you need them,
temporarily set `MIN_WORKERS=1` in the panel (Settings → Fleet), watch a worker
join and your components move onto it, then set it back to 0 and watch them hand
back and the server get deleted.

From here on you should not need to SSH in. Components, configuration,
credentials, redeploys, the firewall and the fleet policy are all in the panel;
the `bin/` scripts are the same paths it uses, if you prefer a terminal.
