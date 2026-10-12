import copy
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lifecycle as pricing

FIXTURES = Path(__file__).parent / "fixtures"
EFFECTIVE = "2026-10-12T00:00:00Z"
APIM = "/subscriptions/fixture/resourceGroups/fixture/providers/Microsoft.ApiManagement/service/fixture"


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.deployments = pricing.load(FIXTURES / "deployments.json")
        self.items = pricing.load(FIXTURES / "retail.json")["Items"]
        self.config = pricing.load(pricing.HERE / "overrides.json")

    def card(self, previous=None, effective=EFFECTIVE):
        return pricing.generate(self.deployments, self.items, self.config, previous, effective)

    def test_normalization_exact_decimal_and_units(self):
        for unit, price, expected in [("1K", "0.000025", "0.025000"),
                                      ("1M", "0.025", "0.025"),
                                      ("1,000 Tokens", "0.002", "2.000")]:
            self.assertEqual(pricing.normalize({"unitOfMeasure": unit, "retailPrice": price}), expected)
        with self.assertRaisesRegex(pricing.PricingError, "unit"):
            pricing.normalize({"unitOfMeasure": "1 Hour", "retailPrice": 1})
        for price in (-1, "NaN", "Infinity", "abc"):
            with self.assertRaises(pricing.PricingError):
                pricing.number(price)

    def test_exact_matching_no_wrong_sku_region_currency_or_future(self):
        for change in [
            {"meterName": "gpt 4.1 mini Inp glbl Tokens"},
            {"meterName": "gpt 4.1 Batch Inp glbl Tokens"},
            {"meterName": "gpt 4.1 Inp regnl Tokens"},
            {"armRegionName": "francecentral"},
            {"currencyCode": "EUR"},
            {"type": "Reservation"},
            {"effectiveStartDate": "2030-01-01T00:00:00Z"},
            {"tierMinimumUnits": 1000}
        ]:
            with self.subTest(change=change):
                wrong = {**self.items[0], **change, "retailPrice": 123}
                self.items.append(wrong)
                self.assertEqual(next(iter(self.card()["entries"].values()))["usdPerMillion"]["input"], "2.000")
                self.items.pop()
        self.deployments[0]["sku"] = "Standard"
        with self.assertRaisesRegex(pricing.PricingError, "mapping"):
            self.card()

    def test_latest_effective_meter_and_ambiguity(self):
        self.items.append({**self.items[0], "effectiveStartDate": "2026-01-01T00:00:00Z",
                           "retailPrice": 0.003})
        self.assertEqual(next(iter(self.card()["entries"].values()))["usdPerMillion"]["input"], "3.000")
        self.items.append({**self.items[-1], "meterId": "other"})
        with self.assertRaisesRegex(pricing.PricingError, "Ambiguous meter"):
            self.card()

    def test_override_precedence_and_specificity(self):
        rule = {"match": {"model": "gpt-4.1", "sku": "GlobalStandard"},
                "usdPerMillion": {"input": "4", "cachedInput": "1", "output": "10"},
                "reason": "fixture explicit price"}
        self.config["prices"].append(rule)
        row = next(iter(self.card()["entries"].values()))
        self.assertEqual(row["source"], "override")
        self.assertEqual(row["usdPerMillion"]["input"], "4")
        self.config["prices"].append({**rule, "match": {**rule["match"], "region": "swedencentral"},
                                      "usdPerMillion": {"input": "5", "cachedInput": "1", "output": "11"}})
        self.assertEqual(next(iter(self.card()["entries"].values()))["usdPerMillion"]["input"], "5")
        self.config["prices"].append(copy.deepcopy(self.config["prices"][-1]))
        with self.assertRaisesRegex(pricing.PricingError, "Ambiguous override"):
            self.card()

    def test_missing_and_zero_prices_fail(self):
        self.items.pop()
        with self.assertRaisesRegex(pricing.PricingError, "Missing meter"):
            self.card()
        self.items = pricing.load(FIXTURES / "retail.json")["Items"]
        self.items[0]["retailPrice"] = 0
        with self.assertRaisesRegex(pricing.PricingError, "positive"):
            self.card()
        self.deployments[0]["version"] = "unknown"
        with self.assertRaisesRegex(pricing.PricingError, "mapping"):
            self.card()

    def test_provisioned_requires_allocation_never_retail(self):
        self.deployments[0]["sku"] = "ProvisionedManaged"
        with self.assertRaisesRegex(pricing.PricingError, "allocation"):
            self.card()
        self.config["allocations"] = [{
            "match": {"model": "gpt-4.1", "sku": "ProvisionedManaged"},
            "usdPerMillion": {"input": "2", "cachedInput": "2", "output": "8"},
            "reason": "Internal amortization fixture, not a retail PTU token rate"
        }]
        self.assertEqual(next(iter(self.card()["entries"].values()))["source"], "allocation")

    def test_explicit_no_cached_discount(self):
        self.config["meterMappings"][0]["meters"]["cachedInput"] = "same-as-input"
        self.items.pop(1)
        rates = next(iter(self.card()["entries"].values()))["usdPerMillion"]
        self.assertEqual(rates["input"], rates["cachedInput"])

    def test_version_noop_diff_and_immutable_history(self):
        first = self.card()
        self.items.reverse()
        second = self.card(first, "2026-10-19T00:00:00Z")
        self.assertEqual(first, second)
        self.assertIn("No price", pricing.diff(first, second))
        self.items[0]["retailPrice"] = 0.009
        third = self.card(first, "2026-10-19T00:00:00Z")
        self.assertNotEqual(first["version"], third["version"])
        self.assertIn("9.000", pricing.diff(first, third))
        with tempfile.TemporaryDirectory() as tmp:
            pricing.save_card(first, None, tmp)
            pricing.save_card(third, first, tmp)
            archive = Path(tmp) / "history" / f"{first['version']}.json"
            self.assertEqual(pricing.load(archive), first)
            pricing.save_card(third, third, tmp)
            self.assertEqual(len(list(archive.parent.glob("*.json"))), 1)

    def test_deployment_add_remove_and_source_change(self):
        first = self.card()
        self.deployments.append({**self.deployments[0], "account": "second-account"})
        second = self.card(first)
        self.assertIn("absent", pricing.diff(first, second))
        self.assertIn("removed", pricing.diff(second, first))
        self.items[0]["meterId"] = "changed-evidence"
        self.assertNotEqual(second["version"], self.card(second)["version"])

    def test_named_value_backward_compatible_atomic_cached_version(self):
        card = self.card()
        value = pricing.named_value(card)
        self.assertIn("gpt-4.1=2.000/8.000/0.5000", value)
        self.assertIn(f"__pricingVersion={card['version']}", value)
        prices = value.split(";")[0].split("=")[1].split("/")
        self.assertEqual(prices[:2], ["2.000", "8.000"])

    def test_alias_conflicts_are_not_silently_collapsed(self):
        self.deployments.append({**self.deployments[0], "region": "francecentral"})
        self.items.extend([{**item, "armRegionName": "francecentral"} for item in self.items])
        self.card()
        self.items[-1]["retailPrice"] = 0.01
        with self.assertRaisesRegex(pricing.PricingError, "Conflicting rates"):
            self.card()

    def test_equal_numeric_alias_rates_ignore_decimal_formatting(self):
        self.deployments.append({**self.deployments[0], "region": "francecentral"})
        self.config["prices"].append({
            "match": {"model": "gpt-4.1", "region": "francecentral", "sku": "GlobalStandard"},
            "usdPerMillion": {"input": "2.0", "cachedInput": "0.5", "output": "8.00"}
        })
        value = pricing.named_value(self.card())
        self.assertEqual(value.count("gpt-4.1="), 1)

    def test_named_value_exact_size_boundary(self):
        card = self.card()
        old_key, entry = next(iter(card["entries"].items()))
        size = len(pricing.named_value(card))
        entry["deployment"] += "x" * (pricing.LIMIT - size)
        card["entries"] = {pricing.key(entry): entry}
        self.rehash(card)
        self.assertEqual(len(pricing.named_value(card)), pricing.LIMIT)
        entry["deployment"] += "x"
        card["entries"] = {pricing.key(entry): entry}
        self.rehash(card)
        with self.assertRaisesRegex(pricing.PricingError, "4096"):
            pricing.named_value(card)

    def rehash(self, card):
        card["sourceHash"] = pricing.card_hash(card["entries"])
        card["version"] = f"{card['effectiveFrom'][:10]}-{card['sourceHash'][:16]}"

    def test_tamper_bootstrap_and_invalid_names_fail(self):
        card = self.card()
        next(iter(card["entries"].values()))["usdPerMillion"]["input"] = "0"
        with self.assertRaisesRegex(pricing.PricingError, "hash"):
            pricing.named_value(card)
        with self.assertRaisesRegex(pricing.PricingError, "Empty rate card"):
            pricing.named_value({"schemaVersion": 1, "currency": "USD", "unit": "1M tokens", "entries": {}})
        for name in ("bad;name", "__pricingVersion", ""):
            self.deployments[0]["deployment"] = name
            with self.assertRaises(pricing.PricingError):
                self.card()

    def test_publish_dry_run_no_azure_writes_and_drift_failure(self):
        card = self.card()
        with patch.object(pricing, "az") as cli, tempfile.TemporaryDirectory() as tmp:
            pricing.publish(card, self.deployments, APIM, tmp)
            cli.assert_not_called()
            with self.assertRaisesRegex(pricing.PricingError, "Live deployments differ"):
                pricing.publish(card, [], APIM, tmp, apply=True)
            cli.assert_not_called()

    def test_publish_etag_preservation_and_readback(self):
        card = self.card()
        value = pricing.named_value(card)
        before = {"etag": '"fixture"', "properties": {"displayName": "model-pricing",
                  "value": "legacy", "secret": False, "tags": ["keep"]}}
        after = {"properties": {**before["properties"], "value": value}}
        captured = {}
        def request(method, url, token, body=None, etag=None):
            if method == "PUT":
                captured.update(body)
                return {}
            return after if captured else before
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(pricing, "az", return_value={"accessToken": "fixture"}), \
                patch.object(pricing, "arm_request", side_effect=request) as mock:
            pricing.publish(card, self.deployments, APIM, tmp, apply=True)
            self.assertEqual(captured["properties"]["tags"], ["keep"])
            self.assertEqual(mock.call_args_list[1].args[-1], '"fixture"')
            self.assertEqual(pricing.load(Path(tmp) / "publish-before.json"), before)
            self.assertTrue(pricing.load(Path(tmp) / "publish-receipt.json")["verified"])
            self.assertFalse((Path(tmp) / "publish-body.json").exists())

    def test_publish_refuses_secret_missing_etag_and_failed_readback(self):
        card = self.card()
        for before in [
            {"etag": "one", "properties": {"secret": True}},
            {"properties": {"value": "old", "secret": False}}
        ]:
            with tempfile.TemporaryDirectory() as tmp, \
                    patch.object(pricing, "az", return_value={"accessToken": "fixture"}), \
                    patch.object(pricing, "arm_request", return_value=before) as cli:
                with self.assertRaises(pricing.PricingError):
                    pricing.publish(card, self.deployments, APIM, tmp, apply=True)
                self.assertEqual(cli.call_count, 1)
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(pricing, "az", return_value={"accessToken": "fixture"}), \
                patch.object(pricing, "arm_request", side_effect=[
            {"etag": "one", "properties": {"value": "old", "secret": False}},
            {}, {"properties": {"value": "wrong"}}
        ]):
            with self.assertRaisesRegex(pricing.PricingError, "read-back"):
                pricing.publish(card, self.deployments, APIM, tmp, apply=True)
            self.assertFalse((Path(tmp) / "publish-receipt.json").exists())

    def test_arm_response_etag_header_and_nonsecret_list_value(self):
        from unittest.mock import MagicMock
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"properties":{"value":"old","secret":false}}'
        response.headers = {"ETag": '"header-etag"'}
        with patch.object(pricing, "urlopen", return_value=response):
            result = pricing.arm_request("GET", pricing.ARM + "/fixture", "fixture")
        self.assertEqual(result["etag"], '"header-etag"')
        with patch.object(pricing, "arm_request", side_effect=[
            {"etag": "one", "properties": {"secret": False}}, {"value": "non-secret"}
        ]) as mock:
            result = pricing.get_named_value(pricing.ARM + "/fixture?api-version=2024-05-01", "fixture")
        self.assertEqual(result["properties"]["value"], "non-secret")
        self.assertIn("/listValue?", mock.call_args.args[1])

    def test_live_discovery_region_sku_and_explicit_image_exclusion(self):
        account = self.deployments[0]["account"]
        model = {"name": "gpt-4.1", "version": "2025-04-14"}
        items = [
            {"name": "alias", "properties": {"model": model}, "sku": {"name": "DataZoneStandard"}},
            {"name": "image", "properties": {"model": {"name": "gpt-image-1-mini", "version": "1"}},
             "sku": {"name": "GlobalStandard"}}
        ]
        with patch.object(pricing, "az", side_effect=[
            {"kind": "AIServices", "location": "SwedenCentral"},
            {"value": items[:1], "nextLink": pricing.ARM + "/next"},
            {"value": items[1:]}
        ]):
            rows = pricing.discover([account], self.config["exclude"])
        self.assertEqual(rows[0]["deployment"], "alias")
        self.assertEqual(rows[0]["sku"], "DataZoneStandard")
        self.assertEqual(rows[0]["region"], "swedencentral")
        self.assertEqual(len(rows), 1)

    def test_retail_pagination_accepts_default_port_and_rejects_other_hosts(self):
        from unittest.mock import MagicMock

        def page(body):
            response = MagicMock()
            response.__enter__.return_value = response
            response.read.return_value = body.encode()
            return response

        region = [{"region": "swedencentral"}]
        with patch.object(pricing, "urlopen", side_effect=[
            page('{"Items":[{"n":1}],"NextPageLink":"https://prices.azure.com:443/api/retail/prices?$skip=1000"}'),
            page('{"Items":[{"n":2}],"NextPageLink":null}')
        ]):
            self.assertEqual(len(pricing.fetch(region)), 2)
        for bad in ("https://evil.example/api", "https://prices.azure.com:8443/api",
                    "http://prices.azure.com/api"):
            with patch.object(pricing, "urlopen", return_value=page(
                    '{"Items":[],"NextPageLink":"%s"}' % bad)):
                with self.assertRaisesRegex(pricing.PricingError, "pagination host"):
                    pricing.fetch(region)

    def test_retail_retries_transient_failures_only(self):
        from unittest.mock import MagicMock
        from urllib.error import HTTPError, URLError
        ok = MagicMock()
        ok.__enter__.return_value = ok
        ok.read.return_value = b'{"Items":[]}'
        reset = URLError(ConnectionResetError(10054, "forcibly closed"))
        throttled = HTTPError("u", 429, "throttled", {}, None)
        with patch.object(pricing.time, "sleep") as sleep, \
                patch.object(pricing, "urlopen", side_effect=[reset, throttled, ok]):
            self.assertEqual(pricing.retail_page("https://prices.azure.com/x"), {"Items": []})
        self.assertEqual(sleep.call_count, 2)
        with patch.object(pricing.time, "sleep"), \
                patch.object(pricing, "urlopen", side_effect=[reset, reset, reset]):
            with self.assertRaisesRegex(pricing.PricingError, "unreachable after 3"):
                pricing.retail_page("https://prices.azure.com/x")
        with patch.object(pricing.time, "sleep") as sleep, \
                patch.object(pricing, "urlopen", side_effect=HTTPError("u", 400, "bad", {}, None)):
            with self.assertRaisesRegex(pricing.PricingError, "HTTP 400"):
                pricing.retail_page("https://prices.azure.com/x")
        sleep.assert_not_called()

    def test_cli_offline_generate_check_noop_and_fixture_publish_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = [sys.executable, str(pricing.HERE / "lifecycle.py")]
            flags = ["--directory", tmp, "--deployments", str(FIXTURES / "deployments.json")]
            generate = command + ["generate", *flags, "--retail", str(FIXTURES / "retail.json"),
                                  "--effective-from", EFFECTIVE]
            subprocess.run(generate, check=True, capture_output=True, text=True)
            path = Path(tmp) / "rate-card.json"
            before = path.read_bytes()
            subprocess.run(command + ["check", *flags], check=True, capture_output=True, text=True)
            generate[-1] = "2026-10-19T00:00:00Z"
            subprocess.run(generate, check=True, capture_output=True, text=True)
            self.assertEqual(path.read_bytes(), before)
            result = subprocess.run(command + ["publish", *flags, "--apim-id", APIM, "--apply"],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("live account discovery", result.stderr)


if __name__ == "__main__":
    unittest.main()
