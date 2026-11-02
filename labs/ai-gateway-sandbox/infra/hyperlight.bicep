// EXPERIMENTAL - Hyperlight executor for the Code mode scenario (azd env set HYPERLIGHT_EXECUTOR true).
// A small Linux VM with nested virtualization (KVM) runs infra/hyperlight/executor.py, which runs each model-written
// script in a Hyperlight micro-VM. It has no public IP: its subnet only accepts port 8080 from the Container Apps
// subnet, and the APIM key stays in the UI (the executor proxies every tool call back to it). Shuts down every night.
param location string
param subnetId string
param tags object = {}

@description('Needs nested virtualization (Dv5/Dsv5/Ev5 and similar)')
param vmSize string = 'Standard_D2s_v6'

@description('Daily auto-shutdown, HHmm in shutdownTimeZone')
param shutdownTime string = '2100'
param shutdownTimeZone string = 'Central Standard Time'

@description('Not used for sign-in (no public IP, SSH blocked); manage the VM with az vm run-command')
@secure()
param adminPassword string = '${newGuid()}Hl#1'
param adminUsername string = 'hladmin'

var resourceSuffix = uniqueString(subscription().id, resourceGroup().id)
var vmName = 'vm-hyperlight-${take(resourceSuffix, 8)}'
var install = replace(loadTextContent('hyperlight/install.sh'), '__EXECUTOR_B64__', base64(loadTextContent('hyperlight/executor.py')))

resource nic 'Microsoft.Network/networkInterfaces@2024-01-01' = {
  name: 'nic-${vmName}'
  location: location
  tags: tags
  properties: {
    ipConfigurations: [
      {
        name: 'ipconfig1'
        properties: {
          subnet: { id: subnetId }
          privateIPAllocationMethod: 'Dynamic'
        }
      }
    ]
  }
}

resource vm 'Microsoft.Compute/virtualMachines@2024-07-01' = {
  name: vmName
  location: location
  tags: tags
  properties: {
    hardwareProfile: { vmSize: vmSize }
    securityProfile: {
      securityType: 'TrustedLaunch'
      uefiSettings: { secureBootEnabled: true, vTpmEnabled: true }
    }
    storageProfile: {
      imageReference: { publisher: 'Canonical', offer: 'ubuntu-24_04-lts', sku: 'server', version: 'latest' }
      osDisk: { createOption: 'FromImage', managedDisk: { storageAccountType: 'StandardSSD_LRS' }, deleteOption: 'Delete' }
    }
    osProfile: {
      computerName: 'hyperlight'
      adminUsername: adminUsername
      adminPassword: adminPassword
      linuxConfiguration: { disablePasswordAuthentication: false }
    }
    networkProfile: { networkInterfaces: [ { id: nic.id, properties: { deleteOption: 'Delete' } } ] }
  }
}

// Re-runs whenever executor.py or install.sh change
resource setup 'Microsoft.Compute/virtualMachines/extensions@2024-07-01' = {
  parent: vm
  name: 'hyperlight-executor'
  location: location
  properties: {
    publisher: 'Microsoft.Azure.Extensions'
    type: 'CustomScript'
    typeHandlerVersion: '2.1'
    autoUpgradeMinorVersion: true
    protectedSettings: { script: base64(install) }
  }
}

resource shutdown 'Microsoft.DevTestLab/schedules@2018-09-15' = {
  name: 'shutdown-computevm-${vmName}'
  location: location
  tags: tags
  properties: {
    status: 'Enabled'
    taskType: 'ComputeVmShutdownTask'
    dailyRecurrence: { time: shutdownTime }
    timeZoneId: shutdownTimeZone
    targetResourceId: vm.id
    notificationSettings: { status: 'Disabled' }
  }
}

output url string = 'http://${nic.properties.ipConfigurations[0].properties.privateIPAddress}:8080'
output vmName string = vm.name
