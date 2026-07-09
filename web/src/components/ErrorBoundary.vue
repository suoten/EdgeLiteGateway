<template>
  <div v-if="hasError" class="error-boundary">
    <n-result status="error" :title="t('common.errorOccurred')" :description="errorMessage">
      <template #footer>
        <n-button @click="handleReset">{{ t('common.retry') }}</n-button>
        <n-button quaternary @click="handleGoHome">{{ t('common.goHome') }}</n-button>
      </template>
    </n-result>
  </div>
  <slot v-else />
</template>

<script setup lang="ts">
import { ref, onErrorCaptured } from 'vue'
import { useRouter } from 'vue-router'
import { NResult, NButton } from 'naive-ui'
import { t } from '@/i18n'

const router = useRouter()
const hasError = ref(false)
const errorMessage = ref('')

// FIX-FE-001: ErrorBoundary component was missing, causing white screen
// when App.vue tried to import it. Created minimal implementation to
// catch component render errors and display a user-friendly fallback.
onErrorCaptured((err: Error) => {
  hasError.value = true
  errorMessage.value = err.message || String(err)
  console.error('[ErrorBoundary]', err)
  return false // Prevent error from propagating further
})

function handleReset() {
  hasError.value = false
  errorMessage.value = ''
}

function handleGoHome() {
  hasError.value = false
  errorMessage.value = ''
  router.push('/')
}
</script>

<style scoped>
.error-boundary {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 400px;
  padding: 24px;
}
</style>
