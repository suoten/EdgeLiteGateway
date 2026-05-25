<template>
  <n-card :title="t('rpc.title')" size="small">
    <n-tabs type="line" animated>
      <n-tab-pane name="rpc" :tab="t('rpc.title')">
        <n-space vertical :size="16">
          <n-form label-placement="left" :label-width="100" :model="rpcForm" :rules="rpcFormRules" ref="rpcFormRef">
            <n-form-item :label="t('rpc.deviceId')" path="device_id">
              <n-select v-model:value="rpcForm.device_id" :options="deviceOptions" filterable :placeholder="t('rpc.deviceId')" />
            </n-form-item>
            <n-form-item :label="t('rpc.method')" path="method">
              <n-input v-model:value="rpcForm.method" :placeholder="t('rpc.method')" />
            </n-form-item>
            <n-form-item :label="t('rpc.params')">
              <n-input v-model:value="paramsJson" type="textarea" :rows="4" placeholder='{"point":"temperature","value":25}' />
            </n-form-item>
            <n-form-item :label="t('rpc.timeout')">
              <n-input-number v-model:value="rpcForm.timeout" :min="1" :max="120" />
            </n-form-item>
          </n-form>
          <n-space>
            <n-button type="primary" :loading="executing" @click="handleExecute">{{ t('rpc.execute') }}</n-button>
          </n-space>
          <n-card v-if="lastResult" :title="t('common.actions')" size="small">
            <n-descriptions label-placement="left" :column="1" bordered>
              <n-descriptions-item :label="t('rpc.commandId')">{{ lastResult.command_id }}</n-descriptions-item>
              <n-descriptions-item :label="t('rpc.successCol')">
                <n-tag :type="lastResult.success ? 'success' : 'error'">{{ lastResult.success ? 'OK' : 'FAIL' }}</n-tag>
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
        <n-data-table :columns="historyColumns" :data="history" :bordered="false" size="small" />
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
                  <n-statistic label="Node ID" :value="topology.node_id || '-'" />
                </n-gi>
                <n-gi>
                  <n-statistic label="Role" :value="topology.role || '-'" />
                </n-gi>
                <n-gi>
                  <n-statistic label="Parent" :value="topology.parent || '-'" />
                </n-gi>
                <n-gi>
                  <n-statistic label="Children" :value="topology.children?.length || 0" />
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
              <n-data-table v-if="neighbors.length > 0" :columns="neighborColumns" :data="neighbors" :bordered="false" size="small" />
              <n-empty v-else-if="!loadingNeighbors" :description="t('cascade.noNeighbors')" />
            </n-space>
          </n-card>
        </n-space>
      </n-tab-pane>
    </n-tabs>
  </n-card>
</template>

<script setup lang="ts">
import { ref, onMounted, h } from 'vue'
import { NTag, NButton, useMessage, useDialog } from 'naive-ui'
import { t } from '@/i18n'
import { extractError } from '@/utils/errorCodes'
import { integrationApi, deviceApi, systemApi } from '@/api'

const message = useMessage()
const dialog = useDialog()

const rpcForm = ref({
  device_id: '',
  method: '',
  params: {} as Record<string, any>,
  timeout: 10,
})
const rpcFormRef = ref<any>(null)
const rpcFormRules = {
  device_id: { required: true, message: t('rpc.deviceId') + t('common.required'), trigger: 'change' },
  method: { required: true, message: t('rpc.method') + t('common.required'), trigger: 'blur' },
}
const paramsJson = ref('{}')
const executing = ref(false)
const lastResult = ref<any>(null)
const history = ref<any[]>([])
const deviceOptions = ref<{ label: string; value: string }[]>([])

// Cascade topology state
const topology = ref<any>(null)
const neighbors = ref<any[]>([])
const loadingTopology = ref(false)
const loadingNeighbors = ref(false)

const historyColumns = [
  { title: t('rpc.commandId'), key: 'command_id', width: 120 },
  { title: t('rpc.method'), key: 'method' },
  { title: t('rpc.deviceId'), key: 'device_id' },
  { title: t('rpc.successCol'), key: 'success', width: 80, render: (r: any) => h(NTag, { type: r.success ? 'success' : 'error', size: 'small' }, { default: () => r.success ? t('rpc.ok') : t('rpc.fail') }) },
  { title: t('rpc.elapsedMs'), key: 'elapsed_ms', width: 100, render: (r: any) => r.elapsed_ms?.toFixed(1) },
  { title: t('rpc.time'), key: 'timestamp', width: 160, render: (r: any) => r.timestamp ? new Date(r.timestamp * 1000).toLocaleString() : '-' },
]

const neighborColumns = [
  { title: t('cascade.nodeId'), key: 'node_id', width: 150 },
  { title: t('cascade.host'), key: 'host' },
  { title: t('cascade.port'), key: 'port', width: 100 },
  { title: t('cascade.role'), key: 'role', width: 100 },
  { title: t('common.actions'), key: 'actions', width: 120, render: (r: any) => h(NButton, { size: 'small', type: 'error', onClick: () => removeNeighbor(r.node_id) }, { default: () => t('common.delete') }) },
]

async function loadDevices() {
  try {
    const data = await deviceApi.list()
    const devices = Array.isArray(data) ? data : (data as any)?.data ?? []
    deviceOptions.value = devices.map((d: any) => ({ label: `${d.name} (${d.device_id})`, value: d.device_id }))
  } catch { /* ignore */ }
}

async function handleExecute() {
  try {
    await rpcFormRef.value?.validate()
  } catch { return }
  try {
    const params = JSON.parse(paramsJson.value)
    rpcForm.value.params = params
  } catch {
    message.error(t('rpc.invalidJsonParams'))
    return
  }
  executing.value = true
  try {
    const result = await integrationApi.rpcExecute({
      method: rpcForm.value.method,
      device_id: rpcForm.value.device_id,
      params: rpcForm.value.params,
      timeout: rpcForm.value.timeout,
    })
    lastResult.value = result
    if (result.success) {
      message.success(t('rpc.success'))
    } else {
      message.error(result.error || t('rpc.timeoutError'))
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
  dialog.warning({
    title: t('common.confirm'),
    content: `Remove neighbor ${nodeId}?`,
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

onMounted(() => { loadDevices(); loadHistory(); loadTopology(); loadNeighbors() })
</script>
