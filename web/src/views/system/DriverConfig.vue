<template>
  <div class="driver-config-page">
    <n-card :title="t('driver.title')">
      <template #header-extra>
        <n-button type="primary" @click="loadDrivers">{{ t('driver.refresh') }}</n-button>
      </template>
      <n-data-table :columns="columns" :data="drivers" :loading="loading">
        <template #empty>
          <n-empty :description="t('common.noData')" size="small" />
        </template>
      </n-data-table>
    </n-card>

    <n-modal v-model:show="showSchemaModal" preset="card" :title="currentSchemaTitle" style="width: 700px; max-width: 95vw" :close-on-esc="true" :auto-focus="true" :close-on-esc-aria-label="t('common.closeDialog')">
      <n-alert v-if="currentDriverMeta?.constraints?.length" type="warning" :bordered="false" style="margin-bottom: 12px">
        <div v-for="c in currentDriverMeta.constraints" :key="c.type + c.message" style="margin-bottom: 4px">
          <n-tag size="tiny" :type="c.type === 'platform' || c.type === 'interop_risk' ? 'warning' : 'info'" :bordered="false">{{ c.type }}</n-tag>
          {{ c.message }}
        </div>
      </n-alert>
      <n-alert v-if="currentSchema?.description" type="info" :show-icon="false" style="margin-bottom: 16px">
        {{ translateFieldText(currentDriverName, currentSchema.description) }}
      </n-alert>
      <n-empty v-if="!currentSchema?.fields?.length" :description="t('driver.noSchema')" />
      <n-descriptions v-else bordered :column="1" label-placement="left">
        <n-descriptions-item v-for="field in currentSchema.fields" :key="field.name" :label="translateFieldText(currentDriverName, field.label) || field.name">
          <n-space vertical :size="4">
            <n-space align="center" :size="8">
              <n-tag size="small" :type="field.required ? 'error' : 'default'">{{ field.required ? t('driver.required') : t('driver.optional') }}</n-tag>
              <n-tag size="small" type="info">{{ typeMap[field.type] || field.type }}</n-tag>
              <n-tag v-if="field.secret" size="small" type="warning">{{ t('driver.sensitive') }}</n-tag>
            </n-space>
            <n-text v-if="field.description" depth="3" style="font-size: 13px">
              {{ translateFieldText(currentDriverName, field.description) }}
            </n-text>
            <n-text v-if="field.default !== undefined && field.default !== ''" depth="3" style="font-size: 13px">
              {{ t('driver.defaultValue') }} <n-text code style="font-size: 12px">{{ field.default }}</n-text>
            </n-text>
            <n-space v-if="field.options" :size="8" style="flex-wrap: wrap">
              <n-tag v-for="opt in field.options" :key="opt" size="small" type="success">{{ opt }}</n-tag>
            </n-space>
          </n-space>
        </n-descriptions-item>
      </n-descriptions>
      <template #footer>
        <n-space justify="end">
          <n-button type="primary" @click="goCreateDevice">{{ t('driver.createDevice') }}</n-button>
        </n-space>
      </template>
    </n-modal>

    <n-modal v-model:show="showDiscoverModal" preset="card" :title="t('driver.deviceDiscover')" style="width: 500px; max-width: 95vw" :close-on-esc="true" :auto-focus="true" :close-on-esc-aria-label="t('common.closeDialog')">
      <n-spin :show="discovering">
        <n-empty v-if="discoveredDevices.length === 0 && !discovering" :description="t('driver.discoverHint')" />
        <n-list v-else>
          <n-list-item v-for="dev in discoveredDevices" :key="dev.device_id">
            <n-thing :title="dev.name">
              <template #description>
                <n-space>
                  <n-tag size="small">{{ dev.protocol }}</n-tag>
                  <span style="font-size: 12px; color: #666">{{ dev.ip }}</span>
                </n-space>
              </template>
            </n-thing>
          </n-list-item>
        </n-list>
      </n-spin>
      <template #footer>
        <n-button type="primary" :loading="discovering" @click="doDiscover">{{ t('driver.startDiscover') }}</n-button>
      </template>
    </n-modal>

    <n-modal v-model:show="showOpcDaModal" preset="card" :title="t('driver.opcDaServerList')" style="width: 500px; max-width: 95vw" :close-on-esc="true" :auto-focus="true" :close-on-esc-aria-label="t('common.closeDialog')">
      <n-space vertical :size="12">
        <n-input v-model:value="opcDaHost" :placeholder="t('driver.opcDaHostPlaceholder')" />
        <n-button type="primary" :loading="opcDaLoading" :disabled="!opcDaHost" @click="fetchOpcDaServers">{{ t('driver.opcDaFetchServers') }}</n-button>
        <n-spin :show="opcDaLoading">
          <n-list v-if="opcDaServers.length">
            <n-list-item v-for="srv in opcDaServers" :key="srv">
              <n-thing :title="srv" />
            </n-list-item>
          </n-list>
          <n-empty v-else-if="!opcDaLoading" :description="t('driver.opcDaNoServers')" />
        </n-spin>
      </n-space>
    </n-modal>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, h } from 'vue'
import { NCard, NButton, NDataTable, NTag, NSpace, NModal, NDescriptions, NDescriptionsItem, NList, NListItem, NThing, NEmpty, NSpin, NAlert, NText, NInput } from 'naive-ui'
import { t } from '@/i18n'
import { extractError } from '@/utils/errorCodes'
import { driverApi } from '@/api'
import { useRouter } from 'vue-router'
import { message } from '@/utils/discreteApi'
import { useDirtyFormGuard } from '@/composables/useDirtyFormGuard'

const router = useRouter()

// [AUDIT-FIX] 严重级-表单未保存离开确认（当前页面为只读列表+查询模态框，无持久化编辑表单；
// 注册守卫以保持与其他优先级页面一致，并为后续可能的编辑表单预留钩子）
useDirtyFormGuard()

