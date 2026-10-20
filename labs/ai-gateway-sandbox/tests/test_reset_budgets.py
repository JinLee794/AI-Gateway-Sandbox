import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


LAB = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sandbox_app", LAB / "src" / "app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class ResetBudgetsTests(unittest.TestCase):
    """The page resets budgets in the background after a run; the next scenario skips its reset only if no call went out since."""

    def setUp(self):
        config = {"apimServiceId": "/subscriptions/test/resourceGroups/test/providers/Microsoft.ApiManagement/service/test"}
        self.patches = [patch.object(app, "CONFIG", config), patch.object(app, "az_token", lambda resource: "token"),
                        patch.object(app, "wait_for_named_value", lambda *args: True),
                        patch.object(app, "reactivate_suspended", lambda token: ([], [])),
                        patch.object(app, "http", lambda *args, **kwargs: (202, {}, "{}", 0.1)),
                        patch.dict(app._budget_unused, {"epoch": None, "at": 0.0, "used": 0.0})]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_reset_marks_budgets_fresh_so_the_next_scenario_skips(self):
        status, first = app.reset_budgets({"ifUsed": True})
        self.assertEqual(status, 200)
        self.assertFalse(first.get("skipped"))
        _, second = app.reset_budgets({"ifUsed": True})
        self.assertTrue(second["skipped"])
        self.assertEqual(second["epoch"], first["epoch"])

    def test_call_during_reset_keeps_the_next_reset(self):
        def call_while_applying(*args):
            app.gateway_auth({"key": "k"}, {"noToken": True}, "model")  # a build-your-own call while ARM applies the epoch
            return True

        with patch.object(app, "wait_for_named_value", call_while_applying):
            app.reset_budgets({"ifUsed": True})
        _, second = app.reset_budgets({"ifUsed": True})
        self.assertFalse(second.get("skipped"))


if __name__ == "__main__":
    unittest.main()
