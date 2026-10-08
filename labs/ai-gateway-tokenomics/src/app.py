"""
AI Gateway Tokenomics - lightweight demo UI.

Standard library only (no extra packages). Azure tokens for the telemetry and budget-reset
features are obtained from the Azure CLI (`az login` is a prerequisite of the lab).

    python app.py [--port 8080] [--config demo-config.private.config]

The config file is written by the lab notebook (step 3) and contains the APIM subscription keys,
so it is git-ignored (*.private.config).
"""
import argparse, json, os, shutil, subprocess, sys, threading, time, urllib.error, urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
CONFIG = {}
_token_cache, _token_lock = {}, threading.Lock()
# Demo safety net: cap the gateway calls the UI can send, independent of the gateway's own tier limits
MAX_CALLS_PER_MINUTE = int(os.environ.get("DEMO_MAX_CALLS_PER_MINUTE", 60))
_recent_calls, _calls_lock = [], threading.Lock()

GW_HEADERS = ["x-gw-agent", "x-gw-tier", "x-gw-model-requested", "x-gw-model-served", "x-gw-governance",
              "x-gw-prompt-tokens", "x-gw-completion-tokens", "x-gw-cost-usd", "x-gw-remaining-tpm",
              "x-gw-remaining-quota-tokens", "x-gw-tokens-consumed", "x-gw-block-reason", "retry-after",
              "x-gw-billed-to", "x-gw-tool", "x-gw-remaining-tool-calls", "x-gw-remaining-agent-calls",
              "x-gw-agent-fee-usd", "x-gw-downstream-cost-usd", "mcp-session-id"]

# The Model dimension carries the model, MCP tool or A2A agent that was billed (metrics allow at most 5 custom dimensions)
DIMS = ('| extend Agent = tostring(customDimensions["Agent"]), Tier = tostring(customDimensions["Tier"]), '
        'Model = tostring(customDimensions["Model"]), Surface = coalesce(tostring(customDimensions["Surface"]), "model"), '
        'Via = coalesce(tostring(customDimensions["Via"]), "direct")')
REQUESTS = ('requests | where url has "/openai/" or url has "/commerce-mcp/" or url has "/sourcing-agent" '
            '| extend Agent = tostring(customDimensions["Subscription Name"]), Tier = tostring(customDimensions["Product Name"])')
QUERIES = {
    "costByAgent": f'customMetrics | where name == "CostMicroUSD" {DIMS} | summarize CostUSD = round(sum(valueSum) / 1e6, 6) by Agent | order by CostUSD desc',
    "costByModel": f'customMetrics | where name == "CostMicroUSD" {DIMS} | where Surface == "model" | summarize CostUSD = round(sum(valueSum) / 1e6, 6) by Model | order by CostUSD desc',
    "costBySurface": f'customMetrics | where name == "CostMicroUSD" {DIMS} | summarize CostUSD = round(sum(valueSum) / 1e6, 6) by Surface | order by CostUSD desc',
    "costByResource": f'customMetrics | where name == "CostMicroUSD" {DIMS} | summarize CostUSD = round(sum(valueSum) / 1e6, 6), Calls = sum(valueCount) by Surface, Resource = Model | order by CostUSD desc',
    "chargeback": f'customMetrics | where name == "CostMicroUSD" {DIMS} | summarize CostUSD = round(sum(valueSum) / 1e6, 6) by Agent, Tier, Surface, Via | order by Agent asc, CostUSD desc',
    "tokensByAgentModel": f'customMetrics | where name in ("Prompt Tokens", "Completion Tokens") {DIMS} '
                          '| summarize PromptTokens = sumif(valueSum, name == "Prompt Tokens"), CompletionTokens = sumif(valueSum, name == "Completion Tokens") by Agent, Model '
                          '| extend TotalTokens = PromptTokens + CompletionTokens | order by TotalTokens desc',
    "costOverTime": f'customMetrics | where name == "CostMicroUSD" {DIMS} | summarize CostUSD = round(sum(valueSum) / 1e6, 6) by Agent, bin(timestamp, 1m) | order by timestamp asc',
    "outcomesByAgent": f'{REQUESTS} | summarize Calls = count() by Agent, resultCode | order by Agent asc',
    "governance": 'customMetrics | where name == "GovernanceEvents" '
                  '| extend Agent = tostring(customDimensions["Agent"]), Event = tostring(customDimensions["Event"]), Model = tostring(customDimensions["Model"]) '
                  '| summarize Events = sum(valueSum) by Agent, Event, RequestedModel = Model | order by Events desc',
}