const driverFieldI18nMap: Record<string, Record<string, string>> = {
  'Modbus TCP': {
    'Modbus TCP industrial standard protocol for reading/writing PLC/ineter coils and registers': 'driverField.modbus-tcp.desc',
    'IP Address': 'driverField.modbus-tcp.ipAddr',
    'PLC or gateway IP address': 'driverField.modbus-tcp.ipAddrDesc',
    'Backup IP': 'driverField.modbus-tcp.backupIp',
    'Backup IP for link redundancy, auto-switch after 3 primary failures': 'driverField.modbus-tcp.backupIpDesc',
    'Port': 'driverField.modbus-tcp.port',
    'Modbus TCP port, default 502': 'driverField.modbus-tcp.portDesc',
    'Slave ID': 'driverField.modbus-tcp.slaveId',
    'Device slave address (Unit ID), usually 1': 'driverField.modbus-tcp.slaveIdDesc',
    'Timeout (s)': 'driverField.modbus-tcp.timeout',
    'Connection and read timeout': 'driverField.modbus-tcp.timeoutDesc',
    'Byte Order': 'driverField.modbus-tcp.byteOrder',
    'Multi-register byte order: ABCD(Big-Endian), BADC, CDAB, DCBA(Little-Endian)': 'driverField.modbus-tcp.byteOrderDesc',
    'Reconnect Interval (s)': 'driverField.modbus-tcp.reconnectInterval',
    'Seconds between reconnection attempts': 'driverField.modbus-tcp.reconnectIntervalDesc',
    'Max Reconnect Attempts': 'driverField.modbus-tcp.maxReconnectAttempts',
    'Maximum consecutive reconnection attempts (default 3)': 'driverField.modbus-tcp.maxReconnectAttemptsDesc',
    'Max Retry Interval (s)': 'driverField.modbus-tcp.maxRetryInterval',
    'Maximum retry backoff interval in seconds (1-300)': 'driverField.modbus-tcp.maxRetryIntervalDesc',
    'Enable Jitter': 'driverField.modbus-tcp.enableJitter',
    'Enable jitter on retry backoff to prevent thundering herd': 'driverField.modbus-tcp.enableJitterDesc',
    'Batch Read Size': 'driverField.modbus-tcp.batchReadSize',
    'Maximum registers per read request (1-125)': 'driverField.modbus-tcp.batchReadSizeDesc',
    'Function Code': 'driverField.modbus-tcp.functionCode',
    'Default Modbus function code': 'driverField.modbus-tcp.functionCodeDesc',
    'Enable Broadcast Write': 'driverField.modbus-tcp.enableBroadcast',
    'Allow writing to slave_id=0 (broadcast address). Note: broadcast writes have no response and cannot be verified': 'driverField.modbus-tcp.enableBroadcastDesc',
    'Rate of Change Threshold': 'driverField.modbus-tcp.rocThreshold',
    'Rate of change threshold for data credibility, mark quality=uncertain when exceeded': 'driverField.modbus-tcp.rocThresholdDesc',
    'Frozen Detection Count': 'driverField.modbus-tcp.frozenCount',
    'Consecutive identical readings to detect frozen value (1-1000)': 'driverField.modbus-tcp.frozenCountDesc',
    'Watchdog Threshold': 'driverField.modbus-tcp.watchdogThreshold',
    'Watchdog disconnect detection threshold in seconds (detection_interval * fail_count >= threshold), default 30s': 'driverField.modbus-tcp.watchdogThresholdDesc',
    'Write Verify': 'driverField.modbus-tcp.writeVerify',
    'Enable read-verify-write: read back after write and compare': 'driverField.modbus-tcp.writeVerifyDesc',
    'Write Rate Limit (s)': 'driverField.modbus-tcp.writeRateLimit',
    'Minimum interval between writes to same register (seconds)': 'driverField.modbus-tcp.writeRateLimitDesc',
    'Write Audit': 'driverField.modbus-tcp.writeAudit',
    'Enable write operation audit logging': 'driverField.modbus-tcp.writeAuditDesc',
    'Deadband': 'driverField.modbus-tcp.deadband',
    'Deadband filter threshold, suppress updates when change < deadband': 'driverField.modbus-tcp.deadbandDesc',
    'Scaling': 'driverField.modbus-tcp.scaling',
    'Linear scaling: y = x * ratio + offset': 'driverField.modbus-tcp.scalingDesc',
    'Clamp': 'driverField.modbus-tcp.clamp',
    'Value range validation, mark quality=bad when out of range': 'driverField.modbus-tcp.clampDesc',
  },
  'Modbus RTU': {
    'Modbus RTU serial protocol for RS-485/RS-232 devices': 'driverField.modbus-rtu.desc',
    'Serial Port': 'driverField.modbus-rtu.serialPort',
    'Serial port device path': 'driverField.modbus-rtu.serialPortDesc',
    'Baud Rate': 'driverField.modbus-rtu.baudRate',
    'Communication baud rate': 'driverField.modbus-rtu.baudRateDesc',
    'Parity': 'driverField.modbus-rtu.parity',
    'Serial parity check': 'driverField.modbus-rtu.parityDesc',
    'Stop Bits': 'driverField.modbus-rtu.stopBits',
    'Number of stop bits': 'driverField.modbus-rtu.stopBitsDesc',
    'Data Bits': 'driverField.modbus-rtu.dataBits',
    'Number of data bits (7 or 8)': 'driverField.modbus-rtu.dataBitsDesc',
    'Slave ID': 'driverField.modbus-rtu.slaveId',
    'Modbus slave address, usually 1-247': 'driverField.modbus-rtu.slaveIdDesc',
    'Timeout (s)': 'driverField.modbus-rtu.timeout',
    'Connection and read timeout': 'driverField.modbus-rtu.timeoutDesc',
    'Byte Order': 'driverField.modbus-rtu.byteOrder',
    'Multi-register byte order': 'driverField.modbus-rtu.byteOrderDesc',
    'Reconnect Interval (s)': 'driverField.modbus-rtu.reconnectInterval',
    'Seconds between reconnection attempts': 'driverField.modbus-rtu.reconnectIntervalDesc',
    'Max Reconnect Attempts': 'driverField.modbus-rtu.maxReconnectAttempts',
    'Maximum consecutive reconnection attempts': 'driverField.modbus-rtu.maxReconnectAttemptsDesc',
    'Batch Read Size': 'driverField.modbus-rtu.batchReadSize',
    'Maximum registers per read request': 'driverField.modbus-rtu.batchReadSizeDesc',
    'Function Code': 'driverField.modbus-rtu.functionCode',
    'Default Modbus function code': 'driverField.modbus-rtu.functionCodeDesc',
    'RS485 Mode': 'driverField.modbus-rtu.rs485Mode',
    'RS485 transceiver mode': 'driverField.modbus-rtu.rs485ModeDesc',
    'RS485 RTS On Send': 'driverField.modbus-rtu.rs485RtsOnSend',
    'RS485 RTS signal state during send': 'driverField.modbus-rtu.rs485RtsOnSendDesc',
    'RS485 RTS On Receive': 'driverField.modbus-rtu.rs485RtsOnReceive',
    'RS485 RTS signal state during receive': 'driverField.modbus-rtu.rs485RtsOnReceiveDesc',
    'RS485 Delay Before Send (ms)': 'driverField.modbus-rtu.rs485DelayBeforeSend',
    'Delay before RS485 transmission in milliseconds': 'driverField.modbus-rtu.rs485DelayBeforeSendDesc',
    'RS485 Delay After Send (ms)': 'driverField.modbus-rtu.rs485DelayAfterSend',
    'Delay after RS485 transmission in milliseconds': 'driverField.modbus-rtu.rs485DelayAfterSendDesc',
    'Deadband': 'driverField.modbus-rtu.deadband',
    'Deadband filter threshold': 'driverField.modbus-rtu.deadbandDesc',
    'Scaling': 'driverField.modbus-rtu.scaling',
    'Linear scaling: y = x * ratio + offset': 'driverField.modbus-rtu.scalingDesc',
    'Clamp Range': 'driverField.modbus-rtu.clampRange',
    'Value range validation': 'driverField.modbus-rtu.clampRangeDesc',
    'Log Language': 'driverField.modbus-rtu.logLanguage',
    'Log output language': 'driverField.modbus-rtu.logLanguageDesc',
    'Backup Serial Port': 'driverField.modbus-rtu.backupSerialPort',
    'Backup serial port for redundancy': 'driverField.modbus-rtu.backupSerialPortDesc',
    'TCP-RTU Gateway': 'driverField.modbus-rtu.tcpRtuGateway',
    'TCP to RTU gateway address': 'driverField.modbus-rtu.tcpRtuGatewayDesc',
  },
  'OPC-UA': {
    'OPC UA industrial communication protocol': 'driverField.opcua.desc',
    'Server URL': 'driverField.opcua.serverUrl',
    'OPC UA server address': 'driverField.opcua.serverUrlDesc',
    'Security Mode': 'driverField.opcua.securityMode',
    'OPC UA security mode': 'driverField.opcua.securityModeDesc',
    'Username': 'driverField.opcua.username',
    'Password': 'driverField.opcua.password',
    'OPC UA Endpoint': 'driverField.opcua.endpoint',
    'Primary OPC UA server endpoint URL': 'driverField.opcua.endpointDesc',
    'Backup Endpoint': 'driverField.opcua.backupEndpoint',
    'Backup OPC UA server endpoint for redundancy': 'driverField.opcua.backupEndpointDesc',
    'Security Policy': 'driverField.opcua.securityPolicy',
    'OPC UA security policy (e.g. None, Basic128Rsa15, Basic256, Basic256Sha256)': 'driverField.opcua.securityPolicyDesc',
    'Client Cert Path': 'driverField.opcua.clientCertPath',
    'Path to client X.509 certificate file': 'driverField.opcua.clientCertPathDesc',
    'Client Key Path': 'driverField.opcua.clientKeyPath',
    'Path to client private key file': 'driverField.opcua.clientKeyPathDesc',
    'CA Cert Path': 'driverField.opcua.caCertPath',
    'Path to CA certificate for server verification': 'driverField.opcua.caCertPathDesc',
    'Session Timeout (ms)': 'driverField.opcua.sessionTimeout',
    'OPC UA session timeout in milliseconds': 'driverField.opcua.sessionTimeoutDesc',
    'Subscription Interval (ms)': 'driverField.opcua.subscriptionInterval',
    'Subscription publishing interval in milliseconds': 'driverField.opcua.subscriptionIntervalDesc',
    'Deadband Type (Native)': 'driverField.opcua.deadbandTypeNative',
    'OPC UA native deadband type (Absolute/Percent)': 'driverField.opcua.deadbandTypeNativeDesc',
    'Deadband Value (Native)': 'driverField.opcua.deadbandValueNative',
    'Native deadband value for server-side filtering': 'driverField.opcua.deadbandValueNativeDesc',
    'Use Native Deadband': 'driverField.opcua.useNativeDeadband',
    'Use OPC UA server-side deadband filtering': 'driverField.opcua.useNativeDeadbandDesc',
    'Use Subscription': 'driverField.opcua.useSubscription',
    'Use subscription-based monitoring instead of polling': 'driverField.opcua.useSubscriptionDesc',
    'Collection Mode': 'driverField.opcua.collectionMode',
    'Data collection mode (Poll/Subscribe/Hybrid)': 'driverField.opcua.collectionModeDesc',
    'Deadband (Software)': 'driverField.opcua.deadbandSoftware',
    'Software-side deadband filter threshold': 'driverField.opcua.deadbandSoftwareDesc',
    'Scaling': 'driverField.opcua.scaling',
    'Linear scaling: y = x * ratio + offset': 'driverField.opcua.scalingDesc',
    'Clamp': 'driverField.opcua.clamp',
    'Value range validation': 'driverField.opcua.clampDesc',
    'Rate of Change': 'driverField.opcua.rocThreshold',
    'Rate of change threshold for data credibility': 'driverField.opcua.rocThresholdDesc',
    'Frozen Count': 'driverField.opcua.frozenCount',
    'Consecutive identical readings to detect frozen value': 'driverField.opcua.frozenCountDesc',
    'Write Type Strategy': 'driverField.opcua.writeTypeStrategy',
    'Strategy for writing typed values (Coerce/Strict/BestEffort)': 'driverField.opcua.writeTypeStrategyDesc',
    'Backup Client Cert Path': 'driverField.opcua.backupClientCertPath',
    'Path to backup client certificate for redundant endpoint': 'driverField.opcua.backupClientCertPathDesc',
    'Backup Client Key Path': 'driverField.opcua.backupClientKeyPath',
    'Path to backup client private key for redundant endpoint': 'driverField.opcua.backupClientKeyPathDesc',
    'Backup CA Cert Path': 'driverField.opcua.backupCaCertPath',
    'Path to backup CA certificate for redundant endpoint': 'driverField.opcua.backupCaCertPathDesc',
  },
  'MQTT Client': {
    'MQTT message queue telemetry transport protocol': 'driverField.mqtt-client.desc',
    'Broker': 'driverField.mqtt-client.broker',
    'MQTT broker address': 'driverField.mqtt-client.brokerDesc',
    'Topic': 'driverField.mqtt-client.topic',
    'MQTT subscribe/publish topic': 'driverField.mqtt-client.topicDesc',
    'QoS': 'driverField.mqtt-client.qos',
    'Broker Address': 'driverField.mqtt-client.brokerAddr',
    'MQTT broker URL (e.g. tcp://host:1883)': 'driverField.mqtt-client.brokerAddrDesc',
    'Port': 'driverField.mqtt-client.port',
    'MQTT broker port (default 1883, TLS 8883)': 'driverField.mqtt-client.portDesc',
    'Username': 'driverField.mqtt-client.username',
    'MQTT authentication username': 'driverField.mqtt-client.usernameDesc',
    'Password': 'driverField.mqtt-client.password',
    'MQTT authentication password': 'driverField.mqtt-client.passwordDesc',
    'Subscribe Topic': 'driverField.mqtt-client.subscribeTopic',
    'Topic pattern to subscribe for incoming data': 'driverField.mqtt-client.subscribeTopicDesc',
    'Will Topic': 'driverField.mqtt-client.willTopic',
    'Last Will and Testament topic': 'driverField.mqtt-client.willTopicDesc',
    'Will Message': 'driverField.mqtt-client.willMessage',
    'Last Will and Testament message payload': 'driverField.mqtt-client.willMessageDesc',
    'Will QoS': 'driverField.mqtt-client.willQos',
    'Last Will QoS level (0/1/2)': 'driverField.mqtt-client.willQosDesc',
    'Will Retain': 'driverField.mqtt-client.willRetain',
    'Whether the Will message should be retained': 'driverField.mqtt-client.willRetainDesc',
    'Clean Session': 'driverField.mqtt-client.cleanSession',
    'Start a clean session on connect': 'driverField.mqtt-client.cleanSessionDesc',
    'Client ID': 'driverField.mqtt-client.clientId',
    'MQTT client identifier (empty for auto-generated)': 'driverField.mqtt-client.clientIdDesc',
    'Enable TLS': 'driverField.mqtt-client.enableTls',
    'Enable TLS/SSL encryption for MQTT connection': 'driverField.mqtt-client.enableTlsDesc',
    'CA Certificate': 'driverField.mqtt-client.caCert',
    'Path to CA certificate file for TLS': 'driverField.mqtt-client.caCertDesc',
    'Client Certificate': 'driverField.mqtt-client.clientCert',
    'Path to client certificate file for mutual TLS': 'driverField.mqtt-client.clientCertDesc',
    'Client Key': 'driverField.mqtt-client.clientKey',
    'Path to client private key file for mutual TLS': 'driverField.mqtt-client.clientKeyDesc',
    'Cert Verify Mode': 'driverField.mqtt-client.certVerifyMode',
    'Certificate verification mode (None/Peer/PeerWithCA)': 'driverField.mqtt-client.certVerifyModeDesc',
    'Topic Routes': 'driverField.mqtt-client.topicRoutes',
    'Topic routing rules for mapping topics to devices/points': 'driverField.mqtt-client.topicRoutesDesc',
    'Max Payload Size (bytes)': 'driverField.mqtt-client.maxPayloadSize',
    'Maximum allowed MQTT message payload size in bytes': 'driverField.mqtt-client.maxPayloadSizeDesc',
  },
  'HTTP Webhook': {
    'HTTP Webhook for receiving external data push': 'driverField.http-webhook.desc',
    'URL': 'driverField.http-webhook.url',
    'Webhook callback URL': 'driverField.http-webhook.urlDesc',
    'Method': 'driverField.http-webhook.method',
    'HTTP request method': 'driverField.http-webhook.methodDesc',
    'Webhook URL': 'driverField.http-webhook.webhookUrl',
    'URL to receive webhook push data': 'driverField.http-webhook.webhookUrlDesc',
    'Push URL': 'driverField.http-webhook.pushUrl',
    'URL for pushing data to external system': 'driverField.http-webhook.pushUrlDesc',
    'HTTP Method': 'driverField.http-webhook.httpMethod',
    'HTTP method for push requests (GET/POST/PUT)': 'driverField.http-webhook.httpMethodDesc',
    'Poll Interval (s)': 'driverField.http-webhook.pollInterval',
    'HTTP polling interval in seconds': 'driverField.http-webhook.pollIntervalDesc',
    'Timeout (s)': 'driverField.http-webhook.timeout',
    'Total request timeout in seconds': 'driverField.http-webhook.timeoutDesc',
    'Connect Timeout (s)': 'driverField.http-webhook.connectTimeout',
    'TCP connection timeout in seconds': 'driverField.http-webhook.connectTimeoutDesc',
    'Read Timeout (s)': 'driverField.http-webhook.readTimeout',
    'Read data timeout in seconds': 'driverField.http-webhook.readTimeoutDesc',
    'Write Timeout (s)': 'driverField.http-webhook.writeTimeout',
    'Write data timeout in seconds': 'driverField.http-webhook.writeTimeoutDesc',
    'Max Connections': 'driverField.http-webhook.maxConnections',
    'Maximum concurrent HTTP connections': 'driverField.http-webhook.maxConnectionsDesc',
    'Max Keepalive': 'driverField.http-webhook.maxKeepalive',
    'Maximum keepalive connections': 'driverField.http-webhook.maxKeepaliveDesc',
    'Health Check Timeout (s)': 'driverField.http-webhook.healthCheckTimeout',
    'Health check request timeout in seconds': 'driverField.http-webhook.healthCheckTimeoutDesc',
    'Health Response Threshold (s)': 'driverField.http-webhook.healthResponseThreshold',
    'Response time threshold for health degradation in seconds': 'driverField.http-webhook.healthResponseThresholdDesc',
    'DNS Cache TTL (s)': 'driverField.http-webhook.dnsCacheTtl',
    'DNS cache time-to-live in seconds': 'driverField.http-webhook.dnsCacheTtlDesc',
    'Rate of Change': 'driverField.http-webhook.rocThreshold',
    'Rate of change threshold for data credibility': 'driverField.http-webhook.rocThresholdDesc',
    'Frozen Count': 'driverField.http-webhook.frozenCount',
    'Consecutive identical readings to detect frozen value': 'driverField.http-webhook.frozenCountDesc',
    'Max Payload Size (bytes)': 'driverField.http-webhook.maxPayloadSize',
    'Maximum allowed request payload size in bytes': 'driverField.http-webhook.maxPayloadSizeDesc',
    'Write Rate Limit (ms)': 'driverField.http-webhook.writeRateLimit',
    'Minimum interval between write requests in milliseconds': 'driverField.http-webhook.writeRateLimitDesc',
    'Authentication': 'driverField.http-webhook.authentication',
    'Authentication method for outgoing requests': 'driverField.http-webhook.authenticationDesc',
    'Auth Token': 'driverField.http-webhook.authToken',
    'Bearer token or API key for authentication': 'driverField.http-webhook.authTokenDesc',
    'Custom Headers': 'driverField.http-webhook.customHeaders',
    'Custom HTTP headers as JSON object': 'driverField.http-webhook.customHeadersDesc',
    'Body Template': 'driverField.http-webhook.bodyTemplate',
    'JSON template for request body': 'driverField.http-webhook.bodyTemplateDesc',
    'Body Type': 'driverField.http-webhook.bodyType',
    'Request body format (JSON/Form/Raw)': 'driverField.http-webhook.bodyTypeDesc',
    'Max Retries': 'driverField.http-webhook.maxRetries',
    'Maximum retry attempts for failed requests': 'driverField.http-webhook.maxRetriesDesc',
    'Retry Backoff (s)': 'driverField.http-webhook.retryBackoff',
    'Exponential backoff base interval in seconds': 'driverField.http-webhook.retryBackoffDesc',
    'Allowed Hosts': 'driverField.http-webhook.allowedHosts',
    'Comma-separated list of allowed hostnames for security': 'driverField.http-webhook.allowedHostsDesc',
    'Deadband': 'driverField.http-webhook.deadband',
    'Deadband filter threshold': 'driverField.http-webhook.deadbandDesc',
    'Scaling': 'driverField.http-webhook.scaling',
    'Linear scaling: y = x * ratio + offset': 'driverField.http-webhook.scalingDesc',
    'Clamp': 'driverField.http-webhook.clamp',
    'Value range validation': 'driverField.http-webhook.clampDesc',
  },
  'Siemens S7': {
    'Siemens S7 protocol for S7-200/300/400/1200/1500 PLCs': 'driverField.siemens-s7.desc',
    'Host': 'driverField.siemens-s7.host',
    'PLC IP address': 'driverField.siemens-s7.hostDesc',
    'Rack': 'driverField.siemens-s7.rack',
    'PLC rack number': 'driverField.siemens-s7.rackDesc',
    'Slot': 'driverField.siemens-s7.slot',
    'PLC slot number': 'driverField.siemens-s7.slotDesc',
    'CPU Type': 'driverField.siemens-s7.cpuType',
    'Siemens CPU model': 'driverField.siemens-s7.cpuTypeDesc',
    'IP Address': 'driverField.siemens-s7.ipAddr',
    'PLC IP address (e.g. 192.168.1.100)': 'driverField.siemens-s7.ipAddrDesc',
    'Port': 'driverField.siemens-s7.port',
    'S7 communication port (default 102)': 'driverField.siemens-s7.portDesc',
    'Connection Timeout (s)': 'driverField.siemens-s7.connectionTimeout',
    'Connection timeout in seconds': 'driverField.siemens-s7.connectionTimeoutDesc',
    'Heartbeat Interval (s)': 'driverField.siemens-s7.heartbeatInterval',
    'S7 keepalive heartbeat interval in seconds': 'driverField.siemens-s7.heartbeatIntervalDesc',
    'PDU Size': 'driverField.siemens-s7.pduSize',
    'Negotiated PDU size, 0=auto': 'driverField.siemens-s7.pduSizeDesc',
    'PLC Model': 'driverField.siemens-s7.plcModel',
    'Siemens PLC model selection': 'driverField.siemens-s7.plcModelDesc',
    'Optimized DB Access': 'driverField.siemens-s7.optimizedDbAccess',
    'Enable S7-1200/1500 optimized block access': 'driverField.siemens-s7.optimizedDbAccessDesc',
    'DB Number': 'driverField.siemens-s7.dbNumber',
    'Default DB block number': 'driverField.siemens-s7.dbNumberDesc',
    'Password': 'driverField.siemens-s7.password',
    'PLC protection password': 'driverField.siemens-s7.passwordDesc',
    'Local TSAP': 'driverField.siemens-s7.localTsap',
    'Local TSAP identifier': 'driverField.siemens-s7.localTsapDesc',
    'Remote TSAP': 'driverField.siemens-s7.remoteTsap',
    'Remote TSAP identifier': 'driverField.siemens-s7.remoteTsapDesc',
    'Deadband': 'driverField.siemens-s7.deadband',
    'Deadband filter threshold': 'driverField.siemens-s7.deadbandDesc',
    'Scaling': 'driverField.siemens-s7.scaling',
    'Linear scaling: y = x * ratio + offset': 'driverField.siemens-s7.scalingDesc',
    'Clamp': 'driverField.siemens-s7.clamp',
    'Value range validation': 'driverField.siemens-s7.clampDesc',
    'Backup IP': 'driverField.siemens-s7.backupIp',
    'Backup PLC IP for link redundancy': 'driverField.siemens-s7.backupIpDesc',
  },
  'Mitsubishi MC': {
    'Mitsubishi MC protocol for Q/FX series PLCs': 'driverField.mitsubishi-mc.desc',
    'Network No': 'driverField.mitsubishi-mc.networkNo',
    'Station No': 'driverField.mitsubishi-mc.stationNo',
    'IP Address': 'driverField.mitsubishi-mc.ipAddr',
    'PLC IP address': 'driverField.mitsubishi-mc.ipAddrDesc',
    'Backup IP': 'driverField.mitsubishi-mc.backupIp',
    'Backup PLC IP for link redundancy': 'driverField.mitsubishi-mc.backupIpDesc',
    'Port': 'driverField.mitsubishi-mc.port',
    'MC protocol port (default 5007)': 'driverField.mitsubishi-mc.portDesc',
    'PLC Type': 'driverField.mitsubishi-mc.plcType',
    'Mitsubishi PLC series': 'driverField.mitsubishi-mc.plcTypeDesc',
    'Frame Format': 'driverField.mitsubishi-mc.frameFormat',
    'MC protocol frame format (3E/4E/3C/4C)': 'driverField.mitsubishi-mc.frameFormatDesc',
    'Network No.': 'driverField.mitsubishi-mc.networkNoDot',
    'Network number for routing': 'driverField.mitsubishi-mc.networkNoDotDesc',
    'PC No.': 'driverField.mitsubishi-mc.pcNo',
    'PC number for routing': 'driverField.mitsubishi-mc.pcNoDesc',
    'Device Type': 'driverField.mitsubishi-mc.deviceType',
    'Default Mitsubishi device type code': 'driverField.mitsubishi-mc.deviceTypeDesc',
    'Batch Size': 'driverField.mitsubishi-mc.batchSize',
    'Maximum words per read request': 'driverField.mitsubishi-mc.batchSizeDesc',
    'Timeout (s)': 'driverField.mitsubishi-mc.timeout',
    'Connection and read timeout': 'driverField.mitsubishi-mc.timeoutDesc',
    'SLMP Direct Mode (FX5U)': 'driverField.mitsubishi-mc.slmpDirectMode',
    'Enable SLMP direct access mode for FX5U': 'driverField.mitsubishi-mc.slmpDirectModeDesc',
    'Deadband': 'driverField.mitsubishi-mc.deadband',
    'Deadband filter threshold': 'driverField.mitsubishi-mc.deadbandDesc',
    'Scaling Ratio': 'driverField.mitsubishi-mc.scalingRatio',
    'Linear scaling ratio (multiplier)': 'driverField.mitsubishi-mc.scalingRatioDesc',
    'Scaling Offset': 'driverField.mitsubishi-mc.scalingOffset',
    'Linear scaling offset (addend)': 'driverField.mitsubishi-mc.scalingOffsetDesc',
    'Clamp Min': 'driverField.mitsubishi-mc.clampMin',
    'Minimum allowed value for clamping': 'driverField.mitsubishi-mc.clampMinDesc',
    'Clamp Max': 'driverField.mitsubishi-mc.clampMax',
    'Maximum allowed value for clamping': 'driverField.mitsubishi-mc.clampMaxDesc',
    'Rate of Change Threshold': 'driverField.mitsubishi-mc.rocThreshold',
    'Rate of change threshold for data credibility': 'driverField.mitsubishi-mc.rocThresholdDesc',
    'Frozen Value Count': 'driverField.mitsubishi-mc.frozenCount',
    'Consecutive identical readings to detect frozen value': 'driverField.mitsubishi-mc.frozenCountDesc',
    'Collect Interval (s)': 'driverField.mitsubishi-mc.collectInterval',
    'Data collection interval in seconds': 'driverField.mitsubishi-mc.collectIntervalDesc',
    'Byte Order': 'driverField.mitsubishi-mc.byteOrder',
    'Multi-register byte order': 'driverField.mitsubishi-mc.byteOrderDesc',
    'TS Storage Enabled': 'driverField.mitsubishi-mc.tsStorageEnabled',
    'Enable time-series data storage': 'driverField.mitsubishi-mc.tsStorageEnabledDesc',
    'InfluxDB URL': 'driverField.mitsubishi-mc.influxdbUrl',
    'InfluxDB server URL': 'driverField.mitsubishi-mc.influxdbUrlDesc',
    'InfluxDB Org': 'driverField.mitsubishi-mc.influxdbOrg',
    'InfluxDB organization name': 'driverField.mitsubishi-mc.influxdbOrgDesc',
    'InfluxDB Bucket': 'driverField.mitsubishi-mc.influxdbBucket',
    'InfluxDB bucket name': 'driverField.mitsubishi-mc.influxdbBucketDesc',
    'InfluxDB Token': 'driverField.mitsubishi-mc.influxdbToken',
    'InfluxDB authentication token': 'driverField.mitsubishi-mc.influxdbTokenDesc',
  },
  'Omron FINS': {
    'Omron FINS protocol for CJ/CP series PLCs': 'driverField.omron-fins.desc',
    'IP Address': 'driverField.omron-fins.ipAddr',
    'Omron PLC IP address': 'driverField.omron-fins.ipAddrDesc',
    'Port': 'driverField.omron-fins.port',
    'FINS protocol port (default 9600)': 'driverField.omron-fins.portDesc',
    'Backup IP': 'driverField.omron-fins.backupIp',
    'Backup PLC IP for redundancy': 'driverField.omron-fins.backupIpDesc',
    'Backup Port': 'driverField.omron-fins.backupPort',
    'Backup FINS port for redundancy': 'driverField.omron-fins.backupPortDesc',
    'Transport': 'driverField.omron-fins.transport',
    'TCP or UDP transport': 'driverField.omron-fins.transportDesc',
    'Batch Size': 'driverField.omron-fins.batchSize',
    'Maximum words per read request': 'driverField.omron-fins.batchSizeDesc',
    'Source Node': 'driverField.omron-fins.sourceNode',
    'Local gateway FINS node number': 'driverField.omron-fins.sourceNodeDesc',
    'Destination Node': 'driverField.omron-fins.destNode',
    'Remote PLC FINS node number': 'driverField.omron-fins.destNodeDesc',
    'Network No.': 'driverField.omron-fins.networkNo',
    'FINS network number': 'driverField.omron-fins.networkNoDesc',
    'Unit No.': 'driverField.omron-fins.unitNo',
    'FINS unit number': 'driverField.omron-fins.unitNoDesc',
    'Command Code': 'driverField.omron-fins.commandCode',
    'FINS command code for custom operations': 'driverField.omron-fins.commandCodeDesc',
    'Direct Mode (CS/CJ2)': 'driverField.omron-fins.directMode',
    'Enable FINS direct mode for CS/CJ2 series': 'driverField.omron-fins.directModeDesc',
    'PLC Series': 'driverField.omron-fins.plcSeries',
    'Omron PLC series (CS/CJ/CP/NJ)': 'driverField.omron-fins.plcSeriesDesc',
    'Deadband': 'driverField.omron-fins.deadband',
    'Deadband filter threshold': 'driverField.omron-fins.deadbandDesc',
    'Scaling': 'driverField.omron-fins.scaling',
    'Linear scaling: y = x * ratio + offset': 'driverField.omron-fins.scalingDesc',
    'Clamp': 'driverField.omron-fins.clamp',
    'Value range validation': 'driverField.omron-fins.clampDesc',
    'Frozen Threshold': 'driverField.omron-fins.frozenThreshold',
    'Consecutive identical readings to detect frozen value': 'driverField.omron-fins.frozenThresholdDesc',
    'Rate of Change Limit': 'driverField.omron-fins.rocLimit',
    'Rate of change limit for data credibility': 'driverField.omron-fins.rocLimitDesc',
    'Points Config': 'driverField.omron-fins.pointsConfig',
    'FINS point address mapping configuration': 'driverField.omron-fins.pointsConfigDesc',
    'Write Verify': 'driverField.omron-fins.writeVerify',
    'Enable read-verify-write after write operations': 'driverField.omron-fins.writeVerifyDesc',
    'Write Rate Limit (ms)': 'driverField.omron-fins.writeRateLimit',
    'Minimum interval between write operations in milliseconds': 'driverField.omron-fins.writeRateLimitDesc',
    'Write Audit': 'driverField.omron-fins.writeAudit',
    'Enable write operation audit logging': 'driverField.omron-fins.writeAuditDesc',
    'Max Response Size': 'driverField.omron-fins.maxResponseSize',
    'Maximum FINS response data size in bytes': 'driverField.omron-fins.maxResponseSizeDesc',
  },
  'Allen-Bradley': {
    'Allen-Bradley CIP protocol for ControlLogix/CompactLogix': 'driverField.allen-bradley.desc',
    'IP Address': 'driverField.allen-bradley.ipAddr',
    'AB PLC IP address': 'driverField.allen-bradley.ipAddrDesc',
    'Port': 'driverField.allen-bradley.port',
    'EtherNet/IP port (default 44818)': 'driverField.allen-bradley.portDesc',
    'Slot': 'driverField.allen-bradley.slot',
    'Controller slot number (0 for CompactLogix)': 'driverField.allen-bradley.slotDesc',
    'CIP Connection Timeout (s)': 'driverField.allen-bradley.cipConnTimeout',
    'CIP explicit message connection timeout': 'driverField.allen-bradley.cipConnTimeoutDesc',
    'Connection Type': 'driverField.allen-bradley.connectionType',
    'CIP connection type (Explicit/IO)': 'driverField.allen-bradley.connectionTypeDesc',
    'PLC Model': 'driverField.allen-bradley.plcModel',
    'Allen-Bradley PLC model (ControlLogix/CompactLogix/Micro800)': 'driverField.allen-bradley.plcModelDesc',
    'Large Forward Open': 'driverField.allen-bradley.largeForwardOpen',
    'Use Large Forward Open for connections > 509 bytes CIP data': 'driverField.allen-bradley.largeForwardOpenDesc',
    'CIP Username': 'driverField.allen-bradley.cipUsername',
    'CIP security username for Logix5000': 'driverField.allen-bradley.cipUsernameDesc',
    'CIP Password': 'driverField.allen-bradley.cipPassword',
    'CIP security password for Logix5000': 'driverField.allen-bradley.cipPasswordDesc',
    'Default Tag/Address': 'driverField.allen-bradley.defaultTag',
    'Default tag or address for data access': 'driverField.allen-bradley.defaultTagDesc',
    'Watchdog Interval (s)': 'driverField.allen-bradley.watchdogInterval',
    'CIP connection watchdog timeout in seconds': 'driverField.allen-bradley.watchdogIntervalDesc',
    'Watchdog Check Mode': 'driverField.allen-bradley.watchdogCheckMode',
    'Watchdog check mode (Timeout/Active/Passive)': 'driverField.allen-bradley.watchdogCheckModeDesc',
    'Backup IP': 'driverField.allen-bradley.backupIp',
    'Backup PLC IP for link redundancy': 'driverField.allen-bradley.backupIpDesc',
    'Backup Port': 'driverField.allen-bradley.backupPort',
    'Backup EtherNet/IP port': 'driverField.allen-bradley.backupPortDesc',
    'Failover Threshold': 'driverField.allen-bradley.failoverThreshold',
    'Consecutive failures before switching to backup': 'driverField.allen-bradley.failoverThresholdDesc',
    'Auto Revert': 'driverField.allen-bradley.autoRevert',
    'Automatically revert to primary when recovered': 'driverField.allen-bradley.autoRevertDesc',
    'Deadband': 'driverField.allen-bradley.deadband',
    'Deadband filter threshold': 'driverField.allen-bradley.deadbandDesc',
    'Scaling Ratio': 'driverField.allen-bradley.scalingRatio',
    'Linear scaling ratio (multiplier)': 'driverField.allen-bradley.scalingRatioDesc',
    'Scaling Offset': 'driverField.allen-bradley.scalingOffset',
    'Linear scaling offset (addend)': 'driverField.allen-bradley.scalingOffsetDesc',
    'Clamp Min': 'driverField.allen-bradley.clampMin',
    'Minimum allowed value for clamping': 'driverField.allen-bradley.clampMinDesc',
    'Clamp Max': 'driverField.allen-bradley.clampMax',
    'Maximum allowed value for clamping': 'driverField.allen-bradley.clampMaxDesc',
  },
  'ONVIF': {
    'ONVIF IP camera protocol': 'driverField.onvif.desc',
    'Camera IP': 'driverField.onvif.cameraIp',
    'ONVIF camera IP address': 'driverField.onvif.cameraIpDesc',
    'IP Address': 'driverField.onvif.ipAddr',
    'ONVIF device IP address': 'driverField.onvif.ipAddrDesc',
    'Port': 'driverField.onvif.port',
    'ONVIF port (default 80)': 'driverField.onvif.portDesc',
    'Username': 'driverField.onvif.username',
    'ONVIF device authentication username': 'driverField.onvif.usernameDesc',
    'Password': 'driverField.onvif.password',
    'ONVIF device authentication password': 'driverField.onvif.passwordDesc',
    'Auth Type': 'driverField.onvif.authType',
    'ONVIF authentication type (Digest/Basic)': 'driverField.onvif.authTypeDesc',
    'Timeout (s)': 'driverField.onvif.timeout',
    'ONVIF request timeout in seconds': 'driverField.onvif.timeoutDesc',
    'Connect Timeout (s)': 'driverField.onvif.connectTimeout',
    'TCP connection timeout in seconds': 'driverField.onvif.connectTimeoutDesc',
    'Read Timeout (s)': 'driverField.onvif.readTimeout',
    'Read data timeout in seconds': 'driverField.onvif.readTimeoutDesc',
    'PTZ Timeout (s)': 'driverField.onvif.ptzTimeout',
    'PTZ control command timeout in seconds': 'driverField.onvif.ptzTimeoutDesc',
    'WSDL Directory': 'driverField.onvif.wsdlDir',
    'Local directory for cached WSDL files': 'driverField.onvif.wsdlDirDesc',
    'Allow Private RTSP': 'driverField.onvif.allowPrivateRtsp',
    'Allow RTSP streaming on private network interfaces': 'driverField.onvif.allowPrivateRtspDesc',
    'Deadband': 'driverField.onvif.deadband',
    'Deadband filter threshold': 'driverField.onvif.deadbandDesc',
    'Scaling': 'driverField.onvif.scaling',
    'Linear scaling: y = x * ratio + offset': 'driverField.onvif.scalingDesc',
    'Clamp': 'driverField.onvif.clamp',
    'Value range validation': 'driverField.onvif.clampDesc',
  },
  'Simulator': {
    'Built-in simulator for testing and demo': 'driverField.simulator.desc',
    'Update Interval (s)': 'driverField.simulator.updateInterval',
    'Simulated value update interval in seconds': 'driverField.simulator.updateIntervalDesc',
    'Min Value': 'driverField.simulator.minValue',
    'Minimum simulated value': 'driverField.simulator.minValueDesc',
    'Max Value': 'driverField.simulator.maxValue',
    'Maximum simulated value': 'driverField.simulator.maxValueDesc',
    'Noise Amplitude': 'driverField.simulator.noiseAmplitude',
    'Random noise amplitude': 'driverField.simulator.noiseAmplitudeDesc',
    'Trend Drift/s': 'driverField.simulator.trendDrift',
    'Value drift rate per second': 'driverField.simulator.trendDriftDesc',
    'Timeout (s)': 'driverField.simulator.timeout',
    'Simulator response timeout': 'driverField.simulator.timeoutDesc',
    'Simulation Mode': 'driverField.simulator.simulationMode',
    'Simulation waveform mode (Sine/RandomWalk/Uniform/Fixed)': 'driverField.simulator.simulationModeDesc',
    'Period (s)': 'driverField.simulator.period',
    'Waveform period in seconds': 'driverField.simulator.periodDesc',
    'Custom Formula': 'driverField.simulator.customFormula',
    'Custom Python formula for value generation': 'driverField.simulator.customFormulaDesc',
    'Fault Simulation': 'driverField.simulator.faultSimulation',
    'Enable fault injection simulation': 'driverField.simulator.faultSimulationDesc',
    'Fault Rate (%)': 'driverField.simulator.faultRate',
    'Probability of fault injection (0-100%)': 'driverField.simulator.faultRateDesc',
    'Deadband': 'driverField.simulator.deadband',
    'Deadband filter threshold': 'driverField.simulator.deadbandDesc',
    'Deadband Type': 'driverField.simulator.deadbandType',
    'Deadband type (Absolute/Percent)': 'driverField.simulator.deadbandTypeDesc',
    'Scaling Ratio': 'driverField.simulator.scalingRatio',
    'Linear scaling ratio (multiplier)': 'driverField.simulator.scalingRatioDesc',
    'Scaling Offset': 'driverField.simulator.scalingOffset',
    'Linear scaling offset (addend)': 'driverField.simulator.scalingOffsetDesc',
    'Clamp Min': 'driverField.simulator.clampMin',
    'Minimum allowed value for clamping': 'driverField.simulator.clampMinDesc',
    'Clamp Max': 'driverField.simulator.clampMax',
    'Maximum allowed value for clamping': 'driverField.simulator.clampMaxDesc',
    'Rate of Change Threshold': 'driverField.simulator.rocThreshold',
    'Rate of change threshold for data credibility': 'driverField.simulator.rocThresholdDesc',
    'Frozen Detection Count': 'driverField.simulator.frozenCount',
    'Consecutive identical readings to detect frozen value': 'driverField.simulator.frozenCountDesc',
    'Write Hold (s)': 'driverField.simulator.writeHold',
    'Duration to hold a written value before reverting to simulation': 'driverField.simulator.writeHoldDesc',
  },
  'Modbus Slave': {
    'Modbus TCP slave/server simulator for testing': 'driverField.modbus-slave.desc',
    'Bind Address': 'driverField.modbus-slave.bindAddr',
    'IP address to bind the Modbus TCP slave server': 'driverField.modbus-slave.bindAddrDesc',
    'Port': 'driverField.modbus-slave.port',
    'Modbus TCP slave port (default 502)': 'driverField.modbus-slave.portDesc',
    'Unit ID': 'driverField.modbus-slave.unitId',
    'Modbus unit identifier for the slave': 'driverField.modbus-slave.unitIdDesc',
    'Timeout (s)': 'driverField.modbus-slave.timeout',
    'Slave connection timeout in seconds': 'driverField.modbus-slave.timeoutDesc',
    'Slave ID Range': 'driverField.modbus-slave.slaveIdRange',
    'Allowed slave ID range (e.g. 1-247)': 'driverField.modbus-slave.slaveIdRangeDesc',
    'Byte Order': 'driverField.modbus-slave.byteOrder',
    'Multi-register byte order': 'driverField.modbus-slave.byteOrderDesc',
    'Deadband': 'driverField.modbus-slave.deadband',
    'Deadband filter threshold': 'driverField.modbus-slave.deadbandDesc',
    'Scaling': 'driverField.modbus-slave.scaling',
    'Linear scaling: y = x * ratio + offset': 'driverField.modbus-slave.scalingDesc',
    'Clamp': 'driverField.modbus-slave.clamp',
    'Value range validation': 'driverField.modbus-slave.clampDesc',
    'Max Connections': 'driverField.modbus-slave.maxConnections',
    'Maximum concurrent TCP connections': 'driverField.modbus-slave.maxConnectionsDesc',
    'Allowed IPs': 'driverField.modbus-slave.allowedIps',
    'Comma-separated list of allowed client IPs': 'driverField.modbus-slave.allowedIpsDesc',
    'Abuse Threshold': 'driverField.modbus-slave.abuseThreshold',
    'Number of bad requests before blocking a client': 'driverField.modbus-slave.abuseThresholdDesc',
    'Abuse Window (s)': 'driverField.modbus-slave.abuseWindow',
    'Time window in seconds for abuse detection': 'driverField.modbus-slave.abuseWindowDesc',
    'Ban Duration (s)': 'driverField.modbus-slave.banDuration',
    'Duration to ban abusive clients in seconds': 'driverField.modbus-slave.banDurationDesc',
    'Audit Write': 'driverField.modbus-slave.auditWrite',
    'Enable write operation audit logging': 'driverField.modbus-slave.auditWriteDesc',
  },
  'OPC DA': {
    'OPC DA Classic protocol for legacy SCADA systems (Windows DCOM)': 'driverField.opc-da.desc',
    'ProgID': 'driverField.opc-da.progId',
    'OPC DA server program identifier': 'driverField.opc-da.progIdDesc',
    'Host': 'driverField.opc-da.host',
    'Remote OPC DA server host name or IP': 'driverField.opc-da.hostDesc',
    'Gateway': 'driverField.opc-da.gateway',
    'OPC DA gateway address for non-Windows platforms': 'driverField.opc-da.gatewayDesc',
    'Connect Timeout (s)': 'driverField.opc-da.connectTimeout',
    'DCOM connection timeout in seconds': 'driverField.opc-da.connectTimeoutDesc',
    'DCOM Username': 'driverField.opc-da.dcomUsername',
    'DCOM authentication username': 'driverField.opc-da.dcomUsernameDesc',
    'DCOM Password': 'driverField.opc-da.dcomPassword',
    'DCOM authentication password': 'driverField.opc-da.dcomPasswordDesc',
    'Use Groups': 'driverField.opc-da.useGroups',
    'Use OPC group-based subscription': 'driverField.opc-da.useGroupsDesc',
    'Update Rate (ms)': 'driverField.opc-da.updateRate',
    'OPC group update rate in milliseconds': 'driverField.opc-da.updateRateDesc',
    'Timeout (s)': 'driverField.opc-da.timeout',
    'OPC operation timeout in seconds': 'driverField.opc-da.timeoutDesc',
    'DCOM Call Timeout (s)': 'driverField.opc-da.dcomCallTimeout',
    'DCOM method call timeout in seconds': 'driverField.opc-da.dcomCallTimeoutDesc',
    'Deadband (%)': 'driverField.opc-da.deadband',
    'OPC DA group deadband percentage': 'driverField.opc-da.deadbandDesc',
    'Scaling': 'driverField.opc-da.scaling',
    'Linear scaling: y = x * ratio + offset': 'driverField.opc-da.scalingDesc',
    'Clamp': 'driverField.opc-da.clamp',
    'Value range validation': 'driverField.opc-da.clampDesc',
    'Rate of Change': 'driverField.opc-da.rocThreshold',
    'Rate of change threshold for data credibility': 'driverField.opc-da.rocThresholdDesc',
    'Frozen Detect': 'driverField.opc-da.frozenDetect',
    'Consecutive identical readings to detect frozen value': 'driverField.opc-da.frozenDetectDesc',
    'Watchdog Interval (s)': 'driverField.opc-da.watchdogInterval',
    'Watchdog check interval in seconds': 'driverField.opc-da.watchdogIntervalDesc',
  },
  'Video AI': {
    'Video AI inference for object detection and classification': 'driverField.video-ai.desc',
    'Video Source': 'driverField.video-ai.videoSource',
    'Video input source (RTSP URL or device ID)': 'driverField.video-ai.videoSourceDesc',
    'Model Path': 'driverField.video-ai.modelPath',
    'Path to ONNX or other AI model file': 'driverField.video-ai.modelPathDesc',
    'Confidence Threshold': 'driverField.video-ai.confidenceThreshold',
    'Minimum confidence score for detection results (0-1)': 'driverField.video-ai.confidenceThresholdDesc',
    'Input Size': 'driverField.video-ai.inputSize',
    'Model input image size (e.g. 640x640)': 'driverField.video-ai.inputSizeDesc',
    'Device Type': 'driverField.video-ai.deviceType',
    'Inference device (CPU/CUDA/TensorRT)': 'driverField.video-ai.deviceTypeDesc',
    'Poll Interval': 'driverField.video-ai.pollInterval',
    'Inference polling interval in seconds': 'driverField.video-ai.pollIntervalDesc',
    'Inference Timeout': 'driverField.video-ai.inferenceTimeout',
    'Single inference timeout in seconds': 'driverField.video-ai.inferenceTimeoutDesc',
    'Deadband': 'driverField.video-ai.deadband',
    'Deadband filter threshold': 'driverField.video-ai.deadbandDesc',
    'Scaling': 'driverField.video-ai.scaling',
    'Linear scaling: y = x * ratio + offset': 'driverField.video-ai.scalingDesc',
    'Clamp': 'driverField.video-ai.clamp',
    'Value range validation': 'driverField.video-ai.clampDesc',
    'Allowed Model Dirs': 'driverField.video-ai.allowedModelDirs',
    'Comma-separated list of allowed directories for model files': 'driverField.video-ai.allowedModelDirsDesc',
  },
}

