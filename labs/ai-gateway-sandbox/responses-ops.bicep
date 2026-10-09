// AI Gateway Sandbox - Responses API operation policies (session affinity for stateful calls).
// Applies responses-policy.xml to the stateful Responses operations of the inference API, so follow-up calls
// (previous_response_id), reads and deletes of a stored response reach the Foundry backend that stored it.

param apimName string

@description('Operation policy (responses-policy.xml)')
param policyXml string

@description('Name of the inference API')
param inferenceAPIName string = 'inference-api'

@description('Responses API operations of the OpenAI v1 specification that get the affinity policy')
param operationNames array = [
  'createResponse'
  'getResponse'
  'deleteResponse'
  'listInputItems'
]

resource apim 'Microsoft.ApiManagement/service@2024-06-01-preview' existing = {
  name: apimName
}

resource inferenceApi 'Microsoft.ApiManagement/service/apis@2024-06-01-preview' existing = {
  parent: apim
  name: inferenceAPIName
}

resource operations 'Microsoft.ApiManagement/service/apis/operations@2024-06-01-preview' existing = [for name in operationNames: {
  parent: inferenceApi
  name: name
}]

@batchSize(1)
resource operationPolicies 'Microsoft.ApiManagement/service/apis/operations/policies@2024-06-01-preview' = [for (name, i) in operationNames: {
  parent: operations[i]
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: policyXml
  }
}]
