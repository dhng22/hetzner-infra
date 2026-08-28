"""
Performance signals, shared by every process that reads or classifies them.

    query        VictoriaMetrics access and the sustain subquery
    expressions  the metric expressions a signal is read through
    discovery    what a service publishes, found rather than configured
    classify     what the numbers mean, and who owns the answer
    workloads    what a workload is, what it costs, and what policy it carries

Copied into the autoscaler, the overseer and the dataguard images. It is deliberately
stdlib + requests only: anything heavier here becomes a dependency of every
component that wants to read a signal.
"""

from . import classify, discovery, expressions, query, workloads  # noqa: F401
