<template>
  <n-config-provider :theme="theme" :locale="naiveLocale" :date-locale="naiveDateLocale">
    <a href="#main-content" class="skip-link">{{ t('common.skipToContent') }}</a>
    <n-message-provider>
      <n-dialog-provider>
        <n-notification-provider>
          <!-- [AUDIT-FIX] 严重-7: 顶层 router-view 未包裹 ErrorBoundary
               Login/Setup/NotFound 等顶层页面渲染异常会导致整页白屏无法恢复 -->
          <ErrorBoundary>
            <router-view />
          </ErrorBoundary>
        </n-notification-provider>
      </n-dialog-provider>
    </n-message-provider>
  </n-config-provider>
</template>

<script lang="ts">
import type { InjectionKey, Ref } from 'vue'
// [AUDIT-FIX] 类型化的 provide/inject key，替代字符串 key 以获得类型安全
export const TOGGLE_THEME_KEY: InjectionKey<() => void> = Symbol('toggleTheme')
export const IS_DARK_KEY: InjectionKey<Ref<boolean>> = Symbol('isDark')
</script>

<script setup lang="ts">
import { ref, computed, provide, watch, onMounted, onUnmounted } from 'vue'
import { NConfigProvider, NMessageProvider, NDialogProvider, NNotificationProvider, darkTheme, zhCN, dateZhCN, enUS, dateEnUS, type GlobalTheme } from 'naive-ui'
import { useCurrentLocale, t } from '@/i18n'
import { setDiscreteApiTheme, notification } from '@/utils/discreteApi'
import ErrorBoundary from '@/components/ErrorBoundary.vue'

const isDark = ref(localStorage.getItem('edgelite_theme') === 'dark')
const theme = computed<GlobalTheme | null>(() => isDark.value ? darkTheme : null)
function toggleTheme() {
  isDark.value = !isDark.value
  localStorage.setItem('edgelite_theme', isDark.value ? 'dark' : 'light')
  setDiscreteApiTheme(isDark.value)
}
provide(TOGGLE_THEME_KEY, toggleTheme)
provide(IS_DARK_KEY, isDark)

const currentLocale = useCurrentLocale()
const naiveLocale = computed(() => currentLocale.value === 'en-US' ? enUS : zhCN)
const naiveDateLocale = computed(() => currentLocale.value === 'en-US' ? dateEnUS : dateZhCN)

watch(isDark, (v) => setDiscreteApiTheme(v), { immediate: true })

// FIXED: 全局 WebSocket 错误处理 — 重连失败时提示用户
const handleWsReconnectFailed = () => {
  notification.warning({
    title: t('ws.reconnectFailedTitle'),
    content: t('ws.reconnectFailedContent'),
    duration: 10000,
  })
}
const handleWsAuthFailed = () => {
  notification.error({
    title: t('ws.authFailedTitle'),
    content: t('ws.authFailedContent'),
    duration: 10000,
  })
}

onMounted(() => {
  window.addEventListener('ws:reconnect-failed', handleWsReconnectFailed)
  window.addEventListener('ws:auth-failed', handleWsAuthFailed)
})
onUnmounted(() => {
  window.removeEventListener('ws:reconnect-failed', handleWsReconnectFailed)
  window.removeEventListener('ws:auth-failed', handleWsAuthFailed)
})
</script>

<style>
/* [AUDIT-FIX] 建议级-全局非 scoped CSS 改为更具体的选择器，仅影响表格操作列内的按钮 */
.n-data-table .n-data-table-tr .n-button + .n-button {
  margin-left: 8px;
}

.skip-link {
  position: absolute;
  top: -40px;
  left: 0;
  background: #2080f0;
  color: white;
  padding: 8px 16px;
  z-index: 9999;
  font-size: 14px;
  text-decoration: none;
  border-radius: 0 0 4px 0;
  transition: top 0.2s;
}
.skip-link:focus {
  top: 0;
}
</style>
