// AI Gateway Sandbox - Entra-only mode identities (optional, entraModeConfig.enabled).
//
// Everything the key-less models API (entra/inference) needs in Microsoft Entra ID:
//   - a gateway app registration: the token audience (its appId, v2 tokens), the Gateway.Invoke app role every caller
//     needs, the groups claim (groupMembershipClaims) and a user_impersonation scope pre-authorized for the Azure CLI
//   - one security group per team: the gateway maps the group to a plan and a shared $ budget
//   - one user-assigned managed identity per persona, a member of its team's group and assigned Gateway.Invoke DIRECTLY
//     (Entra ID doesn't put app roles inherited through a group into a service principal's token)
//   - optionally the deployer (a real user) in one group, for the Azure CLI path of a locally run UI
extension microsoftGraphV1

param location string
param resourceSuffix string

@description('entraModeConfig from sandbox-config.json (built by build_config.py): groups (name, displayName, tier, poolMicroUsd, memberMicroUsd, costCenter), identities (name, displayName, group), groupMembershipClaims, includeDeployerIn')
param config object

@description('Tier names in order of preference (the first tier wins when a caller is in several gateway groups)')
param tierNames array

@description('Object ID of the deployer (azd principalId); used only when config.includeDeployerIn names a group')
param deployerPrincipalId string = ''

param serviceManagementReference string = ''

var groupsConfig = config.?groups ?? []
var identitiesConfig = config.?identities ?? []
var deployerGroup = empty(deployerPrincipalId) ? '' : (config.?includeDeployerIn ?? '')
var azureCliAppId = '04b07795-8ddb-461a-bbee-02f9e1bf7b46'
var invokeRoleId = guid(resourceGroup().id, resourceSuffix, 'entra-gateway-invoke')
var scopeId = guid(resourceGroup().id, resourceSuffix, 'entra-gateway-user-impersonation')

module personaIdentities 'entra-personas.bicep' = {
  name: 'entraPersonaIdentities'
  params: {
    location: location
    resourceSuffix: resourceSuffix
    identities: identitiesConfig
  }
}

var personaOutputs = personaIdentities.outputs.personas

resource gatewayApp 'Microsoft.Graph/applications@v1.0' = {
  uniqueName: 'ai-gateway-sandbox-gateway-${resourceSuffix}'
  displayName: 'AI Gateway Sandbox gateway (${resourceSuffix})'
  signInAudience: 'AzureADMyOrg'
  serviceManagementReference: empty(serviceManagementReference) ? null : serviceManagementReference
  groupMembershipClaims: config.?groupMembershipClaims ?? 'SecurityGroup'
  api: {
    requestedAccessTokenVersion: 2
    oauth2PermissionScopes: [
      {
        id: scopeId
        value: 'user_impersonation'
        type: 'User'
        isEnabled: true
        adminConsentDisplayName: 'Call the AI Gateway Sandbox'
        adminConsentDescription: 'Call the AI Gateway Sandbox models API on behalf of the signed-in user'
        userConsentDisplayName: 'Call the AI Gateway Sandbox'
        userConsentDescription: 'Call the AI Gateway Sandbox models API on your behalf'
      }
    ]
    preAuthorizedApplications: empty(deployerGroup) ? [] : [
      {
        appId: azureCliAppId
        delegatedPermissionIds: [scopeId]
      }
    ]
  }
  appRoles: [
    {
      id: invokeRoleId
      value: 'Gateway.Invoke'
      displayName: 'Gateway.Invoke'
      description: 'Call the AI Gateway Sandbox key-less models API (the plan and $ budget come from the caller\'s security group)'
      allowedMemberTypes: ['User', 'Application']
      isEnabled: true
    }
  ]
}

resource gatewayServicePrincipal 'Microsoft.Graph/servicePrincipals@v1.0' = {
  appId: gatewayApp.appId
}

resource teamGroups 'Microsoft.Graph/groups@v1.0' = [for group in groupsConfig: {
  uniqueName: 'ai-gateway-sandbox-${group.name}-${resourceSuffix}'
  displayName: 'AI Gateway Sandbox ${group.displayName} (${resourceSuffix})'
  description: 'AI Gateway Sandbox: members call the gateway on the ${group.tier} plan and share a team $ budget'
  mailEnabled: false
  mailNickname: 'aigw-${group.name}-${take(resourceSuffix, 8)}'
  securityEnabled: true
  members: {
    relationships: concat(
      filter(map(range(0, length(identitiesConfig)), i => (identitiesConfig[i].?group ?? '') == group.name ? personaOutputs[i].principalId : ''), id => !empty(id)),
      deployerGroup == group.name ? [deployerPrincipalId] : [])
  }
}]

resource personaRoles 'Microsoft.Graph/appRoleAssignedTo@v1.0' = [for (identity, i) in identitiesConfig: {
  appRoleId: invokeRoleId
  principalId: personaOutputs[i].principalId
  resourceId: gatewayServicePrincipal.id
}]

resource deployerRole 'Microsoft.Graph/appRoleAssignedTo@v1.0' = if (!empty(deployerGroup)) {
  appRoleId: invokeRoleId
  principalId: deployerPrincipalId
  resourceId: gatewayServicePrincipal.id
}

// "<group object id>=<name>|<tier>|<rank>|<pool micro-USD>|<member micro-USD>" per group: main.bicep joins them into
// the entra-gateway-groups named value
output groupEntries array = [for (group, i) in groupsConfig: '${teamGroups[i].id}=${group.name}|${group.tier}|${indexOf(tierNames, group.tier)}|${group.poolMicroUsd}|${group.memberMicroUsd}']

// "<object id>=<persona name>;..." (the entra-gateway-members named value): names the caller in headers and chargeback
output membersValue string = join(concat(
  map(range(0, length(identitiesConfig)), i => '${personaOutputs[i].principalId}=${identitiesConfig[i].name}'),
  empty(deployerGroup) ? [] : ['${deployerPrincipalId}=entra-you']), ';')

output gatewayAppId string = gatewayApp.appId

output personas array = concat(map(range(0, length(identitiesConfig)), i => {
  name: identitiesConfig[i].name
  displayName: identitiesConfig[i].displayName
  group: identitiesConfig[i].?group ?? ''
  tier: empty(identitiesConfig[i].?group ?? '') ? '' : filter(groupsConfig, g => g.name == identitiesConfig[i].group)[0].tier
  kind: 'managed-identity'
  clientId: personaOutputs[i].clientId
  resourceId: personaOutputs[i].resourceId
}), empty(deployerGroup) ? [] : [{
  name: 'entra-you'
  displayName: 'You (Azure CLI)'
  group: deployerGroup
  tier: filter(groupsConfig, g => g.name == deployerGroup)[0].tier
  kind: 'azure-cli'
  clientId: ''
  resourceId: ''
}])
