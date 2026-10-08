"""
Sourcing Agent - a minimal A2A (Agent2Agent, JSON-RPC) agent used by the AI Gateway Sandbox lab.

Standard library only, so it runs on a plain Python base image without building a container.

The agent is published through Azure API Management as an A2A agent API. For each task it:
  1. calls the commerce MCP server (through the gateway) - search-products, check-inventory, get-supplier-quote
  2. asks a model (through the gateway) to write a sourcing recommendation
  3. returns an A2A task with the result and a cost breakdown

It calls the gateway with its own platform identity (an APIM subscription of the internal agent-platform
product) plus a Microsoft Entra ID token of its user-assigned managed identity (the gateway validates it with
validate-azure-ad-token), and propagates the x-gw-on-behalf-of* headers stamped by the gateway (payer, tier, via and
session), so every model token and
tool call it makes is charged back to the subscription that delegated the task. It reports the downstream
spend in the x-agent-downstream-cost-micro-usd response header so the gateway can charge the fully-loaded
cost to the caller's budget.

Environment variables: GATEWAY_KEY, MCP_URL, INFERENCE_URL, MODEL, AGENT_BACKEND_SECRET, PORT, AZURE_CLIENT_ID
(+ IDENTITY_ENDPOINT / IDENTITY_HEADER injected by Azure Container Apps)
"""
import json, os, re, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GATEWAY_KEY = os.environ.get("GATEWAY_KEY", "")
MCP_URL = os.environ.get("MCP_URL", "")
INFERENCE_URL = os.environ.get("INFERENCE_URL", "")
MODEL = os.environ.get("MODEL", "gpt-4.1-mini")
BACKEND_SECRET = os.environ.get("AGENT_BACKEND_SECRET", "")
AZURE_CLIENT_ID = os.environ.get("AZURE_CLIENT_ID", "")
TOKEN_AUDIENCE = "https://cognitiveservices.azure.com"
PROPAGATED = ("x-gw-on-behalf-of", "x-gw-on-behalf-of-tier", "x-gw-via-agent", "x-gw-on-behalf-of-session")
_token_cache = {"value": "", "expires": 0}
_token_lock = threading.Lock()


def entra_token():
    """Entra ID token of the Container App's user-assigned managed identity (Container Apps identity endpoint)."""
    endpoint, secret = os.environ.get("IDENTITY_ENDPOINT"), os.environ.get("IDENTITY_HEADER")
    if not endpoint or not secret:
        return ""
    with _token_lock:
        if _token_cache["value"] and _token_cache["expires"] - 300 > time.time():
            return _token_cache["value"]
        query = urllib.parse.urlencode({"resource": TOKEN_AUDIENCE, "api-version": "2019-08-01",
                                        **({"client_id": AZURE_CLIENT_ID} if AZURE_CLIENT_ID else {})})
        request = urllib.request.Request(f"{endpoint}?{query}", headers={"X-IDENTITY-HEADER": secret})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.loads(response.read())
        except (urllib.error.URLError, ValueError) as error:
            print(f"managed identity token request failed: {error}", flush=True)
            return ""
        _token_cache.update(value=data["access_token"], expires=int(data.get("expires_on", time.time() + 600)))
        return _token_cache["value"]


def agent_card(base_url):
    return {
        "protocolVersion": "0.3.0",
        "name": "Sourcing Agent",
        "description": "Procurement agent: finds products, checks inventory, collects live supplier quotes and recommends how to source them.",
        "url": base_url,
        "preferredTransport": "JSONRPC",
        "version": "1.0.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [{
            "id": "source-product",
            "name": "Source a product",
            "description": "Recommend how to source a quantity of a SKU using inventory and live supplier quotes.",
            "tags": ["procurement", "inventory", "pricing"],
            "examples": ["Source 200 units of SKU-1002 for the Dallas store"],
        }],
    }


def call(url, body, headers, timeout=60):
    data = json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, {k.lower(): v for k, v in error.headers.items()}, error.read().decode("utf-8", "replace")


def rpc_payload(text):
    """Parse a JSON-RPC response that may be plain JSON or a Server-Sent Events stream (MCP streamable HTTP)."""
    text = text.strip()
    if text.startswith("{"):
        return json.loads(text)
    for line in text.splitlines():
        if line.startswith("data:"):
            try:
                return json.loads(line[5:].strip())
            except ValueError:
                continue
    return {}


def micro_usd(headers):
    try:
        return int(round(float(headers.get("x-gw-cost-usd", "0")) * 1_000_000))
    except ValueError:
        return 0


