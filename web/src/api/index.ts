import http from './http'
import type { ApiResponse, PagedData } from './http'

// FIXED: 原问题-API返回结构不一致，list函数返回完整PagedData(含total/page/size)，get/create/update/delete函数返回r.data.data(仅业务对象)

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
  csrf_token?: string
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
  created_by: string | null
  created_at: string
  updated_at: string
  version: number
}

// FIXED: 原问题-前端类型约束宽松于后端Literal，导致可传入非法值触发422
export type DataType = 'bool' | 'int16' | 'int32' | 'uint16' | 'uint32' | 'float32' | 'float64' | 'string'
export type AccessMode = 'r' | 'w' | 'rw'
export type RuleOperator = '>' | '>=' | '<' | '<=' | '=='
export type RuleSeverity = 'critical' | 'major' | 'warning' | 'minor' | 'info'
export type NotifyChannel = 'dingtalk' | 'email' | 'wechat' | 'webhook'
export type UserRole = 'admin' | 'operator' | 'viewer'
// FIXED: 原问题-PtzAction/RuleLogic类型未对齐后端Literal，前端可传入非法值触发422
export type PtzAction = 'up' | 'down' | 'left' | 'right' | 'up_left' | 'up_right' | 'down_left' | 'down_right' | 'zoom_in' | 'zoom_out' | 'focus_in' | 'focus_out' | 'stop'
export type RuleLogic = 'AND' | 'OR' | 'NOT'

export interface PointDef {
  name: string
  data_type: DataType
  unit: string
  address: string
  access_mode: AccessMode
  min?: number
  max?: number
  mode?: string
  // FIXED-L2: 线性变换字段 value = raw * scale + offset (与后端 PointDef 对齐)
  scale?: number
  offset?: number
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
  list: (params?: { page?: number; size?: number; status?: string; protocol?: string; search?: string; collect_status?: string }) =>
    http.get<PagedData<Device>>('/devices', { params }).then((r) => r.data),

  get: (id: string) =>
    http.get<ApiResponse<Device>>(`/devices/${id}`).then((r) => r.data.data),

  create: (data: DeviceCreateParams) =>
    http.post<ApiResponse<Device>>('/devices', data).then((r) => r.data.data),

  update: (id: string, data: Omit<Partial<DeviceCreateParams>, 'device_id' | 'protocol'> & { force?: boolean }) =>  // FIXED: 原问题-前端参数类型宽于后端，device_id/protocol不可修改；SEC-FIX(修复3) 增加 force 绕过配置锁定
    http.put<ApiResponse<Device>>(`/devices/${id}`, data).then((r) => r.data.data),

  // SEC-FIX-V11: 写保护策略独立端点，需 DEVICE_WRITE_POLICY_EDIT 权限（仅 ADMIN）
  updateWritePolicy: (id: string, policy: { write_verify?: boolean; write_rate_limit?: number; write_audit?: boolean; write_whitelist?: string[] }) =>
    http.put<ApiResponse<Device>>(`/devices/${id}/write-policy`, policy).then((r) => r.data.data),

  delete: (id: string) =>
    // FIXED: 原问题-r.data.data ?? r.data返回类型不一致，改为统一r.data.data
    // FIXED-P0: 原问题-删除设备超时15s不够（后端需停止采集+驱动+清理sidecar），改为30s
    http.delete<ApiResponse>(`/devices/${id}`, { timeout: 30000 }).then((r) => r.data.data),

  getPoints: (id: string) =>
    http.get<ApiResponse<Record<string, any>>>(`/devices/${id}/points`).then((r) => r.data.data),

  writePoint: (id: string, point: string, value: number | boolean | string) =>  // FIXED: 原问题-value类型any过宽，后端限定float|int|bool|str
    // FIXED: 原问题-r.data.data ?? r.data返回类型不一致，改为统一r.data.data
    http.post<ApiResponse>(`/devices/${id}/points`, { point, value }).then((r) => r.data.data),

  createSimulator: (data: Omit<DeviceCreateParams, 'protocol' | 'config'>) =>  // FIXED: 原问题-前端参数类型宽于后端，simulator不需要config
    http.post<ApiResponse<Device>>('/devices/simulator', data).then((r) => r.data.data),

  discover: (params: { protocol: string; host?: string; port?: number }) =>
    http.post<ApiResponse<any[]>>('/devices/discover', { protocol: params.protocol, config: { host: params.host, port: params.port } }, { timeout: 60000 }).then((r) => r.data.data),

  // 修复1: 创建设备流程连通性测试（保存前预检）
  testConnection: (protocol: string, config: Record<string, any>) =>
    http.post<ApiResponse<{ success: boolean; supported: boolean; host?: string; port?: number; message: string }>>('/devices/test-connection', { protocol, config }, { timeout: 10000 }).then((r) => r.data.data),

  // FIXED: 后端有push路由但前端无对应API函数
  pushData: (deviceId: string, data: Record<string, any>, apiKey?: string) => {
    const headers: Record<string, string> = {}
    if (apiKey) headers['x-api-key'] = apiKey
    return http.post<ApiResponse>(`/devices/${deviceId}/push`, data, { headers }).then((r) => r.data.data)
  },

  // FIXED-P1: 后端存在collect-stats/device-quality-stats路由但前端无对应API函数
  collectStats: () =>
    http.get<ApiResponse<Record<string, any>>>('/devices/collect-stats').then((r) => r.data.data),

  deviceQualityStats: () =>
    http.get<ApiResponse<Record<string, any>>>('/devices/device-quality-stats').then((r) => r.data.data),

  getHealth: (id: string) =>
    http.get<ApiResponse<Record<string, any>>>(`/devices/${id}/health`).then((r) => r.data.data),

  resetHealth: (id: string) =>
    http.post<ApiResponse>(`/devices/${id}/health/reset`).then((r) => r.data.data),

  getHealthDetail: (id: string) =>
    http.get<ApiResponse<{ avg_latency_ms: number; error_trend: number[]; latency_samples: number[] }>>(`/devices/${id}/health`).then((r) => r.data.data),

  getOpsData: (id: string) =>
    http.get<ApiResponse<Record<string, any>>>(`/devices/${id}/ops`).then((r) => r.data.data),

  getPointHealth: (id: string) =>
    http.get<ApiResponse<Record<string, any>[]>>(`/devices/${id}/point-health`).then((r) => r.data.data),

  getWriteAudit: (id: string, limit = 100) =>
    http.get<ApiResponse<Record<string, any>[]>>(`/devices/${id}/write-audit`, { params: { limit } }).then((r) => r.data.data),

  listHealthAll: () =>
    // 适配1: 后端响应从 ApiResponse 变为 PagedResponse，data 为数组
    // 转换为 device_id → health 的映射以保持调用方（Dashboard.vue Object.entries）兼容
    http.get<PagedData<Record<string, any>>>('/devices/health/all').then((r) => {
      const arr = r.data?.data
      if (Array.isArray(arr)) {
        const map: Record<string, Record<string, any>> = {}
        arr.forEach((item: any) => {
          const id = item?.device_id
          if (id) map[id] = item
        })
        return map
      }
      // 兼容旧格式（对象）
      return (arr as unknown as Record<string, Record<string, any>>) || {}
    }),

  listHealth: (ids: string[]) =>
    http.get<ApiResponse<Record<string, Record<string, any>>>>('/devices/health', { params: { ids } }).then((r) => r.data.data),

  batchDeploy: (templateDeviceId: string, targetDeviceIds: string[], overrideConfig?: Record<string, any>) =>
    http.post<ApiResponse<{ success: string[]; failed: { device_id: string; error: string }[] }>>('/devices/batch-deploy', {
      template_device_id: templateDeviceId,
      target_device_ids: targetDeviceIds,
      override_config: overrideConfig,
    }).then((r) => r.data.data),

  batchStartCollect: (deviceIds: string[]) =>
    http.post<ApiResponse<{ success_count: number; failed: Record<string, string> }>>('/devices/batch/start-collect', { device_ids: deviceIds }).then((r) => r.data.data),  // FIXED-P1: failed类型改为Record对齐后端dict

  batchStopCollect: (deviceIds: string[]) =>
    http.post<ApiResponse<{ success_count: number; failed: Record<string, string> }>>('/devices/batch/stop-collect', { device_ids: deviceIds }).then((r) => r.data.data),  // FIXED-P1: failed类型改为Record对齐后端dict

  selfTest: (deviceId: string) =>
    http.post<ApiResponse<any>>('/self-test/run', { device_id: deviceId }).then((r) => r.data.data),

  acceptanceReport: (deviceId: string) =>
    http.post<ApiResponse<any>>('/self-test/acceptance-report', { device_id: deviceId }).then((r) => r.data.data),

  // FIXED-ATOMIC-IMPORT: 批量导入设备（支持事务模式）
  batchImport: (data: any[], overwrite: boolean = false, atomic: boolean = false) =>
    http.post<ApiResponse<{ success: number; failed: number; errors: string[]; mode: 'partial' | 'atomic' }>>('/devices/import', {
      data,
      overwrite,
      atomic,
    }).then((r) => r.data.data),
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
  // FIXED-L2: 与后端 RuleResponse.updated_at 对齐, 避免前端无法类型安全访问
  updated_at?: string | null
  created_by: string | null
  version: number
}

