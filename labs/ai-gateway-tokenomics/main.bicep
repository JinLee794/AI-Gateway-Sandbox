// ------------------
//    PARAMETERS
// ------------------

param aiServicesConfig array = []
param modelsConfig array = []
param apimSku string = 'Basicv2'
param inferenceAPIType string = 'AzureOpenAIV1'
param inferenceAPIPath string = 'inference'
param foundryProjectName string = 'default'

@description('Model price list used by the gateway to price every call. Format: "<deployment>=<input USD per 1M tokens>/<output USD per 1M tokens>;..."')
param modelPricing string

@description('MCP tool price list (micro-USD per tools/call). Format: "<tool>=<micro-USD>;..."')
param toolPricing string

@description('A2A agent fee list (micro-USD per task, on top of the downstream model and tool spend). Format: "<agent>=<micro-USD>;..."')
param agentPricing string

@description('Tiers (APIM products) with their governance settings: name, displayName, description, allowedModels, fallbackModel, onDisallowedModel (downgrade|deny), maxOutputTokens, tpm, tokenQuota, tokenQuotaPeriod, allowedTools, toolCallsPerMinute, allowedAgents, agentCallsPerMinute, budgetMicroUsd, budgetPeriodSeconds, internal (optional). The tier named "agent-platform" is the internal product used by platform agents.')
param tiersConfig array = []

@description('Agents (APIM subscriptions): name, displayName, tier')
param agentsConfig array = []

@description('Model used by the Sourcing Agent (must be allowed in the agent-platform tier)')
param sourcingAgentModel string = 'gpt-4.1-mini'

@description('Region of the Container Apps environment that hosts the Sourcing Agent')
param agentLocation string = resourceGroup().location

@description('Value of the budget-epoch named value. Every $ budget and token quota counter key includes it, so a new value starts fresh budgets.')
param budgetEpoch string = utcNow('yyyyMMddHHmmss')

@description('Shared secret the gateway sends to the Sourcing Agent backend, so the agent only accepts tasks routed (and billed) through the gateway')
@secure()
param agentBackendSecret string = newGuid()

// ------------------
//    VARIABLES
// ------------------

var resourceSuffix = uniqueString(subscription().id, resourceGroup().id)

// ------------------
//    RESOURCES
// ------------------

// 1. Log Analytics Workspace
module lawModule '../../modules/operational-insights/v1/workspaces.bicep' = {
  name: 'lawModule'
}

// 2. Application Insights (custom metrics with dimensions are required for the Agent / Tier / Model split)
module appInsightsModule '../../modules/monitor/v1/appinsights.bicep' = {
  name: 'appInsightsModule'
  params: {
    lawId: lawModule.outputs.id
    customMetricsOptedInType: 'WithDimensions'
  }
}

// 3. API Management
module apimModule '../../modules/apim/v3/apim.bicep' = {
  name: 'apimModule'
  params: {
    apimSku: apimSku
    lawId: lawModule.outputs.id
    appInsightsId: appInsightsModule.outputs.id
    appInsightsInstrumentationKey: appInsightsModule.outputs.instrumentationKey
  }
}

// 4. Microsoft Foundry with the model deployments: one Foundry resource per region (aiServicesConfig). The gateway
//    load-balances them as a priority backend pool (priority 1 = primary, priority 2 = spillover). A model can set
//    primaryCapacity to give the priority-1 resource less capacity (e.g. a small PTU-like reservation), so the demo
//    can show the primary being throttled and the gateway failing over to the secondary region.
module foundryModule '../../modules/cognitive-services/v3/foundry.bicep' = [for config in aiServicesConfig: {
  name: 'foundryModule-${config.name}'
  params: {
    aiServicesConfig: [
      config
    ]
    modelsConfig: map(modelsConfig, model => union(model, {
      capacity: (config.?priority ?? 1) == 1 ? (model.?primaryCapacity ?? model.capacity) : model.capacity
    }))
    apimPrincipalId: apimModule.outputs.principalId
    foundryProjectName: foundryProjectName
  }
}]

resource apim 'Microsoft.ApiManagement/service@2024-06-01-preview' existing = {
  name: 'apim-${resourceSuffix}'
  dependsOn: [
    apimModule
  ]
}

