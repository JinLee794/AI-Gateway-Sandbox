// AI Gateway Sandbox - "Over budget, switched off" (the FinOps operations loop of labs/finops-framework).
//
// An Azure Monitor log search alert adds up the chargeback records the gateway writes (AppTraces, one per billable call)
// per subscription and budget epoch. When a watched subscription reaches suspendAtPercent of its plan budget, the
// alert calls a Logic App through an action group. The Logic App suspends the APIM subscription (its key stops working
// on every API until someone re-activates it), and writes a "BudgetBreach" audit event to Application Insights.
// The action group can also email people.
//
// This complements the in-gateway quota-by-key $ budget, it doesn't replace it:
//   - quota-by-key blocks the next call in real time (per call, per counter key).
//   - alert + suspend is an operations workflow that takes MINUTES (ingestion 1-3 min + evaluation frequency +
//     action group). It notifies, revokes the key everywhere, leaves an audit trail and catches spend the gateway
//     counters don't see.
//
// Reset budgets (UI / notebook) writes a new budget-epoch and re-activates suspended subscriptions. The Logic App only
// suspends when the breach belongs to the current epoch, so spend from before a reset never switches a user off again.

param location string = resourceGroup().location
param resourceSuffix string
param apimName string
param logAnalyticsId string
param appInsightsName string

@description('Watched subscriptions: [{ name, limitMicroUsd }] (limitMicroUsd = the plan $ budget in micro-USD)')
param watchedSubscriptions array

@description('Suspend when the spend of the current budget epoch reaches this percentage of the plan budget')
param suspendAtPercent int = 100

@description('true: custom least-privilege role (needs Owner or User Access Administrator). false: built-in API Management Service Contributor')
param useCustomRole bool = true

@description('Optional email addresses notified by the action group')
param notifyEmails array = []

@description('How often the alert runs. 5 minutes is the shortest frequency the aggregation query supports reliably.')
param evaluationFrequency string = 'PT5M'

@description('Optional principal (the hosted demo UI identity) that may read the Logic App run history')
param readerPrincipalId string = ''

