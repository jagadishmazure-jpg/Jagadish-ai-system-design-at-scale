// Bicep parity for terraform/ (same layout). DESCRIPTIVE ONLY: CI runs `az bicep build`, never deploys.
param prefix string = 'quillmoor'
param location string = resourceGroup().location
param nodeCount int = 3

resource vnet 'Microsoft.Network/virtualNetworks@2023-09-01' = {
  name: '${prefix}-vnet'
  location: location
  properties: {
    addressSpace: { addressPrefixes: [ '10.20.0.0/16' ] }
    subnets: [
      { name: 'edge', properties: { addressPrefix: '10.20.1.0/24' } }
      { name: 'app', properties: { addressPrefix: '10.20.2.0/23' } }
      { name: 'data', properties: { addressPrefix: '10.20.4.0/24', networkSecurityGroup: { id: dataNsg.id } } }
    ]
  }
}

resource dataNsg 'Microsoft.Network/networkSecurityGroups@2023-09-01' = {
  name: '${prefix}-data-nsg'
  location: location
  properties: {
    securityRules: [
      {
        name: 'allow-app-only'
        properties: {
          priority: 100, direction: 'Inbound', access: 'Allow', protocol: 'Tcp'
          sourceAddressPrefix: '10.20.2.0/23', sourcePortRange: '*'
          destinationAddressPrefix: '*', destinationPortRanges: [ '5432', '6379', '9000' ]
        }
      }
      {
        name: 'deny-all-else'
        properties: {
          priority: 4000, direction: 'Inbound', access: 'Deny', protocol: '*'
          sourceAddressPrefix: '*', sourcePortRange: '*', destinationAddressPrefix: '*', destinationPortRange: '*'
        }
      }
    ]
  }
}

resource aks 'Microsoft.ContainerService/managedClusters@2024-02-01' = {
  name: '${prefix}-aks'
  location: location
  identity: { type: 'SystemAssigned' }
  properties: {
    dnsPrefix: prefix
    agentPoolProfiles: [
      { name: 'system', count: nodeCount, vmSize: 'Standard_D2s_v5', mode: 'System', vnetSubnetID: '${vnet.id}/subnets/app' }
    ]
    networkProfile: { networkPlugin: 'azure', networkPolicy: 'azure' }
  }
}

resource objects 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: '${prefix}objects'
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: { minimumTlsVersion: 'TLS1_2', allowBlobPublicAccess: false }
}

resource cache 'Microsoft.Cache/redis@2023-08-01' = {
  name: '${prefix}-redis'
  location: location
  properties: { sku: { name: 'Basic', family: 'C', capacity: 0 }, minimumTlsVersion: '1.2', publicNetworkAccess: 'Disabled' }
}

output aksName string = aks.name
