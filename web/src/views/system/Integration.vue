<template>
  <n-card :title="t('rpc.title')" size="small">
    <n-tabs type="line" animated>
      <n-tab-pane name="rpc" :tab="t('rpc.title')">
        <n-space vertical :size="16">
          <n-form label-placement="left" :label-width="100" :model="rpcForm" :rules="rpcFormRules" ref="rpcFormRef">
            <n-form-item :label="t('rpc.deviceId')" path="device_id">
              <n-select v-model:value="rpcForm.device_id" :options="deviceOptions" filterable clearable :placeholder="t('rpc.selectDevice')" @update:value="onDeviceChange" />
            </n-form-item>
            <n-form-item :label="t('rpc.method')" path="method">
              <n-select v-model:value="rpcForm.method" :options="pointOptions" filterable clearable :placeholder="pointOptions.length ? t('rpc.selectPoint') : t('rpc.selectDeviceFirst')" :disabled="!rpcForm.device_id" @update:value="onPointChange" />
            </n-form-item>
            <n-form-item :label="t('rpc.params')">
              <n-input v-model:value="paramsJson" type="textarea" :rows="4" :placeholder="t('rpc.paramsPlaceholder')" />
            </n-form-item>
            <n-form-item :label="t('rpc.timeout')">
              <n-input-number v-model:value="rpcForm.timeout" :min="1" :max="120" />
            </n-form-item>
          </n-form>
          <n-space>
            <n-button type="primary" :loading="executing" :disabled="!auth.isOperator" @click="handleExecute">{{ t('rpc.execute') }}</n-button>
          </n-space>
          <n-card v-if="lastResult" :title="t('common.actions')" size="small">
            <n-descriptions label-placement="left" :column="1" bordered>
              <n-descriptions-item :label="t('rpc.commandId')">{{ lastResult.command_id }}</n-descriptions-item>
              <n-descriptions-item :label="t('rpc.successCol')">
                <n-tag :type="lastResult.success ? 'success' : 'error'">{{ lastResult.success ? t('status.ok') : t('status.fail') }}</n-tag>
              </n-descriptions-item>
              <n-descriptions-item v-if="lastResult.result" :label="t('rpc.resultLabel')">
                <n-code :code="JSON.stringify(lastResult.result, null, 2)" language="json" />
              </n-descriptions-item>
              <n-descriptions-item v-if="lastResult.error" :label="t('rpc.errorLabel')">
                <n-text type="error">{{ lastResult.error }}</n-text>
              </n-descriptions-item>
              <n-descriptions-item :label="t('rpc.elapsedMs')">{{ lastResult.elapsed_ms?.toFixed(1) }} ms</n-descriptions-item>
            </n-descriptions>
          </n-card>
        </n-space>
      </n-tab-pane>
      <n-tab-pane name="history" :tab="t('rpc.history')">
        <n-data-table :columns="historyColumns" :data="history" :bordered="false" size="small" :pagination="{ pageSize: 20, showSizePicker: true, pageSizes: [10, 20, 50] }" :scroll-x="810" />
      </n-tab-pane>
      <n-tab-pane name="cascade" :tab="t('cascade.title')">
        <n-space vertical :size="16">
          <n-card :title="t('cascade.topology')" size="small">
            <n-space vertical :size="12">
              <n-space>
                <n-button type="primary" :loading="loadingTopology" @click="loadTopology">
                  {{ t('common.refresh') }}
                </n-button>
              </n-space>
              <n-grid v-if="topology" :x-gap="12" :cols="4">
                <n-gi>
                  <n-statistic :label="t('cascade.nodeId')" :value="topology.local_id || '-'" />
                </n-gi>
                <n-gi>
                  <n-statistic :label="t('cascade.role')" :value="topology.status || '-'" />
                </n-gi>
                <n-gi>
                  <n-statistic :label="t('cascade.parent')" :value="topology.parent_id || '-'" />
                </n-gi>
                <n-gi>
                  <n-statistic :label="t('cascade.children')" :value="topology.children?.length || 0" />
                </n-gi>
              </n-grid>
              <n-empty v-else-if="!loadingTopology" :description="t('cascade.noNeighbors')" />
            </n-space>
          </n-card>
          <n-card :title="t('cascade.neighbors')" size="small">
            <n-space vertical :size="12">
              <n-button type="primary" :loading="loadingNeighbors" @click="loadNeighbors">
                {{ t('common.refresh') }}
              </n-button>
              <n-data-table v-if="neighbors.length > 0" :columns="neighborColumns" :data="neighbors" :bordered="false" size="small" :scroll-x="820" />
              <n-empty v-else-if="!loadingNeighbors" :description="t('cascade.noNeighbors')" />
            </n-space>
          </n-card>
        </n-space>
      </n-tab-pane>
    </n-tabs>
  </n-card>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, h, watch } from 'vue'
