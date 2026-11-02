"""EXPERIMENTAL - set up mode B of the "Code mode vs tool calling" scenario: a Foundry Toolbox with tool search in front of
the gateway's MCP server.

    python infra/scripts/setup_toolbox.py [--approve-live] [--agent support-agent] [--toolbox agent-ready-apis]

Run it from labs/ai-gateway-sandbox after the sandbox is deployed (it reads src/demo-config.private.config). Idempotent.

  1. Finds the primary Foundry project and the application (client) ID of its managed identity.
  2. Creates (or updates) a RemoteTool project connection to the APIM MCP server: authType ProjectManagedIdentity with
     audience https://cognitiveservices.azure.com, the audience the entra-identity fragment validates. The plan is the
     agent's subscription key, sent as the subscription-key query parameter because a managed-identity connection carries
     no extra headers. Spike limitation: the key is stored in the connection target.
  3. Creates a new toolbox version with that MCP server plus {"type": "toolbox_search"}, so tools/list on the toolbox only
     exposes tool_search and call_tool, and makes it the default version.
  4. Records the client ID so the gateway approves the project as a caller:
       - azd env set TOOLBOX_CLIENT_ID <id> (applied by the next 'azd provision' / 'azd up' via the toolboxClientId parameter)
       - --approve-live also patches the deployed entra-identity policy fragment right away (no reprovision needed)
  5. Writes foundryProjectEndpoint and toolboxName into src/demo-config.private.config for the local UI.

Every call_tool then runs Foundry Toolbox -> API Management with the project's identity, so the gateway still applies the
plan's entitlements, pricing, limits and logs to each tool call. Needs the Azure CLI signed in as a project owner.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
LAB = os.path.dirname(os.path.dirname(HERE))
CONFIG_PATH = os.path.join(LAB, "src", "demo-config.private.config")
ARM = "https://management.azure.com"
GATEWAY_AUDIENCE = "https://cognitiveservices.azure.com"
CONNECTION = "apim-commerce-mcp"
AZ = shutil.which("az") or shutil.which("az.cmd")


def az(*args):
    result = subprocess.run([AZ, *args, "-o", "json"], capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise SystemExit(f"az {' '.join(args[:3])} failed: {result.stderr.strip()[:500]}")
    return json.loads(result.stdout) if result.stdout.strip() else None


def token(resource):
    return az("account", "get-access-token", "--resource", resource)["accessToken"]


def call(method, url, resource, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={"Authorization": f"Bearer {token(resource)}",
                                                                              "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            text = response.read().decode()
            return response.status, json.loads(text) if text else {}
    except urllib.error.HTTPError as error:
        return error.code, {"error": error.read().decode()[:800]}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--agent", default="support-agent", help="agent whose APIM subscription (plan) pays for the tool calls")
    parser.add_argument("--toolbox", default="agent-ready-apis")
    parser.add_argument("--approve-live", action="store_true", help="also patch the deployed entra-identity fragment now")
    args = parser.parse_args()
    if not AZ:
        raise SystemExit("Azure CLI not found")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = json.load(f)

    # 1. The primary Foundry account (priority 1) and its project
    apim_id = config["apimServiceId"]
    rg_id = apim_id.split("/providers/")[0]
    primary = sorted(config["foundryBackends"], key=lambda b: b.get("priority", 1))[0]
    account = urllib.parse.urlparse(primary["endpoint"]).hostname.split(".")[0]
    account_id = f"{rg_id}/providers/Microsoft.CognitiveServices/accounts/{account}"
    status, projects = call("GET", f"{ARM}{account_id}/projects?api-version=2025-06-01", ARM)
    if status != 200 or not projects.get("value"):
        raise SystemExit(f"No Foundry project on {account} ({status}): {projects}")
    project = projects["value"][0]
    project_id = project["id"]
    endpoint = project["properties"]["endpoints"]["AI Foundry API"].rstrip("/")
    client_id = az("ad", "sp", "show", "--id", project["identity"]["principalId"])["appId"]
    print(f"Project {project['name']}  endpoint {endpoint}\n  managed identity client ID {client_id}")

    # 2. RemoteTool connection: project managed identity -> APIM MCP server, paid by the agent's plan
    agent = next((a for a in config["agents"] if a["name"] == args.agent), None)
    if not agent:
        raise SystemExit(f"Unknown agent {args.agent}")
    target = f"{config['mcpUrl']}?subscription-key={agent['key']}"
    status, body = call("PUT", f"{ARM}{project_id}/connections/{CONNECTION}?api-version=2025-10-01-preview", ARM, {
        "properties": {"category": "RemoteTool", "authType": "ProjectManagedIdentity", "audience": GATEWAY_AUDIENCE,
                       "target": target, "isSharedToAll": True, "metadata": {"type": "custom_MCP", "audience": GATEWAY_AUDIENCE}}})
    if status not in (200, 201):
        raise SystemExit(f"Connection failed ({status}): {body}")
    print(f"Connection {CONNECTION} -> {config['mcpUrl']} (plan: {args.agent}, {agent.get('tier')})")

    # 3. Toolbox version: the APIM MCP server behind tool search
    status, body = call("POST", f"{endpoint}/toolboxes/{args.toolbox}/versions?api-version=v1", "https://ai.azure.com", {
        "description": "AI Gateway Sandbox - commerce MCP tools through API Management, behind tool search (experimental)",
        "tools": [{"type": "mcp", "server_label": "commerce", "server_url": config["mcpUrl"], "require_approval": "never",
                   "project_connection_id": CONNECTION},
                  {"type": "toolbox_search"}]})
    if status not in (200, 201):
        raise SystemExit(f"Toolbox version failed ({status}): {body}")
    version = body.get("version") or body.get("name") or "?"
    status, _ = call("PATCH", f"{endpoint}/toolboxes/{args.toolbox}?api-version=v1", "https://ai.azure.com", {"default_version": version})
    print(f"Toolbox {args.toolbox} version {version}{'' if status in (200, 201) else f' (default version not updated: {status})'}")

    # 4. Approve the project identity as a gateway caller
    if shutil.which("azd"):
        subprocess.run(["azd", "env", "set", "TOOLBOX_CLIENT_ID", client_id], cwd=LAB, capture_output=True)
        print("azd env TOOLBOX_CLIENT_ID set (applied by the next 'azd provision')")
    if args.approve_live:
        approve_live(apim_id, client_id)

    # 5. Local UI config (the hosted UI gets foundryProjectEndpoint from infra/main.bicep)
    config["foundryProjectEndpoint"], config["toolboxName"] = endpoint, args.toolbox
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    print(f"Toolbox MCP endpoint: {endpoint}/toolboxes/{args.toolbox}/mcp?api-version=v1")


def approve_live(apim_id, client_id):
    url = f"{ARM}{apim_id}/policyFragments/entra-identity?api-version=2024-06-01-preview"
    status, fragment = call("GET", url + "&format=rawxml", ARM)
    if status != 200:
        raise SystemExit(f"Could not read the entra-identity fragment ({status})")
    xml = fragment["properties"]["value"]
    if client_id in xml:
        print("entra-identity fragment already approves the project identity")
        return
    xml = xml.replace("</client-application-ids>", f"<application-id>{client_id}</application-id>\n\t\t</client-application-ids>", 1)
    xml = re.sub(r'(if \(appId == "[^"]+"\) \{ return "sourcing-agent \(managed identity\)"; \})',
                 lambda m: m.group(1) + f'\n\t\tif (appId == "{client_id}") {{ return "foundry-toolbox (project managed identity)"; }}', xml, count=1)
    status, body = call("PUT", url, ARM, {"properties": {"format": "rawxml", "value": xml,
                                                         "description": fragment["properties"].get("description", "")}})
    if status not in (200, 201, 202):
        raise SystemExit(f"Could not update the entra-identity fragment ({status}): {body}")
    print("entra-identity fragment now approves the project identity (live)")


if __name__ == "__main__":
    sys.exit(main())
