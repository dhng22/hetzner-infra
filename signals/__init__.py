"""
Performance signals, shared by every process that reads or classifies them.

    query        VictoriaMetrics access and the sustain subquery
    expressions  the metric expressions a signal is read through
    discovery    what a service publishes, found rather than configured
    classify     what the numbers mean, and who owns the answer

Copied into both the autoscaler and the dispatcher images. It is deliberately
stdlib + requests only: anything heavier here becomes a dependency of every
component that wants to read a signal.
"""

from . import classify, discovery, expressions, query  # noqa: F401