import { NTag, NButton } from 'naive-ui'
import { t } from '@/i18n'
import { extractError, getErrorMessage } from '@/utils/errorCodes'
import { integrationApi, deviceApi, systemApi } from '@/api'
import { connect as wsConnect, disconnect as wsDisconnect } from '@/api/websocket'
import { message, dialog } from '@/utils/discreteApi'
import { formatDateTime } from '@/utils/datetime'
// [FIX-6] 致命级-RPC 执行与邻居移除无权限校验，引入 auth store
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

const rpcForm = ref({
  device_id: '',
  method: '',
  params: {} as Record<string, any>,
  timeout: 10,
})
const rpcFormRef = ref<any>(null)
const rpcFormRules = computed(() => ({
  device_id: { required: true, message: t('rpc.deviceId') + t('common.required'), trigger: ['change', 'blur'] },
  method: { required: true, message: t('rpc.method') + t('common.required'), trigger: ['change', 'blur'] },
  timeout: { type: 'number' as const, min: 1, max: 120, message: t('rpc.timeoutRange'), trigger: ['input', 'blur'] },
}))
const paramsJson = ref('{}')
const executing = ref(false)
const lastResult = ref<any>(null)
const history = ref<any[]>([])
const deviceOptions = ref<{ label: string; value: string }[]>([])
const pointOptions = ref<{ label: string; value: string }[]>([])
const deviceList = ref<any[]>([])  // cache full device objects for point lookup

// Cascade topology state
const topology = ref<any>(null)
const neighbors = ref<any[]>([])
const loadingTopology = ref(false)
const loadingNeighbors = ref(false)
let integrationWsHandler: ((data: any) => void) | null = null

// [FIX-15] 严重级-historyColumns 改为 computed，使 i18n 切换时列标题响应更新
const historyColumns = computed(() => [
  { title: t('rpc.commandId'), key: 'command_id', width: 120 },
  { title: t('rpc.method'), key: 'method' },
  { title: t('rpc.deviceId'), key: 'device_id' },
  { title: t('rpc.successCol'), key: 'success', width: 80, render: (r: any) => h(NTag, { type: r.success ? 'success' : 'error', size: 'small' }, { default: () => r.success ? t('rpc.ok') : t('rpc.fail') }) },
  { title: t('rpc.elapsedMs'), key: 'elapsed_ms', width: 100, render: (r: any) => r.elapsed_ms?.toFixed(1) },
  { title: t('rpc.time'), key: 'timestamp', width: 160, render: (r: any) => r.timestamp ? new Date(r.timestamp * 1000).toLocaleString() : '-' },
])

const neighborColumns = [
  { title: t('cascade.neighborId'), key: 'neighbor_id', width: 150 },
  { title: t('cascade.host'), key: 'host' },
  { title: t('cascade.port'), key: 'port', width: 100 },
  { title: t('cascade.role'), key: 'role', width: 100 },
  { title: t('cascade.lastSeen'), key: 'last_seen', width: 150, render: (r: any) => formatDateTime(r.last_seen) },
  { title: t('common.actions'), key: 'actions', width: 120, render: (r: any) => h(NButton, { size: 'small', type: 'error', disabled: !auth.isAdmin, onClick: () => removeNeighbor(r.neighbor_id) }, { default: () => t('common.delete') }) },
]

async function loadDevices() {
  try {
    const data = await deviceApi.list()
    const devices = Array.isArray(data) ? data : (data as any)?.data ?? []
    deviceList.value = devices
    deviceOptions.value = devices.map((d: any) => ({ label: `${d.name} (${d.device_id})`, value: d.device_id }))
  } catch { /* ignore */ }
}

function onDeviceChange(deviceId: string) {
  // Reset method and params when device changes
  rpcForm.value.method = ''
  paramsJson.value = '{}'

  const device = deviceList.value.find((d: any) => d.device_id === deviceId)
  if (!device || !device.points) {
    pointOptions.value = []
    return
  }

  // Only show writable points (access_mode 'w' or 'rw')
  const writablePoints = device.points.filter(
    (p: any) => p.access_mode === 'w' || p.access_mode === 'rw'
  )
  pointOptions.value = writablePoints.map((p: any) => ({
    label: `${p.name}${p.unit ? ` (${p.unit})` : ''}${p.data_type ? ` [${p.data_type}]` : ''}`,
    value: p.name,
  }))

  // If no writable points, show all points as fallback
  if (pointOptions.value.length === 0 && device.points.length > 0) {
    // FIX-32: 添加警告提示
    message.warning(t('integration.noWritablePoints'))
    pointOptions.value = device.points.map((p: any) => ({
      label: `${p.name}${p.unit ? ` (${p.unit})` : ''}${p.data_type ? ` [${p.data_type}]` : ''}${p.access_mode === 'r' ? ' (RO)' : ''}`,
      value: p.name,
    }))
  }
}

