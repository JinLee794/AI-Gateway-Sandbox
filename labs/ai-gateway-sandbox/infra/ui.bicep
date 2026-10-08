// Hosting of the demo UI (src/app.py): container registry and Container App in the lab's Container Apps environment.
param location string
param registryLocation string
param containerAppsEnvironmentId string
param identityName string

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

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'sandbox-ui-${resourceSuffix}'
  location: location
  tags: union(tags, { 'azd-service-name': 'ui' })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
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
          ]
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
  dependsOn: [acrPull]
}

output name string = app.name
output uri string = 'https://${app.properties.configuration.ingress.fqdn}'
output registryName string = registry.name
output registryLoginServer string = registry.properties.loginServer
