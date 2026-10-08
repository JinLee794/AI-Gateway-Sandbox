// AI Gateway Sandbox - azd entry point ('azd up' from labs/ai-gateway-sandbox).
// Creates the resource group, deploys the lab (../main.bicep, the same template the notebook uses) and hosts the
// demo UI on Azure Container Apps behind Microsoft Entra ID sign-in.
targetScope = 'subscription'

@minLength(1)
@maxLength(64)
@description('Name of the azd environment, used to name the resource group')
param environmentName string

@description('Region of the resource group, API Management, monitoring and the container registry')
param location string

@description('Object ID of the user running azd: the only user allowed to sign in to the demo UI (add more in Entra ID > Enterprise applications)')
param principalId string = ''

@description('Image of the demo UI, set by azd deploy. Empty on the first provision (a placeholder image is used).')
param uiImageName string = ''

@description('Service tree ID for the Entra ID app registration, required by some tenants (leave empty otherwise)')
param serviceManagementReference string = ''

// Written by the preprovision hook (infra/scripts/build_config.py) from ../sandbox-config.json
var config = loadJsonContent('sandbox.generated.json')
var lab = config.labParameters
var tags = { 'azd-env-name': environmentName }

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-${environmentName}'
  location: location
  tags: tags
}

// Created first so the lab can approve it as a gateway caller (entra-identity fragment)
module uiIdentity 'ui-identity.bicep' = {
  name: 'uiIdentity'
  scope: rg
  params: {
    location: lab.agentLocation
  }
}

module sandbox '../main.bicep' = {
  name: 'sandbox'
  scope: rg
  params: {
    apimSku: lab.apimSku
    aiServicesConfig: lab.aiServicesConfig
    modelsConfig: lab.modelsConfig
    modelPricing: lab.modelPricing
    toolPricing: lab.toolPricing
    agentPricing: lab.agentPricing
    agentLocation: lab.agentLocation
    tiersConfig: lab.tiersConfig
    agentsConfig: lab.agentsConfig
    inferenceAPIPath: lab.inferenceAPIPath
    inferenceAPIType: lab.inferenceAPIType
    foundryProjectName: lab.foundryProjectName
    uiClientId: uiIdentity.outputs.clientId
  }
}

// Same content as src/demo-config.private.config written by step 3 of the notebook
var demoConfig = union(config.uiConfig, {
  inferenceBaseUrl: sandbox.outputs.inferenceBaseUrl
  mcpUrl: sandbox.outputs.mcpUrl
  a2aUrl: sandbox.outputs.a2aUrl
  agentCardUrl: sandbox.outputs.agentCardUrl
  apimServiceId: sandbox.outputs.apimServiceId
  appInsightsId: sandbox.outputs.appInsightsId
  appInsightsAppId: sandbox.outputs.appInsightsAppId
  workbookId: sandbox.outputs.workbookId
  agents: sandbox.outputs.agentKeys
  tenantId: sandbox.outputs.tenantId
  logAnalyticsWorkspaceId: sandbox.outputs.logAnalyticsWorkspaceId
  logAnalyticsResourceId: sandbox.outputs.logAnalyticsResourceId
  agentIdentityClientId: sandbox.outputs.agentIdentityClientId
  foundryBackends: sandbox.outputs.foundryBackends
})

module ui 'ui.bicep' = {
  name: 'ui'
  scope: rg
  params: {
    location: lab.agentLocation
    registryLocation: location
    containerAppsEnvironmentId: sandbox.outputs.containerAppsEnvironmentId
    identityName: uiIdentity.outputs.name
    imageName: uiImageName
    demoConfig: string(demoConfig)
    tags: tags
  }
}

var issuer = '${environment().authentication.loginEndpoint}${tenant().tenantId}/v2.0'

module uiAuth 'ui-auth.bicep' = {
  name: 'uiAuth'
  scope: rg
  params: {
    containerAppName: ui.outputs.name
    appUrl: ui.outputs.uri
    identityPrincipalId: uiIdentity.outputs.principalId
    issuer: issuer
    environmentName: environmentName
    principalId: principalId
    serviceManagementReference: serviceManagementReference
  }
}

output AZURE_LOCATION string = location
output AZURE_TENANT_ID string = tenant().tenantId
output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = ui.outputs.registryLoginServer
output AZURE_CONTAINER_REGISTRY_NAME string = ui.outputs.registryName
output SERVICE_UI_NAME string = ui.outputs.name
output SERVICE_UI_URI string = ui.outputs.uri
output UI_APP_REGISTRATION_CLIENT_ID string = uiAuth.outputs.clientId
output APIM_GATEWAY_URL string = sandbox.outputs.apimResourceGatewayURL
output INFERENCE_BASE_URL string = sandbox.outputs.inferenceBaseUrl
output MCP_URL string = sandbox.outputs.mcpUrl
output A2A_URL string = sandbox.outputs.a2aUrl
output WORKBOOK_ID string = sandbox.outputs.workbookId
