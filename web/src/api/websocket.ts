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

function getToken(): string {
  try {
    return sessionStorage.getItem('edgelite_token') || ''
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

  const url = `${WS_BASE}${CHANNELS[channel]}?token=${token}`

  try {
    const ws = new WebSocket(url)

    ws.onopen = () => {
      if (conn.reconnectTimer) {
        clearTimeout(conn.reconnectTimer)
        conn.reconnectTimer = null
      }
      conn.reconnectAttempt = 0
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
      // FIXED: 原问题-onclose无条件重连，Token无效时(4001)仍指数退避重连浪费资源
      // 现区分关闭原因：认证失败不自动重连，其他原因正常重连
      if (event.code === WS_AUTH_CLOSE_CODE) {
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
