type TagType = 'primary' | 'error' | 'warning' | 'info' | 'default' | 'success'

export const severityLabel: Record<string, string> = {
  critical: '严重',
  warning: '警告',
  info: '信息',
}

export const severityColor: Record<string, TagType> = {
  critical: 'error',
  warning: 'warning',
  info: 'info',
}

export const channelLabel: Record<string, string> = {
  dingtalk: '钉钉',
  email: '邮件',
  wechat: '企业微信',
  webhook: 'Webhook',
}

export const deviceStatusLabel: Record<string, string> = {
  online: '在线',
  offline: '离线',
  unknown: '未知',
  error: '异常',
}

export const deviceStatusColor: Record<string, TagType> = {
  online: 'success',
  offline: 'error',
  unknown: 'default',
  error: 'warning',
}

export const qualityLabel: Record<string, string> = {
  good: '良好',
  bad: '异常',
  uncertain: '不确定',
  timeout: '超时',
}

export const qualityColor: Record<string, TagType> = {
  good: 'success',
  bad: 'error',
  uncertain: 'warning',
  timeout: 'warning',
}

export const alarmStatusLabel: Record<string, string> = {
  firing: '触发中',
  acknowledged: '已确认',
  recovered: '已恢复',
}

export const alarmStatusColor: Record<string, TagType> = {
  firing: 'error',
  acknowledged: 'warning',
  recovered: 'success',
}

export const auditStatusLabel: Record<string, string> = {
  success: '成功',
  failed: '失败',
}

export const auditActionLabel: Record<string, string> = {
  login: '登录',
  logout: '登出',
  login_failed: '登录失败',
  device_create: '设备创建',
  device_update: '设备更新',
  device_delete: '设备删除',
  rule_create: '规则创建',
  rule_update: '规则更新',
  rule_delete: '规则删除',
  user_create: '用户创建',
  user_update: '用户更新',
  user_delete: '用户删除',
  backup_create: '备份创建',
  backup_restore: '备份恢复',
  platform_connect: '平台连接',
  platform_disconnect: '平台断开',
  service_enable: '服务启用',
  service_disable: '服务停用',
  service_start: '服务启动',
  service_stop: '服务停止',
  service_install_deps: '依赖安装',
  config_update: '配置更新',
  ota_check: 'OTA检查',
  ota_apply: 'OTA应用',
  ota_rollback: 'OTA回滚',
  alarm_ack: '告警确认',
}

export const roleLabel: Record<string, string> = {
  admin: '管理员',
  operator: '操作员',
  viewer: '观察者',
}

export const roleColor: Record<string, TagType> = {
  admin: 'error',
  operator: 'warning',
  viewer: 'info',
}

export const serviceStateLabel: Record<string, string> = {
  disabled: '未启用',
  enabled: '已启用',
  running: '运行中',
  error: '异常',
  installing: '安装中',
}

export const serviceStateColor: Record<string, TagType> = {
  disabled: 'default',
  enabled: 'info',
  running: 'success',
  error: 'error',
  installing: 'warning',
}

export const protocolLabel: Record<string, string> = {
  modbus_tcp: 'Modbus TCP',
  modbus_rtu: 'Modbus RTU',
  opcua: 'OPC-UA',
  opc_da: 'OPC DA',
  mqtt: 'MQTT',
  mqtt_client: 'MQTT',
  http: 'HTTP',
  http_webhook: 'HTTP',
  simulator: '模拟器',
  video: '视频',
  s7: 'S7',
  mc: 'MC',
  fins: 'FINS',
  allen_bradley: 'AB',
  fanuc: 'FANUC',
  mtconnect: 'MTConnect',
  toledo: 'Toledo',
  serial_port: '串口',
  database_source: '数据库',
  barcode_scanner: '扫码枪',
  sparkplug_b: 'Sparkplug B',
  dlt645: 'DL/T 645',
  iec104: 'IEC 104',
  kuka: 'KUKA',
  abb_robot: 'ABB',
  onvif: 'ONVIF',
}

export const otaStatusLabel: Record<string, string> = {
  available: '可更新',
  downloading: '下载中',
  applying: '应用中',
  rolled_back: '已回滚',
  up_to_date: '已是最新',
}
