"""
The Alerts page must never print Go template source at a human.
"""
import os
import sys
import types
import unittest

_ADMIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ADMIN)
sys.path.insert(1, os.path.dirname(_ADMIN))


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


RULES = {"data": {"groups": [{"name": "app", "rules": [
    {"id": "1", "name": "SLOBreach", "state": "firing",
     "labels": {"severity": "critical"},
     "annotations": {"summary": "p95 on {{ $labels.service }} is above its SLO"}},
    {"id": "2", "name": "ServiceCpuThrottled", "state": "inactive",
     "labels": {"severity": "warning"},
     "annotations": {"summary":
                     "{{ $labels.container_label_com_docker_swarm_service_name }}"
                     " is being cut off by its CPU cap"}},
]}]}}

ALERTS = {"data": {"alerts": [
    {"rule_id": "1", "state": "firing",
     "annotations": {"summary": "p95 on api_app is above its SLO"}},
]}}


class AlertTextTest(unittest.TestCase):
    def setUp(self):
        sys.modules.pop("swarm", None)
        import swarm
        self.swarm = swarm
        self.real = swarm.requests.get
        swarm.requests.get = lambda url, timeout=8: _Resp(
            ALERTS if url.endswith("/alerts") else RULES)

    def tearDown(self):
        self.swarm.requests.get = self.real
        sys.modules.pop("swarm", None)

    def rows(self):
        return {r["name"]: r for r in self.swarm.alerts()}

    def test_a_firing_rule_shows_the_instance_not_the_template(self):
        """
        `/api/v1/rules` returns the rule AS WRITTEN, braces and all — there is
        no instance to fill them from. `/api/v1/alerts` has the instances, with
        the labels already substituted, and that is the sentence worth showing
        because it names the service that is actually breaching.
        """
        row = self.rows()["SLOBreach"]
        self.assertEqual(row["summary"], "p95 on api_app is above its SLO")
        self.assertNotIn("{{", row["summary"])

    def test_an_inactive_rule_drops_the_placeholder_rather_than_printing_it(self):
        # No instance exists and none will until it fires, so nothing can fill
        # the braces. Bare reads slightly odd; raw template source reads like
        # the panel is broken, which is exactly what it looked like.
        row = self.rows()["ServiceCpuThrottled"]
        self.assertNotIn("{{", row["summary"])
        self.assertNotIn("$labels", row["summary"])
        self.assertIn("cut off by its CPU cap", row["summary"])

    def test_no_rule_anywhere_leaks_template_syntax(self):
        for name, row in self.rows().items():
            with self.subTest(rule=name):
                self.assertNotIn("{{", row["summary"])


if __name__ == "__main__":
    unittest.main()
