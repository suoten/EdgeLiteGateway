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
    icon: 'flash',
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
    icon: 'flash',
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
    icon: 'link',
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
    icon: 'radio',
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
    icon: 'flash',
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
    icon: 'flash',
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
    icon: 'flash',
    configFields: [
      { key: 'host', label: _tc('protocolConfig.fins.host'), required: true, type: 'string' },
      { key: 'port', label: _tc('protocolConfig.fins.port'), default: 9600, type: 'number' },
    ],
    pointTemplates: [],
  },
  'ab': {
    label: _tc('protocolConfig.ab.label'),
    description: _tc('protocolConfig.ab.description'),
    icon: 'flash',
    configFields: [
      { key: 'host', label: _tc('protocolConfig.ab.host'), required: true, type: 'string' },
      { key: 'port', label: _tc('protocolConfig.ab.port'), default: 44818, type: 'number' },
    ],
    pointTemplates: [],
  },
  'http': {
    label: _tc('protocolConfig.http.label'),
    description: _tc('protocolConfig.http.description'),
    icon: 'globe',
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
    icon: 'radio',
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
    icon: 'flask',
    configFields: [],
    pointTemplates: [
      { name: 'temperature', data_type: 'float32', unit: '°C', address: 'sim_temp', access_mode: 'read', description: _tc('protocolConfig.simulator.pointSimTemp') },
      { name: 'humidity', data_type: 'float32', unit: '%', address: 'sim_hum', access_mode: 'read', description: _tc('protocolConfig.simulator.pointSimHum') },
      { name: 'pressure', data_type: 'float32', unit: 'kPa', address: 'sim_press', access_mode: 'read', description: _tc('protocolConfig.simulator.pointSimPress') },
      { name: 'status', data_type: 'int16', unit: '', address: 'sim_status', access_mode: 'read', description: _tc('protocolConfig.simulator.pointSimStatus') },
    ],
  },
  'bacnet': {
    label: _tc('protocolConfig.bacnet.label'),
    description: _tc('protocolConfig.bacnet.description'),
    icon: 'business',
    configFields: [
      { key: 'host', label: _tc('protocolConfig.bacnet.host'), placeholder: _tc('protocolConfig.bacnet.hostPlaceholder'), default: '192.168.1.255', required: true, type: 'string' },
      { key: 'port', label: _tc('protocolConfig.bacnet.port'), placeholder: _tc('protocolConfig.bacnet.portPlaceholder'), default: 47808, required: true, type: 'number' },
      { key: 'device_instance', label: _tc('protocolConfig.bacnet.deviceInstance'), placeholder: _tc('protocolConfig.bacnet.deviceInstancePlaceholder'), default: 100, type: 'number' },
      { key: 'timeout', label: _tc('protocolConfig.bacnet.timeout'), placeholder: _tc('protocolConfig.bacnet.timeoutPlaceholder'), default: 5, type: 'number' },
      { key: 'enable_cov', label: _tc('protocolConfig.bacnet.enableCov'), default: true, type: 'boolean' },
    ],
    pointTemplates: [
      { name: 'temperature', data_type: 'float32', unit: '°C', address: 'AV:1.presentValue', access_mode: 'read', description: _tc('protocolConfig.bacnet.pointTemp') },
    ],
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
    ],
    pointTemplates: [
      { name: 'switch', data_type: 'bool', unit: '', address: '1/2/3', access_mode: 'readwrite', description: _tc('protocolConfig.knx.pointSwitch') },
    ],
  },
  'dnp3': {
    label: _tc('protocolConfig.dnp3.label'),
    description: _tc('protocolConfig.dnp3.description'),
    icon: 'flash',
    configFields: [
      { key: 'host', label: _tc('protocolConfig.dnp3.host'), placeholder: _tc('protocolConfig.dnp3.hostPlaceholder'), default: '192.168.1.1', required: true, type: 'string' },
      { key: 'port', label: _tc('protocolConfig.dnp3.port'), placeholder: _tc('protocolConfig.dnp3.portPlaceholder'), default: 20000, required: true, type: 'number' },
      { key: 'device_address', label: _tc('protocolConfig.dnp3.deviceAddress'), placeholder: _tc('protocolConfig.dnp3.deviceAddressPlaceholder'), default: 1, type: 'number' },
      { key: 'timeout', label: _tc('protocolConfig.dnp3.timeout'), placeholder: _tc('protocolConfig.dnp3.timeoutPlaceholder'), default: 10, type: 'number' },
    ],
    pointTemplates: [
      { name: 'binary_input', data_type: 'bool', unit: '', address: 'binary:0', access_mode: 'read', description: _tc('protocolConfig.dnp3.pointBinary') },
      { name: 'analog_input', data_type: 'float32', unit: '', address: 'analog:0', access_mode: 'read', description: _tc('protocolConfig.dnp3.pointAnalog') },
    ],
  },
  'ethercat': {
    label: _tc('protocolConfig.ethercat.label'),
    description: _tc('protocolConfig.ethercat.description'),
    icon: 'rocket',
    configFields: [
      { key: 'iface', label: _tc('protocolConfig.ethercat.iface'), placeholder: _tc('protocolConfig.ethercat.ifacePlaceholder'), default: 'eth0', required: true, type: 'string' },
      { key: 'timeout', label: _tc('protocolConfig.ethercat.timeout'), placeholder: _tc('protocolConfig.ethercat.timeoutPlaceholder'), default: 2000, type: 'number' },
      { key: 'enable_dc', label: _tc('protocolConfig.ethercat.enableDc'), default: false, type: 'boolean' },
    ],
    pointTemplates: [
      { name: 'input_0', data_type: 'uint16', unit: '', address: 'input_0', access_mode: 'read', description: _tc('protocolConfig.ethercat.pointInput') },
    ],
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
    ],
    pointTemplates: [
      { name: 'device_status', data_type: 'int16', unit: '', address: 'status', access_mode: 'read', description: _tc('protocolConfig.profinet.pointStatus') },
    ],
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
    ],
    pointTemplates: [
      { name: 'voltage_a', data_type: 'float32', unit: 'V', address: 'voltage_a', access_mode: 'read', description: _tc('protocolConfig.dl645.pointVoltage') },
      { name: 'current_a', data_type: 'float32', unit: 'A', address: 'current_a', access_mode: 'read', description: _tc('protocolConfig.dl645.pointCurrent') },
    ],
  },
  'iec104': {
    label: _tc('protocolConfig.iec104.label'),
    description: _tc('protocolConfig.iec104.description'),
    icon: 'radio',
    configFields: [
      { key: 'host', label: _tc('protocolConfig.iec104.host'), placeholder: _tc('protocolConfig.iec104.hostPlaceholder'), default: '127.0.0.1', required: true, type: 'string' },
      { key: 'port', label: _tc('protocolConfig.iec104.port'), placeholder: _tc('protocolConfig.iec104.portPlaceholder'), default: 2404, required: true, type: 'number' },
      { key: 'asdu_addr', label: _tc('protocolConfig.iec104.asduAddr'), placeholder: _tc('protocolConfig.iec104.asduAddrPlaceholder'), default: 1, type: 'number' },
      { key: 'heartbeat_interval', label: _tc('protocolConfig.iec104.heartbeatInterval'), placeholder: _tc('protocolConfig.iec104.heartbeatIntervalPlaceholder'), default: 30, type: 'number' },
    ],
    pointTemplates: [
      { name: 'digital_input', data_type: 'bool', unit: '', address: '1', access_mode: 'read', description: _tc('protocolConfig.iec104.pointDigital') },
      { name: 'analog_input', data_type: 'float32', unit: '', address: '1', access_mode: 'read', description: _tc('protocolConfig.iec104.pointAnalog') },
    ],
  },
  'fanuc': {
    label: _tc('protocolConfig.fanuc.label'),
    description: _tc('protocolConfig.fanuc.description'),
    icon: 'construct',
    configFields: [
      { key: 'host', label: _tc('protocolConfig.fanuc.host'), placeholder: _tc('protocolConfig.fanuc.hostPlaceholder'), default: '192.168.1.1', required: true, type: 'string' },
      { key: 'port', label: _tc('protocolConfig.fanuc.port'), placeholder: _tc('protocolConfig.fanuc.portPlaceholder'), default: 8193, type: 'number' },
      { key: 'timeout', label: _tc('protocolConfig.fanuc.timeout'), placeholder: _tc('protocolConfig.fanuc.timeoutPlaceholder'), default: 5, type: 'number' },
      { key: 'max_axes', label: _tc('protocolConfig.fanuc.maxAxes'), default: 8, type: 'number' },
    ],
    pointTemplates: [
      { name: 'cnc_status', data_type: 'int16', unit: '', address: 'cnc_status', access_mode: 'read', description: _tc('protocolConfig.fanuc.pointStatus') },
      { name: 'cnc_position', data_type: 'float32', unit: 'mm', address: 'cnc_position', access_mode: 'read', description: _tc('protocolConfig.fanuc.pointPosition') },
    ],
  },
  'mtconnect': {
    label: _tc('protocolConfig.mtconnect.label'),
    description: _tc('protocolConfig.mtconnect.description'),
    icon: 'business',
    configFields: [
      { key: 'url', label: _tc('protocolConfig.mtconnect.url'), placeholder: _tc('protocolConfig.mtconnect.urlPlaceholder'), default: 'http://127.0.0.1:5000', required: true, type: 'string' },
      { key: 'device', label: _tc('protocolConfig.mtconnect.device'), placeholder: _tc('protocolConfig.mtconnect.devicePlaceholder'), type: 'string' },
      { key: 'poll_interval', label: _tc('protocolConfig.mtconnect.pollInterval'), default: 1, type: 'number' },
    ],
    pointTemplates: [
      { name: 'X_position', data_type: 'float32', unit: 'mm', address: 'Xact', access_mode: 'read', description: _tc('protocolConfig.mtconnect.pointXpos') },
      { name: 'spindle_speed', data_type: 'float32', unit: 'rpm', address: 'Sspeed', access_mode: 'read', description: _tc('protocolConfig.mtconnect.pointSpindle') },
    ],
  },
  'kuka': {
    label: _tc('protocolConfig.kuka.label'),
    description: _tc('protocolConfig.kuka.description'),
    icon: 'flash',
    configFields: [
      { key: 'ip', label: _tc('protocolConfig.kuka.host'), placeholder: _tc('protocolConfig.kuka.hostPlaceholder'), default: '192.168.1.100', required: true, type: 'string' },
      { key: 'port', label: _tc('protocolConfig.kuka.port'), placeholder: _tc('protocolConfig.kuka.portPlaceholder'), default: 54600, type: 'number' },
    ],
    pointTemplates: [
      { name: 'position', data_type: 'float32', unit: 'mm', address: '$POS_ACT', access_mode: 'read', description: _tc('protocolConfig.kuka.pointPosition') },
    ],
  },
  'abb': {
    label: _tc('protocolConfig.abb.label'),
    description: _tc('protocolConfig.abb.description'),
    icon: 'flash',
    configFields: [
      { key: 'ip', label: _tc('protocolConfig.abb.host'), placeholder: _tc('protocolConfig.abb.hostPlaceholder'), default: '192.168.1.100', required: true, type: 'string' },
      { key: 'port', label: _tc('protocolConfig.abb.port'), placeholder: _tc('protocolConfig.abb.portPlaceholder'), default: 80, type: 'number' },
    ],
    pointTemplates: [
      { name: 'joints', data_type: 'float32', unit: 'deg', address: 'joints', access_mode: 'read', description: _tc('protocolConfig.abb.pointJoints') },
      { name: 'tcp_position', data_type: 'float32', unit: 'mm', address: 'tcp', access_mode: 'read', description: _tc('protocolConfig.abb.pointTcp') },
    ],
  },
  'opcda': {
    label: _tc('protocolConfig.opcda.label'),
    description: _tc('protocolConfig.opcda.description'),
    icon: 'cube',
    configFields: [
      { key: 'prog_id', label: _tc('protocolConfig.opcda.progId'), placeholder: _tc('protocolConfig.opcda.progIdPlaceholder'), default: 'Matrikon.OPC.Simulation', required: true, type: 'string' },
      { key: 'host', label: _tc('protocolConfig.opcda.host'), placeholder: _tc('protocolConfig.opcda.hostPlaceholder'), default: 'localhost', type: 'string' },
      { key: 'update_rate', label: _tc('protocolConfig.opcda.updateRate'), default: 1000, type: 'number' },
    ],
    pointTemplates: [
      { name: 'random_value', data_type: 'float32', unit: '', address: 'Simulation.Items.Random', access_mode: 'read', description: _tc('protocolConfig.opcda.pointRandom') },
    ],
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
  },
  'onvif': {
    label: _tc('protocolConfig.onvif.label'),
    description: _tc('protocolConfig.onvif.description'),
    icon: 'videocam',
    configFields: [
      { key: 'ip', label: _tc('protocolConfig.onvif.host'), placeholder: _tc('protocolConfig.onvif.hostPlaceholder'), default: '192.168.1.100', required: true, type: 'string' },
      { key: 'port', label: _tc('protocolConfig.onvif.port'), placeholder: _tc('protocolConfig.onvif.portPlaceholder'), default: 80, type: 'number' },
      { key: 'username', label: _tc('protocolConfig.onvif.username'), default: 'admin', type: 'string' },
      { key: 'password', label: _tc('protocolConfig.onvif.password'), type: 'string' },
    ],
    pointTemplates: [
      { name: 'rtsp_url', data_type: 'string', unit: '', address: 'rtsp', access_mode: 'read', description: _tc('protocolConfig.onvif.pointRtsp') },
    ],
  },
  'video': {
    label: _tc('protocolConfig.video.label'),
    description: _tc('protocolConfig.video.description'),
    icon: 'videocam',
    configFields: [
      { key: 'pygbsentry_url', label: _tc('protocolConfig.video.pygbsentryUrl'), placeholder: _tc('protocolConfig.video.pygbsentryUrlPlaceholder'), default: 'http://127.0.0.1:8080', required: true, type: 'string' },
      { key: 'username', label: _tc('protocolConfig.video.username'), default: 'admin', type: 'string' },
      { key: 'password', label: _tc('protocolConfig.video.password'), type: 'string' },
    ],
    pointTemplates: [
      { name: 'device_status', data_type: 'string', unit: '', address: 'status', access_mode: 'read', description: _tc('protocolConfig.video.pointStatus') },
    ],
  },
  'toledo': {
    label: _tc('protocolConfig.toledo.label'),
    description: _tc('protocolConfig.toledo.description'),
    icon: 'scale',
    configFields: [
      { key: 'host', label: _tc('protocolConfig.toledo.host'), placeholder: _tc('protocolConfig.toledo.hostPlaceholder'), default: '192.168.1.100', required: true, type: 'string' },
      { key: 'port', label: _tc('protocolConfig.toledo.port'), placeholder: _tc('protocolConfig.toledo.portPlaceholder'), default: 5000, required: true, type: 'number' },
      { key: 'slave_id', label: _tc('protocolConfig.toledo.slaveId'), default: 1, required: true, type: 'number' },
      { key: 'timeout', label: _tc('protocolConfig.toledo.timeout'), default: 5, type: 'number' },
    ],
    pointTemplates: [
      { name: 'weight', data_type: 'float32', unit: 'kg', address: '0', access_mode: 'read', description: _tc('protocolConfig.toledo.pointWeight') },
    ],
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
    ],
    pointTemplates: [
      { name: 'value', data_type: 'float32', unit: '', address: '0', access_mode: 'read', description: _tc('protocolConfig.serialDevice.pointValue') },
    ],
  },
  'database-source': {
    label: _tc('protocolConfig.databaseSource.label'),
    description: _tc('protocolConfig.databaseSource.description'),
    icon: 'database',
    configFields: [
      { key: 'db_type', label: _tc('protocolConfig.databaseSource.dbType'), default: 'mysql', required: true, type: 'select', options: [
        { label: 'MySQL', value: 'mysql' }, { label: 'PostgreSQL', value: 'postgresql' }, { label: 'SQLite', value: 'sqlite' }, { label: 'SQL Server', value: 'mssql' },
      ]},
      { key: 'host', label: _tc('protocolConfig.databaseSource.host'), placeholder: _tc('protocolConfig.databaseSource.hostPlaceholder'), default: '127.0.0.1', required: true, type: 'string' },
      { key: 'port', label: _tc('protocolConfig.databaseSource.port'), placeholder: _tc('protocolConfig.databaseSource.portPlaceholder'), default: 3306, required: true, type: 'number' },
      { key: 'database', label: _tc('protocolConfig.databaseSource.database'), placeholder: _tc('protocolConfig.databaseSource.databasePlaceholder'), required: true, type: 'string' },
      { key: 'username', label: _tc('protocolConfig.databaseSource.username'), required: true, type: 'string' },
      { key: 'password', label: _tc('protocolConfig.databaseSource.password'), type: 'string' },
      { key: 'query', label: _tc('protocolConfig.databaseSource.query'), placeholder: _tc('protocolConfig.databaseSource.queryPlaceholder'), required: true, type: 'string' },
      { key: 'poll_interval', label: _tc('protocolConfig.databaseSource.pollInterval'), default: 60, type: 'number' },
    ],
    pointTemplates: [
      { name: 'value', data_type: 'float32', unit: '', address: 'query', access_mode: 'read', description: _tc('protocolConfig.databaseSource.pointValue') },
    ],
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
