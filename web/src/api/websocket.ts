/**
 * WebSocket连接管理器
 * 支持多通道连接、自动重连(指数退避)、Token认证
 */

import { useAuthStore } from '@/stores/auth'

type MessageHandler = (data: any) => void
type StatusHandler = (status: 'connected' | 'disconnected' | 'error' | 'reconnecting', reason?: string) => void
// 修复9: 批量消息处理器，接收 50ms 窗口内缓冲的消息数组
type BatchMessageHandler = (messages: any[]) => void

interface WSConnection {
  ws: WebSocket | null
  reconnectTimer: ReturnType<typeof setTimeout> | null
  reconnectAttempt: number
  handlers: Set<MessageHandler>
  statusHandlers: Set<StatusHandler>
  pollingTimer: ReturnType<typeof setTimeout> | null
  pollingActive?: boolean
  lastStatus: 'connected' | 'disconnected' | 'error' | 'reconnecting'
  heartbeatTimer: ReturnType<typeof setInterval> | null
  lastPongTime: number
  // 修复9: 批量处理相关字段
  batchHandlers: Set<BatchMessageHandler>
  batchBuffer: any[]
  batchTimer: ReturnType<typeof setTimeout> | null
}

const connections: Map<string, WSConnection> = new Map()

const WS_BASE = import.meta.env.VITE_WS_BASE_URL
  || `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}`

const CHANNELS = {
  realtime: '/ws/v1/realtime',
  alarm: '/ws/v1/alarm',
  device: '/ws/v1/device',
  integration: '/ws/v1/integration',
  ai: '/ws/v1/ai',
} as const

// FIXED: WebSocket降级HTTP轮询时使用的API端点映射
// 原实现直接将 /ws/v1/{channel} 替换为 /api/v1/{channel}/poll，但后端不存在 /poll 端点
// 改为使用已有的REST API端点进行降级轮询
// FIXED-P0: alarm 通道加 status=firing 过滤，避免拉取已恢复/已确认告警造成重复推送
const POLL_API_MAP: Record<string, string> = {
  alarm: '/api/v1/alarms?size=20&page=1&status=firing',
  device: '/api/v1/devices/health/all',
  realtime: '/api/v1/data/stats',
  ai: '/api/v1/ai/stats',
  integration: '/api/v1/system/status',
}

// FIXED-P0: HTTP 轮询降级时用 Set 去重，避免同一报警 ID 被重复推送
// 超过 500 条时保留最近 250 条，防止内存无限增长
const seenAlarmIds: Set<string> = new Set()

// FIXED: 不同通道使用不同的轮询间隔，避免5个通道同时轮询消耗过多请求
// alarm 通道需要较高频率（告警通知），其他通道可以低频轮询
const POLL_INTERVAL_MAP: Record<string, number> = {
  alarm: 10000,       // 10秒
  device: 30000,      // 30秒
  realtime: 15000,    // 15秒
  ai: 60000,          // 60秒
  integration: 60000, // 60秒
}

type ChannelName = keyof typeof CHANNELS

const MAX_RECONNECT_DELAY = 30000
const BASE_RECONNECT_DELAY = 1000
const WS_AUTH_CLOSE_CODE = 4001
// FIXED: 原问题-重连无上限，添加最大重连次数常量
const MAX_RECONNECT_ATTEMPTS = 10
// FIXED-P0: WebSocket 心跳机制常量，防止静默断线不重连
const HEARTBEAT_INTERVAL = 25000
const HEARTBEAT_TIMEOUT = 60000
// 修复9: 批量消息 flush 间隔（50ms 窗口内缓冲的消息一次性派发给批量处理器）
const BATCH_FLUSH_INTERVAL = 50

function getToken(): string {
  try {
    // FIXED-Critical: LP-02 重构后 token 仅保存在 Pinia 内存中，不再写入 sessionStorage
    // 改为从 auth store 读取，否则认证帧永不发送，WS 连接全部失败
    const auth = useAuthStore()
    return auth.token || ''
  } catch {
    // [AUDIT-FIX] auth store 不可用时返回空字符串；sessionStorage 回退分支为死代码已移除
    return ''
  }
}

function getReconnectDelay(attempt: number): number {
  const delay = BASE_RECONNECT_DELAY * Math.pow(2, attempt)
  return Math.min(delay, MAX_RECONNECT_DELAY)
}

const HTTP_POLL_INTERVAL = 5000