var apimApiVersion = '2024-06-01-preview'
var triggerName = 'When_a_budget_alert_fires'
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
var dims = 'triggerBody()?[\'data\']?[\'alertContext\']?[\'condition\']?[\'allOf\']?[0]?[\'dimensions\']'
var essentials = 'triggerBody()?[\'data\']?[\'essentials\']'

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
          inputs: {
            schema: {}
          }
        }
      }
      actions: {
        Filter_subscription_dimension: {
          runAfter: {}
          type: 'Query'
          inputs: {
            from: '@coalesce(${dims}, json(\'[]\'))'
            where: '@equals(item()?[\'name\'], \'Subscription\')'
          }
        }
        Filter_epoch_dimension: {
          runAfter: {}
          type: 'Query'
          inputs: {
            from: '@coalesce(${dims}, json(\'[]\'))'
            where: '@equals(item()?[\'name\'], \'BudgetEpoch\')'
          }
        }
        Breach_subscription: {
          runAfter: {
            Filter_subscription_dimension: [
              'Succeeded'
            ]
          }
          type: 'Compose'
          inputs: '@coalesce(first(body(\'Filter_subscription_dimension\'))?[\'value\'], \'\')'
        }
        Breach_epoch: {
          runAfter: {
            Filter_epoch_dimension: [
              'Succeeded'
            ]
          }
          type: 'Compose'
          inputs: '@coalesce(first(body(\'Filter_epoch_dimension\'))?[\'value\'], \'\')'
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
          inputs: '@if(not(equals(${essentials}?[\'monitorCondition\'], \'Fired\')), \'skipped: the alert was resolved\', if(not(equals(actions(\'Get_subscription\')?[\'status\'], \'Succeeded\')), concat(\'failed: could not read subscription \', outputs(\'Breach_subscription\')), if(not(equals(actions(\'Get_current_epoch\')?[\'status\'], \'Succeeded\')), \'failed: could not read the budget-epoch named value\', if(not(equals(string(outputs(\'Breach_epoch\')), string(body(\'Get_current_epoch\')?[\'properties\']?[\'value\']))), \'skipped: the budgets were reset after this breach (budget-epoch changed)\', if(not(equals(body(\'Get_subscription\')?[\'properties\']?[\'state\'], \'active\')), concat(\'skipped: the subscription is already \', body(\'Get_subscription\')?[\'properties\']?[\'state\']), \'suspend\')))))'
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
                  'If-Match': '*'
                }
                body: {
                  properties: {
                    state: 'suspended'
                    stateComment: 'Over budget: suspended by the budget-breach alert at @{utcNow()} (budget epoch @{outputs(\'Breach_epoch\')}, Logic App run @{workflow()?[\'run\']?[\'name\']}). Reset budgets re-activates it.'
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
        Write_audit_event: {
          runAfter: {
            Suspend_if_over_budget: [
              'Succeeded'
              'Failed'
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
                    monitorCondition: '@{${essentials}?[\'monitorCondition\']}'
                    alertRule: '@{${essentials}?[\'alertRule\']}'
                    alertId: '@{${essentials}?[\'alertId\']}'
                    firedDateTime: '@{${essentials}?[\'firedDateTime\']}'
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

// 3. Action group: the Logic App plus optional email receivers
resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: 'ag-budget-suspend-${resourceSuffix}'
  location: 'Global'
  properties: {
    groupShortName: 'BudgetStop'
    enabled: true
    logicAppReceivers: [
      {
        name: 'suspend-subscription'
        resourceId: workflow.id
        callbackUrl: listCallbackUrl('${workflow.id}/triggers/${triggerName}', '2019-05-01').value
        useCommonAlertSchema: true
      }
    ]
    emailReceivers: [for (email, i) in notifyEmails: {
      name: 'email-${i}'
      emailAddress: email
      useCommonAlertSchema: true
    }]
  }
}

// 4. Log search alert: spend of the current budget epoch per watched subscription, against its plan budget
var limitRows = join(map(watchedSubscriptions, s => '\'${s.name}\', ${s.limitMicroUsd}'), ', ')
var breachQuery = 'let limits = datatable(Subscription: string, LimitMicroUsd: long) [${limitRows}];\nAppTraces\n| where tostring(Properties.record) == "chargeback"\n| extend Subscription = tostring(Properties.user), BudgetEpoch = tostring(Properties.budgetEpoch), CostMicroUsd = tolong(Properties.costMicroUsd)\n| where isnotempty(BudgetEpoch)\n| summarize SpendMicroUsd = sum(CostMicroUsd) by Subscription, BudgetEpoch\n| join kind=inner limits on Subscription\n| extend PercentOfLimit = round(100.0 * SpendMicroUsd / LimitMicroUsd, 1)\n| where PercentOfLimit >= ${suspendAtPercent}\n| project Subscription, BudgetEpoch, SpendMicroUsd, LimitMicroUsd, PercentOfLimit'

resource breachAlert 'Microsoft.Insights/scheduledQueryRules@2023-12-01' = {
  name: 'alert-budget-breach-${resourceSuffix}'
  location: location
  kind: 'LogAlert'
  properties: {
    displayName: 'AI Gateway Sandbox: over budget, switch off'
    description: 'Fires once per subscription and budget epoch when its gateway spend reaches ${suspendAtPercent}% of the plan budget. The action group calls the Logic App that suspends the APIM subscription.'
    severity: 2
    enabled: true
    evaluationFrequency: evaluationFrequency
    windowSize: evaluationFrequency
    overrideQueryTimeRange: 'P2D'
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
          query: breachQuery
          timeAggregation: 'Count'
          dimensions: [
            {
              name: 'Subscription'
              operator: 'Include'
              values: [
                '*'
              ]
            }
            {
              name: 'BudgetEpoch'
              operator: 'Include'
              values: [
                '*'
              ]
            }
          ]
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    // Stateful: fires once per subscription + epoch, not on every evaluation
    autoMitigate: true
    actions: {
      actionGroups: [
        actionGroup.id
      ]
    }
  }
}

output logicAppName string = workflow.name
output logicAppId string = workflow.id
output alertRuleId string = breachAlert.id
output alertRuleName string = breachAlert.name
output actionGroupId string = actionGroup.id
output roleDefinitionId string = roleDefinitionId
output customRole bool = useCustomRole
output breachQuery string = breachQuery
