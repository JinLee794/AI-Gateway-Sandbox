"""
Code mode spike (experimental): the same agent task run two ways against the gateway's MCP server.

  tools  Classic MCP tool calling. Every tool definition is sent to the model, the model asks for one tool at a time,
         and every tool result is appended to the conversation, so the whole history is re-sent on every round.
  code   Code mode. The model sees typed Python stubs generated from tools/list and writes one script. The script runs
         in a locked-down subprocess; each stub call is proxied back here and sent as a real MCP tools/call through
         API Management with the caller's key. Only what the script prints goes back to the model.
  search Tool search (the POC's mode B). The model talks to a Foundry Toolbox with tool search enabled: tools/list only
         returns tool_search and call_tool, the model looks up the tools it needs, and Foundry relays each call_tool to
         the same APIM MCP server with the Foundry project's managed identity (set up by infra/scripts/setup_toolbox.py).

In every mode every tool call is a separate gateway call, so the plan's tool entitlements, per-call pricing, rate
limits and logs still apply to code written by the model.

Spike-grade sandbox: an AST allow-list, restricted builtins, a separate interpreter process (-I), a wall-clock timeout,
a tool-call cap and (on Linux) CPU and memory limits. A production build would run the script in a real isolation
boundary such as Hyperlight or Azure Container Apps dynamic sessions.

Standard library only; app.py passes in its http, auth and rate-limit helpers so this module has no global state.
"""
import ast, json, queue, re, secrets, subprocess, sys, threading, time

MAX_ROUNDS = 8            # tool-calling rounds before the classic loop gives up
MAX_TOOL_CALLS = 15       # tool calls per run, in either mode
MAX_CODE_CHARS = 6000
SCRIPT_TIMEOUT_S = 60
MAX_OUTPUT_CHARS = 4000   # what a script may print (all of it goes back to the model)
MAX_TOOL_RESULT_CHARS = 6000
MAX_TOKENS = 900

DEFAULT_TASK = ("Find the electronics products. For each SKU, check inventory across all warehouses. For every SKU with "
                "fewer than 150 units on hand in total, get supplier quotes for 500 units and pick the supplier with the lowest "
                "total cost (break ties by shorter lead time). Reply with one line per SKU: SKU, product name, total units on "
                "hand, and either 'in stock' or the chosen supplier with its total cost and lead time.")

TOOLS_SYSTEM = ("You are a procurement analyst. Use the tools to gather the data you need, then answer. "
                "Be precise with numbers and keep the final answer short.")

SEARCH_SYSTEM = ("You are a procurement analyst. You start with two tools: tool_search finds the tools you need from a "
                 "natural-language description, and call_tool invokes a found tool by its exact name with its arguments. "
                 "Found tools are NOT functions you can call directly: always invoke them through call_tool, e.g. "
                 "call_tool(name=\"commerce___check-inventory\", arguments={\"sku\": \"SKU-1001\"}). "
                 "First list the capabilities the task needs, then search once per capability (you can search several in "
                 "parallel). Never repeat a call with the same arguments: reuse results you already have. "
                 "Be precise with numbers and keep the final answer short.")

FOUNDRY_AUDIENCE = "https://ai.azure.com"
DEFAULT_TOOLBOX = "agent-ready-apis"

CODE_SYSTEM = """You are a procurement analyst who answers by writing one Python script.
The script runs in a sandbox where only these functions exist (each one is a governed MCP tool call through an API gateway):

{stubs}

Rules:
- No imports, no file or network access, no names or attributes that start with an underscore, no str.format (use f-strings).
- Each function returns the tool's JSON result as a dict. On a gateway denial it returns {{"error": "...", "status": <http status>}}.
- Do all looping, filtering and arithmetic in the script. Call each tool only as often as needed.
- print() only the compact facts needed for the final answer (at most 30 lines). Do not print raw tool results.
- Print a line for every item the task asks about, including items that need no action.
Reply with a single ```python code block and nothing else."""

ANSWER_SYSTEM = ("You are a procurement analyst. Write the final answer for the user from the script output. The script already "
                 "applied the task's filters: keep every line it reports, including items that need no action, and do not filter "
                 "again. Be precise with numbers and keep it short.")

