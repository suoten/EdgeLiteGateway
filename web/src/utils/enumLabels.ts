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
}

export const deviceStatusColor: Record<string, TagType> = {
  online: 'success',
  offline: 'error',
  unknown: 'default',
}

export const qualityLabel: Record<string, string> = {
  good: '良好',
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