// 5. Named values used by the policies
resource modelPricingNamedValue 'Microsoft.ApiManagement/service/namedValues@2024-06-01-preview' = {
  parent: apim
  name: 'model-pricing'
  properties: {
    displayName: 'model-pricing'
    value: modelPricing
    secret: false
  }
}

resource budgetEpochNamedValue 'Microsoft.ApiManagement/service/namedValues@2024-06-01-preview' = {
  parent: apim
  name: 'budget-epoch'
  properties: {
    displayName: 'budget-epoch'
    // A new value on every deployment, so a redeploy never revives budgets and quotas exhausted earlier
    value: budgetEpoch
    secret: false
  }
}

resource toolPricingNamedValue 'Microsoft.ApiManagement/service/namedValues@2024-06-01-preview' = {
  parent: apim
  name: 'tool-pricing'
  properties: {
    displayName: 'tool-pricing'
    value: toolPricing
    secret: false
  }
}

resource agentPricingNamedValue 'Microsoft.ApiManagement/service/namedValues@2024-06-01-preview' = {
  parent: apim
  name: 'agent-pricing'
  properties: {
    displayName: 'agent-pricing'
    value: agentPricing
    secret: false
  }
}

resource agentBackendSecretNamedValue 'Microsoft.ApiManagement/service/namedValues@2024-06-01-preview' = {
  parent: apim
  name: 'agent-backend-secret'
  properties: {
    displayName: 'agent-backend-secret'
    value: agentBackendSecret
    secret: true
  }
}

// FinOps chargeback logic shared by the model and MCP tool APIs (who pays for a call)
resource attributionFragment 'Microsoft.ApiManagement/service/policyFragments@2024-06-01-preview' = {
  parent: apim
  name: 'tokenomics-attribution'
  properties: {
    description: 'Attributes model and tool spend to the paying subscription, including calls made by platform agents on behalf of a caller'
    format: 'rawxml'
    value: loadTextContent('attribution-fragment.xml')
  }
}

// Workload identity of the Sourcing Agent: it presents an Entra ID token of this identity to the gateway
resource agentIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-sourcing-agent-${resourceSuffix}'
  location: agentLocation
}

// Zero-trust: Entra ID token validation, included first by every tier policy (models, MCP tools and A2A agents)
resource entraIdentityFragment 'Microsoft.ApiManagement/service/policyFragments@2024-06-01-preview' = {
  parent: apim
  name: 'entra-identity'
  properties: {
    description: 'Validates the caller Microsoft Entra ID token (validate-azure-ad-token) and identifies the caller for chargeback'
    format: 'rawxml'
    value: replace(replace(loadTextContent('entra-identity-fragment.xml'), '{tenant-id}', tenant().tenantId), '{agent-client-id}', agentIdentity.properties.clientId)
  }
}

// 6. AI model API (OpenAI v1 compatible) with the API-level tokenomics policy. With more than one Foundry resource the
//    module creates a backend per resource (with a circuit breaker that trips on 429) and a priority backend pool.
module inferenceAPIModule '../../modules/apim/v3/inference-api.bicep' = {
  name: 'inferenceAPIModule'
  params: {
    policyXml: loadTextContent('policy.xml')
    apimLoggerId: apimModule.outputs.loggerId
    appInsightsId: appInsightsModule.outputs.id
    appInsightsInstrumentationKey: appInsightsModule.outputs.instrumentationKey
    aiServicesConfig: [for (config, i) in aiServicesConfig: foundryModule[i].outputs.extendedAIServicesConfig[0]]
    inferenceAPIType: inferenceAPIType
    inferenceAPIPath: inferenceAPIPath
    configureCircuitBreaker: true
  }
  dependsOn: [
    modelPricingNamedValue
    budgetEpochNamedValue
    attributionFragment
  ]
}

// 7. Commerce tools: a REST API (mock backend implemented in the gateway) exposed as an MCP server
resource commerceApi 'Microsoft.ApiManagement/service/apis@2024-06-01-preview' = {
  parent: apim
  name: 'commerce-api'
  properties: {
    apiType: 'http'
    type: 'http'
    displayName: 'Commerce Tools API'
    description: 'Retail commerce tools (catalog, inventory, supplier quotes) - source of the commerce MCP server'
    subscriptionRequired: false
    path: 'commerce'
    protocols: [
      'https'
    ]
    format: 'openapi+json'
    value: loadTextContent('src/tools/openapi.json')
  }
}

