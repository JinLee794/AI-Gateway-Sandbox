import importlib.util
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import re
import threading
import unittest
import urllib.request
from unittest.mock import patch


LAB = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sandbox_app_copilot", LAB / "src" / "app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

CONFIG = {
    "apimServiceId": "/subscriptions/test/resourceGroups/test/providers/Microsoft.ApiManagement/service/apim-test",
    "inferenceBaseUrl": "https://apim-test.azure-api.net/inference/openai/v1",
    "logAnalyticsWorkspaceId": "test-workspace",
    "logAnalyticsResourceId": "/subscriptions/test/resourceGroups/test/providers/Microsoft.OperationalInsights/workspaces/law",
    "tiers": [{"name": "silver", "displayName": "Silver", "budgetUsd": 0.01}, {"name": "agent-platform", "internal": True, "budgetUsd": 5}],
    "agents": [{"name": "user-ben", "displayName": "Ben Okafor", "tier": "silver", "kind": "user", "key": "k" * 32}],
    "models": [{"name": "gpt-4.1-mini", "inputPrice": 0.4, "outputPrice": 1.6}],
    "tools": [{"name": "search-products", "priceUsd": 0.0005}],
}
UI = {"view": "Products & plans", "session": "ui-1014-1130-ab12", "caller": "Ben Okafor (user-ben, silver plan)",
      "calls": [{"n": 3, "agent": "user-ben", "surface": "model", "status": 403, "target": "gpt-4.1",
                 "blockedBy": "quota-by-key (session-budget)", "session": "ui-1014-1130-ab12",
                 "requestId": "6f0e5a9c-1b2d-4e3f-8a7b-0c1d2e3f4a5b"}]}


def tool_call(call_id, name, args):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def llm_reply(message, cost="0.000400"):
    return 200, {"x-gw-tier": "agent-platform", "x-gw-cost-usd": cost, "x-gw-model-served": "gpt-4.1-mini"}, \
        {"choices": [{"message": message}], "usage": {"prompt_tokens": 900, "completion_tokens": 60, "total_tokens": 960}}, 0.5


