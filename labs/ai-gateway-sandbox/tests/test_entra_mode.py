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

PLAN_HOOKS = ("{plan-inbound}", "{plan-outbound}", "{plan-on-error}")


def build_config(config):
    """Runs infra/scripts/build_config.py on a copy of the lab layout and returns sandbox.generated.json."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "infra" / "scripts").mkdir(parents=True)
        shutil.copy(LAB / "infra" / "scripts" / "build_config.py", root / "infra" / "scripts")
        (root / "sandbox-config.json").write_text(json.dumps(config), encoding="utf-8")
        subprocess.run([sys.executable, str(root / "infra" / "scripts" / "build_config.py")], check=True, capture_output=True)
        return json.loads((root / "infra" / "sandbox.generated.json").read_text(encoding="utf-8"))


def render_entra_inbound(tiers):
    """Renders the entra-plan-inbound fragment the way main.bicep does (one tier branch per customer-facing tier)."""
    template = (LAB / "entra-plan-tier.xml").read_text(encoding="utf-8")
    branches = ""
    for tier in tiers:
        if tier.get("internal"):
            continue
        xml = template
        for key, value in {"{tier}": tier["name"], "{allowed-models}": ",".join(tier["allowedModels"]),
                           "{fallback-model}": tier["fallbackModel"], "{on-disallowed-model}": tier["onDisallowedModel"],
                           "{max-output-tokens}": str(tier["maxOutputTokens"]), "{tpm}": str(tier["tpm"]),
                           "{token-quota}": str(tier["tokenQuota"]), "{token-quota-period}": tier["tokenQuotaPeriod"]}.items():
            xml = xml.replace(key, value)
        branches += xml
    xml = (LAB / "entra-plan-inbound-fragment.xml").read_text(encoding="utf-8")
    for key, value in {"{tenant-id}": "tenant", "{gateway-app-id}": "app-id", "{budget-period-seconds}": "2592000",
                       "{tier-branches}": branches}.items():
        xml = xml.replace(key, value)
    return xml


class EntraModeConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((LAB / "sandbox-config.json").read_text(encoding="utf-8"))
        self.config["entraMode"]["enabled"] = True

    def test_disabled_by_default(self):
        config = json.loads((LAB / "sandbox-config.json").read_text(encoding="utf-8"))
        self.assertFalse(config["entraMode"]["enabled"])
        self.assertEqual(build_config(config)["labParameters"]["entraModeConfig"], {"enabled": False})

    def test_budgets_become_micro_usd(self):
        lab = build_config(self.config)["labParameters"]["entraModeConfig"]
        care = next(g for g in lab["groups"] if g["name"] == "customer-care")
        self.assertEqual((care["poolMicroUsd"], care["memberMicroUsd"]), (30000, 20000))
        self.assertNotIn("budgetUsd", care)
        self.assertEqual(lab["groupMembershipClaims"], "SecurityGroup")
        self.assertTrue(any(i["group"] == "" for i in lab["identities"]), "a persona without a group shows the 403")

    def test_unknown_tier_or_group_fails_the_build(self):
        for mutate in (lambda c: c["entraMode"]["groups"][0].update(tier="platinum"),
                       lambda c: c["entraMode"]["identities"][0].update(group="nobody"),
                       lambda c: c["entraMode"].update(includeDeployerIn="nobody")):
            config = json.loads(json.dumps(self.config))
            mutate(config)
            with self.assertRaises(subprocess.CalledProcessError):
                build_config(config)

    def test_scenario_team_has_two_members(self):
        groups = {}
        for identity in self.config["entraMode"]["identities"]:
            groups.setdefault(identity["group"], []).append(identity["name"])
        self.assertTrue(any(len(members) >= 2 for group, members in groups.items() if group))


class EntraModePolicyTests(unittest.TestCase):
    def test_plan_hooks_appear_once(self):
        # Bicep replace() swaps every occurrence: a hook in a comment would inject the fragment into it
        policy = (LAB / "policy.xml").read_text(encoding="utf-8")
        for hook in PLAN_HOOKS:
            self.assertEqual(policy.count(hook), 1, hook)
        inbound, rest = policy.split("<outbound>")
        outbound, on_error = rest.split("<on-error>")
        self.assertLess(inbound.index("<base />"), inbound.index("{plan-inbound}"))
        self.assertLess(inbound.index("{plan-inbound}"), inbound.index('fragment-id="payer-attribution"'), "the payer must be known before attribution")
        self.assertIn("{plan-outbound}", outbound)
        self.assertIn("{plan-on-error}", on_error)

    def test_bicep_renders_both_variants(self):
        bicep = (LAB / "main.bicep").read_text(encoding="utf-8")
        for hook in PLAN_HOOKS:
            self.assertGreaterEqual(bicep.count(f"'{hook}'"), 2, hook)
        for placeholder in set(re.findall(r"\{[a-z-]+\}", (LAB / "entra-plan-inbound-fragment.xml").read_text(encoding="utf-8"))):
            self.assertIn(f"'{placeholder}':", bicep, placeholder)
        for placeholder in set(re.findall(r"\{[a-z-]+\}", (LAB / "entra-plan-tier.xml").read_text(encoding="utf-8"))):
            self.assertIn(f"'{placeholder}':", bicep, placeholder)

    def test_rendered_fragment_has_a_branch_per_customer_tier(self):
        tiers = json.loads((LAB / "sandbox-config.json").read_text(encoding="utf-8"))["tiers"]
        xml = render_entra_inbound(tiers)
        self.assertNotRegex(xml, r"\{[a-z-]+\}")
        customer = [t["name"] for t in tiers if not t.get("internal")]
        self.assertEqual(len(re.findall(r'context\.Variables\["entraTier"\] == "', xml)), len(customer))
        self.assertEqual(xml.count("<llm-token-limit"), len(customer))
        self.assertIn('id="group-budget"', xml)
        self.assertIn('id="member-budget"', xml)
        self.assertIn("<value>Gateway.Invoke</value>", xml)
        self.assertNotIn("api-key", xml)
        self.assertEqual(xml.count("<!--"), xml.count("-->"))

    def test_attribution_prefers_entra_payer_but_keeps_keyed_fallback(self):
        xml = (LAB / "attribution-fragment.xml").read_text(encoding="utf-8")
        self.assertIn('GetValueOrDefault<string>("entraPayer", context.Subscription?.Id ?? "anonymous")', xml)
        self.assertIn('GetValueOrDefault<string>("entraTier", context.Product?.Id ?? "none")', xml)


class EntraModeAppTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "inferenceBaseUrl": "https://gw/inference/openai/v1",
            "agents": [{"name": "user-alice", "tier": "gold", "key": "alice-key"}],
            "entraModeResource": {
                "enabled": True,
                "inferenceBaseUrl": "https://gw/entra/inference/openai/v1",
                "gatewayAppId": "gateway-app",
                "personas": [
                    {"name": "entra-ana", "displayName": "Ana", "group": "customer-care", "tier": "gold", "kind": "managed-identity", "clientId": "ana-client", "resourceId": "/id/ana"},
                    {"name": "entra-you", "displayName": "You", "group": "customer-care", "tier": "gold", "kind": "azure-cli", "clientId": "", "resourceId": ""},
                ],
                "groups": [{"name": "customer-care", "displayName": "Customer Care", "tier": "gold", "poolMicroUsd": 30000, "memberMicroUsd": 20000}],
            },
        }
        for name, value in (("CONFIG", self.config), ("HOSTED", True), ("_token_cache", {})):
            patcher = patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def call(self, payload):
        with patch.object(app, "http", return_value=(200, {"x-gw-group": "customer-care"}, json.dumps({"choices": [{"message": {"content": "hi"}}], "usage": {}}), 0.1)) as http, \
             patch.object(app, "managed_identity_token", return_value=("persona-token", 9e9)) as mi, \
             patch.object(app, "az_token", return_value="foundry-token"):
            status, result = app.chat(payload)
        return status, result, http, mi

    def test_persona_call_uses_the_key_less_api_and_its_own_token(self):
        status, result, http, mi = self.call({"persona": "entra-ana", "model": "gpt-4.1", "prompt": "hi", "session": "entra-ana-1"})
        self.assertEqual(status, 200)
        method, url, _, headers = http.call_args.args
        self.assertEqual(url, "https://gw/entra/inference/openai/v1/chat/completions")
        self.assertNotIn("api-key", headers)
        self.assertTrue(headers["Authorization"].endswith("persona-token"))
        self.assertEqual(headers["x-session-id"], "entra-ana-1")
        mi.assert_called_once_with("gateway-app", "ana-client")
        self.assertEqual((result["mode"], result["agent"], result["group"], result["tier"]), ("entra", "entra-ana", "customer-care", "gold"))
        self.assertEqual(result["headers"]["x-gw-group"], "customer-care")

    def test_token_variants_for_the_rejection_checks(self):
        _, _, http, mi = self.call({"persona": "entra-ana", "model": "m", "tokenAs": "none"})
        self.assertNotIn("Authorization", http.call_args.args[3])
        mi.assert_not_called()
        _, result, http, _ = self.call({"persona": "entra-ana", "model": "m", "tokenAs": "foundry"})
        self.assertTrue(http.call_args.args[3]["Authorization"].endswith("foundry-token"))
        self.assertFalse(result["entraToken"] is False)

    def test_keyed_call_is_unchanged(self):
        _, result, http, _ = self.call({"agent": "user-alice", "model": "m"})
        _, url, _, headers = http.call_args.args
        self.assertEqual(url, "https://gw/inference/openai/v1/chat/completions")
        self.assertEqual(headers["api-key"], "alice-key")
        self.assertNotIn("mode", result)

    def test_unknown_or_undeployed_persona_is_rejected(self):
        status, result, http, _ = self.call({"persona": "entra-nobody", "model": "m"})
        self.assertEqual(status, 400)
        http.assert_not_called()
        self.config["entraModeResource"] = {"enabled": False}
        status, _, http, _ = self.call({"persona": "entra-ana", "model": "m"})
        self.assertEqual(status, 400)
        http.assert_not_called()

    def test_persona_availability_depends_on_where_the_ui_runs(self):
        ana, you = self.config["entraModeResource"]["personas"]
        self.assertTrue(app.persona_available(ana))
        self.assertFalse(app.persona_available(you))
        with patch.object(app, "HOSTED", False):
            self.assertFalse(app.persona_available(ana))
            self.assertTrue(app.persona_available(you))
            with self.assertRaises(RuntimeError):
                app.persona_token(ana)

    def test_public_config_hides_identity_details(self):
        public = app.public_config()["entraMode"]
        self.assertTrue(public["enabled"])
        for persona in public["personas"]:
            self.assertNotIn("clientId", persona)
            self.assertNotIn("resourceId", persona)
        self.assertEqual([p["available"] for p in public["personas"]], [True, False])
        self.config.pop("entraModeResource")
        self.assertEqual(app.public_config()["entraMode"], {"enabled": False})

    def test_group_headers_reach_the_ui(self):
        for header in ("x-gw-group", "x-gw-group-budget-usd", "x-gw-member-budget-usd"):
            self.assertIn(header, app.GW_HEADERS)


if __name__ == "__main__":
    unittest.main()