def az_token(resource):
    with _token_lock:
        cached = _token_cache.get(resource)
        if cached and cached[1] - 300 > time.time():
            return cached[0]
        az = shutil.which("az") or shutil.which("az.cmd")
        if not az:
            raise RuntimeError("Azure CLI not found - install it and run 'az login'")
        result = subprocess.run([az, "account", "get-access-token", "--resource", resource, "-o", "json"],
                                capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(f"az account get-access-token failed: {result.stderr.strip()}")
        data = json.loads(result.stdout)
        expires = data.get("expires_on") or time.time() + 1800
        _token_cache[resource] = (data["accessToken"], float(expires))
        return data["accessToken"]


def http(method, url, body=None, headers=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read().decode("utf-8", "replace"), time.perf_counter() - started
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read().decode("utf-8", "replace"), time.perf_counter() - started


def agent_by_name(name):
    return next((a for a in CONFIG.get("agents", []) if a["name"] == name), None)


def demo_limit():
    with _calls_lock:
        now = time.time()
        _recent_calls[:] = [t for t in _recent_calls if now - t < 60]
        if len(_recent_calls) >= MAX_CALLS_PER_MINUTE:
            return f"Demo UI safety limit: at most {MAX_CALLS_PER_MINUTE} calls per minute (set DEMO_MAX_CALLS_PER_MINUTE to change it)"
        _recent_calls.append(now)
    return None


def gw_headers(headers):
    lower = {k.lower(): v for k, v in headers.items()}
    return {h: lower[h] for h in GW_HEADERS if h in lower}


def parse_rpc(text):
    """Parses a JSON-RPC response that may be plain JSON or a Server-Sent Events stream (MCP streamable HTTP)."""
    try:
        return json.loads(text)
    except ValueError:
        for line in text.splitlines():
            if line.startswith("data:"):
                try:
                    return json.loads(line[5:].strip())
                except ValueError:
                    continue
    return {"raw": text[:500]}


def error_message(status, data, text):
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return error.get("message") or json.dumps(error)[:500]
    if isinstance(data, dict) and data.get("message"):
        return data["message"]
    return (text or f"HTTP {status}")[:500]


def chat(payload):
    agent = agent_by_name(payload.get("agent", ""))
    if not agent:
        return 400, {"error": f"Unknown agent '{payload.get('agent')}'"}
    if limited := demo_limit():
        return 429, {"error": limited}
    body = {"model": payload.get("model"), "messages": [{"role": "user", "content": payload.get("prompt") or "Hello"}]}
    if payload.get("maxTokens"):
        body["max_tokens"] = int(payload["maxTokens"])
    status, headers, text, elapsed = http("POST", f"{CONFIG['inferenceBaseUrl']}/chat/completions", body, {"api-key": agent["key"]})
    result = {"surface": "model", "status": status, "latencyMs": round(elapsed * 1000), "agent": agent["name"], "tier": agent["tier"],
              "requestedModel": payload.get("model"), "headers": gw_headers(headers)}
    try:
        data = json.loads(text)
    except ValueError:
        data = {"raw": text[:500]}
    if status == 200:
        result["usage"] = data.get("usage", {})
        result["content"] = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        result["finishReason"] = (data.get("choices") or [{}])[0].get("finish_reason")
    else:
        error = data.get("error") if isinstance(data.get("error"), dict) else data
        result["error"] = error.get("message") or error.get("raw") or text[:500]
    return 200, result


def mcp(payload):
    """MCP client: initialize a session on the gateway's MCP server, then tools/list or tools/call with the agent's key."""
    agent = agent_by_name(payload.get("agent", ""))
    if not agent:
        return 400, {"error": f"Unknown agent '{payload.get('agent')}'"}
    if limited := demo_limit():
        return 429, {"error": limited}
    url = CONFIG["mcpUrl"]
    headers = {"api-key": agent["key"], "Accept": "application/json, text/event-stream"}
    status, init_headers, text, _ = http("POST", url, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "tokenomics-demo-ui", "version": "1.0"}}}, headers, timeout=60)
    if status != 200:
        data = parse_rpc(text)
        return 200, {"surface": "tool", "status": status, "agent": agent["name"], "tier": agent["tier"], "tool": payload.get("tool"),
                     "headers": gw_headers(init_headers), "error": error_message(status, data, text)}
    session_id = {k.lower(): v for k, v in init_headers.items()}.get("mcp-session-id")
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    http("POST", url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, headers, timeout=30)
    if payload.get("action") == "list":
        request = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    else:
        request = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                   "params": {"name": payload.get("tool"), "arguments": payload.get("arguments") or {}}}
    status, headers_out, text, elapsed = http("POST", url, request, headers, timeout=60)
    data = parse_rpc(text)
    result = {"surface": "tool", "status": status, "latencyMs": round(elapsed * 1000), "agent": agent["name"], "tier": agent["tier"],
              "tool": payload.get("tool"), "action": payload.get("action") or "call", "headers": gw_headers(headers_out)}
    if status == 200 and isinstance(data, dict) and "result" in data:
        if payload.get("action") == "list":
            result["tools"] = [{"name": t.get("name"), "description": t.get("description")} for t in data["result"].get("tools", [])]
        else:
            content = data["result"].get("content") or []
            result["content"] = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
            result["isError"] = data["result"].get("isError", False)
    else:
        result["error"] = error_message(status, data, text)
    return 200, result