resource commerceApiPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-06-01-preview' = {
  parent: commerceApi
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: loadTextContent('src/tools/api-policy.xml')
  }
}

var commerceTools = [
  { name: 'search-products', description: 'Search the product catalog by category and return matching products with SKU and list price.' }
  { name: 'check-inventory', description: 'Return the on-hand stock for a SKU in every warehouse.' }
  { name: 'get-supplier-quote', description: 'Premium data tool: request real-time quotes from the partner supplier network for a SKU and quantity.' }
]

resource commerceMcp 'Microsoft.ApiManagement/service/apis@2024-06-01-preview' = {
  parent: apim
  name: 'commerce-mcp'
  properties: {
    type: 'mcp'
    displayName: 'Commerce Tools MCP'
    description: 'MCP server with retail commerce tools. Each tool call is priced, entitled per plan and charged to the caller budget.'
    subscriptionRequired: true
    subscriptionKeyParameterNames: {
      header: 'api-key'
      query: 'subscription-key'
    }
    path: 'commerce-mcp'
    protocols: [
      'https'
    ]
    mcpTools: [for tool in commerceTools: {
      name: tool.name
      operationId: '${commerceApi.id}/operations/${tool.name}'
      description: tool.description
    }]
  }
}

resource commerceMcpPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-06-01-preview' = {
  parent: commerceMcp
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: loadTextContent('src/tools/mcp-policy.xml')
  }
  dependsOn: [
    toolPricingNamedValue
    attributionFragment
  ]
}

resource commerceMcpDiagnostics 'Microsoft.ApiManagement/service/apis/diagnostics@2022-08-01' = {
  parent: commerceMcp
  name: 'applicationinsights'
  properties: {
    alwaysLog: 'allErrors'
    httpCorrelationProtocol: 'W3C'
    logClientIp: true
    loggerId: resourceId('Microsoft.ApiManagement/service/loggers', apim.name, 'appinsights-logger')
    metrics: true
    verbosity: 'information'
    sampling: {
      samplingType: 'fixed'
      percentage: 100
    }
  }
}

// 8. Sourcing Agent: a minimal A2A agent on Azure Container Apps, published through the gateway as an A2A agent API
resource agentEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'aca-env-${resourceSuffix}'
  location: agentLocation
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: lawModule.outputs.customerId
        sharedKey: lawModule.outputs.primarySharedKey
      }
    }
  }
}

// The agent calls models and tools through the gateway with this platform identity (internal agent-platform product)
resource agentPlatformSubscription 'Microsoft.ApiManagement/service/subscriptions@2024-06-01-preview' = {
  parent: apim
  name: 'sourcing-agent-identity'
  properties: {
    displayName: 'Sourcing Agent (platform identity)'
    scope: '/products/agent-platform'
    state: 'active'
    allowTracing: true
  }
  dependsOn: [
    tierProduct
  ]
}

