// AI Gateway Sandbox - FinOps dashboard (Azure portal shared dashboard), ported from labs/finops-framework/dashboard.bicep.
// The FinOps lab prices ApiManagementGatewayLlmLog rows with the PRICING_CL and SUBSCRIPTION_QUOTA_CL custom tables.
// The sandbox prices every call at the gateway instead, so these tiles read the chargeback records written by the
// chargeback-record policy fragment (AppTraces, record == "chargeback") and the plan budgets from the lab config.

param resourceSuffix string
param workspaceName string
param workspaceId string
param appInsightsId string
param appInsightsName string
param workbookId string

@description('KQL datatable rows: \'subscription\', \'display name\', \'plan\', budgetMicroUsd (same as the sandbox workbook)')
param budgetRows string

@description('Whether the "over budget, switched off" loop is deployed (adds the BudgetBreach audit tile)')
param budgetSuspendEnabled bool = false

var records = '''AppTraces
| where tostring(Properties.record) == "chargeback"
| extend Subscription = tostring(Properties.user), Name = coalesce(tostring(Properties.displayName), tostring(Properties.user)),
         Team = tostring(Properties.team), CostCenter = tostring(Properties.costCenter), Surface = tostring(Properties.surface),
         BudgetEpoch = tostring(Properties.budgetEpoch), CostUSD = tolong(Properties.costMicroUsd) / 1e6
'''

// Spend since the last budget reset (the budget epoch of the latest record) against each subscription's plan budget
var budgetQuery = replace('''let budgets = datatable(Subscription:string, Name:string, Plan:string, BudgetMicroUsd:long)[{budget-rows}];
let records = AppTraces
| where TimeGenerated > ago(30d) and tostring(Properties.record) == "chargeback"
| extend Subscription = tostring(Properties.user), BudgetEpoch = tostring(Properties.budgetEpoch), CostUSD = tolong(Properties.costMicroUsd) / 1e6;
let epoch = toscalar(records | summarize arg_max(TimeGenerated, BudgetEpoch) | project BudgetEpoch);
records
| where BudgetEpoch == epoch
| summarize SpentUSD = sum(CostUSD) by Subscription
| join kind=rightouter budgets on Subscription
| project Subscription = strcat(Name, " (", Plan, ")"), BudgetUSD = BudgetMicroUsd / 1e6, SpentUSD = coalesce(SpentUSD, 0.0)
| order by Subscription asc
''', '{budget-rows}', budgetRows)

var costBySubscriptionQuery = '${records}| summarize CostUSD = sum(CostUSD) by Name\n| order by CostUSD desc\n'
var costOverTimeQuery = '${records}| summarize CostUSD = sum(CostUSD) by bin(TimeGenerated, 15m), Name\n| order by TimeGenerated asc\n'
var costByTeamQuery = '${records}| extend Team = strcat(coalesce(Team, "No team"), " / ", coalesce(CostCenter, "n/a"))\n| summarize CostUSD = sum(CostUSD) by Team, Surface\n| order by CostUSD desc\n'
var budgetBreachQuery = '''AppEvents
| where Name == "BudgetBreach"
| project TimeGenerated, Subscription = tostring(Properties.subscription), Action = tostring(Properties.action),
          Reason = tostring(Properties.reason), BudgetEpoch = tostring(Properties.budgetEpoch)
| order by TimeGenerated desc
'''

var portal = 'https://portal.azure.com/#@${tenant().tenantId}/resource'
var repo = 'https://github.com/Azure-Samples/AI-Gateway/tree/main/labs'
var intro = '''## AI Gateway Sandbox · FinOps

Every call is priced **at the gateway** (model tokens, MCP tool calls, A2A agent tasks, content safety checks) and
charged to the caller's **$ budget** with `quota-by-key`. The `chargeback-record` policy fragment writes one record
per billable call with the user, team, cost center, session and cost: these tiles read those records.

{suspend}

- [Sandbox workbook]({workbook}) · drill into consumers, plans, surfaces and governance events
- [Sandbox lab]({repo}/ai-gateway-sandbox) · [FinOps framework lab]({repo}/finops-framework)
'''
var suspendText = budgetSuspendEnabled
  ? '**Over budget, switched off** is on: the first call blocked by a watched subscription\'s budget makes the gateway notify a Logic App that suspends the APIM subscription (see *Budget suspensions*). **Reset budgets** in the demo UI re-activates it.'
  : '**Over budget, switched off** is not deployed: set `budgetSuspend.enabled` in `sandbox-config.json` and deploy again to suspend over-budget subscriptions automatically.'
