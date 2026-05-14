import { t } from '@/i18n'

type TagType = 'primary' | 'error' | 'warning' | 'info' | 'default' | 'success'

// FIXED: 原问题-中文标签硬编码，现使用i18n t()函数
export const severityLabel: Record<string, string> = {
  critical: t('severity.critical'),
  warning: t('severity.warning'),
  info: t('severity.info'),
}

export const severityColor: Record<string, TagType> = {
  critical: 'error',
  warning: 'warning',
  info: 'info',
}

export const channelLabel: Record<string, string> = {
  dingtalk: t('channel.dingtalk'),
  email: t('channel.email'),
  wechat: t('channel.wechat'),
  webhook: t('channel.webhook'),
}

export const deviceStatusLabel: Record<string, string> = {
  online: t('device.online'),
  offline: t('device.offline'),
  unknown: t('device.unknown'),
  error: t('device.error'),
}

export const deviceStatusColor: Record<string, TagType> = {
  online: 'success',
  offline: 'error',
  unknown: 'default',
  error: 'warning',
}

// FIXED: 原问题-qualityLabel中文硬编码，改为i18n
export const qualityLabel: Record<string, string> = {
  good: t('qualityLabel.good'),
  bad: t('qualityLabel.bad'),
  uncertain: t('qualityLabel.uncertain'),
  timeout: t('qualityLabel.timeout'),
}

export const qualityColor: Record<string, TagType> = {
  good: 'success',
  bad: 'error',
  uncertain: 'warning',
  timeout: 'warning',
}

export const alarmStatusLabel: Record<string, string> = {
  firing: t('alarm.firing'),
  acknowledged: t('alarm.acknowledged'),
  recovered: t('alarm.recovered'),
}

export const alarmStatusColor: Record<string, TagType> = {
  firing: 'error',
  acknowledged: 'warning',
  recovered: 'success',
}

// FIXED: 原问题-auditStatusLabel中文硬编码，改为i18n
export const auditStatusLabel: Record<string, string> = {
  success: t('common.success'),
  failed: t('common.failed'),
}

// FIXED: 原问题-auditActionLabel中文硬编码，改为i18n
export const auditActionLabel: Record<string, string> = {
  login: t('auditAction.login'),
  logout: t('auditAction.logout'),
  login_failed: t('auditAction.login_failed'),
  device_create: t('auditAction.device_create'),
  device_update: t('auditAction.device_update'),
  device_delete: t('auditAction.device_delete'),
  rule_create: t('auditAction.rule_create'),
  rule_update: t('auditAction.rule_update'),
  rule_delete: t('auditAction.rule_delete'),
  user_create: t('auditAction.user_create'),
  user_update: t('auditAction.user_update'),
  user_delete: t('auditAction.user_delete'),
  backup_create: t('auditAction.backup_create'),
  backup_restore: t('auditAction.backup_restore'),
  platform_connect: t('auditAction.platform_connect'),
  platform_disconnect: t('auditAction.platform_disconnect'),
  service_enable: t('auditAction.service_enable'),
  service_disable: t('auditAction.service_disable'),
  service_start: t('auditAction.service_start'),
  service_stop: t('auditAction.service_stop'),
  service_install_deps: t('auditAction.deps_install'),
  config_update: t('auditAction.config_update'),
  ota_check: t('auditAction.ota_check'),
  ota_apply: t('auditAction.ota_apply'),
  ota_rollback: t('auditAction.ota_rollback'),
  alarm_ack: t('auditAction.alarm_ack'),
}

export const roleLabel: Record<string, string> = {
  admin: t('role.admin'),
  operator: t('role.operator'),
  viewer: t('role.viewer'),
}

export const roleColor: Record<string, TagType> = {
  admin: 'error',
  operator: 'warning',
  viewer: 'info',
}

export const serviceStateLabel: Record<string, string> = {
  disabled: t('serviceState.disabled'),
  enabled: t('serviceState.enabled'),
  running: t('serviceState.running'),
  error: t('serviceState.error'),
  installing: t('serviceState.installing'),
}

export const serviceStateColor: Record<string, TagType> = {
  disabled: 'default',
  enabled: 'info',
  running: 'success',
  error: 'error',
  installing: 'warning',
}

// FIXED: 原问题-protocolLabel中文硬编码，改为i18n
export const protocolLabel: Record<string, string> = {
  modbus_tcp: 'Modbus TCP',
  modbus_rtu: 'Modbus RTU',
  opcua: 'OPC-UA',
  opc_da: 'OPC DA',
  mqtt: 'MQTT',
  mqtt_client: 'MQTT',
  http: 'HTTP',
  http_webhook: 'HTTP',
  simulator: t('protocolLabel.simulator'),
  video: t('protocolLabel.video'),
  s7: 'S7',
  siemens_s7: t('protocolLabel.siemens_s7'),
  mc: 'MC',
  mitsubishi_mc: t('protocolLabel.mitsubishi_mc'),
  fins: 'FINS',
  omron_fins: t('protocolLabel.omron_fins'),
  allen_bradley: 'AB',
  fanuc: 'FANUC',
  fanuc_cnc: 'FANUC CNC',
  mtconnect: 'MTConnect',
  toledo: 'Toledo',
  serial_port: t('protocolLabel.serial_port'),
  database_source: t('protocolLabel.database_source'),
  barcode_scanner: t('protocolLabel.barcode_scanner'),
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
  serial: t('protocolLabel.serial'),
  serial_modbus_rtu: t('protocolLabel.serial_modbus_rtu'),
  serial_raw: t('protocolLabel.serial_raw'),
  database: t('protocolLabel.database'),
  mysql: 'MySQL',
  postgresql: 'PostgreSQL',
  sqlite: 'SQLite',
  mssql: 'MSSQL',
  ab: 'AB CIP',
  ab_cip: 'AB CIP',
  ab_pccc: 'AB PCCC',
  dlt645_2007: 'DL/T 645-2007',
}

// FIXED: 原问题-otaStatusLabel中文硬编码，改为i18n
export const otaStatusLabel: Record<string, string> = {
  available: t('otaState.available'),
  downloading: t('otaState.downloading'),
  applying: t('otaState.applying'),
  rolled_back: t('otaState.rolled_back'),
  up_to_date: t('otaState.up_to_date'),
}
