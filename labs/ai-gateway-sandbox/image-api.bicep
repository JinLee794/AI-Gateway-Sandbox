// AI Gateway Sandbox - Image generation API (optional, imagesConfig.enabled).
// OpenAI v1 compatible POST {gateway}/images/openai/v1/images/generations, routed to the priority-1 Foundry backend
// (the image model is deployed there only). image-policy.xml prices every call per image and the plan (product)
// policy enforces the image entitlement, the image rate limit and the shared $ budget.

param apimName string

@description('API policy (image-policy.xml with its placeholders replaced)')
param policyXml string

@description('Products (plans) the image API is added to')
param productNames array = []

param apiName string = 'image-api'
param apiPath string = 'images/openai/v1'

resource apim 'Microsoft.ApiManagement/service@2024-06-01-preview' existing = {
  name: apimName
}

resource imageApi 'Microsoft.ApiManagement/service/apis@2024-06-01-preview' = {
  parent: apim
  name: apiName
  properties: {
    apiType: 'http'
    type: 'http'
    displayName: 'Image Generation API'
    description: 'OpenAI v1 compatible image generation, priced per image by model, quality and size'
    path: apiPath
    protocols: [
      'https'
    ]
    subscriptionRequired: true
    subscriptionKeyParameterNames: {
      header: 'api-key'
      query: 'api-key'
    }
  }
}

resource generateImage 'Microsoft.ApiManagement/service/apis/operations@2024-06-01-preview' = {
  parent: imageApi
  name: 'generateImage'
  properties: {
    displayName: 'Create image'
    method: 'POST'
    urlTemplate: '/images/generations'
  }
}

resource imageApiPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-06-01-preview' = {
  parent: imageApi
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: policyXml
  }
  dependsOn: [
    generateImage
  ]
}

// Request logging to Application Insights (the chargeback records are trace telemetry)
resource imageApiDiagnostics 'Microsoft.ApiManagement/service/apis/diagnostics@2022-08-01' = {
  parent: imageApi
  name: 'applicationinsights'
  properties: {
    alwaysLog: 'allErrors'
    httpCorrelationProtocol: 'W3C'
    logClientIp: true
    loggerId: resourceId('Microsoft.ApiManagement/service/loggers', apim.name, 'appinsights-logger')
    metrics: true
    verbosity: 'information'
    sampling: {
      samplingType: 'fixed'
      percentage: 100
    }
  }
}

resource products 'Microsoft.ApiManagement/service/products@2024-06-01-preview' existing = [for name in productNames: {
  parent: apim
  name: name
}]

@batchSize(1)
resource productLinks 'Microsoft.ApiManagement/service/products/apiLinks@2024-06-01-preview' = [for (name, i) in productNames: {
  parent: products[i]
  name: 'image-${name}'
  properties: {
    apiId: imageApi.id
  }
  dependsOn: [
    imageApiPolicy
  ]
}]

output apiId string = imageApi.id
