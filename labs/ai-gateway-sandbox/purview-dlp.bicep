// AI Gateway Sandbox - OPTIONAL Microsoft Purview DLP for "Blocked before spend" (purviewDlp.enabled = true).
//
// Creates the named values the purview-dlp policy fragment uses for the On-Behalf-Of exchange and the Graph
// processContent call. The Entra app registration, its Graph delegated permissions (Content.Process.User,
// ProtectionScopes.Compute.User, ContentActivity.Write) with admin consent, and the Purview DLP policy for custom
// AI apps are tenant-level prerequisites that this template does not create (see the lab README).

@description('Name of the existing API Management service')
param apimName string

@description('Entra ID tenant of the users and of the gateway app registration')
param tenantId string = tenant().tenantId

@description('Client ID of the gateway app registration (audience of the user tokens sent in x-user-assertion)')
param clientId string

@secure()
@description('Client secret of the gateway app registration (used for the On-Behalf-Of exchange)')
param clientSecret string

@description('Microsoft Graph host')
param graphHost string = 'graph.microsoft.com'

resource apim 'Microsoft.ApiManagement/service@2024-06-01-preview' existing = {
  name: apimName
}

resource tenantNamedValue 'Microsoft.ApiManagement/service/namedValues@2024-06-01-preview' = {
  parent: apim
  name: 'purview-tenant-id'
  properties: {
    displayName: 'purview-tenant-id'
    value: tenantId
  }
}

resource clientIdNamedValue 'Microsoft.ApiManagement/service/namedValues@2024-06-01-preview' = {
  parent: apim
  name: 'purview-client-id'
  properties: {
    displayName: 'purview-client-id'
    value: clientId
  }
}

// In production, store the secret in Key Vault and reference it (keyVault.secretIdentifier) instead
resource clientSecretNamedValue 'Microsoft.ApiManagement/service/namedValues@2024-06-01-preview' = {
  parent: apim
  name: 'purview-client-secret'
  properties: {
    displayName: 'purview-client-secret'
    value: clientSecret
    secret: true
  }
}

resource graphHostNamedValue 'Microsoft.ApiManagement/service/namedValues@2024-06-01-preview' = {
  parent: apim
  name: 'purview-graph-host'
  properties: {
    displayName: 'purview-graph-host'
    value: graphHost
  }
}

output namedValues array = [
  tenantNamedValue.name
  clientIdNamedValue.name
  clientSecretNamedValue.name
  graphHostNamedValue.name
]