function translateFieldText(driverName: string, text: string): string {
  if (!text) return text
  const driverMap = driverFieldI18nMap[driverName]
  if (driverMap && driverMap[text]) return t(driverMap[text])
  return text
}

// FIXED: 原问题-DriverConfig.vue全部中文硬编码，改为i18n
const drivers = ref<any[]>([])
const loading = ref(false)
const showSchemaModal = ref(false)
const currentSchema = ref<any>(null)
const currentDriverName = ref('')
const showDiscoverModal = ref(false)
const discovering = ref(false)
const discoveredDevices = ref<any[]>([])
const driverMeta = ref<Record<string, any>>({})
const loadStatusInfo = ref<any>(null)
const showOpcDaModal = ref(false)
const opcDaHost = ref('')
const opcDaServers = ref<string[]>([])
const opcDaLoading = ref(false)

function openOpcDaModal() {
  opcDaHost.value = ''
  opcDaServers.value = []
  showOpcDaModal.value = true
}

async function fetchOpcDaServers() {
  if (!opcDaHost.value) return
  opcDaLoading.value = true
  try {
    const data = await driverApi.opcDaServers(opcDaHost.value)
    opcDaServers.value = data?.servers || []
  } catch (e: any) {
    message.error(extractError(e, t('driver.opcDaFetchFailed')))
  } finally {
    opcDaLoading.value = false
  }
}

