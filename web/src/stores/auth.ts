import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { authApi } from '@/api'
import type { TokenData } from '@/api'

// LP-02: Token 不再持久化到 sessionStorage，改由 HttpOnly Cookie 存储。
// sessionStorage 仅保存非敏感的用户信息（username, role, 登录标志）。
// _getItem/_setItem 保留用于 CSRF token 和用户信息存储。
// FIXED-P0: 替换弃用的escape/unescape为TextEncoder/TextDecoder
function _encode(value: string): string {
  const bytes = new TextEncoder().encode(value)
  return btoa(String.fromCharCode(...bytes))
}
function _decode(value: string): string {
  try {
    const binary = atob(value)
    const bytes = Uint8Array.from(binary, c => c.charCodeAt(0))
    return new TextDecoder().decode(bytes)
  } catch { return '' }
}
export function _getItem(key: string): string {
  const raw = sessionStorage.getItem(key)
  if (!raw) return ''
  const decoded = _decode(raw)
  return decoded || raw
}
export function _setItem(key: string, value: string): void {
  sessionStorage.setItem(key, _encode(value))
}
export function _removeItem(key: string): void {
  sessionStorage.removeItem(key)
}

export const useAuthStore = defineStore('auth', () => {
  // LP-02: token 和 refreshToken 仅保存在内存中，不持久化到 sessionStorage
  // FIXED-T3: 移除 sessionStorage 回退，完全依赖 HttpOnly Cookie 认证
  const token = ref<string>('')
  const refreshToken = ref<string>('')
  const username = ref<string>(_getItem('edgelite_username'))
  const role = ref<string>(_getItem('edgelite_role'))
  const mustChangePassword = ref<boolean>(_getItem('edgelite_mustChangePassword') === 'true')

  // LP-02: isAuthenticated 不再依赖 token（token 在 HttpOnly Cookie 中，JS 不可读）
  // 改为检查 username（登录成功后保存到 sessionStorage）和登录标志
  const isAuthenticated = computed(() => !!username.value)
  const isAdmin = computed(() => role.value === 'admin')
  const isOperator = computed(() => role.value === 'operator' || role.value === 'admin')

  async function login(user: string, password: string) {
    const data: TokenData = await authApi.login({ username: user, password })
    // 清理旧的 sessionStorage Token（迁移到 HttpOnly Cookie 后不再需要）
    _removeItem('edgelite_token')
    _removeItem('edgelite_refresh')
    // LP-02: token 仅保存在内存中，不写入 sessionStorage（HttpOnly Cookie 由后端设置）
    token.value = data.access_token
    refreshToken.value = data.refresh_token
    username.value = user
    // 仅持久化非敏感的用户信息
    _setItem('edgelite_username', user)
    // 保存CSRF Token（如果登录响应中包含）
    if (data.csrf_token) {
      _setItem('edgelite_csrf_token', data.csrf_token)
    }
    await fetchUserInfo()
    // FIX 8: 登录成功后启动空闲会话超时监听
    startIdleWatch()
  }

  async function fetchUserInfo() {
    try {
      const data = await authApi.me()
      role.value = data.role || 'viewer'
      mustChangePassword.value = data.must_change_password ?? false
      _setItem('edgelite_role', role.value)
      _setItem('edgelite_mustChangePassword', String(mustChangePassword.value))
      // [AUDIT-FIX] 严重级-页面刷新后空闲会话超时失效：fetchUserInfo 由路由守卫在刷新时调用，
      // 此处恢复空闲监听，确保刷新后 30 分钟超时机制仍然生效。
      // startIdleWatch 内部 addEventListener 对相同函数引用去重，resetIdleTimer 先 clear 旧定时器，重复调用安全。
      startIdleWatch()
    } catch (e: any) {
      if (e?.response?.status === 401) {
        // FIXED-Severe: 401 时仅清理本地状态，不调用 logout API，避免 fetchUserInfo→logout→API 401 循环
        // logout API 在 token 已失效时也会返回 401，触发 HTTP 拦截器再次处理
        token.value = ''
        refreshToken.value = ''
        username.value = ''
        role.value = ''
        mustChangePassword.value = false
        _removeItem('edgelite_token')
        _removeItem('edgelite_refresh')
        _removeItem('edgelite_username')
        _removeItem('edgelite_role')
        _removeItem('edgelite_mustChangePassword')
        _removeItem('edgelite_csrf_token')
      } else {
        // 非 401 错误（网络错误/503等）：不清空 role，保留现有值
        // 避免后端短暂抖动导致路由守卫强制登出
        throw e
      }
    }
  }

  let isLoggingOut = false
  async function logout() {
    if (isLoggingOut) return  // 防止递归调用
    isLoggingOut = true
    try {
      try {
        await authApi.logout(refreshToken.value || undefined)
      } catch (e) {
        console.warn('Logout API failed:', e)
      }
      token.value = ''
      refreshToken.value = ''
      username.value = ''
      role.value = ''
      mustChangePassword.value = false
      // LP-02: 清除 sessionStorage 中的所有用户信息（包括旧 session 遗留的 token）
      _removeItem('edgelite_token')
      _removeItem('edgelite_refresh')
      _removeItem('edgelite_username')
      _removeItem('edgelite_role')
      _removeItem('edgelite_mustChangePassword')
      _removeItem('edgelite_csrf_token')
      // FIX 8: 登出时停止空闲会话超时监听
      stopIdleWatch()
    } finally {
      isLoggingOut = false
    }
  }


  // FIX 8: 客户端空闲会话超时（30分钟无操作自动登出）
  const IDLE_TIMEOUT = 30 * 60 * 1000
  let lastActivity = Date.now()
  let idleTimer: number | null = null

  function resetIdleTimer() {
    lastActivity = Date.now()
    if (idleTimer !== null) {
      window.clearTimeout(idleTimer)
    }
    idleTimer = window.setTimeout(() => {
      stopIdleWatch()
      logout()
    }, IDLE_TIMEOUT)
  }

  function startIdleWatch() {
    window.addEventListener('mousemove', resetIdleTimer)
    window.addEventListener('keydown', resetIdleTimer)
    window.addEventListener('click', resetIdleTimer)
    window.addEventListener('scroll', resetIdleTimer)
    resetIdleTimer()
  }

  function stopIdleWatch() {
    window.removeEventListener('mousemove', resetIdleTimer)
    window.removeEventListener('keydown', resetIdleTimer)
    window.removeEventListener('click', resetIdleTimer)
    window.removeEventListener('scroll', resetIdleTimer)
    if (idleTimer !== null) {
      window.clearTimeout(idleTimer)
      idleTimer = null
    }
  }

  return { token, refreshToken, username, role, mustChangePassword, isAuthenticated, isAdmin, isOperator, login, fetchUserInfo, logout }
})
