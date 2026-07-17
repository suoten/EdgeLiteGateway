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

// 仅保留13种核心协议，其他工业协议已彻底清除
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
  'onvif': {
    label: _tc('protocolConfig.onvif.label'),
    description: _tc('protocolConfig.onvif.description'),
    icon: 'videocam',
    configFields: [
      { key: 'ip', label: _tc('protocolConfig.onvif.host'), placeholder: _tc('protocolConfig.onvif.hostPlaceholder'), default: '192.168.1.100', required: true, type: 'string', pattern: HOST_PATTERN },
      { key: 'port', label: _tc('protocolConfig.onvif.port'), placeholder: _tc('protocolConfig.onvif.portPlaceholder'), default: 80, type: 'number', min: 1, max: 65535 },
      { key: 'username', label: _tc('protocolConfig.onvif.username'), default: '', required: true, type: 'string' },
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
