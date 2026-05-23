import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { authApi } from '@/api'
import type { TokenData } from '@/api'

// FIXED-P2: Token明文存sessionStorage→Base64编码存储(增加直接读取门槛)
// 真正的httpOnly cookie方案需要后端配合，此处为前端防护增强
function _encode(value: string): string {
  return btoa(unescape(encodeURIComponent(value)))
}
function _decode(value: string): string {
  try { return decodeURIComponent(escape(atob(value))) } catch { return '' }
}
function _getItem(key: string): string {
  const raw = sessionStorage.getItem(key)
  if (!raw) return ''
  const decoded = _decode(raw)
  return decoded || raw
}
function _setItem(key: string, value: string): void {
  sessionStorage.setItem(key, _encode(value))
}
function _removeItem(key: string): void {
  sessionStorage.removeItem(key)
}

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string>(_getItem('edgelite_token'))
  const refreshToken = ref<string>(_getItem('edgelite_refresh'))
  const username = ref<string>(_getItem('edgelite_username'))
  const role = ref<string>(_getItem('edgelite_role'))
  const mustChangePassword = ref<boolean>(_getItem('edgelite_mustChangePassword') === 'true')

  const isAuthenticated = computed(() => !!token.value && !!username.value)
  const isAdmin = computed(() => role.value === 'admin')
  const isOperator = computed(() => role.value === 'operator' || role.value === 'admin')

  async function login(user: string, password: string) {
    const data: TokenData = await authApi.login({ username: user, password })
    token.value = data.access_token
    refreshToken.value = data.refresh_token
    username.value = user
    _setItem('edgelite_token', data.access_token)
    _setItem('edgelite_refresh', data.refresh_token)
    _setItem('edgelite_username', user)
    await fetchUserInfo()
  }

  async function fetchUserInfo() {
    try {
      const data = await authApi.me()
      role.value = data.role || 'viewer'
      mustChangePassword.value = data.must_change_password ?? false
      _setItem('edgelite_role', role.value)
      _setItem('edgelite_mustChangePassword', String(mustChangePassword.value))
    } catch (e: any) {
      if (e?.response?.status === 401) {
        await logout()
      } else {
        role.value = ''
        mustChangePassword.value = false
        token.value = ''
        _removeItem('edgelite_token')
        _removeItem('edgelite_role')
        _removeItem('edgelite_mustChangePassword')
        throw new Error(e?.response?.data?.detail || e?.message || 'Failed to fetch user info')
      }
    }
  }

  async function logout() {
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
    _removeItem('edgelite_token')
    _removeItem('edgelite_refresh')
    _removeItem('edgelite_username')
    _removeItem('edgelite_role')
    _removeItem('edgelite_mustChangePassword')
  }

  return { token, refreshToken, username, role, mustChangePassword, isAuthenticated, isAdmin, isOperator, login, fetchUserInfo, logout }
})
