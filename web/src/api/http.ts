import axios from 'axios'
import type { AxiosInstance, AxiosRequestConfig, AxiosResponse } from 'axios'
import { useAuthStore } from '@/stores/auth'
import { t } from '@/i18n'

export interface ApiResponse<T = any> {
  code: number
  message: string
  data: T
  warning?: string
}

export interface PagedData<T = any> {
  code: number  // FIXED: 原问题-缺少后端PagedResponse中的code字段
  message: string  // FIXED: 原问题-缺少后端PagedResponse中的message字段
  data: T[]
  total: number
  page: number
  size: number
}

// FIXED: 原问题-超时时间硬编码为魔法数字，现提取为命名常量
const HTTP_TIMEOUT_MS = 15000

const http: AxiosInstance = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || '/api/v1',
  timeout: HTTP_TIMEOUT_MS,
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

const REFRESH_TIMEOUT_MS = 10000

let isRefreshing = false
let refreshSubscribers: ((token: string | null) => void)[] = []

function onTokenRefreshed(token: string | null) {
  refreshSubscribers.forEach((cb) => cb(token))
  refreshSubscribers = []
}

function addRefreshSubscriber(cb: (token: string | null) => void) {
  refreshSubscribers.push(cb)
  // FIXED: 原问题-排队请求无超时机制，刷新挂起时页面永久卡死，现添加超时自动拒绝
  setTimeout(() => {
    const idx = refreshSubscribers.indexOf(cb)
    if (idx !== -1) {
      refreshSubscribers.splice(idx, 1)
      cb(null)
    }
  }, REFRESH_TIMEOUT_MS)
}

async function refreshAuthToken(refreshToken: string): Promise<{ access_token: string; refresh_token: string }> {
  // FIXED: 原问题-Token刷新响应解析逻辑脆弱(body.data ?? body as any)，
  // 现严格校验data字段存在且包含access_token
  const resp = await http.post<ApiResponse<{ access_token: string; refresh_token: string }>>(
    '/auth/refresh',
    { refresh: refreshToken }
  )
  const body = resp.data
  const tokenData = body?.data
  if (!tokenData || !tokenData.access_token) {
    throw new Error('Token refresh response missing access_token')
  }
  return tokenData
}

http.interceptors.response.use(
  (response: AxiosResponse) => {
    if (response.config.responseType === 'blob') {
      return response
    }
    const data = response.data
    // FIXED: 原问题-拦截器code校验可能与后端不一致，现统一判断逻辑
    // 后端约定 code=0 表示成功，非0表示业务错误；无code字段视为非标准响应直接放行
    if (data && typeof data === 'object' && 'code' in data && data.code !== 0) {
      // FIXED: 原问题-业务错误reject的Error无response对象，调用方e?.response?.status为undefined
      const bizError: any = new Error(data.message || t('http.requestFailed'))
      bizError.isBusinessError = true
      bizError.code = data.code
      bizError.data = data
      return Promise.reject(bizError)
    }
    return response
  },
  async (error) => {
    const originalRequest = error.config as AxiosRequestConfig & { _retry?: boolean }
    const status = error.response?.status

    if (status === 401 && originalRequest.url !== '/auth/refresh' && originalRequest.url !== '/auth/login') {
      const auth = useAuthStore()

      if (!auth.username) {
        auth.logout()
        window.location.href = '/login'
        return Promise.reject(error)
      }

      if (originalRequest._retry) {
        // FIXED: 原问题-刷新后重试仍401时仅检查username可能不登出导致死循环
        // 现无论username是否存在，二次401均强制登出
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
            if (originalRequest.headers) {
              originalRequest.headers['Authorization'] = `Bearer ${token}`
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
        if (originalRequest.headers) {
          originalRequest.headers['Authorization'] = `Bearer ${tokenData.access_token}`
        }
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
      if (originalRequest.url === '/auth/login') {
        return Promise.reject(error)
      }
      const auth = useAuthStore()
      if (!auth.username) {
        auth.logout()
        window.location.href = '/login'
        return Promise.reject(error)
      }
      return Promise.reject(new Error(error.response?.data?.detail || t('http.permissionDenied')))
    }

    return Promise.reject(error)
  }
)

export default http
