import { computed } from 'vue'
import { t, useCurrentLocale } from '@/i18n'

// [AUDIT-FIX] 致命级-协议字段格式校验 pattern 定义
// IP 地址或主机名：允许 IPv4 或域名（含连字符）
export const HOST_PATTERN = '^(\\d{1,3}\\.){3}\\d{1,3}$|^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?(\\.[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?)*$'
// URL：http/https 协议
export const URL_PATTERN = '^https?://.+'
// OPC UA endpoint：opc.tcp 协议
export const OPCUA_ENDPOINT_PATTERN = '^opc\\.tcp://.+'

export interface ProtocolFieldDef {
  key: string
  label: string
  placeholder?: string
  tooltip?: string
  default?: any
  required?: boolean
  type?: 'string' | 'password' | 'number' | 'boolean' | 'select'
  options?: { label: string; value: any }[]
  min?: number
  max?: number
  pattern?: string
  notImplemented?: boolean
}

export interface PointTemplate {
  name: string
  data_type: string
  unit?: string
  address?: string
  access_mode?: string
  access?: string
  value?: any
  description?: string
}

export interface ProtocolConfig {
  label: string
  description: string
  icon: string
  configFields: ProtocolFieldDef[]
  pointTemplates: PointTemplate[]
  capabilities?: {
    discover?: boolean
    read?: boolean
    write?: boolean
    subscribe?: boolean
    batch_read?: boolean
    batch_write?: boolean
  }
  constraints?: Array<{
    type: string
    message: string
  }>
  experimental?: boolean
}

// FIXED-P3: 顶层t()→computed，语言切换响应式
const _locale = useCurrentLocale()
function _tc(key: string) {
  void _locale.value
  return t(key)
}

