"""
What "add a replica" MEANS, per engine.

Everything above this file is engine-agnostic: the state machine decides that a
component should have one more member, and the gates decide whether that is safe
right now. This is where that becomes `rs.reconfig` or `SENTINEL MONITOR`, and it
is the only place either appears.

TWO RULES HOLD FOR BOTH ENGINES.

THE TRUTH IS THE SERVER, NEVER OUR NOTES. Every method reads live state and
returns it; nothing here caches what it did last time. A dataguard restart
midway through adding a member must resume from what the replica set actually
says, because that is the only description of the cluster that cannot be stale.

NO CREDENTIAL EVER REACHES ARGV. The password is read out of the member
service's own container spec — dataguard holds the docker socket, which is
root-equivalent already, so this adds no privilege — and passed to the driver,
never to a command line. The master's process table is readable by anything else
on the box.
"""

import logging

log = logging.getLogger("dataguard")

#: What mongod answers with while it is running `--replSet` and has never been
#: given a configuration. The one error that means "say the word", as opposed to
#: every other one, which means "do not touch this".
NOT_YET_INITIALIZED = 94

#: Replica set member states worth naming. Everything else is transitional.
PRIMARY = "PRIMARY"
SECONDARY = "SECONDARY"
STARTUP = "STARTUP2"
DOWN = "DOWN"


class Unavailable(Exception):
    """The engine could not be reached. Never a reason to change anything."""


class Refused(Exception):
    """The engine was reached and said no. Reported, never retried blindly."""


class Member:
    """One replica, as the engine describes it right now."""

    def __init__(self, name, host, state, lag_seconds=None, votes=1, priority=1.0,
                 hidden=False):
        self.name = name
        self.host = host
        self.state = state
        self.lag_seconds = lag_seconds
        self.votes = votes
        self.priority = priority
        self.hidden = hidden

    @property
    def healthy(self):
        return self.state in (PRIMARY, SECONDARY)

    def __repr__(self):
        return f"<{self.name} {self.state} lag={self.lag_seconds}>"


class Topology:
    """What the engine currently is. Read, never assembled from intent."""

    def __init__(self, members, primary=None, ok=True, detail=""):
        self.members = list(members)
        self.primary = primary
        self.ok = ok
        self.detail = detail

    @property
    def voting(self):
        return [m for m in self.members if m.votes > 0]

    @property
    def healthy_voting(self):
        return [m for m in self.voting if m.healthy]

    @property
    def has_majority(self):
        """
        A majority of the CONFIGURED voting members must be reachable.

        Configured, not healthy — that is the whole point of the arithmetic. A
        three-member set with two members down has a healthy voting count of
        one, and one is not a majority of three, so it has no primary and takes
        no writes. Computing the majority from the survivors instead would say
        everything is fine right up until the data stopped.
        """
        return len(self.healthy_voting) > len(self.voting) // 2

    @property
    def worst_lag(self):
        lags = [m.lag_seconds for m in self.members
                if m.state == SECONDARY and m.lag_seconds is not None]
        return max(lags) if lags else None

    def by_host(self, host):
        return next((m for m in self.members if m.host == host), None)


class Engine:
    """
    The verbs the state machine needs, and nothing else.

    Deliberately small. Everything an engine could be asked to do that is not on
    this list is a decision, and decisions live above.
    """

    #: Whether adding a member changes the majority arithmetic. True for a
    #: replica set (members vote); False for sentinel (the sentinels vote, and
    #: they are a separate quorum from the data replicas).
    VOTING_MEMBERS = True

    def topology(self):
        raise NotImplementedError

    def initiate(self, host):
        """
        Bring an empty set into existence as one member, if it has none yet.

        Returns True only if this call is what created it. The default is
        False: for engines where a lone server is already a working primary
        there is nothing to create.
        """
        return False

    def add_member(self, host):
        raise NotImplementedError

    def remove_member(self, host):
        raise NotImplementedError

    def step_down(self, host, seconds=60):
        raise NotImplementedError

    def promote(self, host):
        raise NotImplementedError

    def set_hidden(self, host, hidden):
        raise NotImplementedError


# ---------------------------------------------------------------------------
# MongoDB
# ---------------------------------------------------------------------------