const currentSchemaTitle = computed(() => {
  const name = currentDriverName.value
  return `${name} - ${t('driver.configTemplate')}`
})

const currentDriverMeta = computed(() => {
  return driverMeta.value[currentDriverName.value] || null
})

const typeMap = computed<Record<string, string>>(() => ({
  string: t('driver.typeText'),
  integer: t('driver.typeInteger'),
  number: t('driver.typeNumber'),
  boolean: t('driver.typeBoolean'),
  array: t('driver.typeArray'),
  object: t('driver.typeObject'),
}))

const columns = computed(() => [
  { title: t('driver.colName'), key: 'name', width: 160 },
  { title: t('driver.colVersion'), key: 'version', width: 80 },
  { title: t('driver.colProtocol'), key: 'protocols', render: (row: any) => h(NSpace, { size: 4 }, () => (row.protocols ?? []).map((p: string) => h(NTag, { size: 'small', type: 'info' }, () => p))) },
  {
    title: t('dbMonitor.capabilities'),  // FIXED-P3: 替换硬编码'Capabilities'为i18n引用
    key: 'capabilities',
    width: 200,
    render: (row: any) => {
      const meta = driverMeta.value[row.name]
      if (!meta) return '-'
      const caps = meta.capabilities || {}
      const tags: any[] = []
      if (meta.experimental) {
        tags.push(h(NTag, { size: 'small', type: 'warning', bordered: false, style: 'margin: 2px' }, { default: () => t('capabilities.experimental') }))
      }
      const capLabels: Record<string, string> = {
        discover: t('capabilities.discover'),
        write: t('capabilities.write'),
        subscribe: t('capabilities.subscribe'),
        batch_read: t('capabilities.batchRead'),
        batch_write: t('capabilities.batchWrite'),
      }
      for (const [key, label] of Object.entries(capLabels)) {
        if (caps[key]) {
          tags.push(h(NTag, { size: 'small', type: 'info', bordered: false, style: 'margin: 2px' }, { default: () => label }))
        }
      }
      return tags.length > 0 ? h(NSpace, { size: 2, wrap: true }, { default: () => tags }) : h(NTag, { size: 'small', type: 'success', bordered: false }, { default: () => t('capabilities.read') })
    },
  },
  { title: t('driver.colActions'), key: 'actions', width: 340, render: (row: any) => {
    const btns: any[] = [
      h(NButton, { size: 'small', onClick: () => viewSchema(row.name) }, () => t('driver.colConfig')),
      // UX-11: 直接"创建设备"按钮，跳过 Schema 查看弹窗，减少 1 次弹窗+1 次跳转
      h(NButton, { size: 'small', type: 'primary', onClick: () => goCreateDeviceDirect(row.name) }, () => t('driver.createDevice')),
      h(NButton, { size: 'small', type: 'info', onClick: () => startDiscover(row.name) }, () => t('driver.colDiscover')),
    ]
    if (row.name === 'OPC DA' || row.protocols?.some((p: string) => p.toLowerCase().includes('opc-da') || p.toLowerCase().includes('opcda'))) {
      btns.push(h(NButton, { size: 'small', type: 'info', onClick: () => openOpcDaModal() }, () => t('driver.opcDaServers')))
    }
    return h(NSpace, {}, () => btns)
  } },
])