export interface RuleCondition {
  point: string
  operator: RuleOperator
  threshold: number
  type?: string
  model_id?: string | null
  ai_threshold?: number | null
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
  // SEC-FIX-RULE-VERSION: 规则版本历史端点
  listVersions: (id: string) =>
    http.get<ApiResponse<any[]>>(`/rules/${id}/versions`).then((r) => r.data.data),
  getVersion: (id: string, version: number) =>
    http.get<ApiResponse<any>>(`/rules/${id}/versions/${version}`).then((r) => r.data.data),
  rollbackVersion: (id: string, version: number) =>
    http.post<ApiResponse<any>>(`/rules/${id}/versions/rollback`, { version }).then((r) => r.data.data),
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
  rule_type: string
  fired_at: string
  acknowledged_at: string | null
  acknowledged_by: string | null
  recovered_at: string | null
  version: number
}

export const alarmApi = {
  list: (params?: { page?: number; size?: number; status?: string; severity?: string; device_id?: string; search?: string; rule_type?: string; sort_by?: string; sort_order?: string; since?: string }) =>
    http.get<PagedData<Alarm>>('/alarms', { params }).then((r) => r.data),

  get: (id: string) =>
    http.get<ApiResponse<Alarm>>(`/alarms/${id}`).then((r) => r.data.data),

  ack: (id: string, remark?: string) =>
    http.put<ApiResponse<Alarm>>(`/alarms/${id}/ack`, remark ? { remark } : undefined).then((r) => r.data.data),

  // FIXED: 批量确认告警，N 条报警一次 HTTP 请求（替代前端循环调用单条 ack）
  batchAck: (alarmIds: string[]) =>
    http.post<ApiResponse<{ succeeded: Alarm[]; failed: { alarm_id: string; reason: string }[]; success_count: number; failed_count: number }>>('/alarms/batch-ack', { alarm_ids: alarmIds }).then((r) => r.data.data),

  recover: (id: string) =>
    http.put<ApiResponse<Alarm>>(`/alarms/${id}/recover`).then((r) => r.data.data),

  statistics: (params?: { days?: number }) =>
    http.get<ApiResponse<Record<string, any>>>('/alarms/statistics', { params }).then((r) => r.data.data),

  // 告警趋势
  getTrend: (hours: number = 24) =>
    http.get<ApiResponse<any[]>>('/alarms/trend', { params: { hours } }).then((r) => r.data.data),

  // 告警抑制
  suppress: (alarmId: string, durationSeconds: number, reason: string) =>
    http.post<ApiResponse<{ alarm_id: string; suppressed: boolean; duration_seconds: number }>>(
      `/alarms/${alarmId}/suppress`,
      { duration_seconds: durationSeconds, reason, tag_match: {} }
    ).then((r) => r.data.data),

  // 告警关联
  getCorrelations: (params?: { limit?: number; offset?: number }) =>
    http.get<ApiResponse<{ groups: any[]; limit: number; offset: number }>>('/alarms/correlation', { params }).then((r) => r.data.data),

  // 修复9: 告警历史触发记录
  // 适配6: 后端返回 PagedResponse，类型由 ApiResponse<Alarm[]> 改为 PagedData<Alarm>
  // 调用方仅取 r.data.data（即 Alarm[]），功能正常
  getHistory: (ruleId: string, days: number = 7) =>
    http.get<PagedData<Alarm>>(`/alarms/history/${encodeURIComponent(ruleId)}`, { params: { days } }).then((r) => r.data.data),
}

// ─── 数据查询 ───

export const dataApi = {
  // aggregate 为降采样窗口大小（如 "10s"/"5m"/"1h"），agg_fn 为聚合函数名（默认 mean）
  query: (params: { device_id: string; point_name: string; start: string; stop?: string; aggregate?: string; agg_fn?: string; limit?: number }) =>
    // FIXED-P0: 历史曲线查询可能涉及大数据量，添加 30s 专用超时避免默认超时过短导致查询失败
    http.get<ApiResponse<any[]>>('/data/query', { params, timeout: 30000 }).then((r) => r.data.data),  // FIXED-P2: 时序数据查询添加分页限制

  export: (params: { device_id: string; point_name: string; start: string; stop?: string; format?: string }) =>
    http.get('/data/export', { params, responseType: 'blob' }),

  stats: (params?: { device_id?: string }) =>
    http.get<ApiResponse<Record<string, any>>>('/data/stats', { params }).then((r) => r.data.data),

  // Data analytics
  trend: (params: { device_id: string; point_name: string; start: string; stop?: string; bucket_size?: string }) =>
    http.get<ApiResponse<any>>('/data/trend', { params }).then((r) => r.data.data),

  correlation: (params: { device_id: string; point1: string; point2: string; start: string; stop?: string }) =>
    http.get<ApiResponse<any>>('/data/correlation', { params }).then((r) => r.data.data),

  statistics: (params: { device_id: string; point_name: string; start: string; stop?: string }) =>
    http.get<ApiResponse<any>>('/data/statistics', { params }).then((r) => r.data.data),

  multiPoint: (params: { device_id: string; point_names: string; start: string; stop?: string }) =>
    http.get<ApiResponse<any[]>>('/data/multi-point', { params }).then((r) => r.data.data),

  qualityTrend: (params: { days?: number }) =>
    http.get<ApiResponse<any>>('/data-quality/trend', { params: { hours: (params.days ?? 1) * 24 } }).then((r) => r.data.data),  // FIXED-P1: days转hours对齐后端参数

  // 触发数据降采样
  triggerDownsample: (params?: DownsampleRequest) =>
    http.post<ApiResponse<{ results: DownsampleResult[] }>>('/data/downsample', params).then(r => r.data.data),
}

// Downsample types
export interface DownsampleRequest {
  device_id?: string
  point_name?: string
}

export interface DownsampleResult {
  tier: string
  source_granularity: string
  target_granularity: string
  records_processed: number
  records_created: number
  device_id: string
  point_name: string
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

export interface ComponentHealth {
  name: string
  status: string
  message: string
  latency_ms: number
}

export interface HealthCheckResponse {
  status: string
  version: string
  uptime: number
  components: ComponentHealth[]
}

export interface PerformanceData {
  cpu_percent: number
  memory_percent: number
  memory_used_mb: number
  memory_total_mb: number
  disk_percent: number
  disk_used_gb: number
  disk_total_gb: number
  net_sent_mb: number
  net_recv_mb: number
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

  deleteBackup: (backupId: string) =>
    http.delete<ApiResponse<any>>(`/system/backup/${encodeURIComponent(backupId)}`).then((r) => r.data.data),

  getCascadeTopology: () =>
    http.get<ApiResponse<any>>('/system/cascade/topology').then((r) => r.data.data),

  getCascadeNeighbors: () =>
    http.get<ApiResponse<any[]>>('/system/cascade/neighbors').then((r) => r.data.data),

  updateCascadeConfig: (config: Record<string, any>) =>
    http.post<ApiResponse<any>>('/system/cascade/config', config).then((r) => r.data.data),

  removeCascadeNeighbor: (neighborId: string) =>
    http.delete<ApiResponse<any>>(`/system/cascade/neighbors/${encodeURIComponent(neighborId)}`).then((r) => r.data.data),

  getHealth: () =>
    http.get<ApiResponse<HealthCheckResponse>>('/system/health').then((r) => r.data.data),

  getReady: () =>
    http.get<ApiResponse<{ ready: boolean; message: string }>>('/system/ready').then((r) => r.data.data),

  getPerformance: () =>
    http.get<ApiResponse<PerformanceData>>('/system/performance').then((r) => r.data.data),

  getRetentionPolicy: () =>
    http.get<ApiResponse<any>>('/system/retention').then((r) => r.data.data),

  updateRetentionPolicy: (retentionPeriod: string) =>
    http.put<ApiResponse<any>>('/system/retention', { retention_period: retentionPeriod }).then((r) => r.data.data),

  getCertInfo: () =>
    http.get<ApiResponse<any>>('/system/cert').then((r) => r.data.data),

  rotateCert: () =>
    http.post<ApiResponse<any>>('/system/cert/rotate').then((r) => r.data.data),

  getNtpConfig: () =>
    http.get<ApiResponse<{ enabled: boolean; server: string; sync_status: string; current_time: string }>>('/system/ntp').then((r) => r.data.data),

  updateNtpConfig: (config: { enabled?: boolean; server?: string }) =>
    http.put<ApiResponse<any>>('/system/ntp', config).then((r) => r.data.data),

  getResources: () =>
    http.get<ApiResponse<{ cpu: Record<string, any>; memory: Record<string, any>; disk: Record<string, any>; network: Record<string, any> }>>('/system/resources').then((r) => r.data.data),

  // 备份调度
  getBackupSchedule: () =>
    http.get<ApiResponse<{
      enabled: boolean;
      interval_seconds: number;
      retain_days: number;
      is_running: boolean;
      last_backup_time: number | null;
      last_backup_duration_ms: number | null;
      backup_count: number;
      total_backup_size_bytes: number;
      backups: any[];
    }>>('/system/backup/schedule').then((r) => r.data.data),

