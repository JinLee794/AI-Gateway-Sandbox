---
name: AI Gateway Sandbox
architectureDiagram: images/ai-gateway.gif
categories:
  - Governance & Responsible AI
  - Platform Capabilities
services:
  - Azure OpenAI
  - Microsoft Foundry
  - Azure Monitor
shortDescription: "A hands-on sandbox for Azure API Management governing Microsoft Foundry models, MCP servers and A2A agents, covering identity, plans, routing, metering and evidence, with live monitoring and a demo UI."
detailedDescription: "A hands-on, customer-ready sandbox for Azure API Management as an AI Gateway for the three AI surfaces, Microsoft Foundry AI models, MCP servers and A2A agents. Gold, Silver and Bronze APIM products are the commercial plans. Each plan sets model allow lists (downgrade or deny), output caps, tokens-per-minute limits and token quotas, MCP tool and A2A agent entitlements and rate limits, and one $ budget that every model token, tool call and agent task draws from. Spend that an A2A agent makes on a caller's behalf is charged back to the caller. Every call also needs a Microsoft Entra ID token (validate-azure-ad-token), model calls are load balanced across two Foundry regions with a circuit breaker and retry, and the demo shows each policy in action through APIM request traces and the gateway, LLM and MCP log tables in Log Analytics. Cost metrics flow to Application Insights with Agent, Tier, Model, Surface and Via dimensions. Demo users (one subscription key each) work in sessions: a per-session $ cap stops runaway sessions, and chargeback records by user, team, cost center and session roll up the fully loaded cost of each piece of work. An Azure Monitor workbook shows the chargeback and the policy evidence, and a lightweight web UI drives guided scenarios live."
tags: [sandbox, finops, cost, budget, chargeback, mcp, a2a, products, llm-token-limit, quota-by-key, emit-metric, validate-azure-ad-token, entra-id, load-balancing, circuit-breaker, tracing, log-analytics]
authors:
  - jinle_microsoft
---

# APIM ❤️ Microsoft Foundry

## [AI Gateway Sandbox lab](ai-gateway-sandbox.ipynb)

[![flow](../../images/ai-gateway.gif)](ai-gateway-sandbox.ipynb)

A hands-on sandbox for Azure API Management as **one AI Gateway for the three AI surfaces**: **AI models** (Microsoft Foundry `gpt-4.1`, `gpt-4.1-mini`, `gpt-4.1-nano` and `DeepSeek-V3.2`), **MCP servers** (a REST API exposed as MCP tools) and **A2A agents** (a Sourcing Agent on Azure Container Apps). It covers **identity, plans, routing, metering and evidence** for all three in one place, with cost and tokens as one theme among them:

- **One plan, one contract.** Each APIM **product** (Gold, Silver, Bronze) is a commercial plan. It bundles which models, MCP tools and A2A agents a consumer may use, their limits, and **one $ budget**.
- **Every call is priced.** Model calls by tokens, MCP tool calls per call, A2A tasks by a fee plus everything the agent spends downstream.
- **Pricing beyond tokens.** Responses API calls pay the cheaper cached rate for input tokens served from the prompt cache. Reading a stored response is free, and follow-up calls stay on the region that stored the response. Optional image generation is priced per image by model, quality and size. All of it draws on the same plan and $ budget (see [Pricing beyond tokens](#pricing-beyond-tokens-responses-api-and-images)).
- **Spend follows the payer.** When an A2A agent calls models and tools for a caller, the gateway charges that spend back to the caller's budget and records the agent as `Via`.
- **One chargeback view.** Cost by consumer, plan, surface (model, tool, agent) and resource in Application Insights and an Azure Monitor workbook.
- **Chargeback by user and session.** Demo users (one subscription key each, no Entra ID accounts needed) work in sessions. A $ cap per session stops a runaway conversation or agent loop, and every priced call is recorded by user, team, cost center and session (see [FinOps chargeback by user and session](#finops-chargeback-by-user-and-session)).
- **Zero trust and resilience, with evidence.** Every call needs a Microsoft Entra ID token as well as the key. Model calls are load balanced across two Foundry regions with a circuit breaker, and every policy decision can be traced and found in the Azure Monitor logs (see [Policies in action](#policies-in-action-entra-id-load-balancing-and-what-the-logs-track)).

```mermaid
flowchart LR
    subgraph Consumers["Consumers (APIM subscriptions)"]
        A1["Customer Support Agent<br/>🥇 Gold"]
        A2["Research Agent<br/>🥈 Silver"]
        A3["Marketing Copilot<br/>🥉 Bronze"]
        U["Demo users<br/>Alice · Ben · Chloe · Dev<br/>+ x-session-id"]
    end
    subgraph APIM["Azure API Management - AI Gateway"]
        E["Entra ID fragment<br/>validate-azure-ad-token"]
        P1["Plan policy (APIM product)<br/>model allow list · output cap · TPM · token quota<br/>tool / agent entitlement + rate limit<br/>one $ budget · $ cap per session"]
        M["AI models API<br/>backend pool · retry · price tokens<br/>unpriced model → 409 model-unpriced"]
        T["MCP server<br/>commerce tools · price per call"]
        G["A2A agent API<br/>fee + downstream spend"]
    end
    A1 & A2 & A3 & U -->|"api-key + Entra ID token"| E --> P1
    P1 --> M --> F["Microsoft Foundry<br/>Sweden Central (priority 1)<br/>France Central (priority 2)"]
    P1 --> T --> R["Commerce REST API"]
    P1 --> G --> S["Sourcing Agent<br/>(Container Apps)"]
    S -->|"managed identity token<br/>on behalf of caller<br/>(agent-platform plan)"| E
    M & T & G -.->|"cost + tokens<br/>(Agent, Tier, Model, Surface, Via)<br/>+ chargeback records (user, team, session)"| AI["Application Insights<br/>+ Sandbox workbook"]
    APIM -.->|"gateway, LLM and MCP logs"| LA["Log Analytics"]
    RC["Weekly rate card (GitHub Actions)<br/>retail prices + overrides → PR → publish"] -.->|"model-pricing named value<br/>+ pricingVersion"| M
    UI["Demo UI"] --> A1 & A2 & A3 & U
    UI -.->|"KQL + request traces"| AI & LA
```

### What each plan includes

Each **consumer** has its own APIM subscription, which gives it an identity and a key. Each consumer belongs to a **plan** (an APIM product). The [plan policy](product-policy.xml) applies the plan to every surface:

| Control | Surface | Policy | Result when exceeded |
|---|---|---|---|
| 🧭 **Model governance**: which models a plan can use | models | `choose` + `set-body` | premium models are **downgraded** to a cheaper model, or **denied** (403) |
| ✂️ **Output cap**: maximum `max_tokens` per call | models | `set-body` | the request is capped transparently |
| ⏱️ **Tokens per minute** | models | [`llm-token-limit`](https://learn.microsoft.com/azure/api-management/llm-token-limit-policy) | 429 + `Retry-After` |
| 📦 **Token quota** per day/month | models | [`llm-token-limit`](https://learn.microsoft.com/azure/api-management/llm-token-limit-policy) | 403 |
| 🧰 **Tool entitlement + calls per minute** | MCP | `choose` + [`rate-limit-by-key`](https://learn.microsoft.com/azure/api-management/rate-limit-by-key-policy) | 403 `tool-denied` (JSON-RPC error) / 429 |
| 🤝 **Agent entitlement + tasks per minute** | A2A | `choose` + [`rate-limit-by-key`](https://learn.microsoft.com/azure/api-management/rate-limit-by-key-policy) | 403 `agent-denied` / 429 |
| 🖼️ **Image model and quality entitlement + images per minute** (optional) | images | `choose` + `rate-limit-by-key` | 403 `image-entitlement` / `image-quality` / 429 |
| 🔒 **Stored responses stay with their owner** | Responses API | APIM cache: response id → backend + subscription | 404 `response-ownership` |
| 💵 **One $ budget** for models, tools and agents | all | [`quota-by-key`](https://learn.microsoft.com/azure/api-management/quota-by-key-policy) incremented by each call's cost | 403 |
| 🧾 **$ cap per session** | all | a second `quota-by-key` keyed on subscription + session | 403 `session-budget` |
| 🛡️ **Blocked before spend**: content safety (optional, off by default; Gold, Silver, Bronze when on) | all | [`llm-content-safety`](https://learn.microsoft.com/azure/api-management/llm-content-safety-policy) with Prompt Shields and a sensitive-data blocklist, in the [blocked-before-spend fragment](content-safety-fragment.xml) | 403 `llm-content-safety`, $0 of model tokens; the check itself is billed to the $ budget |
| 🔏 **Purview DLP** (optional, off by default) | all | Microsoft Graph `processContent` in the [purview-dlp fragment](purview-dlp-fragment.xml) | 403 `purview-dlp` |
| ⛔ **Over budget, switched off** (operations loop, optional) | all | Gateway subscription-budget error → direct callback → Logic App with a managed identity ([budget-suspend.bicep](budget-suspend.bicep)) | the APIM **subscription is suspended** asynchronously: its key is rejected on every API and a `BudgetBreach` audit event is written. No periodic spend scans. See [Over budget, switched off](#over-budget-switched-off) |

| Consumer | Plan | Models | Disallowed models | TPM | Token quota | Max output | MCP tools | A2A agents | $ budget | $ per session |
|---|---|---|---|---|---|---|---|---|---|---|
| Customer Support Agent | 🥇 Gold | all four | n/a | 20,000 | 1M / month | 2,000 | all three, 60/min | Sourcing Agent, 10/min | $5.00 | $0.02 |
| Research Agent | 🥈 Silver | gpt-4.1-mini, gpt-4.1-nano, DeepSeek-V3.2 | downgraded to gpt-4.1-mini | 12,000 | 200K / month | 800 | `search-products`, `check-inventory`, 30/min | Sourcing Agent, 3/min | $0.01 | $0.01 |
| Marketing Copilot | 🥉 Bronze | gpt-4.1-nano | denied (403) | 1,500 | 20K / day | 300 | `search-products`, 10/min | none | $0.02 | $0.01 |

Image generation (optional, `images.enabled`): Gold may generate `low`, `medium` and `high` quality images, 5 per minute. Silver may generate `low` and `medium`, 2 per minute. Bronze may not generate images.

Demo price list (the `model-pricing`, `tool-pricing` and `agent-pricing` named values):

**Optional weekly model pricing (phase 1):** the [price-list lifecycle](pricing/README.md)
discovers deployed token models, resolves exact retail meters or reviewed internal
overrides/PTU allocations, and opens a weekly price-card PR with a readable diff and
version history. After review/merge, a separately approval-gated OIDC workflow updates
only `model-pricing` (including cached rates and `pricingVersion`), with coverage and
4,096-character checks. Configure the Azure identities/account allow list and provision
the updated policy files once before enabling publication. Flat tool, agent, image and
content-safety prices are unchanged; streaming responses remain a known unpriced path.
Cost Management reconciliation is **phase 2**, not implemented here.

| Resource | Surface | Price |
|---|---|---|
| `search-products` | MCP tool | $0.0005 per call |
| `check-inventory` | MCP tool | $0.0002 per call |
| `get-supplier-quote` (premium data) | MCP tool | $0.0020 per call |
| `sourcing-agent` | A2A agent | $0.0050 per task + its downstream model and tool spend |
| `content-safety` (screening) | safety check | $0.38 per 1K text records (1 record = up to 1,000 characters; Prompt Shields adds a second record per call), from `contentSafety.pricePer1KRecordsUsd` |
| Foundry models | AI model | per 1M input/output tokens, see the notebook |
| Cached input tokens (Responses API) | AI model | `gpt-4.1` $0.50, `gpt-4.1-mini` $0.10, `gpt-4.1-nano` $0.025 per 1M (`cached-input-pricing`); other models pay the full input price |
| Stored response read (`GET /responses/{id}`) | AI model | free |
| `gpt-image-1-mini` (optional) | Image | per image (`image-pricing`): `low` $0.005–0.006, `medium` $0.011–0.015, `high` $0.036–0.052, by size (1024x1024, 1024x1536, 1536x1024) |

### How it works

- **AI models**: the [models API policy](policy.xml) prices every call from the token usage and returns `x-gw-*` headers (served model, governance actions, tokens, cost, remaining tokens per minute).
- **Streamed model calls** (`"stream": true`) are charged at their **ceiling** (`x-gw-pricing-rule: stream-ceiling`). The usage block arrives at the end of a Server-Sent Events stream, and reading it would make the gateway buffer the whole stream. So the gateway charges the estimated prompt tokens (text length / 4, 1,200 tokens per image) plus the plan's full `max_tokens`. No cached-input discount applies. Before this ceiling charge, a streamed call cost $0 against the $ budget. The charge errs high, so send `"stream": false` when you need the exact price. The `x-gw-prompt-tokens` and `x-gw-completion-tokens` headers and the chargeback record carry the estimate. To reconcile, compare chargeback records with `pricingRule = stream-ceiling` against the actual streamed token counts in `ApiManagementGatewayLlmLog` (`IsStreamCompletion`).
- **MCP server**: APIM exposes the [commerce REST API](src/tools/openapi.json) as the `commerce-mcp` **MCP server**. Every MCP tool maps to an API operation, so the [MCP policy](src/tools/mcp-policy.xml) can price each `tools/call` and the plan policy can entitle and rate-limit each tool.
- **A2A agents**: APIM fronts the [Sourcing Agent](src/agent/agent.py) as an **A2A agent API**. Both the agent card and `message/send` tasks need a subscription key, so discovery is governed too, and tasks need a plan that includes the agent. The agent runs on Container Apps and only accepts calls that carry a gateway secret, so it can't be called around the gateway.
- **Chargeback**: the Sourcing Agent calls models and tools through APIM with its own `agent-platform` subscription and sends `x-gw-on-behalf-of: <caller>`. The [attribution fragment](attribution-fragment.xml) trusts that header only from the `agent-platform` plan, so the spend is drawn from the **caller's** $ budget and tagged `Via = sourcing-agent`. The [A2A policy](src/agent/a2a-policy.xml) adds the agent fee and returns the full bill (`x-gw-agent-fee-usd`, `x-gw-downstream-cost-usd`, `x-gw-cost-usd`, `x-gw-billed-to`).
- **Metrics**: every surface emits `CostMicroUSD` (plus `Prompt Tokens`, `Completion Tokens`, `Total Tokens` for models, and `GovernanceEvents`) to Application Insights with the `Agent`, `Tier`, `Model` (the model, tool or agent), `Surface` and `Via` dimensions.

> [!NOTE]
> The prices are **demo prices**. Check the [Azure OpenAI](https://azure.microsoft.com/pricing/details/cognitive-services/openai-service/) and Foundry Models pricing for current prices in your region. The Silver and Bronze budgets are deliberately tiny so you can exhaust them live: one Sourcing Agent task (about $0.009) nearly uses up Silver's $0.01. Every counter key includes the `budget-epoch` named value, so changing it resets all budgets and quotas: use the notebook reset step or the UI **Reset budgets** button before each demo. The UI's reset waits until Azure reports the new named value as provisioned (up to 90 s), so the scenarios that reset first don't race the old counters.

In the Azure portal, open the APIM instance to see the gateway's own views of the three surfaces: **APIs → AI models**, **APIs → MCP servers** and **APIs → A2A agents** (preview), and **Products** for the plans.

### Blocked before spend: content safety and Purview DLP

A guardrail that runs *after* the model has answered has already spent the tokens. The [blocked-before-spend fragment](content-safety-fragment.xml) runs **last in the plan policy's inbound**, after identity, entitlements, limits and the $ budget check, and before any backend is called:

- **What is screened.** The text the caller sends, on every surface listed in `contentSafety.surfaces`: model `messages` / `input`, the MCP `tools/call` **arguments**, and the A2A `message/send` **task text**. A blocked tool call never reaches the MCP server, and a blocked task never reaches the Sourcing Agent or spends anything downstream.
- **How.** [`llm-content-safety`](https://learn.microsoft.com/azure/api-management/llm-content-safety-policy) calls an [Azure AI Content Safety](https://learn.microsoft.com/azure/ai-services/content-safety/overview) account ([content-safety.bicep](content-safety.bicep), managed identity, local auth disabled) with **Prompt Shields** (jailbreaks), the four harm categories (threshold `contentSafety.thresholds`, eight severity levels) and a **sensitive-data blocklist** (`contentSafety.blocklist`: US SSN and card number regexes). Edit those in [sandbox-config.json](sandbox-config.json) (or the notebook's first cell).
- **Which plans.** `"contentSafety": true` on a plan in `sandbox-config.json`. Gold, Silver and Bronze have it; the internal `agent-platform` plan doesn't, because the Sourcing Agent's downstream calls carry a task that was already screened when it came in.
- **What the caller sees.** **403** with `x-gw-blocked-by: llm-content-safety`, `x-gw-governance: content-blocked`, `x-gw-prompt-tokens: 0`, `x-gw-cost-usd: 0` and `x-gw-safety-cost-usd`. Every screened call, passed or blocked, returns `x-gw-safety-cost-usd`.
- **Tokenomics.** Blocked calls spend **$0 of model tokens**. The screening itself is not free: the [safety-ledger fragment](content-safety-ledger-fragment.xml) prices each check from the text length (records × `pricePer1KRecordsUsd`), draws it from the plan's $ budget, and writes it as its own line item: a chargeback record with `surface = safety`, `item = content-safety`, `blocked`, `checkedSurface` and `checkedItem`, plus a `CostMicroUSD` metric with `Surface = safety`. A block also emits `GovernanceEvents` with `Event = content-blocked`. A short call costs 2 records, about **$0.00076**.
- **Evidence.** `ApiManagementGatewayLogs` rows with `LastErrorReason == "ContentSafetyPolicyViolated"` and no backend call (Prompt Shields and harm blocks log `LastErrorSource = llm-content-safety`, blocklist matches `request-forwarder`; the `x-gw-blocked-by` header is `llm-content-safety` for both), the `content-blocked` events, and the `safety` chargeback records. The workbook's **Budgets & governance** and **Gateway evidence** tabs show them, and so do the UI's **Safety checks** ledger column and **Policies & evidence** tab.

**It is off by default.** To turn it on, set `"contentSafety": {"enabled": true}` in [sandbox-config.json](sandbox-config.json) (or `content_safety_config["enabled"] = True` in the notebook's first cell) and redeploy (`azd up` or the notebook's deployment step). The plans already opt in (`"contentSafety": true` on Gold, Silver and Bronze), so the lab-level flag is all it takes. It deploys one Content Safety account (S0, pay per use). Every screened call then costs about **$0.0008 more**, and that comes out of the plan's $ budget. Silver's budget is deliberately tiny ($0.01), so with content safety on, scenarios 5 and 8 reach Silver's 403 a call or two sooner. When it's off, the fragments are deployed as no-ops, no Content Safety account is created, scenario 15 only says how to turn it on, and scenarios 1–14 behave exactly as before.

> [!NOTE]
> The [Content Safety price](https://azure.microsoft.com/pricing/details/cognitive-services/content-safety/) is a list price for the standard tier at the time of writing [verify for your region and agreement]. Content Safety blocklists can take a few minutes to take effect after a deployment. If the sensitive-data calls pass right after `azd up`, run the scenario again.

#### Purview DLP (optional, off by default)

The [purview-dlp fragment](purview-dlp-fragment.xml) and [purview-dlp.bicep](purview-dlp.bicep) follow the [Purview DLP lab](../apim-purview-dlp/README.md): the gateway exchanges the caller's **user** token (sent as `x-user-assertion`) on behalf of the user and calls Microsoft Graph [`processContent`](https://learn.microsoft.com/graph/api/userprotectionscopecontainer-processcontent). If a Purview DLP policy for custom AI apps says *block*, the call returns **403** `x-gw-blocked-by: purview-dlp` and emits a `dlp-blocked` governance event. An unreadable verdict also blocks (fail closed). Calls without `x-user-assertion` are not evaluated (`x-gw-purview: skipped-no-user`), or get 403 when `purviewDlp.requireUser` is true. A Purview block returns directly from inbound, so its content safety check is not written to the safety ledger. It is **off by default** (`purviewDlp.enabled: false`) because it needs things a demo tenant usually doesn't have:

- real **Entra ID users** (not the subscription-key demo users), each with a Microsoft 365 E5 or E5 Compliance license, sending a delegated user token
- an app registration with the Graph delegated permissions `Content.Process.User`, `ProtectionScopes.Compute.User` and `ContentActivity.Write`, admin consent, and a **client secret** (`PURVIEW_CLIENT_SECRET` for `azd`, or the environment variable of the same name for the notebook)
- a Purview DLP policy scoped to custom AI apps, Purview pay-as-you-go billing and Audit turned on, and up to about an hour of policy propagation

Content safety's sensitive-data blocklist covers the demo story without any of that. Turn Purview on with `purviewDlp.enabled: true` and `purviewDlp.clientId` once the tenant is ready. Only the Bicep build is validated for this path.

In the Azure portal, the content safety account and its blocklist are under **Azure AI services → Content Safety**, and the two fragments are under **APIM → Policy fragments**.

### FinOps chargeback by user and session

The plans answer *how much can this subscription spend*. FinOps also needs *who* spent it and *on what piece of work*, so it can be charged back to a team and cost center.

| Demo user | Subscription | Plan | Team | Cost center |
|---|---|---|---|---|
| Alice Chen | `user-alice` | 🥇 Gold | Customer Care | CC-1001 |
| Ben Okafor | `user-ben` | 🥈 Silver | Customer Care | CC-1001 |
| Chloe Martin | `user-chloe` | 🥉 Bronze | Marketing | CC-3003 |
| Dev Patel | `user-dev` | 🥇 Gold | Supply Chain | CC-2002 |

- **Users are subscription keys.** Each demo user has their own APIM subscription on a plan, so the demo needs no Entra ID accounts. The `chargeback-directory` named value maps every subscription (users and agents) to its display name, team and cost center.
- **Sessions come from the request.** The [plan policy](product-policy.xml) takes the session from `x-session-id`, then the MCP `Mcp-Session-Id`, then the A2A `contextId`. Values must match `^[A-Za-z0-9._:-]{1,128}$`, otherwise the call counts as `no-session`. The session is returned in `x-gw-session`. When the Sourcing Agent works for a user, it forwards the session (`x-gw-on-behalf-of-session`, trusted only from the `agent-platform` plan), so the agent's model and tool calls land in the **same user and session**.
- **A $ cap per session.** A second `quota-by-key`, keyed on subscription + session, stops one runaway conversation or agent loop (`403`, `x-gw-blocked-by: quota-by-key (session-budget)`) without touching the user's plan budget. A new session starts fresh. The cap is a soft limit: quota counters are updated asynchronously after each successful call, so a fast loop can run a few calls past it (in testing, $0.024–$0.029 against a $0.02 cap). Use it to contain runaway loops, not as an exact invoice limit.
- **Chargeback records.** The [`chargeback-record` fragment](chargeback-fragment.xml), included by the models, MCP and A2A policies, writes one `trace` per priced call to Application Insights (`traces`, or `AppTraces` in Log Analytics) with `record = chargeback` and the user, display name, team, cost center, kind (user or agent), session, surface, item, tokens, plan, `via`, Entra ID caller and request id.
- **No double counting.** Sum `costMicroUsd`. An A2A record carries only the agent fee; the agent's model and tool calls are separate records (`via = sourcing-agent`) for the same user and session. So a session total is the **fully loaded cost** of that piece of work. `chargedMicroUsd` is what the call drew from the budgets (for an A2A task, the fee plus the downstream spend).
- **Why traces, not metric dimensions?** `emit-metric` allows at most 5 custom dimensions, and user and session are high-cardinality. The cost metrics keep the 5 low-cardinality dimensions (Agent, Tier, Model, Surface, Via); the records carry everything else.

```kusto
traces
| where tostring(customDimensions["record"]) == "chargeback"
| extend Team = tostring(customDimensions["team"]), User = tostring(customDimensions["displayName"]),
         Session = tostring(customDimensions["session"]), Cost = tolong(customDimensions["costMicroUsd"])
| summarize CostUSD = round(sum(Cost) / 1e6, 6), Calls = count() by Team, User, Session
| order by CostUSD desc
```

> [!TIP]
> In production, take the user from the Entra ID token (the `oid` or `preferred_username` claim that `validate-azure-ad-token` already validates) and the team and cost center from a directory attribute, instead of one subscription key per user. The session cap, the records and the queries stay the same.

### Per-user $ budgets inside one app

One subscription key per user doesn't scale when an app serves thousands of people. Instead, the app keeps **one key on one product** and names the person it is acting for in the `x-gw-end-user` header. The [plan policy](product-policy.xml) gives each end user their own $ counter inside that key:

- **Budgets come from config, not policy.** The `end-user-budgets` named value (rendered from `endUserBudgets` in [sandbox-config.json](sandbox-config.json)) holds budget classes, a default class for anyone not listed, and per-user overrides: `default=standard;standard=5000;power=15000;exec=50000;@raj.iyer=power;@tom.intern=2000` (amounts in micro-USD). Changing a budget is a named value update, with no policy redeploy.
- **One policy for any amount.** `quota-by-key` only accepts a literal `calls` limit, so the counter is keyed on subscription + user + budget epoch with `calls="1000000"`, and each call adds its cost in **millionths of that user's own budget** (`ceil(cost × 10⁶ / budget)`). A $0.005 user and a $50 user share the same policy.
- **Budgets nest.** End user < app (plan $ budget) < per-session cap. A call must fit every level; the app's own calls (no header) only draw on the plan budget. Blocked calls return 403 with `x-gw-blocked-by: quota-by-key (end-user-budget)`; every response carries `x-gw-end-user` (user and class) and `x-gw-end-user-budget-usd`, and chargeback records carry `endUser`.
- **Validation.** Values must match `^[a-z0-9._@-]{1,64}$` (lower-cased, so `Raj.Iyer` and `raj.iyer` share one budget); anything else gets 400 `end-user-header`. The header is removed before the call reaches Foundry, MCP or the agent. **Reset budgets** resets end-user budgets too.

Limits: the header is asserted by the app, so it is only as trusted as the app key. In production, take the user from the validated Entra ID token (`oid`) instead, the class from an app role or group, and overrides from a budget service via `send-request` + `cache-lookup-value` (a named value tops out around 4,096 characters). Calls the Sourcing Agent makes on a user's behalf are charged to the A2A task, not to the end user's counter. Like every quota here, the cap is soft and counters are per region.

### Pricing beyond tokens: Responses API and images

The same plan and $ budget also cover the **Responses API** and **image generation**:

| Surface | Priced by | Named value | Plan controls | Response headers |
|---|---|---|---|---|
| Responses API (`POST /responses`, also with `previous_response_id`) | tokens. Input tokens served from the prompt cache use the model's cached rate | `model-pricing` + `cached-input-pricing` | same as chat completions: models, output cap, TPM, token quota, $ budget, $ per session | `x-gw-cached-tokens`, `x-gw-cached-savings-usd`, `x-gw-pricing-rule` (`tokens` / `tokens+cached`, or `stream-ceiling` for `"stream": true`), `x-gw-affinity` |
| Stored response (`GET /responses/{id}`, input items, `DELETE`) | free (`stored-read`) | | only the subscription that created the response can use it | `x-gw-pricing-rule: stored-read`, `x-gw-affinity` |
| Image generation (`POST /images/generations`, optional) | **per image** by model, quality and size | `image-pricing` | image models and qualities per plan, images per minute, $ budget, $ per session | `x-gw-image-count`, `x-gw-image-unit-usd`, `x-gw-image-spec`, `x-gw-remaining-images` |

- **Cached input.** When a prompt over 1,024 tokens repeats a recent prefix, Foundry serves part of it from the prompt cache and reports `usage.input_tokens_details.cached_tokens`. The [`responses-pricing` fragment](responses-pricing-fragment.xml), included by the models API policy, prices those tokens at `cached-input-pricing` instead of the full input price. The cost metric, the chargeback record (`cachedTokens`) and the $ budget all use the lower price. Prompt caching is best effort, so a cold cache reports 0 cached tokens.
- **Session affinity for stateful calls.** A stored response lives in the Foundry resource that created it, but model calls are load balanced across two regions. The [Responses operation policy](responses-policy.xml) ([responses-ops.bicep](responses-ops.bicep)) records which backend and subscription created each response id in the APIM cache (30 days). Follow-up calls, reads and deletes then go to that backend (`x-gw-affinity: sticky:foundry1`). A call that names another subscription's response id gets 404 `response-ownership`. The built-in cache is volatile: after a cache miss (`x-gw-affinity: unknown`) the backend pool routes the call, so treat affinity and ownership as best effort, not as a security boundary.
- **Images** are an optional API ([image-api.bicep](image-api.bicep), [image policy](image-policy.xml)) at `{gateway}/images/openai/v1/images/generations`. It uses the same plans, keys, Entra ID token and managed identity as the models API. The plan policy checks the image model and quality (403 `image-entitlement` / `image-quality`), the images per minute (429), and the shared $ budget and session cap. The image policy refuses a model, quality and size combination with no price (400) and more than `maxImagesPerRequest` images. It prices the images the backend actually returned, then emits `CostMicroUSD` with `Surface = image` and a chargeback record with `imageCount`.
- **Enable images.** Images are off by default, so the base demo needs no extra model quota. To turn them on, set `"enabled": true` under `images` in [sandbox-config.json](sandbox-config.json) (for `azd up`), or set `images_config["enabled"] = True` in the notebook's first cell, then deploy again. `gpt-image-1-mini` (GlobalStandard, capacity 2) is deployed in the **priority-1 region only**, because gpt-image models are not offered in France Central, and the image API routes there directly. The image prices are illustrative, so check the [Azure OpenAI pricing](https://azure.microsoft.com/pricing/details/cognitive-services/openai-service/) for your region.
- **Realtime audio (not yet).** Realtime traffic runs over a WebSocket, and APIM runs WebSocket API policies only at the handshake (`onHandshake`), not per message. Audio tokens can't be priced or limited per message at the gateway. A reduced design would apply the plan entitlement, a connection rate limit and a budget pre-check at the handshake, then price the session afterwards from `ApiManagementGatewayLlmLog` (audio vs text tokens) with KQL. This is tracked on the roadmap in the [root README](../../README.md).

**Observed in an isolated Sweden Central test:** a Responses follow-up stayed on its owning backend and used 2,944 cached input tokens, saving $0.000883 at the demo rates. A stored read was free; a different subscription received 404 `response-ownership`. A Gold low-quality 1024x1024 image returned one image and cost $0.005; Silver's high-quality request was denied. The charges appeared in Application Insights.

**Validation limits:** Bronze image denial, the live UI scenario cards, workbook rendering, and stored-read token-quota behavior were not verified in that Azure round. The combined safety, suspension and pricing configuration has local integration checks, but has not had a combined Azure deployment test. Per-image amounts are configured demo charges, not a reconciliation of Azure's infrastructure and model invoice.

### Policies in action: Entra ID, load balancing and what the logs track

The plan policies above decide *what a consumer may spend*. These policies decide *who may call* and *how the call is served*:

| Policy | Where | What it does | Evidence |
|---|---|---|---|
| 🔐 [`validate-azure-ad-token`](https://learn.microsoft.com/azure/api-management/validate-azure-ad-token-policy) | [entra-identity fragment](entra-identity-fragment.xml), included first by every plan | Every model, MCP and A2A call needs a Microsoft Entra ID token for `https://cognitiveservices.azure.com` from an approved client app (Azure CLI and the Sourcing Agent's managed identity), **on top of** the key. The key says *who pays*, the token says *who calls*. No token returns **401** before any tokens are spent. The token is removed before forwarding. | `x-gw-blocked-by: validate-azure-ad-token`, `x-gw-caller`. `ApiManagementGatewayLogs.LastErrorSource`. `caller` field of the chargeback records. |
| 🪪 Managed identity end to end | Sourcing Agent → gateway → Foundry | The agent calls the gateway with its **user-assigned managed identity** token. The gateway calls Foundry with **its own** managed identity (`authentication-managed-identity`). There are no Foundry keys anywhere. | Trace: `authentication-managed-identity`. Caller `sourcing-agent (managed identity)`. |
| ⚖️ [Backend pool](https://learn.microsoft.com/azure/api-management/backends#load-balanced-pool) + [circuit breaker](https://learn.microsoft.com/azure/api-management/backends#circuit-breaker) | `inference-backend-pool`: `foundry1` (Sweden Central, priority 1) and `foundry2` (France Central, priority 2) | Priority routing with failover. A 429 from a region trips that backend's breaker for 1 minute (honouring `Retry-After`), so the next calls go straight to the other region. | `x-gw-route` (for example `foundry1 (Sweden Central) 429 > foundry2 (France Central) 200`), `x-gw-backend`. `BackendAttempts` metric. Gateway log `BackendUrl`. |
| 🔁 [`retry`](https://learn.microsoft.com/azure/api-management/retry-policy) | [models API policy](policy.xml), `<backend>` | Retries a 429/5xx call (up to twice) on the next available pool member, so the caller sees 200 instead of the regional throttle. | Trace: `retry`, `backend-pool` (`backend:foundry1 is inactive`). |
| 🧾 [`trace`](https://learn.microsoft.com/azure/api-management/trace-policy) + request tracing | all APIs | Writes the policy decisions (caller, failover) to the request trace, App Insights `traces` and gateway log `TraceRecords`. | The **Trace policies** toggle in the UI. |

> [!NOTE]
> To make failover easy to show, the lab deploys `gpt-4.1-nano` in Sweden Central with **capacity 1** (about 1K tokens per minute), so a short burst hits a real regional 429. While that backend's breaker is open, *every* model call is served from France Central for about a minute.

#### What each log tracks

| Where | Table / signal | What it tracks | Join key |
|---|---|---|---|
| Log Analytics | `ApiManagementGatewayLogs` | Every request: API, operation, product (plan), subscription, backend URL, status and backend status codes, timings, region, client IP, and **the policy that failed** (`LastErrorSource`, `LastErrorReason`, `LastErrorMessage`) | `CorrelationId` = `x-gw-request-id` |
| Log Analytics | `ApiManagementGatewayLlmLog` | Every AI model call: deployment, model, prompt/completion/total tokens counted by the gateway, streaming | `CorrelationId` |
| Log Analytics | `ApiManagementGatewayMCPLog` | Every MCP message: client, server, JSON-RPC method, tool name, session, authentication method and errors | `CorrelationId` |
| Application Insights | `requests`, `dependencies`, `traces` | Gateway requests and backend calls with end-to-end correlation, plus the `trace` policy records and the **chargeback records** (user, team, cost center, session, cost) | `operation_Id`, `requestId` |
| Application Insights | `customMetrics` | `CostMicroUSD`, tokens, `GovernanceEvents` and `BackendAttempts`, with `Agent`, `Tier`, `Model`, `Surface`, `Via` (and `Backend`, `Region`, `Failover`) dimensions | dimensions |
| APIM request trace | `listTrace` (debug credentials) | The full policy-by-policy execution of one request: inbound, backend (pool choice, retry) and outbound | `apim-trace-id` |

Every gateway response returns `x-gw-request-id`, so any call in the UI can be looked up in Log Analytics. Diagnostic logs reach Log Analytics **2 to 5 minutes** after the call. To find the same evidence in the Azure portal:

- **APIM → APIs → (API) → Test** with **Trace** enabled shows the same policy-by-policy trace.
- **APIM → Logs** (or the Log Analytics workspace → Logs): run, for example, `ApiManagementGatewayLogs | where CorrelationId == "<x-gw-request-id>"`. Each table and query in the UI has an **Open in Log Analytics** link that opens it in the portal.
- **APIM → Backends** shows the pool, priorities and circuit breaker rules. **APIM → APIs → (API) → Policies** and **Policy fragments** show the deployed XML.
- The workbook's **Gateway evidence** tab shows outcomes by policy (select one to list the calls it stopped), latency by API, backend calls by region, failovers, tokens by deployment and MCP tool calls.

### Over budget, switched off

`quota-by-key` is the **real-time** stop: the gateway answers 403 on the next call once a consumer's budget is spent. This optional loop, taken from the [FinOps framework lab](../finops-framework/), adds the **operations** side on top of it. It is deployed by [budget-suspend.bicep](budget-suspend.bicep) when `budgetSuspend.enabled` is `true` in [sandbox-config.json](sandbox-config.json) (or `budget_suspend_config` in the notebook's first cell). **It is off by default**, so the standard demo deploys without it. To turn it on, set `"enabled": true` in `sandbox-config.json` (for `azd up`) or `"enabled": True` in `budget_suspend_config` (for the notebook), then deploy again. The default watches `user-ben` (Silver, $0.01).

1. Successful calls increment the shared subscription budget, including any content-safety cost. Chargeback records remain available for reporting, but are not used to trigger suspension.
2. The first **subsequent request blocked by the subscription-budget quota** enters the product's `on-error` policy. For watched subscriptions only, the gateway sends `{subscription, epoch, requestId, source, reason, stage}` directly to `la-budget-suspend-<suffix>`. A session cap, token quota, rate limit or entitlement denial does not trigger it.
3. The callback URL is held in the **secret** `budget-suspend-callback` APIM named value, not exported to the UI. The gateway waits at most 5 seconds for HTTP 202, then suppresses duplicate notifications for 30 seconds using its internal cache. This cache is an optimization, not an atomic lock; workflow runs are serialized and duplicate notifications are harmless. A failed notification is traced at error severity, returns `x-gw-suspension-status: failed`, and is not cached. Accepted notifications return `accepted` or `recently-accepted`.
4. The Logic App validates the watched subscription and error contract, checks the current epoch and subscription state, and **suspends the APIM subscription** (`state: suspended`) with a conditional ETag PATCH. Old-epoch notifications and already inactive subscriptions are skipped. It writes a `BudgetBreach` audit event with action, reason, subscription, epoch, previous state, originating request id and run id. APIM does not retain `stateComment` on suspended subscriptions; the event is the record of why.

A suspended key is rejected on **every** API (models, MCP tools, A2A agents) with **HTTP 401** (`Access denied due to invalid subscription key`) once the state propagates to the gateway. **Reset budgets** sets a new epoch first, then reactivates watched subscriptions. Queued notifications for old epochs are skipped. Epoch and subscription updates are separate ARM operations, not an atomic transaction; a reset racing an already executing suspension may require another reset.

| | `quota-by-key` (in the gateway) | Gateway callback + Logic App |
|---|---|---|
| Latency | the next call | Workflow starts on that blocked call; ARM update and gateway propagation are asynchronous |
| Scope | the plan's budget counter for this API call | the APIM subscription: its key stops working on every API |
| Thresholds | budget per period, per session | Only exhaustion of the watched subscription budget; not custom percentages or external Azure billing |
| Side effects | 403 to the caller | audit event and subscription state change until someone reactivates it |

The UI polls ARM state every 5 seconds for up to 3 minutes, with **no Log Analytics queries while waiting**. It fetches reporting evidence once after suspension. Audit events and `WorkflowRuntime` diagnostics can arrive later; an empty evidence table does not imply the subscription is still active.

**Upgrading an existing deployment:** redeployment keeps `alert-budget-breach-<suffix>` and `ag-budget-suspend-<suffix>` as disabled migration resources with no actions/receivers. Incremental Bicep deployments do not delete removed resources, so simply removing them would leave the old scan running. The new mechanism has no active scheduled alert. Remove old `suspendAtPercent` and `notifyEmails` settings from your config; for compatibility only `100` and `[]` are accepted. Custom warning thresholds and email notifications require a separate integration and are not silently carried forward. The notebook-generated private UI configuration must be regenerated and the hosted UI republished after deployment.

**Least privilege.** The Logic App's system-assigned managed identity gets a custom role, *AI Gateway Sandbox budget suspender*, on the APIM instance only. The role allows `Microsoft.ApiManagement/service/read`, `service/subscriptions/read`, `service/subscriptions/write` and `service/namedValues/read`. Set `useCustomRole` to `false` if you can't create custom roles; the identity then gets the built-in *API Management Service Contributor* role, still scoped to the APIM instance. **Permissions to deploy it:** creating the custom role definition needs `Microsoft.Authorization/roleDefinitions/write` at subscription scope, which comes with *Owner* or *User Access Administrator* (the lab's usual *Role Based Access Control Administrator* is not enough). With `useCustomRole: false`, the lab's usual Contributor + Role Based Access Control Administrator roles are enough. The audit event uses the Application Insights ingestion endpoint (no role needed). The role definition lives at the subscription level, so the clean-up notebook and the `azd down` hook delete it after the resource group.

> [!NOTE]
> The gateway quota remains the spend control; suspension is an asynchronous administrative action. No further request means no exhaustion notification. HTTP 202 means accepted, not completed: the Logic App retries transient HTTP failures, and a later blocked request can notify it again after the 30-second cache expires. Inspect failed runs and `BudgetBreach` events for persistent failures. Turning the feature off removes gateway callbacks on redeployment, but incremental deployment does not delete its previously created workflow or migration resources.

### Monitoring

- The **AI Gateway Sandbox** Azure Monitor workbook, deployed with the lab, has:
  - **filters** for time range, plan, consumer and surface (AI models, MCP tools, A2A agents), which apply to every tab
  - a **KPI strip** with a trend sparkline per KPI: spend, cost per 1K calls, tokens, gateway calls, blocked %, plan enforcements and regional failovers. Click a KPI to open the tab that explains it.
  - four tabs, each with a drill-down, plus an **About** tab that lists the data sources:
    - **Overview**: spend over time and by surface, top consumers and top resources. Select a consumer to see its resources, direct vs via-agent spend, line items with the Entra ID caller, and its plan enforcement events.
    - **Chargeback**: cost by team and cost center and by user or app, plus a Team › User › Session tree. Select a session to see its cumulative spend and a call-by-call ledger.
    - **Budgets & governance**: budget burn per consumer, enforcement events and gateway outcomes by plan, blocked calls over time, who hit which limit, and content safety checks, blocks and what the screening cost. With [Over budget, switched off](#over-budget-switched-off) deployed, it also shows the `BudgetBreach` audit trail, spend per budget epoch, and Logic App runs.
    - **Gateway evidence**: outcomes by policy (select one to list the calls it stopped), latency by API, backend calls by region, failovers, tokens by deployment, MCP tool calls, and the calls `llm-content-safety` stopped before any backend call
    - **Beyond tokens**: input tokens served from the prompt cache per model, Responses API stateful operations by the backend that served them, image count and spend by consumer, image plan enforcement and image spend over time
- All the data lives in **Application Insights** and **Log Analytics**:
  - `customMetrics` for tokens, cost, governance events and backend attempts
  - `traces` for the chargeback records by user, team, cost center and session
  - `customEvents` for the `BudgetBreach` audit events of the over-budget Logic App, and its run history in Log Analytics (`AzureDiagnostics`, category `WorkflowRuntime`)
  - `requests` for the gateway outcomes, by subscription (agent) and product (plan)
  - `ApiManagementGatewayLogs`, `ApiManagementGatewayLlmLog` and `ApiManagementGatewayMCPLog` for the per-request policy evidence

### Demo UI

[src/app.py](src/app.py) is a lightweight web UI that uses only the Python standard library. With it you can:

- run **guided scenarios**: one-click stories that each send a small, fixed number of real calls through the gateway and explain what it did (see below)
- switch between the **AI models**, **MCP tools** and **A2A agents** surfaces, pick a consumer, and see what its plan allows
- send single requests or bursts, list or call MCP tools, read the agent card and send A2A tasks. A request you build gets its own results card: each call, its latency, and running totals for its `x-session-id`. Send again to add to the session, or press **New session** to start over. Guided scenarios use their own sessions and don't change the builder's
- see what the gateway did for every call, including the agent's downstream steps and who paid for each. **Live session** and each run in **Run history** show latency p50 and p95. This is the end-to-end time `app.py` measures for each call (app → APIM policies → backend → back)
- follow the **Conversation**, which sits to the right of the scenario picker and request builder (on screens narrower than 1,100 px it stacks below them). Press **«** to minimize the panel to a slim rail so the conversation gets the full width; press **»**, the rail label or a mode icon to bring it back. The page remembers this choice. The **Your request** card and the rest of **Live session** follow underneath. To keep the builder short, agent and user cards show a one-line summary, and you hover over (or focus) a card to see its full plan: models, tools, agents, TPM and budget. Every prompt, tool call and agent task, from scenarios and from your own requests, appears as a chat transcript with the model's answer, the tool's result or the agent's reply. Each answer shows its status, latency, tokens and cost. **Details** expands into the tool arguments, the agent's steps, what the gateway decided, and errors. Model answers whose prompt reused Foundry's prompt cache show a **⚡ N of M input cached** pill (or *no cache hit* for prompts of 1,024+ tokens that missed). Their **Details** have a **Prompt cache** section covering who caches (Foundry on the backend, not APIM), how this prompt hit or missed (an identical 1,024+ token prefix, `previous_response_id` or earlier turns, and the affinity backend), the `cached_tokens` the model reported, and how the `responses-pricing` fragment priced it. Each model answer also shows a **📍 region** pill for the backend that served it (blue for the primary, amber for spillover). When the backend pool moves the conversation to another region, an **⇄ Region switched** divider says from where to where and why: the primary answered 429 and the retry policy re-sent the request, the circuit breaker skipped the primary, or the primary came back. A failover answer also lists every attempt from `x-gw-route` (for example `foundry1 · Sweden Central · 429 → foundry2 · France Central · 200`). Filter by session, or type in the box at the bottom to message the selected model, tool or agent in the builder's session. With **Remember earlier turns** on (the default), a single **Send** to a model includes up to the last 10 turns of the same session, so the model remembers the conversation. `app.py` accepts at most 20 user/assistant messages and 24,000 characters of history. Bursts and guided scenarios send single prompts
- **inspect the raw requests**: every result, request-log row (**inspect**) and run (**Inspect requests**) opens the exact HTTP calls `app.py` made through APIM (MCP `initialize` and `tools/call`, A2A `message/send`, chat completions, and the ARM call that fetches a trace credential). Each call shows the method, URL, headers, the JSON body as a collapsible tree, the status, latency, key response headers (`x-gw-*`, rate limits, `x-ms-region`, `Apim-Trace-Id`) and a truncated response body, plus **Copy as curl**. Keys, tokens, cookies, signatures and SAS or `subscription-key` query parameters are masked by `app.py` before anything reaches the browser. Copy as curl swaps them for `$APIM_SUBSCRIPTION_KEY`, `$ENTRA_TOKEN` and `$APIM_DEBUG_TOKEN`. `GET /api/invocations` returns the last 50 inspected requests
- send calls with or without an **Entra ID token**, and with **policy tracing** on
- watch the **$ budget left** for every subscription in real time. Each agent and user card has a thin bar ("$X of $Y left"), a live meter directly above the Conversation message box shows the selected caller's plan budget bar, this session's spend against its per-session cap, the end user's own budget when **End user** is set (from this page's calls), and the cost of the last call, updating as each answer arrives, and **Live session** has a **$ budget left by tier and subscription** card grouped into Gold, Silver and Bronze, with tier totals and a row per subscription. `POST /api/budgets` sums each subscription's own `chargedMicroUsd` from the chargeback records in Log Analytics since the last reset (the `budget-epoch` named value) or the start of the budget period. It reads the logs because APIM v2 can't read `quota-by-key` counters. Records land about 20 s after a call, so this page's newer calls are added right away as a striped segment until their records arrive. The card refreshes every 60 s, and again 20–25 s after calls while some are still pending. A subscription whose budget is spent shows in red and gets 403 from the gateway
- open the **Policies & evidence** tab, which is split into four views so no single page gets too long: **Investigate a request**, **Policy pipeline**, **Logs & telemetry** and **Deployed policies**. Under **Investigate a request**, the **request log** lists every call (filter by run or show only blocked calls). Click a request, or step through them with ↑ / ↓, to replay it in the gateway flow diagram and to load its policy trace (a timeline of the inbound, backend and outbound sections) and its Log Analytics rows. **Policy pipeline** shows the policy scopes and the backend pool. **Logs & telemetry** shows where each piece of evidence lands, along with aggregate KQL cards that have data bars and portal links. **Deployed policies** reads the XML back from Azure and summarises each scope as chips, one per policy element. Explanations sit behind ⓘ tooltips. The UI uses the Azure service icons and the MCP and A2A logos ([src/static/icons](src/static/icons/README.md) lists their sources)
- reopen earlier runs in **Run history**: finished runs are saved per user, so they survive a reload. Hosted behind Easy Auth, runs are keyed by the signed-in user's Entra ID object id (`X-MS-CLIENT-PRINCIPAL-ID`) and stored as JSON Lines under App Service's persistent `/home/data/sandbox-history`. Locally, they are keyed by your OS user and stored in the git-ignored `src/.history`. Set `HISTORY_DIR` to use another folder. On Container Apps (`azd up`), the history lasts until the replica restarts. Saved runs reload their calls, story and request inspector, and **Run again** works for them too. **Clear** deletes your saved history. App.py keeps the latest 50 runs per user
- watch **How a call flows through the gateway**: an animated diagram of the client, the product (plan) and API policy scopes, the backend pool, managed identity, Foundry regions, the MCP server, the A2A agent, and the Log Analytics and Application Insights sinks. Opening the **evidence** of any call replays that call from its `x-gw-*` response headers: a 401, 403 or 429 stops at the policy that rejected it, a downgrade swaps the model, a retry jumps to the next region, and the response leaves log and metric drops in Azure Monitor. If the call was traced, the APIM trace is summarised next to it. With no call selected, play the canned examples (happy path, downgrade, 429, 403 budget, regional failover, MCP tool call, A2A task, 401). The animation honours *reduce motion*
- click **See the pattern** on a pipeline stage, or any component in the diagram, to open the matching AI Gateway lab animation from [images](../../images) (identity, token limits, FinOps, load balancing, circuit breaking, token metrics and logging)
- open the **Chargeback** tab: act as a demo user in a session (or start a new one), see a live session ledger from the response headers, and the Azure Monitor chargeback by team, user and session with a drill-down into every call of a session
- compare plans in the **Plans & pricing** tab, including the cached-input prices, the per-image prices and each plan's image entitlement
- track each consumer's $ budget and its split by surface
- query the Application Insights chargeback live
- reset the budgets

Run it after step 3️⃣ of the notebook, which writes the git-ignored `src/demo-config.private.config`:

```bash
python src/app.py --port 8080
```

Then open http://localhost:8080. The UI gets the Entra ID tokens it sends, the Azure Monitor and Log Analytics queries, the request traces, the policy read-back and the **Reset budgets** button from your Azure CLI login. As a safety net, the UI server refuses more than 60 gateway calls per minute (set `DEMO_MAX_CALLS_PER_MINUTE` to change it).

#### Hosted demo UI behind Easy Auth (optional)

To have the demo always available to your team, set `host_demo_ui = True` in the first notebook cell before you deploy, then run the **Deploy the app to the hosted demo UI** cell in step 1️⃣4️⃣. (With `azd up`, the UI is hosted on Container Apps instead; see [Get started](#-get-started).) [demo-ui.bicep](demo-ui.bicep) adds:

- **App Service** (Linux, B1 by default via `demoUiSku`, billed per hour while the plan exists) that runs the same `src/app.py`, with no build step and no dependencies.
- **App Service authentication (Easy Auth)** on a new **single-tenant app registration**: every page and API needs a sign-in to your Microsoft Entra tenant. Only `/healthz` (which returns `{"ok": true}`) is open, as a readiness probe.
- A **secretless sign-in**. The app registration trusts the app's **user-assigned managed identity** through a federated identity credential (`OVERRIDE_USE_MI_FIC_ASSERTION_CLIENTID`), so there is no client secret to store or rotate.
- **Managed identity instead of your CLI login**. The hosted app calls the gateway (the identity is added to the `entra-identity` fragment's allowed client apps), the management API (traces, policy read-back, budget reset), Log Analytics and Application Insights with the managed identity. It gets *API Management Service Contributor*, *Log Analytics Reader* and *Monitoring Reader*.
- **Presenter attribution**. For calls from the hosted UI, the fragment records the signed-in user as the caller: `x-gw-caller: you@contoso.com (via demo UI)`. The presenter header is trusted only on tokens issued to the UI's managed identity.

The deployment cell zips `app.py`, `static/` and the generated configuration. The configuration contains the subscription keys, but the app only serves `static/`, so the keys never reach the browser. The **See the pattern** GIFs load from GitHub. Run that cell again after every redeployment so the hosted app picks up the new keys. The request inspector buffer (`/api/invocations`) is shared by everyone signed in to the hosted app. Creating the app registration needs permission to register applications in the tenant. The clean-up notebook deletes the app registration as well.

### Guided scenarios

The UI groups the scenarios by topic (budgets, pricing, model governance, tools and agents, identity and safety, routing, chargeback and evidence). Each keeps its number, so the numbers below still apply. Click a scenario in the UI, or **Run the full demo** to play the quick scenarios in order (about 99 calls with content safety and images off; up to about 109 with both enabled, at demo prices, about 10 minutes). Scenario 16 waits for asynchronous subscription suspension, so run it on its own. Scenarios whose feature isn't deployed (content safety, budget suspend or images turned off in `sandbox-config.json`) are shown hatched and struck through with an **OFF** tag. They can't be clicked, and the full demo skips them. Each scenario card draws its path as it starts:

| # | Scenario | What the customer sees | Calls |
|---|----------|------------------------|-------|
| 1 | Same question, every model | Gold asks one question on each model. The gateway prices each call (about 24× between `gpt-4.1` and `gpt-4.1-nano`). | 4 |
| 2 | Model governance | Budgets are reset, then Silver asks for `gpt-4.1` and is downgraded to `gpt-4.1-mini`. Bronze is denied with 403 and nothing reaches Foundry. | 2 |
| 3 | Output cap | Bronze asks for `max_tokens: 4000` and the gateway caps it at 300. | 1 |
| 4 | Tokens-per-minute limit | Bronze bursts long generations and gets 429 with Retry-After after 1,500 tokens per minute. | ≤ 10 |
| 5 | $ budget exhausted | Budgets are reset, then Silver runs long reports until its $0.01 budget is spent and gets 403. | ≤ 12 |
| 6 | MCP tools, priced and entitled per plan | Gold calls the premium `get-supplier-quote` tool and pays $0.002. Silver gets 403 `tool-denied` for the same tool and $0.0005 for `search-products`. | 5 |
| 7 | A2A agent task cost | Gold sends a task to the Sourcing Agent. The bill is the agent fee plus every model and tool step, all charged to Gold. Bronze gets 403 `agent-denied`. | 2 |
| 8 | One $ budget across models, tools and agents | Budgets are reset, then Silver mixes a model call, a tool call and an agent task until the shared $0.01 budget returns 403. | ≤ 6 |
| 9 | Chargeback in Azure Monitor | Opens the App Insights view: cost per consumer, plan, surface and resource, including spend made via the agent. | 0 |
| 10 | Chargeback by user & session | Alice, Ben, Chloe and Dev (one subscription key each) work in named sessions: model calls, tool calls and an A2A task. The session ledger fills from the response headers, then the Azure Monitor table shows cost by team, user and session. Alice's A2A session includes the agent fee and the agent's downstream calls. | 7 |
| 11 | Runaway session: per-session $ cap | Alice loops on `gpt-4.1` in one session until the $0.02 session cap returns 403 `quota-by-key (session-budget)`. A new session for Alice still works. | ≤ 21 |
| 12 | Zero trust: Microsoft Entra ID | A model call and an MCP call with the key but **no token** get 401 from `validate-azure-ad-token`. The same call with a token returns 200, and `x-gw-caller` shows who called. | 3 |
| 13 | Load balancing & regional failover | Gold bursts `gpt-4.1-nano`. Sweden Central returns 429, the gateway retries in France Central (`x-gw-route`), and the circuit breaker keeps sending calls to France. | 8 |
| 14 | Policy trace & Azure Monitor evidence | Budgets are reset, then a traced Silver call is downgraded. The UI shows the policy-by-policy trace, then the call's rows in the gateway and LLM log tables, the deployed policy XML and the aggregate evidence, each with a portal link. | 1 |
| 15 | Blocked before spend: content safety | Needs content safety turned on (`contentSafety.enabled`; off by default). Gold sends a clean prompt (200, plus a small safety-check charge), then a jailbreak, an SSN in a prompt, an SSN in MCP tool arguments and a card number in an A2A task. Each gets 403 `llm-content-safety` and $0 of model tokens, and never reaches Foundry, the MCP server or the agent. The screening shows up as a **Safety checks** line item. | 5 |
| 16 | Over budget, switched off (gateway-triggered, not in the full demo) | Ben spends the Silver budget; the first subscription-budget 403 directly queues suspension. The UI polls ARM every 5 seconds for up to 3 minutes, then checks model and MCP key rejection and loads the audit evidence. **Reset budgets** reactivates him. Needs `budgetSuspend.enabled` (off by default). | ≤ 14 |
| 17 | Responses API: cached input & sticky state | Budgets are reset, then Gold starts a Responses API conversation with a long shared context and continues it with `previous_response_id`. The follow-up goes to the backend that stored the response (`x-gw-affinity: sticky:…`), and input tokens served from the prompt cache are priced at the cached rate (`x-gw-cached-tokens`, `x-gw-cached-savings-usd`). Reading the stored response is free (`stored-read`). Silver gets 404 `response-ownership` for Gold's response id. If the gateway was deployed without `responses-policy.xml` or the `responses-pricing` fragment, the takeaway says so instead of claiming the discount or the 404. | 4 |
| 18 | Images priced per image | Requires `images.enabled`; otherwise it explains how to turn images on and sends no calls. Gold generates a `low` image priced per image (`x-gw-image-spec`, `x-gw-image-unit-usd`). Silver is denied `high` quality (403 `image-quality`) and Bronze has no image entitlement (403 `image-entitlement`). A Silver `medium` image ($0.011) uses up the same $0.01 budget that tokens, tools and agents draw on, so the next image returns 403 from `quota-by-key`. | ≤ 5 |
| 19 | One app, many users: per-user $ budgets | Budgets are reset, then the Gold app (one key, one product) runs long `gpt-4.1` reports for three end users named in `x-gw-end-user`: `ana.lopez` (default `standard` class, $0.005) is blocked with 403 `quota-by-key (end-user-budget)` once her counter catches up (in testing after 4 calls, $0.0099 against $0.005: counters sync asynchronously, so a tiny demo budget overshoots by a call or two), `raj.iyer` (`power`, $0.015) keeps going, `tom.intern` (override $0.002) is blocked after 1. The app's own call without the header still answers 200. See [Per-user $ budgets inside one app](#per-user--budgets-inside-one-app). | ≤ 16 |

#### If a scenario doesn't match its description

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Silver or Bronze gets 403 `quota-by-key` before the scenario's point is made (2, 5, 8, 14, 17) | An earlier run spent the tiny budget | Click **Reset budgets**. A warning toast means the new `budget-epoch` wasn't provisioned within 90 s; wait a moment and reset again. |
| A model (often `DeepSeek-V3.2`) returns 500 intermittently in scenario 1 | The deployed inference policy predates the `["usage"] as JObject` null guard in [policy.xml](policy.xml) | Redeploy (notebook step 2️⃣ or `azd up`) so the gateway gets the current policies. |
| Scenario 17 shows no `x-gw-affinity` header, no cached-input pricing, or Silver gets 200 instead of 404 | The gateway was deployed without [responses-policy.xml](responses-policy.xml) or the `responses-pricing` fragment | Redeploy. The takeaway reports this case instead of claiming the result. |
| The local UI won't start or shows no keys | `src/demo-config.private.config` is missing (for example, after a fresh clone or worktree) | Run notebook step 3️⃣ again. If you use the hosted demo UI, you can also copy the file from the App Service (`site/wwwroot/demo-config.private.config`, for example through Kudu). It holds subscription keys, so keep it git-ignored. |

The scenarios check the gateway's `x-gw-*` headers rather than assuming an outcome. A gateway deployed from an older version of this lab can therefore show a different result than the table above. Redeploying brings its policies and fragments in line with the repo.

### Suggested demo script (manual)

1. **Reset budgets** in the UI.
2. **AI models**: select **Customer Support Agent** (Gold) and send to `gpt-4.1`, then `DeepSeek-V3.2`. Point out the served model, tokens and cost of each call. Switch to **Research Agent** (Silver) and send to `gpt-4.1`: the gateway downgrades it to `gpt-4.1-mini`.
3. **MCP tools**: select Gold, call `get-supplier-quote` ($0.002). Switch to Silver and call the same tool: 403 `tool-denied`, because the tool is not in the Silver plan.
4. **A2A agents**: select Gold, read the agent card, then send a sourcing task. Walk through the steps table: every model and tool call the agent made is billed to Gold, with the agent fee on top. Switch to Bronze: 403 `agent-denied`.
5. Select **Research Agent** (Silver) and send one Sourcing Agent task, then a model call: the shared $0.01 budget is exhausted and the gateway returns 403 on every surface.
6. Open **Plans & pricing** to show that each plan is one APIM product, then the **Azure Monitor** tab or the **Workbook** to show the chargeback by consumer, plan and surface.
7. **Policies & evidence**: untick **Send Entra ID token** and send one call to show the 401, then tick it again. Tick **Trace policies**, send a Silver `gpt-4.1` call, then pick it in the tab's **Request log** (or click its **Evidence** link) to replay it in the flow diagram and show the trace and, a few minutes later, its Log Analytics rows. Use ↑ / ↓ to step through the other calls of the run. Click **Open in Log Analytics** to show the same rows in the Azure portal.
8. **Chargeback**: pick **Alice Chen**, send a couple of calls in her session, click **New session** and send another. The session ledger splits her spend by session at once; a few minutes later the Azure Monitor table rolls it up by team and cost center. Run **Runaway session** to show the per-session $ cap.

### Prerequisites

- [Python 3.12 or later version](https://www.python.org/) installed
- [VS Code](https://code.visualstudio.com/) installed with the [Jupyter notebook extension](https://marketplace.visualstudio.com/items?itemName=ms-toolsai.jupyter) enabled
- [uv](https://docs.astral.sh/uv/): run `uv sync` from the repo root to install dependencies
- [An Azure Subscription](https://azure.microsoft.com/free/) with [Contributor](https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/privileged#contributor) + [RBAC Administrator](https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/privileged#role-based-access-control-administrator) or [Owner](https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/privileged#owner) roles. The optional [Over budget, switched off](#over-budget-switched-off) loop with its default custom role needs Owner or User Access Administrator.
- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) installed and [Signed into your Azure subscription](https://learn.microsoft.com/cli/azure/authenticate-azure-cli-interactively)

### 🚀 Get started

Proceed by opening the [Jupyter notebook](ai-gateway-sandbox.ipynb), and follow the steps provided.

#### Or deploy everything with one command (azd)

[Azure Developer CLI](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd) deploys the same infrastructure as the notebook ([main.bicep](main.bicep)) and also hosts the demo UI on Azure Container Apps, behind Microsoft Entra ID sign-in:

```bash
cd labs/ai-gateway-sandbox   # or stay at the repo root: its azure.yaml deploys the same thing
azd auth login
azd up        # asks for an environment name, subscription and region (e.g. swedencentral)
```

`azd up` takes about 10 minutes and prints the UI URL (`SERVICE_UI_URI`). What it does:

- a `preprovision` hook ([infra/scripts/build_config.py](infra/scripts/build_config.py)) turns [sandbox-config.json](sandbox-config.json) into the lab parameters, the same way step 2️⃣ of the notebook does. Edit `sandbox-config.json` to change regions, models, prices, plans or consumers (it mirrors the notebook's first cell).
- [infra/main.bicep](infra/main.bicep) creates the resource group `rg-<environment name>`, deploys the lab, then the UI: a container registry, a Container App built from [src/Dockerfile](src/Dockerfile) (built in Azure, no local Docker needed) and an Entra ID app registration that signs users in with a federated credential of the UI managed identity (no client secret).
- the UI uses its **managed identity** instead of your Azure CLI login: the gateway approves it as a caller and records the signed-in user as the caller (`x-gw-caller: you@contoso.com (via demo UI)`, or `sandbox-ui (managed identity)` without a signed-in user), and it has *API Management Service Contributor*, *Log Analytics Reader* and *Monitoring Reader* on the resource group for the traces, policy read-back, budget reset and Azure Monitor queries. The configuration written by step 3️⃣ of the notebook is a Container App secret.
- only the user who ran `azd up` can sign in. To let others in, assign them to the *AI Gateway Sandbox UI (&lt;environment&gt;)* enterprise application in Microsoft Entra ID.

Run `azd deploy` to ship UI changes only. If your tenant requires a service tree ID on app registrations, run `azd env set AZURE_SERVICE_MANAGEMENT_REFERENCE <id>` first. Remove everything with `azd down --purge` (it also purges the Foundry resources, and a `postdown` hook, [infra/scripts/delete_budget_role.py](infra/scripts/delete_budget_role.py), deletes the custom role of the over-budget Logic App); the app registration is not deleted by `azd down`.

> [!NOTE]
> The hosted UI image only contains `src/app.py` and `src/static`; the **See the pattern** GIFs are redirected to GitHub. Running the UI locally (`python src/app.py`) still works with a notebook deployment.

> [!NOTE]
> This lab was previously named *AI Gateway Tokenomics* (`labs/ai-gateway-tokenomics`). The notebook derives the resource group from the folder name, so new deployments go to `lab-ai-gateway-sandbox`. If you deployed the lab under its old name and want to keep using that deployment, set `deployment_name = "ai-gateway-tokenomics"` in the first cell (and in the clean-up notebook). A redeploy over an old deployment renames the attribution policy fragment to `payer-attribution`, the custom metric namespace to `ai-gateway-sandbox` and replaces the workbook; delete the old `tokenomics-attribution` fragment and *AI Gateway Tokenomics* workbook afterwards.

### 🗑️ Clean up resources

When you're finished with the lab, you should remove all your deployed resources from Azure to avoid extra charges and keep your Azure subscription uncluttered.
Use the [clean-up-resources notebook](clean-up-resources.ipynb) for that.
