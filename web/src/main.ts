import { createApp } from 'vue'
import { createPinia } from 'pinia'
import naive from 'naive-ui'
import App from './App.vue'
import router from './router'
import { setupLocale, initLocale } from './i18n'
import zhCN from './i18n/zh-CN'
import enUS from './i18n/en-US'

setupLocale('zh-CN', zhCN)
setupLocale('en-US', enUS)
initLocale()

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.use(naive)
app.mount('#app')
