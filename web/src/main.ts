import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import { setupLocale, initLocale } from './i18n'
import zhCN from './i18n/zh-CN'
import enUS from './i18n/en-US'
import { useAuthStore } from './stores/auth'

// [AUDIT-FIX] 建议级-移除 window.notice.avoidance 全局 stub，避免污染 window 对象
// 浏览器扩展导致的 "notice.avoidance is not a function" 错误不应由应用代码处理

setupLocale('zh-CN', zhCN)
setupLocale('en-US', enUS)
initLocale()

import naive from 'naive-ui'

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.use(naive)

// FIXED-ErrorBoundary: 设置 Vue 全局错误处理器，防止组件异常导致白屏
// 错误提示用 console.error 记录，避免在初始化阶段依赖 notification（可能未就绪）
app.config.errorHandler = (err, _instance, info) => {
  console.error('[Vue Error]', err, info)
}

// 捕获未处理的 Promise rejection
window.addEventListener('unhandledrejection', (event) => {
  console.error('[Unhandled Promise Rejection]', event.reason)
})

// 捕获全局错误（脚本异常、资源加载失败等）
window.addEventListener('error', (event) => {
  console.error('[Global Error]', event.message, event.error)
})

// [AUDIT-FIX] 严重级-页面刷新时强制验证后端会话，防止 sessionStorage 篡改绕过权限。
// sessionStorage 中的 edgelite_role 可被攻击者伪造，仅靠路由守卫的 !auth.role 判断无法防御。
// 此处在 mount 前对已认证会话强制调用一次后端 /me 验证，由 fetchUserInfo 内部处理 401 清理。
;(async () => {
  const auth = useAuthStore()
  if (auth.isAuthenticated) {
    try {
      await auth.fetchUserInfo()
    } catch {
      // fetchUserInfo 内部已处理 401 清理逻辑；非 401 错误保留现有 role，避免后端抖动踢人
    }
  }
  app.mount('#app')
})()
