// AI Gateway Sandbox - "Blocked before spend": Azure AI Content Safety for the llm-content-safety policy.
//
// Deploys the Content Safety account the gateway calls before any model, MCP tool or A2A agent is reached, a custom
// blocklist (regex patterns for sensitive data such as SSNs and card numbers), the APIM backend that the
// llm-content-safety policy points to, and the Cognitive Services User role for the APIM managed identity.

@description('Name of the existing API Management service')
param apimName string

@description('Principal ID of the API Management system-assigned managed identity')
param apimPrincipalId string

@description('Region of the Content Safety account')
param location string = resourceGroup().location

@description('Suffix that makes the resource names unique')
param resourceSuffix string

@description('Name of the custom blocklist (referenced by the llm-content-safety policy)')
param blocklistName string = 'sensitive-data'

@description('Blocklist items: name, pattern and isRegex (optional, default true)')
param blocklistItems array = []

resource contentSafety 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: 'contentsafety-${resourceSuffix}'
  location: location
  sku: {
    name: 'S0'
  }
  kind: 'ContentSafety'
  properties: {
    publicNetworkAccess: 'Enabled'
    customSubDomainName: toLower('contentsafety-${resourceSuffix}')
    // The gateway authenticates with its managed identity; no keys are needed
    disableLocalAuth: true
  }
}

resource blocklist 'Microsoft.CognitiveServices/accounts/raiBlocklists@2025-06-01' = if (!empty(blocklistItems)) {
  parent: contentSafety
  name: blocklistName
  properties: {
    description: 'Sensitive data that must never reach a model, tool or agent (AI Gateway Sandbox)'
  }
}

// Items are written one at a time: parallel writes to the same blocklist fail with IfMatchPreconditionFailed
@batchSize(1)
resource blocklistItem 'Microsoft.CognitiveServices/accounts/raiBlocklists/raiBlocklistItems@2025-06-01' = [for item in blocklistItems: {
  parent: blocklist
  name: item.name
  properties: {
    isRegex: item.?isRegex ?? true
    pattern: item.pattern
  }
}]

// Cognitive Services User: lets the gateway call text:analyze and text:shieldPrompt with its managed identity
var cognitiveServicesUserRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'a97b65f3-24c7-4388-baec-2e87135dc908')
resource apimContentSafetyUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: contentSafety
  name: guid(contentSafety.id, apimPrincipalId, cognitiveServicesUserRoleId)
  properties: {
    roleDefinitionId: cognitiveServicesUserRoleId
    principalId: apimPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource apim 'Microsoft.ApiManagement/service@2024-06-01-preview' existing = {
  name: apimName
}

// The llm-content-safety policy refers to this backend by id
resource contentSafetyBackend 'Microsoft.ApiManagement/service/backends@2024-06-01-preview' = {
  parent: apim
  name: 'content-safety-backend'
  properties: {
    description: 'Azure AI Content Safety (llm-content-safety policy, Blocked before spend)'
    url: contentSafety.properties.endpoint
    protocol: 'http'
    credentials: {
      #disable-next-line BCP037
      managedIdentity: {
        resource: 'https://cognitiveservices.azure.com'
      }
    }
  }
  dependsOn: [
    apimContentSafetyUser
  ]
}

output contentSafetyName string = contentSafety.name
output contentSafetyEndpoint string = contentSafety.properties.endpoint
output backendId string = contentSafetyBackend.name
output blocklistName string = empty(blocklistItems) ? '' : blocklist.name
