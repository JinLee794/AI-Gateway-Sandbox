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

// EXPERIMENTAL - subnet of the optional Hyperlight executor VM (hyperlight.bicep): only the Container Apps subnet may call it
resource hlNsg 'Microsoft.Network/networkSecurityGroups@2024-01-01' = {
  name: 'nsg-hyperlight-${resourceSuffix}'
  location: location
  tags: tags
  properties: {
    securityRules: [
      {
        name: 'allow-executor-from-container-apps'
        properties: {
          priority: 100, direction: 'Inbound', access: 'Allow', protocol: 'Tcp'
          sourceAddressPrefix: '10.40.0.0/23', sourcePortRange: '*', destinationAddressPrefix: '*', destinationPortRange: '8080'
        }
      }
      {
        name: 'deny-other-vnet-inbound'
        properties: {
          priority: 4000, direction: 'Inbound', access: 'Deny', protocol: '*'
          sourceAddressPrefix: 'VirtualNetwork', sourcePortRange: '*', destinationAddressPrefix: '*', destinationPortRange: '*'
        }
      }
    ]
  }
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
      {
        name: 'snet-hyperlight'
        properties: {
          addressPrefix: '10.40.3.0/28'
          networkSecurityGroup: { id: hlNsg.id }
          defaultOutboundAccess: true  // the executor VM installs its packages from Ubuntu and PyPI
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
output hyperlightSubnetId string = vnet.properties.subnets[2].id
output blobPrivateDnsZoneId string = blobZone.id
