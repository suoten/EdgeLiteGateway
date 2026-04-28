import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { authApi } from '@/api'
import type { TokenData } from '@/api'

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string>(localStorage.getItem('edgelite_token') || '')
  const refreshToken = ref<string>(localStorage.getItem('edgelite_refresh') || '')
  const username = ref<string>(localStorage.getItem('edgelite_username') || '')
  const role = ref<string>(localStorage.getItem('edgelite_role') || '')

  const isAuthenticated = computed(() => !!token.value)
  const isAdmin = computed(() => role.value === 'admin')
  const isOperator = computed(() => role.value === 'operator' || role.value === 'admin')

  async function login(user: string, password: string) {
    const data: TokenData = await authApi.login({ username: user, password })
    token.value = data.access_token
    refreshToken.value = data.refresh_token
    username.value = user
    // 解析角色（从JWT payload）
    try {
      const b64 = data.access_token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')
      const pad = b64.length % 4
      const payload = JSON.parse(atob(pad ? b64 + '='.repeat(4 - pad) : b64))
      role.value = payload.role || 'viewer'
    } catch {
      role.value = 'viewer'
    }
    localStorage.setItem('edgelite_token', data.access_token)
    localStorage.setItem('edgelite_refresh', data.refresh_token)
    localStorage.setItem('edgelite_username', user)
    localStorage.setItem('edgelite_role', role.value)
  }

  function logout() {
    token.value = ''
    refreshToken.value = ''
    username.value = ''
    role.value = ''
    localStorage.removeItem('edgelite_token')
    localStorage.removeItem('edgelite_refresh')
    localStorage.removeItem('edgelite_username')
    localStorage.removeItem('edgelite_role')
  }

  return { token, refreshToken, username, role, isAuthenticated, isAdmin, isOperator, login, logout }
})
