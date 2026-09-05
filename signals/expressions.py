"""
The metric expressions every performance signal is read through.

One definition each, imported by the autoscaler and the overseer. A service
is "slow" or "busy" according to these and nothing else, which is what lets two
processes reach the same verdict about the same service.
"""

#: cadvisor writes the Swarm service name into this label. Everything per-replica
#: is grouped by it and divided by that service's OWN limit by the caller — which
#: is what removed the hand-maintained APP_CPU_LIMIT.
CPU_LABEL = "container_label_com_docker_swarm_service_name"

CPU_BY_SERVICE = (f'avg by ({CPU_LABEL}) (rate(container_cpu_usage_seconds_total'
                  f'{{{CPU_LABEL}!=""}}[3m]))')

# Working set rather than RSS or usage: usage counts reclaimable page cache, so a
# container that has merely READ a large file reads as nearly full and would
# scale a service out for having done its job.
MEM_BY_SERVICE = (f'avg by ({CPU_LABEL}) (container_memory_working_set_bytes'
                  f'{{{CPU_LABEL}!=""}})')


def unit_of(name):
    return "milliseconds" if ("millis" in name or name.endswith("_ms")) else "seconds"


def p95_expr(histogram, unit, by="service"):
    """
    p95 in milliseconds, grouped by `by`. `service` is written by vmagent.

    `by` exists for DEPENDENCY timers, which have to be grouped by whatever
    names the thing being called — `host`, `server_address` — so one slow
    third party can be told apart from the rest of a service's outbound calls.
    Grouping those by `service` would average the slow one into the fast ones
    and name nothing.
    """
    scale = 1000 if unit == "seconds" else 1
    return (f"histogram_quantile(0.95, sum by ({by}, le) "
            f"(rate({histogram}[2m]))) * {scale}")


def mean_expr(base, unit):
    """
    Mean latency by service, in milliseconds.

    Used when a service publishes a timer but no buckets. It is NOT a p95 and is
    not pretended to be one: the mean sits below the tail, so a service compared
    against a p95 SLO through this scales up later than one with a real
    histogram. That is still enormously better than no latency signal at all,
    and it is the common case — a Micrometer/Prometheus timer publishes _sum and
    _count by default and publishes buckets only when someone enables them.

    THE 2-MINUTE WINDOW IS WHY LOW TRAFFIC LIES. With four requests in it, this
    fraction is one request's duration, held steady for two minutes — long
    enough to satisfy any sustain check. That is not a bug in the expression; it
    is why a latency breach has to be corroborated by local saturation before
    anything acts on it. See signals.classify.saturated.
    """
    scale = 1000 if unit == "seconds" else 1
    return (f"(sum by (service) (rate({base}_sum[2m])) "
            f"/ sum by (service) (rate({base}_count[2m]))) * {scale}")
