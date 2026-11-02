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

    def test_search_mode_needs_the_toolbox_setup(self):
        with self.assertRaisesRegex(RuntimeError, "setup_toolbox.py"):
            self._toolbox_run({}).toolbox_url()
        status, body = codemode.run({"agent": "a", "mode": "kong"}, {"agent_by_name": lambda n: {"name": n}})
        self.assertEqual((status, body["error"]), (400, "mode must be 'tools', 'search' or 'code'"))


if __name__ == "__main__":
    unittest.main()
