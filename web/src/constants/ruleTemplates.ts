/**规则告警模板 — 常见工业场景的预置规则*/

export interface RuleTemplate {
  id: string
  name: string
  category: string
  description: string
  severity: string
  conditions: {
    point: string
    operator: string
    threshold: number
    unit: string
  }[]
  logic: 'AND' | 'OR'
  duration: number
}

export const RULE_TEMPLATES: RuleTemplate[] = [
  {
    id: 'temp-high',
    name: '温度过高告警',
    category: '温度',
    description: '当温度超过设定阈值时触发告警，适用于电机、轴承、环境温度监控',
    severity: 'critical',
    conditions: [{ point: 'temperature', operator: '>', threshold: 80, unit: '°C' }],
    logic: 'AND',
    duration: 60,
  },
  {
    id: 'temp-low',
    name: '温度过低告警',
    category: '温度',
    description: '当温度低于设定阈值时触发告警，适用于冷冻、冷藏、防冻场景',
    severity: 'warning',
    conditions: [{ point: 'temperature', operator: '<', threshold: 5, unit: '°C' }],
    logic: 'AND',
    duration: 120,
  },
  {
    id: 'pressure-high',
    name: '压力过高告警',
    category: '压力',
    description: '当压力超过安全阈值时触发告警，适用于管道、容器、锅炉监控',
    severity: 'critical',
    conditions: [{ point: 'pressure', operator: '>', threshold: 1.0, unit: 'MPa' }],
    logic: 'AND',
    duration: 30,
  },
  {
    id: 'humidity-abnormal',
    name: '湿度异常告警',
    category: '湿度',
    description: '当湿度超出正常范围时触发告警，适用于仓储、机房、温室场景',
    severity: 'warning',
    conditions: [{ point: 'humidity', operator: '>', threshold: 85, unit: '%' }],
    logic: 'AND',
    duration: 300,
  },
  {
    id: 'voltage-low',
    name: '电压偏低告警',
    category: '电气',
    description: '当电压低于正常值时触发告警，适用于供电质量监控',
    severity: 'warning',
    conditions: [{ point: 'voltage', operator: '<', threshold: 200, unit: 'V' }],
    logic: 'AND',
    duration: 60,
  },
  {
    id: 'current-overload',
    name: '电流过载告警',
    category: '电气',
    description: '当电流超过额定值时触发告警，适用于电机保护、线路监控',
    severity: 'critical',
    conditions: [{ point: 'current', operator: '>', threshold: 50, unit: 'A' }],
    logic: 'AND',
    duration: 30,
  },
  {
    id: 'level-high',
    name: '液位过高告警',
    category: '液位',
    description: '当液位超过高限位时触发告警，适用于水箱、油罐、反应釜',
    severity: 'critical',
    conditions: [{ point: 'level', operator: '>', threshold: 90, unit: '%' }],
    logic: 'AND',
    duration: 60,
  },
  {
    id: 'device-offline',
    name: '设备离线告警',
    category: '通信',
    description: '当设备持续无数据上报时触发告警，适用于通信故障检测',
    severity: 'warning',
    conditions: [{ point: 'last_seen', operator: '<', threshold: 0, unit: '秒前' }],
    logic: 'AND',
    duration: 300,
  },
]

export const OPERATOR_OPTIONS = [
  { label: '大于 (>)', value: '>' },
  { label: '小于 (<)', value: '<' },
  { label: '等于 (=)', value: '==' },
  { label: '不等于 (≠)', value: '!=' },
  { label: '大于等于 (≥)', value: '>=' },
  { label: '小于等于 (≤)', value: '<=' },
]

export const SEVERITY_OPTIONS = [
  { label: '提示', value: 'info', color: '#909399' },
  { label: '警告', value: 'warning', color: '#E6A23C' },
  { label: '严重', value: 'critical', color: '#F56C6C' },
]

export function getTemplatesByCategory(category: string): RuleTemplate[] {
  return RULE_TEMPLATES.filter(t => t.category === category)
}

export function getTemplateCategories(): string[] {
  return [...new Set(RULE_TEMPLATES.map(t => t.category))]
}
