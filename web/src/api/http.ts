import axios from 'axios'
import type { AxiosInstance, AxiosRequestConfig, AxiosResponse } from 'axios'
import { useAuthStore } from '@/stores/auth'

export interface ApiResponse<T = any> {
  code: number
  message: string
  data: T
}

export interface PagedData<T = any> {
  data: T[]
  total: number
  page: number
  size: number
}

const http: AxiosInstance = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || '/api/v1',
  timeout: 15000,
  headers: { 'Content-Type': 'application/json' },
  withCredentials: true,
})

http.interceptors.request.use((config) => {
  config.withCredentials = true
  const token = sessionStorage.getItem('edgelite_token')
  if (token) {
    config.headers['Authorization'] = `Bearer ${token}`
  }
  return config
})

let isRefreshing = false
let refreshSubscribers: ((token: string | null) => void)[] = []

function onTokenRefreshed(token: string | null) {
  refreshSubscribers.forEach((cb) => cb(token))
  refreshSubscribers = []
}

function addRefreshSubscriber(cb: (token: string | null) => void) {
  refreshSubscribers.push(cb)
}

async function refreshAuthToken(refreshToken: string): Promise<{ access_token: string; refresh_token: string }> {
  const resp = await http.post<ApiResponse<{ access_token: string; refresh_token: string }>>(
    '/auth/refresh',
    { refresh: refreshToken }
  )
  const data = resp.data
  return (data as any)?.data || data
}

http.interceptors.response.use(
  (response: AxiosResponse) => {
    if (response.config.responseType === 'blob') {
      return response
    }
    const data = response.data
    if (data && typeof data === 'object' && data.code !== 0 && data.code !== undefined) {
      return Promise.reject(new Error(data.message || '请求失败'))
    }
    return response
  },
  async (error) => {
    const originalRequest = error.config as AxiosRequestConfig & { _retry?: boolean }
    const status = error.response?.status

    if (status === 401 && originalRequest.url !== '/auth/refresh') {
      const auth = useAuthStore()

      if (!auth.username) {
        auth.logout()
        window.location.href = '/login'
        return Promise.reject(error)
      }

      if (originalRequest._retry) {
        auth.logout()
        window.location.href = '/login'
        return Promise.reject(error)
      }

      if (isRefreshing) {
        return new Promise((resolve, reject) => {
          addRefreshSubscriber((token: string | null) => {
            if (!token) {
              reject(error)
              return
            }
            resolve(http(originalRequest))
          })
        })
      }

      isRefreshing = true
      originalRequest._retry = true

      try {
        const tokenData = await refreshAuthToken(auth.refreshToken || '')
        auth.token = tokenData.access_token
        auth.refreshToken = tokenData.refresh_token
        sessionStorage.setItem('edgelite_token', tokenData.access_token)
        sessionStorage.setItem('edgelite_refresh', tokenData.refresh_token)
        isRefreshing = false
        onTokenRefreshed(tokenData.access_token)
        return http(originalRequest)
      } catch (refreshError) {
        isRefreshing = false
        onTokenRefreshed(null)
        auth.logout()
        window.location.href = '/login'
        return Promise.reject(refreshError)
      }
    }

    if (status === 403) {
      const auth = useAuthStore()
      if (!auth.username) {
        auth.logout()
        window.location.href = '/login'
        return Promise.reject(error)
      }
      return Promise.reject(new Error(error.response?.data?.detail || '权限不足，无法执行此操作'))
    }

    return Promise.reject(error)
  }
)

export default http
