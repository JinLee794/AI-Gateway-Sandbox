import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch


LAB = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sandbox_app", LAB / "src" / "app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class BudgetSuspendTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "apimServiceId": "/subscriptions/test/resourceGroups/test/providers/Microsoft.ApiManagement/service/test",
            "logAnalyticsWorkspaceId": "test-workspace",
            "budgetPeriodSeconds": 2592000,
            "budgetSuspend": {
                "enabled": True,
                "trigger": "gateway",
                "subscriptions": ["user-ben"],
                "logicAppName": "la-budget-suspend-test",
                "logicAppId": "/subscriptions/test/resourceGroups/test/providers/Microsoft.Logic/workflows/test",
            },
            "tiers": [{"name": "silver", "budgetUsd": 0.01}],
            "agents": [{"name": "user-ben", "tier": "silver"}],
        }
        self.config_patch = patch.object(app, "CONFIG", self.config)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)

    def arm_response(self, method, url, *args, **kwargs):
        if "/namedValues/" in url:
            body = {"properties": {"value": "12345"}}
        elif "/runs?" in url:
            body = {"value": [{"name": "run-1", "properties": {"status": "Succeeded"}}]}
        elif "/subscriptions/user-ben?" in url:
            body = {"properties": {"state": "suspended"}}
        else:
            self.fail(f"Unexpected ARM request: {method} {url}")
        return 200, {}, json.dumps(body), 0

    def test_state_poll_does_not_query_log_analytics(self):
        with patch.object(app, "az_token", return_value="test-token"), \
             patch.object(app, "http", side_effect=self.arm_response), \
             patch.object(app, "law_query") as query:
            status, result = app.budget_suspend({"evidence": False})
        self.assertEqual(status, 200)
        self.assertEqual(result["subscriptions"][0]["state"], "suspended")
        self.assertEqual(result["subscriptions"][0]["limitMicroUsd"], 10000)
        self.assertEqual(result["trigger"], "gateway")
        self.assertNotIn("events", result)
        self.assertNotIn("spend", result)
        self.assertNotIn("alertRule", result["links"])
        query.assert_not_called()

    def test_reporting_uses_budget_period_not_alert_lookback(self):
        with patch.object(app, "az_token", return_value="test-token"), \
             patch.object(app, "http", side_effect=self.arm_response), \
             patch.object(app, "portal_logs_link", return_value="portal"), \
             patch.object(app, "law_query", return_value=[]) as query:
            status, result = app.budget_suspend({})
        self.assertEqual(status, 200)
        self.assertEqual(query.call_count, 2)
        self.assertEqual(query.call_args_list[0].args[1], "P2D")
        self.assertEqual(query.call_args_list[1].args[1], "PT2592000S")
        self.assertIn("events", result)
        self.assertIn("spend", result)
        self.assertIn("Properties.requestId", result["events"]["query"])

    def test_subscription_read_failure_is_visible(self):
        def failing_response(method, url, *args, **kwargs):
            if "/subscriptions/user-ben?" in url:
                return 403, {}, "Forbidden", 0
            return self.arm_response(method, url, *args, **kwargs)

        with patch.object(app, "az_token", return_value="test-token"), \
             patch.object(app, "http", side_effect=failing_response):
            _, result = app.budget_suspend({"evidence": False})
        self.assertIn("403", result["subscriptions"][0]["error"])
        self.assertNotIn("state", result["subscriptions"][0])

    def test_disabled_feature_does_not_call_azure(self):
        self.config["budgetSuspend"]["enabled"] = False
        with patch.object(app, "az_token") as token, patch.object(app, "law_query") as query:
            status, result = app.budget_suspend({})
        self.assertEqual(status, 200)
        self.assertFalse(result["enabled"])
        token.assert_not_called()
        query.assert_not_called()

    def test_public_configuration_excludes_callback_and_legacy_alert(self):
        self.config["budgetSuspend"].update(callbackUrl="not-for-browser", alertRuleName="legacy")
        public = app.public_config()["budgetSuspend"]
        self.assertNotIn("callbackUrl", public)
        self.assertNotIn("alertRuleName", public)
        self.assertEqual(public["trigger"], "gateway")

    def test_gateway_status_is_available_to_ui(self):
        self.assertIn("x-gw-suspension-status", app.GW_HEADERS)

    def test_disabled_policy_render_has_no_callback_reference(self):
        policy = (LAB / "product-policy.xml").read_text(encoding="utf-8")
        self.assertIn('id="subscription-budget"', policy)
        self.assertIn('id="session-budget"', policy)
        self.assertIn("{budget-suspend-policy}", policy)
        disabled = policy.replace("{budget-suspend-policy}", "")
        self.assertNotIn("budget-suspend-callback", disabled)


if __name__ == "__main__":
    unittest.main()
