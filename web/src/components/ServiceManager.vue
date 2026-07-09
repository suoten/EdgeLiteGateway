<template>
  <div class="service-manager">
    <n-spin :show="loading">
      <n-card :bordered="false">
        <template #header>
          <div style="display:flex;align-items:center;justify-content:space-between">
            <span>{{ displayName }}</span>
            <n-tag :type="statusTagType" size="small">{{ statusLabel }}</n-tag>
          </div>
        </template>

        <n-space vertical size="large">
          <n-descriptions v-if="isRunning && runningFields?.length" label-placement="left" :column="2" bordered>
            <n-descriptions-item v-for="field in runningFields" :key="field.label" :label="field.label">
              {{ field.value }}
            </n-descriptions-item>
          </n-descriptions>

          <slot name="extra" />

          <n-space>
            <n-button
              v-if="!isEnabled"
              type="primary"
              size="small"
              :loading="toggleLoading"
              @click="handleEnable"
            >
              {{ t('serviceManager.enableService') }}
            </n-button>
            <n-button
              v-if="isEnabled && !isRunning"
              type="success"
              size="small"
              :loading="toggleLoading"
              @click="handleStart"
            >
              {{ t('serviceManager.start') }}
            </n-button>
            <n-button
              v-if="isRunning"
              type="warning"
              size="small"
              :loading="toggleLoading"
              @click="handleStop"
            >
              {{ t('serviceManager.stop') }}
            </n-button>
            <n-button
              v-if="isEnabled"
              type="error"
              size="small"
              :loading="toggleLoading"
              @click="handleDisable"
            >
              {{ t('serviceManager.disableService') }}
            </n-button>
            <n-button
              v-if="isEnabled && !depsInstalled"
              type="info"
              size="small"
              :loading="installing"
              @click="handleInstallDeps"
            >
              {{ t('serviceManager.installDeps') }}
            </n-button>
          </n-space>

          <n-alert v-if="statusData.error_message" type="error" :show-icon="true" style="margin-top:8px">
            {{ statusData.error_message }}
          </n-alert>
        </n-space>
      </n-card>
    </n-spin>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { serviceApi } from '@/api'
import { extractError } from '@/utils/errorCodes'
import { t } from '@/i18n'
import { message as msg, dialog } from '@/utils/discreteApi'
// [AUDIT-FIX] 严重级-服务启停属高危操作，需权限校验
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

const props = defineProps<{
  serviceName: string
  displayName: string
  runningFields?: { label: string; value: any }[]
}>()

const emit = defineEmits<{
  (e: 'status-loaded', data: any): void
}>()

const loading = ref(true)
const toggleLoading = ref(false)
const installing = ref(false)
const statusData = ref<any>({})

const isEnabled = computed(() => statusData.value.state !== 'disabled')
const isRunning = computed(() => statusData.value.state === 'running')
const depsInstalled = computed(() => (statusData.value.dependencies || []).every((d: any) => d.installed))

const statusLabel = computed(() => {
  if (isRunning.value) return t('serviceManager.running')
  if (isEnabled.value) return t('serviceManager.enabled')
  return t('serviceManager.notEnabled')
})

const statusTagType = computed(() => {
  if (isRunning.value) return 'success'
  if (isEnabled.value) return 'warning'
  return 'default'
})

async function fetchStatus() {
  loading.value = true
  try {
    const resp = await serviceApi.status(props.serviceName)
    statusData.value = resp
    emit('status-loaded', statusData.value)
  } catch {
    statusData.value = { state: 'disabled', error_message: t('serviceManager.statusFailed'), dependencies: [] }
  } finally {
    loading.value = false
  }
}

