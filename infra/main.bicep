@description('Azure region for regional resources.')
param location string = resourceGroup().location

@description('Short lowercase prefix used to make globally unique resource names.')
@minLength(3)
@maxLength(12)
param namePrefix string

@description('Microsoft Entra tenant containing the Dataverse and bot applications.')
param tenantId string = tenant().tenantId

@description('Application (client) ID of the Dataverse application user.')
param dataverseClientId string

@secure()
@description('Client secret of the Dataverse application user.')
param dataverseClientSecret string

@description('Application (client) ID registered for the Teams bot.')
param teamsBotClientId string

@secure()
@description('Client secret registered for the Teams bot.')
param teamsBotClientSecret string

@description('Application (client) ID with Microsoft Graph Mail.Send application permission.')
param emailClientId string = ''

@secure()
@description('Client secret of the email notification application.')
param emailClientSecret string = ''

@description('Enable scheduled Microsoft Graph SLA email notifications.')
param emailNotificationsEnabled bool = false

@description('Exchange Online mailbox used to send SLA alerts.')
param emailSender string = 'viojha@microsoft.com'

@description('Comma-separated recipients for SLA alert emails.')
param emailRecipients string = 'viojha@microsoft.com'

@description('Dataverse environment URL.')
param dataverseUrl string = 'https://sbamanager.crm.dynamics.com'

@description('Azure Functions NCRONTAB schedule. Default: every five minutes.')
param alertSchedule string = '0 */5 * * * *'

var suffix = uniqueString(subscription().subscriptionId, resourceGroup().id, namePrefix)
var storageName = take(toLower('${namePrefix}${suffix}'), 24)
var functionName = take(toLower('${namePrefix}-sla-${suffix}'), 60)
var planName = '${namePrefix}-sla-fc'
var insightsName = '${namePrefix}-sla-insights'
var workspaceName = '${namePrefix}-sla-logs'
var botName = '${namePrefix}-sla-bot'
var deploymentContainerName = 'function-releases'

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource deploymentContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: deploymentContainerName
  properties: {
    publicAccess: 'None'
  }
}

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: workspaceName
  location: location
  properties: {
    retentionInDays: 30
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
    }
  }
}

resource insights 'Microsoft.Insights/components@2020-02-02' = {
  name: insightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspace.id
  }
}

resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: planName
  location: location
  kind: 'functionapp'
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  properties: {
    reserved: true
  }
}

resource functionApp 'Microsoft.Web/sites@2024-04-01' = {
  name: functionName
  location: location
  kind: 'functionapp,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      appSettings: [
        {
          name: 'AzureWebJobsStorage__accountName'
          value: storage.name
        }
        {
          name: 'AzureWebJobsStorage__credential'
          value: 'managedidentity'
        }
        {
          name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
          value: insights.properties.ConnectionString
        }
        {
          name: 'AUTH_MODE'
          value: 'app'
        }
        {
          name: 'DATAVERSE_URL'
          value: dataverseUrl
        }
        {
          name: 'TENANT_ID'
          value: tenantId
        }
        {
          name: 'CLIENT_ID'
          value: dataverseClientId
        }
        {
          name: 'CLIENT_SECRET'
          value: dataverseClientSecret
        }
        {
          name: 'TEAMS_BOT_CLIENT_ID'
          value: teamsBotClientId
        }
        {
          name: 'TEAMS_BOT_CLIENT_SECRET'
          value: teamsBotClientSecret
        }
        {
          name: 'TEAMS_NOTIFICATIONS_ENABLED'
          value: 'true'
        }
        {
          name: 'TEAMS_ALERT_SCHEDULE'
          value: alertSchedule
        }
        {
          name: 'TEAMS_STATE_TABLE'
          value: 'slanotifications'
        }
        {
          name: 'EMAIL_NOTIFICATIONS_ENABLED'
          value: string(emailNotificationsEnabled)
        }
        {
          name: 'EMAIL_TENANT_ID'
          value: tenantId
        }
        {
          name: 'EMAIL_CLIENT_ID'
          value: emailClientId
        }
        {
          name: 'EMAIL_CLIENT_SECRET'
          value: emailClientSecret
        }
        {
          name: 'EMAIL_SENDER'
          value: emailSender
        }
        {
          name: 'EMAIL_RECIPIENTS'
          value: emailRecipients
        }
        {
          name: 'TABLE_STORAGE_ENDPOINT'
          value: storage.properties.primaryEndpoints.table
        }
      ]
    }
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}${deploymentContainerName}'
          authentication: {
            type: 'SystemAssignedIdentity'
          }
        }
      }
      runtime: {
        name: 'python'
        version: '3.11'
      }
      scaleAndConcurrency: {
        maximumInstanceCount: 20
        instanceMemoryMB: 2048
      }
    }
  }
}

resource blobContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, functionApp.id, 'blob-data-contributor')
  scope: storage
  properties: {
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
    )
  }
}

resource queueContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, functionApp.id, 'queue-data-contributor')
  scope: storage
  properties: {
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
    )
  }
}

resource storageAccountContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, functionApp.id, 'storage-account-contributor')
  scope: storage
  properties: {
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '17d1049b-9a84-46fb-8f53-869881c3d3ab'
    )
  }
}

resource tableContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, functionApp.id, 'table-data-contributor')
  scope: storage
  properties: {
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
    )
  }
}

resource bot 'Microsoft.BotService/botServices@2022-09-15' = {
  name: botName
  location: 'global'
  kind: 'azurebot'
  sku: {
    name: 'F0'
  }
  properties: {
    displayName: 'SLA Alert Notification'
    endpoint: 'https://${functionApp.properties.defaultHostName}/api/messages'
    msaAppId: teamsBotClientId
    msaAppTenantId: tenantId
    msaAppType: 'SingleTenant'
  }
}

resource teamsChannel 'Microsoft.BotService/botServices/channels@2022-09-15' = {
  parent: bot
  name: 'MsTeamsChannel'
  location: 'global'
  properties: {
    channelName: 'MsTeamsChannel'
    properties: {
      isEnabled: true
    }
  }
}

output functionAppName string = functionApp.name
output functionHostName string = functionApp.properties.defaultHostName
output messagingEndpoint string = bot.properties.endpoint
output botResourceName string = bot.name
output teamsBotClientId string = teamsBotClientId