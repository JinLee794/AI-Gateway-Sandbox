import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


LAB = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sandbox_app", LAB / "src" / "app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


def build_config(config):
    """Runs infra/scripts/build_config.py on a copy of the lab layout and returns sandbox.generated.json."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "infra" / "scripts").mkdir(parents=True)
        shutil.copy(LAB / "infra" / "scripts" / "build_config.py", root / "infra" / "scripts")
        (root / "sandbox-config.json").write_text(json.dumps(config), encoding="utf-8")
        subprocess.run([sys.executable, str(root / "infra" / "scripts" / "build_config.py")], check=True, capture_output=True)
        return json.loads((root / "infra" / "sandbox.generated.json").read_text(encoding="utf-8"))


def render_policy(enabled, placeholders):
    """Renders the models API policy the way main.bicep does (inferencePolicyXml)."""
    def fill(name):
        xml = (LAB / name).read_text(encoding="utf-8")
        for key, value in placeholders.items():
            xml = xml.replace(key, value)
        return xml
    lookup = fill("semantic-cache-lookup-policy.xml") if enabled else "<!-- Semantic cache is disabled -->"
    store = fill("semantic-cache-store-policy.xml") if enabled else ""
    policy = (LAB / "policy.xml").read_text(encoding="utf-8")
    return policy.replace("{semantic-cache-lookup}", lookup).replace("{semantic-cache-store}", store)


PLACEHOLDERS = {"{semantic-cache-tiers}": "gold,silver", "{semantic-cache-score-threshold}": "0.15",
                "{semantic-cache-ttl-seconds}": "600", "{semantic-cache-embedding-price}": "0.0200",
                "{semantic-cache-default-scope}": "session", "{semantic-cache-allowed-scopes}": "session,user,plan"}


class SemanticCacheConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((LAB / "sandbox-config.json").read_text(encoding="utf-8"))

    def test_enabled_config_passes_decimals_as_strings(self):
        generated = build_config(self.config)
        lab = generated["labParameters"]["semanticCacheConfig"]
        self.assertTrue(lab["enabled"])
        self.assertEqual(lab["scoreThreshold"], "0.15")
        self.assertEqual(lab["embeddingPrice"], "0.0200")
        self.assertEqual(lab["ttlSeconds"], 600)
        self.assertEqual(lab["embeddingsModel"]["name"], "text-embedding-3-small")
        self.assertEqual(lab["defaultScope"], "session")
        self.assertEqual(lab["allowedScopes"], ["session", "user", "plan"])
        self.assertNotIn("$comment", generated["uiConfig"]["semanticCache"])
        self.assertEqual(generated["uiConfig"]["semanticCache"]["tiers"], ["gold", "silver"])

    def test_invalid_scope_fails_the_build(self):
        for key, value in (("defaultScope", "everyone"), ("allowedScopes", ["session", "global"])):
            config = json.loads(json.dumps(self.config))
            config["semanticCache"][key] = value
            with self.assertRaises(subprocess.CalledProcessError, msg=key):
                build_config(config)

    def test_disabled_config_deploys_nothing(self):
        self.config["semanticCache"]["enabled"] = False
        self.assertEqual(build_config(self.config)["labParameters"]["semanticCacheConfig"], {"enabled": False})
        del self.config["semanticCache"]
        self.assertEqual(build_config(self.config)["labParameters"]["semanticCacheConfig"], {"enabled": False})

    def test_tiers_exist(self):
        tiers = {t["name"] for t in self.config["tiers"]}
        self.assertTrue(set(self.config["semanticCache"]["tiers"]) <= tiers)

    def test_rate_card_excludes_the_embeddings_deployment(self):
        overrides = json.loads((LAB / "pricing" / "overrides.json").read_text(encoding="utf-8"))
        self.assertIn({"model": self.config["semanticCache"]["embeddingsModel"]["name"]}, overrides["exclude"])


class SemanticCachePolicyTests(unittest.TestCase):
    def test_bicep_fills_every_placeholder(self):
        bicep = (LAB / "main.bicep").read_text(encoding="utf-8")
        snippets = "".join((LAB / n).read_text(encoding="utf-8") for n in ("semantic-cache-lookup-policy.xml", "semantic-cache-store-policy.xml"))
        for placeholder in set(re.findall(r"\{semantic-cache-[a-z-]+\}", snippets)):
            self.assertIn(f"'{placeholder}':", bicep, placeholder)
        self.assertEqual(set(re.findall(r"\{semantic-cache-[a-z-]+\}", snippets)), set(PLACEHOLDERS))

    def test_policy_placeholders_appear_once(self):
        # Bicep replace() swaps every occurrence: a placeholder in a comment would inject the snippet into it
        policy = (LAB / "policy.xml").read_text(encoding="utf-8")
        for placeholder in ("{semantic-cache-lookup}", "{semantic-cache-store}"):
            self.assertEqual(policy.count(placeholder), 1, placeholder)
        for name in ("semantic-cache-lookup-policy.xml", "semantic-cache-store-policy.xml"):
            xml = (LAB / name).read_text(encoding="utf-8")
            self.assertNotIn("{semantic-cache-lookup}", xml)
            self.assertNotIn("{semantic-cache-store}", xml)
            self.assertEqual(xml.count("<!--"), xml.count("-->"), name)

    def test_enabled_render_order(self):
        policy = render_policy(True, PLACEHOLDERS)
        self.assertNotRegex(policy, r"\{semantic-cache-[a-z-]+\}")
        inbound, outbound = policy.split("<outbound>")
        self.assertLess(inbound.index("<base />"), inbound.index("<llm-semantic-cache-lookup"), "lookup must run after the plan policy")
        self.assertLess(inbound.index("<llm-semantic-cache-lookup"), inbound.index("<set-backend-service"))
        self.assertLess(inbound.index("</llm-semantic-cache-lookup>"), inbound.index("<llm-emit-token-metric"), "a hit must not emit token metrics")
        self.assertLess(outbound.index("<llm-semantic-cache-store"), outbound.index("<base />"), "store must be first in outbound so a hit still runs pricing")
        self.assertLess(outbound.index('fragment-id="responses-pricing"'), outbound.index('fragment-id="semantic-cache-pricing"'))
        self.assertLess(outbound.index('fragment-id="semantic-cache-pricing"'), outbound.index('name="CostMicroUSD"'))
        self.assertLess(outbound.index('fragment-id="semantic-cache-pricing"'), outbound.index('fragment-id="chargeback-record"'))
        self.assertEqual(inbound.count("<vary-by>"), 4)
        self.assertIn('<vary-by>@((string)context.Variables["semanticCachePartition"])</vary-by>', inbound)
        self.assertLess(inbound.index('name="semanticCachePartition"'), inbound.index("<llm-semantic-cache-lookup"))
        self.assertIn('<set-header name="x-gw-cache-scope" exists-action="delete" />', inbound, "the scope header must not reach the backend")
        self.assertIn('score-threshold="0.15"', inbound)
        self.assertIn('duration="600"', outbound)

    def test_disabled_render_has_no_cache_policy(self):
        policy = render_policy(False, PLACEHOLDERS)
        self.assertNotIn("llm-semantic-cache", policy)
        self.assertNotRegex(policy, r"\{semantic-cache-[a-z-]+\}")

    def test_other_cache_policies_stay_internal(self):
        for name in ("responses-policy.xml", "purview-dlp-fragment.xml", "budget-suspend-policy.xml"):
            xml = (LAB / name).read_text(encoding="utf-8")
            for tag in re.findall(r"<cache-(?:lookup|store|remove)-value[\s\S]*?/>", xml):
                self.assertIn('caching-type="internal"', tag, f"{name}: {tag}")


class SemanticCacheUiTests(unittest.TestCase):
    def test_headers_reach_the_ui(self):
        for header in ("x-gw-semantic-cache", "x-gw-cache-saved-usd", "x-gw-cache-saved-tokens", "x-gw-cache-lookup-usd",
                       "x-gw-cache-scope", "x-gw-cache-partition"):
            self.assertIn(header, app.GW_HEADERS)

    def test_cache_scope_is_forwarded_on_model_calls_only(self):
        agent = {"name": "user-alice", "key": "k"}
        for scope in ("session", "user", "plan"):
            self.assertEqual(app.gateway_auth(agent, {"noToken": True, "cacheScope": scope}, "model")["x-gw-cache-scope"], scope)
        self.assertNotIn("x-gw-cache-scope", app.gateway_auth(agent, {"noToken": True, "cacheScope": "everyone"}, "model"))
        self.assertNotIn("x-gw-cache-scope", app.gateway_auth(agent, {"noToken": True}, "model"))
        self.assertNotIn("x-gw-cache-scope", app.gateway_auth(agent, {"noToken": True, "cacheScope": "plan"}, "tool"))

    def test_scenario_is_off_until_deployed(self):
        static = {"enabled": True, "tiers": ["gold"], "$comment": "x"}
        with patch.object(app, "CONFIG", {"semanticCache": static, "semanticCacheResource": {"enabled": False}}):
            self.assertFalse(app.public_config()["semanticCache"]["enabled"])
        with patch.object(app, "CONFIG", {"semanticCache": static, "semanticCacheResource": {"enabled": True, "redisName": "redis-x", "redisId": "/subscriptions/s/x"}}):
            config = app.public_config()
            self.assertTrue(config["semanticCache"]["enabled"])
            self.assertNotIn("$comment", config["semanticCache"])
            self.assertEqual(config["semanticCacheResource"]["redisName"], "redis-x")
            self.assertTrue(config["links"]["redis"].endswith("/subscriptions/s/x/overview"))
        with patch.object(app, "CONFIG", {}):
            self.assertFalse(app.public_config()["semanticCache"]["enabled"])
            self.assertIsNone(app.public_config()["links"]["redis"])


if __name__ == "__main__":
    unittest.main()