var introMarkdown = replace(replace(replace(intro, '{suspend}', suspendText), '{workbook}', '${portal}${workbookId}/workbook'), '{repo}', repo)

func position(x int, y int, colSpan int, rowSpan int) object => { x: x, y: y, colSpan: colSpan, rowSpan: rowSpan }

func logsPart(pos object, workspaceId string, workspaceName string, title string, query string, chart string, dimensions object, timeRange string, queryHasTimeRange bool) object => {
  position: pos
  metadata: {
    inputs: [
      { name: 'resourceTypeMode', isOptional: true }
      { name: 'ComponentId', isOptional: true }
      { name: 'Scope', value: { resourceIds: [ workspaceId ] }, isOptional: true }
      { name: 'PartId', value: guid(workspaceId, title), isOptional: true }
      { name: 'Version', value: '2.0', isOptional: true }
      { name: 'TimeRange', value: timeRange, isOptional: true }
      { name: 'DashboardId', isOptional: true }
      { name: 'DraftRequestParameters', isOptional: true }
      { name: 'Query', value: query, isOptional: true }
      { name: 'ControlType', value: chart == 'Table' ? 'AnalyticsGrid' : 'FrameControlChart', isOptional: true }
      { name: 'SpecificChart', value: chart, isOptional: true }
      { name: 'PartTitle', value: title, isOptional: true }
      { name: 'PartSubTitle', value: workspaceName, isOptional: true }
      { name: 'Dimensions', value: dimensions, isOptional: true }
      { name: 'LegendOptions', value: { isEnabled: true, position: 'Bottom' }, isOptional: true }
      { name: 'IsQueryContainTimeRange', value: queryHasTimeRange, isOptional: true }
    ]
    type: 'Extension/Microsoft_OperationsManagementSuite_Workspace/PartType/LogsDashboardPart'
    settings: {}
  }
}

func metricPart(pos object, appInsightsId string, appInsightsName string, metric string, aggregation int, title string, chartType int, color string, menu string) object => {
  position: pos
  metadata: {
    inputs: [
      {
        name: 'options'
        value: {
          chart: {
            metrics: [
              {
                resourceMetadata: { id: appInsightsId }
                name: metric
                aggregationType: aggregation
                namespace: 'microsoft.insights/components'
                metricVisualization: { displayName: title, resourceDisplayName: appInsightsName, color: color }
              }
            ]
            title: title
            titleKind: 2
            visualization: { chartType: chartType }
            openBladeOnClick: {
              openBlade: true
              destinationBlade: {
                bladeName: 'ResourceMenuBlade'
                parameters: { id: appInsightsId, menuid: menu }
                extensionName: 'HubsExtension'
                options: { parameters: { id: appInsightsId, menuid: menu } }
              }
            }
          }
        }
        isOptional: true
      }
      { name: 'sharedTimeRange', isOptional: true }
    ]
    type: 'Extension/HubsExtension/PartType/MonitorChartPart'
    settings: { content: {} }
  }
}