function createConnection(channel: ChannelName): WSConnection {
  return {
    ws: null,
    reconnectTimer: null,
    reconnectAttempt: 0,
    handlers: new Set(),
    statusHandlers: new Set(),
    pollingTimer: null,
    lastStatus: 'disconnected',
    heartbeatTimer: null,
    lastPongTime: 0,
    // 修复9: 批量处理字段初始化
    batchHandlers: new Set(),
    batchBuffer: [],
    batchTimer: null,
  }
}

function notifyStatus(conn: WSConnection, status: 'connected' | 'disconnected' | 'error' | 'reconnecting', reason?: string) {
  conn.lastStatus = status
  conn.statusHandlers.forEach((handler) => {
    try { handler(status, reason) } catch (e) { console.error('[WS] Status handler error:', e) }
  })
}

function startHttpPolling(channel: ChannelName): void {
  const conn = connections.get(channel)
  if (!conn || conn.pollingTimer) return
  const pollUrl = POLL_API_MAP[channel]
  if (!pollUrl) return
  const interval = POLL_INTERVAL_MAP[channel] || 30000
  // FIXED-P0: 原问题-轮询返回的REST API数据格式与WS推送格式不兼容，handler直接丢弃
  // 记录上次轮询的最新报警ID，仅推送增量数据
  let lastAlarmId: string | null = null
  conn.pollingActive = true
  const pollOnce = async () => {
    try {
      const resp = await fetch(`${location.origin}${pollUrl}`, {
        headers: { 'Authorization': `Bearer ${getToken()}` },
      })
      if (resp.ok) {
        const data = await resp.json()
        // FIXED-P0: 将REST API响应转换为WS推送兼容格式
        const normalized = normalizePollData(channel, data, lastAlarmId)
        if (normalized) {
          conn.handlers.forEach((handler) => {
            try { handler(normalized) } catch (e) { console.error('[WS] Poll handler error:', e) }
          })
          // 更新lastAlarmId用于下次增量比较
          if (channel === 'alarm' && data?.data?.length > 0) {
            lastAlarmId = data.data[0].id || data.data[0].alarm_id || null
          }
        }
      }
    } catch { /* ignore poll errors */ }
    if (conn.pollingActive !== false) {
      conn.pollingTimer = setTimeout(pollOnce, interval)
    }
  }
  conn.pollingTimer = setTimeout(pollOnce, interval)
}

// FIXED-P0: 将轮询REST API响应转换为WS推送兼容格式，防止报警通知在降级轮询时丢失
function normalizePollData(channel: ChannelName, raw: any, lastId: string | null): any | null {
  if (!raw) return null
  if (channel === 'alarm') {
    // REST API返回 {code, data: [...alarms], total, page, size}
    // WS推送格式: {type: 'alarm', action: 'trigger'|'recover', alarm_id, ...}
    const alarms = raw.data || raw.alarms || []
    if (!Array.isArray(alarms) || alarms.length === 0) return null
    // FIXED-P0: 改用模块级 Set 去重，避免仅靠 lastId 过滤导致的重复推送
    // （lastId 只能挡住最新一条，分页/重排时其他报警会重复进入）
    const newAlarms = alarms.filter((a: any) => {
      const id = a.id || a.alarm_id
      if (!id || seenAlarmIds.has(id)) return false
      seenAlarmIds.add(id)
      return true
    })
    // 防止 Set 无限增长：超过 500 条时保留最近 250 条
    if (seenAlarmIds.size > 500) {
      const arr = Array.from(seenAlarmIds).slice(-250)
      seenAlarmIds.clear()
      arr.forEach(id => seenAlarmIds.add(id))
    }
    if (newAlarms.length === 0) return null
    // 转换为WS推送格式
    return {
      type: 'alarm',
      action: 'trigger',
      data: newAlarms.map((a: any) => ({
        alarm_id: a.id || a.alarm_id,
        // FIXED-BugR8: 透传 rule_id/rule_name/device_name，避免轮询降级时通知显示"未知规则: {device_id}"
        rule_id: a.rule_id,
        rule_name: a.rule_name,
        device_id: a.device_id,
        device_name: a.device_name,
        point_name: a.point_name,
        severity: a.severity,
        message: a.message,
        timestamp: a.triggered_at || a.created_at,
      })),
    }
  }
  // 其他通道直接透传
  return raw
}