  triggerBackup: () =>
    http.post<ApiResponse<{ results: any[]; triggered_at: number }>>('/system/backup/schedule/trigger').then((r) => r.data.data),

  // [AUDIT-FIX] G-08: 新增 updateBackupSchedule 接口，支持编辑调度配置
  updateBackupSchedule: (config: { enabled?: boolean; interval_seconds?: number; retain_days?: number }) =>
    http.put<ApiResponse<any>>('/system/backup/schedule', config).then((r) => r.data.data),

  // 系统配置热重载
  getConfig: () =>
    http.get<ApiResponse<any>>('/system/config').then((r) => r.data.data),

  reloadConfig: () =>
    http.post<ApiResponse<{ changed_sensitive_keys: string[]; message: string }>>('/system/config/reload').then((r) => r.data.data),

  // 更新指定配置节（用于未注册为"服务"的配置节，如 mqtt、scheduler 等）
  updateConfigSection: (section: string, config: Record<string, any>) =>
    http.put<ApiResponse>(`/system/config/${section}`, { config }).then((r) => r.data.data),

  // 获取网络配置信息（只读）
  getNetwork: () =>
    http.get<ApiResponse<{ hostname: string; local_ip: string; interfaces: Array<{ name: string; family: string; address: string; netmask: string; broadcast: string; isup: boolean }> }>>('/system/network').then((r) => r.data.data),
}

// 数据库监控 API
export const dbMonitorApi = {
  getPoolStats: () =>
    http.get<ApiResponse<{
      pool_name: string;
      pool_size: number;
      min_size: number;
      max_size: number;
      checked_out: number;
      overflow: number;
      checked_out_timeout_mins: number;
    }>>('/db-monitor/pool-stats').then((r) => r.data.data),

  getSlowQueries: (limit: number = 20) =>
    http.get<ApiResponse<{ queries: any[]; count: number }>>('/db-monitor/slow-queries', { params: { limit } }).then((r) => r.data.data),
}

// ─── 用户管理 ───

export interface User {
  user_id: string
  username: string
  role: string
  enabled: boolean
  must_change_password: boolean
  password_changed_at: string | null
  created_at: string
  updated_at: string
  version: number
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
    http.post<ApiResponse<{ devices: any[] }>>(`/drivers/${driverName}/discover`, { config: config || {} }, { timeout: 60000 }).then((r) => r.data.data),

  loadStatus: () =>
    http.get<ApiResponse<{ drivers: Record<string, { loaded: boolean; error: string | null; module: string; class: string }>; loaded_count: number; skipped_count: number }>>('/drivers/load-status').then((r) => r.data.data),

  meta: () =>
    http.get<ApiResponse<any>>('/drivers/meta').then((r) => r.data.data),

  opcuaBrowse: (deviceId: string, nodeId?: string) =>
    http.post<ApiResponse<any[]>>('/drivers/opcua/browse', { device_id: deviceId, node_id: nodeId }).then((r) => r.data.data),  // FIXED-P1: GET→POST, endpoint→device_id，与后端OpcUaBrowseRequest对齐

  opcDaServers: (host: string) =>
    http.get<ApiResponse<{ servers: string[]; host: string }>>('/drivers/opc-da/servers', { params: { host } }).then((r) => r.data.data),  // FIXED-P1: 返回类型与后端ApiResponse(data={servers,host})对齐
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

  testConnection: (platformName: string, config: Record<string, any>) =>
    http.post<ApiResponse<any>>(`/platforms/test-connection/${platformName}`, { config }).then((r) => r.data.data),

  dashboard: () =>
    http.get<ApiResponse<any[]>>('/platforms/dashboard').then((r) => r.data.data),

  metrics: () =>
    http.get<string>('/platforms/metrics', { responseType: 'text' }).then((r) => r.data),

  reload: (platformName: string, config: Record<string, any>) =>
    http.post<ApiResponse<{ status: string; connected: boolean }>>(`/platforms/reload/${platformName}`, { config }).then((r) => r.data.data),

  messagePreview: (platformName: string) =>
    http.get<ApiResponse<any[]>>(`/platforms/message-preview/${platformName}`).then((r) => r.data.data),

  brokerQuality: (platformName: string) =>
    http.get<ApiResponse<{ avg_latency_ms: number; max_latency_ms: number; min_latency_ms: number; packet_loss_count: number; samples: number }>>(`/platforms/broker-quality/${platformName}`).then((r) => r.data.data),

  validateTopic: (template: string) =>
    http.post<ApiResponse<{ valid: boolean; errors: string[]; variables: string[] }>>('/platforms/validate-topic', { template }).then((r) => r.data.data),

  tbDevices: (platformName: string) =>
    http.get<ApiResponse<any[]>>(`/platforms/tb/devices/${platformName}`).then((r) => r.data.data),

  tbRpcLogs: (platformName: string) =>
    http.get<ApiResponse<any[]>>(`/platforms/tb/rpc-logs/${platformName}`).then((r) => r.data.data),

  tbAlarms: (platformName: string) =>
    http.get<ApiResponse<any[]>>(`/platforms/tb/alarms/${platformName}`).then((r) => r.data.data),

  tbSyncStatus: (platformName: string) =>
    http.get<ApiResponse<any>>(`/platforms/tb/sync-status/${platformName}`).then((r) => r.data.data),

  platformShadow: (platformName: string) =>
    http.get<ApiResponse<any>>(`/platforms/shadow/${platformName}`).then((r) => r.data.data),

  platformCommandLogs: (platformName: string) =>
    http.get<ApiResponse<any[]>>(`/platforms/command-logs/${platformName}`).then((r) => r.data.data),

  platformAlarmRecords: (platformName: string) =>
    http.get<ApiResponse<any[]>>(`/platforms/alarm-records/${platformName}`).then((r) => r.data.data),

  platformDeviceMapping: (platformName: string) =>
    http.get<ApiResponse<any[]>>(`/platforms/device-mapping/${platformName}`).then((r) => r.data.data),

  exportConfig: (platformName: string) =>
    http.get<ApiResponse<any>>(`/platforms/export/${platformName}`).then((r) => r.data.data),

  importConfig: (platformName: string, configData: Record<string, any>) =>
    http.post<ApiResponse<any>>(`/platforms/import/${platformName}`, { config_data: configData }).then((r) => r.data.data),

  brokerStatus: (platformName: string) =>
    http.get<ApiResponse<any[]>>(`/platforms/broker-status/${platformName}`).then((r) => r.data.data),

  validateAdvancedTemplate: (template: string, templateType: string = 'payload') =>
    http.post<ApiResponse<{ valid: boolean; errors: string[]; variables: string[]; template_type: string }>>('/platforms/validate-advanced-template', { template, template_type: templateType }).then((r) => r.data.data),

  previewTemplate: (template: string, testData: Record<string, any>, templateType: string = 'payload') =>
    http.post<ApiResponse<{ success: boolean; result?: string; error?: string }>>('/platforms/preview-template', { template, test_data: testData, template_type: templateType }).then((r) => r.data.data),

  validateScript: (script: string) =>
    http.post<ApiResponse<{ valid: boolean; errors: string[] }>>('/platforms/validate-script', { script }).then((r) => r.data.data),

  testScript: (script: string, testPayload: Record<string, any>, testContext?: Record<string, any>) =>
    http.post<ApiResponse<{ success: boolean; data?: any; error?: string; execution_ms: number }>>('/platforms/test-script', { script, test_payload: testPayload, test_context: testContext }).then((r) => r.data.data),

  mqttTestPublish: (platformName: string, topic: string, payload: string, qos: number = 0) =>
    http.post<ApiResponse<{ success: boolean; message?: string; error?: string }>>(`/platforms/mqtt-test-publish/${platformName}`, { topic, payload, qos }).then((r) => r.data.data),
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

