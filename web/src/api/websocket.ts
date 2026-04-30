/**
 * WebSocket连接管理器
 * 支持多通道连接、自动重连、Token认证
 */

type MessageHandler = (data: any) => void

interface WSConnection {
  ws: WebSocket | null
  reconnectTimer: ReturnType<typeof setTimeout> | null
  handlers: Set<MessageHandler>
}

const connections: Map<string, WSConnection> = new Map()

const WS_BASE = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}`

const CHANNELS = {
  realtime: '/ws/v1/realtime',
  alarm: '/ws/v1/alarm',
  device: '/ws/v1/device',
  integration: '/ws/v1/integration',
} as const

type ChannelName = keyof typeof CHANNELS

function getToken(): string {
  try {
    const auth = JSON.parse(localStorage.getItem('auth') || '{}')
    return auth.token || ''
  } catch {
    return ''
  }
}

function createConnection(channel: ChannelName): WSConnection {
  return {
    ws: null,
    reconnectTimer: null,
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
    }

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data)
        conn.handlers.forEach((handler) => {
          try { handler(data) } catch {}
        })
      } catch {}
    }

    ws.onclose = () => {
      conn.ws = null
      scheduleReconnect(channel)
    }

    ws.onerror = () => {
      ws.close()
    }

    conn.ws = ws
  } catch {
    scheduleReconnect(channel)
  }
}

function scheduleReconnect(channel: ChannelName): void {
  const conn = connections.get(channel)
  if (!conn) return

  if (conn.reconnectTimer) clearTimeout(conn.reconnectTimer)

  conn.reconnectTimer = setTimeout(() => {
    connectChannel(channel)
  }, 5000)
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
