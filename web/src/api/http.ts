import axios from 'axios'
import type { AxiosInstance, AxiosRequestConfig, AxiosResponse } from 'axios'
import { useAuthStore } from '@/stores/auth'
import { authApi } from './index'

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
  baseURL: '/api/v1',
  timeout: 15000,
  headers: { 'Content-Type': 'application/json' },
})

// 请求拦截器：注入Token
http.interceptors.request.use((config) => {
  const auth = useAuthStore()
  if (auth.token) {
    config.headers.Authorization = `Bearer ${auth.token}`
  }
  return config
})

// 是否正在刷新Token
let isRefreshing = false
// 等待刷新完成的请求队列
let refreshSubscribers: ((token: string) => void)[] = []

function onTokenRefreshed(token: string) {
  refreshSubscribers.forEach((cb) => cb(token))
  refreshSubscribers = []
}

function addRefreshSubscriber(cb: (token: string) => void) {
  refreshSubscribers.push(cb)
}

// 响应拦截器：处理401、错误码和Token自动刷新
http.interceptors.response.use(
  (response: AxiosResponse<ApiResponse>) => {
    const data = response.data
    if (data.code !== 0 && data.code !== undefined) {
      return Promise.reject(new Error(data.message || '请求失败'))
    }
    return response
  },
  async (error) => {
    const originalRequest = error.config as AxiosRequestConfig & { _retry?: boolean }

    // 401 且不是刷新请求本身
    if (error.response?.status === 401 && originalRequest.url !== '/auth/refresh') {
      const auth = useAuthStore()

      if (!auth.refreshToken) {
        auth.logout()
        window.location.href = '/login'
        return Promise.reject(error)
      }

      // 正在刷新中，加入队列等待
      if (isRefreshing) {
        return new Promise((resolve) => {
          addRefreshSubscriber((token: string) => {
            originalRequest.headers = originalRequest.headers || {}
            originalRequest.headers.Authorization = `Bearer ${token}`
            resolve(http(originalRequest))
          })
        })
      }

      // 开始刷新
      isRefreshing = true
      originalRequest._retry = true

      try {
        const data = await authApi.refresh(auth.refreshToken)
        auth.token = data.access_token
        auth.refreshToken = data.refresh_token
        localStorage.setItem('edgelite_token', data.access_token)
        localStorage.setItem('edgelite_refresh', data.refresh_token)

        onTokenRefreshed(data.access_token)
        isRefreshing = false

        // 重试原请求
        originalRequest.headers = originalRequest.headers || {}
        originalRequest.headers.Authorization = `Bearer ${data.access_token}`
        return http(originalRequest)
      } catch (refreshError) {
        isRefreshing = false
        auth.logout()
        window.location.href = '/login'
        return Promise.reject(refreshError)
      }
    }

    return Promise.reject(error)
  }
)

export default http
