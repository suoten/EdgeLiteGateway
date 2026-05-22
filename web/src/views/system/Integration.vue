<template>
  <n-card :title="t('rpc.title')" size="small">
    <n-tabs type="line" animated>
      <n-tab-pane name="rpc" :tab="t('rpc.title')">
        <n-space vertical :size="16">
          <n-form label-placement="left" :label-width="100">
            <n-form-item :label="t('rpc.deviceId')">
              <n-select v-model:value="rpcForm.device_id" :options="deviceOptions" filterable :placeholder="t('rpc.deviceId')" />
            </n-form-item>
            <n-form-item :label="t('rpc.method')">
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
    </n-tabs>
  </n-card>
</template>

<script setup lang="ts">
import { ref, onMounted, h } from 'vue'
import { NTag, useMessage } from 'naive-ui'
import { t } from '@/i18n'
import { integrationApi, deviceApi } from '@/api'

const message = useMessage()

const rpcForm = ref({
  device_id: '',
  method: '',
  params: {} as Record<string, any>,
  timeout: 10,
})
const paramsJson = ref('{}')
const executing = ref(false)
const lastResult = ref<any>(null)
const history = ref<any[]>([])
const deviceOptions = ref<{ label: string; value: string }[]>([])

const historyColumns = [
  { title: 'ID', key: 'command_id', width: 120 },
  { title: t('rpc.method'), key: 'method' },
  { title: t('rpc.deviceId'), key: 'device_id' },
  { title: t('rpc.successCol'), key: 'success', width: 80, render: (r: any) => h(NTag, { type: r.success ? 'success' : 'error', size: 'small' }, { default: () => r.success ? 'OK' : 'FAIL' }) },
  { title: t('rpc.elapsedMs'), key: 'elapsed_ms', width: 100, render: (r: any) => r.elapsed_ms?.toFixed(1) },
  { title: t('rpc.time'), key: 'timestamp', width: 160, render: (r: any) => r.timestamp ? new Date(r.timestamp * 1000).toLocaleString() : '-' },
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
    const params = JSON.parse(paramsJson.value)
    rpcForm.value.params = params
  } catch {
    message.error(t('rpc.invalidJsonParams'))
    return
  }
  if (!rpcForm.value.device_id || !rpcForm.value.method) {
    message.error(t('common.required'))
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
    message.error(e?.response?.data?.detail || e?.message || t('common.failed'))
  } finally {
    executing.value = false
  }
}

async function loadHistory() {
  try {
    history.value = await integrationApi.rpcHistory()
  } catch { /* ignore */ }
}

onMounted(() => { loadDevices(); loadHistory() })
</script>