class CopilotTests(unittest.TestCase):
    def setUp(self):
        for target, value in (("CONFIG", CONFIG), ("_copilot_calls", [])):
            p = patch.object(app, target, value)
            p.start()
            self.addCleanup(p.stop)
        self.ui = app.copilot_ui(UI)

    def run_copilot(self, replies, question="Why was call #3 blocked?"):
        events = []
        with patch.object(app, "copilot_llm", side_effect=replies) as llm:
            app.copilot({"messages": [{"role": "user", "content": question}], "ui": UI}, events.append)
        return events, llm

    def test_ui_actions_are_validated(self):
        action, error = app.copilot_ui_action("ui_navigate", {"view": "policies.pipeline"}, self.ui)
        self.assertEqual(action, {"type": "navigate", "view": "policies.pipeline"})
        self.assertIsNone(app.copilot_ui_action("ui_navigate", {"view": "settings"}, self.ui)[0])
        self.assertEqual(app.copilot_ui_action("ui_highlight", {"target": "plan:silver", "caption": "x" * 300}, self.ui)[0]["caption"], "x" * 140)
        for bad in ("#plansMatrix", "div > .card", "plan:<script>", "", "highest-cost-session", "row:3"):
            self.assertIsNone(app.copilot_ui_action("ui_highlight", {"target": bad}, self.ui)[0], bad)
        self.assertEqual(app.copilot_ui_action("ui_highlight", {"target": "session:alice-loop-10132123-a0ue"}, self.ui)[0]["target"], "session:alice-loop-10132123-a0ue")
        self.assertEqual(app.copilot_ui_action("ui_open_request", {"call_number": 3}, self.ui)[0], {"type": "open_request", "call": 3})
        self.assertIsNone(app.copilot_ui_action("ui_open_request", {"call_number": 9}, self.ui)[0])
        self.assertIsNone(app.copilot_ui_action("ui_open_request", {"call_number": True}, self.ui)[0])
        self.assertIsNone(app.copilot_ui_action("ui_play_flow", {"example": "explode"}, self.ui)[0])

    def test_policy_flow_and_prepared_request_are_sanitised(self):
        flow, _ = app.copilot_ui_action("ui_show_policy_flow", {"title": "Silver", "steps": [
            {"section": "inbound", "policy": "llm-token-limit", "does": "caps tokens", "outcome": "429"},
            {"section": "sideways", "policy": "quota-by-key", "does": "d" * 500}, {"policy": ""}, "junk"] * 5}, self.ui)
        self.assertEqual(len(flow["steps"]), 10)
        self.assertEqual(flow["steps"][1]["section"], "inbound")
        self.assertEqual(len(flow["steps"][1]["does"]), 160)
        prepared, _ = app.copilot_ui_action("ui_prepare_request", {"caller": "user-ben", "surface": "model", "model": "gpt-9", "prompt": "hi"}, self.ui)
        self.assertEqual((prepared["caller"], prepared["model"], prepared["prompt"]), ("user-ben", None, "hi"))
        self.assertIsNone(app.copilot_ui_action("ui_prepare_request", {"caller": "mallory", "surface": "model"}, self.ui)[0])

    def test_kql_is_read_only_and_capped(self):
        self.assertIn("error", app.copilot_kql(".drop table AppTraces"))
        self.assertIn("error", app.copilot_kql("AppTraces | take 1; .set-or-append x <| AppTraces"))
        self.assertIn("error", app.copilot_kql(""))
        with patch.object(app, "law_query", return_value=[{"Calls": 2, "Text": "t" * 500}]) as law:
            result = app.copilot_kql("AppTraces | summarize Calls = count();", "P30D")
        query, timespan = law.call_args[0]
        self.assertTrue(query.endswith("\n| take 50"))
        self.assertNotIn(";", query)
        self.assertEqual(timespan, "P1D")
        self.assertEqual(result["rowCount"], 1)
        self.assertEqual(len(result["rows"][0]["Text"]), 301)
        self.assertTrue(result["portalLink"].startswith("https://portal.azure.com/"))

    def test_policy_outline_and_comment_stripping(self):
        xml = ("<policies><inbound><base /><!-- why --><include-fragment fragment-id=\"entra-identity\" />"
               "<set-variable name=\"x\" value=\"@(new List<string>())\" /><llm-token-limit tokens-per-minute=\"1\" /></inbound>"
               "<outbound><!-- emit-metric is not allowed in <backend>; default=<class>;@<user>=1 -->"
               "<set-header name=\"x-gw-tier\"><value>gold</value></set-header></outbound></policies>")
        expected = {"inbound": ["include-fragment", "set-variable", "llm-token-limit"], "outbound": ["set-header"]}
        with patch.object(app, "http", return_value=(200, {}, json.dumps({"properties": {"value": xml}}), 0.1)), \
             patch.object(app, "az_token", return_value="token"):
            result = app.copilot_policy("product:silver", "outbound")
            self.assertEqual(result["outline"], expected)
            self.assertEqual(app.copilot_policy("product:silver", include_comments=True)["outline"], expected)
            self.assertNotIn("inbound", result["xml"])
            self.assertIn("x-gw-tier", result["xml"])
            self.assertNotIn("why", app.copilot_policy("product:silver")["xml"])
            app.copilot_policy("Silver")
            self.assertIn("/products/silver/policies/policy", app.http.call_args[0][1])
            app.copilot_policy("entra-identity")
            self.assertIn("/policyFragments/entra-identity", app.http.call_args[0][1])
        self.assertIn("error", app.copilot_policy("product:../../secrets"))

    def test_closing_narration_of_ui_actions_is_dropped(self):
        body = "Silver allows **3 models**.\n\n- TPM: 12,000"
        for tail in ("I highlighted the Silver plan for you.", "I’ve also shown the policy flow.", "I highlight the Silver plan details for you."):
            self.assertEqual(app.copilot_tidy(f"{body}\n\n{tail}"), body, tail)
        self.assertEqual(app.copilot_tidy("I opened the evidence of #2."), "I opened the evidence of #2.")
        self.assertEqual(app.copilot_tidy("I highlighted nothing.\n" + body), "I highlighted nothing.\n" + body)

    def test_chargeback_ranks_sessions_and_la_errors_are_explained(self):
        rows = [{"User": "user-alice", "Session": "alice-loop-10132123-a0ue", "CostUSD": 0.01}, {"User": "user-ben", "Session": "raj-10141250-yn81", "CostUSD": 0.035},
                {"User": "sandbox-copilot", "Session": "copilot-ui-1", "CostUSD": 0.002}, {"User": "user-dev", "Session": "dev-10141100-zz11", "CostUSD": 0.001}]
        data = {"teams": {"rows": [{"Team": "Care", "CostUSD": 0.048}]}, "sessions": {"rows": rows, "portalLink": "https://portal.azure.com/x"}}
        ui = app.copilot_ui({"runs": [{"n": 4, "title": "Chargeback by user & session", "sessions": ["dev-10141100-zz11"]}]})
        with patch.object(app, "chargeback", return_value=(200, data)):
            result = app.copilot_chargeback(ui, "30d")
        self.assertEqual(result["window"], "1d")
        top = result["topSessions"]
        self.assertEqual([r["Session"] for r in top], ["raj-10141250-yn81", "alice-loop-10132123-a0ue", "copilot-ui-1", "dev-10141100-zz11"])
        self.assertEqual([r["CreatedBy"] for r in top], ['scenario "One app, many users: per-user $ budgets"', 'scenario "Runaway session: per-session $ cap"',
                                                         "the Sandbox Copilot's own model calls", "run #4 on this page: Chargeback by user & session"])
        self.assertEqual(app.copilot_session_origin("mystery-1", ui), "unknown")
        self.assertAlmostEqual(result["totalUsd"], 0.048)
        with patch.object(app, "chargeback", return_value=(200, {"sessions": {"rows": {"error": "boom"}}, "teams": {"rows": []}})):
            self.assertEqual(app.copilot_chargeback(ui)["error"], "boom")
        nested = json.dumps({"error": {"message": "The request had some invalid properties", "code": "BadArgumentError",
                                       "innererror": {"message": "A recognition error occurred.", "innererror": {"message": "Query could not be parsed at 'x' on line [1,5]"}}}})
        self.assertEqual(app._la_error(nested), "The request had some invalid properties → A recognition error occurred. → Query could not be parsed at 'x' on line [1,5]")
        self.assertEqual(app._la_error("plain failure"), "plain failure")

    def test_screen_context_lists_the_calls(self):
        screen = app.copilot_screen(self.ui)
        self.assertIn("ui-1014-1130-ab12", screen)
        self.assertIn("#3", screen)
        self.assertIn("blocked-by quota-by-key (session-budget)", screen)
        with_runs = app.copilot_ui({**UI, "runs": [{"n": 7, "title": "One app, many users", "status": "finished", "calls": 16,
                                                    "sessions": ["alice-loop-10132123-a0ue", "ben-loop-1"]}]})
        self.assertIn("#7 One app, many users (finished, 16 calls; sessions alice-loop-10132123-a0ue, ben-loop-1)", app.copilot_screen(with_runs))
        self.assertNotIn("<script>", app.copilot_screen(app.copilot_ui({"calls": "not a list", "session": "bad session!"})))
        many = app.copilot_ui({"calls": [{"n": 1, "ref": True, "agent": "old-call"}] + [{"n": n, "agent": f"a{n}"} for n in range(140, 181)]})
        screen = app.copilot_screen(many)
        self.assertIn("old-call", screen)
        self.assertNotIn("a150 ", screen)
        self.assertIn("a180", screen)
        self.assertIn("older than the calls the page shared", app.copilot_ui_action("ui_open_request", {"call_number": 5}, many)[1])
        self.assertIn("no call #999", app.copilot_tool("get_call_details", {"call_number": 999}, many)[0]["error"])

    def test_agent_loop_streams_steps_actions_and_answer(self):
        replies = [
            llm_reply({"content": None, "tool_calls": [tool_call("c1", "get_call_details", {"call_number": 3}),
                                                       tool_call("c2", "ui_open_request", {"call_number": 3}),
                                                       tool_call("c3", "ui_highlight", {"target": "#evil"})]}),
            llm_reply({"content": "Call #3 hit the **session cap**."}, cost="0.000200"),
        ]
        events, llm = self.run_copilot(replies)
        types = [e["type"] for e in events]
        self.assertEqual(types, ["llm", "llm", "step", "step_done", "action", "llm", "llm", "answer"])
        self.assertEqual(events[4]["action"], {"type": "open_request", "call": 3})
        answer = events[-1]
        self.assertEqual(answer["usage"]["gatewayCalls"], 2)
        self.assertAlmostEqual(answer["usage"]["costUsd"], 0.0006)
        messages, session = llm.call_args_list[1][0][0], llm.call_args_list[1][0][1]
        self.assertEqual(session, "copilot-ui-1014-1130-ab12")
        tool_results = {m["tool_call_id"]: json.loads(m["content"]) for m in messages if m["role"] == "tool"}
        self.assertEqual(tool_results["c1"]["blockedBy"], "quota-by-key (session-budget)")
        self.assertTrue(tool_results["c2"]["shown"])
        self.assertIn("error", tool_results["c3"])
        self.assertIn("The user's page right now", messages[0]["content"])

    def test_ui_only_turn_forces_the_answer_and_actions_are_not_repeated(self):
        highlight = {"target": "plan:silver", "caption": "Silver"}
        replies = [
            llm_reply({"content": None, "tool_calls": [tool_call("c1", "get_gateway_config", {"section": "overview"})]}),
            llm_reply({"content": None, "tool_calls": [tool_call("c2", "ui_highlight", highlight), tool_call("c3", "ui_highlight", highlight)]}),
            llm_reply({"content": "Silver allows three models."}),
        ]
        with patch.object(app, "copilot_config", return_value={"plans": ["silver"]}):
            events, llm = self.run_copilot(replies, "What does Silver allow?")
        self.assertEqual([e["action"] for e in events if e["type"] == "action"], [{"type": "highlight", "target": "plan:silver", "caption": "Silver"}])
        self.assertEqual([c[0][3] for c in llm.call_args_list], [False, False, True])
        self.assertEqual(events[-1]["text"], "Silver allows three models.")

        flow = {"title": "Silver", "steps": [{"section": "inbound", "policy": "quota-by-key", "does": "caps $"}]}
        replies = [llm_reply({"content": None, "tool_calls": [tool_call("c1", "ui_show_policy_flow", flow)]}),
                   llm_reply({"content": "It caps spend."})]
        events, llm = self.run_copilot(replies, "What does quota-by-key do?")
        self.assertEqual([c[0][3] for c in llm.call_args_list], [False, True])
        self.assertEqual([e["action"]["type"] for e in events if e["type"] == "action"], ["policy_flow"])

        four = [tool_call(f"h{i}", "ui_highlight", {"target": t}) for i, t in enumerate(["kpis", "signals", "budgets", "plans-matrix"])]
        events, llm = self.run_copilot([llm_reply({"content": None, "tool_calls": four}), llm_reply({"content": "Done."})])
        self.assertEqual([e["action"]["target"] for e in events if e["type"] == "action"], ["kpis", "signals", "budgets"])
        self.assertIn("error", json.loads(llm.call_args_list[1][0][0][-1]["content"]))

        retry = [llm_reply({"content": None, "tool_calls": [tool_call("x1", "ui_highlight", {"target": "highest-cost-session"})]}),
                 llm_reply({"content": None, "tool_calls": [tool_call("x2", "ui_highlight", {"target": "session:alice-loop-10132123-a0ue"})]}),
                 llm_reply({"content": "Alice's loop."})]
        events, llm = self.run_copilot(retry)
        self.assertEqual([c[0][3] for c in llm.call_args_list], [False, False, True])
        self.assertEqual([e["action"]["target"] for e in events if e["type"] == "action"], ["session:alice-loop-10132123-a0ue"])

    def test_highlight_targets_match_the_client(self):
        js = (LAB / "src" / "static" / "copilot.js").read_text(encoding="utf-8")
        registry = js[js.index("const T = {"):js.index("const STAGES")]
        names = set(re.findall(r'(?:^|[\s{,])"?([a-z][a-z-]+)"?: \[', registry))
        kinds = set(re.findall(r'case "([a-z]+)":', js[js.index("function resolve("):js.index("const shown =")]))
        self.assertEqual(names, app.COPILOT_TARGETS)
        self.assertEqual(kinds, app.COPILOT_TARGET_KINDS)
        for name in app.COPILOT_TARGETS | {k + ":" for k in app.COPILOT_TARGET_KINDS}:
            self.assertIn(name, app.COPILOT_PROMPT, name)

    def test_gateway_block_is_reported_and_last_step_forces_an_answer(self):
        blocked = (429, {"x-gw-blocked-by": "llm-token-limit", "retry-after": "12"}, {"error": {"message": "Token limit is exceeded."}}, 0.1)
        events, _ = self.run_copilot([blocked])
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("429", events[-1]["error"])
        self.assertEqual(events[-1]["headers"]["x-gw-blocked-by"], "llm-token-limit")

        looping = [llm_reply({"content": None, "tool_calls": [tool_call(f"c{i}", "get_call_details", {"call_number": 3})]}) for i in range(5)]
        events, llm = self.run_copilot(looping + [llm_reply({"content": "Done."})])
        self.assertEqual(events[-1]["text"], "Done.")
        self.assertEqual([c[0][3] for c in llm.call_args_list], [False] * 5 + [True])

    def test_rejects_bad_history_and_rate_limits(self):
        events = []
        app.copilot({"messages": [{"role": "assistant", "content": "hi"}]}, events.append)
        self.assertEqual(events[0]["type"], "error")
        with patch.object(app, "COPILOT_PER_MINUTE", 1), patch.object(app, "copilot_llm", return_value=llm_reply({"content": "ok"})):
            first, second = [], []
            app.copilot({"messages": [{"role": "user", "content": "a"}]}, first.append)
            app.copilot({"messages": [{"role": "user", "content": "b"}]}, second.append)
        self.assertEqual(first[-1]["type"], "answer")
        self.assertIn("at most 1 questions per minute", second[0]["error"])

    def test_key_is_created_on_first_use_and_scrubbed(self):
        calls = []

        def arm(method, url, body=None, headers=None, timeout=120):
            calls.append((method, url.split("?")[0].rsplit("/", 1)[-1]))
            if method == "PUT":
                self.assertEqual(body["properties"]["scope"], "/products/agent-platform")
                return 201, {}, "{}", 0.1
            created = any(m == "PUT" for m, _ in calls)
            return (200, {}, json.dumps({"primaryKey": "c" * 32}), 0.1) if created else (404, {}, "not found", 0.1)

        with patch.object(app, "http", side_effect=arm), patch.object(app, "az_token", return_value="token"), \
             patch.dict(app._token_cache, clear=True):
            self.assertEqual(app.copilot_key(), "c" * 32)
            self.assertEqual(app.copilot_key(), "c" * 32)  # cached
            self.assertNotIn("c" * 32, app.scrub("key " + "c" * 32))
        self.assertEqual(calls, [("POST", "listSecrets"), ("PUT", "sandbox-copilot"), ("POST", "listSecrets")])

    def test_stream_endpoint_returns_json_lines(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        body = json.dumps({"messages": [{"role": "user", "content": "What does silver allow?"}], "ui": UI}).encode()
        request = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}/api/copilot", data=body,
                                         headers={"Content-Type": "application/json"}, method="POST")
        with patch.object(app, "copilot_llm", return_value=llm_reply({"content": "Silver allows **gpt-4.1-mini**."})):
            with urllib.request.urlopen(request, timeout=10) as response:
                self.assertTrue(response.headers["Content-Type"].startswith("application/x-ndjson"))
                events = [json.loads(line) for line in response.read().decode().splitlines() if line.strip()]
        self.assertEqual(events[-1]["type"], "answer")
        self.assertIn("gpt-4.1-mini", events[-1]["text"])
        base = f"http://127.0.0.1:{server.server_address[1]}"
        for path in ("/", "/copilot.js", "/copilot.css"):
            with urllib.request.urlopen(base + path, timeout=10) as response:
                self.assertEqual(response.headers["Cache-Control"], "no-cache", path)


if __name__ == "__main__":
    unittest.main()
