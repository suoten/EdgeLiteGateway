import { computed } from 'vue'
import { t, useCurrentLocale } from '@/i18n'

type TagType = 'primary' | 'error' | 'warning' | 'info' | 'default' | 'success'

// FIXED-P3: 顶层t()调用→computed，语言切换时响应式更新
const _locale = useCurrentLocale()

function _tc(key: string) {
  void _locale.value
  return t(key)
}

export const severityLabel = computed<Record<string, string>>(() => ({
  critical: _tc('severity.critical'),
  warning: _tc('severity.warning'),
  info: _tc('severity.info'),
}))

export const severityColor: Record<string, TagType> = {
  critical: 'error',
  warning: 'warning',
  info: 'info',
}

export const channelLabel = computed<Record<string, string>>(() => ({
  dingtalk: _tc('channel.dingtalk'),
  email: _tc('channel.email'),
  wechat: _tc('channel.wechat'),
  webhook: _tc('channel.webhook'),
}))

export const deviceStatusLabel = computed<Record<string, string>>(() => ({
  online: _tc('device.online'),
  offline: _tc('device.offline'),
  unknown: _tc('device.unknown'),
  error: _tc('device.error'),
}))

export const deviceStatusColor: Record<string, TagType> = {
  online: 'success',
  offline: 'error',
  unknown: 'default',
  error: 'warning',
}

export const qualityLabel = computed<Record<string, string>>(() => ({
  good: _tc('qualityLabel.good'),
  bad: _tc('qualityLabel.bad'),
  uncertain: _tc('qualityLabel.uncertain'),
  timeout: _tc('qualityLabel.timeout'),
}))

export const qualityColor: Record<string, TagType> = {
  good: 'success',
  bad: 'error',
  uncertain: 'warning',
  timeout: 'warning',
}

export const alarmStatusLabel = computed<Record<string, string>>(() => ({
  firing: _tc('alarm.firing'),
  acknowledged: _tc('alarm.acknowledged'),
  recovered: _tc('alarm.recovered'),
}))

export const alarmStatusColor: Record<string, TagType> = {
  firing: 'error',
  acknowledged: 'warning',
  recovered: 'success',
}

export const auditStatusLabel = computed<Record<string, string>>(() => ({
  success: _tc('common.success'),
  failed: _tc('common.failed'),
}))

export const auditActionLabel = computed<Record<string, string>>(() => ({
  login: _tc('auditAction.login'),
  logout: _tc('auditAction.logout'),
  login_failed: _tc('auditAction.login_failed'),
  device_create: _tc('auditAction.device_create'),
  device_update: _tc('auditAction.device_update'),
  device_delete: _tc('auditAction.device_delete'),
  rule_create: _tc('auditAction.rule_create'),
  rule_update: _tc('auditAction.rule_update'),
  rule_delete: _tc('auditAction.rule_delete'),
  user_create: _tc('auditAction.user_create'),
  user_update: _tc('auditAction.user_update'),
  user_delete: _tc('auditAction.user_delete'),
  backup_create: _tc('auditAction.backup_create'),
  backup_restore: _tc('auditAction.backup_restore'),
  platform_connect: _tc('auditAction.platform_connect'),
  platform_disconnect: _tc('auditAction.platform_disconnect'),
  service_enable: _tc('auditAction.service_enable'),
  service_disable: _tc('auditAction.service_disable'),
  service_start: _tc('auditAction.service_start'),
  service_stop: _tc('auditAction.service_stop'),
  service_install_deps: _tc('auditAction.deps_install'),
  config_update: _tc('auditAction.config_update'),
  ota_check: _tc('auditAction.ota_check'),
  ota_apply: _tc('auditAction.ota_apply'),
  ota_rollback: _tc('auditAction.ota_rollback'),
  alarm_ack: _tc('auditAction.alarm_ack'),
}))

export const roleLabel = computed<Record<string, string>>(() => ({
  admin: _tc('role.admin'),
  operator: _tc('role.operator'),
  viewer: _tc('role.viewer'),
}))

export const roleColor: Record<string, TagType> = {
  admin: 'error',
  operator: 'warning',
  viewer: 'info',
}

export const serviceStateLabel = computed<Record<string, string>>(() => ({
  disabled: _tc('serviceState.disabled'),
  enabled: _tc('serviceState.enabled'),
  running: _tc('serviceState.running'),
  error: _tc('serviceState.error'),
  installing: _tc('serviceState.installing'),
}))

export const serviceStateColor: Record<string, TagType> = {
  disabled: 'default',
  enabled: 'info',
  running: 'success',
  error: 'error',
  installing: 'warning',
}

export const protocolLabel = computed<Record<string, string>>(() => ({
  modbus_tcp: 'Modbus TCP',
  modbus_rtu: 'Modbus RTU',
  opcua: 'OPC-UA',
  opc_da: 'OPC DA',
  mqtt: 'MQTT',
  mqtt_client: 'MQTT',
  http: 'HTTP',
  http_webhook: 'HTTP',
  simulator: _tc('protocolLabel.simulator'),
  video: _tc('protocolLabel.video'),
  s7: 'S7',
  siemens_s7: _tc('protocolLabel.siemens_s7'),
  mc: 'MC',
  mitsubishi_mc: _tc('protocolLabel.mitsubishi_mc'),
  fins: 'FINS',
  omron_fins: _tc('protocolLabel.omron_fins'),
  allen_bradley: 'AB',
  fanuc: 'FANUC',
  fanuc_cnc: 'FANUC CNC',
  mtconnect: 'MTConnect',
  toledo: 'Toledo',
  serial_port: _tc('protocolLabel.serial_port'),
  database_source: _tc('protocolLabel.database_source'),
  barcode_scanner: _tc('protocolLabel.barcode_scanner'),
  sparkplug_b: 'Sparkplug B',
  dlt645: 'DL/T 645',
  iec104: 'IEC 104',
  kuka: 'KUKA',
  kuka_ekrl: 'KUKA EKRL',
  abb_robot: 'ABB',
  abb_rws: 'ABB RWS',
  onvif: 'ONVIF',
  bacnet: 'BACnet',
  gb28181: 'GB28181',
  focas: 'FOCAS',
  webhook: 'Webhook',
  serial: _tc('protocolLabel.serial'),
  serial_modbus_rtu: _tc('protocolLabel.serial_modbus_rtu'),
  serial_raw: _tc('protocolLabel.serial_raw'),
  database: _tc('protocolLabel.database'),
  mysql: 'MySQL',
  postgresql: 'PostgreSQL',
  sqlite: 'SQLite',
  mssql: 'MSSQL',
  ab: 'AB CIP',
  ab_cip: 'AB CIP',
  ab_pccc: 'AB PCCC',
  dlt645_2007: 'DL/T 645-2007',
}))

export const otaStatusLabel = computed<Record<string, string>>(() => ({
  available: _tc('otaState.available'),
  downloading: _tc('otaState.downloading'),
  applying: _tc('otaState.applying'),
  rolled_back: _tc('otaState.rolled_back'),
  up_to_date: _tc('otaState.up_to_date'),
}))
