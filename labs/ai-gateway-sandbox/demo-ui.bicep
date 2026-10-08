// Hosts the AI Gateway Sandbox demo UI (src/app.py) on Azure App Service, protected by App Service authentication
// (Easy Auth) so only users of this Microsoft Entra tenant can open it. No secrets: Easy Auth signs users in with an
// app registration whose credential is the UI's user-assigned managed identity (federated identity credential), and
// app.py uses the same identity for the gateway's Entra ID token, the APIM trace / policy read-back and Azure Monitor queries.
extension microsoftGraphV1

param location string
param resourceSuffix string
@allowed(['B1', 'B2', 'B3', 'P0v3', 'P1v3'])
param sku string = 'B1'
param identityId string
param identityClientId string
param identityPrincipalId string
param apimName string
param logAnalyticsName string
param appInsightsName string
param tenantId string = tenant().tenantId

var siteName = 'app-sandbox-ui-${resourceSuffix}'
var issuer = '${environment().authentication.loginEndpoint}${tenantId}/v2.0'
var roles = {
  apimServiceContributor: '312a565d-c81f-4fd8-895a-4e21e48d571c'
  logAnalyticsReader: '73c42c96-874c-492b-b04d-ab87d138a893'
  monitoringReader: '43d0d8ad-25c7-4714-9337-8ba259a9fe05'
}

// Sign-in app registration: single tenant (AzureADMyOrg), Easy Auth callback, managed identity as its only credential
resource uiApp 'Microsoft.Graph/applications@v1.0' = {
  displayName: 'AI Gateway Sandbox demo UI (${siteName})'
  uniqueName: 'ai-gateway-sandbox-ui-${resourceSuffix}'
  signInAudience: 'AzureADMyOrg'
  web: {
    redirectUris: [
      'https://${siteName}.azurewebsites.net/.auth/login/aad/callback'
    ]
    implicitGrantSettings: {
      enableIdTokenIssuance: true
    }
  }
  requiredResourceAccess: [
    {
      resourceAppId: '00000003-0000-0000-c000-000000000000' // Microsoft Graph
      resourceAccess: [
        {
          id: 'e1fe6dd8-ba31-4d61-89e7-88639da4683d' // User.Read
          type: 'Scope'
        }
      ]
    }
  ]

  resource fic 'federatedIdentityCredentials@v1.0' = {
    name: '${uiApp.uniqueName}/demo-ui-managed-identity'
    description: 'The demo UI managed identity is the sign-in credential of App Service authentication (no client secret)'
    audiences: [
      'api://AzureADTokenExchange'
    ]
    issuer: issuer
    subject: identityPrincipalId
  }
}

resource uiServicePrincipal 'Microsoft.Graph/servicePrincipals@v1.0' = {
  appId: uiApp.appId
}

resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: 'plan-sandbox-ui-${resourceSuffix}'
  location: location
  kind: 'linux'
  sku: {
    name: sku
  }
  properties: {
    reserved: true
  }
}

resource site 'Microsoft.Web/sites@2024-04-01' = {
  name: siteName
  location: location
  kind: 'app,linux'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityId}': {}
    }
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.12'
      appCommandLine: 'python app.py --host 0.0.0.0 --port 8000'
      alwaysOn: true
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      http20Enabled: true
      appSettings: [
        { name: 'WEBSITES_PORT', value: '8000' }
        { name: 'SCM_DO_BUILD_DURING_DEPLOYMENT', value: 'false' }
        { name: 'AZURE_CLIENT_ID', value: identityClientId }
        { name: 'OVERRIDE_USE_MI_FIC_ASSERTION_CLIENTID', value: identityClientId }
      ]
    }
  }
}

// Easy Auth: every request needs a signed-in user of this tenant; anonymous browsers are sent to the Entra ID sign-in page
resource auth 'Microsoft.Web/sites/config@2024-04-01' = {
  parent: site
  name: 'authsettingsV2'
  properties: {
    platform: {
      enabled: true
    }
    globalValidation: {
      requireAuthentication: true
      unauthenticatedClientAction: 'RedirectToLoginPage'
      redirectToProvider: 'azureactivedirectory'
      excludedPaths: [ '/healthz' ] // readiness probe only (returns {"ok": true}); everything else requires a tenant sign-in
    }
    httpSettings: {
      requireHttps: true
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: uiApp.appId
          clientSecretSettingName: 'OVERRIDE_USE_MI_FIC_ASSERTION_CLIENTID'
          openIdIssuer: issuer
        }
        validation: {
          allowedAudiences: [
            'api://${uiApp.appId}'
          ]
        }
      }
    }
    login: {
      tokenStore: {
        enabled: true
      }
    }
  }
}

// What the UI does with your Azure CLI login when it runs locally, it does with this identity when hosted
resource apim 'Microsoft.ApiManagement/service@2024-06-01-preview' existing = {
  name: apimName
}
resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logAnalyticsName
}
resource appInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: appInsightsName
}

// Request tracing (listDebugCredentials / listTrace), policy read-back and the "Reset budgets" named value
resource apimRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: apim
  name: guid(apim.id, identityPrincipalId, roles.apimServiceContributor)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.apimServiceContributor)
    principalId: identityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// Policies & evidence and Chargeback queries (Log Analytics)
resource workspaceRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: workspace
  name: guid(workspace.id, identityPrincipalId, roles.logAnalyticsReader)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.logAnalyticsReader)
    principalId: identityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// Cost and token charts (Application Insights query API)
resource appInsightsRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: appInsights
  name: guid(appInsights.id, identityPrincipalId, roles.monitoringReader)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.monitoringReader)
    principalId: identityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

output url string = 'https://${site.properties.defaultHostName}'
output name string = site.name
output appId string = uiApp.appId
