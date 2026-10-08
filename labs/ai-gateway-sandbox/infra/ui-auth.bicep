// Microsoft Entra ID sign-in (Container Apps built-in authentication) for the demo UI, without a client secret:
// the app registration trusts a federated credential of the UI managed identity.
extension microsoftGraphV1

param containerAppName string
param appUrl string
param identityPrincipalId string
param issuer string
param environmentName string

@description('Object ID of the user allowed to sign in (empty: any user of the tenant)')
param principalId string = ''

param serviceManagementReference string = ''

var resourceSuffix = uniqueString(subscription().id, resourceGroup().id)

resource app 'Microsoft.App/containerApps@2024-03-01' existing = {
  name: containerAppName
}

resource appRegistration 'Microsoft.Graph/applications@v1.0' = {
  uniqueName: 'ai-gateway-sandbox-ui-${resourceSuffix}'
  displayName: 'AI Gateway Sandbox UI (${environmentName})'
  signInAudience: 'AzureADMyOrg'
  serviceManagementReference: empty(serviceManagementReference) ? null : serviceManagementReference
  web: {
    redirectUris: ['${appUrl}/.auth/login/aad/callback']
    implicitGrantSettings: { enableIdTokenIssuance: true }
  }
  requiredResourceAccess: [
    {
      resourceAppId: '00000003-0000-0000-c000-000000000000' // Microsoft Graph
      resourceAccess: [{ id: 'e1fe6dd8-ba31-4d61-89e7-88639da4683d', type: 'Scope' }] // User.Read
    }
  ]

  resource fic 'federatedIdentityCredentials@v1.0' = {
    name: '${appRegistration.uniqueName}/sandbox-ui-msi'
    audiences: ['api://AzureADTokenExchange']
    issuer: issuer
    subject: identityPrincipalId
  }
}

resource servicePrincipal 'Microsoft.Graph/servicePrincipals@v1.0' = {
  appId: appRegistration.appId
  // Only users assigned to the enterprise application can sign in
  appRoleAssignmentRequired: !empty(principalId)
}

resource userAssignment 'Microsoft.Graph/appRoleAssignedTo@v1.0' = if (!empty(principalId)) {
  appRoleId: '00000000-0000-0000-0000-000000000000' // default access
  principalId: principalId
  resourceId: servicePrincipal.id
}

resource authConfig 'Microsoft.App/containerApps/authConfigs@2024-10-02-preview' = {
  parent: app
  name: 'current'
  properties: {
    platform: { enabled: true }
    globalValidation: {
      unauthenticatedClientAction: 'RedirectToLoginPage'
      redirectToProvider: 'azureactivedirectory'
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: appRegistration.appId
          clientSecretSettingName: 'override-use-mi-fic-assertion-client-id'
          openIdIssuer: issuer
        }
        validation: {
          defaultAuthorizationPolicy: {
            allowedApplications: []
          }
        }
      }
    }
  }
}

output clientId string = appRegistration.appId
