// AI Gateway Sandbox - "Over budget, switched off" (the FinOps operations loop of labs/finops-framework).
//
// The first blocked subscription-budget request notifies this Logic App directly. No log ingestion or scheduled
// spend scan is needed. quota-by-key still blocks spend; suspension revokes the key across all APIs.
// The callback URL is stored only in a secret APIM named value, never in deployment outputs or the demo UI.
//
// Reset budgets (UI / notebook) writes a new budget-epoch and re-activates suspended subscriptions. The Logic App only
// skips old-epoch notifications. Epoch and subscription updates are separate ARM operations; concurrent resets
// and already executing suspensions are not atomic (see the README).

param location string = resourceGroup().location
param resourceSuffix string
param apimName string
param logAnalyticsId string
param appInsightsName string

@description('Watched subscriptions: [{ name, limitMicroUsd }]. Only these subscriptions may be suspended.')
@minLength(1)
param watchedSubscriptions array

@description('Legacy compatibility only: gateway-triggered suspension supports exhaustion (100%) only.')
@allowed([100])
param suspendAtPercent int = 100

@description('true: custom least-privilege role (needs Owner or User Access Administrator). false: built-in API Management Service Contributor')
param useCustomRole bool = true

@description('Legacy compatibility only: email notifications need a separate workflow integration.')
@allowed([[]])
#disable-next-line no-unused-params
param notifyEmails array = []

@description('Optional principal (the hosted demo UI identity) that may read the Logic App run history')
param readerPrincipalId string = ''

var apimApiVersion = '2024-06-01-preview'
var triggerName = 'When_a_budget_is_exhausted'
var roles = {
  apimServiceContributor: '312a565d-c81f-4fd8-895a-4e21e48d571c'
  monitoringReader: '43d0d8ad-25c7-4714-9337-8ba259a9fe05'
}

resource apim 'Microsoft.ApiManagement/service@2024-06-01-preview' existing = {
  name: apimName
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: appInsightsName
}

// App Insights ingestion endpoint (from the connection string) for the audit event
var ingestionEndpoint = replace(filter(split(appInsights.properties.ConnectionString, ';'), part => startsWith(part, 'IngestionEndpoint='))[0], 'IngestionEndpoint=', '')

// 1. Least privilege: the Logic App may only read and change APIM subscriptions and read the budget-epoch named value
var customRoleName = guid(resourceGroup().id, 'ai-gateway-sandbox-budget-suspend')
resource suspenderRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = if (useCustomRole) {
  name: customRoleName
  properties: {
    roleName: 'AI Gateway Sandbox budget suspender (${resourceGroup().name})'
    description: 'Lets the budget-breach Logic App of the AI Gateway Sandbox suspend APIM subscriptions: read the service, read and update subscriptions, read named values.'
    type: 'CustomRole'
    permissions: [
      {
        actions: [
          'Microsoft.ApiManagement/service/read'
          'Microsoft.ApiManagement/service/subscriptions/read'
          'Microsoft.ApiManagement/service/subscriptions/write'
          'Microsoft.ApiManagement/service/namedValues/read'
        ]
        notActions: []
      }
    ]
    assignableScopes: [
      resourceGroup().id
    ]
  }
}

var roleDefinitionId = useCustomRole
  ? subscriptionResourceId('Microsoft.Authorization/roleDefinitions', customRoleName)
  : subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.apimServiceContributor)

// 2. Logic App: suspend the subscription that went over budget, then write the audit event
var apimArm = uri(environment().resourceManager, apim.id)