// The agent code is passed in an environment variable and runs on a stock Python image, so the lab needs no
// container registry or image build. For production, build an image and push it to Azure Container Registry.
resource sourcingAgentApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'sourcing-agent-${resourceSuffix}'
  location: agentLocation
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${agentIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: agentEnvironment.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8080
        transport: 'auto'
        allowInsecure: false
      }
      secrets: [
        { name: 'gateway-key', value: agentPlatformSubscription.listSecrets().primaryKey }
        { name: 'backend-secret', value: agentBackendSecret }
      ]
    }
    template: {
      containers: [
        {
          name: 'sourcing-agent'
          image: 'mcr.microsoft.com/azurelinux/base/python:3.12'
          command: [ 'python3', '-c', 'import os;exec(os.environ["APP_CODE"])' ]
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          env: [
            { name: 'APP_CODE', value: loadTextContent('src/agent/agent.py') }
            { name: 'PORT', value: '8080' }
            { name: 'GATEWAY_KEY', secretRef: 'gateway-key' }
            { name: 'AGENT_BACKEND_SECRET', secretRef: 'backend-secret' }
            // Secret changes alone don't create a revision; this forces one per deployment so the agent picks up the new secret
            { name: 'DEPLOYMENT_ID', value: budgetEpoch }
            { name: 'MCP_URL', value: '${apimModule.outputs.gatewayUrl}/${commerceMcp.properties.path}/mcp' }
            { name: 'INFERENCE_URL', value: '${apimModule.outputs.gatewayUrl}/${inferenceAPIPath}/openai/v1' }
            { name: 'MODEL', value: sourcingAgentModel }
            { name: 'AZURE_CLIENT_ID', value: agentIdentity.properties.clientId }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}

resource sourcingAgentApi 'Microsoft.ApiManagement/service/apis@2024-10-01-preview' = {
  parent: apim
  name: 'sourcing-agent'
  properties: {
    type: 'a2a'
    displayName: 'Sourcing Agent'
    description: 'A2A procurement agent. Each task is charged a fee plus the model and tool spend the agent incurs on the caller behalf.'
    agent: {
      id: 'sourcing-agent'
    }
    isAgent: true
    a2aProperties: {
      agentCardPath: '/.well-known/agent-card.json'
      agentCardBackendUrl: 'https://${sourcingAgentApp.properties.configuration.ingress.fqdn}/.well-known/agent-card.json'
    }
    jsonRpcProperties: {
      backendUrl: 'https://${sourcingAgentApp.properties.configuration.ingress.fqdn}'
      path: '/'
    }
    subscriptionRequired: true
    subscriptionKeyParameterNames: {
      header: 'api-key'
      query: 'subscription-key'
    }
    path: 'sourcing-agent'
    protocols: [
      'https'
    ]
  }
}

resource sourcingAgentApiPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-06-01-preview' = {
  parent: sourcingAgentApi
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: loadTextContent('src/agent/a2a-policy.xml')
  }
  dependsOn: [
    agentPricingNamedValue
    agentBackendSecretNamedValue
  ]
}

resource sourcingAgentApiDiagnostics 'Microsoft.ApiManagement/service/apis/diagnostics@2022-08-01' = {
  parent: sourcingAgentApi
  name: 'applicationinsights'
  properties: {
    alwaysLog: 'allErrors'
    httpCorrelationProtocol: 'W3C'
    logClientIp: true
    loggerId: resourceId('Microsoft.ApiManagement/service/loggers', apim.name, 'appinsights-logger')
    metrics: true
    verbosity: 'information'
    sampling: {
      samplingType: 'fixed'
      percentage: 100
    }
  }
}

// 9. Tiers (products): ONE plan per tier that bundles AI models, MCP tools and A2A agents under one contract
@batchSize(1)
resource tierProduct 'Microsoft.ApiManagement/service/products@2024-06-01-preview' = [for tier in tiersConfig: {
  name: tier.name
  parent: apim
  properties: {
    displayName: tier.displayName
    description: tier.description
    subscriptionRequired: true
    approvalRequired: false
    state: 'published'
  }
}]

@batchSize(1)
resource tierProductApiLink 'Microsoft.ApiManagement/service/products/apiLinks@2024-06-01-preview' = [for (tier, i) in tiersConfig: {
  parent: tierProduct[i]
  name: 'inference-${tier.name}'
  properties: {
    apiId: inferenceAPIModule.outputs.apiId
  }
}]

@batchSize(1)
resource tierProductMcpLink 'Microsoft.ApiManagement/service/products/apiLinks@2024-06-01-preview' = [for (tier, i) in tiersConfig: {
  parent: tierProduct[i]
  name: 'commerce-mcp-${tier.name}'
  properties: {
    apiId: commerceMcp.id
  }
  dependsOn: [
    tierProductApiLink
  ]
}]

@batchSize(1)
resource tierProductAgentLink 'Microsoft.ApiManagement/service/products/apiLinks@2024-06-01-preview' = [for (tier, i) in tiersConfig: if (!(tier.?internal ?? false)) {
  parent: tierProduct[i]
  name: 'sourcing-agent-${tier.name}'
  properties: {
    apiId: sourcingAgentApi.id
  }
  dependsOn: [
    tierProductMcpLink
  ]
}]

var productPolicyTemplate = loadTextContent('product-policy.xml')

@batchSize(1)
resource tierProductPolicy 'Microsoft.ApiManagement/service/products/policies@2024-06-01-preview' = [for (tier, i) in tiersConfig: {
  parent: tierProduct[i]
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: reduce(items({
      '{tier}': tier.name
      '{allowed-models}': join(tier.allowedModels, ',')
      '{fallback-model}': tier.fallbackModel
      '{on-disallowed-model}': tier.onDisallowedModel
      '{max-output-tokens}': string(tier.maxOutputTokens)
      '{budget-micro-usd}': string(tier.budgetMicroUsd)
      '{budget-period-seconds}': string(tier.budgetPeriodSeconds)
      '{tpm}': string(tier.tpm)
      '{token-quota}': string(tier.tokenQuota)
      '{token-quota-period}': tier.tokenQuotaPeriod
      '{allowed-tools}': join(tier.allowedTools, ',')
      '{tool-calls-per-minute}': string(tier.toolCallsPerMinute)
      '{allowed-agents}': join(tier.allowedAgents, ',')
      '{agent-calls-per-minute}': string(tier.agentCallsPerMinute)
    }), productPolicyTemplate, (xml, placeholder) => replace(xml, placeholder.key, placeholder.value))
  }
  dependsOn: [
    budgetEpochNamedValue
    entraIdentityFragment
    tierProductApiLink
    tierProductMcpLink
    tierProductAgentLink
  ]
}]

