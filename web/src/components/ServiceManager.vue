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
import { useMessage, useDialog } from 'naive-ui'
import { serviceApi } from '@/api'
import { extractError } from '@/utils/errorCodes'
import { t } from '@/i18n'

const props = defineProps<{
  serviceName: string
  displayName: string
  runningFields?: { label: string; value: any }[]
}>()

const emit = defineEmits<{
  (e: 'status-loaded', data: any): void
}>()

const msg = useMessage()
const dialog = useDialog()
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
      const errMsg = extractError(e, t('serviceManager.enableFailed'))
      const hint = detail.hint || ''
      if (hint) {
        dialog.error({ title: errMsg, content: hint, positiveText: t('common.confirm') })
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
  toggleLoading.value = true
  try {
    await serviceApi.disable(props.serviceName)
    msg.success(t('serviceManager.disableSuccess'))
    await fetchStatus()
  } catch (e: any) {
    msg.error(t('serviceManager.disableFailed') + (e?.message ? ': ' + e.message : ''))
  } finally {
    toggleLoading.value = false
  }
}

async function handleStart() {
  toggleLoading.value = true
  try {
    await serviceApi.start(props.serviceName)
    msg.success(t('serviceManager.startSuccess'))
    await fetchStatus()
  } catch (e: any) {
    const detail = e?.response?.data?.detail
    if (typeof detail === 'object' && detail !== null) {
      const errMsg = extractError(e, t('serviceManager.startFailed'))
      const hint = detail.hint || ''
      if (hint) {
        dialog.error({ title: errMsg, content: hint, positiveText: t('common.confirm') })
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
  toggleLoading.value = true
  try {
    await serviceApi.stop(props.serviceName)
    msg.success(t('serviceManager.stopSuccess'))
    await fetchStatus()
  } catch (e: any) {
    msg.error(t('serviceManager.stopFailed') + (e?.message ? ': ' + e.message : ''))
  } finally {
    toggleLoading.value = false
  }
}

async function handleInstallDeps() {
  installing.value = true
  try {
    await serviceApi.installDeps(props.serviceName)
    msg.success(t('serviceManager.installSuccess'))
    await fetchStatus()
  } catch (e: any) {
    msg.error(t('serviceManager.installFailed') + (e?.message ? ': ' + e.message : ''))
  } finally {
    installing.value = false
  }
}

onMounted(fetchStatus)
</script>
