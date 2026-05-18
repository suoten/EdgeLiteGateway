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

  me: () =>
    http.get<ApiResponse<{ user_id: string; username: string; role: string; must_change_password?: boolean }>>('/auth/me').then((r) => r.data.data),

  logout: (refreshToken?: string) =>
    // FIXED: 原问题-r.data.data ?? r.data返回类型不一致，改为统一r.data.data
    http.post<ApiResponse>('/auth/logout', refreshToken ? { refresh_token: refreshToken } : undefined).then((r) => r.data.data),

  changePassword: (oldPassword: string, newPassword: string) =>
    // FIXED: 返回值解包不一致，统一提取内层data
    http.post<ApiResponse>('/auth/change-password', { old_password: oldPassword, new_password: newPassword }).then((r) => r.data.data),
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

// FIXED: 原问题-前端类型约束宽松于后端Literal，导致可传入非法值触发422
export type DataType = 'bool' | 'int16' | 'int32' | 'uint16' | 'uint32' | 'float32' | 'float64' | 'string'
export type AccessMode = 'r' | 'w' | 'rw'
export type RuleOperator = '>' | '>=' | '<' | '<=' | '=='
export type RuleSeverity = 'critical' | 'warning' | 'info'
export type NotifyChannel = 'dingtalk' | 'email' | 'wechat' | 'webhook'
export type UserRole = 'admin' | 'operator' | 'viewer'
// FIXED: 原问题-PtzAction/RuleLogic类型未对齐后端Literal，前端可传入非法值触发422
export type PtzAction = 'up' | 'down' | 'left' | 'right' | 'up_left' | 'up_right' | 'down_left' | 'down_right' | 'zoom_in' | 'zoom_out' | 'focus_in' | 'focus_out' | 'stop'
export type RuleLogic = 'AND' | 'OR'

export interface PointDef {
  name: string
  data_type: DataType
  unit: string
  address: string
  access_mode: AccessMode
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
  list: (params?: { page?: number; size?: number; status?: string; protocol?: string; search?: string }) =>
    http.get<PagedData<Device>>('/devices', { params }).then((r) => r.data),

  get: (id: string) =>
    http.get<ApiResponse<Device>>(`/devices/${id}`).then((r) => r.data.data),

  create: (data: DeviceCreateParams) =>
    http.post<ApiResponse<Device>>('/devices', data).then((r) => r.data.data),

  update: (id: string, data: Omit<Partial<DeviceCreateParams>, 'device_id' | 'protocol'>) =>  // FIXED: 原问题-前端参数类型宽于后端，device_id/protocol不可修改
    http.put<ApiResponse<Device>>(`/devices/${id}`, data).then((r) => r.data.data),

  delete: (id: string) =>
    // FIXED: 原问题-r.data.data ?? r.data返回类型不一致，改为统一r.data.data
    http.delete<ApiResponse>(`/devices/${id}`).then((r) => r.data.data),

  getPoints: (id: string) =>
    http.get<ApiResponse<Record<string, any>>>(`/devices/${id}/points`).then((r) => r.data.data),

  writePoint: (id: string, point: string, value: number | boolean | string) =>  // FIXED: 原问题-value类型any过宽，后端限定float|int|bool|str
    // FIXED: 原问题-r.data.data ?? r.data返回类型不一致，改为统一r.data.data
    http.post<ApiResponse>(`/devices/${id}/points`, { point, value }).then((r) => r.data.data),

  createSimulator: (data: Omit<DeviceCreateParams, 'protocol' | 'config'>) =>  // FIXED: 原问题-前端参数类型宽于后端，simulator不需要config
    http.post<ApiResponse<Device>>('/devices/simulator', data).then((r) => r.data.data),

  discover: (params: { protocol: string; host?: string; port?: number }) =>
    http.post<ApiResponse<any[]>>('/devices/discover', { protocol: params.protocol, config: { host: params.host, port: params.port } }).then((r) => r.data.data),

  // FIXED: 后端有push路由但前端无对应API函数
  pushData: (deviceId: string, data: Record<string, any>, apiKey?: string) => {
    const headers: Record<string, string> = {}
    if (apiKey) headers['x-api-key'] = apiKey
    return http.post<ApiResponse>(`/devices/${deviceId}/push`, data, { headers }).then((r) => r.data.data)
  },
}

// ─── 规则 ───

export interface Rule {
  rule_id: string
  name: string
  device_id: string | null
  conditions: RuleCondition[]
  logic: RuleLogic
  duration: number
  severity: string
  enabled: boolean
  notify_channels: string[]
  created_at: string
}

