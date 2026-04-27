import http from './http'
import type { ApiResponse, PagedData } from './http'

// ─── 认证 ───

export interface LoginParams {
  username: string
  password: string
}

export interface TokenData {
  access_token: string
  refresh_token: string
  token_type: string
  expires_in: number
}

export const authApi = {
  login: (data: LoginParams) =>
    http.post<ApiResponse<TokenData>>('/auth/login', data).then((r) => r.data.data),

  refresh: (refreshToken: string) =>
    http.post<ApiResponse<TokenData>>('/auth/refresh', { refresh: refreshToken }).then((r) => r.data.data),
}

// ─── 设备 ───

export interface Device {
  device_id: string
  name: string
  protocol: string
  status: string
  config: Record<string, any>
  points: PointDef[]
  collect_interval: number
  created_at: string
  updated_at: string
}

export interface PointDef {
  name: string
  data_type: string
  unit: string
  address: string
  access_mode: string
  min?: number
  max?: number
  mode?: string
}

export interface DeviceCreateParams {
  device_id: string
  name: string
  protocol: string
  config?: Record<string, any>
  points: PointDef[]
  collect_interval?: number
}

export const deviceApi = {
  list: (params?: { page?: number; size?: number; status?: string; protocol?: string }) =>
    http.get<ApiResponse<PagedData<Device>>>('/devices', { params }).then((r) => r.data.data),

  get: (id: string) =>
    http.get<ApiResponse<Device>>(`/devices/${id}`).then((r) => r.data.data),

  create: (data: DeviceCreateParams) =>
    http.post<ApiResponse<Device>>('/devices', data).then((r) => r.data.data),

  update: (id: string, data: Partial<DeviceCreateParams>) =>
    http.put<ApiResponse<Device>>(`/devices/${id}`, data).then((r) => r.data.data),

  delete: (id: string) =>
    http.delete(`/devices/${id}`),

  getPoints: (id: string) =>
    http.get<ApiResponse<Record<string, any>>>(`/devices/${id}/points`).then((r) => r.data.data),

  writePoint: (id: string, point: string, value: any) =>
    http.post(`/devices/${id}/points`, { point, value }),

  createSimulator: (data: Omit<DeviceCreateParams, 'protocol'>) =>
    http.post<ApiResponse<Device>>('/devices/simulator', { ...data, protocol: 'simulator' }).then((r) => r.data.data),

  discover: (params: { protocol: string; host?: string; port?: number }) =>
    http.post<ApiResponse<any[]>>('/devices/discover', { protocol: params.protocol, config: { host: params.host, port: params.port } }).then((r) => r.data.data),
}

// ─── 规则 ───

export interface Rule {
  rule_id: string
  name: string
  device_id: string
  conditions: RuleCondition[]
  logic: string
  duration: number
  severity: string
  enabled: boolean
  notify_channels: string[]
  created_at: string
}

export interface RuleCondition {
  point: string
  operator: string
  threshold: number
}

export interface RuleCreateParams {
  name: string
  device_id: string
  conditions: RuleCondition[]
  logic?: string
  duration?: number
  severity: string
  notify_channels?: string[]
}

export const ruleApi = {
  list: (params?: { page?: number; size?: number; device_id?: string }) =>
    http.get<ApiResponse<PagedData<Rule>>>('/rules', { params }).then((r) => r.data.data),

  get: (id: string) =>
    http.get<ApiResponse<Rule>>(`/rules/${id}`).then((r) => r.data.data),

  create: (data: RuleCreateParams) =>
    http.post<ApiResponse<Rule>>('/rules', data).then((r) => r.data.data),

  update: (id: string, data: Partial<RuleCreateParams>) =>
    http.put<ApiResponse<Rule>>(`/rules/${id}`, data).then((r) => r.data.data),

  delete: (id: string) =>
    http.delete(`/rules/${id}`),

  enable: (id: string) =>
    http.post<ApiResponse<Rule>>(`/rules/${id}/enable`).then((r) => r.data.data),

  disable: (id: string) =>
    http.post<ApiResponse<Rule>>(`/rules/${id}/disable`).then((r) => r.data.data),

  test: (id: string, pointValues: Record<string, number>) =>
    http.post<ApiResponse<any>>(`/rules/${id}/test`, { point_values: pointValues }).then((r) => r.data.data),
}

// ─── 告警 ───

export interface Alarm {
  alarm_id: string
  rule_id: string
  device_id: string
  severity: string
  status: string
  trigger_value: Record<string, any>
  trigger_count: number
  fired_at: string
  acknowledged_at: string | null
  acknowledged_by: string | null
  recovered_at: string | null
}

export const alarmApi = {
  list: (params?: { page?: number; size?: number; status?: string; severity?: string }) =>
    http.get<ApiResponse<PagedData<Alarm>>>('/alarms', { params }).then((r) => r.data.data),

  get: (id: string) =>
    http.get<ApiResponse<Alarm>>(`/alarms/${id}`).then((r) => r.data.data),

  ack: (id: string) =>
    http.put<ApiResponse<Alarm>>(`/alarms/${id}/ack`).then((r) => r.data.data),
}

// ─── 数据查询 ───

export const dataApi = {
  query: (params: { device_id: string; point_name: string; start: string; stop?: string; aggregate?: string }) =>
    http.get<ApiResponse<any[]>>('/data/query', { params }).then((r) => r.data.data),

  export: (params: { device_id: string; point_name: string; start: string; format?: string }) =>
    http.get('/data/export', { params, responseType: 'blob' }),
}