# Code mode needs to know what each tool returns. MCP tools/list may carry an outputSchema; the gateway's API-to-MCP
# server does not (the OpenAPI responses have no schemas), so the spike documents the result shapes here, which is what
# a response schema in the OpenAPI definition would give you. Without them the model guesses field names.
RESULT_SHAPES = {
    "search-products": '{"category": str, "products": [{"sku": str, "name": str, "listPriceUsd": float}]}',
    "check-inventory": '{"sku": str, "warehouses": [{"warehouse": str, "onHand": int}]}',
    "get-supplier-quote": '{"sku": str, "quantity": int, "quotes": [{"supplier": str, "unitPriceUsd": float, "totalUsd": float, "leadTimeDays": int}]}',
}

SAFE_BUILTINS = ("abs", "all", "any", "bool", "dict", "enumerate", "filter", "float", "int", "isinstance", "len", "list", "map", "max",
                 "min", "range", "reversed", "round", "set", "sorted", "str", "sum", "tuple", "zip",
                 "Exception", "KeyError", "ValueError", "TypeError", "IndexError", "ZeroDivisionError")
BLOCKED_NODES = (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal, ast.ClassDef, ast.AsyncFunctionDef, ast.Await,
                 ast.AsyncFor, ast.AsyncWith, ast.With, ast.Yield, ast.YieldFrom)
BLOCKED_ATTRS = {"format", "format_map", "mro"}
BLOCKED_NAMES = {"eval", "exec", "compile", "open", "input", "getattr", "setattr", "delattr", "globals", "locals", "vars", "type",
                 "breakpoint", "help", "memoryview", "object", "super", "classmethod", "staticmethod", "property"}

# Runs in a separate interpreter (python -I). Tool calls are JSON lines on stdout, answered on stdin.
RUNNER = r'''
import builtins, io, json, sys
try:
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
except Exception:
    pass
out, inp = sys.stdout, sys.stdin
spec = json.loads(inp.readline())
buf, size = io.StringIO(), [0]
def emit(*args, sep=" ", end="\n"):
    text = sep.join(str(a) for a in args) + end
    size[0] += len(text)
    if size[0] > spec["maxOutput"]:
        raise RuntimeError("the script printed too much output")
    buf.write(text)
def make(tool, params):
    def call(*args, **kwargs):
        arguments = dict(zip(params, args))
        arguments.update(kwargs)
        out.write(json.dumps({"call": tool, "args": arguments}) + "\n")
        out.flush()
        line = inp.readline()
        if not line:
            raise SystemExit(3)
        reply = json.loads(line)
        if reply.get("fatal"):
            raise RuntimeError(reply["fatal"])
        return reply["result"]
    return call
env = {"__builtins__": {name: getattr(builtins, name) for name in spec["builtins"]}}
env["__builtins__"]["print"] = emit
for fn, (tool, params) in spec["functions"].items():
    env[fn] = make(tool, params)
error = None
try:
    exec(compile(spec["code"], "<agent-script>", "exec"), env)
except BaseException as e:
    error = f"{type(e).__name__}: {e}"
out.write(json.dumps({"done": True, "output": buf.getvalue(), "error": error}) + "\n")
out.flush()
'''


def py_name(tool_name):
    name = re.sub(r"\W", "_", tool_name)
    return name if not name[:1].isdigit() else "t_" + name


def stubs(tools):
    """Typed Python stubs for the model, generated from the MCP tools/list schemas (plus RESULT_SHAPES when there is no outputSchema)."""
    types = {"string": "str", "integer": "int", "number": "float", "boolean": "bool", "array": "list", "object": "dict"}
    lines = []
    for t in tools:
        props = (t.get("inputSchema") or {}).get("properties") or {}
        required = set((t.get("inputSchema") or {}).get("required") or [])
        args = [f"{p}: {types.get((s or {}).get('type'), 'str')}" + ("" if p in required else " = None") for p, s in props.items()]
        docs = [f"    {p}: {(s or {}).get('description', '')}".rstrip() for p, s in props.items() if (s or {}).get("description")]
        returns = json.dumps(t["outputSchema"], separators=(",", ":")) if t.get("outputSchema") else RESULT_SHAPES.get(t["name"])
        if returns:
            docs.append(f"    Returns: {returns}")
        lines.append(f"def {py_name(t['name'])}({', '.join(args)}) -> dict:\n    \"\"\"{t.get('description') or t['name']}"
                     + ("\n" + "\n".join(docs) + "\n    " if docs else "") + "\"\"\"")
    return "\n\n".join(lines)