// 10. Agents (subscriptions), each one scoped to its tier
@batchSize(1)
resource agentSubscription 'Microsoft.ApiManagement/service/subscriptions@2024-06-01-preview' = [for agent in agentsConfig: {
  parent: apim
  name: agent.name
  properties: {
    displayName: agent.displayName
    scope: '/products/${agent.tier}'
    state: 'active'
    allowTracing: true
  }
  dependsOn: [
    tierProduct
    tierProductPolicy
  ]
}]

// 11. Tokenomics workbook on top of Application Insights
var budgetRows = join(map(agentsConfig, agent => '\'${agent.name}\', \'${agent.displayName}\', \'${agent.tier}\', ${filter(tiersConfig, tier => tier.name == agent.tier)[0].budgetMicroUsd}'), ', ')

resource tokenomicsWorkbook 'Microsoft.Insights/workbooks@2022-04-01' = {
  name: guid(resourceGroup().id, resourceSuffix, 'tokenomicsWorkbook')
  location: resourceGroup().location
  kind: 'shared'
  properties: {
    displayName: 'AI Gateway Tokenomics'
    serializedData: replace(replace(loadTextContent('workbook.json'), '{budget-rows}', budgetRows), '{law-id}', lawModule.outputs.id)
    sourceId: appInsightsModule.outputs.id
    category: 'workbook'
  }
}

// ------------------
//    OUTPUTS
// ------------------

output logAnalyticsWorkspaceId string = lawModule.outputs.customerId
output logAnalyticsResourceId string = lawModule.outputs.id
output tenantId string = tenant().tenantId
output agentIdentityClientId string = agentIdentity.properties.clientId
output foundryBackends array = [for (config, i) in aiServicesConfig: {
  name: config.name
  location: config.location
  priority: config.?priority ?? 1
  endpoint: foundryModule[i].outputs.extendedAIServicesConfig[0].endpoint
}]
output appInsightsId string = appInsightsModule.outputs.id
output appInsightsAppId string = appInsightsModule.outputs.appId
output appInsightsName string = appInsightsModule.outputs.name
output workbookId string = tokenomicsWorkbook.id
output apimServiceId string = apimModule.outputs.id
output apimServiceName string = apimModule.outputs.name
output apimResourceGatewayURL string = apimModule.outputs.gatewayUrl
output inferenceBaseUrl string = '${apimModule.outputs.gatewayUrl}/${inferenceAPIPath}/openai/v1'
output mcpUrl string = '${apimModule.outputs.gatewayUrl}/${commerceMcp.properties.path}/mcp'
output a2aUrl string = '${apimModule.outputs.gatewayUrl}/${sourcingAgentApi.properties.path}'
output agentCardUrl string = '${apimModule.outputs.gatewayUrl}/${sourcingAgentApi.properties.path}/.well-known/agent-card.json'
output agentAppUrl string = 'https://${sourcingAgentApp.properties.configuration.ingress.fqdn}'
output toolPricing string = toolPricing
output agentPricing string = agentPricing

#disable-next-line outputs-should-not-contain-secrets
output agentKeys array = [for (agent, i) in agentsConfig: {
  name: agent.name
  displayName: agent.displayName
  tier: agent.tier
  key: agentSubscription[i].listSecrets().primaryKey
}]