  // FIXED-Severe: SSE Token URL 泄露 — 用 fetch+ReadableStream 替代 EventSource
  // EventSource 不支持自定义 Header，原实现将 token 放在 URL ?token=xxx 中，
  // 会泄露到访问日志、浏览器历史、Referer。改用 fetch + credentials:'include' 通过 HttpOnly Cookie 认证。
  createSseConnection: (options: {
    onMessage?: (data: any) => void
    onError?: (err: Error) => void
    signal?: AbortSignal
  } = {}): Promise<AbortController> => {
    const base = import.meta.env.VITE_API_BASE_URL || '/api/v1'
    const url = `${base}/mcp/sse`
    const onMessage = options.onMessage || (() => {})
    const onError = options.onError
    const externalSignal = options.signal
    const controller = new AbortController()
    const signal = externalSignal || controller.signal

    // 检测 ReadableStream 支持，不支持则回退到 EventSource
    const canUseFetchSse = typeof fetch !== 'undefined' && typeof ReadableStream !== 'undefined'

    if (!canUseFetchSse) {
      console.warn('[SSE] ReadableStream not supported, falling back to EventSource')
      const fallback = new EventSource(url, { withCredentials: true })
      if (onError) fallback.onerror = () => onError(new Error('SSE connection error'))
      fallback.onmessage = (e) => {
        const data = e.data
        if (!data) return
        try {
          onMessage(JSON.parse(data))
        } catch {
          onMessage(data)
        }
      }
      // 用 AbortController 包装 EventSource，保持返回类型一致
      signal.addEventListener('abort', () => fallback.close())
      return Promise.resolve(controller)
    }

    return (async () => {
      try {
        const response = await fetch(url, {
          method: 'GET',
          headers: {
            'Accept': 'text/event-stream',
            'Cache-Control': 'no-cache',
            // Token 通过 HttpOnly Cookie 传递，不再泄露到 URL
          },
          credentials: 'include',  // 携带 HttpOnly Cookie
          signal,
        })

        if (!response.ok) {
          throw new Error(`SSE connection failed: ${response.status}`)
        }

        const reader = response.body?.getReader()
        if (!reader) {
          throw new Error('No response body')
        }

        const decoder = new TextDecoder()
        let buffer = ''

        // 异步读取流
        ;(async () => {
          try {
            while (true) {
              const { done, value } = await reader.read()
              if (done) break
              buffer += decoder.decode(value, { stream: true })
              // 按 SSE 协议解析（双换行分隔事件）
              const lines = buffer.split('\n')
              buffer = lines.pop() || ''  // 保留不完整的行
              for (const line of lines) {
                if (line.startsWith('data:')) {
                  const data = line.slice(5).trim()
                  if (data) {
                    try {
                      onMessage(JSON.parse(data))
                    } catch {
                      onMessage(data)
                    }
                  }
                }
              }
            }
          } catch (err) {
            if (!signal.aborted) {
              onError?.(err as Error)
            }
          }
        })()

        return controller
      } catch (err) {
        onError?.(err as Error)
        throw err
      }
    })()
  },
}

// ─── App Update (EdgeLite self-upgrade) ───
// NOTE: For industrial device firmware OTA, see firmwareApi above

export const appUpdateApi = {
  check: () =>
    http.get<ApiResponse<any>>('/app-update/check').then((r) => r.data.data),

  apply: () =>
    http.post<ApiResponse>('/app-update/apply').then((r) => r.data.data),

  rollback: (version?: string) =>
    http.post<ApiResponse>('/app-update/rollback', null, { params: version ? { version } : undefined }).then((r) => r.data.data),

  backups: () =>
    http.get<ApiResponse<any>>('/app-update/backups').then((r) => r.data.data),

  status: () =>
    http.get<ApiResponse<any>>('/app-update/status').then((r) => r.data.data),

  cancel: () =>
    http.post<ApiResponse>('/app-update/cancel').then((r) => r.data.data),
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

  rpcExecute: (data: { method: string; device_id: string; params: Record<string, any>; timeout: number }) =>
    http.post<ApiResponse<any>>('/integration/rpc/execute', data).then((r) => r.data.data),

  rpcHistory: (limit = 50) =>
    http.get<ApiResponse<any[]>>('/integration/rpc/history', { params: { limit } }).then((r) => r.data.data),
}

// ─── 组态管理 ───

export const scadaApi = {
  listProjects: () =>
    http.get<ApiResponse<any[]>>('/scada/projects').then((r) => r.data.data),

  getProject: (name: string) =>
    http.get<ApiResponse<any>>(`/scada/project/${encodeURIComponent(name)}`).then((r) => r.data.data),

  saveProject: (data: { name: string; widgets: any[]; scenes?: any[] }) =>
    // FIXED: 原问题-r.data.data ?? r.data返回类型不一致，改为统一r.data.data
    http.post<ApiResponse>('/scada/project', data).then((r) => r.data.data),

  deleteProject: (name: string) =>
    // FIXED: 原问题-r.data.data ?? r.data返回类型不一致，改为统一r.data.data
    http.delete<ApiResponse>(`/scada/project/${encodeURIComponent(name)}`).then((r) => r.data.data),
}

// ─── AI推理引擎 ───

export const aiApi = {
  listModels: (page = 1, pageSize = 20) =>
    http.get<PagedData<any>>('/ai/models', { params: { page, size: pageSize } }).then(r => r.data),
  getModel: (id: string) =>
    http.get<ApiResponse<any>>(`/ai/models/${id}`).then(r => r.data.data),
  enableModel: (id: string) =>
    http.post<ApiResponse>(`/ai/models/${id}/enable`).then(r => r.data.data),
  disableModel: (id: string) =>
    http.post<ApiResponse>(`/ai/models/${id}/disable`).then(r => r.data.data),
  reloadModel: (id: string, modelFilePath: string) =>
    http.post<ApiResponse>(`/ai/models/${id}/reload`, { model_file_path: modelFilePath }).then(r => r.data.data),
  inference: (modelId: string, inputData: number[], deviceId?: string, pointName?: string) =>
    http.post<ApiResponse<any>>('/ai/inference', { model_id: modelId, input_data: inputData, device_id: deviceId, point_name: pointName }).then(r => r.data.data),
  getStats: () =>
    http.get<ApiResponse<any>>('/ai/stats').then(r => r.data.data),
  getSummary: () =>
    http.get<ApiResponse<any>>('/ai/summary').then(r => r.data.data),
  getModelStats: (id: string) =>
    http.get<ApiResponse<any>>(`/ai/models/${id}/stats`).then(r => r.data.data),
  getInferenceLogs: (modelId?: string, page = 1, pageSize = 20) =>
    http.get<PagedData<any>>('/ai/inference/logs', { params: { model_id: modelId, page, size: pageSize } }).then(r => r.data),
  updateModel: (id: string, data: any) =>
    http.put<ApiResponse<any>>(`/ai/models/${id}`, data).then(r => r.data.data),
  deleteModel: (id: string) =>
    http.delete<ApiResponse>(`/ai/models/${id}`).then(r => r.data.data),
  // Scheduled inference
  startSchedule: (id: string, interval: number, inputWindowSize?: number) =>
    http.post<ApiResponse>(`/ai/models/${id}/schedule`, { interval, input_window_size: inputWindowSize }).then(r => r.data.data),
  stopSchedule: (id: string) =>
    http.delete<ApiResponse>(`/ai/models/${id}/schedule`).then(r => r.data.data),
  listSchedules: () =>
    http.get<ApiResponse<any[]>>('/ai/schedules').then(r => r.data.data),
  // Model upload
  uploadModel: (file: File, name?: string) => {
    const formData = new FormData()
    formData.append('file', file)
    if (name) formData.append('name', name)
    return http.post<ApiResponse<any>>('/ai/models/upload', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
    }).then(r => r.data.data)
  },
  getModelVersions: (id: string) =>
    http.get<ApiResponse<any[]>>(`/ai/models/${id}/versions`).then(r => r.data.data),
  rollbackModel: (id: string, targetVersion: string) =>
    http.post<ApiResponse<any>>(`/ai/models/${id}/rollback`, { target_version: targetVersion }).then(r => r.data.data),
}

// ─── AI增强功能 ───

export const aiEnhancedApi = {
  createAbTest: (data: { model_id: string; variant_a_version: string; variant_b_version: string; traffic_split_b?: number }) =>
    http.post<ApiResponse<any>>('/ai/ab-test', data).then(r => r.data.data),
  getAbTest: (modelId: string) =>
    http.get<ApiResponse<any>>(`/ai/ab-test/${modelId}`).then(r => r.data.data),
  updateAbTestSplit: (modelId: string, trafficSplitB: number) =>
    http.put<ApiResponse<any>>(`/ai/ab-test/${modelId}/split`, { traffic_split_b: trafficSplitB }).then(r => r.data.data),  // FIXED-P1: 路由对齐后端/ai/ab-test/{model_id}/split
  deleteAbTest: (modelId: string) =>
    http.delete<ApiResponse>(`/ai/ab-test/${modelId}`).then(r => r.data.data),
  promoteCanary: (modelId: string) =>
    http.post<ApiResponse<any>>(`/ai/ab-test/${modelId}/promote`).then(r => r.data.data),
  rollbackCanary: (modelId: string) =>
    http.post<ApiResponse<any>>(`/ai/ab-test/${modelId}/rollback`).then(r => r.data.data),
  hotSwap: (data: { model_id: string; new_version: string; new_model_path: string; warmup_target?: number }) =>
    http.post<ApiResponse<any>>('/ai/hot-swap', data).then(r => r.data.data),
  getHotSwapStatus: (modelId: string) =>
    http.get<ApiResponse<any>>(`/ai/hot-swap/${modelId}`).then(r => r.data.data),
  updatePreprocess: (modelId: string, config: Record<string, any>[]) =>
    http.put<ApiResponse<any>>(`/ai/models/${modelId}/preprocess`, { preprocess_config: config }).then(r => r.data.data),
  updatePostprocess: (modelId: string, config: Record<string, any>[]) =>
    http.put<ApiResponse<any>>(`/ai/models/${modelId}/postprocess`, { postprocess_config: config }).then(r => r.data.data),
  getPreprocessSteps: () =>
    http.get<ApiResponse<any>>('/ai/preprocess/steps').then(r => r.data.data),
  getPostprocessSteps: () =>
    http.get<ApiResponse<any>>('/ai/postprocess/steps').then(r => r.data.data),
  getCacheStats: () =>
    http.get<ApiResponse<any>>('/ai/cache/stats').then(r => r.data.data),
  clearCache: () =>
    http.post<ApiResponse>('/ai/cache/clear').then(r => r.data.data),
  getResources: () =>
    http.get<ApiResponse<any>>('/ai/resources').then(r => r.data.data),
  getLatencyDistribution: (modelId: string) =>
    http.get<ApiResponse<any>>(`/ai/latency/${modelId}`).then(r => r.data.data),
  detectDevices: () =>
    http.get<ApiResponse<any>>('/ai/devices').then(r => r.data.data),
  getBatchStats: () =>
    http.get<ApiResponse<any>>('/ai/batch/stats').then(r => r.data.data),
}

