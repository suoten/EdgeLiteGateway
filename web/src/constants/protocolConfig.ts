/**协议配置模板 — 设备创建时的字段映射、标签、默认值和测点模板*/

import { t } from '@/i18n'

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

export const PROTOCOL_CONFIGS: Record<string, ProtocolConfig> = {
  'modbus-tcp': {
    label: t('protocolConfig.modbusTcp.label'),
    description: t('protocolConfig.modbusTcp.description'),
    icon: '🔌',
    configFields: [
      { key: 'host', label: t('protocolConfig.modbusTcp.host'), placeholder: t('protocolConfig.modbusTcp.hostPlaceholder'), required: true, type: 'string' },
      { key: 'port', label: t('protocolConfig.modbusTcp.port'), placeholder: t('protocolConfig.modbusTcp.portPlaceholder'), default: 502, required: true, type: 'number' },
      { key: 'unit_id', label: t('protocolConfig.modbusTcp.unitId'), placeholder: t('protocolConfig.modbusTcp.unitIdPlaceholder'), default: 1, required: true, type: 'number' },
      { key: 'timeout', label: t('protocolConfig.modbusTcp.timeout'), placeholder: t('protocolConfig.modbusTcp.timeoutPlaceholder'), default: 3, type: 'number' },
    ],
    pointTemplates: [
      { name: 'temperature', data_type: 'float32', unit: '°C', address: 'HR_0', access_mode: 'read', description: t('protocolConfig.modbusTcp.pointTemperature') },
      { name: 'humidity', data_type: 'float32', unit: '%', address: 'HR_1', access_mode: 'read', description: t('protocolConfig.modbusTcp.pointHumidity') },
      { name: 'pressure', data_type: 'float32', unit: 'MPa', address: 'HR_2', access_mode: 'read', description: t('protocolConfig.modbusTcp.pointPressure') },
    ],
  },
  'modbus-rtu': {
    label: t('protocolConfig.modbusRtu.label'),
    description: t('protocolConfig.modbusRtu.description'),
    icon: '🔌',
    configFields: [
      { key: 'port', label: t('protocolConfig.modbusRtu.port'), placeholder: t('protocolConfig.modbusRtu.portPlaceholder'), required: true, type: 'string' },
      { key: 'baudrate', label: t('protocolConfig.modbusRtu.baudrate'), default: 9600, required: true, type: 'select', options: [
        { label: '9600', value: 9600 }, { label: '19200', value: 19200 }, { label: '38400', value: 38400 }, { label: '115200', value: 115200 },
      ]},
      { key: 'unit_id', label: t('protocolConfig.modbusRtu.unitId'), placeholder: t('protocolConfig.modbusRtu.unitIdPlaceholder'), default: 1, required: true, type: 'number' },
      { key: 'parity', label: t('protocolConfig.modbusRtu.parity'), default: 'N', type: 'select', options: [
        { label: t('protocolConfig.modbusRtu.parityNone'), value: 'N' }, { label: t('protocolConfig.modbusRtu.parityEven'), value: 'E' }, { label: t('protocolConfig.modbusRtu.parityOdd'), value: 'O' },
      ]},
    ],
    pointTemplates: [
      { name: 'temperature', data_type: 'float32', unit: '°C', address: 'HR_0', access_mode: 'read', description: t('protocolConfig.modbusRtu.pointTemperature') },
    ],
  },
  'opcua': {
    label: t('protocolConfig.opcua.label'),
    description: t('protocolConfig.opcua.description'),
    icon: '🔗',
    configFields: [
      { key: 'server_url', label: t('protocolConfig.opcua.serverUrl'), placeholder: t('protocolConfig.opcua.serverUrlPlaceholder'), required: true, type: 'string' },
      { key: 'username', label: t('protocolConfig.opcua.username'), placeholder: t('protocolConfig.opcua.usernamePlaceholder'), type: 'string' },
      { key: 'password', label: t('protocolConfig.opcua.password'), type: 'string' },
      { key: 'security_mode', label: t('protocolConfig.opcua.securityMode'), default: 'None', type: 'select', options: [
        { label: t('protocolConfig.opcua.securityNone'), value: 'None' }, { label: t('protocolConfig.opcua.securitySign'), value: 'Sign' }, { label: t('protocolConfig.opcua.securitySignAndEncrypt'), value: 'SignAndEncrypt' },
      ]},
    ],
    pointTemplates: [
      { name: 'value', data_type: 'float64', unit: '', address: 'ns=2;s=Node1', access_mode: 'read', description: t('protocolConfig.opcua.pointValue') },
    ],
  },
  'mqtt': {
    label: t('protocolConfig.mqtt.label'),
    description: t('protocolConfig.mqtt.description'),
    icon: '📡',
    configFields: [
      { key: 'broker', label: t('protocolConfig.mqtt.broker'), placeholder: t('protocolConfig.mqtt.brokerPlaceholder'), required: true, type: 'string' },
      { key: 'topic', label: t('protocolConfig.mqtt.topic'), placeholder: t('protocolConfig.mqtt.topicPlaceholder'), required: true, type: 'string' },
      { key: 'username', label: t('protocolConfig.mqtt.username'), type: 'string' },
      { key: 'password', label: t('protocolConfig.mqtt.password'), type: 'string' },
      { key: 'qos', label: t('protocolConfig.mqtt.qos'), default: 0, type: 'select', options: [
        { label: t('protocolConfig.mqtt.qos0'), value: 0 }, { label: t('protocolConfig.mqtt.qos1'), value: 1 }, { label: t('protocolConfig.mqtt.qos2'), value: 2 },
      ]},
    ],
    pointTemplates: [
      { name: 'payload', data_type: 'string', unit: '', address: 'payload', access_mode: 'read', description: t('protocolConfig.mqtt.pointPayload') },
    ],
  },
  's7': {
    label: t('protocolConfig.s7.label'),
    description: t('protocolConfig.s7.description'),
    icon: '⚡',
    configFields: [
      { key: 'host', label: t('protocolConfig.s7.host'), placeholder: t('protocolConfig.s7.hostPlaceholder'), required: true, type: 'string' },
      { key: 'rack', label: t('protocolConfig.s7.rack'), default: 0, type: 'number' },
      { key: 'slot', label: t('protocolConfig.s7.slot'), default: 1, type: 'number' },
      { key: 'cpu_type', label: t('protocolConfig.s7.cpuType'), default: 'S7-1200', type: 'select', options: [
        { label: 'S7-200', value: 'S7-200' }, { label: 'S7-300', value: 'S7-300' }, { label: 'S7-400', value: 'S7-400' },
        { label: 'S7-1200', value: 'S7-1200' }, { label: 'S7-1500', value: 'S7-1500' },
      ]},
    ],
    pointTemplates: [
      { name: 'DB1_value', data_type: 'float32', unit: '', address: 'DB1.DBD0', access_mode: 'read', description: t('protocolConfig.s7.pointDb1') },
    ],
  },
  'mc': {
    label: t('protocolConfig.mc.label'),
    description: t('protocolConfig.mc.description'),
    icon: '⚡',
    configFields: [
      { key: 'host', label: t('protocolConfig.mc.host'), placeholder: t('protocolConfig.mc.hostPlaceholder'), required: true, type: 'string' },
      { key: 'port', label: t('protocolConfig.mc.port'), default: 5007, type: 'number' },
      { key: 'network_no', label: t('protocolConfig.mc.networkNo'), default: 0, type: 'number' },
      { key: 'station_no', label: t('protocolConfig.mc.stationNo'), default: 0, type: 'number' },
    ],
    pointTemplates: [
      { name: 'D0', data_type: 'int16', unit: '', address: 'D0', access_mode: 'read', description: t('protocolConfig.mc.pointD0') },
    ],
  },
  'fins': {
    label: t('protocolConfig.fins.label'),
    description: t('protocolConfig.fins.description'),
    icon: '⚡',
    configFields: [
      { key: 'host', label: t('protocolConfig.fins.host'), required: true, type: 'string' },
      { key: 'port', label: t('protocolConfig.fins.port'), default: 9600, type: 'number' },
    ],
    pointTemplates: [],
  },
  'ab': {
    label: t('protocolConfig.ab.label'),
    description: t('protocolConfig.ab.description'),
    icon: '⚡',
    configFields: [
      { key: 'host', label: t('protocolConfig.ab.host'), required: true, type: 'string' },
      { key: 'port', label: t('protocolConfig.ab.port'), default: 44818, type: 'number' },
    ],
    pointTemplates: [],
  },
  'http': {
    label: t('protocolConfig.http.label'),
    description: t('protocolConfig.http.description'),
    icon: '🌐',
    configFields: [
      { key: 'url', label: t('protocolConfig.http.url'), placeholder: t('protocolConfig.http.urlPlaceholder'), required: true, type: 'string' },
      { key: 'method', label: t('protocolConfig.http.method'), default: 'GET', type: 'select', options: [
        { label: 'GET', value: 'GET' }, { label: 'POST', value: 'POST' },
      ]},
      { key: 'interval', label: t('protocolConfig.http.interval'), default: 10, type: 'number' },
    ],
    pointTemplates: [],
  },
  'sparkplug-b': {
    label: t('protocolConfig.sparkplugB.label'),
    description: t('protocolConfig.sparkplugB.description'),
    icon: '📡',
    configFields: [
      { key: 'broker', label: t('protocolConfig.sparkplugB.broker'), placeholder: t('protocolConfig.sparkplugB.brokerPlaceholder'), required: true, type: 'string' },
      { key: 'group_id', label: t('protocolConfig.sparkplugB.groupId'), placeholder: t('protocolConfig.sparkplugB.groupIdPlaceholder'), required: true, type: 'string' },
      { key: 'edge_node_id', label: t('protocolConfig.sparkplugB.edgeNodeId'), placeholder: t('protocolConfig.sparkplugB.edgeNodeIdPlaceholder'), required: true, type: 'string' },
      { key: 'username', label: t('protocolConfig.sparkplugB.username'), type: 'string' },
      { key: 'password', label: t('protocolConfig.sparkplugB.password'), type: 'string' },
    ],
    pointTemplates: [
      { name: 'metrics', data_type: 'float32', unit: '', address: 'metrics/0', access_mode: 'read', description: t('protocolConfig.sparkplugB.pointMetrics') },
    ],
  },
  'simulator': {
    label: t('protocolConfig.simulator.label'),
    description: t('protocolConfig.simulator.description'),
    icon: '🧪',
    configFields: [],
    pointTemplates: [
      { name: 'temperature', data_type: 'float32', unit: '°C', address: 'sim_temp', access_mode: 'read', description: t('protocolConfig.simulator.pointSimTemp') },
      { name: 'humidity', data_type: 'float32', unit: '%', address: 'sim_hum', access_mode: 'read', description: t('protocolConfig.simulator.pointSimHum') },
      { name: 'pressure', data_type: 'float32', unit: 'kPa', address: 'sim_press', access_mode: 'read', description: t('protocolConfig.simulator.pointSimPress') },
      { name: 'status', data_type: 'int16', unit: '', address: 'sim_status', access_mode: 'read', description: t('protocolConfig.simulator.pointSimStatus') },
    ],
  },
}

export function getProtocolConfig(protocol: string): ProtocolConfig | undefined {
  return PROTOCOL_CONFIGS[protocol]
}

export function getProtocolOptions(): { label: string; value: string; description: string }[] {
  return Object.entries(PROTOCOL_CONFIGS).map(([key, cfg]) => ({
    label: cfg.label,
    value: key,
    description: cfg.description,
  }))
}
