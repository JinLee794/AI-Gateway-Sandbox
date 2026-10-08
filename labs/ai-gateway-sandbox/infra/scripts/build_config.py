"""
azd preprovision hook: turns ../sandbox-config.json into infra/sandbox.generated.json, which infra/main.bicep loads.

Bicep has no floating-point math, so the price lists and micro-USD budgets are computed here, exactly like step 2
of the lab notebook. Standard library only.
"""
import json, os

INFRA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAB = os.path.dirname(INFRA)

with open(os.path.join(LAB, "sandbox-config.json"), encoding="utf-8") as f:
    config = json.load(f)

models, tiers, tools, a2a_agents = config["models"], config["tiers"], config["tools"], config["a2aAgents"]

lab_parameters = {
    "apimSku": config["apimSku"],
    "aiServicesConfig": config["foundryRegions"],
    "modelsConfig": [{k: v for k, v in m.items() if k not in ("inputPrice", "outputPrice")} for m in models],
    # USD per 1M input/output tokens, micro-USD per MCP tools/call and per A2A task
    "modelPricing": ";".join(f"{m['name']}={m['inputPrice']:.2f}/{m['outputPrice']:.2f}" for m in models),
    "toolPricing": ";".join(f"{t['name']}={int(round(t['priceUsd'] * 1_000_000))}" for t in tools),
    "agentPricing": ";".join(f"{a['name']}={int(round(a['feeUsd'] * 1_000_000))}" for a in a2a_agents),
    "agentLocation": config["agentLocation"],
    "tiersConfig": [{**{k: v for k, v in t.items() if k not in ("budgetUsd", "sessionBudgetUsd")},
                     "budgetMicroUsd": int(round(t["budgetUsd"] * 1_000_000)),
                     "sessionBudgetMicroUsd": int(round(t.get("sessionBudgetUsd", t["budgetUsd"]) * 1_000_000)),
                     "budgetPeriodSeconds": config["budgetPeriodSeconds"]} for t in tiers],
    "agentsConfig": config["agents"],
    "inferenceAPIPath": config["inferenceAPIPath"],
    "inferenceAPIType": config["inferenceAPIType"],
    "foundryProjectName": config["foundryProjectName"],
    "budgetSuspend": {k: v for k, v in config.get("budgetSuspend", {"enabled": False}).items() if not k.startswith("$")},
}

# Static part of the demo UI configuration (the endpoints and subscription keys are added by Bicep)
ui_config = {
    "tiers": tiers,
    "models": [{k: m[k] for k in ("name", "inputPrice", "outputPrice")} for m in models],
    "tools": tools,
    "a2aAgents": a2a_agents,
}

target = os.path.join(INFRA, "sandbox.generated.json")
with open(target, "w", encoding="utf-8") as f:
    json.dump({"labParameters": lab_parameters, "uiConfig": ui_config}, f, indent=2)
print(f"Wrote {os.path.relpath(target, LAB)} from sandbox-config.json")
