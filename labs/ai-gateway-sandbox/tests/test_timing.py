import importlib.util
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch


LAB = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sandbox_app_timing", LAB / "src" / "app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class FakeCli:
    """Stands in for `az account get-access-token`: slow, and counts how often it runs."""
    def __init__(self, delay=0.0):
        self.delay, self.runs = delay, 0

    def __call__(self, args, **_kwargs):
        self.runs += 1
        time.sleep(self.delay)
        return type("R", (), {"returncode": 0, "stderr": "", "stdout": f'{{"accessToken": "t-{args[4]}", "expires_on": {time.time() + 3600}}}'})()


class TimingTests(unittest.TestCase):
    def setUp(self):
        app._capture.timing, app._capture.call, app._capture.presenter = [], "call-1", None
        self.patches = [patch.object(app, "_token_cache", {}), patch.object(app, "HOSTED", False),
                        patch.object(app.shutil, "which", return_value="az")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        app._capture.timing = app._capture.call = None

    def test_token_fetch_is_timed_then_cached(self):
        cli = FakeCli()
        with patch.object(app.subprocess, "run", cli):
            app.az_token(app.GATEWAY_AUDIENCE)
            app.az_token(app.GATEWAY_AUDIENCE)
        self.assertEqual(cli.runs, 1)
        fetch, cached = app._capture.timing
        self.assertEqual((fetch["kind"], fetch["step"]), ("token", "Fetching the Entra ID token for the gateway"))
        self.assertIn("Azure CLI", fetch["detail"])
        self.assertTrue(cached["detail"].startswith("cached"))
        self.assertEqual(app._activity, {})

    def test_activity_shows_a_fetch_in_flight_with_its_call(self):
        cli, seen = FakeCli(delay=0.3), {}
        with patch.object(app.subprocess, "run", cli):
            worker = threading.Thread(target=lambda: (setattr(app._capture, "call", "call-7"), app.az_token(app.ARM)))
            worker.start()
            time.sleep(0.15)
            seen.update(app.activity_now())
            worker.join()
        acts = seen["activities"]
        self.assertEqual([(a["kind"], a["call"]) for a in acts], [("token", "call-7")])
        self.assertIn("Azure Resource Manager token", acts[0]["what"])
        self.assertTrue(any(t["fetching"] for t in seen["tokens"] if t["resource"] == app.ARM))

    def test_a_slow_token_fetch_doesnt_block_another_resource(self):
        app._token_cache[app.GATEWAY_AUDIENCE] = ("gw", time.time() + 3600)
        with patch.object(app.subprocess, "run", FakeCli(delay=0.5)):
            slow = threading.Thread(target=app.az_token, args=(app.ARM,))
            slow.start()
            time.sleep(0.1)
            started = time.perf_counter()
            self.assertEqual(app.az_token(app.GATEWAY_AUDIENCE), "gw")
            self.assertLess(time.perf_counter() - started, 0.1)
            slow.join()

    def test_gateway_round_trip_is_labelled(self):
        with patch.dict(app.CONFIG, {"inferenceBaseUrl": "https://apim.example.net/inference/openai/v1"}):
            kind, what, _ = app._activity_of("POST", "https://apim.example.net/inference/openai/v1/chat/completions", {"model": "gpt-4.1"})
        self.assertEqual((kind, what), ("gateway", "Gateway round trip: chat.completions gpt-4.1"))


if __name__ == "__main__":
    unittest.main()