export const PROTOCOL_CONFIGS = computed<Record<string, ProtocolConfig>>(() => ({
  'modbus-tcp': {
    label: _tc('protocolConfig.modbusTcp.label'),
    description: _tc('protocolConfig.modbusTcp.description'),
    icon: 'flash',
    configFields: [
      { key: 'host', label: _tc('protocolConfig.modbusTcp.host'), placeholder: _tc('protocolConfig.modbusTcp.hostPlaceholder'), required: true, type: 'string', pattern: HOST_PATTERN },
      { key: 'port', label: _tc('protocolConfig.modbusTcp.port'), placeholder: _tc('protocolConfig.modbusTcp.portPlaceholder'), default: 502, required: true, type: 'number', min: 1, max: 65535 },
      { key: 'slave_id', label: _tc('protocolConfig.modbusTcp.unitId'), placeholder: _tc('protocolConfig.modbusTcp.unitIdPlaceholder'), default: 1, required: true, type: 'number', min: 1, max: 247 },
      { key: 'timeout', label: _tc('protocolConfig.modbusTcp.timeout'), placeholder: _tc('protocolConfig.modbusTcp.timeoutPlaceholder'), default: 3, type: 'number', min: 1, max: 60 },
      { key: 'byte_order', label: _tc('protocolConfig.modbusTcp.byteOrder'), default: 'ABCD', type: 'select', options: [
        { label: `ABCD (${_tc('protocolConfig.byteOrder.bigEndian')})`, value: 'ABCD' }, { label: 'BADC', value: 'BADC' }, { label: 'CDAB', value: 'CDAB' }, { label: `DCBA (${_tc('protocolConfig.byteOrder.littleEndian')})`, value: 'DCBA' },
      ]},
      { key: 'batch_read_size', label: _tc('protocolConfig.modbusTcp.batchReadSize'), default: 125, type: 'number', min: 1, max: 125 },
      { key: 'function_code', label: _tc('protocolConfig.modbusTcp.functionCode'), default: '03', type: 'select', options: [
        { label: _tc('protocolConfig.functionCodes.fc01'), value: '01' }, { label: _tc('protocolConfig.functionCodes.fc02'), value: '02' },
        { label: _tc('protocolConfig.functionCodes.fc03'), value: '03' }, { label: _tc('protocolConfig.functionCodes.fc04'), value: '04' },
        { label: _tc('protocolConfig.functionCodes.fc05'), value: '05' }, { label: _tc('protocolConfig.functionCodes.fc06'), value: '06' },
        { label: _tc('protocolConfig.functionCodes.fc15'), value: '15' }, { label: _tc('protocolConfig.functionCodes.fc16'), value: '16' },
      ]},
      { key: 'reconnect_interval', label: _tc('protocolConfig.modbusTcp.reconnectInterval'), default: 10, type: 'number', min: 1, max: 300 },
      { key: 'max_reconnect_attempts', label: _tc('protocolConfig.modbusTcp.maxReconnectAttempts'), default: 3, type: 'number', min: 1, max: 10 },
    ],
    pointTemplates: [
      { name: 'temperature', data_type: 'float32', unit: '°C', address: 'HR_0', access_mode: 'read', description: _tc('protocolConfig.modbusTcp.pointTemperature') },
      { name: 'humidity', data_type: 'float32', unit: '%', address: 'HR_1', access_mode: 'read', description: _tc('protocolConfig.modbusTcp.pointHumidity') },
      { name: 'pressure', data_type: 'float32', unit: 'MPa', address: 'HR_2', access_mode: 'read', description: _tc('protocolConfig.modbusTcp.pointPressure') },
    ],
    capabilities: { discover: true, read: true, write: true, subscribe: false, batch_read: true, batch_write: true },
    experimental: false,
  },
  'modbus-slave': {
    label: _tc('protocolConfig.modbusSlave.label'),
    description: _tc('protocolConfig.modbusSlave.description'),
    icon: 'server',
    configFields: [
      { key: 'host', label: _tc('protocolConfig.modbusSlave.host'), placeholder: _tc('protocolConfig.modbusSlave.hostPlaceholder'), default: '0.0.0.0', required: true, type: 'string', pattern: HOST_PATTERN },
      { key: 'port', label: _tc('protocolConfig.modbusSlave.port'), default: 502, required: true, type: 'number', min: 1, max: 65535 },
      { key: 'unit_id', label: _tc('protocolConfig.modbusSlave.unitId'), default: 1, required: true, type: 'number', min: 1, max: 247 },
      { key: 'byte_order', label: _tc('protocolConfig.modbusSlave.byteOrder'), default: 'ABCD', type: 'select', options: [
        { label: `ABCD (${_tc('protocolConfig.byteOrder.bigEndian')})`, value: 'ABCD' }, { label: 'BADC', value: 'BADC' }, { label: 'CDAB', value: 'CDAB' }, { label: `DCBA (${_tc('protocolConfig.byteOrder.littleEndian')})`, value: 'DCBA' },
      ]},
      { key: 'timeout', label: _tc('protocolConfig.modbusSlave.timeout'), default: 5, type: 'number', min: 1, max: 60 },
      { key: 'max_connections', label: _tc('protocolConfig.modbusSlave.maxConnections'), default: 10, type: 'number', min: 1, max: 1000 },
      { key: 'allowed_ips', label: _tc('protocolConfig.modbusSlave.allowedIps'), type: 'string', placeholder: _tc('protocolConfig.modbusSlave.allowedIpsPlaceholder') },
      { key: 'abuse_threshold', label: _tc('protocolConfig.modbusSlave.abuseThreshold'), default: 50, type: 'number', min: 1, max: 10000 },
      { key: 'abuse_window', label: _tc('protocolConfig.modbusSlave.abuseWindow'), default: 60, type: 'number', min: 1, max: 3600 },
      { key: 'ban_duration', label: _tc('protocolConfig.modbusSlave.banDuration'), default: 300, type: 'number', min: 0, max: 86400 },
      { key: 'audit_write', label: _tc('protocolConfig.shared.writeAudit'), default: true, type: 'boolean' },
    ],
    pointTemplates: [
      { name: 'HR_0', data_type: 'uint16', access: 'rw', value: 0, description: _tc('protocolConfig.modbusSlave.pointHRDefault') },
    ],
    capabilities: { discover: false, read: true, write: true, subscribe: false, batch_read: false, batch_write: false },
    constraints: [{ type: 'protocol_note', message: _tc('protocolConfig.modbusSlave.serverModeNote') }],
    experimental: false,
  },
  'modbus-rtu': {
    label: _tc('protocolConfig.modbusRtu.label'),
    description: _tc('protocolConfig.modbusRtu.description'),
    icon: 'flash',
    configFields: [
      { key: 'port', label: _tc('protocolConfig.modbusRtu.port'), placeholder: _tc('protocolConfig.modbusRtu.portPlaceholder'), required: true, type: 'string' },
      { key: 'baudrate', label: _tc('protocolConfig.modbusRtu.baudrate'), default: 9600, required: true, type: 'select', options: [
        { label: '9600', value: 9600 }, { label: '19200', value: 19200 }, { label: '38400', value: 38400 }, { label: '115200', value: 115200 },
      ]},
      { key: 'unit_id', label: _tc('protocolConfig.modbusRtu.unitId'), placeholder: _tc('protocolConfig.modbusRtu.unitIdPlaceholder'), default: 1, required: true, type: 'number', min: 1, max: 247 },
      { key: 'parity', label: _tc('protocolConfig.modbusRtu.parity'), default: 'N', type: 'select', options: [
        { label: _tc('protocolConfig.modbusRtu.parityNone'), value: 'N' }, { label: _tc('protocolConfig.modbusRtu.parityEven'), value: 'E' }, { label: _tc('protocolConfig.modbusRtu.parityOdd'), value: 'O' },
      ]},
      { key: 'stopbits', label: _tc('protocolConfig.modbusRtu.stopbits'), default: 1, type: 'number', min: 1, max: 2 },
      { key: 'bytesize', label: _tc('protocolConfig.modbusRtu.bytesize'), default: 8, type: 'number', min: 7, max: 8 },
      { key: 'timeout', label: _tc('protocolConfig.modbusRtu.timeout'), default: 3, type: 'number', min: 1, max: 60 },
      { key: 'byte_order', label: _tc('protocolConfig.modbusRtu.byteOrder'), default: 'ABCD', type: 'select', options: [
        { label: `ABCD (${_tc('protocolConfig.byteOrder.bigEndian')})`, value: 'ABCD' }, { label: 'BADC', value: 'BADC' }, { label: 'CDAB', value: 'CDAB' }, { label: `DCBA (${_tc('protocolConfig.byteOrder.littleEndian')})`, value: 'DCBA' },
      ]},
      { key: 'batch_read_size', label: _tc('protocolConfig.modbusRtu.batchReadSize'), default: 125, type: 'number', min: 1, max: 125 },
      { key: 'function_code', label: _tc('protocolConfig.modbusRtu.functionCode'), default: '03', type: 'select', options: [
        { label: _tc('protocolConfig.functionCodes.fc01'), value: '01' }, { label: _tc('protocolConfig.functionCodes.fc02'), value: '02' },
        { label: _tc('protocolConfig.functionCodes.fc03'), value: '03' }, { label: _tc('protocolConfig.functionCodes.fc04'), value: '04' },
        { label: _tc('protocolConfig.functionCodes.fc05'), value: '05' }, { label: _tc('protocolConfig.functionCodes.fc06'), value: '06' },
        { label: _tc('protocolConfig.functionCodes.fc15'), value: '15' }, { label: _tc('protocolConfig.functionCodes.fc16'), value: '16' },
      ]},
      { key: 'reconnect_interval', label: _tc('protocolConfig.modbusRtu.reconnectInterval'), default: 10, type: 'number', min: 1, max: 300 },
      { key: 'max_reconnect_attempts', label: _tc('protocolConfig.modbusRtu.maxReconnectAttempts'), default: 3, type: 'number', min: 1, max: 10 },
    ],
    pointTemplates: [
      { name: 'temperature', data_type: 'float32', unit: '°C', address: 'HR_0', access_mode: 'read', description: _tc('protocolConfig.modbusRtu.pointTemperature') },
    ],
    capabilities: { discover: false, read: true, write: true, subscribe: false, batch_read: true, batch_write: true },
    constraints: [{ type: 'protocol_note', message: _tc('protocolConfig.modbusRtu.busConstraint') }],
    experimental: false,
  },
  'opcua': {
    label: _tc('protocolConfig.opcua.label'),
    description: _tc('protocolConfig.opcua.description'),
    icon: 'link',
    configFields: [
      { key: 'endpoint', label: _tc('protocolConfig.opcua.serverUrl'), placeholder: _tc('protocolConfig.opcua.serverUrlPlaceholder'), required: true, type: 'string', pattern: OPCUA_ENDPOINT_PATTERN },
      { key: 'username', label: _tc('protocolConfig.opcua.username'), placeholder: _tc('protocolConfig.opcua.usernamePlaceholder'), type: 'string' },
      { key: 'password', label: _tc('protocolConfig.opcua.password'), type: 'string' },
      { key: 'security_mode', label: _tc('protocolConfig.opcua.securityMode'), default: 'None', type: 'select', options: [
        { label: _tc('protocolConfig.opcua.securityNone'), value: 'None' }, { label: _tc('protocolConfig.opcua.securitySign'), value: 'Sign' }, { label: _tc('protocolConfig.opcua.securitySignAndEncrypt'), value: 'SignAndEncrypt' },
      ]},
      { key: 'security_policy', label: _tc('protocolConfig.opcua.securityPolicy'), default: 'None', type: 'select', options: [
        { label: 'None', value: 'None' }, { label: 'Basic128Rsa15', value: 'Basic128Rsa15' }, { label: 'Basic256', value: 'Basic256' }, { label: 'Basic256Sha256', value: 'Basic256Sha256' },
      ]},
      { key: 'session_timeout', label: _tc('protocolConfig.opcua.sessionTimeout'), default: 60000, type: 'number', min: 1000, max: 3600000 },
      { key: 'client_cert_path', label: _tc('protocolConfig.opcua.clientCertPath'), type: 'string' },
      { key: 'client_key_path', label: _tc('protocolConfig.opcua.clientKeyPath'), type: 'string' },
      { key: 'ca_cert_path', label: _tc('protocolConfig.opcua.caCertPath'), type: 'string' },
      { key: 'subscription_interval', label: _tc('protocolConfig.opcua.subscriptionInterval'), default: 500, type: 'number', min: 50, max: 60000 },
      { key: 'deadband_type', label: _tc('protocolConfig.opcua.deadbandType'), default: 'None', type: 'select', options: [
        { label: _tc('protocolConfig.opcua.deadbandNone'), value: 'None' }, { label: _tc('protocolConfig.opcua.deadbandAbsolute'), value: 'Absolute' }, { label: _tc('protocolConfig.opcua.deadbandPercent'), value: 'Percent' },
      ]},
      { key: 'deadband_value', label: _tc('protocolConfig.opcua.deadbandValue'), default: 0, type: 'number', min: 0 },
      { key: 'use_subscription', label: _tc('protocolConfig.opcua.useSubscription'), default: true, type: 'boolean' },
      { key: 'backup_endpoint', label: _tc('protocolConfig.opcua.backupEndpoint'), type: 'string', placeholder: _tc('protocolConfig.opcua.backupEndpointPlaceholder') },
      { key: 'max_retry_interval', label: _tc('protocolConfig.opcua.maxRetryInterval'), default: 600, type: 'number', min: 5, max: 600 },
      { key: 'jitter_enable', label: _tc('protocolConfig.opcua.jitterEnable'), default: true, type: 'boolean' },
      { key: 'cert_auto_renew', label: _tc('protocolConfig.opcua.certAutoRenew'), default: true, type: 'boolean' },
      { key: 'publishing_interval', label: _tc('protocolConfig.opcua.publishingInterval'), default: 1000, type: 'number', min: 50, max: 60000 },
      { key: 'collection_mode', label: _tc('protocolConfig.opcua.collectionMode'), default: 'auto', type: 'select', options: [
        { label: _tc('protocolConfig.opcua.collectionModeAuto'), value: 'auto' }, { label: _tc('protocolConfig.opcua.collectionModeSubscription'), value: 'subscription' }, { label: _tc('protocolConfig.opcua.collectionModePolling'), value: 'polling' },
      ]},
      { key: 'use_native_deadband', label: _tc('protocolConfig.opcua.useNativeDeadband'), default: true, type: 'boolean' },
      { key: 'sampling_interval', label: _tc('protocolConfig.opcua.samplingInterval'), default: 500, type: 'number', min: 50, max: 60000 },
      { key: 'queue_size', label: _tc('protocolConfig.opcua.queueSize'), default: 10, type: 'number', min: 1, max: 1000 },
      { key: 'discard_policy', label: _tc('protocolConfig.opcua.discardPolicy'), default: 'oldest', type: 'select', options: [
        { label: _tc('protocolConfig.opcua.discardOldest'), value: 'oldest' }, { label: _tc('protocolConfig.opcua.discardNewest'), value: 'newest' },
      ]},
      { key: 'rate_of_change_threshold', label: _tc('protocolConfig.opcua.rocThreshold'), type: 'number', min: 0 },
      { key: 'frozen_count', label: _tc('protocolConfig.opcua.frozenCount'), default: 10, type: 'number', min: 1, max: 1000 },
      { key: 'write_verify', label: _tc('protocolConfig.opcua.writeVerify'), default: true, type: 'boolean' },
      { key: 'write_rate_limit', label: _tc('protocolConfig.opcua.writeRateLimit'), default: 500, type: 'number', min: 100, max: 60000 },
      { key: 'write_type_strategy', label: _tc('protocolConfig.opcua.writeTypeStrategy'), default: 'reject', type: 'select', options: [
        { label: _tc('protocolConfig.opcua.writeTypeReject'), value: 'reject' }, { label: _tc('protocolConfig.opcua.writeTypeTruncate'), value: 'truncate' },
      ]},
      { key: 'write_audit', label: _tc('protocolConfig.opcua.writeAudit'), default: true, type: 'boolean' },
      { key: 'array_bound_check', label: _tc('protocolConfig.opcua.arrayBoundCheck'), default: true, type: 'boolean' },
    ],
    pointTemplates: [
      { name: 'value', data_type: 'float64', unit: '', address: 'ns=2;s=Node1', access_mode: 'read', description: _tc('protocolConfig.opcua.pointValue') },
    ],
    capabilities: { discover: true, read: true, write: true, subscribe: true },
    experimental: false,
  },
  'mqtt': {
    label: _tc('protocolConfig.mqtt.label'),
    description: _tc('protocolConfig.mqtt.description'),
    icon: 'radio',
    configFields: [
      { key: 'broker', label: _tc('protocolConfig.mqtt.broker'), placeholder: _tc('protocolConfig.mqtt.brokerPlaceholder'), required: true, type: 'string', pattern: HOST_PATTERN },
      { key: 'port', label: _tc('protocolConfig.mqtt.port'), default: 1883, type: 'number', min: 1, max: 65535 },
      { key: 'client_id', label: _tc('protocolConfig.mqtt.clientId'), type: 'string' },
      { key: 'username', label: _tc('protocolConfig.mqtt.username'), type: 'string' },
      { key: 'password', label: _tc('protocolConfig.mqtt.password'), type: 'string' },
      { key: 'clean_session', label: _tc('protocolConfig.mqtt.cleanSession'), default: true, type: 'boolean' },
      { key: 'keepalive', label: _tc('protocolConfig.mqtt.keepalive'), default: 60, type: 'number', min: 1, max: 600 },
      { key: 'qos', label: _tc('protocolConfig.mqtt.qos'), default: 1, type: 'select', options: [
        { label: _tc('protocolConfig.mqtt.qos0'), value: 0 }, { label: _tc('protocolConfig.mqtt.qos1'), value: 1 }, { label: _tc('protocolConfig.mqtt.qos2'), value: 2 },
      ]},
      { key: 'tls_enabled', label: _tc('protocolConfig.mqtt.tlsEnabled'), default: false, type: 'boolean' },
      { key: 'tls_fallback_enabled', label: _tc('protocolConfig.mqtt.tlsFallbackEnabled'), default: false, type: 'boolean' },
      { key: 'ca_cert', label: _tc('protocolConfig.mqtt.caCert'), type: 'string' },
      { key: 'client_cert', label: _tc('protocolConfig.mqtt.clientCert'), type: 'string' },
      { key: 'client_key', label: _tc('protocolConfig.mqtt.clientKey'), type: 'string' },
      { key: 'cert_reqs', label: _tc('protocolConfig.mqtt.certReqs'), default: 'required', type: 'select', options: [
        { label: _tc('protocolConfig.mqtt.certRequired'), value: 'required' },
        { label: _tc('protocolConfig.mqtt.certOptional'), value: 'optional' },
        { label: _tc('protocolConfig.mqtt.certNone'), value: 'none' },
      ]},
      { key: 'backup_broker', label: _tc('protocolConfig.mqtt.backupBroker'), type: 'string' },
      { key: 'backup_port', label: _tc('protocolConfig.mqtt.backupPort'), default: 1883, type: 'number', min: 1, max: 65535 },
      { key: 'backup_username', label: _tc('protocolConfig.mqtt.backupUsername'), type: 'string' },
      { key: 'backup_password', label: _tc('protocolConfig.mqtt.backupPassword'), type: 'string' },
      { key: 'reconnect_base', label: _tc('protocolConfig.mqtt.reconnectBase'), default: 5.0, type: 'number', min: 1.0, max: 60.0 },
      { key: 'reconnect_max', label: _tc('protocolConfig.mqtt.reconnectMax'), default: 300.0, type: 'number', min: 10.0, max: 600.0 },
      { key: 'network_qos_mode', label: _tc('protocolConfig.mqtt.qosMode'), default: 'auto', type: 'select', options: [
        { label: _tc('protocolConfig.mqtt.qosAuto'), value: 'auto' },
        { label: _tc('protocolConfig.mqtt.qosFixed0'), value: 'fixed0' },
        { label: _tc('protocolConfig.mqtt.qosFixed1'), value: 'fixed1' },
        { label: _tc('protocolConfig.mqtt.qosFixed2'), value: 'fixed2' },
      ]},
      { key: 'network_latency_threshold', label: _tc('protocolConfig.mqtt.latencyThreshold'), default: 500.0, type: 'number', min: 10.0 },
      { key: 'network_loss_threshold', label: _tc('protocolConfig.mqtt.lossThreshold'), default: 5.0, type: 'number', min: 0.0, max: 100.0 },
      { key: 'expected_interval', label: _tc('protocolConfig.mqtt.expectedInterval'), default: 30.0, type: 'number', min: 1.0 },
      { key: 'frozen_threshold', label: _tc('protocolConfig.mqtt.frozenThreshold'), default: 0, type: 'number', min: 0, max: 100 },
      { key: 'rate_of_change_threshold', label: _tc('protocolConfig.mqtt.rocThreshold'), default: 0.0, type: 'number', min: 0.0 },
      { key: 'pub_verify_timeout', label: _tc('protocolConfig.mqtt.pubVerifyTimeout'), default: 5.0, type: 'number', min: 1.0 },
      { key: 'pub_max_retries', label: _tc('protocolConfig.mqtt.pubMaxRetries'), default: 3, type: 'number', min: 0, max: 10 },
      { key: 'rate_limit_per_topic_ms', label: _tc('protocolConfig.mqtt.rateLimitPerTopic'), default: 100, type: 'number', min: 0 },
      { key: 'rate_limit_global_max', label: _tc('protocolConfig.mqtt.rateLimitGlobal'), default: 1000, type: 'number', min: 0 },
      { key: 'rate_limit_policy', label: _tc('protocolConfig.mqtt.rateLimitPolicy'), default: 'reject', type: 'select', options: [
        { label: _tc('protocolConfig.mqtt.policyReject'), value: 'reject' },
        { label: _tc('protocolConfig.mqtt.policyQueue'), value: 'queue' },
      ]},
      { key: 'payload_max_bytes', label: _tc('protocolConfig.mqtt.payloadMaxBytes'), default: 262144, type: 'number', min: 1024, max: 10485760 },
      { key: 'write_clamp_min', label: _tc('protocolConfig.mqtt.writeClampMin'), type: 'number' },
      { key: 'write_clamp_max', label: _tc('protocolConfig.mqtt.writeClampMax'), type: 'number' },
      { key: 'audit_log_enabled', label: _tc('protocolConfig.mqtt.auditLogEnabled'), default: false, type: 'boolean' },
      { key: 'will_topic', label: _tc('protocolConfig.mqtt.willTopic'), type: 'string' },
      { key: 'will_message', label: _tc('protocolConfig.mqtt.willMessage'), default: '{"status":"offline"}', type: 'string' },
      { key: 'will_qos', label: _tc('protocolConfig.mqtt.willQos'), default: 1, type: 'select', options: [
        { label: '0', value: 0 }, { label: '1', value: 1 }, { label: '2', value: 2 },
      ]},
      { key: 'will_retain', label: _tc('protocolConfig.mqtt.willRetain'), default: true, type: 'boolean' },
      { key: 'topic', label: _tc('protocolConfig.mqtt.topic'), placeholder: _tc('protocolConfig.mqtt.topicPlaceholder'), required: true, type: 'string' },
      { key: 'topic_routes', label: _tc('protocolConfig.mqtt.topicRoutes'), type: 'string' },
    ],
    pointTemplates: [
      { name: 'payload', data_type: 'string', unit: '', address: 'payload', access_mode: 'read', description: _tc('protocolConfig.mqtt.pointPayload') },
      { name: 'temperature', data_type: 'float32', unit: '°C', address: 'temperature', access_mode: 'read', description: _tc('protocolConfig.mqtt.pointTemp') },
      { name: 'humidity', data_type: 'float32', unit: '%RH', address: 'humidity', access_mode: 'read', description: _tc('protocolConfig.mqtt.pointHumidity') },
    ],
    capabilities: { discover: false, read: true, write: true, subscribe: true },
    constraints: [{ type: 'protocol_note', message: _tc('protocolConfig.mqtt.mqtt311Only') }],
    experimental: false,
  },
  's7': {
    label: _tc('protocolConfig.s7.label'),
    description: _tc('protocolConfig.s7.description'),
    icon: 'flash',
    configFields: [
      { key: 'ip', label: _tc('protocolConfig.s7.host'), placeholder: _tc('protocolConfig.s7.hostPlaceholder'), required: true, type: 'string', pattern: HOST_PATTERN },
      { key: 'rack', label: _tc('protocolConfig.s7.rack'), default: 0, type: 'number', min: 0, max: 7 },
      { key: 'slot', label: _tc('protocolConfig.s7.slot'), default: 1, type: 'number', min: 0, max: 31 },
      { key: 'cpu_type', label: _tc('protocolConfig.s7.cpuType'), default: 'S7-1200', type: 'select', options: [
        { label: 'S7-200', value: 'S7-200' }, { label: 'S7-300', value: 'S7-300' }, { label: 'S7-400', value: 'S7-400' },
        { label: 'S7-1200', value: 'S7-1200' }, { label: 'S7-1500', value: 'S7-1500' },
      ]},
      { key: 'port', label: _tc('protocolConfig.s7.port'), default: 102, type: 'number', min: 1, max: 65535 },
      { key: 'timeout', label: _tc('protocolConfig.s7.timeout'), default: 5, type: 'number', min: 1, max: 60 },
      { key: 'heartbeat_interval', label: _tc('protocolConfig.s7.heartbeatInterval'), default: 30, type: 'number', min: 5, max: 300 },
      { key: 'plc_model', label: _tc('protocolConfig.s7.plcModel'), default: 'auto', type: 'select', options: [
        { label: _tc('protocolConfig.s7.plcModelAuto'), value: 'auto' }, { label: 'S7-200', value: 'S7-200' },
        { label: 'S7-300', value: 'S7-300' }, { label: 'S7-400', value: 'S7-400' },
        { label: 'S7-1200', value: 'S7-1200' }, { label: 'S7-1500', value: 'S7-1500' },
      ]},
      { key: 'optimized_db', label: _tc('protocolConfig.s7.optimizedDb'), default: true, type: 'boolean' },
      { key: 'pdu_size', label: _tc('protocolConfig.s7.pduSize'), default: 0, type: 'number', min: 0, max: 960 },
      { key: 'db_number', label: _tc('protocolConfig.s7.dbNumber'), default: 1, type: 'number', min: 1, max: 65535 },
    ],
    pointTemplates: [
      { name: 'DB1_value', data_type: 'float32', unit: '', address: 'DB1.DBD0', access_mode: 'read', description: _tc('protocolConfig.s7.pointDb1') },
    ],
    capabilities: { discover: false, read: true, write: true, batch_read: true },
    experimental: false,
  },
  'mc': {
    label: _tc('protocolConfig.mc.label'),
    description: _tc('protocolConfig.mc.description'),
    icon: 'flash',
    configFields: [
      { key: 'host', label: _tc('protocolConfig.mc.host'), placeholder: _tc('protocolConfig.mc.hostPlaceholder'), required: true, type: 'string', pattern: HOST_PATTERN },
      { key: 'port', label: _tc('protocolConfig.mc.port'), default: 5007, type: 'number', min: 1, max: 65535 },
      { key: 'network_no', label: _tc('protocolConfig.mc.networkNo'), default: 0, type: 'number', min: 0, max: 255 },
      { key: 'station_no', label: _tc('protocolConfig.mc.stationNo'), default: 0, type: 'number' },
      { key: 'timeout', label: _tc('protocolConfig.mc.timeout'), default: 5, type: 'number', min: 1, max: 60 },
      { key: 'plc_type', label: _tc('protocolConfig.mc.plcType'), default: 'iQ-R', type: 'select', options: [
        { label: 'iQ-R', value: 'iQ-R' }, { label: 'Q', value: 'Q' }, { label: 'L', value: 'L' }, { label: 'FX', value: 'FX' },
      ]},
      { key: 'batch_size', label: _tc('protocolConfig.mc.batchSize'), default: 10, type: 'number', min: 1, max: 100 },
      { key: 'frame_type', label: _tc('protocolConfig.mc.frameType'), default: '3E', type: 'select', options: [
        { label: '3E', value: '3E' }, { label: '4E', value: '4E' }, { label: _tc('protocolConfig.mc.frameTypeAuto'), value: 'auto' },
      ]},
      { key: 'pc_no', label: _tc('protocolConfig.mc.pcNo'), default: 255, type: 'number', min: 0, max: 255 },
      { key: 'device_type', label: _tc('protocolConfig.mc.deviceType'), default: 'D', type: 'select', options: [
        { label: 'X (Input)', value: 'X' }, { label: 'Y (Output)', value: 'Y' }, { label: 'M (Relay)', value: 'M' },
        { label: 'D (Data)', value: 'D' }, { label: 'W (Link)', value: 'W' }, { label: 'R (File)', value: 'R' },
      ]},
    ],
    pointTemplates: [
      { name: 'D0', data_type: 'int16', unit: '', address: 'D0', access_mode: 'read', description: _tc('protocolConfig.mc.pointD0') },
    ],
    capabilities: { discover: false, read: true, write: true, batch_read: true },
    experimental: false,
  },
  'fins': {
    label: _tc('protocolConfig.fins.label'),
    description: _tc('protocolConfig.fins.description'),
    icon: 'flash',
    configFields: [
      { key: 'host', label: _tc('protocolConfig.fins.host'), required: true, type: 'string', pattern: HOST_PATTERN },
      { key: 'port', label: _tc('protocolConfig.fins.port'), default: 9600, type: 'number', min: 1, max: 65535 },
      { key: 'source_node', label: _tc('protocolConfig.fins.sourceNode'), default: 0, type: 'number', min: 0, max: 255 },
      { key: 'dest_node', label: _tc('protocolConfig.fins.destNode'), default: 1, type: 'number', min: 0, max: 255 },
      { key: 'transport', label: _tc('protocolConfig.fins.transport'), default: 'TCP', type: 'select', options: [
        { label: 'TCP', value: 'TCP' }, { label: 'UDP', value: 'UDP' },
      ]},
      { key: 'batch_size', label: _tc('protocolConfig.fins.batchSize'), default: 10, type: 'number', min: 1, max: 100 },
      { key: 'timeout', label: _tc('protocolConfig.fins.timeout'), default: 5, type: 'number', min: 1, max: 60 },
      { key: 'network_no', label: _tc('protocolConfig.fins.networkNo'), default: 0, type: 'number', min: 0, max: 255 },
      { key: 'unit_no', label: _tc('protocolConfig.fins.unitNo'), default: 0, type: 'number', min: 0, max: 255 },
      { key: 'command_code', label: _tc('protocolConfig.fins.commandCode'), default: '0101', type: 'select', options: [
        { label: '0101 - Memory Area Read', value: '0101' }, { label: '0102 - Memory Area Write', value: '0102' },
        { label: '0103 - Memory Area Fill', value: '0103' }, { label: '0104 - Read Multiple', value: '0104' },
      ]},
    ],
    pointTemplates: [],
    capabilities: { discover: false, read: true, write: true, batch_read: true },
    experimental: false,
  },
  'ab': {
    label: _tc('protocolConfig.ab.label'),
    description: _tc('protocolConfig.ab.description'),
    icon: 'flash',
    configFields: [
      { key: 'ip', label: _tc('protocolConfig.ab.host'), required: true, type: 'string', pattern: HOST_PATTERN },
      { key: 'port', label: _tc('protocolConfig.ab.port'), default: 44818, type: 'number', min: 1, max: 65535 },
      { key: 'slot', label: _tc('protocolConfig.ab.slot'), default: 0, type: 'number', min: 0, max: 31 },
      { key: 'timeout', label: _tc('protocolConfig.ab.timeout'), default: 5, type: 'number', min: 1, max: 60 },
      { key: 'connection_type', label: _tc('protocolConfig.ab.connectionType'), default: 'CIP', type: 'select', options: [
        { label: 'CIP', value: 'CIP' }, { label: 'PCCC', value: 'PCCC' },
      ]},
      { key: 'plc_model', label: _tc('protocolConfig.ab.plcModel'), default: 'ControlLogix', type: 'select', options: [
        { label: 'MicroLogix', value: 'MicroLogix' }, { label: 'ControlLogix', value: 'ControlLogix' }, { label: 'CompactLogix', value: 'CompactLogix' }, { label: 'Micro800', value: 'Micro800' },
      ]},
      { key: 'large_forward_open', label: _tc('protocolConfig.ab.largeForwardOpen'), default: false, type: 'boolean' },
      { key: 'default_tag', label: _tc('protocolConfig.ab.defaultTag'), default: '@cpu', type: 'string' },
      { key: 'backup_ip', label: _tc('protocolConfig.ab.backupIp'), type: 'string' },
      { key: 'failover_threshold', label: _tc('protocolConfig.ab.failoverThreshold'), default: 3, type: 'number', min: 1, max: 10 },
      { key: 'deadband', label: _tc('protocolConfig.ab.deadband'), type: 'number', min: 0 },
      { key: 'scaling_ratio', label: _tc('protocolConfig.ab.scalingRatio'), default: 1.0, type: 'number' },
      { key: 'scaling_offset', label: _tc('protocolConfig.ab.scalingOffset'), default: 0.0, type: 'number' },
      { key: 'clamp_min', label: _tc('protocolConfig.ab.clampMin'), type: 'number' },
      { key: 'clamp_max', label: _tc('protocolConfig.ab.clampMax'), type: 'number' },
    ],
    pointTemplates: [],
    capabilities: { discover: true, read: true, write: true, batch_read: true, batch_write: true },
    experimental: false,
  },
  'http': {
    label: _tc('protocolConfig.http.label'),
    description: _tc('protocolConfig.http.description'),
    icon: 'globe',
    configFields: [
      { key: 'url', label: _tc('protocolConfig.http.url'), placeholder: _tc('protocolConfig.http.urlPlaceholder'), required: true, type: 'string', pattern: URL_PATTERN },
      { key: 'method', label: _tc('protocolConfig.http.method'), default: 'POST', type: 'select', options: [
        { label: 'GET', value: 'GET' }, { label: 'POST', value: 'POST' }, { label: 'PUT', value: 'PUT' }, { label: 'DELETE', value: 'DELETE' }, { label: 'PATCH', value: 'PATCH' },
      ]},
      { key: 'interval', label: _tc('protocolConfig.http.interval'), default: 10, type: 'number' },
      { key: 'headers', label: _tc('protocolConfig.http.headers'), type: 'string' },
      { key: 'body_template', label: _tc('protocolConfig.http.bodyTemplate'), type: 'string' },
      { key: 'push_url', label: _tc('protocolConfig.http.pushUrl'), type: 'string' },
      { key: 'auth_type', label: _tc('protocolConfig.http.authType'), default: 'None', type: 'select', options: [
        { label: 'None', value: 'None' }, { label: 'Basic', value: 'Basic' }, { label: 'Bearer', value: 'Bearer' }, { label: 'OAuth2', value: 'OAuth2' },
      ]},
      { key: 'body_type', label: _tc('protocolConfig.http.bodyType'), default: 'json', type: 'select', options: [
        { label: 'JSON', value: 'json' }, { label: 'XML', value: 'xml' }, { label: 'Form', value: 'form' }, { label: 'Raw', value: 'raw' },
      ]},
      { key: 'connect_timeout', label: _tc('protocolConfig.http.connectTimeout'), default: 5, type: 'number', min: 1, max: 30 },
      { key: 'read_timeout', label: _tc('protocolConfig.http.readTimeout'), default: 30, type: 'number', min: 1, max: 120 },
      { key: 'write_timeout', label: _tc('protocolConfig.http.writeTimeout'), default: 10, type: 'number', min: 1, max: 60 },
      { key: 'max_retries', label: _tc('protocolConfig.http.maxRetries'), default: 3, type: 'number', min: 0, max: 10 },
      { key: 'retry_backoff', label: _tc('protocolConfig.http.retryBackoff'), default: 1, type: 'number', min: 0.1, max: 30 },
      { key: 'timeout', label: _tc('protocolConfig.http.timeout'), default: 30, type: 'number' },
      { key: 'auth_token', label: _tc('protocolConfig.http.authToken'), type: 'password' },
    ],
    pointTemplates: [],
    capabilities: { discover: false, read: true, write: true },
    experimental: false,
  },
  'sparkplug-b': {
    label: _tc('protocolConfig.sparkplugB.label'),
    description: _tc('protocolConfig.sparkplugB.description'),
    icon: 'radio',
    configFields: [
      { key: 'broker', label: _tc('protocolConfig.sparkplugB.broker'), placeholder: _tc('protocolConfig.sparkplugB.brokerPlaceholder'), required: true, type: 'string', pattern: HOST_PATTERN },
      { key: 'group_id', label: _tc('protocolConfig.sparkplugB.groupId'), placeholder: _tc('protocolConfig.sparkplugB.groupIdPlaceholder'), required: true, type: 'string' },
      { key: 'edge_node_id', label: _tc('protocolConfig.sparkplugB.edgeNodeId'), placeholder: _tc('protocolConfig.sparkplugB.edgeNodeIdPlaceholder'), required: true, type: 'string' },
      { key: 'username', label: _tc('protocolConfig.sparkplugB.username'), type: 'string' },
      { key: 'password', label: _tc('protocolConfig.sparkplugB.password'), type: 'password' },
      { key: 'port', label: _tc('protocolConfig.sparkplugB.port'), default: 1883, type: 'number', min: 1, max: 65535 },
      { key: 'keepalive', label: _tc('protocolConfig.sparkplugB.keepalive'), default: 60, type: 'number', min: 1, max: 600 },
      { key: 'enable_cmd_response', label: _tc('protocolConfig.sparkplugB.enableCmdResponse'), default: true, type: 'boolean' },
      { key: 'batch_interval_ms', label: _tc('protocolConfig.sparkplugB.batchIntervalMs'), default: 100, type: 'number', min: 10, max: 60000 },
      { key: 'mqtt_username', label: _tc('protocolConfig.sparkplugB.mqttUsername'), type: 'string' },
      { key: 'mqtt_password', label: _tc('protocolConfig.sparkplugB.mqttPassword'), type: 'string' },
    ],
    pointTemplates: [
      { name: 'metrics', data_type: 'float32', unit: '', address: 'metrics/0', access_mode: 'read', description: _tc('protocolConfig.sparkplugB.pointMetrics') },
    ],
    capabilities: { discover: false, read: true, write: true, subscribe: true },
    experimental: false,
  },
  'simulator': {
    label: _tc('protocolConfig.simulator.label'),
    description: _tc('protocolConfig.simulator.description'),
    icon: 'flask',
    configFields: [
      { key: 'update_interval', label: _tc('protocolConfig.simulator.updateInterval'), default: 1, type: 'number', min: 0.1, max: 3600 },
      { key: 'value_range_min', label: _tc('protocolConfig.simulator.valueRangeMin'), default: 0, type: 'number' },
      { key: 'value_range_max', label: _tc('protocolConfig.simulator.valueRangeMax'), default: 100, type: 'number' },
      { key: 'noise_amplitude', label: _tc('protocolConfig.simulator.noiseAmplitude'), default: 0, type: 'number', min: 0 },
      { key: 'trend_drift', label: _tc('protocolConfig.simulator.trendDrift'), default: 0, type: 'number' },
      { key: 'sim_mode', label: _tc('protocolConfig.simulator.simMode'), default: 'random', type: 'select', options: [
        { label: _tc('protocolConfig.simulator.modeRandom'), value: 'random' }, { label: _tc('protocolConfig.simulator.modeSine'), value: 'sine' },
        { label: _tc('protocolConfig.simulator.modeSquare'), value: 'square' }, { label: _tc('protocolConfig.simulator.modeTriangle'), value: 'triangle' },
        { label: _tc('protocolConfig.simulator.modeSawtooth'), value: 'sawtooth' },
        { label: _tc('protocolConfig.simulator.modeWalk'), value: 'random_walk' }, { label: _tc('protocolConfig.simulator.modeRamp'), value: 'ramp' },
        { label: _tc('protocolConfig.simulator.modeStep'), value: 'step' }, { label: _tc('protocolConfig.simulator.modeFormula'), value: 'formula' },
        { label: _tc('protocolConfig.simulator.modeFixed'), value: 'fixed' },
      ]},
      { key: 'period', label: _tc('protocolConfig.simulator.period'), default: 60, type: 'number', min: 1, max: 3600 },
      { key: 'formula', label: _tc('protocolConfig.simulator.formula'), type: 'string' },
      { key: 'fault_simulation', label: _tc('protocolConfig.simulator.faultSimulation'), default: 'none', type: 'select', options: [
        { label: _tc('protocolConfig.simulator.faultNone'), value: 'none' }, { label: _tc('protocolConfig.simulator.faultTimeout'), value: 'timeout' },
        { label: _tc('protocolConfig.simulator.faultDisconnect'), value: 'disconnect' }, { label: _tc('protocolConfig.simulator.faultDataError'), value: 'data_error' },
        { label: _tc('protocolConfig.simulator.faultRandom'), value: 'random' },
      ]},
      { key: 'fault_rate', label: _tc('protocolConfig.simulator.faultRate'), default: 0, type: 'number', min: 0, max: 100 },
      { key: 'write_hold_seconds', label: _tc('protocolConfig.simulator.writeHold'), default: 10, type: 'number', min: 0, max: 3600 },
      { key: 'timeout', label: _tc('protocolConfig.simulator.timeout'), default: 5, type: 'number', min: 0, max: 60 },
    ],
    pointTemplates: [
      { name: 'temperature', data_type: 'float32', unit: '°C', address: 'sim_temp', access_mode: 'read', description: _tc('protocolConfig.simulator.pointSimTemp') },
      { name: 'humidity', data_type: 'float32', unit: '%', address: 'sim_hum', access_mode: 'read', description: _tc('protocolConfig.simulator.pointSimHum') },
      { name: 'pressure', data_type: 'float32', unit: 'kPa', address: 'sim_press', access_mode: 'read', description: _tc('protocolConfig.simulator.pointSimPress') },
      { name: 'status', data_type: 'int16', unit: '', address: 'sim_status', access_mode: 'read', description: _tc('protocolConfig.simulator.pointSimStatus') },
    ],
    capabilities: { discover: false, read: true, write: true, subscribe: true },
    experimental: false,
  },
  'bacnet': {
    label: _tc('protocolConfig.bacnet.label'),
    description: _tc('protocolConfig.bacnet.description'),
    icon: 'business',
    configFields: [
      { key: 'host', label: _tc('protocolConfig.bacnet.host'), placeholder: _tc('protocolConfig.bacnet.hostPlaceholder'), default: '192.168.1.255', required: true, type: 'string', pattern: HOST_PATTERN },
      { key: 'port', label: _tc('protocolConfig.bacnet.port'), placeholder: _tc('protocolConfig.bacnet.portPlaceholder'), default: 47808, required: true, type: 'number' },
      { key: 'device_instance', label: _tc('protocolConfig.bacnet.deviceInstance'), placeholder: _tc('protocolConfig.bacnet.deviceInstancePlaceholder'), default: 100, type: 'number' },
      { key: 'timeout', label: _tc('protocolConfig.bacnet.timeout'), placeholder: _tc('protocolConfig.bacnet.timeoutPlaceholder'), default: 5, type: 'number' },
      { key: 'enable_cov', label: _tc('protocolConfig.bacnet.enableCov'), default: true, type: 'boolean' },
      { key: 'cov_lifetime', label: _tc('protocolConfig.bacnet.covLifetime'), placeholder: _tc('protocolConfig.bacnet.covLifetimePlaceholder'), default: 300, type: 'number' },
    ],
    pointTemplates: [
      { name: 'temperature', data_type: 'float32', unit: '°C', address: 'AV:1.presentValue', access_mode: 'read', description: _tc('protocolConfig.bacnet.pointTemp') },
      { name: 'switch', data_type: 'bool', unit: '', address: 'BV:1.presentValue', access_mode: 'readwrite', description: _tc('protocolConfig.bacnet.pointSwitch') },
    ],
    capabilities: { discover: true, read: true, write: true, subscribe: true },
    experimental: false,
  },
  'knx': {
    label: _tc('protocolConfig.knx.label'),
    description: _tc('protocolConfig.knx.description'),
    icon: 'home',
    configFields: [
      { key: 'gateway_host', label: _tc('protocolConfig.knx.gatewayHost'), placeholder: _tc('protocolConfig.knx.gatewayHostPlaceholder'), default: '192.168.1.100', required: true, type: 'string' },
      { key: 'gateway_port', label: _tc('protocolConfig.knx.gatewayPort'), placeholder: _tc('protocolConfig.knx.gatewayPortPlaceholder'), default: 3671, required: true, type: 'number' },
      { key: 'local_port', label: _tc('protocolConfig.knx.localPort'), placeholder: _tc('protocolConfig.knx.localPortPlaceholder'), default: 0, type: 'number' },
      { key: 'enable_events', label: _tc('protocolConfig.knx.enableEvents'), default: true, type: 'boolean' },
      { key: 'timeout', label: _tc('protocolConfig.knx.timeout'), default: 10, type: 'number' },
      { key: 'group_address_format', label: _tc('protocolConfig.knx.groupAddressFormat'), type: 'string', default: 'x/x/x or x.x.x', tooltip: _tc('protocolConfig.knx.groupAddressFormatTip') },
    ],
    pointTemplates: [
      { name: 'switch', data_type: 'bool', unit: '', address: '1/2/3', access_mode: 'readwrite', description: _tc('protocolConfig.knx.pointSwitch') },
      { name: 'percent', data_type: 'uint8', unit: '%', address: '1/2/4', access_mode: 'readwrite', description: _tc('protocolConfig.knx.pointPercent') },
      { name: 'temperature', data_type: 'float32', unit: '°C', address: '1/2/5', access_mode: 'read', description: _tc('protocolConfig.knx.pointTemperature') },
    ],
    capabilities: { discover: true, read: true, write: true, subscribe: true, batch_read: true },
    experimental: true,
    constraints: [{ type: 'interop_risk', message: _tc('protocolConfig.knx.interopRisk') }],
  },
  'dnp3': {
    label: _tc('protocolConfig.dnp3.label'),
    description: _tc('protocolConfig.dnp3.description'),
    icon: 'flash',
    configFields: [
      { key: 'host', label: _tc('protocolConfig.dnp3.host'), placeholder: _tc('protocolConfig.dnp3.hostPlaceholder'), default: '192.168.1.1', required: true, type: 'string', pattern: HOST_PATTERN },
      { key: 'port', label: _tc('protocolConfig.dnp3.port'), placeholder: _tc('protocolConfig.dnp3.portPlaceholder'), default: 20000, required: true, type: 'number' },
      { key: 'device_address', label: _tc('protocolConfig.dnp3.deviceAddress'), placeholder: _tc('protocolConfig.dnp3.deviceAddressPlaceholder'), default: 1, type: 'number' },
      { key: 'timeout', label: _tc('protocolConfig.dnp3.timeout'), placeholder: _tc('protocolConfig.dnp3.timeoutPlaceholder'), default: 10, type: 'number' },
      { key: 'source_address', label: _tc('protocolConfig.dnp3.sourceAddress'), default: 1, type: 'number' },
      { key: 'interrogation_interval', label: _tc('protocolConfig.dnp3.interrogationInterval'), default: 10, type: 'number' },
      { key: 'mode', label: _tc('protocolConfig.dnp3.mode'), default: 'master', type: 'select', options: [
        { label: _tc('protocolConfig.dnp3.modeMaster'), value: 'master' }, { label: _tc('protocolConfig.dnp3.modeOutstation'), value: 'outstation' },
      ]},
      { key: 'enable_aglv5', label: _tc('protocolConfig.dnp3.enableAgLv5'), default: false, type: 'boolean' },
      { key: 'aglv5_key', label: _tc('protocolConfig.dnp3.aglv5Key'), type: 'string', pattern: '^[0-9a-fA-F]{0,64}$' },
    ],
    pointTemplates: [
      { name: 'binary_input', data_type: 'bool', unit: '', address: 'binary:0', access_mode: 'read', description: _tc('protocolConfig.dnp3.pointBinary') },
      { name: 'analog_input', data_type: 'float32', unit: '', address: 'analog:0', access_mode: 'read', description: _tc('protocolConfig.dnp3.pointAnalog') },
      { name: 'counter_input', data_type: 'uint32', unit: '', address: 'counter:0', access_mode: 'read', description: _tc('protocolConfig.dnp3.pointCounter') },
    ],
    capabilities: { discover: false, read: true, write: true, subscribe: true },
    experimental: true,
  },
  'ethercat': {
    label: _tc('protocolConfig.ethercat.label'),
    description: _tc('protocolConfig.ethercat.description'),
    icon: 'rocket',
    configFields: [
      { key: 'iface', label: _tc('protocolConfig.ethercat.iface'), placeholder: _tc('protocolConfig.ethercat.ifacePlaceholder'), default: 'eth0', required: true, type: 'string' },
      { key: 'timeout', label: _tc('protocolConfig.ethercat.timeout'), placeholder: _tc('protocolConfig.ethercat.timeoutPlaceholder'), default: 2000, type: 'number' },
      { key: 'enable_dc', label: _tc('protocolConfig.ethercat.enableDc'), default: false, type: 'boolean' },
      { key: 'cycle_time_ms', label: _tc('protocolConfig.ethercat.cycleTimeMs'), default: 1, type: 'number' },
    ],
    pointTemplates: [
      { name: 'input_0', data_type: 'uint16', unit: '', address: 'input_0', access_mode: 'read', description: _tc('protocolConfig.ethercat.pointInput') },
    ],
    capabilities: { discover: true, read: true, write: true, subscribe: true },
    experimental: true,
    constraints: [{ type: 'platform', message: _tc('protocolConfig.ethercat.linuxOnly') }],
  },
  'profinet': {
    label: _tc('protocolConfig.profinet.label'),
    description: _tc('protocolConfig.profinet.description'),
    icon: 'construct',
    configFields: [
      { key: 'interface_ip', label: _tc('protocolConfig.profinet.interfaceIp'), placeholder: _tc('protocolConfig.profinet.interfaceIpPlaceholder'), default: '0.0.0.0', required: true, type: 'string' },
      { key: 'port', label: _tc('protocolConfig.profinet.port'), placeholder: _tc('protocolConfig.profinet.portPlaceholder'), default: 34964, type: 'number' },
      { key: 'enable_snap7', label: _tc('protocolConfig.profinet.enableSnap7'), default: false, type: 'boolean' },
      { key: 'snap7_plc_ip', label: _tc('protocolConfig.profinet.snap7PlcIp'), placeholder: _tc('protocolConfig.profinet.snap7PlcIpPlaceholder'), type: 'string' },
      { key: 'snap7_rack', label: _tc('protocolConfig.profinet.snap7Rack'), default: 0, type: 'number' },
      { key: 'snap7_slot', label: _tc('protocolConfig.profinet.snap7Slot'), default: 1, type: 'number' },
      { key: 'timeout', label: _tc('protocolConfig.profinet.timeout'), default: 5, type: 'number' },
    ],
    pointTemplates: [
      { name: 'device_status', data_type: 'int16', unit: '', address: 'status', access_mode: 'read', description: _tc('protocolConfig.profinet.pointStatus') },
    ],
    capabilities: { discover: true, read: true, write: true },
    experimental: true,
    constraints: [{ type: 'scope', message: _tc('protocolConfig.profinet.dcpOnly') }],
  },
  'dl645': {
    label: _tc('protocolConfig.dl645.label'),
    description: _tc('protocolConfig.dl645.description'),
    icon: 'flash',
    configFields: [
      { key: 'port', label: _tc('protocolConfig.dl645.port'), placeholder: _tc('protocolConfig.dl645.portPlaceholder'), default: 'COM1', required: true, type: 'string' },
      { key: 'baud_rate', label: _tc('protocolConfig.dl645.baudRate'), default: 2400, type: 'select', options: [
        { label: '2400', value: 2400 }, { label: '4800', value: 4800 }, { label: '9600', value: 9600 }, { label: '19200', value: 19200 },
      ]},
      { key: 'parity', label: _tc('protocolConfig.dl645.parity'), default: 'E', type: 'select', options: [
        { label: _tc('protocolConfig.dl645.parityEven'), value: 'E' }, { label: _tc('protocolConfig.dl645.parityNone'), value: 'N' }, { label: _tc('protocolConfig.dl645.parityOdd'), value: 'O' },
      ]},
      { key: 'timeout', label: _tc('protocolConfig.dl645.timeout'), placeholder: _tc('protocolConfig.dl645.timeoutPlaceholder'), default: 5, type: 'number' },
      { key: 'stopbits', label: _tc('protocolConfig.dl645.stopbits'), default: 1, type: 'number' },
      { key: 'bytesize', label: _tc('protocolConfig.dl645.bytesize'), default: 8, type: 'number' },
      { key: 'meter_address', label: _tc('protocolConfig.dl645.meterAddress'), placeholder: _tc('protocolConfig.dl645.meterAddressPlaceholder'), type: 'string', required: true, pattern: '^[0-9a-fA-F]{12}$' },
    ],
    pointTemplates: [
      { name: 'voltage_a', data_type: 'float32', unit: 'V', address: 'voltage_a', access_mode: 'read', description: _tc('protocolConfig.dl645.pointVoltage') },
      { name: 'current_a', data_type: 'float32', unit: 'A', address: 'current_a', access_mode: 'read', description: _tc('protocolConfig.dl645.pointCurrent') },
      { name: 'active_power', data_type: 'float32', unit: 'W', address: 'active_power', access_mode: 'read', description: _tc('protocolConfig.dl645.pointPower') },
    ],
    capabilities: { discover: false, read: true, write: false },
    experimental: false,
  },
  'iec104': {
    label: _tc('protocolConfig.iec104.label'),
    description: _tc('protocolConfig.iec104.description'),
    icon: 'radio',
    configFields: [
      { key: 'host', label: _tc('protocolConfig.iec104.host'), placeholder: _tc('protocolConfig.iec104.hostPlaceholder'), default: '127.0.0.1', required: true, type: 'string', pattern: HOST_PATTERN },
      { key: 'port', label: _tc('protocolConfig.iec104.port'), placeholder: _tc('protocolConfig.iec104.portPlaceholder'), default: 2404, required: true, type: 'number' },
      { key: 'asdu_addr', label: _tc('protocolConfig.iec104.asduAddr'), placeholder: _tc('protocolConfig.iec104.asduAddrPlaceholder'), default: 1, type: 'number' },
      { key: 'heartbeat_interval', label: _tc('protocolConfig.iec104.heartbeatInterval'), placeholder: _tc('protocolConfig.iec104.heartbeatIntervalPlaceholder'), default: 30, type: 'number' },
      { key: 'timeout', label: _tc('protocolConfig.iec104.timeout'), default: 15, type: 'number' },
      { key: 'standby_host', label: _tc('protocolConfig.iec104.standbyHost'), type: 'string' },
      { key: 'standby_port', label: _tc('protocolConfig.iec104.standbyPort'), default: 2404, type: 'number', min: 1, max: 65535 },
      { key: 'clock_sync', label: _tc('protocolConfig.iec104.clockSync'), default: true, type: 'boolean' },
      { key: 'enable_file_transfer', label: _tc('protocolConfig.iec104.enableFileTransfer'), default: false, type: 'boolean' },
      { key: 't1_timeout', label: _tc('protocolConfig.iec104.t1Timeout'), default: 15, type: 'number' },
      { key: 't2_timeout', label: _tc('protocolConfig.iec104.t2Timeout'), default: 10, type: 'number' },
      { key: 't3_timeout', label: _tc('protocolConfig.iec104.t3Timeout'), default: 20, type: 'number' },
    ],
    pointTemplates: [
      { name: 'digital_input', data_type: 'bool', unit: '', address: '1', access_mode: 'read', description: _tc('protocolConfig.iec104.pointDigital') },
      { name: 'analog_input', data_type: 'float32', unit: '', address: '1', access_mode: 'read', description: _tc('protocolConfig.iec104.pointAnalog') },
    ],
    capabilities: { discover: false, read: true, write: true, subscribe: true },
    experimental: false,
    constraints: [{ type: 'protocol_note', message: _tc('protocolConfig.iec104.eventDriven') }],
  },
  'fanuc': {
    label: _tc('protocolConfig.fanuc.label'),
    description: _tc('protocolConfig.fanuc.description'),
    icon: 'construct',
    configFields: [
      { key: 'host', label: _tc('protocolConfig.fanuc.host'), placeholder: _tc('protocolConfig.fanuc.hostPlaceholder'), default: '192.168.1.1', required: true, type: 'string', pattern: HOST_PATTERN },
      { key: 'port', label: _tc('protocolConfig.fanuc.port'), placeholder: _tc('protocolConfig.fanuc.portPlaceholder'), default: 8193, type: 'number', min: 1, max: 65535 },
      { key: 'timeout', label: _tc('protocolConfig.fanuc.timeout'), placeholder: _tc('protocolConfig.fanuc.timeoutPlaceholder'), default: 5, type: 'number', min: 1, max: 60 },
      { key: 'max_axes', label: _tc('protocolConfig.fanuc.maxAxes'), default: 8, type: 'number', min: 1, max: 32 },
      { key: 'data_items', label: _tc('protocolConfig.fanuc.dataItems'), type: 'string' },
      { key: 'fwlib_path', label: _tc('protocolConfig.fanuc.fwlibPath'), type: 'string' },
    ],
    pointTemplates: [
      { name: 'cnc_status', data_type: 'int16', unit: '', address: 'cnc_status', access_mode: 'read', description: _tc('protocolConfig.fanuc.pointStatus') },
      { name: 'cnc_position', data_type: 'float32', unit: 'mm', address: 'cnc_position', access_mode: 'read', description: _tc('protocolConfig.fanuc.pointPosition') },
    ],
    capabilities: { discover: false, read: true, write: false },
    experimental: false,
  },
  'mtconnect': {
    label: _tc('protocolConfig.mtconnect.label'),
    description: _tc('protocolConfig.mtconnect.description'),
    icon: 'business',
    configFields: [
      { key: 'url', label: _tc('protocolConfig.mtconnect.url'), placeholder: _tc('protocolConfig.mtconnect.urlPlaceholder'), default: 'http://127.0.0.1:5000', required: true, type: 'string', pattern: URL_PATTERN },
      { key: 'device', label: _tc('protocolConfig.mtconnect.device'), placeholder: _tc('protocolConfig.mtconnect.devicePlaceholder'), type: 'string' },
      { key: 'poll_interval', label: _tc('protocolConfig.mtconnect.pollInterval'), default: 1, type: 'number' },
      { key: 'heartbeat_interval', label: _tc('protocolConfig.mtconnect.heartbeatInterval'), default: 10, type: 'number' },
      { key: 'timeout', label: _tc('protocolConfig.mtconnect.timeout'), default: 10, type: 'number' },
    ],
    pointTemplates: [
      { name: 'X_position', data_type: 'float32', unit: 'mm', address: 'Xact', access_mode: 'read', description: _tc('protocolConfig.mtconnect.pointXpos') },
      { name: 'spindle_speed', data_type: 'float32', unit: 'rpm', address: 'Sspeed', access_mode: 'read', description: _tc('protocolConfig.mtconnect.pointSpindle') },
    ],
    capabilities: { discover: false, read: true, write: false },
    experimental: false,
  },
  'kuka': {
    label: _tc('protocolConfig.kuka.label'),
    description: _tc('protocolConfig.kuka.description'),
    icon: 'flash',
    configFields: [
      { key: 'ip', label: _tc('protocolConfig.kuka.host'), placeholder: _tc('protocolConfig.kuka.hostPlaceholder'), default: '192.168.1.100', required: true, type: 'string', pattern: HOST_PATTERN },
      { key: 'port', label: _tc('protocolConfig.kuka.port'), placeholder: _tc('protocolConfig.kuka.portPlaceholder'), default: 54600, type: 'number', min: 1, max: 65535 },
      { key: 'timeout', label: _tc('protocolConfig.kuka.timeout'), default: 5, type: 'number', min: 1, max: 60 },
      { key: 'robot_name', label: _tc('protocolConfig.kuka.robotName'), type: 'string' },
      { key: 'variables', label: _tc('protocolConfig.kuka.variables'), type: 'string' },
      { key: 'program_control', label: _tc('protocolConfig.kuka.programControl'), default: false, type: 'boolean' },
    ],
    pointTemplates: [
      { name: 'position', data_type: 'float32', unit: 'mm', address: '$POS_ACT', access_mode: 'read', description: _tc('protocolConfig.kuka.pointPosition') },
    ],
    capabilities: { discover: false, read: true, write: false },
    experimental: true,
  },
  'abb': {
    label: _tc('protocolConfig.abb.label'),
    description: _tc('protocolConfig.abb.description'),
    icon: 'flash',
    configFields: [
      { key: 'ip', label: _tc('protocolConfig.abb.host'), placeholder: _tc('protocolConfig.abb.hostPlaceholder'), default: '192.168.1.100', required: true, type: 'string', pattern: HOST_PATTERN },
      { key: 'port', label: _tc('protocolConfig.abb.port'), placeholder: _tc('protocolConfig.abb.portPlaceholder'), default: 80, type: 'number', min: 1, max: 65535 },
      { key: 'username', label: _tc('protocolConfig.abb.username'), type: 'string' },
      { key: 'password', label: _tc('protocolConfig.abb.password'), type: 'string' },
      { key: 'timeout', label: _tc('protocolConfig.abb.timeout'), default: 10, type: 'number', min: 1, max: 60 },
      { key: 'auth_type', label: _tc('protocolConfig.abb.authType'), default: 'Basic', type: 'select', options: [
        { label: _tc('protocolConfig.abb.authNone'), value: 'None' }, { label: 'Basic', value: 'Basic' }, { label: 'Digest', value: 'Digest' },
      ]},
      { key: 'use_websocket', label: _tc('protocolConfig.abb.useWebsocket'), default: false, type: 'boolean' },
    ],
    pointTemplates: [
      { name: 'joints', data_type: 'float32', unit: 'deg', address: 'joints', access_mode: 'read', description: _tc('protocolConfig.abb.pointJoints') },
      { name: 'tcp_position', data_type: 'float32', unit: 'mm', address: 'tcp', access_mode: 'read', description: _tc('protocolConfig.abb.pointTcp') },
    ],
    capabilities: { discover: false, read: true, write: false },
    experimental: true,
  },
  'opcda': {
    label: _tc('protocolConfig.opcda.label'),
    description: _tc('protocolConfig.opcda.description'),
    icon: 'cube',
    configFields: [
      { key: 'prog_id', label: _tc('protocolConfig.opcda.progId'), placeholder: _tc('protocolConfig.opcda.progIdPlaceholder'), default: 'Matrikon.OPC.Simulation', required: true, type: 'string' },
      { key: 'host', label: _tc('protocolConfig.opcda.host'), placeholder: _tc('protocolConfig.opcda.hostPlaceholder'), default: 'localhost', type: 'string' },
      { key: 'update_rate', label: _tc('protocolConfig.opcda.updateRate'), default: 1000, type: 'number', min: 100, max: 60000 },
      { key: 'gateway', label: _tc('protocolConfig.opcda.gateway'), type: 'string' },
      { key: 'use_groups', label: _tc('protocolConfig.opcda.useGroups'), default: true, type: 'boolean' },
      { key: 'timeout', label: _tc('protocolConfig.opcda.timeout'), default: 5, type: 'number' },
    ],
    pointTemplates: [
      { name: 'random_value', data_type: 'float32', unit: '', address: 'Simulation.Items.Random', access_mode: 'read', description: _tc('protocolConfig.opcda.pointRandom') },
    ],
    capabilities: { discover: true, read: true, write: true, subscribe: true },
    experimental: false,
    constraints: [{ type: 'platform', message: _tc('protocolConfig.opcda.windowsOnly') }],
  },
  'opcuaserver': {
    label: _tc('protocolConfig.opcuaserver.label'),
    description: _tc('protocolConfig.opcuaserver.description'),
    icon: 'globe',
    configFields: [
      { key: 'host', label: _tc('protocolConfig.opcuaserver.host'), placeholder: _tc('protocolConfig.opcuaserver.hostPlaceholder'), default: '0.0.0.0', required: true, type: 'string' },
      { key: 'port', label: _tc('protocolConfig.opcuaserver.port'), placeholder: _tc('protocolConfig.opcuaserver.portPlaceholder'), default: 4840, type: 'number' },
      { key: 'server_name', label: _tc('protocolConfig.opcuaserver.serverName'), default: 'EdgeLite Gateway', type: 'string' },
      { key: 'namespace', label: _tc('protocolConfig.opcuaserver.namespace'), default: 'http://edgelite.io/nodes', type: 'string' },
      { key: 'allow_anonymous', label: _tc('protocolConfig.opcuaserver.allowAnonymous'), default: true, type: 'boolean' },
    ],
    pointTemplates: [
      { name: 'value', data_type: 'float32', unit: '', address: 'Data.Value', access_mode: 'readwrite', description: _tc('protocolConfig.opcuaserver.pointValue') },
    ],
    capabilities: { discover: false, read: true, write: true, subscribe: true },
    experimental: false,
  },
  'onvif': {
    label: _tc('protocolConfig.onvif.label'),
    description: _tc('protocolConfig.onvif.description'),
    icon: 'videocam',
    configFields: [
      { key: 'ip', label: _tc('protocolConfig.onvif.host'), placeholder: _tc('protocolConfig.onvif.hostPlaceholder'), default: '192.168.1.100', required: true, type: 'string', pattern: HOST_PATTERN },
      { key: 'port', label: _tc('protocolConfig.onvif.port'), placeholder: _tc('protocolConfig.onvif.portPlaceholder'), default: 80, type: 'number', min: 1, max: 65535 },
      { key: 'username', label: _tc('protocolConfig.onvif.username'), default: '', required: true, type: 'string' },  // FIXED-P2: 默认用户名从admin改为空
      { key: 'password', label: _tc('protocolConfig.onvif.password'), required: true, type: 'string' },
      { key: 'auth_type', label: _tc('protocolConfig.onvif.authType'), default: 'Basic', type: 'string' },
      { key: 'timeout', label: _tc('protocolConfig.onvif.timeout'), default: 10, type: 'number', min: 1, max: 60 },
      { key: 'connect_timeout', label: _tc('protocolConfig.onvif.connectTimeout'), default: 5, type: 'number', min: 1, max: 30 },
      { key: 'read_timeout', label: _tc('protocolConfig.onvif.readTimeout'), default: 10, type: 'number', min: 1, max: 60 },
      { key: 'ptz_timeout', label: _tc('protocolConfig.onvif.ptzTimeout'), default: 5, type: 'number', min: 1, max: 30 },
      { key: 'wsdl_dir', label: _tc('protocolConfig.onvif.wsdlDir'), type: 'string' },
    ],
    pointTemplates: [
      { name: 'rtsp_url', data_type: 'string', unit: '', address: 'rtsp', access_mode: 'read', description: _tc('protocolConfig.onvif.pointRtsp') },
      { name: 'ptz_status', data_type: 'json', unit: '', address: 'ptz_status', access_mode: 'read', description: 'PTZ position status' },
      { name: 'snapshot', data_type: 'binary', unit: '', address: 'snapshot', access_mode: 'read', description: 'Snapshot image' },
    ],
    capabilities: { discover: true, read: true, write: true },
    experimental: true,
  },
  'video': {
    label: _tc('protocolConfig.video.label'),
    description: _tc('protocolConfig.video.description'),
    icon: 'videocam',
    configFields: [
      { key: 'pygbsentry_url', label: _tc('protocolConfig.video.pygbsentryUrl'), placeholder: _tc('protocolConfig.video.pygbsentryUrlPlaceholder'), default: 'http://127.0.0.1:8080', required: true, type: 'string' },
      { key: 'username', label: _tc('protocolConfig.video.username'), default: '', type: 'string' },  // FIXED-P2: 默认用户名从admin改为空
      { key: 'password', label: _tc('protocolConfig.video.password'), type: 'string' },
    ],
    pointTemplates: [
      { name: 'device_status', data_type: 'string', unit: '', address: 'status', access_mode: 'read', description: _tc('protocolConfig.video.pointStatus') },
    ],
    capabilities: { discover: false, read: true, write: false },
    experimental: false,
  },
  'toledo': {
    label: _tc('protocolConfig.toledo.label'),
    description: _tc('protocolConfig.toledo.description'),
    icon: 'scale',
    configFields: [
      { key: 'host', label: _tc('protocolConfig.toledo.host'), placeholder: _tc('protocolConfig.toledo.hostPlaceholder'), default: '192.168.1.100', required: true, type: 'string', pattern: HOST_PATTERN },
      { key: 'port', label: _tc('protocolConfig.toledo.port'), placeholder: _tc('protocolConfig.toledo.portPlaceholder'), default: 5000, required: true, type: 'number', min: 1, max: 65535 },
      { key: 'slave_id', label: _tc('protocolConfig.toledo.slaveId'), default: 1, required: true, type: 'number' },
      { key: 'timeout', label: _tc('protocolConfig.toledo.timeout'), default: 5, type: 'number', min: 1, max: 60 },
      { key: 'mode', label: _tc('protocolConfig.toledo.mode'), default: 'continuous', type: 'select', options: [
        { label: _tc('protocolConfig.toledo.modeContinuous'), value: 'continuous' }, { label: _tc('protocolConfig.toledo.modeSingle'), value: 'single' },
      ]},
      { key: 'serial_port', label: _tc('protocolConfig.toledo.serialPort'), type: 'string' },
      { key: 'baudrate', label: _tc('protocolConfig.toledo.baudrate'), default: 9600, type: 'select', options: [
        { label: '9600', value: 9600 }, { label: '19200', value: 19200 }, { label: '38400', value: 38400 }, { label: '57600', value: 57600 }, { label: '115200', value: 115200 },
      ]},
      { key: 'parity', label: _tc('protocolConfig.toledo.parity'), default: 'N', type: 'select', options: [
        { label: 'N', value: 'N' }, { label: 'E', value: 'E' }, { label: 'O', value: 'O' },
      ]},
      { key: 'stopbits', label: _tc('protocolConfig.toledo.stopbits'), default: 1, type: 'number', min: 1, max: 2 },
      { key: 'continuous_mode', label: _tc('protocolConfig.toledo.continuousMode'), default: false, type: 'boolean' },
    ],
    pointTemplates: [
      { name: 'weight', data_type: 'float32', unit: 'kg', address: '0', access_mode: 'read', description: _tc('protocolConfig.toledo.pointWeight') },
    ],
    capabilities: { discover: false, read: true, write: false },
    experimental: false,
  },
  'serial-device': {
    label: _tc('protocolConfig.serialDevice.label'),
    description: _tc('protocolConfig.serialDevice.description'),
    icon: 'flash',
    configFields: [
      { key: 'port', label: _tc('protocolConfig.serialDevice.port'), placeholder: _tc('protocolConfig.serialDevice.portPlaceholder'), default: 'COM1', required: true, type: 'string' },
      { key: 'baudrate', label: _tc('protocolConfig.serialDevice.baudrate'), default: 9600, required: true, type: 'select', options: [
        { label: '9600', value: 9600 }, { label: '19200', value: 19200 }, { label: '38400', value: 38400 }, { label: '115200', value: 115200 },
      ]},
      { key: 'parity', label: _tc('protocolConfig.serialDevice.parity'), default: 'N', type: 'select', options: [
        { label: _tc('protocolConfig.serialDevice.parityNone'), value: 'N' }, { label: _tc('protocolConfig.serialDevice.parityEven'), value: 'E' }, { label: _tc('protocolConfig.serialDevice.parityOdd'), value: 'O' },
      ]},
      { key: 'stopbits', label: _tc('protocolConfig.serialDevice.stopbits'), default: 1, type: 'number' },
      { key: 'bytesize', label: _tc('protocolConfig.serialDevice.bytesize'), default: 8, type: 'number' },
      { key: 'timeout', label: _tc('protocolConfig.serialDevice.timeout'), default: 5, type: 'number' },
      { key: 'flow_control', label: _tc('protocolConfig.serial.flowControl'), default: 'None', type: 'select', options: [
        { label: 'None', value: 'None' }, { label: 'XON/XOFF', value: 'XON/XOFF' }, { label: 'RTS/CTS', value: 'RTS/CTS' },
      ]},
      { key: 'data_encoding', label: _tc('protocolConfig.serial.dataEncoding'), default: 'ascii', type: 'select', options: [
        { label: 'ASCII', value: 'ascii' }, { label: 'Hex', value: 'hex' }, { label: 'UTF-8', value: 'utf-8' },
      ]},
      { key: 'slave_id', label: _tc('protocolConfig.serial.slaveId'), default: 1, type: 'number', min: 1, max: 247 },
    ],
    pointTemplates: [
      { name: 'value', data_type: 'float32', unit: '', address: '0', access_mode: 'read', description: _tc('protocolConfig.serialDevice.pointValue') },
    ],
    capabilities: { discover: false, read: true, write: true },
    experimental: false,
  },
  'database-source': {
    label: _tc('protocolConfig.databaseSource.label'),
    description: _tc('protocolConfig.databaseSource.description'),
    icon: 'database',
    configFields: [
      { key: 'db_type', label: _tc('protocolConfig.databaseSource.dbType'), default: 'mysql', required: true, type: 'select', options: [
        { label: 'MySQL', value: 'mysql' }, { label: 'PostgreSQL', value: 'postgresql' }, { label: 'SQLite', value: 'sqlite' }, { label: 'SQL Server', value: 'mssql' },
      ]},
      { key: 'host', label: _tc('protocolConfig.databaseSource.host'), placeholder: _tc('protocolConfig.databaseSource.hostPlaceholder'), default: '127.0.0.1', required: true, type: 'string', pattern: HOST_PATTERN },
      { key: 'port', label: _tc('protocolConfig.databaseSource.port'), placeholder: _tc('protocolConfig.databaseSource.portPlaceholder'), default: 3306, required: true, type: 'number' },
      { key: 'database', label: _tc('protocolConfig.databaseSource.database'), placeholder: _tc('protocolConfig.databaseSource.databasePlaceholder'), required: true, type: 'string' },
      { key: 'username', label: _tc('protocolConfig.databaseSource.username'), required: true, type: 'string' },
      { key: 'password', label: _tc('protocolConfig.databaseSource.password'), type: 'password' },
      { key: 'query', label: _tc('protocolConfig.databaseSource.query'), placeholder: _tc('protocolConfig.databaseSource.queryPlaceholder'), required: true, type: 'string' },
      { key: 'poll_interval', label: _tc('protocolConfig.databaseSource.pollInterval'), default: 60, type: 'number' },
    ],
    pointTemplates: [
      { name: 'value', data_type: 'float32', unit: '', address: 'query', access_mode: 'read', description: _tc('protocolConfig.databaseSource.pointValue') },
    ],
    capabilities: { discover: false, read: true, write: false },
    experimental: false,
  },
  'barcode-scanner': {
    label: _tc('protocolConfig.barcodeScanner.label'),
    description: _tc('protocolConfig.barcodeScanner.description'),
    icon: 'flash',
    configFields: [
      { key: 'port', label: _tc('protocolConfig.barcodeScanner.port'), placeholder: _tc('protocolConfig.barcodeScanner.portPlaceholder'), default: 'COM3', required: true, type: 'string' },
      { key: 'baudrate', label: _tc('protocolConfig.barcodeScanner.baudrate'), default: 115200, type: 'select', options: [
        { label: '9600', value: 9600 }, { label: '19200', value: 19200 }, { label: '38400', value: 38400 }, { label: '115200', value: 115200 },
      ]},
      { key: 'timeout', label: _tc('protocolConfig.barcodeScanner.timeout'), default: 5, type: 'number' },
    ],
    pointTemplates: [
      { name: 'barcode', data_type: 'string', unit: '', address: '0', access_mode: 'read', description: _tc('protocolConfig.barcodeScanner.pointBarcode') },
    ],
    capabilities: { discover: false, read: true, write: false },
    experimental: false,
  },
  'opc-da-gateway': {
    label: _tc('protocolConfig.opcDaGateway.label'),
    description: _tc('protocolConfig.opcDaGateway.description'),
    icon: 'cube',
    configFields: [
      { key: 'proxy_url', label: _tc('protocolConfig.opcDaGateway.proxyUrl'), placeholder: _tc('protocolConfig.opcDaGateway.proxyUrlPlaceholder'), default: 'http://localhost:8081', required: true, type: 'string' },
      { key: 'timeout', label: _tc('protocolConfig.opcDaGateway.timeout'), default: 10, type: 'number', min: 1, max: 60 },
      { key: 'default_server', label: _tc('protocolConfig.opcDaGateway.defaultServer'), placeholder: _tc('protocolConfig.opcDaGateway.defaultServerPlaceholder'), type: 'string' },
      { key: 'default_host', label: _tc('protocolConfig.opcDaGateway.defaultHost'), placeholder: _tc('protocolConfig.opcDaGateway.defaultHostPlaceholder'), default: 'localhost', type: 'string' },
    ],
    pointTemplates: [
      { name: 'value', data_type: 'float32', unit: '', address: 'Simulation.Items.Random', access_mode: 'read', description: _tc('protocolConfig.opcDaGateway.pointValue') },
    ],
    capabilities: { discover: true, read: true, write: true, subscribe: true },
    experimental: false,
  },
  'video-ai': {
    label: _tc('protocolConfig.videoAi.label'),
    description: _tc('protocolConfig.videoAi.description'),
    icon: 'videocam',
    configFields: [
      { key: 'model_path', label: _tc('protocolConfig.videoAi.modelPath'), placeholder: _tc('protocolConfig.videoAi.modelPathPlaceholder'), type: 'string' },
      { key: 'input_width', label: _tc('protocolConfig.videoAi.inputWidth'), default: 640, type: 'number', min: 1, max: 4096 },
      { key: 'input_height', label: _tc('protocolConfig.videoAi.inputHeight'), default: 640, type: 'number', min: 1, max: 4096 },
      { key: 'confidence_threshold', label: _tc('protocolConfig.videoAi.confidenceThreshold'), default: 0.5, type: 'number', min: 0, max: 1 },
      { key: 'nms_threshold', label: _tc('protocolConfig.videoAi.nmsIouThreshold'), default: 0.45, type: 'number', min: 0, max: 1 },
      { key: 'inference_timeout', label: _tc('protocolConfig.videoAi.inferenceTimeout'), default: 5, type: 'number', min: 0.1, max: 60 },
      { key: 'device_type', label: _tc('protocolConfig.videoAi.deviceType'), default: 'auto', type: 'select', options: [
        { label: _tc('protocolConfig.videoAi.deviceAuto'), value: 'auto' }, { label: _tc('protocolConfig.videoAi.deviceGPU'), value: 'cuda' }, { label: _tc('protocolConfig.videoAi.deviceCPU'), value: 'cpu' },
      ]},
      { key: 'poll_interval', label: _tc('protocolConfig.videoAi.pollInterval'), default: 1000, type: 'number', min: 100, max: 60000 },
      { key: 'video_source', label: _tc('protocolConfig.videoAi.sourceType'), placeholder: _tc('protocolConfig.videoAi.rtspUrlPlaceholder'), required: true, type: 'string' },
      { key: 'allowed_model_dirs', label: _tc('protocolConfig.videoAi.allowedModelDirs'), placeholder: _tc('protocolConfig.videoAi.allowedModelDirsPlaceholder'), type: 'string' },
    ],
    pointTemplates: [
      { name: 'object_count', data_type: 'int32', unit: '', address: 'object_count', access_mode: 'read', description: _tc('protocolConfig.videoAi.pointObjectCount') },
      { name: 'confidence_avg', data_type: 'float32', unit: '', address: 'confidence_avg', access_mode: 'read', description: _tc('protocolConfig.videoAi.pointConfidence') },
      { name: 'detections_json', data_type: 'string', unit: '', address: 'detections_json', access_mode: 'read', description: _tc('protocolConfig.videoAi.pointDetection') },
    ],
    capabilities: { discover: false, read: true, write: false },
    experimental: true,
  },
}))

export function getProtocolConfig(protocol: string): ProtocolConfig | undefined {
  return PROTOCOL_CONFIGS.value[protocol]
}

export function getProtocolOptions(): { label: string; value: string; description: string }[] {
  return Object.entries(PROTOCOL_CONFIGS.value).map(([key, cfg]) => ({
    label: cfg.label,
    value: key,
    description: cfg.description,
  }))
}