function onPointChange(pointName: string) {
  // Auto-fill params JSON with the selected point
  const device = deviceList.value.find((d: any) => d.device_id === rpcForm.value.device_id)
  const point = device?.points?.find((p: any) => p.name === pointName)
  // FIX-33: 改进默认值选择
  let defaultVal: any
  if (point?.data_type === 'bool') {
    defaultVal = false
  } else if (point?.data_type === 'string') {
    defaultVal = ''
  } else if (point?.min != null) {
    defaultVal = point.min
  } else {
    defaultVal = null  // 无 min 时让用户必填
  }
  paramsJson.value = JSON.stringify({ point: pointName, value: defaultVal }, null, 2)
}

async function handleExecute() {
  // [FIX-6] 致命级-RPC 执行属高危写操作，需操作员及以上权限
  if (!auth.isOperator) {
    message.warning(t('common.permissionDenied'))
    return
  }
  try {
    await rpcFormRef.value?.validate()
  } catch { return }
  // [FIX-16] 严重级-JSON.parse 后未校验类型，非对象（如数组/数字/字符串）会导致后端异常
  let parsed: any
  try {
    parsed = JSON.parse(paramsJson.value)
  } catch {
    message.error(t('integration.invalidJson'))
    return
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    message.error(t('integration.paramsMustBeObject'))
    return
  }
  rpcForm.value.params = parsed
  executing.value = true
  try {
    const result = await integrationApi.rpcExecute({
      method: rpcForm.value.method,
      device_id: rpcForm.value.device_id,
      params: rpcForm.value.params,
      timeout: rpcForm.value.timeout ?? 10,
    })
    lastResult.value = result
    if (result.success) {
      message.success(t('rpc.success'))
    } else {
      // FIX-25: 区分空 error 和超时，不假设超时
      const errorMsg = result.error
        ? (result.error.startsWith('ERR_')
          ? (getErrorMessage(result.error) || t('rpc.failed'))
          : result.error)
        : t('rpc.failed')
      message.error(errorMsg)
    }
    loadHistory()
  } catch (e: any) {
    const detail = e?.response?.data?.detail
    if (e?.response?.status === 503 && detail) {
      message.warning(extractError(e, t('common.failed')))
    } else {
      message.error(extractError(e, t('common.failed')))
    }
  } finally {
    executing.value = false
  }
}

async function loadHistory() {
  try {
    history.value = await integrationApi.rpcHistory()
  } catch { /* ignore */ }
}

// Cascade topology functions
async function loadTopology() {
  loadingTopology.value = true
  try {
    topology.value = await systemApi.getCascadeTopology()
  } catch (e: any) {
    message.error(extractError(e, t('cascade.topologyFailed')))
  } finally {
    loadingTopology.value = false
  }
}

async function loadNeighbors() {
  loadingNeighbors.value = true
  try {
    neighbors.value = await systemApi.getCascadeNeighbors()
  } catch (e: any) {
    message.error(extractError(e, t('cascade.neighborsFailed')))
  } finally {
    loadingNeighbors.value = false
  }
}

async function removeNeighbor(nodeId: string) {
  // [FIX-6] 致命级-移除级联邻居属高危操作，仅 admin 可执行
  if (!auth.isAdmin) {
    message.warning(t('common.permissionDenied'))
    return
  }
  dialog.warning({
    title: t('common.confirm'),
    content: t('integration.removeNeighborConfirm', { id: nodeId }),
    positiveText: t('common.confirm'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      try {
        await systemApi.removeCascadeNeighbor(nodeId)
        message.success(t('common.success'))
        loadNeighbors()
      } catch (e: any) {
        message.error(extractError(e, t('cascade.removeFailed')))
      }
    },
  })
}

// FIX-24: WS 事件防抖
// [AUDIT-FIX] 严重级-setTimeout 在 TS DOM lib 下返回 Timeout 而非 number，使用 ReturnType<typeof setTimeout> 兼容两种环境
let wsDebounceTimer: ReturnType<typeof setTimeout> | null = null
function debouncedWsReload() {
  if (wsDebounceTimer) clearTimeout(wsDebounceTimer)
  wsDebounceTimer = setTimeout(() => {
    loadHistory()
    loadTopology()
    loadNeighbors()
  }, 500)
}

onMounted(() => {
  loadDevices()
  loadHistory()
  loadTopology()
  loadNeighbors()
  integrationWsHandler = (data: any) => {
    if (data.type === 'status_change' || data.type === 'integration_update') {
      debouncedWsReload()
    }
    if (data.type === 'cascade_update' || data.type === 'neighbor_change') {
      debouncedWsReload()
    }
  }
  wsConnect('integration', integrationWsHandler)
})

onUnmounted(() => {
  // FIX-PERF1: 清理防抖定时器，避免组件卸载后定时器仍触发回调导致内存泄漏
  if (wsDebounceTimer) {
    clearTimeout(wsDebounceTimer)
    wsDebounceTimer = null
  }
  if (integrationWsHandler) {
    wsDisconnect('integration', integrationWsHandler)
    integrationWsHandler = null
  }
})
</script>