var basicParts = [
  {
    position: position(0, 0, 6, 4)
    metadata: {
      inputs: []
      type: 'Extension/HubsExtension/PartType/MarkdownPart'
      settings: { content: { settings: { content: introMarkdown, markdownUri: null } } }
    }
  }
  {
    position: position(6, 0, 2, 2)
    metadata: {
      inputs: [
        { name: 'resourceGroup', isOptional: true }
        { name: 'id', value: resourceGroup().id, isOptional: true }
      ]
      type: 'Extension/HubsExtension/PartType/ResourceGroupMapPinnedPart'
    }
  }
  {
    position: position(6, 2, 2, 2)
    metadata: {
      inputs: []
      type: 'Extension/HubsExtension/PartType/MarkdownPart'
      settings: { content: { settings: { content: '### [Open the sandbox workbook ↗](${portal}${workbookId}/workbook)\n\nCost, budgets and governance by consumer, plan and surface.', markdownUri: null } } }
    }
  }
  logsPart(position(8, 0, 7, 4), workspaceId, workspaceName, 'Budget vs spend since the last reset', budgetQuery, 'UnstackedColumn', {
    xAxis: { name: 'Subscription', type: 'string' }
    yAxis: [ { name: 'BudgetUSD', type: 'real' }, { name: 'SpentUSD', type: 'real' } ]
    splitBy: []
    aggregation: 'Sum'
  }, 'P30D', true)
  logsPart(position(0, 4, 7, 4), workspaceId, workspaceName, 'Cost by subscription (USD)', costBySubscriptionQuery, 'Donut', {
    xAxis: { name: 'Name', type: 'string' }
    yAxis: [ { name: 'CostUSD', type: 'real' } ]
    splitBy: []
    aggregation: 'Sum'
  }, 'P1D', false)
  logsPart(position(7, 4, 8, 4), workspaceId, workspaceName, 'Cost over time by subscription (USD)', costOverTimeQuery, 'StackedColumn', {
    xAxis: { name: 'TimeGenerated', type: 'datetime' }
    yAxis: [ { name: 'CostUSD', type: 'real' } ]
    splitBy: [ { name: 'Name', type: 'string' } ]
    aggregation: 'Sum'
  }, 'P1D', false)
  logsPart(position(0, 8, budgetSuspendEnabled ? 7 : 15, 4), workspaceId, workspaceName, 'Chargeback by team / cost center and surface (USD)', costByTeamQuery, 'StackedColumn', {
    xAxis: { name: 'Team', type: 'string' }
    yAxis: [ { name: 'CostUSD', type: 'real' } ]
    splitBy: [ { name: 'Surface', type: 'string' } ]
    aggregation: 'Sum'
  }, 'P7D', false)
]

var suspendParts = budgetSuspendEnabled ? [
  logsPart(position(7, 8, 8, 4), workspaceId, workspaceName, 'Budget suspensions (BudgetBreach audit events)', budgetBreachQuery, 'Table', {}, 'P7D', false)
] : []

var metricParts = [
  metricPart(position(0, 12, 5, 3), appInsightsId, appInsightsName, 'requests/count', 7, 'Gateway requests', 3, '#0078D4', 'performance')
  metricPart(position(5, 12, 5, 3), appInsightsId, appInsightsName, 'requests/duration', 4, 'Gateway response time', 2, '#0078D4', 'performance')
  metricPart(position(10, 12, 5, 3), appInsightsId, appInsightsName, 'requests/failed', 7, 'Failed requests', 3, '#EC008C', 'failures')
]

resource finOpsDashboard 'Microsoft.Portal/dashboards@2022-12-01-preview' = {
  name: guid(resourceGroup().id, resourceSuffix, 'sandboxFinOpsDashboard')
  location: resourceGroup().location
  tags: {
    'hidden-title': 'AI Gateway Sandbox - FinOps'
  }
  properties: {
    lenses: [
      {
        order: 0
        parts: concat(basicParts, suspendParts, metricParts)
      }
    ]
    metadata: {
      model: {
        timeRange: {
          value: { relative: { duration: 24, timeUnit: 1 } }
          type: 'MsPortalFx.Composition.Configuration.ValueTypes.TimeRange'
        }
        filterLocale: { value: 'en-us' }
      }
    }
  }
}

output id string = finOpsDashboard.id
output name string = finOpsDashboard.name
