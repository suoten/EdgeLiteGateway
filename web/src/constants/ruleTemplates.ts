/**规则告警模板 — 常见工业场景的预置规则*/

import { t } from '@/i18n'

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
    name: t('ruleTemplate.tempHigh.name'),
    category: t('ruleTemplate.category.temperature'),
    description: t('ruleTemplate.tempHigh.description'),
    severity: 'critical',
    conditions: [{ point: 'temperature', operator: '>', threshold: 80, unit: '°C' }],
    logic: 'AND',
    duration: 60,
  },
  {
    id: 'temp-low',
    name: t('ruleTemplate.tempLow.name'),
    category: t('ruleTemplate.category.temperature'),
    description: t('ruleTemplate.tempLow.description'),
    severity: 'warning',
    conditions: [{ point: 'temperature', operator: '<', threshold: 5, unit: '°C' }],
    logic: 'AND',
    duration: 120,
  },
  {
    id: 'pressure-high',
    name: t('ruleTemplate.pressureHigh.name'),
    category: t('ruleTemplate.category.pressure'),
    description: t('ruleTemplate.pressureHigh.description'),
    severity: 'critical',
    conditions: [{ point: 'pressure', operator: '>', threshold: 1.0, unit: 'MPa' }],
    logic: 'AND',
    duration: 30,
  },
  {
    id: 'humidity-abnormal',
    name: t('ruleTemplate.humidityAbnormal.name'),
    category: t('ruleTemplate.category.humidity'),
    description: t('ruleTemplate.humidityAbnormal.description'),
    severity: 'warning',
    conditions: [{ point: 'humidity', operator: '>', threshold: 85, unit: '%' }],
    logic: 'AND',
    duration: 300,
  },
  {
    id: 'voltage-low',
    name: t('ruleTemplate.voltageLow.name'),
    category: t('ruleTemplate.category.electrical'),
    description: t('ruleTemplate.voltageLow.description'),
    severity: 'warning',
    conditions: [{ point: 'voltage', operator: '<', threshold: 200, unit: 'V' }],
    logic: 'AND',
    duration: 60,
  },
  {
    id: 'current-overload',
    name: t('ruleTemplate.currentOverload.name'),
    category: t('ruleTemplate.category.electrical'),
    description: t('ruleTemplate.currentOverload.description'),
    severity: 'critical',
    conditions: [{ point: 'current', operator: '>', threshold: 50, unit: 'A' }],
    logic: 'AND',
    duration: 30,
  },
  {
    id: 'level-high',
    name: t('ruleTemplate.levelHigh.name'),
    category: t('ruleTemplate.category.liquidLevel'),
    description: t('ruleTemplate.levelHigh.description'),
    severity: 'critical',
    conditions: [{ point: 'level', operator: '>', threshold: 90, unit: '%' }],
    logic: 'AND',
    duration: 60,
  },
  {
    id: 'device-offline',
    name: t('ruleTemplate.deviceOffline.name'),
    category: t('ruleTemplate.category.communication'),
    description: t('ruleTemplate.deviceOffline.description'),
    severity: 'warning',
    conditions: [{ point: 'last_seen', operator: '<', threshold: 0, unit: t('ruleTemplate.unit.secondsAgo') }],
    logic: 'AND',
    duration: 300,
  },
]

export const OPERATOR_OPTIONS = [
  { label: t('ruleTemplate.operator.gt'), value: '>' },
  { label: t('ruleTemplate.operator.lt'), value: '<' },
  { label: t('ruleTemplate.operator.eq'), value: '==' },
  { label: t('ruleTemplate.operator.ne'), value: '!=' },
  { label: t('ruleTemplate.operator.gte'), value: '>=' },
  { label: t('ruleTemplate.operator.lte'), value: '<=' },
]

export const SEVERITY_OPTIONS = [
  { label: t('ruleTemplate.severity.info'), value: 'info', color: '#909399' },
  { label: t('ruleTemplate.severity.warning'), value: 'warning', color: '#E6A23C' },
  { label: t('ruleTemplate.severity.critical'), value: 'critical', color: '#F56C6C' },
]

export function getTemplatesByCategory(category: string): RuleTemplate[] {
  return RULE_TEMPLATES.filter(t => t.category === category)
}

export function getTemplateCategories(): string[] {
  return [...new Set(RULE_TEMPLATES.map(t => t.category))]
}
