import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


LAB = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sandbox_codemode", LAB / "src" / "codemode.py")
codemode = importlib.util.module_from_spec(spec)
spec.loader.exec_module(codemode)

TOOLS = [
    {"name": "check-inventory", "description": "Stock for one SKU",
     "inputSchema": {"type": "object", "properties": {"sku": {"type": "string", "description": "The SKU"}}, "required": ["sku"]}},
    {"name": "get-supplier-quote", "description": "Quotes",
     "inputSchema": {"type": "object", "properties": {"sku": {"type": "string"}, "quantity": {"type": "integer"}}, "required": ["sku"]}},
]


class CodeModeTests(unittest.TestCase):
    def test_check_code_blocks_escapes(self):
        for code in ["import os", "from os import path", "open('x')", "().__class__", "x = __builtins__",
                     "eval('1')", "getattr(1, 'real')", "'{0.__class__}'.format(1)", "def f(_x): pass"]:
            self.assertIsNotNone(codemode.check_code(code), code)
        self.assertIsNone(codemode.check_code("total = sum(w['onHand'] for w in check_inventory('A')['warehouses'])\nprint(total)"))
        self.assertIn("SyntaxError", codemode.check_code("print(("))
        self.assertIn("longer", codemode.check_code("x = 1\n" * codemode.MAX_CODE_CHARS))

    def test_stubs_are_typed_and_carry_result_shapes(self):
        text = codemode.stubs(TOOLS)
        self.assertIn("def check_inventory(sku: str) -> dict:", text)
        self.assertIn("def get_supplier_quote(sku: str, quantity: int = None) -> dict:", text)
        self.assertIn("Returns: " + codemode.RESULT_SHAPES["check-inventory"], text)
        self.assertIn('Returns: {"type":"object"}', codemode.stubs([{**TOOLS[0], "outputSchema": {"type": "object"}}]))
        self.assertEqual(codemode.extract_code("Here:\n```python\nprint(1)\n```\n"), "print(1)")

    def test_execute_proxies_each_call_and_returns_only_printed_output(self):
        run = codemode.Run({}, None, {}, "code")
        calls = []

        def fake_tool(name, arguments):
            calls.append((name, arguments))
            return {"sku": arguments["sku"], "warehouses": [{"warehouse": "W1", "onHand": 40}, {"warehouse": "W2", "onHand": 2}]}

        functions = {"check_inventory": ["check-inventory", ["sku"]]}
        code = "for s in ['A', 'B']:\n    print(s, sum(w['onHand'] for w in check_inventory(s)['warehouses']))"
        with patch.object(run, "tool", side_effect=fake_tool):
            result = run.execute(code, functions)
        self.assertIsNone(result["error"])
        self.assertEqual(result["output"], "A 42\nB 42\n")
        self.assertEqual(calls, [("check-inventory", {"sku": "A"}), ("check-inventory", {"sku": "B"})])

    def test_execute_has_no_open_or_import_at_runtime(self):
        run = codemode.Run({}, None, {}, "code")
        self.assertIn("NameError", run.execute("open('x')", {})["error"])
        with patch.object(run, "tool", side_effect=RuntimeError("tool-call cap reached")):
            self.assertIn("tool-call cap reached", run.execute("check_inventory('A')", {"check_inventory": ["check-inventory", ["sku"]]})["error"])

    def _toolbox_run(self, config):
        deps = {"config": config, "demo_limit": lambda: None, "token": lambda audience: "t", "error_message": lambda s, d, t: f"HTTP {s}"}
        return codemode.Run(deps, {"name": "a"}, {}, "search")

    def test_toolbox_relays_found_tools_and_prices_them_from_the_catalog(self):
        run = self._toolbox_run({"foundryProjectEndpoint": "https://f/api/projects/p/", "tools": [{"name": "check-inventory", "priceUsd": 0.002}]})
        self.assertEqual(run.toolbox_url(), "https://f/api/projects/p/toolboxes/agent-ready-apis/mcp?api-version=v1")
        sent = []

        def fake_rpc(method, params=None, notify=False):
            sent.append(params)
            return 200, {}, {"result": {"content": [{"type": "text", "text": '{"ok": 1}'}]}}, "", 0.01

        with patch.object(run, "_tb_rpc", side_effect=fake_rpc):
            self.assertEqual(run.toolbox_tool("tool_search", {"query": "stock"}), {"ok": 1})
            run.toolbox_tool("commerce___check-inventory", {"sku": "A"})  # called by name instead of through call_tool
        self.assertEqual(sent[1], {"name": "call_tool", "arguments": {"name": "commerce___check-inventory", "arguments": {"sku": "A"}}})
        search, call = run.tool_calls
        self.assertEqual((search["via"], search["query"], search["costUsd"]), ("search", "stock", 0))
        self.assertEqual((call["via"], call["name"], call["args"], call["costUsd"]), ("toolbox", "check-inventory", {"sku": "A"}, 0.002))

    def test_every_toolbox_call_carries_the_runs_trace_id(self):
        run = self._toolbox_run({"foundryProjectEndpoint": "https://f/api/projects/p"})
        sent = []

        def fake_http(method, url, body, headers, timeout=None):
            sent.append(headers)
            return 200, {"x-request-id": run.trace_id}, '{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{}"}]}}', 0.01

        run.deps.update(http=fake_http, parse_rpc=lambda text: __import__("json").loads(text))
        run.toolbox_tool("call_tool", {"name": "commerce___check-inventory", "arguments": {"sku": "A"}})
        run.toolbox_tool("tool_search", {"query": "stock"})
        self.assertRegex(run.trace_id, r"^[0-9a-f]{32}$")
        parents = [h["traceparent"] for h in sent]
        self.assertTrue(all(p.startswith(f"00-{run.trace_id}-") and p.endswith("-01") for p in parents), parents)
        self.assertNotEqual(parents[0], parents[1])  # a new span per call, one trace per run
        self.assertNotIn("Mcp-Session-Id", sent[0])
        relayed, search = run.tool_calls
        self.assertEqual(relayed["traceId"], run.trace_id)
        self.assertNotIn("traceId", search)

    def test_gateway_tool_calls_record_the_request_id_and_send_the_trace(self):
        sent = []

        def fake_http(method, url, body, headers, timeout=None):
            sent.append(headers)
            return 200, {"X-Gw-Request-Id": "11111111-2222-3333-4444-555555555555", "x-gw-cost-usd": "0.002"}, \
                '{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{}"}]}}', 0.01

        deps = {"config": {"mcpUrl": "https://gw/mcp"}, "demo_limit": lambda: None, "http": fake_http, "error_message": lambda s, d, t: "",
                "auth": lambda agent, payload, surface: {"api-key": "k", "Apim-Debug-Authorization": "x"},
                "parse_rpc": lambda text: __import__("json").loads(text)}
        run = codemode.Run(deps, {"name": "a"}, {}, "tools")
        run.tool("check-inventory", {"sku": "A"})
        self.assertEqual(run.tool_calls[0]["requestId"], "11111111-2222-3333-4444-555555555555")
        self.assertTrue(sent[0]["traceparent"].startswith(f"00-{run.trace_id}-"))
        self.assertNotIn("Apim-Debug-Authorization", sent[0])

    def test_search_mode_needs_the_toolbox_setup(self):
        with self.assertRaisesRegex(RuntimeError, "setup_toolbox.py"):
            self._toolbox_run({}).toolbox_url()
        status, body = codemode.run({"agent": "a", "mode": "kong"}, {"agent_by_name": lambda n: {"name": n}})
        self.assertEqual((status, body["error"]), (400, "mode must be 'tools', 'search', 'code', 'mesh' or 'probe'"))

    def test_mesh_catalog_names_every_operation_domain_resource_verb(self):
        entries, ops = codemode.mesh_catalog(TOOLS, 300)
        self.assertEqual(len(entries), 300)
        self.assertEqual({o: n for o, n in ops.items() if n}, {"commerce.inventory.check": "check-inventory", "commerce.quotes.get": "get-supplier-quote"})
        self.assertTrue(all(len(e["id"].split(".")) == 3 for e in entries))
        self.assertEqual(entries[0]["params"], {"sku": "str"})
        self.assertEqual(codemode.mesh_catalog(TOOLS, 300)[0], entries)
        index = codemode.mesh_index(entries)
        self.assertIn("commerce (inventory, quotes)", index)
        self.assertLess(len(index), len(__import__("json").dumps(entries)) / 20)

    def test_mesh_searches_in_the_sandbox_and_executes_through_the_gateway(self):
        import json
        mcp, replies = [], iter([
            {"tool_calls": [{"id": "1", "function": {"name": "search", "arguments": json.dumps(
                {"code": "print([e['id'] for e in catalog if e['resource'] == 'inventory'])"})}}]},
            {"tool_calls": [{"id": "2", "function": {"name": "execute", "arguments": json.dumps(
                {"code": "r = call('commerce.inventory.check', sku='A')\nprint(r['sku'])\nprint(call('finance.invoices.get'))\nprint(call('nope.x.y'))"})}}]},
            {"content": "done"}])

        def fake_http(method, url, body, headers, timeout=None):
            if url.endswith("/mcp"):
                mcp.append(body["method"])
                result = {"tools": TOOLS} if body["method"] == "tools/list" else {"content": [{"type": "text", "text": '{"sku": "A"}'}]}
                return 200, {"mcp-session-id": "s", "x-gw-cost-usd": "0.002", "x-other": "1"}, json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}), 0.0
            return 200, {"x-gw-model-served": "m"}, json.dumps({"choices": [{"message": next(replies)}], "usage": {"prompt_tokens": 10, "completion_tokens": 1}}), 0.0

        deps = {"config": {"mcpUrl": "https://gw/mcp", "inferenceBaseUrl": "https://gw/inference"}, "demo_limit": lambda: None, "http": fake_http,
                "error_message": lambda s, d, t: "", "auth": lambda agent, payload, surface: {}, "parse_rpc": json.loads,
                "agent_by_name": lambda n: {"name": n}}
        with patch.object(codemode, "decoys", side_effect=lambda n, scale=1: [{"name": f"get-invoice-v{i + 2}" if i else "get-invoice",
                                                                               "description": "x", "inputSchema": {}} for i in range(n)]):
            status, body = codemode.run({"agent": "a", "mode": "mesh", "catalog": 20, "task": "t"}, deps)
        self.assertEqual((status, body["answer"], body["toolsAdvertised"]), (200, "done", 2))
        self.assertEqual(body["mesh"]["operations"], 20)
        self.assertEqual([s["kind"] for s in body["scripts"]], ["search", "execute"])
        self.assertIn("commerce.inventory.check", body["scripts"][0]["output"])
        calls = [(c["name"], c.get("via"), c["status"]) for c in body["toolCalls"]]
        self.assertEqual(calls, [("search", "search", 200), ("check-inventory", None, 200), ("finance.invoices.get", "catalog", 501), ("nope.x.y", "catalog", 404)])
        self.assertEqual(mcp.count("tools/call"), 1)
        self.assertEqual(body["toolCalls"][1]["headers"], {"x-gw-cost-usd": "0.002"})
        self.assertEqual([c["method"] for c in body["mcpCalls"]], ["initialize", "notifications/initialized", "tools/list"])
        self.assertEqual(body["modelCalls"][0]["headers"], {"x-gw-model-served": "m"})
        self.assertEqual(codemode.run({"agent": "a", "mode": "mesh", "catalog": 5}, deps)[0], 400)

    def test_codemode_logs_validates_ids_and_queries_by_trace_and_correlation_id(self):
        spec_app = importlib.util.spec_from_file_location("sandbox_app_cm", LAB / "src" / "app.py")
        app = importlib.util.module_from_spec(spec_app)
        spec_app.loader.exec_module(app)
        self.assertEqual(app.codemode_logs({"traceIds": ["nope\") | take 1"], "requestIds": ["x"]})[0], 400)
        queries = []
        with patch.object(app, "law_query", side_effect=lambda q, *a: queries.append(q) or []), patch.object(app, "portal_logs_link", return_value=""):
            status, body = app.codemode_logs({"traceIds": ["a" * 32, "bad"], "requestIds": ["11111111-2222-3333-4444-555555555555"]})
        self.assertEqual((status, body["traceIds"]), (200, ["a" * 32]))
        self.assertEqual(set(body["tables"]), {"chargeback", "requests", "gateway", "mcp"})
        self.assertTrue(all(f'dynamic(["{"a" * 32}"])' in q and "11111111-2222-3333-4444-555555555555" in q for q in queries))

    def test_trace_timeline_validates_ids_and_queries_requests_and_dependencies(self):
        spec_app = importlib.util.spec_from_file_location("sandbox_app_tl", LAB / "src" / "app.py")
        app = importlib.util.module_from_spec(spec_app)
        spec_app.loader.exec_module(app)
        self.assertEqual(app.trace_timeline({"traceIds": ["x\") | take 1"]})[0], 400)
        with patch.object(app, "law_query", return_value=[]) as law, patch.object(app, "portal_logs_link", return_value=""):
            status, body = app.trace_timeline({"traceIds": ["b" * 32, "bad"]})
        query = law.call_args[0][0]
        self.assertEqual((status, body["traceIds"], body["rows"]), (200, ["b" * 32], []))
        for part in (f'dynamic(["{"b" * 32}"])', "AppRequests", "AppDependencies", "ParentId", "DurationMs"):
            self.assertIn(part, query)

    def test_calls_record_when_they_started_in_the_run(self):
        def fake_http(method, url, body, headers, timeout=None):
            return 200, {}, '{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{}"}]}}', 0.0
        deps = {"config": {"mcpUrl": "https://gw/mcp"}, "demo_limit": lambda: None, "http": fake_http, "error_message": lambda s, d, t: "",
                "auth": lambda agent, payload, surface: {}, "parse_rpc": lambda text: __import__("json").loads(text)}
        run = codemode.Run(deps, {"name": "a"}, {}, "tools")
        run.tool("check-inventory", {"sku": "A"})
        self.assertGreaterEqual(run.tool_calls[0]["startMs"], 0)
        self.assertIn("startedAt", run.summary(run.started, {}))

    def test_decoys_are_unique_deterministic_and_grow_with_scale(self):
        tools = codemode.decoys(1000)
        self.assertEqual(len({t["name"] for t in tools}), 1000)
        self.assertEqual(tools, codemode.decoys(1000))
        self.assertFalse({t["name"] for t in tools} & {"check-inventory", "get-supplier-quote", "search-products"})
        one, four = codemode.catalog_cost(codemode.decoys(50)), codemode.catalog_cost(codemode.decoys(50, 4))
        self.assertGreater(four["defTokens"], 3 * one["defTokens"])
        self.assertGreater(four["stubTokens"], 3 * one["stubTokens"])
        self.assertEqual(codemode.catalog_cost(TOOLS)["tools"], 2)

    def test_probe_measures_first_prompts_with_one_completion_token(self):
        import json
        chats = []

        def fake_http(method, url, body, headers, timeout=None):
            if url.endswith("/mcp"):
                result = {"tools": TOOLS} if body["method"] == "tools/list" else {}
                return 200, {"mcp-session-id": "s"}, json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}), 0.0
            chats.append(body)
            if len(body.get("tools") or []) > codemode.OPENAI_TOOL_LIMIT:
                return 400, {}, '{"error":{"message":"too many tools"}}', 0.0
            return 200, {}, json.dumps({"choices": [{"message": {}}], "usage": {"prompt_tokens": len(json.dumps(body)) // 4, "completion_tokens": 1}}), 0.0

        deps = {"config": {"mcpUrl": "https://gw/mcp", "inferenceBaseUrl": "https://gw/inference"}, "demo_limit": lambda: None, "http": fake_http,
                "error_message": lambda s, d, t: "too many tools", "auth": lambda agent, payload, surface: {}, "parse_rpc": json.loads,
                "agent_by_name": lambda n: {"name": n}}
        status, body = codemode.run({"agent": "a", "mode": "probe", "catalog": 50}, deps)
        p = body["probe"]
        self.assertEqual((status, p["realTools"], p["decoys"], p["catalog"]), (200, 2, 48, 50))
        self.assertTrue(all(c["max_tokens"] == 1 for c in chats))
        self.assertEqual(len(chats), 4)
        self.assertTrue(all(c.get("tool_choice") == "none" for c in chats if c.get("tools")))
        self.assertGreater(p["perToolTokens"]["tools"], 10)
        self.assertGreater(p["perToolTokens"]["code"], 10)
        status, body = codemode.run({"agent": "a", "mode": "probe", "catalog": 200}, deps)
        a_big = next(x for x in body["probe"]["points"] if x["mode"] == "tools" and x["tools"] == 200)
        self.assertIn("error", a_big)
        self.assertNotIn("tools", body["probe"]["perToolTokens"])
        self.assertIn("code", body["probe"]["perToolTokens"])
        self.assertEqual(codemode.run({"agent": "a", "mode": "probe", "catalog": 5000}, deps)[0], 400)


if __name__ == "__main__":
    unittest.main()
