import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { authApi } from '@/api'

export const useAuthStore = defineStore('auth', () => {
  const username = ref<string>(sessionStorage.getItem('edgelite_username') || '')
  const role = ref<string>(sessionStorage.getItem('edgelite_role') || '')
  const mustChangePassword = ref<boolean>(sessionStorage.getItem('edgelite_mustChangePassword') === 'true')

  const isAuthenticated = computed(() => !!username.value)
  const isAdmin = computed(() => role.value === 'admin')
  const isOperator = computed(() => role.value === 'operator' || role.value === 'admin')

  async function login(user: string, password: string) {
    await authApi.login({ username: user, password })
    username.value = user
    sessionStorage.setItem('edgelite_username', user)
    await fetchUserInfo()
  }

  async function fetchUserInfo() {
    try {
      const data = await authApi.me()
      role.value = data.role || 'viewer'
      mustChangePassword.value = data.must_change_password ?? false
      sessionStorage.setItem('edgelite_role', role.value)
      sessionStorage.setItem('edgelite_mustChangePassword', String(mustChangePassword.value))
    } catch {
      role.value = 'viewer'
      mustChangePassword.value = false
    }
  }

  async function logout() {
    try {
      await authApi.logout()
    } catch { /* ignore */ }
    username.value = ''
    role.value = ''
    mustChangePassword.value = false
    sessionStorage.removeItem('edgelite_username')
    sessionStorage.removeItem('edgelite_role')
    sessionStorage.removeItem('edgelite_mustChangePassword')
  }

  return { username, role, mustChangePassword, isAuthenticated, isAdmin, isOperator, login, fetchUserInfo, logout }
})