// ─── AI自学习引擎 ───

export const anomalyLearnerApi = {
  initialize: (data: { model_id?: string; device_type?: string; device_params?: Record<string, any>; anomaly_threshold?: number; initial_data?: number[] }) =>
    http.post<ApiResponse<any>>('/anomaly-learner/initialize', data).then(r => r.data.data),
  infer: (data: { model_id?: string; input_window: number[] }) =>
    http.post<ApiResponse<any>>('/anomaly-learner/infer', data).then(r => r.data.data),
  feedback: (data: { model_id?: string; value: number; score: number; is_anomaly: boolean; feedback: string }) =>
    http.post<ApiResponse<any>>('/anomaly-learner/feedback', data).then(r => r.data.data),
  dashboard: (modelId?: string) =>
    http.get<ApiResponse<any>>('/anomaly-learner/dashboard', { params: { model_id: modelId } }).then(r => r.data.data),
  status: () =>
    http.get<ApiResponse<any>>('/anomaly-learner/status').then(r => r.data.data),
}

export const trendLearnerApi = {
  initialize: (data: { model_id?: string; device_type?: string; device_params?: Record<string, any>; initial_data?: number[] }) =>
    http.post<ApiResponse<any>>('/trend-learner/initialize', data).then(r => r.data.data),
  predict: (data: { model_id?: string; input_window: number[] }) =>
    http.post<ApiResponse<any>>('/trend-learner/predict', data).then(r => r.data.data),
  dashboard: (modelId?: string) =>
    http.get<ApiResponse<any>>('/trend-learner/dashboard', { params: { model_id: modelId } }).then(r => r.data.data),
  residualAnalysis: (modelId?: string) =>
    http.get<ApiResponse<any>>('/trend-learner/residual-analysis', { params: { model_id: modelId } }).then(r => r.data.data),
}

export const thresholdLearnerApi = {
  initialize: (data: { model_id?: string; device_range?: number[]; spec_limits?: number[]; initial_data?: number[] }) =>
    http.post<ApiResponse<any>>('/threshold-learner/initialize', data).then(r => r.data.data),
  infer: (data: { model_id?: string; value: number }) =>
    http.post<ApiResponse<any>>('/threshold-learner/infer', data).then(r => r.data.data),
  feedback: (data: { model_id?: string; feedback_type: string; reason?: string }) =>
    http.post<ApiResponse<any>>('/threshold-learner/feedback', data).then(r => r.data.data),
  dashboard: (modelId?: string) =>
    http.get<ApiResponse<any>>('/threshold-learner/dashboard', { params: { model_id: modelId } }).then(r => r.data.data),
  decomposition: (modelId?: string) =>
    http.get<ApiResponse<any>>('/threshold-learner/decomposition', { params: { model_id: modelId } }).then(r => r.data.data),
}

// ─── 通知渠道 ───

export interface NotifyChannelStatus {
  id: string
  name: string
  type: string
  enabled: boolean
  status: string
  last_test: string | null
  config: Record<string, any>
}

export interface DingTalkConfig {
  enabled?: boolean
  name?: string
  webhook_url: string
  secret?: string
  at_mobiles?: string[]
  is_at_all?: boolean
  max_per_minute?: number
  cooldown_seconds?: number
}

export interface WeComConfig {
  enabled?: boolean
  name?: string
  webhook_url: string
  max_per_minute?: number
  cooldown_seconds?: number
}

export interface EmailConfig {
  enabled?: boolean
  name?: string
  smtp_host: string
  smtp_port?: number
  smtp_user?: string
  smtp_password?: string
  from_address?: string
  to_addresses: string[]
  use_tls?: boolean
  use_ssl?: boolean
  max_per_minute?: number
  cooldown_seconds?: number
}

export interface WebhookConfig {
  enabled?: boolean
  name?: string
  url: string
  method?: string
  headers?: Record<string, string>
  auth_type?: 'none' | 'basic' | 'bearer' | 'api_key'
  auth_token?: string
  auth_username?: string
  auth_password?: string
  max_per_minute?: number
  cooldown_seconds?: number
}

export const notifyApi = {
  listChannels: () =>
    http.get<ApiResponse<{ channels: NotifyChannelStatus[] }>>('/notify/channels').then(r => r.data.data),

  updateDingTalk: (data: DingTalkConfig) =>
    http.post<ApiResponse>('/notify/channels/dingtalk', data).then(r => r.data.data),

  updateWeCom: (data: WeComConfig) =>
    http.post<ApiResponse>('/notify/channels/wecom', data).then(r => r.data.data),

  updateEmail: (data: EmailConfig) =>
    http.post<ApiResponse>('/notify/channels/email', data).then(r => r.data.data),

  updateWebhook: (data: WebhookConfig) =>
    http.post<ApiResponse>('/notify/channels/webhook', data).then(r => r.data.data),

  testChannel: (channelId: string, configOverride?: Record<string, any>) =>
    http.post<ApiResponse<{ success: boolean; message: string }>>(`/notify/channels/${channelId}/test`, configOverride).then(r => r.data.data),

  enableChannel: (channelId: string, enabled: boolean = true) =>
    http.post<ApiResponse>(`/notify/channels/${channelId}/enable`, null, { params: { enabled } }).then(r => r.data.data),

  deleteChannel: (channelId: string) =>
    http.delete<ApiResponse>(`/notify/channels/${channelId}`).then(r => r.data.data),
}

// ─── 设备影子 ───

export interface ShadowInfo {
  device_id: string
  reported_count: number
  desired_count: number
  delta_count: number
  version: number
  last_updated: number
}

export interface ShadowDetail {
  device_id: string
  reported: Record<string, any>
  desired: Record<string, any>
  metadata: Record<string, Record<string, any>>
  version: number
  last_updated: number
  delta: Record<string, any>
}

export const shadowApi = {
  list: () =>
    http.get<ApiResponse<ShadowInfo[]>>('/shadows').then((r) => r.data.data),

  get: (deviceId: string) =>
    http.get<ApiResponse<ShadowDetail>>(`/shadows/${deviceId}`).then((r) => r.data.data),

  updateDesired: (deviceId: string, desired: Record<string, any>) =>
    http.put<ApiResponse<{ device_id: string; delta: Record<string, any>; version: number }>>(`/shadows/${deviceId}/desired`, { desired }).then((r) => r.data.data),

  updateReported: (deviceId: string, reported: Record<string, any>, quality?: string) =>
    http.put<ApiResponse<{ device_id: string; version: number }>>(`/shadows/${deviceId}/reported`, { reported, quality: quality || 'good' }).then((r) => r.data.data),

  delete: (deviceId: string) =>
    http.delete<ApiResponse<{ device_id: string; deleted: boolean }>>(`/shadows/${deviceId}`).then((r) => r.data.data),

  getDelta: (deviceId: string) =>
    http.get<ApiResponse<{ device_id: string; desired: Record<string, any>; reported: Record<string, any>; delta: Record<string, any>; version: number }>>(`/shadows/${deviceId}/delta`).then((r) => r.data.data),
}

// ─── MQTT Forwarder ───

export const mqttForwarderApi = {
  getOfflineQueueStatus: () =>
    http.get<ApiResponse<any>>('/mqtt-forwarder/offline-queue/status').then(r => r.data.data),  // FIXED: 后端路由前缀已改为/api/v1/mqtt-forwarder，使用默认baseURL
}

// ─── 设备模板 ───

export interface DeviceTemplateCreateParams {
  device_id: string  // FIXED-P1: 与后端TemplateCreate.device_id对齐
  template_name: string  // FIXED-P1: 与后端TemplateCreate.template_name对齐
}

export interface CreateFromTemplateParams {
  template_name: string
  device_id: string
  name: string
  config?: Record<string, any>  // FIXED: 与后端CreateFromTemplateRequest.config对齐（原名overrides）
}

export const templateApi = {
  list: () =>
    http.get<ApiResponse<any[]>>('/devices/templates').then((r) => r.data.data),

  create: (data: DeviceTemplateCreateParams) =>
    http.post<ApiResponse<any>>('/devices/templates', data).then((r) => r.data.data),

  createFromTemplate: (data: CreateFromTemplateParams) =>
    http.post<ApiResponse<any>>('/devices/from-template', data).then((r) => r.data.data),

  delete: (name: string) =>
    http.delete<ApiResponse>(`/devices/templates/${encodeURIComponent(name)}`).then((r) => r.data.data),

  export: (deviceIds: string[]) =>
    http.post<ApiResponse<any>>('/devices/export', { device_ids: deviceIds }).then((r) => r.data.data),

  import: (data: any) =>
    http.post<ApiResponse<any>>('/devices/import', data).then((r) => r.data.data),
}