export interface RuleCondition {
  point: string
  operator: RuleOperator
  threshold: number
}

export interface RuleCreateParams {
  name: string
  device_id: string
  conditions: RuleCondition[]
  logic?: RuleLogic
  duration?: number
  severity: RuleSeverity
  notify_channels?: NotifyChannel[]
}

export const ruleApi = {
  list: (params?: { page?: number; size?: number; device_id?: string; search?: string; severity?: string }) =>
    http.get<PagedData<Rule>>('/rules', { params }).then((r) => r.data),

  get: (id: string) =>
    http.get<ApiResponse<Rule>>(`/rules/${id}`).then((r) => r.data.data),

  create: (data: RuleCreateParams) =>
    http.post<ApiResponse<Rule>>('/rules', data).then((r) => r.data.data),

  update: (id: string, data: Omit<Partial<RuleCreateParams>, 'device_id'>) =>  // FIXED: 原问题-前端参数类型宽于后端，device_id不可修改
    http.put<ApiResponse<Rule>>(`/rules/${id}`, data).then((r) => r.data.data),

  delete: (id: string) =>
    // FIXED: 原问题-r.data.data ?? r.data返回类型不一致，改为统一r.data.data
    http.delete<ApiResponse>(`/rules/${id}`).then((r) => r.data.data),

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
  device_id: string | null
  severity: string
  status: string
  message: string
  trigger_value: Record<string, any>
  trigger_count: number
  fired_at: string
  acknowledged_at: string | null
  acknowledged_by: string | null
  recovered_at: string | null
}

export const alarmApi = {
  list: (params?: { page?: number; size?: number; status?: string; severity?: string; device_id?: string; search?: string }) =>
    http.get<PagedData<Alarm>>('/alarms', { params }).then((r) => r.data),

  get: (id: string) =>
    http.get<ApiResponse<Alarm>>(`/alarms/${id}`).then((r) => r.data.data),

  ack: (id: string) =>
    http.put<ApiResponse<Alarm>>(`/alarms/${id}/ack`).then((r) => r.data.data),
}

// ─── 数据查询 ───

export const dataApi = {
  query: (params: { device_id: string; point_name: string; start: string; stop?: string; aggregate?: string }) =>
    http.get<ApiResponse<any[]>>('/data/query', { params }).then((r) => r.data.data),

  export: (params: { device_id: string; point_name: string; start: string; stop?: string; format?: string }) =>
    http.get('/data/export', { params, responseType: 'blob' }),
}

// ─── 视频 ───

export interface VideoStreamInfo {
  url: string
  device_id: string
  channel_id: string
}

export const videoApi = {
  getStreamUrl: (deviceId: string, channelId?: string) =>
    http.get<ApiResponse<VideoStreamInfo>>(`/video/${deviceId}/stream`, { params: { channel_id: channelId || '1' } }).then((r) => r.data.data),

  ptzControl: (deviceId: string, action: PtzAction, channelId?: string) =>
    // FIXED: 原问题-r.data.data ?? r.data返回类型不一致，改为统一r.data.data
    http.post<ApiResponse>(`/video/${deviceId}/ptz`, null, { params: { action, channel_id: channelId || '1' } }).then((r) => r.data.data),

  // FIXED: 后端有video/webhook路由但前端无对应API函数
  webhook: (event: Record<string, any>, apiKey?: string) => {
    const headers: Record<string, string> = {}
    if (apiKey) headers['x-api-key'] = apiKey
    return http.post<ApiResponse>('/video/webhook', event, { headers }).then((r) => r.data.data)
  },
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
  rule_enabled: number
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
  enabled: boolean
  must_change_password: boolean
  created_at: string
  updated_at: string
}

export const userApi = {
  list: (params?: { page?: number; size?: number }) =>
    http.get<PagedData<User>>('/users', { params }).then((r) => r.data),

  create: (data: { username: string; password: string; role: UserRole }) =>
    http.post<ApiResponse<User>>('/users', data).then((r) => r.data.data),

  update: (id: string, data: { role?: UserRole; password?: string; enabled?: boolean }) =>
    http.put<ApiResponse<User>>(`/users/${id}`, data).then((r) => r.data.data),

  delete: (id: string) =>
    // FIXED: 原问题-r.data.data ?? r.data返回类型不一致，改为统一r.data.data
    http.delete<ApiResponse>(`/users/${id}`).then((r) => r.data.data),
}

