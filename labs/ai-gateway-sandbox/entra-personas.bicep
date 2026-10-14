// AI Gateway Sandbox - Entra-only mode persona identities: one user-assigned managed identity per persona.
// A separate module so entra-mode.bicep can use their principal IDs (runtime values) in group memberships.
param location string
param resourceSuffix string
param identities array

resource personas 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = [for identity in identities: {
  name: 'id-${identity.name}-${resourceSuffix}'
  location: location
}]

output personas array = [for (identity, i) in identities: {
  name: identity.name
  principalId: personas[i].properties.principalId
  clientId: personas[i].properties.clientId
  resourceId: personas[i].id
}]