// ─── 视频 ───

export const videoApi = {
  getStreamUrl: (deviceId: string, channelId?: string) =>
    http.get<ApiResponse<{ url: string }>>(`/video/${deviceId}/stream`, { params: { channel_id: channelId || '1' } }).then((r) => r.data.data),

  ptzControl: (deviceId: string, action: string, channelId?: string) =>
    http.post(`/video/${deviceId}/ptz`, { action, channel_id: channelId || '1' }),
}

// ─── 系统 ───

export interface SystemStatus {
  cpu_percent: number
  memory_total: number
  memory_used: number
  memory_percent: number
  disk_total: number
  disk_used: number
  disk_percent: number
  device_total: number
  device_online: number
  rule_total: number
  alarm_firing: number
  collect_task_count: number
  uptime: number
  version: string
}

export const systemApi = {
  getStatus: () =>
    http.get<ApiResponse<SystemStatus>>('/system/status').then((r) => r.data.data),

  listBackups: () =>
    http.get<ApiResponse<any[]>>('/system/backup').then((r) => r.data.data),

  createBackup: () =>
    http.post<ApiResponse<any>>('/system/backup').then((r) => r.data.data),

  restore: (backupId: string) =>
    http.post<ApiResponse<any>>('/system/restore', { backup_id: backupId }).then((r) => r.data.data),
}

// ─── 用户管理 ───

export interface User {
  user_id: string
  username: string
  role: string
  created_at: string
  updated_at: string
}

export const userApi = {
  list: () =>
    http.get<ApiResponse<User[]>>('/users').then((r) => r.data.data),

  create: (data: { username: string; password: string; role: string }) =>
    http.post<ApiResponse<User>>('/users', data).then((r) => r.data.data),

  update: (id: string, data: { role?: string; password?: string }) =>
    http.put<ApiResponse<User>>(`/users/${id}`, data).then((r) => r.data.data),

  delete: (id: string) =>
    http.delete(`/users/${id}`),
}

// ─── 审计日志 ───

export const auditApi = {
  list: (params?: { page?: number; size?: number; user_id?: string; action?: string; start_time?: string; end_time?: string }) =>
    http.get<ApiResponse<{ logs: any[]; total: number }>>('/audit/logs', { params }).then((r) => r.data.data),

  integrity: () =>
    http.get<ApiResponse<{ valid: boolean; total: number; broken_at: number[] }>>('/audit/integrity').then((r) => r.data.data),

  exportCsv: (params?: { start_time?: string; end_time?: string }) =>
    http.get<ApiResponse<{ content: string }>>('/audit/export/csv', { params }).then((r) => r.data.data),

  cleanup: (retentionDays: number = 90) =>
    http.post<ApiResponse<{ deleted: number }>>('/audit/cleanup', null, { params: { retention_days: retentionDays } }).then((r) => r.data.data),
}

// ─── 驱动配置 ───

export const driverApi = {
  list: () =>
    http.get<ApiResponse<{ drivers: any[]; total: number }>>('/drivers/list').then((r) => r.data.data),

  protocols: () =>
    http.get<ApiResponse<{ protocols: string[] }>>('/drivers/protocols').then((r) => r.data.data),

  configSchema: (driverName: string) =>
    http.get<ApiResponse<{ driver_name: string; schema: any }>>(`/drivers/${driverName}/config-schema`).then((r) => r.data.data),

  discover: (driverName: string, config?: Record<string, any>) =>
    http.post<ApiResponse<{ devices: any[] }>>(`/drivers/${driverName}/discover`, config || {}).then((r) => r.data.data),
}

// ─── 平台对接 ───

export const platformApi = {
  list: () =>
    http.get<ApiResponse<{ platforms: any[]; supported: any[] }>>('/platforms/list').then((r) => r.data.data),

  configSchema: (platformName: string) =>
    http.get<ApiResponse<{ platform_name: string; schema: any }>>(`/platforms/config-schema/${platformName}`).then((r) => r.data.data),

  connect: (platformName: string, config: Record<string, any>) =>
    http.post<ApiResponse<{ status: string }>>(`/platforms/connect/${platformName}`, config).then((r) => r.data.data),

  disconnect: (platformName: string) =>
    http.post<ApiResponse<{ status: string }>>(`/platforms/disconnect/${platformName}`).then((r) => r.data.data),

  status: (platformName: string) =>
    http.get<ApiResponse<{ connected: boolean; name: string; version: string }>>(`/platforms/status/${platformName}`).then((r) => r.data.data),
}

// ─── 计算表达式 ───

export const expressionApi = {
  evaluate: (expression: string, variables?: Record<string, any>) =>
    http.post<ApiResponse<{ expression: string; result: any }>>('/expressions/evaluate', { expression, variables }).then((r) => r.data.data),

  evaluateBatch: (expressions: Record<string, string>, variables?: Record<string, any>) =>
    http.post<ApiResponse<{ results: Record<string, any> }>>('/expressions/evaluate-batch', { expressions, variables }).then((r) => r.data.data),

  validate: (expression: string, variables?: Record<string, any>) =>
    http.post<ApiResponse<{ valid: boolean; expression: string; error?: string }>>('/expressions/validate', { expression, variables }).then((r) => r.data.data),

  functions: () =>
    http.get<ApiResponse<{ functions: any[]; operators: any[] }>>('/expressions/functions').then((r) => r.data.data),
}