// ─── 驱动配置 ───

export const driverApi = {
  list: () =>
    http.get<ApiResponse<{ drivers: any[]; total: number }>>('/drivers/list').then((r) => r.data.data),

  protocols: () =>
    http.get<ApiResponse<{ protocols: string[] }>>('/drivers/protocols').then((r) => r.data.data),

  status: () =>
    http.get<ApiResponse<any[]>>('/drivers').then((r) => r.data.data),

  configSchema: (driverName: string) =>
    http.get<ApiResponse<{ driver_name: string; config_schema: any }>>(`/drivers/${driverName}/config-schema`).then((r) => r.data.data),

  discover: (driverName: string, config?: Record<string, any>) =>
    http.post<ApiResponse<{ devices: any[] }>>(`/drivers/${driverName}/discover`, { config: config || {} }).then((r) => r.data.data),
}

// ─── 预处理配置 ───

export interface PreprocessGlobalConfig {
  enabled?: boolean
  default_deadband?: number
  default_filter_window?: number
  default_aggregate_window_sec?: number
}

export interface PreprocessUpdateParams {
  global?: PreprocessGlobalConfig
  points?: Record<string, Record<string, any>>
}

export const preprocessApi = {
  getConfig: () =>
    http.get<ApiResponse<any>>('/preprocess/config').then((r) => r.data.data),

  updateConfig: (data: PreprocessUpdateParams) =>
    // FIXED: 返回值解包不一致，统一提取内层data
    http.put<ApiResponse>('/preprocess/config', data).then((r) => r.data.data),
}

// ─── 串口透传 ───

export const serialBridgeApi = {
  getStatus: () =>
    http.get<ApiResponse<any>>('/serial-bridge/status').then((r) => r.data.data),

  start: () =>
    // FIXED: 原问题-r.data.data ?? r.data返回类型不一致，改为统一r.data.data
    http.post<ApiResponse>('/serial-bridge/start').then((r) => r.data.data),

  stop: () =>
    // FIXED: 原问题-r.data.data ?? r.data返回类型不一致，改为统一r.data.data
    http.post<ApiResponse>('/serial-bridge/stop').then((r) => r.data.data),

  // FIXED: 原问题-后端有PUT /serial-bridge/config路由但前端缺失对应API函数
  updateConfig: (data: Record<string, any>) =>
    http.put<ApiResponse>('/serial-bridge/config', data).then((r) => r.data.data),
}

// ─── 平台对接 ───

export const platformApi = {
  list: () =>
    http.get<ApiResponse<{ platforms: any[]; supported: any[] }>>('/platforms/list').then((r) => r.data.data),

  configSchema: (platformName: string) =>
    http.get<ApiResponse<{ platform_name: string; config_schema: any }>>(`/platforms/config-schema/${platformName}`).then((r) => r.data.data),

  connect: (platformName: string, config: Record<string, any>) =>
    http.post<ApiResponse<{ status: string }>>(`/platforms/connect/${platformName}`, { config }).then((r) => r.data.data),

  disconnect: (platformName: string) =>
    http.post<ApiResponse<{ status: string }>>(`/platforms/disconnect/${platformName}`).then((r) => r.data.data),

  status: (platformName: string) =>
    http.get<ApiResponse<{ connected: boolean; name: string; version: string }>>(`/platforms/status/${platformName}`).then((r) => r.data.data),
}

// ─── 审计日志 ───

export interface AuditLog {
  log_id: string
  username: string
  action: string
  resource_type: string
  resource_id: string
  ip_address: string
  status: string
  details: string
  created_at: string
}