def a2a(payload):
    """A2A client: read the agent card or send a task (JSON-RPC message/send) through the gateway with the agent's key."""
    agent = agent_by_name(payload.get("agent", ""))
    if not agent:
        return 400, {"error": f"Unknown agent '{payload.get('agent')}'"}
    if limited := demo_limit():
        return 429, {"error": limited}
    headers = {"api-key": agent["key"]}
    if payload.get("action") == "card":
        status, headers_out, text, elapsed = http("GET", CONFIG["agentCardUrl"], None, headers, timeout=30)
        data = parse_rpc(text)
        result = {"surface": "agent", "action": "card", "status": status, "latencyMs": round(elapsed * 1000), "agent": agent["name"],
                  "tier": agent["tier"], "headers": gw_headers(headers_out)}
        if status == 200:
            result["card"] = data
        else:
            result["error"] = error_message(status, data, text)
        return 200, result
    message = {"role": "user", "messageId": f"ui-{int(time.time() * 1000)}", "kind": "message",
               "parts": [{"kind": "text", "text": payload.get("prompt") or "Source 100 units of SKU-1001"}]}
    request = {"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {"message": message}}
    status, headers_out, text, elapsed = http("POST", CONFIG["a2aUrl"], request, headers, timeout=180)
    data = parse_rpc(text)
    result = {"surface": "agent", "action": "send", "status": status, "latencyMs": round(elapsed * 1000), "agent": agent["name"],
              "tier": agent["tier"], "a2aAgent": "sourcing-agent", "headers": gw_headers(headers_out)}
    task = data.get("result") if isinstance(data, dict) else None
    if status == 200 and isinstance(task, dict):
        texts = []
        for artifact in task.get("artifacts") or []:
            texts += [p.get("text", "") for p in artifact.get("parts", []) if p.get("kind") == "text"]
        result["content"] = "\n".join(texts)
        result["state"] = (task.get("status") or {}).get("state")
        result["steps"] = (task.get("metadata") or {}).get("steps", [])
    else:
        result["error"] = error_message(status, data, text)
    return 200, result


def telemetry(payload):
    app_id = CONFIG.get("appInsightsAppId")
    timespan = payload.get("timespan", "PT1H")
    token = az_token("https://api.applicationinsights.io")
    results = {}
    for name, query in QUERIES.items():
        status, _, text, _ = http("POST", f"https://api.applicationinsights.io/v1/apps/{app_id}/query",
                                  {"query": query, "timespan": timespan}, {"Authorization": f"Bearer {token}"})
        if status != 200:
            results[name] = {"error": text[:300]}
            continue
        table = json.loads(text)["tables"][0]
        columns = [c["name"] for c in table["columns"]]
        results[name] = [dict(zip(columns, row)) for row in table["rows"]]
    return 200, results


def reset_budgets(_payload):
    token = az_token("https://management.azure.com")
    epoch = str(int(time.time()))
    url = f"https://management.azure.com{CONFIG['apimServiceId']}/namedValues/budget-epoch?api-version=2024-05-01"
    status, _, text, _ = http("PATCH", url, {"properties": {"value": epoch}}, {"Authorization": f"Bearer {token}", "If-Match": "*"})
    if status not in (200, 202):
        return 502, {"error": text[:500]}
    return 200, {"epoch": epoch, "message": "Budgets and token quotas reset (named value budget-epoch updated). It can take a few seconds to apply."}


def public_config():
    portal = "https://portal.azure.com/#@/resource"
    return {
        "agents": [{k: v for k, v in a.items() if k != "key"} for a in CONFIG.get("agents", [])],
        "tiers": [t for t in CONFIG.get("tiers", []) if not t.get("internal")],
        "models": CONFIG.get("models", []),
        "tools": CONFIG.get("tools", []),
        "a2aAgents": CONFIG.get("a2aAgents", []),
        "endpoints": {k: CONFIG.get(k) for k in ("inferenceBaseUrl", "mcpUrl", "a2aUrl", "agentCardUrl")},
        "links": {
            "workbook": f"{portal}{CONFIG['workbookId']}/workbook" if CONFIG.get("workbookId") else None,
            "appInsights": f"{portal}{CONFIG['appInsightsId']}/overview" if CONFIG.get("appInsightsId") else None,
            "apim": f"{portal}{CONFIG['apimServiceId']}/overview" if CONFIG.get("apimServiceId") else None,
        },
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC, **kwargs)

    def log_message(self, fmt, *args):
        sys.stderr.write("[ui] " + (fmt % args) + "\n")

    def send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/config":
            return self.send_json(200, public_config())
        return super().do_GET()

    def do_POST(self):
        routes = {"/api/chat": chat, "/api/mcp": mcp, "/api/a2a": a2a, "/api/telemetry": telemetry, "/api/reset-budgets": reset_budgets}
        handler = routes.get(self.path)
        if not handler:
            return self.send_json(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            self.send_json(*handler(payload))
        except Exception as error:  # surface errors to the UI instead of dropping the connection
            self.send_json(500, {"error": str(error)})


def main():
    parser = argparse.ArgumentParser(description="AI Gateway Tokenomics demo UI")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)))
    parser.add_argument("--config", default=os.path.join(HERE, "demo-config.private.config"))
    args = parser.parse_args()
    if not os.path.exists(args.config):
        sys.exit(f"Config file not found: {args.config}. Run step 3 of the lab notebook first.")
    with open(args.config, encoding="utf-8") as f:
        CONFIG.update(json.load(f))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"AI Gateway Tokenomics demo UI running on http://localhost:{args.port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