// ─── 协议调试 ───

export interface DebugProtocol {
  key: string
  name: string
  schema: {
    name: string
    fields: DebugProtocolField[]
  }
}

export interface DebugProtocolField {
  key: string
  label: string
  type: 'text' | 'number' | 'select' | 'textarea'
  placeholder?: string
  default?: any
  min?: number
  max?: number
  optional?: boolean
  options?: { value: string; label: string }[]
}

export interface DebugDevice {
  device_id: string
  name: string
  protocol: string
  status: string
}

export interface DebugPacket {
  timestamp: number
  direction: 'tx' | 'rx'
  protocol: string
  device_id: string
  content: string
  content_type: 'hex' | 'ascii'
  metadata: Record<string, any>
}

export interface SimulateResult {
  protocol: string
  device_id: string
  operation: string
  params: Record<string, any>
  request_raw: string | null
  response_raw: string | null
  values: any
  error: string | null
  elapsed_ms: number
  alias_of?: string
}

export const debugApi = {
  listProtocols: () =>
    http.get<ApiResponse<{ protocols: DebugProtocol[] }>>('/debug/protocols').then(r => r.data.data),

  listDevices: (protocol?: string) =>
    http.get<ApiResponse<{ devices: DebugDevice[] }>>('/debug/devices', { params: { protocol } }).then(r => r.data.data),

  getPackets: (params?: { protocol?: string; device_id?: string; limit?: number }) =>
    http.get<ApiResponse<{ packets: DebugPacket[]; total: number }>>('/debug/packets', { params }).then(r => r.data.data),

  clearPackets: (protocol?: string) =>
    http.delete<ApiResponse<{ cleared: number }>>('/debug/packets', { params: { protocol } }).then(r => r.data.data),

  simulate: (protocol: string, deviceId: string, operation: string, params?: Record<string, any>) =>
    http.post<ApiResponse<SimulateResult>>('/debug/simulate', params, { params: { protocol, device_id: deviceId, operation } }).then(r => r.data.data),
}

// ─── 协议桥接 ───

export interface BridgeMapping {
  source_protocol: string
  source_device_id: string
  source_point: string
  target_protocol: string
  target_device_id: string
  target_point: string
  transform?: string
  enabled: boolean
}

export interface BridgeConfig {
  name: string
  mappings: BridgeMapping[]
  enabled: boolean
}

export const bridgeApi = {
  list: () =>
    http.get<ApiResponse<{ bridges: BridgeConfig[] }>>('/bridge/list').then((r) => r.data.data),

  get: (name: string) =>
    http.get<ApiResponse<BridgeConfig>>(`/bridge/${encodeURIComponent(name)}`).then((r) => r.data.data),

  create: (data: BridgeConfig) =>
    http.post<ApiResponse<BridgeConfig>>('/bridge/create', data).then((r) => r.data.data),

  update: (name: string, data: Partial<BridgeConfig>) =>
    http.put<ApiResponse<BridgeConfig>>(`/bridge/${encodeURIComponent(name)}`, data).then((r) => r.data.data),

  delete: (name: string) =>
    http.delete<ApiResponse>(`/bridge/${encodeURIComponent(name)}`).then((r) => r.data.data),

  enable: (name: string) =>
    http.post<ApiResponse>(`/bridge/${encodeURIComponent(name)}/enable`).then((r) => r.data.data),

  disable: (name: string) =>
    http.post<ApiResponse>(`/bridge/${encodeURIComponent(name)}/disable`).then((r) => r.data.data),
}

// ─── 设备联动 ───

export const linkageApi = {
  listRules: () =>
    http.get<ApiResponse<{ rules: any[]; total: number }>>('/linkage/rules').then(r => r.data.data),

  getRule: (ruleId: string) =>
    http.get<ApiResponse<any>>(`/linkage/rules/${ruleId}`).then(r => r.data.data),

  createRule: (data: any) =>
    http.post<ApiResponse<any>>('/linkage/rules', data).then(r => r.data.data),

  updateRule: (ruleId: string, data: any) =>
    http.put<ApiResponse<any>>(`/linkage/rules/${ruleId}`, data).then(r => r.data.data),

  deleteRule: (ruleId: string) =>
    http.delete<ApiResponse>(`/linkage/rules/${ruleId}`).then(r => r.data.data),

  enableRule: (ruleId: string) =>
    http.post<ApiResponse>(`/linkage/rules/${ruleId}/enable`).then(r => r.data.data),

  disableRule: (ruleId: string) =>
    http.post<ApiResponse>(`/linkage/rules/${ruleId}/disable`).then(r => r.data.data),

  // 适配3: 后端改为 DB 层分页，返回 PagedResponse {data: [...], total, ...}
  getExecutions: (ruleId?: string, limit?: number) =>
    http.get<PagedData<any>>('/linkage/executions', { params: { rule_id: ruleId, limit } }).then(r => r.data),

  getStats: () =>
    http.get<ApiResponse<any>>('/linkage/executions/stats').then(r => r.data.data),
}

// ─── 性能剖析 ───

export const profilerApi = {
  getStats: (minCalls?: number, topN?: number) =>
    http.get<ApiResponse<any>>('/profiler/stats', { params: { min_calls: minCalls, top_n: topN } }).then(r => r.data.data),

  getSlowest: (topN?: number) =>
    http.get<ApiResponse<any>>('/profiler/slowest', { params: { top_n: topN } }).then(r => r.data.data),

  getMemory: () =>
    http.get<ApiResponse<any>>('/profiler/memory').then(r => r.data.data),

  getRequestStats: () =>
    http.get<ApiResponse<any>>('/profiler/requests').then(r => r.data.data),

  enable: () =>
    http.post<ApiResponse>('/profiler/enable').then(r => r.data.data),

  disable: () =>
    http.post<ApiResponse>('/profiler/disable').then(r => r.data.data),

  reset: () =>
    http.post<ApiResponse>('/profiler/reset').then(r => r.data.data),

  export: (filename?: string) =>
    http.get<ApiResponse<any>>('/profiler/export', { params: { filename } }).then(r => r.data.data),
}

// ─── 日志聚合 ───

export const logApi = {
  query: (params: {
    level?: string; logger_name?: string; keyword?: string; start_time?: string; end_time?: string;
    request_id?: string; user_id?: string; device_id?: string; limit?: number; offset?: number;
  }) =>
    http.get<ApiResponse<{ logs: any[]; total: number; limit: number; offset: number }>>('/logs/query', { params }).then(r => r.data.data),

  getStats: () =>
    http.get<ApiResponse<any>>('/logs/stats').then(r => r.data.data),

  addFilter: (field: string, operator: string, value: string) =>
    http.post<ApiResponse>('/logs/filters', null, { params: { field, operator, value } }).then(r => r.data.data),

  clearFilters: () =>
    http.delete<ApiResponse>('/logs/filters').then(r => r.data.data),

  setLogLevel: (loggerName: string, level: string) =>
    http.post<ApiResponse>('/logs/level', null, { params: { logger_name: loggerName, level } }).then(r => r.data.data),

  archive: (archiveName?: string) =>
    http.post<ApiResponse>('/logs/archive', null, { params: { archive_name: archiveName } }).then(r => r.data.data),

  cleanup: () =>
    http.post<ApiResponse>('/logs/cleanup').then(r => r.data.data),
}

// ─── 固件签名验证 ───

export const firmwareApi = {
  verifySignature: (file: File, signature: string, publicKey: string, algorithm?: string) => {
    const formData = new FormData()
    formData.append('file', file)
    formData.append('signature', signature)
    formData.append('public_key', publicKey)
    if (algorithm) formData.append('algorithm', algorithm)
    return http.post<ApiResponse<any>>('/firmware/verify/signature', formData, { headers: { 'Content-Type': 'multipart/form-data' } }).then(r => r.data.data)
  },

  verifyHash: (file: File, expectedHash: string, algorithm?: string) => {
    const formData = new FormData()
    formData.append('file', file)
    formData.append('expected_hash', expectedHash)
    if (algorithm) formData.append('algorithm', algorithm)
    return http.post<ApiResponse<any>>('/firmware/verify/hash', formData, { headers: { 'Content-Type': 'multipart/form-data' } }).then(r => r.data.data)
  },

  generateManifest: (file: File, version: string, description?: string, algorithm?: string) => {
    const formData = new FormData()
    formData.append('file', file)
    formData.append('version', version)
    if (description) formData.append('description', description)
    if (algorithm) formData.append('algorithm', algorithm)
    return http.post<ApiResponse<any>>('/firmware/manifest/generate', formData, { headers: { 'Content-Type': 'multipart/form-data' } }).then(r => r.data.data)
  },
}

// ─── 配置版本管理 ───

export interface ConfigVersion {
  version_id: string
  version_number: number
  operator: string
  change_time: string
  change_content: string
  change_type: string
  snapshot: Record<string, any>
}

