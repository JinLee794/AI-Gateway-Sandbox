// Private network of the azd deployment: the Container Apps environment runs in a VNet so the demo UI reaches its
// run-history Storage account through a private endpoint (tenants may force publicNetworkAccess=Disabled on Storage).
param location string
param tags object = {}

var resourceSuffix = uniqueString(subscription().id, resourceGroup().id)

resource nsg 'Microsoft.Network/networkSecurityGroups@2024-01-01' = {
  name: 'nsg-sandbox-${resourceSuffix}'
  location: location
  tags: tags
  properties: { securityRules: [] }  // the default rules: VNet and load balancer in, internet out
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-01-01' = {
  name: 'vnet-sandbox-${resourceSuffix}'
  location: location
  tags: tags
  properties: {
    addressSpace: { addressPrefixes: [ '10.40.0.0/16' ] }
    subnets: [
      {
        // Container Apps environment with workload profiles: /27 minimum, delegated
        name: 'snet-aca'
        properties: {
          addressPrefix: '10.40.0.0/23'
          networkSecurityGroup: { id: nsg.id }
          delegations: [ { name: 'aca', properties: { serviceName: 'Microsoft.App/environments' } } ]
        }
      }
      {
        name: 'snet-pe'
        properties: {
          addressPrefix: '10.40.2.0/27'
          networkSecurityGroup: { id: nsg.id }
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
    ]
  }
}

resource blobZone 'Microsoft.Network/privateDnsZones@2020-06-01' = {
  name: 'privatelink.blob.${environment().suffixes.storage}'
  location: 'global'
  tags: tags
}

resource blobZoneLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = {
  parent: blobZone
  name: 'link-${vnet.name}'
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: vnet.id }
  }
}

output containerAppsSubnetId string = vnet.properties.subnets[0].id
output privateEndpointSubnetId string = vnet.properties.subnets[1].id
output blobPrivateDnsZoneId string = blobZone.id
