import axios from 'axios'
import type { AxiosInstance, AxiosRequestConfig, AxiosResponse, AxiosError } from 'axios'
import { useAuthStore, _getItem, _setItem } from '@/stores/auth'
import { t } from '@/i18n'
import { getErrorMessage } from '@/utils/errorCodes'

// FIXED-Severe: 延迟导入 router 避免 401/403 时 window.location.href 全页面重载
// 全页面重载会丢失 Pinia 状态、断开 WebSocket、丢失用户正在填写的表单
async function redirectToLogin() {
  try {
    const router = (await import('@/router')).default
    const current = router.currentRoute.value.fullPath
    if (router.currentRoute.value.name !== 'Login') {
      router.push({ name: 'Login', query: { redirect: current } })
    }
  } catch {
    // 降级：router 加载失败时才使用 location 跳转
    window.location.href = '/login'
  }
}

// FIXED-OPTIMISTIC-LOCK: 扩展 AxiosError 类型以支持 409 冲突错误
declare module 'axios' {
  interface AxiosError {
    isConflictError?: boolean
    conflictMessage?: string
  }
  interface AxiosRequestConfig {
    _retry?: boolean
    _csrfRetry?: boolean
    _rateLimitRetry?: boolean
  }
}

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
  // #[AUDIT-FIX] 用户重新登录时重置 refreshFailed 标志位
  // 确保新 session 的 token 过期时仍会正常尝试 refresh
  if (config.url === '/auth/login') {
    refreshFailed = false
  }
  // LP-02: 认证完全依赖 HttpOnly Cookie（withCredentials 自动携带）
  // FIXED-Severe: 移除 sessionStorage token 回退，避免 XSS 窃取 token
  const csrfToken = _getItem('edgelite_csrf_token')
  if (csrfToken && ['post', 'put', 'patch', 'delete'].includes((config.method || '').toLowerCase())) {
    config.headers['X-CSRF-Token'] = csrfToken
  }
  return config
})

const REFRESH_TIMEOUT_MS = 10000