async function loadDrivers() {
  loading.value = true
  try {
    const data = await driverApi.list()
    drivers.value = data?.drivers || []
  } catch (e: any) { message.error(extractError(e, t('driver.loadListFailed'))) }
  finally { loading.value = false }
}

async function viewSchema(name: string) {
  currentDriverName.value = name
  try {
    const data = await driverApi.configSchema(name)
    if (data) {
      currentSchema.value = data.config_schema
      showSchemaModal.value = true
    }
  } catch (e: any) { message.error(extractError(e, t('driver.loadSchemaFailed'))) }
}

function startDiscover(name: string) {
  currentDriverName.value = name
  discoveredDevices.value = []
  showDiscoverModal.value = true
}

function goCreateDevice() {
  showSchemaModal.value = false
  router.push({
    name: 'Devices',
    query: { create: '1', driver: currentDriverName.value }
  })
}

// UX-11: 直接跳转创建设备页，不经过 Schema 弹窗
function goCreateDeviceDirect(driverName: string) {
  currentDriverName.value = driverName
  router.push({
    name: 'Devices',
    query: { create: '1', driver: driverName }
  })
}

async function doDiscover() {
  discovering.value = true
  try {
    const data = await driverApi.discover(currentDriverName.value)
    discoveredDevices.value = data?.devices || []
  } catch (e: any) { message.error(extractError(e, t('driver.discoverFailed'))) }
  finally { discovering.value = false }
}

async function loadDriverStatus() {
  try {
    const data = await driverApi.loadStatus()
    loadStatusInfo.value = data
  } catch { /* ignore */ }
}

async function loadMeta() {
  try {
    const data = await driverApi.meta()
    if (data && Array.isArray(data)) {
      const metaMap: Record<string, any> = {}
      data.forEach((item: any) => {
        metaMap[item.name] = item
      })
      driverMeta.value = metaMap
    }
  } catch {
    // Non-critical, silently ignore
  }
}

onMounted(() => { loadDrivers(); loadDriverStatus(); loadMeta() })
</script>

<style scoped>
.driver-config-page { padding: 16px; }
</style>