resource workflow 'Microsoft.Logic/workflows@2019-05-01' = {
  name: 'la-budget-suspend-${resourceSuffix}'
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    state: 'Enabled'
    definition: {
      '$schema': 'https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#'
      contentVersion: '1.0.0.0'
      triggers: {
        '${triggerName}': {
          type: 'Request'
          kind: 'Http'
          // Rejects (400) any body that doesn't match the schema, e.g. a subscription that isn't watched
          operationOptions: 'EnableSchemaValidation'
          runtimeConfiguration: {
            concurrency: {
              runs: 1
            }
          }
          inputs: {
            schema: {
              type: 'object'
              required: [
                'subscription'
                'epoch'
                'requestId'
                'source'
                'reason'
                'stage'
              ]
              properties: {
                subscription: {
                  type: 'string'
                  enum: map(watchedSubscriptions, s => s.name)
                }
                epoch: {
                  type: 'string'
                  minLength: 1
                }
                requestId: {
                  type: 'string'
                  minLength: 1
                }
                source: {
                  type: 'string'
                  enum: ['quota-by-key']
                }
                reason: {
                  type: 'string'
                  enum: ['QuotaExceeded']
                }
                stage: {
                  type: 'string'
                  enum: ['subscription-budget']
                }
              }
            }
          }
        }
      }
      actions: {
        Breach_subscription: {
          runAfter: {}
          type: 'Compose'
          inputs: '@triggerBody()[\'subscription\']'
        }
        Breach_epoch: {
          runAfter: {}
          type: 'Compose'
          inputs: '@triggerBody()[\'epoch\']'
        }
        Get_current_epoch: {
          runAfter: {}
          type: 'Http'
          inputs: {
            method: 'GET'
            uri: '${apimArm}/namedValues/budget-epoch?api-version=${apimApiVersion}'
            authentication: {
              type: 'ManagedServiceIdentity'
              audience: environment().resourceManager
            }
          }
        }
        Get_subscription: {
          runAfter: {
            Breach_subscription: [
              'Succeeded'
            ]
          }
          type: 'Http'
          inputs: {
            method: 'GET'
            uri: '${apimArm}/subscriptions/@{encodeUriComponent(outputs(\'Breach_subscription\'))}?api-version=${apimApiVersion}'
            authentication: {
              type: 'ManagedServiceIdentity'
              audience: environment().resourceManager
            }
          }
        }
        Decision: {
          runAfter: {
            Breach_epoch: [
              'Succeeded'
            ]
            Get_current_epoch: [
              'Succeeded'
              'Failed'
              'TimedOut'
            ]
            Get_subscription: [
              'Succeeded'
              'Failed'
              'TimedOut'
            ]
          }
          type: 'Compose'
          inputs: '@if(not(equals(actions(\'Get_subscription\')?[\'status\'], \'Succeeded\')), concat(\'failed: could not read subscription \', outputs(\'Breach_subscription\')), if(not(equals(actions(\'Get_current_epoch\')?[\'status\'], \'Succeeded\')), \'failed: could not read the budget-epoch named value\', if(not(equals(string(outputs(\'Breach_epoch\')), string(body(\'Get_current_epoch\')?[\'properties\']?[\'value\']))), \'skipped: the budgets were reset after this breach (budget-epoch changed)\', if(not(equals(body(\'Get_subscription\')?[\'properties\']?[\'state\'], \'active\')), concat(\'skipped: the subscription is already \', body(\'Get_subscription\')?[\'properties\']?[\'state\']), \'suspend\'))))'
        }
        Suspend_if_over_budget: {
          runAfter: {
            Decision: [
              'Succeeded'
            ]
          }
          type: 'If'
          expression: {
            and: [
              {
                equals: [
                  '@outputs(\'Decision\')'
                  'suspend'
                ]
              }
            ]
          }
          actions: {
            Suspend_subscription: {
              runAfter: {}
              type: 'Http'
              inputs: {
                method: 'PATCH'
                uri: '${apimArm}/subscriptions/@{encodeUriComponent(outputs(\'Breach_subscription\'))}?api-version=${apimApiVersion}'
                headers: {
                  'Content-Type': 'application/json'
                  'If-Match': '@coalesce(outputs(\'Get_subscription\')?[\'headers\']?[\'ETag\'], body(\'Get_subscription\')?[\'etag\'])'
                }
                body: {
                  properties: {
                    state: 'suspended'
                    stateComment: 'Over budget: suspended after gateway quota exhaustion at @{utcNow()} (budget epoch @{outputs(\'Breach_epoch\')}, Logic App run @{workflow()?[\'run\']?[\'name\']}). Reset budgets re-activates it.'
                  }
                }
                authentication: {
                  type: 'ManagedServiceIdentity'
                  audience: environment().resourceManager
                }
              }
            }
          }
          else: {
            actions: {}
          }
        }
        Mark_failed_run: {
          runAfter: {
            Write_audit_event: [
              'Succeeded'
            ]
          }
          type: 'If'
          expression: '@or(startsWith(outputs(\'Decision\'), \'failed\'), not(equals(actions(\'Suspend_if_over_budget\')?[\'status\'], \'Succeeded\')))'
          actions: {
            Fail_suspension: {
              runAfter: {}
              type: 'Terminate'
              inputs: {
                runStatus: 'Failed'
                runError: {
                  code: 'BudgetSuspensionFailed'
                  message: 'Budget suspension failed. Inspect the ARM action results and the BudgetBreach audit event.'
                }
              }
            }
          }
          else: {
            actions: {}
          }
        }
        Write_audit_event: {
          runAfter: {
            Suspend_if_over_budget: [
              'Succeeded'
              'Failed'
              'TimedOut'
            ]
          }
          type: 'Http'
          inputs: {
            method: 'POST'
            uri: '${ingestionEndpoint}${endsWith(ingestionEndpoint, '/') ? '' : '/'}v2/track'
            headers: {
              'Content-Type': 'application/json'
            }
            body: {
              name: 'Microsoft.ApplicationInsights.Event'
              time: '@{utcNow()}'
              iKey: appInsights.properties.InstrumentationKey
              data: {
                baseType: 'EventData'
                baseData: {
                  ver: 2
                  name: 'BudgetBreach'
                  properties: {
                    action: '@{if(equals(outputs(\'Decision\'), \'suspend\'), if(equals(actions(\'Suspend_if_over_budget\')?[\'status\'], \'Succeeded\'), \'suspended\', \'failed\'), if(startsWith(outputs(\'Decision\'), \'failed\'), \'failed\', \'skipped\'))}'
                    reason: '@{outputs(\'Decision\')}'
                    subscription: '@{outputs(\'Breach_subscription\')}'
                    budgetEpoch: '@{outputs(\'Breach_epoch\')}'
                    currentEpoch: '@{body(\'Get_current_epoch\')?[\'properties\']?[\'value\']}'
                    previousState: '@{body(\'Get_subscription\')?[\'properties\']?[\'state\']}'
                    thresholdPercent: string(suspendAtPercent)
                    source: 'gateway'
                    requestId: '@{triggerBody()[\'requestId\']}'
                    quotaStage: '@{triggerBody()[\'stage\']}'
                    logicAppRun: '@{workflow()?[\'run\']?[\'name\']}'
                  }
                }
              }
            }
          }
        }
      }
      outputs: {}
    }
  }
}

