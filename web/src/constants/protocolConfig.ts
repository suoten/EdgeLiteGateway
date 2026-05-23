import { computed } from 'vue'
import { t, useCurrentLocale } from '@/i18n'

export interface ProtocolFieldDef {
  key: string
  label: string
  placeholder?: string
  tooltip?: string
  default?: any
  required?: boolean
  type?: 'string' | 'number' | 'boolean' | 'select'
  options?: { label: string; value: any }[]
}

export interface PointTemplate {
  name: string
  data_type: string
  unit: string
  address: string
  access_mode: string
  description?: string
}

export interface ProtocolConfig {
  label: string
  description: string
  icon: string
  configFields: ProtocolFieldDef[]
  pointTemplates: PointTemplate[]
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
    icon: '🔌',
    configFields: [
      { key: 'host', label: _tc('protocolConfig.modbusTcp.host'), placeholder: _tc('protocolConfig.modbusTcp.hostPlaceholder'), required: true, type: 'string' },
      { key: 'port', label: _tc('protocolConfig.modbusTcp.port'), placeholder: _tc('protocolConfig.modbusTcp.portPlaceholder'), default: 502, required: true, type: 'number' },
      { key: 'unit_id', label: _tc('protocolConfig.modbusTcp.unitId'), placeholder: _tc('protocolConfig.modbusTcp.unitIdPlaceholder'), default: 1, required: true, type: 'number' },
      { key: 'timeout', label: _tc('protocolConfig.modbusTcp.timeout'), placeholder: _tc('protocolConfig.modbusTcp.timeoutPlaceholder'), default: 3, type: 'number' },
    ],
    pointTemplates: [
      { name: 'temperature', data_type: 'float32', unit: '°C', address: 'HR_0', access_mode: 'read', description: _tc('protocolConfig.modbusTcp.pointTemperature') },
      { name: 'humidity', data_type: 'float32', unit: '%', address: 'HR_1', access_mode: 'read', description: _tc('protocolConfig.modbusTcp.pointHumidity') },
      { name: 'pressure', data_type: 'float32', unit: 'MPa', address: 'HR_2', access_mode: 'read', description: _tc('protocolConfig.modbusTcp.pointPressure') },
    ],
  },
  'modbus-rtu': {
    label: _tc('protocolConfig.modbusRtu.label'),
    description: _tc('protocolConfig.modbusRtu.description'),
    icon: '🔌',
    configFields: [
      { key: 'port', label: _tc('protocolConfig.modbusRtu.port'), placeholder: _tc('protocolConfig.modbusRtu.portPlaceholder'), required: true, type: 'string' },
      { key: 'baudrate', label: _tc('protocolConfig.modbusRtu.baudrate'), default: 9600, required: true, type: 'select', options: [
        { label: '9600', value: 9600 }, { label: '19200', value: 19200 }, { label: '38400', value: 38400 }, { label: '115200', value: 115200 },
      ]},
      { key: 'unit_id', label: _tc('protocolConfig.modbusRtu.unitId'), placeholder: _tc('protocolConfig.modbusRtu.unitIdPlaceholder'), default: 1, required: true, type: 'number' },
      { key: 'parity', label: _tc('protocolConfig.modbusRtu.parity'), default: 'N', type: 'select', options: [
        { label: _tc('protocolConfig.modbusRtu.parityNone'), value: 'N' }, { label: _tc('protocolConfig.modbusRtu.parityEven'), value: 'E' }, { label: _tc('protocolConfig.modbusRtu.parityOdd'), value: 'O' },
      ]},
    ],
    pointTemplates: [
      { name: 'temperature', data_type: 'float32', unit: '°C', address: 'HR_0', access_mode: 'read', description: _tc('protocolConfig.modbusRtu.pointTemperature') },
    ],
  },
  'opcua': {
    label: _tc('protocolConfig.opcua.label'),
    description: _tc('protocolConfig.opcua.description'),
    icon: '🔗',
    configFields: [
      { key: 'server_url', label: _tc('protocolConfig.opcua.serverUrl'), placeholder: _tc('protocolConfig.opcua.serverUrlPlaceholder'), required: true, type: 'string' },
      { key: 'username', label: _tc('protocolConfig.opcua.username'), placeholder: _tc('protocolConfig.opcua.usernamePlaceholder'), type: 'string' },
      { key: 'password', label: _tc('protocolConfig.opcua.password'), type: 'string' },
      { key: 'security_mode', label: _tc('protocolConfig.opcua.securityMode'), default: 'None', type: 'select', options: [
        { label: _tc('protocolConfig.opcua.securityNone'), value: 'None' }, { label: _tc('protocolConfig.opcua.securitySign'), value: 'Sign' }, { label: _tc('protocolConfig.opcua.securitySignAndEncrypt'), value: 'SignAndEncrypt' },
      ]},
    ],
    pointTemplates: [
      { name: 'value', data_type: 'float64', unit: '', address: 'ns=2;s=Node1', access_mode: 'read', description: _tc('protocolConfig.opcua.pointValue') },
    ],
  },
  'mqtt': {
    label: _tc('protocolConfig.mqtt.label'),
    description: _tc('protocolConfig.mqtt.description'),
    icon: '📡',
    configFields: [
      { key: 'broker', label: _tc('protocolConfig.mqtt.broker'), placeholder: _tc('protocolConfig.mqtt.brokerPlaceholder'), required: true, type: 'string' },
      { key: 'topic', label: _tc('protocolConfig.mqtt.topic'), placeholder: _tc('protocolConfig.mqtt.topicPlaceholder'), required: true, type: 'string' },
      { key: 'username', label: _tc('protocolConfig.mqtt.username'), type: 'string' },
      { key: 'password', label: _tc('protocolConfig.mqtt.password'), type: 'string' },
      { key: 'qos', label: _tc('protocolConfig.mqtt.qos'), default: 0, type: 'select', options: [
        { label: _tc('protocolConfig.mqtt.qos0'), value: 0 }, { label: _tc('protocolConfig.mqtt.qos1'), value: 1 }, { label: _tc('protocolConfig.mqtt.qos2'), value: 2 },
      ]},
    ],
    pointTemplates: [
      { name: 'payload', data_type: 'string', unit: '', address: 'payload', access_mode: 'read', description: _tc('protocolConfig.mqtt.pointPayload') },
    ],
  },
  's7': {
    label: _tc('protocolConfig.s7.label'),
    description: _tc('protocolConfig.s7.description'),
    icon: '⚡',
    configFields: [
      { key: 'host', label: _tc('protocolConfig.s7.host'), placeholder: _tc('protocolConfig.s7.hostPlaceholder'), required: true, type: 'string' },
      { key: 'rack', label: _tc('protocolConfig.s7.rack'), default: 0, type: 'number' },
      { key: 'slot', label: _tc('protocolConfig.s7.slot'), default: 1, type: 'number' },
      { key: 'cpu_type', label: _tc('protocolConfig.s7.cpuType'), default: 'S7-1200', type: 'select', options: [
        { label: 'S7-200', value: 'S7-200' }, { label: 'S7-300', value: 'S7-300' }, { label: 'S7-400', value: 'S7-400' },
        { label: 'S7-1200', value: 'S7-1200' }, { label: 'S7-1500', value: 'S7-1500' },
      ]},
    ],
    pointTemplates: [
      { name: 'DB1_value', data_type: 'float32', unit: '', address: 'DB1.DBD0', access_mode: 'read', description: _tc('protocolConfig.s7.pointDb1') },
    ],
  },
  'mc': {
    label: _tc('protocolConfig.mc.label'),
    description: _tc('protocolConfig.mc.description'),
    icon: '⚡',
    configFields: [
      { key: 'host', label: _tc('protocolConfig.mc.host'), placeholder: _tc('protocolConfig.mc.hostPlaceholder'), required: true, type: 'string' },
      { key: 'port', label: _tc('protocolConfig.mc.port'), default: 5007, type: 'number' },
      { key: 'network_no', label: _tc('protocolConfig.mc.networkNo'), default: 0, type: 'number' },
      { key: 'station_no', label: _tc('protocolConfig.mc.stationNo'), default: 0, type: 'number' },
    ],
    pointTemplates: [
      { name: 'D0', data_type: 'int16', unit: '', address: 'D0', access_mode: 'read', description: _tc('protocolConfig.mc.pointD0') },
    ],
  },
  'fins': {
    label: _tc('protocolConfig.fins.label'),
    description: _tc('protocolConfig.fins.description'),
    icon: '⚡',
    configFields: [
      { key: 'host', label: _tc('protocolConfig.fins.host'), required: true, type: 'string' },
      { key: 'port', label: _tc('protocolConfig.fins.port'), default: 9600, type: 'number' },
    ],
    pointTemplates: [],
  },
  'ab': {
    label: _tc('protocolConfig.ab.label'),
    description: _tc('protocolConfig.ab.description'),
    icon: '⚡',
    configFields: [
      { key: 'host', label: _tc('protocolConfig.ab.host'), required: true, type: 'string' },
      { key: 'port', label: _tc('protocolConfig.ab.port'), default: 44818, type: 'number' },
    ],
    pointTemplates: [],
  },
  'http': {
    label: _tc('protocolConfig.http.label'),
    description: _tc('protocolConfig.http.description'),
    icon: '🌐',
    configFields: [
      { key: 'url', label: _tc('protocolConfig.http.url'), placeholder: _tc('protocolConfig.http.urlPlaceholder'), required: true, type: 'string' },
      { key: 'method', label: _tc('protocolConfig.http.method'), default: 'GET', type: 'select', options: [
        { label: 'GET', value: 'GET' }, { label: 'POST', value: 'POST' },
      ]},
      { key: 'interval', label: _tc('protocolConfig.http.interval'), default: 10, type: 'number' },
    ],
    pointTemplates: [],
  },
  'sparkplug-b': {
    label: _tc('protocolConfig.sparkplugB.label'),
    description: _tc('protocolConfig.sparkplugB.description'),
    icon: '📡',
    configFields: [
      { key: 'broker', label: _tc('protocolConfig.sparkplugB.broker'), placeholder: _tc('protocolConfig.sparkplugB.brokerPlaceholder'), required: true, type: 'string' },
      { key: 'group_id', label: _tc('protocolConfig.sparkplugB.groupId'), placeholder: _tc('protocolConfig.sparkplugB.groupIdPlaceholder'), required: true, type: 'string' },
      { key: 'edge_node_id', label: _tc('protocolConfig.sparkplugB.edgeNodeId'), placeholder: _tc('protocolConfig.sparkplugB.edgeNodeIdPlaceholder'), required: true, type: 'string' },
      { key: 'username', label: _tc('protocolConfig.sparkplugB.username'), type: 'string' },
      { key: 'password', label: _tc('protocolConfig.sparkplugB.password'), type: 'string' },
    ],
    pointTemplates: [
      { name: 'metrics', data_type: 'float32', unit: '', address: 'metrics/0', access_mode: 'read', description: _tc('protocolConfig.sparkplugB.pointMetrics') },
    ],
  },
  'simulator': {
    label: _tc('protocolConfig.simulator.label'),
    description: _tc('protocolConfig.simulator.description'),
    icon: '🧪',
    configFields: [],
    pointTemplates: [
      { name: 'temperature', data_type: 'float32', unit: '°C', address: 'sim_temp', access_mode: 'read', description: _tc('protocolConfig.simulator.pointSimTemp') },
      { name: 'humidity', data_type: 'float32', unit: '%', address: 'sim_hum', access_mode: 'read', description: _tc('protocolConfig.simulator.pointSimHum') },
      { name: 'pressure', data_type: 'float32', unit: 'kPa', address: 'sim_press', access_mode: 'read', description: _tc('protocolConfig.simulator.pointSimPress') },
      { name: 'status', data_type: 'int16', unit: '', address: 'sim_status', access_mode: 'read', description: _tc('protocolConfig.simulator.pointSimStatus') },
    ],
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
