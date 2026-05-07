/**
 * WebSocket连接管理器
 * 支持多通道连接、自动重连(指数退避)、Token认证
 */

type MessageHandler = (data: any) => void

interface WSConnection {
  ws: WebSocket | null
  reconnectTimer: ReturnType<typeof setTimeout> | null
  reconnectAttempt: number
  handlers: Set<MessageHandler>
}

const connections: Map<string, WSConnection> = new Map()

const WS_BASE = import.meta.env.VITE_WS_BASE_URL
  || `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}`

const CHANNELS = {
  realtime: '/realtime',
  alarm: '/alarm',
  device: '/device',
  integration: '/integration',
} as const

type ChannelName = keyof typeof CHANNELS

const MAX_RECONNECT_DELAY = 30000
const BASE_RECONNECT_DELAY = 1000

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
  }
}

function connectChannel(channel: ChannelName): void {
  const conn = connections.get(channel)
  if (!conn) return

  const token = getToken()
  if (!token) return

  const url = `${WS_BASE}${CHANNELS[channel]}?token=${token}`

  try {
    const ws = new WebSocket(url)

    ws.onopen = () => {
      if (conn.reconnectTimer) {
        clearTimeout(conn.reconnectTimer)
        conn.reconnectTimer = null
      }
      conn.reconnectAttempt = 0
    }

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data)
        conn.handlers.forEach((handler) => {
          try { handler(data) } catch (e) { console.error('[WS] 消息处理器异常:', e) }
        })
      } catch (e) { console.error('[WS] 消息解析失败:', e) }
    }

    ws.onclose = () => {
      conn.ws = null
      scheduleReconnect(channel)
    }

    ws.onerror = () => {
      ws.close()
    }

    conn.ws = ws
  } catch (e) {
    console.error('[WS] 连接创建失败:', e)
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
  connections.delete(channel)
}

export function send(channel: ChannelName, data: any): void {
  const conn = connections.get(channel)
  if (!conn?.ws || conn.ws.readyState !== WebSocket.OPEN) return

  conn.ws.send(typeof data === 'string' ? data : JSON.stringify(data))
}
