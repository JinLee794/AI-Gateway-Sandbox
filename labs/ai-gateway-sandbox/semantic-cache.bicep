// AI Gateway Sandbox - Semantic cache (optional, semanticCache.enabled): Azure Managed Redis as the APIM external cache.
//
// Deploys the pieces the llm-semantic-cache-lookup / llm-semantic-cache-store policies need:
//   - Azure Managed Redis with the RediSearch module (vector search over the prompt embeddings). RediSearch can only be
//     enabled when the database is created, and it needs the EnterpriseCluster clustering policy and NoEviction.
//   - The APIM external cache ("default" location) pointing at that database. APIM connects with an access key
//     connection string (Microsoft Entra authentication isn't supported for the external cache), so access key
//     authentication is enabled on the database.
//   - The "embeddings-backend" APIM backend: the embeddings deployment in the priority-1 Foundry resource. The lookup
//     policy calls it with the APIM system-assigned managed identity (embeddings-backend-auth="system-assigned"), which
//     already has Cognitive Services User on the Foundry resource.

@description('Name of the existing API Management service')
param apimName string

@description('Region of Azure Managed Redis (keep it next to API Management: every model call waits for the lookup)')
param location string = resourceGroup().location

@description('Suffix that makes the resource names unique')
param resourceSuffix string

@description('Azure Managed Redis SKU. Balanced_B0 is the smallest one that supports RediSearch.')
param redisSku string = 'Balanced_B0'

@description('Full URL of the embeddings deployment, without query parameters: https://<foundry>/openai/deployments/<name>/embeddings')
param embeddingsUrl string

var redisPort = 10000

resource redis 'Microsoft.Cache/redisEnterprise@2025-05-01-preview' = {
  name: 'redis-${resourceSuffix}'
  location: location
  sku: {
    name: redisSku
  }
  properties: {
    minimumTlsVersion: '1.2'
  }
}

resource redisDatabase 'Microsoft.Cache/redisEnterprise/databases@2025-05-01-preview' = {
  parent: redis
  name: 'default'
  properties: {
    clientProtocol: 'Encrypted'
    port: redisPort
    clusteringPolicy: 'EnterpriseCluster'
    evictionPolicy: 'NoEviction'
    accessKeysAuthentication: 'Enabled'
    modules: [
      {
        name: 'RediSearch'
      }
    ]
  }
}

resource apim 'Microsoft.ApiManagement/service@2024-06-01-preview' existing = {
  name: apimName
}

// APIM stores the connection string as a secret named value. Only the semantic cache policies use it: the lab's other
// cache-lookup-value / cache-store-value policies pin caching-type="internal".
resource apimCache 'Microsoft.ApiManagement/service/caches@2024-06-01-preview' = {
  parent: apim
  name: 'default'
  properties: {
    description: 'Azure Managed Redis (RediSearch) - semantic cache of the AI Gateway Sandbox'
    connectionString: '${redis.properties.hostName}:${redisPort},password=${redisDatabase.listKeys().primaryKey},ssl=True,abortConnect=False'
    useFromLocation: 'default'
    resourceId: '${environment().resourceManager}${substring(redis.id, 1)}'
  }
}

// The llm-semantic-cache-lookup policy refers to this backend by id
resource embeddingsBackend 'Microsoft.ApiManagement/service/backends@2024-06-01-preview' = {
  parent: apim
  name: 'embeddings-backend'
  properties: {
    description: 'Embeddings deployment used by the semantic cache lookup (llm-semantic-cache-lookup)'
    url: embeddingsUrl
    protocol: 'http'
  }
}

output redisName string = redis.name
output redisId string = redis.id
output redisHostName string = redis.properties.hostName
output embeddingsBackendId string = embeddingsBackend.name
output cacheId string = apimCache.name