async function handleEnable() {
  // [AUDIT-FIX] 严重级-服务启停需操作员及以上权限
  if (!auth.isOperator) { msg.warning(t('common.permissionDenied')); return }
  toggleLoading.value = true
  try {
    const result = await serviceApi.enable(props.serviceName)
    if (result?.warning) {
      msg.warning(result.warning || result.message || t('serviceManager.enableStartFailed'))
    } else {
      msg.success(t('serviceManager.enableSuccess'))
    }
    await fetchStatus()
  } catch (e: any) {
    const detail = e?.response?.data?.detail
    if (typeof detail === 'object' && detail?.missing_dependencies) {
      msg.warning(t('serviceManager.depMissing'))
    } else if (typeof detail === 'object' && detail !== null) {
      const errCode = detail.error || ''
      const hintCode = detail.hint || ''
      const errMsg = errCode ? extractError({ response: { data: { detail: errCode } } } as any, t('serviceManager.enableFailed')) : t('serviceManager.enableFailed')
      const hintMsg = hintCode ? extractError({ response: { data: { detail: hintCode } } } as any, '') : ''
      if (hintMsg) {
        dialog.error({ title: errMsg, content: hintMsg, positiveText: t('common.confirm') })
      } else {
        msg.error(errMsg)
      }
    } else {
      const errStr = extractError(e, '')
      if (errStr.includes('depend') || errStr.includes('424') || errStr.includes('ERR_')) {
        msg.warning(t('serviceManager.depMissing'))
      } else {
        msg.error(t('serviceManager.enableFailed') + (errStr ? ': ' + errStr : ''))
      }
    }
  } finally {
    toggleLoading.value = false
  }
}

async function handleDisable() {
  // [AUDIT-FIX] 严重级-禁用服务需权限校验 + 二次确认（可能中断关键服务）
  if (!auth.isOperator) { msg.warning(t('common.permissionDenied')); return }
  dialog.warning({
    title: t('serviceManager.disableConfirmTitle'),
    content: t('serviceManager.disableConfirmContent', { name: props.displayName }),
    positiveText: t('common.confirm'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      toggleLoading.value = true
      try {
        await serviceApi.disable(props.serviceName)
        msg.success(t('serviceManager.disableSuccess'))
        await fetchStatus()
      } catch (e: any) {
        msg.error(extractError(e, t('serviceManager.disableFailed')))
      } finally {
        toggleLoading.value = false
      }
    },
  })
}

async function handleStart() {
  // [AUDIT-FIX] 严重级-启动服务需权限校验
  if (!auth.isOperator) { msg.warning(t('common.permissionDenied')); return }
  toggleLoading.value = true
  try {
    await serviceApi.start(props.serviceName)
    msg.success(t('serviceManager.startSuccess'))
    await fetchStatus()
  } catch (e: any) {
    const detail = e?.response?.data?.detail
    if (typeof detail === 'object' && detail !== null) {
      const errCode = detail.error || ''
      const hintCode = detail.hint || ''
      const errMsg = errCode ? extractError({ response: { data: { detail: errCode } } } as any, t('serviceManager.startFailed')) : t('serviceManager.startFailed')
      const hintMsg = hintCode ? extractError({ response: { data: { detail: hintCode } } } as any, '') : ''
      if (hintMsg) {
        dialog.error({ title: errMsg, content: hintMsg, positiveText: t('common.confirm') })
      } else {
        msg.error(errMsg)
      }
    } else {
      const errStr = extractError(e, t('serviceManager.unknownError'))
      msg.error(t('serviceManager.startFailed') + (errStr ? ': ' + errStr : ''))
    }
  } finally {
    toggleLoading.value = false
  }
}

async function handleStop() {
  // [AUDIT-FIX] 严重级-停止服务需权限校验 + 二次确认（可能中断数据采集）
  if (!auth.isOperator) { msg.warning(t('common.permissionDenied')); return }
  dialog.warning({
    title: t('serviceManager.stopConfirmTitle'),
    content: t('serviceManager.stopConfirmContent', { name: props.displayName }),
    positiveText: t('common.confirm'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      toggleLoading.value = true
      try {
        await serviceApi.stop(props.serviceName)
        msg.success(t('serviceManager.stopSuccess'))
        await fetchStatus()
      } catch (e: any) {
        msg.error(extractError(e, t('serviceManager.stopFailed')))
      } finally {
        toggleLoading.value = false
      }
    },
  })
}

async function handleInstallDeps() {
  // [AUDIT-FIX] 严重级-安装依赖需权限校验
  if (!auth.isOperator) { msg.warning(t('common.permissionDenied')); return }
  installing.value = true
  try {
    await serviceApi.installDeps(props.serviceName)
    msg.success(t('serviceManager.installSuccess'))
    await fetchStatus()
  } catch (e: any) {
    msg.error(extractError(e, t('serviceManager.installFailed')))
  } finally {
    installing.value = false
  }
}

onMounted(fetchStatus)
</script>