class MongoEngine(Engine):
    """
    A replica set, driven through pymongo against the seed list.

    RECONFIGURATION IS ONE MEMBER AT A TIME, ALWAYS. `rs.reconfig` replaces the
    whole config document, so adding two members in one call moves a
    three-member set straight to five — and for the moment between the config
    committing and the new members finishing their initial sync, the majority is
    three of five while only three exist and two of them are busy. One at a time
    keeps every intermediate state a set that can elect.
    """

    VOTING_MEMBERS = True

    def __init__(self, client_factory, set_name, direct_factory=None):
        self._client = client_factory
        # A SECOND way in, connected to exactly one member and doing no topology
        # discovery. `initiate` is the one thing that cannot use the pooled
        # client: a set with no configuration has no primary, so the driver
        # will not select any of its servers for a command, and the seed list
        # times out instead of reporting what is wrong.
        self._direct = direct_factory
        self.set_name = set_name

    def _admin(self):
        try:
            return self._client().admin
        except Exception as exc:                                 # noqa: BLE001
            raise Unavailable(str(exc)) from exc

    def topology(self):
        try:
            status = self._admin().command("replSetGetStatus")
        except Exception as exc:                                 # noqa: BLE001
            # An UNREACHABLE set is not an empty one. Returning no members would
            # read as "nothing configured" and invite the state machine to build
            # the whole thing again on top of a running database.
            return Topology([], ok=False, detail=str(exc))
        try:
            config = self._admin().command("replSetGetConfig")["config"]
        except Exception as exc:                                 # noqa: BLE001
            return Topology([], ok=False, detail=str(exc))

        cfg = {m["host"]: m for m in config.get("members", [])}
        primary_optime = next(
            (m.get("optimeDate") for m in status.get("members", [])
             if m.get("stateStr") == PRIMARY), None)
        members, primary = [], None
        for entry in status.get("members", []):
            host = entry.get("name", "")
            conf = cfg.get(host, {})
            lag = None
            optime = entry.get("optimeDate")
            if primary_optime is not None and optime is not None:
                lag = max(0.0, (primary_optime - optime).total_seconds())
            member = Member(
                name=host.split(":")[0], host=host,
                state=entry.get("stateStr", DOWN), lag_seconds=lag,
                votes=int(conf.get("votes", 1)),
                priority=float(conf.get("priority", 1.0)),
                hidden=bool(conf.get("hidden", False)))
            members.append(member)
            if member.state == PRIMARY:
                primary = member
        return Topology(members, primary=primary, ok=True)

    def initiate(self, host):
        """
        Create the one-member configuration a fresh set is missing.

        `mongod --replSet` starts a server that REFUSES EVERY COMMAND until it
        has been given a configuration — including the reads a client makes to
        discover a primary. So a brand new component is not slow or degraded,
        it is inert: nothing on the connection string works, and the only thing
        it will say is `NotYetInitialized`. Somebody has to say the word once,
        and it is this process, because it is the one that owns the shape of
        the set for the rest of the component's life.

        ONE MEMBER, always, whatever the pool is. The connection string names
        every member the component will ever have, but the configuration names
        only the ones that exist — a set configured with four members while one
        is running is a set that cannot reach a majority and therefore cannot
        elect anybody. Members 2..n join later, one at a time, through
        `add_member`.

        Refuses unless the server says NotYetInitialized (94), so a set that
        already exists is never rebuilt on top of its own data, and a server
        that is merely unreachable is left alone.
        """
        if self._direct is None:
            return False
        try:
            admin = self._direct(host).admin
        except Exception as exc:                                 # noqa: BLE001
            raise Unavailable(str(exc)) from exc
        try:
            admin.command("replSetGetStatus")
        except Exception as exc:                                 # noqa: BLE001
            # The SERVER'S OWN code, not the text of the message. `mongod`
            # answers an uninitiated set with 94 and nothing else does; the
            # English beside it is free to change between releases.
            if getattr(exc, "code", None) != NOT_YET_INITIALIZED:
                raise Unavailable(str(exc)) from exc
        else:
            return False
        try:
            admin.command("replSetInitiate", {
                "_id": self.set_name, "version": 1,
                "members": [{"_id": 0, "host": host}]})
        except Exception as exc:                                 # noqa: BLE001
            raise Refused(str(exc)) from exc
        return True

    def _reconfig(self, mutate):
        """
        Read the config, change exactly one thing, write it back with majority.

        `{w: "majority"}` on the reconfig itself is what stops a config change
        being accepted by a minority and then rolled back by an election —
        leaving the set running a configuration nobody chose and nothing recorded.
        """
        admin = self._admin()
        try:
            config = admin.command("replSetGetConfig")["config"]
        except Exception as exc:                                 # noqa: BLE001
            raise Unavailable(str(exc)) from exc
        config = dict(config)
        config["version"] = int(config.get("version", 1)) + 1
        mutate(config)
        try:
            admin.command("replSetReconfig", config, maxTimeMS=30000)
        except Exception as exc:                                 # noqa: BLE001
            raise Refused(str(exc)) from exc

    def add_member(self, host):
        def mutate(config):
            members = list(config.get("members", []))
            if any(m["host"] == host for m in members):
                raise Refused(f"{host} is already a member")
            used = {int(m["_id"]) for m in members}
            new_id = next(i for i in range(0, 255) if i not in used)
            # priority 0 and votes 0 while it syncs. A member that can vote
            # before it has any data changes the majority arithmetic the instant
            # the config commits, and an election in that window can hand the
            # primary role to a machine holding nothing.
            members.append({"_id": new_id, "host": host, "priority": 0, "votes": 0})
            config["members"] = members
        self._reconfig(mutate)
        log.info("%s: %s added as a non-voting, non-electable member; it will be "
                 "promoted once it has caught up", self.set_name, host)

    def enfranchise(self, host):
        """
        Give a caught-up member its vote and its priority.

        Separate from `add_member` on purpose, and the state machine only calls
        it after the member reads SECONDARY with acceptable lag. This is the step
        that changes the majority, so it is the step that must be earned.
        """
        def mutate(config):
            for member in config.get("members", []):
                if member["host"] == host:
                    member["votes"] = 1
                    member["priority"] = 1
                    return
            raise Refused(f"{host} is not a member")
        self._reconfig(mutate)
        log.info("%s: %s now votes", self.set_name, host)

    def promote(self, host):
        """
        Make this member the primary by making it the obvious choice.

        Priority, not a forced stepdown. A member whose priority is higher than
        the current primary's calls an election itself once it is within ten
        seconds of the primary — mongod's own priority takeover — so the handover
        happens when the set agrees it is safe rather than when we asked. A
        stepdown does the opposite: it makes the primary unavailable first and
        lets the election work out the rest, which is how a set ends up
        promoting the member we were trying to replace.

        Every other member is put back to 1 in the same reconfig. Leaving a
        previous winner at 2 means the NEXT upgrade has two members convinced
        they should be primary, and they take turns.
        """
        def mutate(config):
            found = False
            for member in config["members"]:
                if member["host"] == host:
                    found = True
                    if member.get("votes", 1) == 0 or member.get("hidden"):
                        raise Refused(
                            f"{host} cannot be promoted while it is non-voting "
                            "or hidden")
                    member["priority"] = 2
                elif member.get("priority", 1) > 0:
                    member["priority"] = 1
            if not found:
                raise Refused(f"{host} is not in the set")
        self._reconfig(mutate)

    def remove_member(self, host):
        def mutate(config):
            members = [m for m in config.get("members", []) if m["host"] != host]
            if len(members) == len(config.get("members", [])):
                raise Refused(f"{host} is not a member")
            config["members"] = members
        self._reconfig(mutate)
        log.info("%s: %s removed from the set", self.set_name, host)

    def set_hidden(self, host, hidden):
        """
        Take a member out of, or back into, read eligibility.

        A hidden member receives no reads at all, whatever the client's
        readPreference. This is the tight lag gate: `maxStalenessSeconds` is
        driver-side and bottoms out at 90 seconds, which is a very long time to
        serve a stale read from a database that knows better.
        """
        def mutate(config):
            for member in config.get("members", []):
                if member["host"] == host:
                    member["hidden"] = bool(hidden)
                    if hidden:
                        # Mongo refuses a hidden member with priority > 0.
                        member["priority"] = 0
                    return
            raise Refused(f"{host} is not a member")
        self._reconfig(mutate)

    def step_down(self, host, seconds=60):
        """
        Hand the primary role to somebody else, gracefully.

        `secondaryCatchUpPeriodSecs` is the whole safety of this: the primary
        waits for a secondary to catch up to its own optime before standing
        down, so the election cannot promote a member that is behind. Forcing
        instead is how a stepdown becomes a rollback of everything written in
        the last few seconds.
        """
        try:
            self._admin().command(
                "replSetStepDown", seconds, secondaryCatchUpPeriodSecs=30)
        except Exception as exc:                                 # noqa: BLE001
            # The driver's connection is severed BY a successful stepdown, so a
            # network error here is the expected outcome, not a failure. The
            # caller re-reads the topology to find out what actually happened —
            # which is the correct way to learn it in any case.
            log.info("%s: stepdown of %s returned %s; re-reading the set",
                     self.set_name, host, exc)

    def op_latencies(self):
        """
        (read_micros, write_micros) as MONGOD accounts for them, or (None, None).

        The application's driver timers say the database is slow; only this says
        which HALF, and the two answers lead to opposite actions — more replicas
        for reads, a bigger machine for writes. Unknown is returned as None
        rather than as zero, because guessing "reads" sends the loop to add a
        replica for a write-bound database, which cannot help and costs a machine.
        """
        try:
            latencies = (self._admin().command("serverStatus")
                         .get("opLatencies") or {})
        except Exception:                                        # noqa: BLE001
            return None, None

        def avg(kind):
            entry = latencies.get(kind) or {}
            ops = entry.get("ops") or 0
            return (entry.get("latency", 0) / ops) if ops else None

        return avg("reads"), avg("writes")

    def collection_stats(self):
        """
        (data_bytes, storage_bytes) across every database, or (None, None).

        Used for one decision and one only: whether a target machine's disk can
        hold this database. CPU and memory are recoverable mistakes; a full disk
        on a database is not.
        """
        try:
            admin = self._admin()
            names = admin.command("listDatabases").get("databases", [])
            data = sum(int(d.get("sizeOnDisk", 0) or 0) for d in names)
            return data, data
        except Exception:                                        # noqa: BLE001
            return None, None


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------

