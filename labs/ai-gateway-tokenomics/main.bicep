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

@description('Tiers (APIM products) with their governance settings: name, displayName, description, allowedModels, fallbackModel, onDisallowedModel (downgrade|deny), maxOutputTokens, tpm, tokenQuota, tokenQuotaPeriod, budgetMicroUsd, budgetPeriodSeconds')
param tiersConfig array = []

@description('Agents (APIM subscriptions): name, displayName, tier')
param agentsConfig array = []

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

// 4. Microsoft Foundry with the model deployments
module foundryModule '../../modules/cognitive-services/v3/foundry.bicep' = {
  name: 'foundryModule'
  params: {
    aiServicesConfig: aiServicesConfig
    modelsConfig: modelsConfig
    apimPrincipalId: apimModule.outputs.principalId
    foundryProjectName: foundryProjectName
  }
}

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
    value: '1'
    secret: false
  }
}

// 6. Inference API (OpenAI v1 compatible) with the API-level tokenomics policy
module inferenceAPIModule '../../modules/apim/v3/inference-api.bicep' = {
  name: 'inferenceAPIModule'
  params: {
    policyXml: loadTextContent('policy.xml')
    apimLoggerId: apimModule.outputs.loggerId
    appInsightsId: appInsightsModule.outputs.id
    appInsightsInstrumentationKey: appInsightsModule.outputs.instrumentationKey
    aiServicesConfig: foundryModule.outputs.extendedAIServicesConfig
    inferenceAPIType: inferenceAPIType
    inferenceAPIPath: inferenceAPIPath
  }
  dependsOn: [
    modelPricingNamedValue
    budgetEpochNamedValue
  ]
}

// 7. Tiers (products) with the governance policy
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

var productPolicyTemplate = loadTextContent('product-policy.xml')

@batchSize(1)
resource tierProductPolicy 'Microsoft.ApiManagement/service/products/policies@2024-06-01-preview' = [for (tier, i) in tiersConfig: {
  parent: tierProduct[i]
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(productPolicyTemplate,
      '{tier}', tier.name),
      '{allowed-models}', join(tier.allowedModels, ',')),
      '{fallback-model}', tier.fallbackModel),
      '{on-disallowed-model}', tier.onDisallowedModel),
      '{max-output-tokens}', string(tier.maxOutputTokens)),
      '{budget-micro-usd}', string(tier.budgetMicroUsd)),
      '{budget-period-seconds}', string(tier.budgetPeriodSeconds)),
      '{tpm}', string(tier.tpm)),
      '{token-quota}', string(tier.tokenQuota)),
      '{token-quota-period}', tier.tokenQuotaPeriod)
  }
  dependsOn: [
    budgetEpochNamedValue
    tierProductApiLink
  ]
}]

// 8. Agents (subscriptions), each one scoped to its tier
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

// 9. Tokenomics workbook on top of Application Insights
var budgetRows = join(map(agentsConfig, agent => '\'${agent.name}\', \'${agent.displayName}\', \'${agent.tier}\', ${filter(tiersConfig, tier => tier.name == agent.tier)[0].budgetMicroUsd}'), ', ')

resource tokenomicsWorkbook 'Microsoft.Insights/workbooks@2022-04-01' = {
  name: guid(resourceGroup().id, resourceSuffix, 'tokenomicsWorkbook')
  location: resourceGroup().location
  kind: 'shared'
  properties: {
    displayName: 'AI Gateway Tokenomics'
    serializedData: replace(loadTextContent('workbook.json'), '{budget-rows}', budgetRows)
    sourceId: appInsightsModule.outputs.id
    category: 'workbook'
  }
}

// ------------------
//    OUTPUTS
// ------------------

output logAnalyticsWorkspaceId string = lawModule.outputs.customerId
output appInsightsId string = appInsightsModule.outputs.id
output appInsightsAppId string = appInsightsModule.outputs.appId
output appInsightsName string = appInsightsModule.outputs.name
output workbookId string = tokenomicsWorkbook.id
output apimServiceId string = apimModule.outputs.id
output apimServiceName string = apimModule.outputs.name
output apimResourceGatewayURL string = apimModule.outputs.gatewayUrl
output inferenceBaseUrl string = '${apimModule.outputs.gatewayUrl}/${inferenceAPIPath}/openai/v1'

#disable-next-line outputs-should-not-contain-secrets
output agentKeys array = [for (agent, i) in agentsConfig: {
  name: agent.name
  displayName: agent.displayName
  tier: agent.tier
  key: agentSubscription[i].listSecrets().primaryKey
}]
