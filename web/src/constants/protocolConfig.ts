/**协议配置模板 — 设备创建时的字段映射、中文标签、默认值和测点模板*/

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
    label: 'Modbus TCP',
    description: '最常用的工业协议，适用于PLC、仪表等设备',
    icon: '🔌',
    configFields: [
      { key: 'host', label: '设备IP地址', placeholder: '例如: 192.168.1.100', required: true, type: 'string' },
      { key: 'port', label: '端口号', placeholder: '默认502', default: 502, required: true, type: 'number' },
      { key: 'unit_id', label: '从站地址', placeholder: '1-247，通常为1', default: 1, required: true, type: 'number' },
      { key: 'timeout', label: '超时时间(秒)', placeholder: '默认3秒', default: 3, type: 'number' },
    ],
    pointTemplates: [
      { name: 'temperature', data_type: 'float32', unit: '°C', address: 'HR_0', access_mode: 'read', description: '温度' },
      { name: 'humidity', data_type: 'float32', unit: '%', address: 'HR_1', access_mode: 'read', description: '湿度' },
      { name: 'pressure', data_type: 'float32', unit: 'MPa', address: 'HR_2', access_mode: 'read', description: '压力' },
    ],
  },
  'modbus-rtu': {
    label: 'Modbus RTU',
    description: '串口通信协议，适用于RS485总线设备',
    icon: '🔌',
    configFields: [
      { key: 'port', label: '串口设备', placeholder: '例如: COM3 或 /dev/ttyUSB0', required: true, type: 'string' },
      { key: 'baudrate', label: '波特率', default: 9600, required: true, type: 'select', options: [
        { label: '9600', value: 9600 }, { label: '19200', value: 19200 }, { label: '38400', value: 38400 }, { label: '115200', value: 115200 },
      ]},
      { key: 'unit_id', label: '从站地址', placeholder: '1-247', default: 1, required: true, type: 'number' },
      { key: 'parity', label: '校验位', default: 'N', type: 'select', options: [
        { label: '无校验(N)', value: 'N' }, { label: '偶校验(E)', value: 'E' }, { label: '奇校验(O)', value: 'O' },
      ]},
    ],
    pointTemplates: [
      { name: 'temperature', data_type: 'float32', unit: '°C', address: 'HR_0', access_mode: 'read', description: '温度' },
    ],
  },
  'opcua': {
    label: 'OPC UA',
    description: '工业标准通信协议，适用于SCADA/MES系统对接',
    icon: '🔗',
    configFields: [
      { key: 'server_url', label: '服务器地址', placeholder: '例如: opc.tcp://192.168.1.100:4840', required: true, type: 'string' },
      { key: 'username', label: '用户名(可选)', placeholder: '匿名连接可留空', type: 'string' },
      { key: 'password', label: '密码(可选)', type: 'string' },
      { key: 'security_mode', label: '安全模式', default: 'None', type: 'select', options: [
        { label: '无加密', value: 'None' }, { label: '签名', value: 'Sign' }, { label: '签名+加密', value: 'SignAndEncrypt' },
      ]},
    ],
    pointTemplates: [
      { name: 'value', data_type: 'float64', unit: '', address: 'ns=2;s=Node1', access_mode: 'read', description: '变量值' },
    ],
  },
  'mqtt': {
    label: 'MQTT',
    description: '物联网消息协议，适用于传感器数据上报',
    icon: '📡',
    configFields: [
      { key: 'broker', label: 'Broker地址', placeholder: '例如: 192.168.1.100:1883', required: true, type: 'string' },
      { key: 'topic', label: '订阅主题', placeholder: '例如: sensor/data', required: true, type: 'string' },
      { key: 'username', label: '用户名(可选)', type: 'string' },
      { key: 'password', label: '密码(可选)', type: 'string' },
      { key: 'qos', label: 'QoS等级', default: 0, type: 'select', options: [
        { label: '0 - 最多一次', value: 0 }, { label: '1 - 至少一次', value: 1 }, { label: '2 - 恰好一次', value: 2 },
      ]},
    ],
    pointTemplates: [
      { name: 'payload', data_type: 'string', unit: '', address: 'payload', access_mode: 'read', description: '消息内容' },
    ],
  },
  's7': {
    label: '西门子S7',
    description: '西门子PLC专有协议(S7Comm)',
    icon: '⚡',
    configFields: [
      { key: 'host', label: 'PLC IP地址', placeholder: '例如: 192.168.1.100', required: true, type: 'string' },
      { key: 'rack', label: '机架号', default: 0, type: 'number' },
      { key: 'slot', label: '槽号', default: 1, type: 'number' },
      { key: 'cpu_type', label: 'CPU类型', default: 'S7-1200', type: 'select', options: [
        { label: 'S7-200', value: 'S7-200' }, { label: 'S7-300', value: 'S7-300' }, { label: 'S7-400', value: 'S7-400' },
        { label: 'S7-1200', value: 'S7-1200' }, { label: 'S7-1500', value: 'S7-1500' },
      ]},
    ],
    pointTemplates: [
      { name: 'DB1_value', data_type: 'float32', unit: '', address: 'DB1.DBD0', access_mode: 'read', description: 'DB1数据' },
    ],
  },
  'mc': {
    label: '三菱MC',
    description: '三菱PLC专有协议(MC Protocol)',
    icon: '⚡',
    configFields: [
      { key: 'host', label: 'PLC IP地址', placeholder: '例如: 192.168.1.100', required: true, type: 'string' },
      { key: 'port', label: '端口号', default: 5007, type: 'number' },
      { key: 'network_no', label: '网络号', default: 0, type: 'number' },
      { key: 'station_no', label: '站号', default: 0, type: 'number' },
    ],
    pointTemplates: [
      { name: 'D0', data_type: 'int16', unit: '', address: 'D0', access_mode: 'read', description: 'D0寄存器' },
    ],
  },
  'fins': {
    label: '欧姆龙FINS',
    description: '欧姆龙PLC专有协议',
    icon: '⚡',
    configFields: [
      { key: 'host', label: 'PLC IP地址', required: true, type: 'string' },
      { key: 'port', label: '端口号', default: 9600, type: 'number' },
    ],
    pointTemplates: [],
  },
  'ab': {
    label: 'Allen-Bradley',
    description: 'AB/Rockwell PLC协议(CIP)',
    icon: '⚡',
    configFields: [
      { key: 'host', label: 'PLC IP地址', required: true, type: 'string' },
      { key: 'port', label: '端口号', default: 44818, type: 'number' },
    ],
    pointTemplates: [],
  },
  'http': {
    label: 'HTTP/Webhook',
    description: 'HTTP接口轮询或被动接收',
    icon: '🌐',
    configFields: [
      { key: 'url', label: '接口地址', placeholder: '例如: http://192.168.1.100/api/data', required: true, type: 'string' },
      { key: 'method', label: '请求方法', default: 'GET', type: 'select', options: [
        { label: 'GET', value: 'GET' }, { label: 'POST', value: 'POST' },
      ]},
      { key: 'interval', label: '轮询间隔(秒)', default: 10, type: 'number' },
    ],
    pointTemplates: [],
  },
  'simulator': {
    label: '模拟器',
    description: '内置模拟设备，用于测试和演示',
    icon: '🧪',
    configFields: [],
    pointTemplates: [
      { name: 'temperature', data_type: 'float32', unit: '°C', address: 'sim_temp', access_mode: 'read', description: '模拟温度(0-100°C)' },
      { name: 'humidity', data_type: 'float32', unit: '%', address: 'sim_hum', access_mode: 'read', description: '模拟湿度(0-100%)' },
      { name: 'pressure', data_type: 'float32', unit: 'kPa', address: 'sim_press', access_mode: 'read', description: '模拟压力(80-120kPa)' },
      { name: 'status', data_type: 'int16', unit: '', address: 'sim_status', access_mode: 'read', description: '设备状态(0=停机 1=运行)' },
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