class Task:
    def __init__(self, upstream_headers):
        self.base = {"api-key": GATEWAY_KEY, **{h: upstream_headers[h] for h in PROPAGATED if h in upstream_headers}}
        token = entra_token()
        if token:
            self.base["Authorization"] = f"Bearer {token}"
        self.steps, self.session, self.next_id = [], None, 0

    def mcp(self, method, params=None, notify=False):
        headers = {**self.base, "Accept": "application/json, text/event-stream"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        body = {"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})}
        if not notify:
            # The APIM MCP server only accepts numeric JSON-RPC ids
            self.next_id += 1
            body["id"] = self.next_id
        status, headers_out, text = call(MCP_URL, body, headers)
        self.session = headers_out.get("mcp-session-id", self.session)
        return status, headers_out, text

    def tool(self, name, arguments):
        status, headers, text = self.mcp("tools/call", {"name": name, "arguments": arguments})
        cost = micro_usd(headers) if status == 200 else 0
        payload = rpc_payload(text) if text else {}
        output = None
        if status == 200 and "result" in payload:
            parts = payload["result"].get("content") or [{}]
            output = parts[0].get("text")
            try:
                output = json.loads(output)
            except (TypeError, ValueError):
                pass
        error = None if output is not None else (payload.get("error", {}).get("message") or text[:200] or f"HTTP {status}")
        self.steps.append({"type": "tool", "name": name, "status": status, "costMicroUsd": cost,
                           "billedTo": headers.get("x-gw-billed-to"), **({"error": error} if error else {})})
        return output

    def model(self, prompt):
        body = {"model": MODEL, "max_tokens": 350, "messages": [
            {"role": "system", "content": "You are a procurement analyst. Write a concise sourcing recommendation (max 6 bullet points) using only the data provided. Name the recommended supplier and the expected total cost."},
            {"role": "user", "content": prompt}]}
        status, headers, text = call(f"{INFERENCE_URL}/chat/completions", body, self.base, timeout=90)
        cost = micro_usd(headers) if status == 200 else 0
        try:
            data = json.loads(text)
        except ValueError:
            data = {}
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content") if status == 200 else None
        self.steps.append({"type": "model", "name": headers.get("x-gw-model-served") or MODEL, "status": status, "costMicroUsd": cost,
                           "tokens": (data.get("usage") or {}).get("total_tokens"), "billedTo": headers.get("x-gw-billed-to"),
                           **({} if content else {"error": (data.get("error") or {}).get("message") or text[:200]})})
        return content

    def run(self, request_text):
        sku = (re.search(r"SKU-\d+", request_text, re.I) or [None])[0]
        sku = sku.upper() if sku else "SKU-1001"
        quantity = int((re.search(r"(\d+)\s*(units|pcs|pieces|x\b)", request_text, re.I) or [None, "100"])[1])
        category = next((c for c in ("electronics", "appliances", "clothing") if c in request_text.lower()), "electronics")

        status, _, _ = self.mcp("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                               "clientInfo": {"name": "sourcing-agent", "version": "1.0.0"}})
        if status == 200:
            self.mcp("notifications/initialized", notify=True)
        catalog = self.tool("search-products", {"category": category})
        inventory = self.tool("check-inventory", {"sku": sku})
        quotes = self.tool("get-supplier-quote", {"sku": sku, "quantity": quantity})
        data = json.dumps({"request": request_text, "sku": sku, "quantity": quantity,
                           "catalog": catalog, "inventory": inventory, "supplierQuotes": quotes})
        answer = self.model(f"Sourcing request and tool results:\n{data}")
        if not answer:
            answer = f"Could not complete the analysis. Tool results: {data[:800]}"
        return answer


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print("[agent] " + (fmt % args), flush=True)

    def reply(self, status, payload, extra=None):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/.well-known/agent"):
            proto = self.headers.get("X-Forwarded-Proto", "https")
            return self.reply(200, agent_card(f"{proto}://{self.headers.get('Host', 'localhost')}/"))
        if self.path.startswith("/healthz"):
            return self.reply(200, {"status": "ok"})
        return self.reply(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            request = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self.reply(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
        rpc_id = request.get("id")
        if BACKEND_SECRET and self.headers.get("x-agent-backend-secret") != BACKEND_SECRET:
            return self.reply(401, {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32001, "message": "Call this agent through the AI gateway"}})
        if request.get("method") not in ("message/send", "tasks/send"):
            return self.reply(200, {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32601, "message": f"Method not supported: {request.get('method')}"}})
        message = (request.get("params") or {}).get("message") or {}
        text = " ".join(p.get("text", "") for p in message.get("parts", []) if p.get("kind", p.get("type")) == "text").strip()
        started = time.time()
        task = Task({k.lower(): v for k, v in self.headers.items()})
        answer = task.run(text or "Source 100 units of SKU-1001")
        downstream = sum(step["costMicroUsd"] for step in task.steps)
        context_id = message.get("contextId") or str(uuid.uuid4())
        result = {
            "kind": "task", "id": str(uuid.uuid4()), "contextId": context_id,
            "status": {"state": "completed", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
            "artifacts": [{"artifactId": str(uuid.uuid4()), "name": "sourcing-recommendation", "parts": [{"kind": "text", "text": answer}]}],
            "metadata": {"steps": task.steps, "downstreamCostMicroUsd": downstream, "durationMs": int((time.time() - started) * 1000),
                         "billedTo": self.headers.get("x-gw-on-behalf-of"), "session": self.headers.get("x-gw-on-behalf-of-session")},
        }
        self.reply(200, {"jsonrpc": "2.0", "id": rpc_id, "result": result}, {"x-agent-downstream-cost-micro-usd": str(downstream)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"Sourcing Agent listening on :{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
