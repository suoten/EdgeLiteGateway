<template>
  <n-config-provider :theme="theme">
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
import { darkTheme, type GlobalTheme } from 'naive-ui'

const isDark = ref(localStorage.getItem('edgelite_theme') === 'dark')
const theme = computed<GlobalTheme | null>(() => isDark.value ? darkTheme : null)
function toggleTheme() {
  isDark.value = !isDark.value
  localStorage.setItem('edgelite_theme', isDark.value ? 'dark' : 'light')
}
provide('toggleTheme', toggleTheme)
provide('isDark', isDark)
</script>