function stopHttpPolling(conn: WSConnection): void {
  conn.pollingActive = false
  if (conn.pollingTimer) {
    clearTimeout(conn.pollingTimer)
    conn.pollingTimer = null
  }
}

// FIXED-P0: WebSocket 心跳机制，定期发送 ping 检测连接活性，超时强制重连
function startHeartbeat(channel: ChannelName): void {
  const conn = connections.get(channel)
  if (!conn || conn.heartbeatTimer) return
  conn.heartbeatTimer = setInterval(() => {
    if (!conn.ws || conn.ws.readyState !== WebSocket.OPEN) return
    try {
      conn.ws.send(JSON.stringify({ type: 'ping', ts: Date.now() }))
    } catch { /* ignore ping send errors */ }
    if (Date.now() - conn.lastPongTime > HEARTBEAT_TIMEOUT) {
      console.warn('[WS] Heartbeat timeout on channel "' + channel + '", forcing reconnect')
      conn.ws.close()
    }
  }, HEARTBEAT_INTERVAL)
}

function stopHeartbeat(conn: WSConnection): void {
  if (conn.heartbeatTimer) {
    clearInterval(conn.heartbeatTimer)
    conn.heartbeatTimer = null
  }
}

// 修复9: 批量消息缓冲与 flush 机制
// 将消息推入缓冲区，启动 50ms 定时器，到时一次性派发给所有批量处理器
function enqueueBatchMessage(conn: WSConnection, data: any): void {
  if (conn.batchHandlers.size === 0) return
  conn.batchBuffer.push(data)
  if (conn.batchTimer === null) {
    conn.batchTimer = setTimeout(() => {
      flushBatchBuffer(conn)
    }, BATCH_FLUSH_INTERVAL)
  }
}

// 立即 flush 缓冲区，将累积的消息数组派发给所有批量处理器
function flushBatchBuffer(conn: WSConnection): void {
  if (conn.batchTimer !== null) {
    clearTimeout(conn.batchTimer)
    conn.batchTimer = null
  }
  if (conn.batchBuffer.length === 0) return
  const batch = conn.batchBuffer
  conn.batchBuffer = []
  conn.batchHandlers.forEach((handler) => {
    try { handler(batch) } catch (e) { console.error('[WS] Batch message handler error:', e) }
  })
}