class RedisEngine(Engine):
    """
    A primary with replicas, watched by a sentinel quorum.

    Different arithmetic from Mongo and the difference matters: the SENTINELS
    vote, not the data nodes. Adding a replica therefore does not change the
    quorum, and the sentinel count is what has to stay odd. That is what
    `VOTING_MEMBERS` is False for, and where it is read: `would_break_majority`,
    which is what stops a REMOVAL taking the quorum with it.

    It is deliberately not read while growing. `plan.next_action` used to accept
    it as a parameter and never look at it, under a comment claiming there was
    an odd-voting-set gate to skip; there was not, and there should not be — an
    even voting set is a legal waypoint on the way to an odd one, which is
    exactly what "building out to a set that can lose one" means.
    """

    VOTING_MEMBERS = False

    def __init__(self, sentinel_factory, master_name):
        self._sentinel = sentinel_factory
        self.master_name = master_name

    def _s(self):
        try:
            return self._sentinel()
        except Exception as exc:                                 # noqa: BLE001
            raise Unavailable(str(exc)) from exc

    def topology(self):
        try:
            sentinel = self._s()
            master = sentinel.discover_master(self.master_name)
            replicas = sentinel.discover_slaves(self.master_name)
        except Exception as exc:                                 # noqa: BLE001
            return Topology([], ok=False, detail=str(exc))

        members = []
        primary = None
        if master:
            primary = Member(name=master[0], host=f"{master[0]}:{master[1]}",
                             state=PRIMARY)
            members.append(primary)
        for host, port in replicas or []:
            members.append(Member(name=host, host=f"{host}:{port}", state=SECONDARY))
        return Topology(members, primary=primary, ok=bool(master))

    def add_member(self, host):
        """
        A Redis replica joins by being told who to follow, not by a reconfig.

        There is no cluster-wide config document to edit: the new server issues
        REPLICAOF and the sentinels notice it. That is done by the member's own
        service command at start, so this verb has nothing to do — and saying so
        is better than pretending a no-op succeeded.
        """
        log.info("%s: %s follows the primary through its own REPLICAOF at start; "
                 "the sentinels will discover it", self.master_name, host)

    def remove_member(self, host):
        """
        Stop watching a replica that is going away.

        `SENTINEL RESET` is the only way to make the sentinels forget a replica
        that no longer exists; without it they keep reporting it as down forever,
        and `+sdown` events for a machine nobody deleted look exactly like a
        machine that failed.
        """
        try:
            for sentinel in self._s().sentinels:
                sentinel.execute_command("SENTINEL", "RESET", self.master_name)
        except Exception as exc:                                 # noqa: BLE001
            raise Refused(str(exc)) from exc

    def step_down(self, host, seconds=60):
        try:
            for sentinel in self._s().sentinels:
                sentinel.execute_command("SENTINEL", "FAILOVER", self.master_name)
                return
        except Exception as exc:                                 # noqa: BLE001
            raise Refused(str(exc)) from exc

    def promote(self, host):
        """
        Ask the sentinels to fail over. They pick the replica, not us.

        There is no priority reconfig to do here: `SENTINEL FAILOVER` promotes
        whichever replica the sentinels rank best, and `replica-priority` is set
        on the replica itself rather than through the sentinel connection this
        engine holds. So this is a request, and the loop confirms the result the
        same way it confirms everything else — by reading the topology again
        next pass and acting on what it says.
        """
        log.info("%s: asking the sentinels to fail over; they choose the "
                 "replica, and %s is only the one we would prefer",
                 self.master_name, host)
        self.step_down(host)

    def set_hidden(self, host, hidden):
        """
        Not available, and saying so beats a silent no-op.

        Redis has no per-replica read-eligibility flag; a sentinel-aware client
        picks a replica itself. Lag is handled by the state machine refusing to
        promote a lagging replica, not by taking it out of rotation.
        """
        raise Refused("redis replicas cannot be hidden; the client chooses one")
