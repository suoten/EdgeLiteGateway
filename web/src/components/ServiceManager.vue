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
              启用服务
            </n-button>
            <n-button
              v-if="isEnabled && !isRunning"
              type="success"
              size="small"
              :loading="toggleLoading"
              @click="handleStart"
            >
              启动
            </n-button>
            <n-button
              v-if="isRunning"
              type="warning"
              size="small"
              :loading="toggleLoading"
              @click="handleStop"
            >
              停止
            </n-button>
            <n-button
              v-if="isEnabled"
              type="error"
              size="small"
              :loading="toggleLoading"
              @click="handleDisable"
            >
              禁用服务
            </n-button>
            <n-button
              v-if="isEnabled && !depsInstalled"
              type="info"
              size="small"
              :loading="installing"
              @click="handleInstallDeps"
            >
              安装依赖
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
import { useMessage } from 'naive-ui'
import { serviceApi } from '@/api'

const props = defineProps<{
  serviceName: string
  displayName: string
  runningFields?: { label: string; value: any }[]
}>()

const emit = defineEmits<{
  (e: 'status-loaded', data: any): void
}>()

const msg = useMessage()
const loading = ref(true)
const toggleLoading = ref(false)
const installing = ref(false)
const statusData = ref<any>({})

const isEnabled = computed(() => statusData.value.state !== 'disabled')
const isRunning = computed(() => statusData.value.state === 'running')
const depsInstalled = computed(() => (statusData.value.dependencies || []).every((d: any) => d.installed))

const statusLabel = computed(() => {
  if (isRunning.value) return '运行中'
  if (isEnabled.value) return '已启用'
  return '未启用'
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
    statusData.value = { state: 'disabled', error_message: '无法获取服务状态', dependencies: [] }
  } finally {
    loading.value = false
  }
}

async function handleEnable() {
  toggleLoading.value = true
  try {
    await serviceApi.enable(props.serviceName)
    msg.success('服务已启用')
    await fetchStatus()
  } catch (e: any) {
    const detail = e?.message || ''
    if (detail.includes('424') || detail.includes('依赖')) {
      msg.warning('缺少依赖，请先安装依赖')
    } else {
      msg.error('启用失败: ' + (detail || '未知错误'))
    }
  } finally {
    toggleLoading.value = false
  }
}

async function handleDisable() {
  toggleLoading.value = true
  try {
    await serviceApi.disable(props.serviceName)
    msg.success('服务已禁用')
    await fetchStatus()
  } catch (e: any) {
    msg.error('禁用失败: ' + (e?.message || '未知错误'))
  } finally {
    toggleLoading.value = false
  }
}

async function handleStart() {
  toggleLoading.value = true
  try {
    await serviceApi.start(props.serviceName)
    msg.success('服务已启动')
    await fetchStatus()
  } catch (e: any) {
    msg.error('启动失败: ' + (e?.message || '未知错误'))
  } finally {
    toggleLoading.value = false
  }
}

async function handleStop() {
  toggleLoading.value = true
  try {
    await serviceApi.stop(props.serviceName)
    msg.success('服务已停止')
    await fetchStatus()
  } catch (e: any) {
    msg.error('停止失败: ' + (e?.message || '未知错误'))
  } finally {
    toggleLoading.value = false
  }
}

async function handleInstallDeps() {
  installing.value = true
  try {
    await serviceApi.installDeps(props.serviceName)
    msg.success('依赖安装成功')
    await fetchStatus()
  } catch (e: any) {
    msg.error('安装依赖失败: ' + (e?.message || '未知错误'))
  } finally {
    installing.value = false
  }
}

onMounted(fetchStatus)
</script>