let isRefreshing = false
// #[AUDIT-FIX] refresh 失败后标记，防止页面跳转完成前再次触发 refresh
// 原问题：refresh 失败 → isRefreshing=false → 跳转未完成时新 401 又触发 refresh →
// 此时 refresh token 已被 logout 吊销 → 再次 401，形成无意义的重复请求
let refreshFailed = false
// 记录 refreshFailed 设置的时间，10秒后自动重置以应对瞬时网络故障
let refreshFailedTime = 0
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
    // Token自动续期：检查 X-New-Access-Token 响应头
    const newAccessToken = response.headers['x-new-access-token']
    if (newAccessToken && typeof newAccessToken === 'string') {
      const auth = useAuthStore()
      auth.token = newAccessToken
      // LP-02: 不再写入 sessionStorage，token 仅保存在内存中
      // HttpOnly Cookie 由后端 TokenRenewalMiddleware 自动更新
    }
    // 保存CSRF Token：从响应头或响应体中提取
    // Axios将响应头名称统一转为小写，无需大写查找
    const csrfFromHeader = response.headers['x-csrf-token']
    if (csrfFromHeader && typeof csrfFromHeader === 'string') {
      _setItem('edgelite_csrf_token', csrfFromHeader)
    }
    // 响应体中的csrf_token可能在 data 字段内（如登录/刷新响应）
    const body = response.data
    if (body && typeof body === 'object') {
      const csrfFromBody = body.csrf_token || body.data?.csrf_token
      if (csrfFromBody && typeof csrfFromBody === 'string') {
        _setItem('edgelite_csrf_token', csrfFromBody)
      }
    }
    if (response.config.responseType === 'blob') {
      return response
    }
    const data = response.data
    // FIXED: 原问题-拦截器code校验可能与后端不一致，现统一判断逻辑
    // 后端约定 code=0 表示成功，非0表示业务错误；无code字段视为非标准响应直接放行
    if (data && typeof data === 'object' && 'code' in data && data.code !== 0) {
      const bizError: any = new Error(data.message || t('http.requestFailed'))
      bizError.isBusinessError = true
      bizError.code = data.code
      bizError.data = data
      bizError.response = response
      return Promise.reject(bizError)
    }
    return response
  },
  async (error) => {
    const originalRequest = error.config as AxiosRequestConfig & { _retry?: boolean }
    const status = error.response?.status
    const respData = error.response?.data

    // 统一错误消息提取：后端现在返回 {code, message, data, error_code} 格式
    // 同时兼容旧格式 {"detail": "..."} 和 FastAPI 默认 422 格式
    let errorMessage = ''
    let errorCode = ''

    if (respData && typeof respData === 'object') {
      // 新格式：{code, message, data, error_code}
      if ('error_code' in respData && typeof respData.error_code === 'string') {
        errorCode = respData.error_code
        errorMessage = getErrorMessage(errorCode)
      }
      if ('message' in respData && typeof respData.message === 'string') {
        // 如果 error_code 已翻译成功（翻译结果不等于原始错误码），优先使用翻译结果
        // 否则：message 以 ERR_ 开头时翻译，否则直接使用
        if (!errorCode) {
          if (respData.message.startsWith('ERR_')) {
            errorCode = respData.message
            errorMessage = getErrorMessage(respData.message)
          } else {
            errorMessage = respData.message
          }
        } else if (!errorMessage || errorMessage === errorCode) {
          // error_code 翻译失败时，尝试翻译 message
          errorMessage = respData.message.startsWith('ERR_')
            ? getErrorMessage(respData.message)
            : respData.message
        }
      }
      // 旧格式兼容：{"detail": "ERR_XXX"} 或 {"detail": {error_code, errors, warnings}}
      if (!errorMessage && 'detail' in respData && respData.detail !== undefined) {
        const detail = respData.detail
        if (typeof detail === 'string') {
          if (detail.startsWith('ERR_')) {
            errorCode = detail
            errorMessage = getErrorMessage(detail)
          } else {
            errorMessage = detail
          }
        } else if (typeof detail === 'object' && detail !== null) {
          if (typeof detail.error_code === 'string' && detail.error_code.startsWith('ERR_')) {
            errorCode = detail.error_code
            const translatedCode = getErrorMessage(detail.error_code)
            const errorList = Array.isArray(detail.errors) ? detail.errors : []
            const warningList = Array.isArray(detail.warnings) ? detail.warnings : []
            const parts: string[] = []
            if (translatedCode !== detail.error_code) parts.push(translatedCode)
            if (errorList.length > 0) parts.push(...errorList)
            if (warningList.length > 0) parts.push(...warningList)
            errorMessage = parts.length > 0 ? parts.join('\n') : translatedCode
          } else if (typeof detail.message === 'string' && detail.message.startsWith('ERR_')) {
            errorCode = detail.message
            const translatedMsg = getErrorMessage(detail.message)
            const hint = detail.hint ? `\n${detail.hint}` : ''
            errorMessage = `${translatedMsg}${hint}`
          } else {
            errorMessage = JSON.stringify(detail)
          }
        }
      }
    }

    // 将提取的错误信息附加到 error 对象上，便于调用方使用
    if (errorMessage) {
      error.userMessage = errorMessage
    }
    if (errorCode) {
      error.errorCode = errorCode
    }

    if (status === 401 && originalRequest.url !== '/auth/refresh' && originalRequest.url !== '/auth/login' && originalRequest.url !== '/auth/logout') {
      const auth = useAuthStore()

      if (!auth.username) {
        auth.logout()
        redirectToLogin()
        return Promise.reject(error)
      }

      // #[AUDIT-FIX] refresh 已失败时，直接 logout，不再尝试 refresh
      // 防止页面跳转完成前的 401 请求再次触发无意义的 refresh
      if (refreshFailed || originalRequest._retry) {
        // 10秒超时后重置，允许重新尝试 refresh（应对瞬时网络故障）
        if (refreshFailed && Date.now() - refreshFailedTime > 10000) {
          refreshFailed = false
          // 不 return，继续走正常 refresh 流程
        } else {
          refreshFailed = false  // 重置标志位，供下次登录使用
          auth.logout()
          redirectToLogin()
          return Promise.reject(error)
        }
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
        // LP-02: token 仅保存在内存中，不写入 sessionStorage
        // HttpOnly Cookie 由后端 /auth/refresh 端点自动设置
        isRefreshing = false
        refreshFailed = false  // refresh 成功，重置标志位
        onTokenRefreshed(tokenData.access_token)
        if (originalRequest.headers) {
          originalRequest.headers['Authorization'] = `Bearer ${tokenData.access_token}`
        }
        return http(originalRequest)
      } catch (refreshError) {
        isRefreshing = false
        refreshFailed = true  // #[AUDIT-FIX] 标记 refresh 失败，阻止后续 401 再次触发 refresh
        refreshFailedTime = Date.now()  // 记录失败时间，10秒后允许重试
        onTokenRefreshed(null)
        auth.logout()
        redirectToLogin()
        return Promise.reject(refreshError)
      }
    }

    if (status === 403) {
      if (originalRequest.url === '/auth/login') {
        return Promise.reject(error)
      }
      // CSRF token 失败时，后端会在403响应中返回新的csrf_token，直接保存并重试
      if (respData?.error_code === 'ERR_AUTH_CSRF_FAILED' && !originalRequest._csrfRetry) {
        originalRequest._csrfRetry = true
        // 优先从403响应中提取新CSRF token（后端在验证失败时生成新token返回）
        const csrfFromErrorHeader = error.response?.headers?.['x-csrf-token']
        const csrfFromErrorBody = respData?.csrf_token
        const newCsrfToken = csrfFromErrorHeader || csrfFromErrorBody
        if (newCsrfToken && typeof newCsrfToken === 'string') {
          _setItem('edgelite_csrf_token', newCsrfToken)
          // 重试原始请求（请求拦截器会自动附加新的 CSRF token）
          return http(originalRequest)
        }
        // 如果403响应中没有新token，尝试通过refresh获取
        try {
          const auth = useAuthStore()
          // LP-02: refreshToken 可能为空（Cookie 模式），后端会从 Cookie 读取
          const tokenData = await refreshAuthToken(auth.refreshToken || '')
          auth.token = tokenData.access_token
          auth.refreshToken = tokenData.refresh_token
          // LP-02: token 仅保存在内存中，不写入 sessionStorage
          if (originalRequest.headers) {
            originalRequest.headers['Authorization'] = `Bearer ${tokenData.access_token}`
          }
          return http(originalRequest)
        } catch {
          // 刷新失败，继续正常 403 处理
        }
      }
      const auth = useAuthStore()
      if (!auth.username) {
        auth.logout()
        redirectToLogin()
        return Promise.reject(error)
      }
      return Promise.reject(new Error(errorMessage || t('http.permissionDenied')))
    }

    // FIXED-OPTIMISTIC-LOCK: 乐观锁冲突处理（409 Conflict）
    if (status === 409) {
      error.isConflictError = true
      error.conflictMessage = errorMessage || t('http.dataConflict')
      return Promise.reject(error)
    }

    // FIXED: 429 Too Many Requests 处理
    // 已认证用户不应被 Rate Limit（后端已跳过），此处作为安全兜底
    // 未认证请求（如登录）被限流时，显示友好提示
    if (status === 429) {
      if (!originalRequest._rateLimitRetry) {
        const auth = useAuthStore()
        if (auth.isAuthenticated) {
          originalRequest._rateLimitRetry = true
          await new Promise(resolve => setTimeout(resolve, 2000))
          return http(originalRequest)
        }
      }
      error.userMessage = t('http.tooManyRequests')
      return Promise.reject(error)
    }

    // 网络错误兜底：无 response 时（断网/超时/DNS失败），提供友好提示
    if (!error.response) {
      const netMsg = error.code === 'ECONNABORTED'
        ? t('http.timeout')
        : t('http.networkError')
      error.userMessage = netMsg
      return Promise.reject(error)
    }

    // 对于其他错误，如果有提取到的错误消息，包装为可读的 Error
    if (errorMessage) {
      error.userMessage = errorMessage
    }

    return Promise.reject(error)
  }
)

export default http