export const configVersionApi = {
  list: (params?: { page?: number; size?: number; search?: string }) =>
    http.get<PagedData<ConfigVersion>>('/config/versions', { params }).then((r) => r.data),  // FIXED-P1: 路由对齐后端/config/versions

  get: (versionId: string) =>
    http.get<ApiResponse<ConfigVersion>>(`/config/versions/${versionId}`).then((r) => r.data.data),

  diff: (fromVersion: number, toVersion: number) =>
    http.get<ApiResponse<{ diff: Record<string, any> }>>('/config/versions/diff', { params: { from: fromVersion, to: toVersion } }).then((r) => r.data.data),

  rollback: (versionId: number) =>
    http.post<ApiResponse<any>>(`/config/rollback/${versionId}`).then((r) => r.data.data),  // FIXED-P1: 路由对齐后端/config/rollback/{version}，参数改为number

  createSnapshot: () =>
    http.post<ApiResponse<ConfigVersion>>('/config/versions/snapshot').then((r) => r.data.data),

  delete: (versionId: string) =>
    http.delete<ApiResponse<any>>(`/config/versions/${versionId}`).then((r) => r.data.data),
}

// ─── 告警静默期 ───

export interface AlarmSilence {
  id: string
  device_id: string
  rule_id: string
  start_time: string
  end_time: string
  reason: string
  operator: string
  created_at: string
}

export interface AlarmSilenceCreateParams {
  device_id?: string | null
  rule_id?: string | null
  start_time: string
  end_time: string
  reason: string
}

export const alarmSilenceApi = {
  list: (params?: { page?: number; size?: number; status?: string }) =>
    http.get<PagedData<AlarmSilence>>('/alarms/silence', { params }).then((r) => r.data),  // FIXED-P1: 告警静默路由对齐后端

  create: (data: AlarmSilenceCreateParams) =>
    http.post<ApiResponse<AlarmSilence>>('/alarms/silence', data).then((r) => r.data.data),  // FIXED-P1: 告警静默路由对齐后端

  cancel: (silenceId: string) =>
    http.delete<ApiResponse<any>>(`/alarms/silence/${silenceId}`).then((r) => r.data.data),  // FIXED-P1: cancel对齐后端DELETE路由

  delete: (silenceId: string) =>
    http.delete<ApiResponse>(`/alarms/silence/${silenceId}`).then((r) => r.data.data),  // FIXED-P1: 告警静默路由对齐后端
}

// ─── 数据质量监控 ───

export const qualityMonitorApi = {
  getSummary: () =>
    http.get<ApiResponse<any>>('/data-quality/summary').then(r => r.data.data),
  getDevices: () =>
    http.get<ApiResponse<any[]>>('/data-quality/devices').then(r => r.data.data),
  getDeviceQuality: (deviceId: string) =>
    http.get<ApiResponse<any>>(`/data-quality/devices/${deviceId}`).then(r => r.data.data),
  getDevicePoints: (deviceId: string) =>
    http.get<ApiResponse<any[]>>(`/data-quality/devices/${deviceId}/points`).then(r => r.data.data),
  getTrend: (deviceId?: string, hours?: number) =>
    http.get<ApiResponse<any[]>>('/data-quality/trend', { params: { device_id: deviceId, hours } }).then(r => r.data.data),
  generateReport: () =>
    http.get<ApiResponse<any>>('/data-quality/report').then(r => r.data.data),
  resetStats: (deviceId?: string) =>
    http.post<ApiResponse>('/data-quality/reset', null, { params: { device_id: deviceId } }).then(r => r.data.data),
}

// ─── 资源共享 ───

export type ResourceType = 'device' | 'rule' | 'template'
export type PermissionLevel = 'read' | 'write'

export interface ResourceShare {
  share_id: string
  resource_type: ResourceType
  resource_id: string
  shared_with_user_id: string
  shared_with_username?: string
  permission_level: PermissionLevel
  shared_by_user_id: string
  shared_by_username?: string
  shared_at: string
}

export interface TransferResult {
  resource_type: ResourceType
  resource_id: string
  previous_owner: string
  new_owner: string
  new_owner_username: string
}

export const resourceShareApi = {
  share: (data: {
    resource_type: ResourceType
    resource_id: string
    shared_with_user_id: string
    permission_level?: PermissionLevel
  }) =>
    http.post<ApiResponse<ResourceShare>>('/resources/share', data).then((r) => r.data.data),

  unshare: (data: {
    resource_type: ResourceType
    resource_id: string
    shared_with_user_id: string
  }) =>
    http.post<ApiResponse<{ removed: boolean }>>('/resources/unshare', data).then((r) => r.data.data),

  listShares: (resourceType: ResourceType, resourceId: string) =>
    http.get<ApiResponse<{ shares: ResourceShare[] }>>('/resources/shares', {
      params: { resource_type: resourceType, resource_id: resourceId },
    }).then((r) => r.data.data?.shares ?? []),

  transfer: (data: {
    resource_type: ResourceType
    resource_id: string
    new_owner_id: string
  }) =>
    http.post<ApiResponse<TransferResult>>('/resources/transfer', data).then((r) => r.data.data),
}

// ─── 脚本引擎 ───

export const scriptApi = {
  list: () =>
    http.get<ApiResponse<any[]>>('/scripts/list').then(r => r.data.data),
  create: (data: { name: string; language: string; device_id?: string; code: string; interval_seconds?: number }) =>
    http.post<ApiResponse<any>>('/scripts/create', data).then(r => r.data.data),
  update: (scriptId: string, data: Record<string, any>) =>
    http.put<ApiResponse<any>>(`/scripts/${scriptId}`, data).then(r => r.data.data),
  delete: (scriptId: string) =>
    http.delete<ApiResponse>(`/scripts/${scriptId}`).then(r => r.data.data),
  enable: (scriptId: string) =>
    http.post<ApiResponse>(`/scripts/${scriptId}/enable`).then(r => r.data.data),
  disable: (scriptId: string) =>
    http.post<ApiResponse>(`/scripts/${scriptId}/disable`).then(r => r.data.data),
  test: (scriptId: string, inputData?: Record<string, any>) =>
    http.post<ApiResponse<any>>(`/scripts/${scriptId}/test`, { input_data: inputData }).then(r => r.data.data),
  getLogs: (scriptId: string, limit?: number) =>
    http.get<ApiResponse<any[]>>(`/scripts/${scriptId}/logs`, { params: { limit } }).then(r => r.data.data),
  // SEC-FIX-SCRIPT-SIGN: 脚本审核状态机端点
  submitReview: (scriptId: string) =>
    http.post<ApiResponse<any>>(`/scripts/${scriptId}/submit-review`).then(r => r.data.data),
  approve: (scriptId: string) =>
    http.post<ApiResponse<any>>(`/scripts/${scriptId}/approve`).then(r => r.data.data),
  reject: (scriptId: string) =>
    http.post<ApiResponse<any>>(`/scripts/${scriptId}/reject`).then(r => r.data.data),
}

// ─── Modbus TCP 运维面板 ───

export interface ModbusDeviceOps {
  device_id: string
  online_rate: number
  state: string
  avg_latency_ms: number
  total_reconnects: number
  total_reads: number
  failed_reads: number
  total_writes: number
  failed_writes: number
  is_connected: boolean
  degrade_level: number
  latency_history: { time: number; value: number }[]
  reconnect_history: { hour: string; count: number }[]
}

export interface ModbusPointHealth {
  point_name: string
  success_count: number
  fail_count: number
  total: number
  consecutive_fails: number
  success_rate: number
  avg_latency_ms: number
  quality_history: string[]
  current_quality: string
  last_success_at: string | null
}

export interface ModbusWriteAudit {
  timestamp: string
  user: string
  device_id: string
  point_id: string
  old_value: any
  new_value: any
  result: string
  error_msg: string
}

export const modbusOpsApi = {
  getDeviceOps: (deviceId: string) =>
    http.get<ApiResponse<ModbusDeviceOps>>(`/devices/${deviceId}/ops`).then(r => r.data.data),

  getPointHealth: (deviceId: string) =>
    http.get<ApiResponse<ModbusPointHealth[]>>(`/devices/${deviceId}/point-health`).then(r => r.data.data),

  getWriteAudit: (deviceId: string, params?: { limit?: number; result?: string; start_time?: string; end_time?: string }) =>
    http.get<ApiResponse<ModbusWriteAudit[]>>(`/devices/${deviceId}/write-audit`, { params }).then(r => r.data.data),
}

export interface ModbusSlaveOpsData {
  device_id: string
  online: boolean
  guard_stats: {
    active_connections: number
    active_ips: Record<string, number>
    banned_ips: string[]
    total_connections: number
    total_rejected: number
  }
  registers: Array<{
    name: string
    reg_type: string
    address: number
    data_type: string
    access: string
    value: any
    quality: string
    last_modified: string | null
  }>
  audit_log: Array<{
    timestamp: string
    device_id: string
    point: string
    old_value: any
    new_value: any
    result: string
    reason: string
  }>
}

export const modbusSlaveOpsApi = {
  getSlaveOps: (deviceId: string) =>
    http.get<ApiResponse<ModbusSlaveOpsData>>(`/modbus-slave/devices/${deviceId}/ops`).then(r => r.data.data),
}