function connectChannel(channel: ChannelName): void {
  const conn = connections.get(channel)
  if (!conn) return

  const token = getToken()
  // LP-02: Cookie 模式下 token 可能为空，此时依赖 HttpOnly Cookie 认证
  // 后端 WS 处理器会优先从 Cookie 读取 access_token
  // 仅当既无 token 又无 Cookie 时才报错（Cookie 无法通过 JS 检测，所以放行让后端判断）
  if (!token) {
    // 不再阻止连接，让后端通过 Cookie 认证
    // 如果 Cookie 也不存在，后端会关闭连接（code 4001）
    console.debug('[WS] No in-memory token for channel "' + channel + '", relying on HttpOnly Cookie')
  }

  // FIXED-P2: Token不再通过URL查询参数传输(防止日志/Referer泄露)，改为连接后首帧认证
  const url = `${WS_BASE}${CHANNELS[channel]}`

  // FIXED-P1: 创建新连接前检查现有连接，避免重复连接导致状态混乱
  if (conn.ws) {
    if (conn.ws.readyState === WebSocket.OPEN || conn.ws.readyState === WebSocket.CONNECTING) {
      console.warn('[WS] Channel "' + channel + '" already active, closing old')
      conn.ws.onclose = null
      conn.ws.close()
    }
    conn.ws = null
  }

  try {
    const ws = new WebSocket(url)

    ws.onopen = () => {
      if (conn.reconnectTimer) {
        clearTimeout(conn.reconnectTimer)
        conn.reconnectTimer = null
      }
      conn.reconnectAttempt = 0
      stopHttpPolling(conn)
      // FIXED-Bug20: WS 恢复后清理 HTTP 轮询期间的去重缓存
      // 之前：seenAlarmIds 不清理，下次轮询降级时误去重状态变更后重新返回的报警，导致推送丢失
      if (channel === 'alarm') {
        seenAlarmIds.clear()
      }
      // LP-02: 仅当有 in-memory token 时发送首帧认证消息
      // 无 token 时依赖 HttpOnly Cookie 认证（后端 _recv_auth_token 会优先检查 Cookie）
      if (token) {
        try {
          ws.send(JSON.stringify({ type: 'auth', token }))
        } catch (e) { console.error('[WS] Auth frame send failed:', e) }
      }
      // FIXED-P2: 不在认证前通知 connected，等待服务端返回 auth ok 后再通知
      // FIXED-P0: 启动心跳并初始化 lastPongTime
      conn.lastPongTime = Date.now()
      startHeartbeat(channel)
    }

    ws.onmessage = (event) => {
      // FIXED(安全): 限制接收消息大小为2MB，防止恶意超大消息导致客户端内存耗尽
      // 服务端已限制发送消息大小(broadcast数据由服务端控制)，此处为客户端防御性检查
      if (typeof event.data === 'string' && event.data.length > 2 * 1024 * 1024) {
        console.warn('[WS] Received oversized message (' + event.data.length + ' bytes), discarding')
        return
      }
      try {
        const data = JSON.parse(event.data)
        // FIXED-P0: 收到任何消息即更新 lastPongTime，作为心跳存活依据
        conn.lastPongTime = Date.now()
        // FIXED-P2: 收到 auth ok 响应后才通知 connected，避免认证前误报
        if (data && data.type === 'auth' && data.status === 'ok' && conn.lastStatus !== 'connected') {
          notifyStatus(conn, 'connected')
          return
        }
        // 心跳 pong 响应仅更新 lastPongTime，不分发给业务 handler
        if (data && data.type === 'pong') {
          return
        }
        conn.handlers.forEach((handler) => {
          try { handler(data) } catch (e) { console.error('[WS] Message handler error:', e) }
        })
        // 修复9: 将消息推入批量缓冲区，50ms 后 flush 给批量处理器
        enqueueBatchMessage(conn, data)
      } catch (e) { console.error('[WS] Message parse error:', e) }
    }

    ws.onclose = (event) => {
      conn.ws = null
      // FIXED-P0: 关闭时停止心跳，避免定时器泄漏
      stopHeartbeat(conn)
      // FIXED: 原问题-认证失败后静默断开，收到4001关闭码后不再重连也不通知用户
      // 现添加console.warn提示并触发全局事件通知
      if (event.code === WS_AUTH_CLOSE_CODE) {
        console.warn(`[WS] Authentication failed on channel "${channel}" (code: ${event.code}). Token may be invalid or expired. No auto-reconnect.`)
        try {
          window.dispatchEvent(new CustomEvent('ws:auth-failed', { detail: { channel, code: event.code } }))
        } catch (e) { console.error('[WS] Failed to dispatch auth-failed event:', e) }
        notifyStatus(conn, 'error', 'Authentication failed')
        return
      }
      notifyStatus(conn, 'disconnected', event.reason)
      startHttpPolling(channel)
      scheduleReconnect(channel)
    }

    ws.onerror = (event) => {
      // FIXED: 原问题-onerror仅调用close不记录信息不通知用户
      // 现记录错误并通知status监听者
      console.error('[WS] Connection error:', event)
      notifyStatus(conn, 'error', 'Connection error')
      ws.close()
    }

    conn.ws = ws
  } catch (e) {
    console.error('[WS] Connection create failed:', e)
    notifyStatus(conn, 'error', String(e))
    scheduleReconnect(channel)
  }
}

function scheduleReconnect(channel: ChannelName): void {
  const conn = connections.get(channel)
  if (!conn) return

  // FIXED: 原问题-重连无上限，超过最大重连次数后停止重连并console.warn
  if (conn.reconnectAttempt >= MAX_RECONNECT_ATTEMPTS) {
    console.warn(`[WS] Max reconnect attempts (${MAX_RECONNECT_ATTEMPTS}) reached for channel "${channel}". Stopping reconnect.`)
    notifyStatus(conn, 'error', `Max reconnect attempts (${MAX_RECONNECT_ATTEMPTS}) reached`)
    // FIXED: 通知全局，让 UI 层提示用户实时数据已断开
    window.dispatchEvent(new CustomEvent('ws:reconnect-failed', { detail: { channel } }))
    return
  }

  if (conn.reconnectTimer) clearTimeout(conn.reconnectTimer)

  const delay = getReconnectDelay(conn.reconnectAttempt)
  conn.reconnectAttempt++
  notifyStatus(conn, 'reconnecting', `Attempt ${conn.reconnectAttempt}, retry in ${delay}ms`)

  conn.reconnectTimer = setTimeout(() => {
    connectChannel(channel)
  }, delay)
}

