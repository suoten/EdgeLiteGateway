/**
 * WebSocket连接管理器
 * 支持多通道连接、自动重连(指数退避)、Token认证
 */

type MessageHandler = (data: any) => void
type StatusHandler = (status: 'connected' | 'disconnected' | 'error', reason?: string) => void

interface WSConnection {
  ws: WebSocket | null
  reconnectTimer: ReturnType<typeof setTimeout> | null
  reconnectAttempt: number
  handlers: Set<MessageHandler>
  statusHandlers: Set<StatusHandler>
}

const connections: Map<string, WSConnection> = new Map()

const WS_BASE = import.meta.env.VITE_WS_BASE_URL
  || `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}`

const CHANNELS = {
  realtime: '/ws/v1/realtime',
  alarm: '/ws/v1/alarm',
  device: '/ws/v1/device',
  integration: '/ws/v1/integration',
} as const

type ChannelName = keyof typeof CHANNELS

const MAX_RECONNECT_DELAY = 30000
const BASE_RECONNECT_DELAY = 1000
const WS_AUTH_CLOSE_CODE = 4001
// FIXED: 原问题-重连无上限，添加最大重连次数常量
const MAX_RECONNECT_ATTEMPTS = 10

function getToken(): string {
  try {
    const raw = sessionStorage.getItem('edgelite_token') || ''
    if (!raw) return ''
    // FIXED-P2: Token已Base64编码存储，需解码
    try { return decodeURIComponent(escape(atob(raw))) } catch { return raw }
  } catch {
    return ''
  }
}

function getReconnectDelay(attempt: number): number {
  const delay = BASE_RECONNECT_DELAY * Math.pow(2, attempt)
  return Math.min(delay, MAX_RECONNECT_DELAY)
}

function createConnection(channel: ChannelName): WSConnection {
  return {
    ws: null,
    reconnectTimer: null,
    reconnectAttempt: 0,
    handlers: new Set(),
    statusHandlers: new Set(),
  }
}

function notifyStatus(conn: WSConnection, status: 'connected' | 'disconnected' | 'error', reason?: string) {
  conn.statusHandlers.forEach((handler) => {
    try { handler(status, reason) } catch (e) { console.error('[WS] Status handler error:', e) }
  })
}

function connectChannel(channel: ChannelName): void {
  const conn = connections.get(channel)
  if (!conn) return

  const token = getToken()
  // FIXED: 原问题-无Token时静默返回不通知调用方，用户无法感知实时数据断开
  // 现通知所有status监听者，调用方可据此显示提示
  if (!token) {
    notifyStatus(conn, 'error', 'No authentication token')
    scheduleReconnect(channel)
    return
  }

  // FIXED-P2: Token不再通过URL查询参数传输(防止日志/Referer泄露)，改为连接后首帧认证
  const url = `${WS_BASE}${CHANNELS[channel]}`

  try {
    const ws = new WebSocket(url)

    ws.onopen = () => {
      if (conn.reconnectTimer) {
        clearTimeout(conn.reconnectTimer)
        conn.reconnectTimer = null
      }
      conn.reconnectAttempt = 0
      // FIXED-P2: 连接建立后首帧发送Token认证，替代URL参数
      try {
        ws.send(JSON.stringify({ type: 'auth', token }))
      } catch (e) { console.error('[WS] Auth frame send failed:', e) }
      notifyStatus(conn, 'connected')
    }

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data)
        conn.handlers.forEach((handler) => {
          try { handler(data) } catch (e) { console.error('[WS] Message handler error:', e) }
        })
      } catch (e) { console.error('[WS] Message parse error:', e) }
    }

    ws.onclose = (event) => {
      conn.ws = null
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
    return
  }

  if (conn.reconnectTimer) clearTimeout(conn.reconnectTimer)

  const delay = getReconnectDelay(conn.reconnectAttempt)
  conn.reconnectAttempt++

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
    connectChannel(channel)
  }
}

export function onStatus(channel: ChannelName, handler: StatusHandler): void {
  if (!connections.has(channel)) {
    connections.set(channel, createConnection(channel))
  }
  connections.get(channel)!.statusHandlers.add(handler)
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

  if (conn.ws) {
    conn.ws.onclose = null
    conn.ws.close()
    conn.ws = null
  }

  conn.handlers.clear()
  conn.statusHandlers.clear()
  connections.delete(channel)
}

export function send(channel: ChannelName, data: any): void {
  const conn = connections.get(channel)
  if (!conn?.ws || conn.ws.readyState !== WebSocket.OPEN) return

  conn.ws.send(typeof data === 'string' ? data : JSON.stringify(data))
}