export const auditApi = {
  list: (params?: { page?: number; size?: number; user_id?: string; action?: string; resource_type?: string; start_time?: string; end_time?: string }) =>
    http.get<ApiResponse<{ logs: any[]; total: number }>>('/audit/logs', { params }).then((r) => r.data.data),

  integrity: () =>
    http.get<ApiResponse<{ valid: boolean; total: number; broken_at: number[] }>>('/audit/integrity').then((r) => r.data.data),

  exportCsv: (params?: { start_time?: string; end_time?: string }) =>
    http.get<ApiResponse<{ content: string }>>('/audit/export/csv', { params }).then((r) => r.data.data),

  cleanup: (retentionDays: number = 90) =>
    http.post<ApiResponse<{ deleted: number }>>('/audit/cleanup', null, { params: { retention_days: retentionDays } }).then((r) => r.data.data),
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

// ─── 服务管理 ───

export interface ServiceDependency {
  package: string
  installed: boolean
  version: string
}

export interface ServiceRelatedFeature {
  name: string
  route: string
  hint: string
}

export interface ServiceInfo {
  name: string
  display_name: string
  description: string
  icon: string
  category: string
  state: 'disabled' | 'enabled' | 'running' | 'error' | 'installing'
  config_section: string
  dependencies: ServiceDependency[]
  use_cases: string[]
  related_features: ServiceRelatedFeature[]
  setup_guide: string[]
  config_schema: Record<string, any>
  current_config: Record<string, any>
  error_message: string
}

export const serviceApi = {
  list: () =>
    http.get<ApiResponse<{ services: ServiceInfo[] }>>('/services/list').then((r) => r.data.data),

  status: (serviceName: string) =>
    http.get<ApiResponse<ServiceInfo & { running_info?: Record<string, any> }>>(`/services/${serviceName}/status`).then((r) => r.data.data),

  enable: (serviceName: string, config?: Record<string, any>) =>
    // FIXED: 原问题-r.data.data ?? r.data返回类型不一致，改为统一r.data.data
    http.post<ApiResponse>(`/services/${serviceName}/enable`, { config }).then((r) => r.data.data),

  disable: (serviceName: string) =>
    // FIXED: 原问题-r.data.data ?? r.data返回类型不一致，改为统一r.data.data
    http.post<ApiResponse>(`/services/${serviceName}/disable`).then((r) => r.data.data),

  start: (serviceName: string) =>
    // FIXED: 原问题-r.data.data ?? r.data返回类型不一致，改为统一r.data.data
    http.post<ApiResponse>(`/services/${serviceName}/start`).then((r) => r.data.data),

  stop: (serviceName: string) =>
    // FIXED: 原问题-r.data.data ?? r.data返回类型不一致，改为统一r.data.data
    http.post<ApiResponse>(`/services/${serviceName}/stop`).then((r) => r.data.data),

  installDeps: (serviceName: string) =>
    // FIXED: 原问题-r.data.data ?? r.data返回类型不一致，改为统一r.data.data
    http.post<ApiResponse>(`/services/${serviceName}/install-deps`).then((r) => r.data.data),

  updateConfig: (serviceName: string, config: Record<string, any>) =>
    // FIXED: 原问题-r.data.data ?? r.data返回类型不一致，改为统一r.data.data
    http.put<ApiResponse>(`/services/${serviceName}/config`, { config }).then((r) => r.data.data),
}

// ─── MQTT Server ───

export interface MqttServerConfig {
  host?: string
  port?: number
  ws_port?: number
  username?: string
  password?: string
}

export const mqttServerApi = {
  status: () =>
    http.get<ApiResponse<any>>('/mqtt-server/status').then((r) => r.data.data),

  start: () =>
    // FIXED: 原问题-r.data.data ?? r.data返回类型不一致，改为统一r.data.data
    http.post<ApiResponse>('/mqtt-server/start').then((r) => r.data.data),

  stop: () =>
    // FIXED: 原问题-r.data.data ?? r.data返回类型不一致，改为统一r.data.data
    http.post<ApiResponse>('/mqtt-server/stop').then((r) => r.data.data),

  updateConfig: (data: MqttServerConfig) =>
    // FIXED: 原问题-r.data.data ?? r.data返回类型不一致，改为统一r.data.data
    http.put<ApiResponse>('/mqtt-server/config', data).then((r) => r.data.data),
}

// ─── Modbus Slave ───

export interface ModbusSlaveConfig {
  host?: string
  port?: number
  holding_size?: number
  input_size?: number
  coil_size?: number
  discrete_size?: number
}

export const modbusSlaveApi = {
  status: () =>
    http.get<ApiResponse<any>>('/modbus-slave/status').then((r) => r.data.data),

  start: () =>
    // FIXED: 原问题-r.data.data ?? r.data返回类型不一致，改为统一r.data.data
    http.post<ApiResponse>('/modbus-slave/start').then((r) => r.data.data),

  stop: () =>
    // FIXED: 原问题-r.data.data ?? r.data返回类型不一致，改为统一r.data.data
    http.post<ApiResponse>('/modbus-slave/stop').then((r) => r.data.data),

  updateConfig: (data: ModbusSlaveConfig) =>
    // FIXED: 原问题-r.data.data ?? r.data返回类型不一致，改为统一r.data.data
    http.put<ApiResponse>('/modbus-slave/config', data).then((r) => r.data.data),
}

// ─── MCP协议 ───

export interface McpCreateKeyParams {
  name: string
  scopes?: string[]
}

export const mcpApi = {
  tools: () =>
    http.get<ApiResponse<{ tools: any[] }>>('/mcp/tools').then((r) => r.data.data),

  callTool: (name: string, arguments_: Record<string, any>) =>
    http.post<ApiResponse>('/mcp/call', { name, arguments: arguments_ }).then((r) => r.data.data),

  resources: () =>
    http.get<ApiResponse<{ resources: any[] }>>('/mcp/resources').then((r) => r.data.data),

  prompts: () =>
    http.get<ApiResponse<{ prompts: any[] }>>('/mcp/prompts').then((r) => r.data.data),

  authKeys: () =>
    http.get<ApiResponse<{ keys: any[]; enabled: boolean }>>('/mcp/auth-keys').then((r) => r.data.data),

  createKey: (data: McpCreateKeyParams) =>
    // FIXED: 原问题-r.data.data ?? r.data返回类型不一致，改为统一r.data.data
    http.post<ApiResponse>('/mcp/auth-keys', data).then((r) => r.data.data),

  deleteKey: (keyId: string) =>
    // FIXED: 原问题-r.data.data ?? r.data返回类型不一致，改为统一r.data.data
    http.delete<ApiResponse>(`/mcp/auth-keys/${keyId}`).then((r) => r.data.data),

  // FIXED: 后端有mcp/sse路由但前端无对应API函数
  createSseConnection: (token?: string) => {
    const base = import.meta.env.VITE_API_BASE_URL || '/api/v1'
    const url = token ? `${base}/mcp/sse?token=${encodeURIComponent(token)}` : `${base}/mcp/sse`
    return new EventSource(url)
  },
}

// ─── OTA升级 ───

export const otaApi = {
  check: () =>
    http.get<ApiResponse<any>>('/ota/check').then((r) => r.data.data),

  apply: () =>
    // FIXED: 原问题-r.data.data ?? r.data返回类型不一致，改为统一r.data.data
    http.post<ApiResponse>('/ota/apply').then((r) => r.data.data),

  rollback: (version?: string) =>
    // FIXED: 原问题-r.data.data ?? r.data返回类型不一致，改为统一r.data.data
    http.post<ApiResponse>('/ota/rollback', null, { params: { version: version || '' } }).then((r) => r.data.data),

  backups: () =>
    http.get<ApiResponse<any>>('/ota/backups').then((r) => r.data.data),
}

// ─── Grafana集成 ───

export const grafanaApi = {
  config: () =>
    http.get<ApiResponse<any>>('/grafana/config').then((r) => r.data.data),

  dashboards: () =>
    http.get<ApiResponse<any>>('/grafana/dashboards').then((r) => r.data.data),

  embedUrl: (dashboardUid?: string) =>
    http.get<ApiResponse<{ url: string }>>('/grafana/embed-url', { params: { dashboard_uid: dashboardUid || '' } }).then((r) => r.data.data),
}

// ─── 联调集成 ───

export interface IntegrationHandshakeParams {
  cloud_url?: string
  protocol_version?: string
  device_id?: string
  [key: string]: any
}

export const integrationApi = {
  handshake: (data: IntegrationHandshakeParams) =>
    // FIXED: 原问题-r.data.data ?? r.data返回类型不一致，改为统一r.data.data
    http.post<ApiResponse>('/integration/handshake', data).then((r) => r.data.data),

  status: () =>
    http.get<ApiResponse<any>>('/integration/status').then((r) => r.data.data),
}

// ─── 组态管理 ───

export const scadaApi = {
  listProjects: () =>
    http.get<ApiResponse<any[]>>('/scada/projects').then((r) => r.data.data),

  getProject: (name: string) =>
    http.get<ApiResponse<any>>(`/scada/project/${encodeURIComponent(name)}`).then((r) => r.data.data),

  saveProject: (data: { name: string; widgets: any[] }) =>
    // FIXED: 原问题-r.data.data ?? r.data返回类型不一致，改为统一r.data.data
    http.post<ApiResponse>('/scada/project', data).then((r) => r.data.data),

  deleteProject: (name: string) =>
    // FIXED: 原问题-r.data.data ?? r.data返回类型不一致，改为统一r.data.data
    http.delete<ApiResponse>(`/scada/project/${encodeURIComponent(name)}`).then((r) => r.data.data),
}