export function connect(channel: ChannelName, onMessage: MessageHandler): void {
  if (!connections.has(channel)) {
    connections.set(channel, createConnection(channel))
  }

  const conn = connections.get(channel)!
  conn.handlers.add(onMessage)

  if (!conn.ws || conn.ws.readyState === WebSocket.CLOSED) {
    // FIXED-P2: 重连次数达上限后断网恢复时，重置计数允许重新连接
    if (conn.reconnectAttempt >= MAX_RECONNECT_ATTEMPTS) {
      conn.reconnectAttempt = 0
    }
    // FIXED-P1: 调用 connectChannel 前清理重连定时器，避免定时器残留导致重复连接
    if (conn.reconnectTimer) {
      clearTimeout(conn.reconnectTimer)
      conn.reconnectTimer = null
    }
    connectChannel(channel)
  }
}

// 修复9: 注册批量消息处理器
// 与 connect() 不同，批量处理器在 50ms 窗口内累积的消息数组上被调用一次，
// 适用于高频消息场景（如实时数据流），减少 UI 重渲染次数。
// 返回一个注销函数，调用后移除该批量处理器。
export function connectBatch(channel: ChannelName, onBatch: BatchMessageHandler): () => void {
  if (!connections.has(channel)) {
    connections.set(channel, createConnection(channel))
  }

  const conn = connections.get(channel)!
  conn.batchHandlers.add(onBatch)

  // 若连接未建立则触发连接（与 connect 行为一致）
  if (!conn.ws || conn.ws.readyState === WebSocket.CLOSED) {
    if (conn.reconnectAttempt >= MAX_RECONNECT_ATTEMPTS) {
      conn.reconnectAttempt = 0
    }
    if (conn.reconnectTimer) {
      clearTimeout(conn.reconnectTimer)
      conn.reconnectTimer = null
    }
    connectChannel(channel)
  }

  // 返回注销函数
  return () => {
    conn.batchHandlers.delete(onBatch)
  }
}

export function onStatus(channel: ChannelName, handler: StatusHandler): void {
  if (!connections.has(channel)) {
    connections.set(channel, createConnection(channel))
  }
  connections.get(channel)!.statusHandlers.add(handler)
}

// FIXED-P0: 新增offStatus函数，允许组件单独注销status handler，防止匿名handler泄漏
export function offStatus(channel: ChannelName, handler: StatusHandler): void {
  const conn = connections.get(channel)
  if (!conn) return
  conn.statusHandlers.delete(handler)
}

export function disconnect(channel: ChannelName, handler?: MessageHandler): void {
  const conn = connections.get(channel)
  if (!conn) return

  if (handler) {
    conn.handlers.delete(handler)
    if (conn.handlers.size > 0) return
  }

  if (conn.reconnectTimer) {
    clearTimeout(conn.reconnectTimer)
    conn.reconnectTimer = null
  }

  stopHttpPolling(conn)
  // FIXED-P0: 断开时停止心跳，避免定时器泄漏
  stopHeartbeat(conn)
  // 修复9: 断开时 flush 并清理批量处理定时器
  if (conn.batchTimer !== null) {
    flushBatchBuffer(conn)
  }

  if (conn.ws) {
    // FIXED-P2: 清理全部 WS 回调，防止旧回调在新连接上误触发
    conn.ws.onopen = null
    conn.ws.onmessage = null
    conn.ws.onerror = null
    conn.ws.onclose = null
    conn.ws.close()
    conn.ws = null
  }

  conn.handlers.clear()
  conn.statusHandlers.clear()
  // 修复9: 清理批量处理器和缓冲区
  conn.batchHandlers.clear()
  conn.batchBuffer = []
  connections.delete(channel)
  // FIXED-Severe: 断开 alarm 通道时清空去重 Set，避免重新登录后旧告警 ID 残留导致漏推
  if (channel === 'alarm') {
    seenAlarmIds.clear()
  }
}

export function send(channel: ChannelName, data: any): void {
  const conn = connections.get(channel)
  if (!conn?.ws || conn.ws.readyState !== WebSocket.OPEN) return

  conn.ws.send(typeof data === 'string' ? data : JSON.stringify(data))
}

export function getChannelStatus(channel: ChannelName): 'connected' | 'disconnected' | 'error' | 'reconnecting' {
  const conn = connections.get(channel)
  if (!conn) return 'disconnected'
  if (conn.ws && conn.ws.readyState === WebSocket.OPEN) return 'connected'
  return conn.lastStatus
}
