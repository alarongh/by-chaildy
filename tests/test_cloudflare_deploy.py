import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts import cloudflare_deploy as deploy


class FreePlanGuardTests(unittest.TestCase):
    account = "a" * 32

    def authorize(self):
        return patch.object(deploy, "command", side_effect=[
            json.dumps({"accounts": [{"id": self.account}]}),
            json.dumps({"token": "test-only"}),
        ])

    def test_dashboard_evidence_is_fresh_and_bound_to_account(self):
        evidence = {"account": self.account, "plan": "free", "verified_at": 10000,
                    "source": "https://dash.cloudflare.com/"+self.account+"/workers/plans"}
        cases = [({}, True), ({"verified_at": 6399}, False),
                 ({"verified_at": 10001}, False), ({"account": "b"*32}, False),
                 ({"plan": "paid"}, False), ({"source": "https://example.com/"}, False)]
        for changes, permitted in cases:
            with self.subTest(changes=changes), self.authorize(), \
                 patch.dict(os.environ, {}, clear=True), \
                 patch.object(deploy, "state", return_value={}), \
                 patch.object(deploy.time, "time", return_value=10000), \
                 patch.object(deploy, "request", side_effect=RuntimeError("Remote request failed: HTTP 403")), \
                 patch.object(Path, "exists", return_value=True), \
                 patch.object(Path, "read_text", return_value=json.dumps({**evidence, **changes})):
                if permitted:
                    self.assertEqual(deploy.free_account(), self.account)
                else:
                    with self.assertRaises(RuntimeError):
                        deploy.free_account()

    def test_paid_subscription_always_blocks_deployment(self):
        with self.authorize(), patch.dict(os.environ, {}, clear=True), \
             patch.object(deploy, "state", return_value={}), \
             patch.object(deploy, "request", return_value={"success": True, "result": [
                 {"rate_plan": {"id": "workers_paid"}, "price": 5}]}):
            with self.assertRaisesRegex(RuntimeError, "paid Workers"):
                deploy.free_account()

    def test_network_failure_does_not_use_dashboard_exception(self):
        with self.authorize(), patch.dict(os.environ, {}, clear=True), \
             patch.object(deploy, "state", return_value={}), \
             patch.object(deploy, "request", side_effect=RuntimeError("Remote request failed: HTTP 503")), \
             patch.object(Path, "exists", return_value=False):
            with self.assertRaises(RuntimeError):
                deploy.free_account()


if __name__ == "__main__":
    unittest.main()
