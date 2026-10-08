// Workload identity of the hosted demo UI: pulls its image, signs users in (federated credential) and calls the
// gateway, Azure Resource Manager and Azure Monitor with its own tokens (no Azure CLI in the container).
param location string = resourceGroup().location

var resourceSuffix = uniqueString(subscription().id, resourceGroup().id)

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-sandbox-ui-${resourceSuffix}'
  location: location
}

output name string = identity.name
output clientId string = identity.properties.clientId
output principalId string = identity.properties.principalId