resource workflowRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: apim
  name: guid(apim.id, workflow.id, roleDefinitionId)
  properties: {
    roleDefinitionId: roleDefinitionId
    principalId: workflow.identity.principalId
    principalType: 'ServicePrincipal'
  }

  dependsOn: [
    suspenderRole
  ]
}

resource callbackNamedValue 'Microsoft.ApiManagement/service/namedValues@2024-06-01-preview' = {
  parent: apim
  name: 'budget-suspend-callback'
  properties: {
    displayName: 'budget-suspend-callback'
    secret: true
    value: listCallbackUrl('${workflow.id}/triggers/${triggerName}', '2019-05-01').value
  }
  dependsOn: [
    workflowRole
  ]
}

// The hosted demo UI (notebook path) reads the run history to show it in the guided scenario
resource uiRunReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(readerPrincipalId)) {
  scope: workflow
  name: guid(workflow.id, readerPrincipalId, roles.monitoringReader)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.monitoringReader)
    principalId: readerPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// Logic App run history in Log Analytics (AzureDiagnostics, Category WorkflowRuntime)
resource workflowDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: workflow
  name: 'budgetSuspendDiagnostics'
  properties: {
    workspaceId: logAnalyticsId
    logs: [
      {
        categoryGroup: 'allLogs'
        enabled: true
      }
    ]
  }
}

// Migration tombstones: incremental deployments do not delete removed resources.
// Keep the legacy alert and action group disabled so an upgrade stops scans and callbacks immediately.
resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: 'ag-budget-suspend-${resourceSuffix}'
  location: 'Global'
  properties: {
    groupShortName: 'BudgetStop'
    enabled: false
    logicAppReceivers: []
    emailReceivers: []
  }
}

resource breachAlert 'Microsoft.Insights/scheduledQueryRules@2023-12-01' = {
  name: 'alert-budget-breach-${resourceSuffix}'
  location: location
  kind: 'LogAlert'
  properties: {
    displayName: 'AI Gateway Sandbox: legacy budget alert (disabled)'
    description: 'Disabled migration resource. Suspension is triggered directly by gateway subscription-budget exhaustion.'
    severity: 2
    enabled: false
    evaluationFrequency: 'PT5M'
    windowSize: 'PT5M'
    scopes: [
      logAnalyticsId
    ]
    targetResourceTypes: [
      'Microsoft.OperationalInsights/workspaces'
    ]
    skipQueryValidation: true
    criteria: {
      allOf: [
        {
          query: 'AppTraces | where false'
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    autoMitigate: true
    actions: {
      actionGroups: []
    }
  }
}

output logicAppName string = workflow.name
output logicAppId string = workflow.id
output roleDefinitionId string = roleDefinitionId
output customRole bool = useCustomRole