def check_code(code):
    """AST allow-list for model-written code. Returns an error message, or None when the code may run."""
    if len(code) > MAX_CODE_CHARS:
        return f"the script is longer than {MAX_CODE_CHARS} characters"
    try:
        tree = ast.parse(code)
    except SyntaxError as error:
        return f"SyntaxError: {error.msg} (line {error.lineno})"
    for node in ast.walk(tree):
        if isinstance(node, BLOCKED_NODES):
            return f"{type(node).__name__} is not allowed in the sandbox"
        if isinstance(node, ast.Attribute) and (node.attr.startswith("_") or node.attr in BLOCKED_ATTRS):
            return f"attribute '{node.attr}' is not allowed in the sandbox"
        if isinstance(node, ast.Name) and (node.id.startswith("_") or node.id in BLOCKED_NAMES):
            return f"name '{node.id}' is not allowed in the sandbox"
        if isinstance(node, ast.arg) and node.arg.startswith("_"):
            return f"argument '{node.arg}' is not allowed in the sandbox"
    return None


def extract_code(text):
    match = re.search(r"```(?:python|py)?\s*\n(.*?)```", text or "", re.S)
    return (match.group(1) if match else (text or "")).strip()


class Run:
    """One run of one mode: an MCP session on the gateway plus the model calls, with everything measured."""

    def __init__(self, deps, agent, payload, mode):
        self.deps, self.agent, self.payload, self.mode = deps, agent, payload, mode
        self.model = payload.get("model") or "gpt-4.1-mini"
        self.mcp_session, self.rpc_id = None, 0
        self.tb_session = None  # Foundry Toolbox MCP session (search mode)
        self.model_calls, self.tool_calls = [], []
        self.seq = 0            # order of model and tool calls across both lists
        self.context_chars = 0  # tool output that entered the model's context
        # One W3C trace per run, sent on every call. APIM (W3C correlation) logs it as OperationId on its App Insights
        # requests and chargeback traces, and Foundry propagates it on the calls it relays, so all of a run's calls,
        # including the Toolbox's, can be found in the logs by this id.
        self.trace_id = secrets.token_hex(16)
        self.started, self.started_at = time.perf_counter(), time.time()  # for each call's startMs (the run's timeline)

    def _start_ms(self, elapsed=0.0):
        """When a call that just took `elapsed` seconds started, in ms since the run began."""
        return max(0, round((time.perf_counter() - self.started - elapsed) * 1000))

    def _traceparent(self):
        return f"00-{self.trace_id}-{secrets.token_hex(8)}-01"

    def _next(self):
        self.seq += 1
        return self.seq

    # ---- gateway calls ----
    def _limit(self):
        limited = self.deps["demo_limit"]()
        if limited:
            raise RuntimeError(limited)

    def _rpc(self, method, params=None, notify=False):
        headers = {**self.deps["auth"](self.agent, self.payload, "tool"), "Accept": "application/json, text/event-stream",
                   "traceparent": self._traceparent()}
        headers.pop("Apim-Debug-Authorization", None)
        if self.mcp_session:
            headers["Mcp-Session-Id"] = self.mcp_session
        body = {"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})}
        if not notify:
            self.rpc_id += 1
            body["id"] = self.rpc_id  # the APIM MCP server only accepts numeric ids
        status, headers_out, text, elapsed = self.deps["http"]("POST", self.deps["config"]["mcpUrl"], body, headers, timeout=60)
        lower = {k.lower(): v for k, v in headers_out.items()}
        self.mcp_session = lower.get("mcp-session-id", self.mcp_session)
        return status, lower, self.deps["parse_rpc"](text) if text else {}, text, elapsed

    def open_mcp(self):
        self._limit()
        status, _, data, text, _ = self._rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                                            "clientInfo": {"name": "sandbox-code-mode", "version": "0.1"}})
        if status != 200:
            raise RuntimeError(f"MCP initialize failed ({status}): {self.deps['error_message'](status, data, text)}")
        self._rpc("notifications/initialized", notify=True)
        self._limit()
        status, _, data, text, _ = self._rpc("tools/list", {})
        if status != 200 or "result" not in data:
            raise RuntimeError(f"MCP tools/list failed ({status}): {self.deps['error_message'](status, data, text)}")
        return data["result"].get("tools") or []

    def tool(self, name, arguments):
        if len(self.tool_calls) >= MAX_TOOL_CALLS:
            raise RuntimeError(f"tool-call cap reached ({MAX_TOOL_CALLS})")
        self._limit()
        status, headers, data, text, elapsed = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        entry = {"seq": self._next(), "name": name, "args": arguments or {}, "status": status, "latencyMs": round(elapsed * 1000),
                 "startMs": self._start_ms(elapsed), "costUsd": _float(headers.get("x-gw-cost-usd")), "governance": headers.get("x-gw-governance"),
                 "requestId": headers.get("x-gw-request-id")}
        return self._record(entry, status, data, text)

    def _record(self, entry, status, data, text):
        if status == 200 and isinstance(data, dict) and "result" in data and not data["result"].get("isError"):
            out = "\n".join(c.get("text", "") for c in data["result"].get("content") or [] if isinstance(c, dict))
            try:
                result = json.loads(out)
            except ValueError:
                result = {"text": out}
        else:
            entry["error"] = self.deps["error_message"](status, data, text) if status != 200 else json.dumps((data.get("result") or data))[:300]
            result = {"error": entry["error"], "status": status}
        entry["resultChars"] = len(json.dumps(result))
        self.tool_calls.append(entry)
        return result

    # ---- Foundry Toolbox (search mode) ----
    def toolbox_url(self):
        config = self.deps["config"]
        endpoint = (config.get("foundryProjectEndpoint") or "").rstrip("/")
        if not endpoint:
            raise RuntimeError("No Foundry project endpoint in the sandbox config. Run infra/scripts/setup_toolbox.py "
                               "(see the scenario explanation) to create the Toolbox for mode B.")
        return f"{endpoint}/toolboxes/{config.get('toolboxName') or DEFAULT_TOOLBOX}/mcp?api-version=v1"

    def _tb_rpc(self, method, params=None, notify=False):
        headers = {"Authorization": f"Bearer {self.deps['token'](FOUNDRY_AUDIENCE)}", "Accept": "application/json, text/event-stream"}
        headers["traceparent"] = self._traceparent()
        if self.tb_session:
            headers["Mcp-Session-Id"] = self.tb_session
        body = {"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})}
        if not notify:
            self.rpc_id += 1
            body["id"] = self.rpc_id
        status, headers_out, text, elapsed = self.deps["http"]("POST", self.toolbox_url(), body, headers, timeout=120)
        lower = {k.lower(): v for k, v in headers_out.items()}
        self.tb_session = lower.get("mcp-session-id", self.tb_session)
        return status, lower, self.deps["parse_rpc"](text) if text else {}, text, elapsed

    def open_toolbox(self):
        status, _, data, text, _ = self._tb_rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                                               "clientInfo": {"name": "sandbox-code-mode", "version": "0.1"}})
        if status != 200:
            raise RuntimeError(f"Foundry Toolbox initialize failed ({status}): {self.deps['error_message'](status, data, text)}")
        self._tb_rpc("notifications/initialized", notify=True)
        status, _, data, text, _ = self._tb_rpc("tools/list", {})
        if status != 200 or "result" not in data:
            raise RuntimeError(f"Foundry Toolbox tools/list failed ({status}): {self.deps['error_message'](status, data, text)}")
        return data["result"].get("tools") or []

    def toolbox_tool(self, name, arguments):
        """tool_search runs in Foundry; call_tool is relayed by Foundry to API Management with the project's identity."""
        arguments = arguments or {}
        if name not in ("tool_search", "call_tool"):
            # models sometimes call a discovered tool by name; relay it the way the Toolbox expects
            name, arguments = "call_tool", {"name": name, "arguments": arguments}
        relayed = name == "call_tool"
        if relayed and len([c for c in self.tool_calls if c.get("via") == "toolbox"]) >= MAX_TOOL_CALLS:
            raise RuntimeError(f"tool-call cap reached ({MAX_TOOL_CALLS})")
        self._limit()
        status, tb_headers, data, text, elapsed = self._tb_rpc("tools/call", {"name": name, "arguments": arguments})
        inner = str(arguments.get("name") or "")
        tool = inner.split("___")[-1] if relayed else name
        price = next((t.get("priceUsd", 0) for t in self.deps["config"].get("tools") or [] if t.get("name") == tool), 0)
        entry = {"seq": self._next(), "name": tool if relayed else name, "args": arguments.get("arguments") if relayed else arguments,
                 "status": status, "latencyMs": round(elapsed * 1000), "startMs": self._start_ms(elapsed), "costUsd": price if relayed and status == 200 else 0,
                 "via": "toolbox" if relayed else "search", "governance": "Foundry Toolbox -> APIM (project managed identity)" if relayed else "Foundry tool search (no gateway call)"}
        if relayed:
            entry["traceId"] = (tb_headers.get("x-request-id") or self.trace_id).replace("-", "")
        else:
            entry["query"] = arguments.get("query")
        return self._record(entry, status, data, text)

    def chat(self, messages, step, tools=None):
        self._limit()
        body = {"model": self.model, "messages": messages, "max_tokens": MAX_TOKENS, "temperature": 0}
        if tools:
            body["tools"], body["tool_choice"] = tools, "auto"
        headers = {**self.deps["auth"](self.agent, {**self.payload, "cacheScope": "off"}, "model"), "traceparent": self._traceparent()}
        headers.pop("Apim-Debug-Authorization", None)
        status, headers_out, text, elapsed = self.deps["http"]("POST", f"{self.deps['config']['inferenceBaseUrl']}/chat/completions", body, headers)
        lower = {k.lower(): v for k, v in headers_out.items()}
        try:
            data = json.loads(text)
        except ValueError:
            data = {}
        usage = data.get("usage") or {}
        entry = {"seq": self._next(), "step": step, "status": status, "latencyMs": round(elapsed * 1000), "startMs": self._start_ms(elapsed),
                 "model": lower.get("x-gw-model-served") or self.model,
                 "promptTokens": usage.get("prompt_tokens", 0), "completionTokens": usage.get("completion_tokens", 0),
                 "costUsd": _float(lower.get("x-gw-cost-usd")), "messages": len(messages), "requestChars": len(json.dumps(body)),
                 "requestId": lower.get("x-gw-request-id")}
        self.model_calls.append(entry)
        if status != 200:
            entry["error"] = self.deps["error_message"](status, data, text)
            raise RuntimeError(f"model call failed ({status}): {entry['error']}")
        return (data.get("choices") or [{}])[0].get("message") or {}

    # ---- the three modes ----
    def run_tools(self, task, tools, dispatch=None, system=TOOLS_SYSTEM):
        dispatch = dispatch or self.tool
        definitions = [{"type": "function", "function": {"name": t["name"], "description": t.get("description") or t["name"],
                                                         "parameters": t.get("inputSchema") or {"type": "object", "properties": {}}}} for t in tools]
        messages = [{"role": "system", "content": system}, {"role": "user", "content": task}]
        for rnd in range(1, MAX_ROUNDS + 1):
            msg = self.chat(messages, f"round {rnd}", definitions)
            calls = msg.get("tool_calls") or []
            if not calls:
                return {"answer": msg.get("content") or ""}
            messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": calls})
            for c in calls:
                fn = c.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                result = json.dumps(dispatch(fn.get("name"), args))[:MAX_TOOL_RESULT_CHARS]
                self.context_chars += len(result)
                messages.append({"role": "tool", "tool_call_id": c.get("id"), "content": result})
        return {"answer": "", "error": f"no answer after {MAX_ROUNDS} tool-calling rounds"}

    def run_code(self, task, tools):
        stub_text = stubs(tools)
        functions = {py_name(t["name"]): [t["name"], list(((t.get("inputSchema") or {}).get("properties") or {}).keys())] for t in tools}
        messages = [{"role": "system", "content": CODE_SYSTEM.format(stubs=stub_text)}, {"role": "user", "content": task}]
        code, script = "", {}
        for attempt in (1, 2):  # one repair round when the script is rejected or fails
            msg = self.chat(messages, "write the script" if attempt == 1 else "fix the script")
            code = extract_code(msg.get("content"))
            problem = check_code(code)
            script_start = self._start_ms()
            script =  {"output": "", "error": f"rejected by the sandbox: {problem}"} if problem else self.execute(code, functions)
            if not script.get("error"):
                break
            messages += [{"role": "assistant", "content": msg.get("content") or ""},
                         {"role": "user", "content": f"The script failed: {script['error']}\nOutput so far:\n{script.get('output', '')[:1000]}\nReply with a corrected script."}]
        output = script.get("output", "")
        self.context_chars += len(output)
        result = {"code": code, "stubs": stub_text, "scriptOutput": output, "scriptError": script.get("error"), "scriptMs": script.get("ms"),
                  "scriptStartMs": script_start}
        if script.get("error") and not output:
            return {**result, "answer": "", "error": f"the script failed: {script['error']}"}
        final = self.chat([{"role": "system", "content": ANSWER_SYSTEM},
                           {"role": "user", "content": f"Task: {task}\n\nScript output:\n{output}"}], "write the answer")
        return {**result, "answer": final.get("content") or ""}

    def execute(self, code, functions):
        """Runs the script in a separate interpreter; each stub call becomes a governed MCP tools/call here."""
        began = time.perf_counter()
        proc = subprocess.Popen([sys.executable, "-I", "-c", RUNNER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8")
        lines = queue.Queue()
        threading.Thread(target=lambda: [lines.put(l) for l in proc.stdout] + [lines.put(None)], daemon=True).start()
        deadline = time.time() + SCRIPT_TIMEOUT_S
        try:
            proc.stdin.write(json.dumps({"code": code, "functions": functions, "builtins": SAFE_BUILTINS, "maxOutput": MAX_OUTPUT_CHARS}) + "\n")
            proc.stdin.flush()
            while True:
                try:
                    line = lines.get(timeout=max(0.1, deadline - time.time()))
                except queue.Empty:
                    return {"output": "", "error": f"timed out after {SCRIPT_TIMEOUT_S} s", "ms": _ms(began)}
                if line is None:
                    err = proc.stderr.read()[-300:] if proc.poll() is not None else ""
                    return {"output": "", "error": f"the sandbox exited ({proc.returncode}) {err}".strip(), "ms": _ms(began)}
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("done"):
                    return {"output": msg.get("output", ""), "error": msg.get("error"), "ms": _ms(began)}
                try:
                    reply = {"result": self.tool(msg.get("call"), msg.get("args") or {})}
                except RuntimeError as error:
                    reply = {"fatal": str(error)}
                proc.stdin.write(json.dumps(reply) + "\n")
                proc.stdin.flush()
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    pipe.close()
                except OSError:
                    pass

    def summary(self, started, result):
        prompt = sum(c["promptTokens"] for c in self.model_calls)
        completion = sum(c["completionTokens"] for c in self.model_calls)
        cost = sum(c["costUsd"] for c in self.model_calls) + sum(c["costUsd"] for c in self.tool_calls)
        return {"status": 200 if not result.get("error") else 500, "mode": self.mode, "agent": self.agent["name"], "tier": self.agent.get("tier", ""),
                "model": self.model, "modelCalls": self.model_calls, "toolCalls": self.tool_calls, "promptTokens": prompt,
                "completionTokens": completion, "totalTokens": prompt + completion, "costUsd": round(cost, 6),
                "contextChars": self.context_chars, "elapsedMs": _ms(started), "traceId": self.trace_id,
                "startedAt": round(self.started_at * 1000), **result}


def run(payload, deps):
    """POST /api/codemode {agent, model, mode: tools | search | code, task?, session?}"""
    agent = deps["agent_by_name"](payload.get("agent", ""))
    if not agent:
        return 400, {"error": f"Unknown agent '{payload.get('agent')}'"}
    mode = payload.get("mode")
    if mode not in ("tools", "search", "code"):
        return 400, {"error": "mode must be 'tools', 'search' or 'code'"}
    task = payload.get("task") or DEFAULT_TASK
    if not isinstance(task, str) or len(task) > 2000:
        return 400, {"error": "task must be text of at most 2,000 characters"}
    started = time.perf_counter()
    r = Run(deps, agent, {k: payload.get(k) for k in ("session", "noToken", "model")}, mode)
    try:
        if mode == "search":
            tools = r.open_toolbox()
            result = r.run_tools(task, tools, r.toolbox_tool, SEARCH_SYSTEM)
            result["toolbox"] = r.toolbox_url().split("?")[0]
            result["toolboxPlan"] = "the Toolbox connection's subscription (one plan for every caller)"
            result["autoPinned"] = [t.get("name") for t in tools if (t.get("_meta") or {}).get("com.microsoft.foundry/tool_visibility") == "auto_pinned"]
        else:
            tools = r.open_mcp()
            result = r.run_tools(task, tools) if mode == "tools" else r.run_code(task, tools)
        result["toolsAdvertised"] = len(tools)
        result["toolNamesAdvertised"] = [t.get("name") for t in tools]
    except RuntimeError as error:
        result = {"answer": "", "error": str(error)}
    return 200, {**r.summary(started, result), "task": task}


def _float(value):
    try:
        return float(value or 0)
    except ValueError:
        return 0.0


def _ms(began):
    return round((time.perf_counter() - began) * 1000)
