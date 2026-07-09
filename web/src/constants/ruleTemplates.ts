import { computed } from 'vue'
import { t, useCurrentLocale } from '@/i18n'
import { SEMANTIC_COLORS } from '@/constants/chartPalette'

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

// FIXED-P3: 顶层t()→computed，语言切换响应式
const _locale = useCurrentLocale()
function _tc(key: string) {
  void _locale.value
  return t(key)
}

export const RULE_TEMPLATES = computed<RuleTemplate[]>(() => [
  {
    id: 'temp-high',
    name: _tc('ruleTemplate.tempHigh.name'),
    category: _tc('ruleTemplate.category.temperature'),
    description: _tc('ruleTemplate.tempHigh.description'),
    severity: 'critical',
    conditions: [{ point: 'temperature', operator: '>', threshold: 80, unit: '°C' }],
    logic: 'AND',
    duration: 60,
  },
  {
    id: 'temp-low',
    name: _tc('ruleTemplate.tempLow.name'),
    category: _tc('ruleTemplate.category.temperature'),
    description: _tc('ruleTemplate.tempLow.description'),
    severity: 'warning',
    conditions: [{ point: 'temperature', operator: '<', threshold: 5, unit: '°C' }],
    logic: 'AND',
    duration: 120,
  },
  {
    id: 'pressure-high',
    name: _tc('ruleTemplate.pressureHigh.name'),
    category: _tc('ruleTemplate.category.pressure'),
    description: _tc('ruleTemplate.pressureHigh.description'),
    severity: 'critical',
    conditions: [{ point: 'pressure', operator: '>', threshold: 1.0, unit: 'MPa' }],
    logic: 'AND',
    duration: 30,
  },
  {
    id: 'humidity-abnormal',
    name: _tc('ruleTemplate.humidityAbnormal.name'),
    category: _tc('ruleTemplate.category.humidity'),
    description: _tc('ruleTemplate.humidityAbnormal.description'),
    severity: 'warning',
    conditions: [{ point: 'humidity', operator: '>', threshold: 85, unit: '%' }],
    logic: 'AND',
    duration: 300,
  },
  {
    id: 'voltage-low',
    name: _tc('ruleTemplate.voltageLow.name'),
    category: _tc('ruleTemplate.category.electrical'),
    description: _tc('ruleTemplate.voltageLow.description'),
    severity: 'warning',
    conditions: [{ point: 'voltage', operator: '<', threshold: 200, unit: 'V' }],
    logic: 'AND',
    duration: 60,
  },
  {
    id: 'current-overload',
    name: _tc('ruleTemplate.currentOverload.name'),
    category: _tc('ruleTemplate.category.electrical'),
    description: _tc('ruleTemplate.currentOverload.description'),
    severity: 'critical',
    conditions: [{ point: 'current', operator: '>', threshold: 50, unit: 'A' }],
    logic: 'AND',
    duration: 30,
  },
  {
    id: 'level-high',
    name: _tc('ruleTemplate.levelHigh.name'),
    category: _tc('ruleTemplate.category.liquidLevel'),
    description: _tc('ruleTemplate.levelHigh.description'),
    severity: 'critical',
    conditions: [{ point: 'level', operator: '>', threshold: 90, unit: '%' }],
    logic: 'AND',
    duration: 60,
  },
  {
    id: 'device-offline',
    name: _tc('ruleTemplate.deviceOffline.name'),
    category: _tc('ruleTemplate.category.communication'),
    description: _tc('ruleTemplate.deviceOffline.description'),
    severity: 'warning',
    conditions: [{ point: 'last_seen', operator: '<', threshold: 0, unit: _tc('ruleTemplate.unit.secondsAgo') }],
    logic: 'AND',
    duration: 300,
  },
])

export const OPERATOR_OPTIONS = computed(() => [
  { label: _tc('ruleTemplate.operator.gt'), value: '>' },
  { label: _tc('ruleTemplate.operator.lt'), value: '<' },
  { label: _tc('ruleTemplate.operator.eq'), value: '==' },
  { label: _tc('ruleTemplate.operator.ne'), value: '!=' },
  { label: _tc('ruleTemplate.operator.gte'), value: '>=' },
  { label: _tc('ruleTemplate.operator.lte'), value: '<=' },
])

export const SEVERITY_OPTIONS = computed(() => [
  { label: _tc('ruleTemplate.severity.info'), value: 'info', color: SEMANTIC_COLORS.info },
  { label: _tc('ruleTemplate.severity.warning'), value: 'warning', color: SEMANTIC_COLORS.warning },
  { label: _tc('ruleTemplate.severity.critical'), value: 'critical', color: SEMANTIC_COLORS.critical },
])

export function getTemplatesByCategory(category: string): RuleTemplate[] {
  return RULE_TEMPLATES.value.filter(t => t.category === category)
}

export function getTemplateCategories(): string[] {
  return [...new Set(RULE_TEMPLATES.value.map(t => t.category))]
}
