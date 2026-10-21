// Hosting of the demo UI (src/app.py): container registry and Container App in the lab's Container Apps environment.
param location string
param registryLocation string
param containerAppsEnvironmentId string
param identityName string

@description('Subnet of the run-history Storage private endpoint (the Container Apps environment\'s VNet)')
param privateEndpointSubnetId string

@description('privatelink.blob private DNS zone linked to that VNet')
param blobPrivateDnsZoneId string

@description('Entra-only mode persona identities (user-assigned managed identity resource IDs) the UI calls the key-less API as')
param extraIdentityIds array = []

@description('Image set by azd deploy; empty until the first deploy')
param imageName string = ''

@secure()
@description('Demo UI configuration (JSON), same content as src/demo-config.private.config')
param demoConfig string

param tags object = {}

var resourceSuffix = uniqueString(subscription().id, resourceGroup().id)
var roles = {
  acrPull: '7f951dda-4ed3-4680-a7ca-43fe172d538d'
  apimServiceContributor: '312a565d-c81f-4fd8-895a-4e21e48d571c' // debug traces, policies, budget reset
  logAnalyticsReader: '73c42c96-874c-492b-b04d-ab87d138a893' // chargeback and telemetry queries
  monitoringReader: '43d0d8ad-25c7-4714-9337-8ba259a9fe05' // Application Insights queries
  storageBlobDataContributor: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe' // run history
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: identityName
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: 'crsandbox${resourceSuffix}'
  location: registryLocation
  tags: tags
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false
  }
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, identity.id, roles.acrPull)
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPull)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource rgRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for role in ['apimServiceContributor', 'logAnalyticsReader', 'monitoringReader']: {
  name: guid(resourceGroup().id, identity.id, roles[role])
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles[role])
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}]

// Run history: the container's disk is wiped by every deploy, revision or restart, so each signed-in user's runs are
// blobs here (run-history/<Entra object id>/<run id>.json), written by the UI's managed identity. Entra ID auth only,
// and private: the UI reaches it through a private endpoint in the Container Apps environment's VNet.
resource historyStorage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'sthist${resourceSuffix}'
  location: location
  tags: tags
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    allowSharedKeyAccess: false
    allowBlobPublicAccess: false
    defaultToOAuthAuthentication: true
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: 'Disabled'
  }
}

resource historyEndpoint 'Microsoft.Network/privateEndpoints@2024-01-01' = {
  name: 'pe-${historyStorage.name}-blob'
  location: location
  tags: tags
  properties: {
    subnet: { id: privateEndpointSubnetId }
    privateLinkServiceConnections: [
      { name: 'blob', properties: { privateLinkServiceId: historyStorage.id, groupIds: [ 'blob' ] } }
    ]
  }
}

resource historyEndpointDns 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-01-01' = {
  parent: historyEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [ { name: 'blob', properties: { privateDnsZoneId: blobPrivateDnsZoneId } } ]
  }
}

resource historyBlobs 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: historyStorage
  name: 'default'
}

resource historyContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: historyBlobs
  name: 'run-history'
  properties: { publicAccess: 'None' }
}

resource historyRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(historyContainer.id, identity.id, roles.storageBlobDataContributor)
  scope: historyContainer
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.storageBlobDataContributor)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'sandbox-ui-${resourceSuffix}'
  location: location
  tags: union(tags, { 'azd-service-name': 'ui' })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: reduce(extraIdentityIds, { '${identity.id}': {} }, (identities, id) => union(identities, { '${id}': {} }))
  }
  properties: {
    managedEnvironmentId: containerAppsEnvironmentId
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 80
        transport: 'auto'
      }
      registries: [
        {
          server: registry.properties.loginServer
          identity: identity.id
        }
      ]
      secrets: [
        { name: 'demo-config', value: demoConfig }
        // Lets Easy Auth sign users in with a federated credential of the managed identity (no client secret)
        { name: 'override-use-mi-fic-assertion-client-id', value: identity.properties.clientId }
      ]
    }
    template: {
      containers: [
        {
          name: 'ui'
          image: empty(imageName) ? 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest' : imageName
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: [
            { name: 'DEMO_CONFIG', secretRef: 'demo-config' }
            { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
            { name: 'HOST', value: '0.0.0.0' }
            { name: 'PORT', value: '80' }
            { name: 'HISTORY_BLOB_URL', value: '${historyStorage.properties.primaryEndpoints.blob}${historyContainer.name}' }
          ]
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
  dependsOn: [acrPull, historyRole, historyEndpointDns]
}

output name string = app.name
output uri string = 'https://${app.properties.configuration.ingress.fqdn}'
output registryName string = registry.name
output registryLoginServer string = registry.properties.loginServer
