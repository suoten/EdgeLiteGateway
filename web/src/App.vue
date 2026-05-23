<template>
  <n-config-provider :theme="theme" :locale="naiveLocale" :date-locale="naiveDateLocale">
    <n-message-provider>
      <n-dialog-provider>
        <n-notification-provider>
          <router-view />
        </n-notification-provider>
      </n-dialog-provider>
    </n-message-provider>
  </n-config-provider>
</template>

<script setup lang="ts">
import { ref, computed, provide } from 'vue'
import { darkTheme, zhCN, dateZhCN, enUS, dateEnUS, type GlobalTheme } from 'naive-ui'
import { useCurrentLocale } from '@/i18n'

const isDark = ref(localStorage.getItem('edgelite_theme') === 'dark')
const theme = computed<GlobalTheme | null>(() => isDark.value ? darkTheme : null)
function toggleTheme() {
  isDark.value = !isDark.value
  localStorage.setItem('edgelite_theme', isDark.value ? 'dark' : 'light')
}
provide('toggleTheme', toggleTheme)
provide('isDark', isDark)

// FIXED-P3: Naive UI locale硬编码zhCN，切换英文后DatePicker等组件仍为中文
const currentLocale = useCurrentLocale()
const naiveLocale = computed(() => currentLocale.value === 'en-US' ? enUS : zhCN)
const naiveDateLocale = computed(() => currentLocale.value === 'en-US' ? dateEnUS : dateZhCN)
</script>