export interface FinsDeviceOps {
  online_rate: number
  state: string
  active_ip: string
  transport: string
  plc_series: string
  avg_latency_ms: number
  latency_history: { value: number }[]
  error_distribution: Record<string, number>
  total_reconnects: number
  degrade_level: number
}

export const finsOpsApi = {
  getDeviceOps: (deviceId: string) =>
    http.get<ApiResponse<FinsDeviceOps>>(`/devices/${deviceId}/ops`).then(r => r.data.data),

  getPointHealth: (deviceId: string) =>
    http.get<ApiResponse<Record<string, any>[]>>(`/devices/${deviceId}/point-health`).then(r => r.data.data),

  getWriteAudit: (deviceId: string, params?: { limit?: number; result?: string; start_time?: string; end_time?: string }) =>
    http.get<ApiResponse<Record<string, any>[]>>(`/devices/${deviceId}/write-audit`, { params }).then(r => r.data.data),
}

// ─── 可观测性告警 ───

export type AlertSeverity = 'low' | 'medium' | 'high' | 'critical'

export interface AlertRule {
  id: number
  name: string
  condition: string
  threshold: number
  severity: AlertSeverity
  window_seconds: number
  cooldown_seconds: number
  enabled: boolean
  notify_channels: string[]
  created_at?: number
  updated_at?: number
}

export interface AlertRuleCreate {
  name: string
  condition: string
  threshold?: number
  severity?: AlertSeverity
  window_seconds?: number
  cooldown_seconds?: number
  enabled?: boolean
  notify_channels?: string[]
}

export interface AlertEvent {
  id?: number
  rule_name: string
  severity: string
  message: string
  value: number
  threshold: number
  timestamp: number
  status: 'open' | 'resolved'
  acknowledged: boolean
  resolved_at?: number
}

// Message Trace types
export interface TraceNode {
  name: string
  enter_time: number
  exit_time: number | null
  duration_ms: number | null
  result: string
  error: string
  metadata: Record<string, any>
}

export interface MessageTrace {
  message_id: string
  trace_id: string
  status: string
  created_at: number
  completed_at: number | null
  total_duration_ms: number | null
  nodes: TraceNode[]
}

// Latency stats types
export interface LatencyStats {
  platform: string
  stats: {
    count: number
    sum: number
    avg: number
    min: number
    max: number
  }
  percentiles: {
    p50: number
    p95: number
    p99: number
  }
}

export interface LatencyHistogram {
  platform: string
  name: string
  count: number
  sum: number
  buckets: Record<string, number>
  p50_ms: number
  p95_ms: number
  p99_ms: number
}

// Overview types
export interface PlatformOverview {
  platform: string
  connected: boolean
  total_messages: number
  success_rate: number
  avg_latency_ms: number
  p50_latency_ms: number
  p95_latency_ms: number
  p99_latency_ms: number
  error_rate: number
  health_score: number
  last_updated: number
}

// Node stats types
export interface NodeStats {
  node: string
  platform: string
  total: number
  errors: number
  error_rate: number
  avg_ms: number
  max_ms: number
  p50_ms: number
  p95_ms: number
  p99_ms: number
}

export const observabilityApi = {
  // Overview
  getOverview: (platform?: string) =>
    http.get<ApiResponse<{ platforms: PlatformOverview[]; timestamp: number }>>('/observability/overview', { params: { platform: platform || '' } }).then(r => r.data.data),

  // Latency
  getLatencyStats: (platform?: string) =>
    http.get<ApiResponse<{ platforms: LatencyStats[]; timestamp: number }>>('/observability/latency', { params: { platform: platform || '' } }).then(r => r.data.data),

  getLatencyPercentiles: (platform?: string) =>
    http.get<ApiResponse<{ platforms: any[]; timestamp: number }>>('/observability/latency/percentiles', { params: { platform: platform || '' } }).then(r => r.data.data),

  getLatencyHistogram: (platform?: string) =>
    http.get<ApiResponse<{ platforms: LatencyHistogram[]; timestamp: number }>>('/observability/latency/histogram', { params: { platform: platform || '' } }).then(r => r.data.data),

  // Rules
  getRules: () =>
    http.get<ApiResponse<{ rules: AlertRule[] }>>('/observability/alerts/rules').then(r => r.data.data?.rules || []),

  createRule: (data: AlertRuleCreate) =>
    http.post<ApiResponse<AlertRule>>('/observability/alerts/rules', data).then(r => r.data.data),

  updateRule: (ruleName: string, data: Partial<AlertRuleCreate>) =>
    http.put<ApiResponse<AlertRule>>(`/observability/alerts/rules/${encodeURIComponent(ruleName)}`, data).then(r => r.data.data),

  deleteRule: (ruleName: string) =>
    http.delete<ApiResponse>(`/observability/alerts/rules/${encodeURIComponent(ruleName)}`).then(r => r.data.data),

  // Events
  // 适配2: 后端改为 DB 层分页，返回 PagedResponse {data: [...], total, ...}
  getEvents: (params?: { rule_name?: string; status?: string; limit?: number; offset?: number }) =>
    http.get<PagedData<AlertEvent>>('/observability/alerts/events', { params }).then(r => r.data),

  resolveEvent: (ruleName: string, timestamp: number) =>
    http.post<ApiResponse>(`/observability/alerts/events/${encodeURIComponent(ruleName)}/${timestamp}/resolve`).then(r => r.data.data),

  // Traces
  getTraces: (params?: { platform?: string; status?: string; limit?: number; offset?: number }) =>
    http.get<ApiResponse<{ traces: MessageTrace[]; total: number; timestamp: number }>>('/observability/traces', { params }).then(r => r.data.data),

  getTrace: (messageId: string) =>
    http.get<ApiResponse<{ trace: MessageTrace; platform: string }>>(`/observability/traces/${encodeURIComponent(messageId)}`).then(r => r.data.data),

  getTraceNodeStats: (nodeName: string, platform?: string, windowSeconds?: number) =>
    http.get<ApiResponse<{ stats: NodeStats[]; timestamp: number }>>(`/observability/traces/stats/${encodeURIComponent(nodeName)}`, {
      params: { platform: platform || '', window_seconds: windowSeconds || 300 }
    }).then(r => r.data.data),

  // Metrics
  getMetrics: () =>
    http.get<ApiResponse<{
      metrics: any[];
      summary: {
        total_requests: number;
        total_errors: number;
        avg_latency_ms: number;
        error_rate: number;
      };
      timestamp: number;
    }>>('/observability/metrics').then(r => r.data.data),
}

// FIXED-DATA-MIGRATION: 数据迁移 API
export interface DataExportParams {
  scope: 'devices' | 'rules' | 'alarms' | 'all'
  ids?: string[]
  format?: 'json' | 'csv'
}

export interface DataImportParams {
  scope: 'devices' | 'rules'
  data: string
  format?: 'json' | 'csv'
  mode: 'skip' | 'overwrite' | 'rename' | 'error'
  atomic?: boolean
}

export interface DataImportResult {
  success: boolean
  total_count: number
  imported_count: number
  skipped_count: number
  error_count: number
  errors: string[]
  warnings: string[]
}

export const dataMigrationApi = {
  // 导出数据
  exportData: (params: DataExportParams) =>
    http.post<ApiResponse<{ content: string; scope: string; format: string }>>('/data/export', params).then(r => r.data.data),

  // 导入数据
  importData: (params: DataImportParams) =>
    http.post<ApiResponse<DataImportResult>>('/data/import', params, { timeout: 300000 }).then(r => r.data.data),
}

// ─── 数据仿真 ───

export interface SimType {
  type: string
  name: string
  equation: string
  default_params?: Record<string, any>
}

export interface SimulationForm {
  sim_type: string
  duration_seconds: number
  sample_rate_hz: number
  anomaly_ratio: number
  noise_snr_db: number
  seed: number
  params?: Record<string, any>
}

export interface PreviewData {
  timestamps: number[]
  values: number[] | number[][]
  labels: number[]
  annotations?: Annotation[]
}

export interface Annotation {
  anomaly_type: string
  start_time: number
  end_time: number
  severity: number
  root_cause: string
}

export interface QualityAssessment {
  quality_score: number
  data_completeness: number
  anomaly_ratio: number
  outlier_ratio: number
  mean: number
  std: number
  anomaly_type_distribution: Record<string, number>
}

export interface SimulationExportParams extends SimulationForm {
  format: 'csv' | 'parquet' | 'npy'
  include_labels: boolean
}

export const simulationApi = {
  // 获取仿真类型列表
  getTypes: () =>
    http.get<ApiResponse<SimType[]>>('/simulation/types').then(r => r.data.data),

  // 运行预览
  preview: (data: SimulationForm) =>
    http.post<ApiResponse<PreviewData>>('/simulation/preview', data).then(r => r.data.data),

  // 完整运行
  run: (data: SimulationForm) =>
    http.post<ApiResponse<PreviewData>>('/simulation/run', data).then(r => r.data.data),

  // 评估数据质量
  assess: (data: SimulationForm) =>
    http.post<ApiResponse<QualityAssessment>>('/simulation/assess', data).then(r => r.data.data),

  // 导出仿真数据
  export: (params: SimulationExportParams) =>
    http.post('/simulation/export', params, { responseType: 'blob' }),
}

