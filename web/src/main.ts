import { createApp } from 'vue'
import { createPinia } from 'pinia'
import naive from 'naive-ui'
import App from './App.vue'
import router from './router'
import { setupLocale } from './i18n'
import zhCN from './i18n/zh-CN'

// FIXED: 原问题-中文文本硬编码散布在代码中，现建立i18n框架集中管理
setupLocale('zh-CN', zhCN)

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.use(naive)
app.mount('#app')
