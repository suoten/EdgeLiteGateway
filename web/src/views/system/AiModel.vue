<template>
  <n-space vertical :size="16">
    <n-grid :cols="3" :x-gap="12" :y-gap="12">
      <n-gi>
        <n-card size="small" :bordered="true">
          <n-statistic :label="t('ai.modelCount')">
            <n-number-animation :from="0" :to="models.length" :duration="500" />
          </n-statistic>
        </n-card>
      </n-gi>
      <n-gi>
        <n-card size="small" :bordered="true">
          <n-statistic :label="t('ai.totalCalls')">
            <n-number-animation :from="0" :to="stats.totalCalls" :duration="500" />
          </n-statistic>
        </n-card>
      </n-gi>
      <n-gi>
        <n-card size="small" :bordered="true">
          <n-statistic :label="t('ai.totalErrors')">
            <n-number-animation :from="0" :to="stats.totalErrors" :duration="500" />
          </n-statistic>
        </n-card>
      </n-gi>
    </n-grid>

    <n-card :bordered="false">
      <template #header>
        <n-space align="center" :size="12">
          <n-tag :type="aiEnabled ? 'success' : 'default'" size="small" round>
            {{ aiEnabled ? t('ai.statusActive') : t('ai.statusInactive') }}
          </n-tag>
          <span style="font-size: 16px; font-weight: 600">{{ t('ai.manageModels') }}</span>
        </n-space>
      </template>
      <template #header-extra>
        <n-space :size="8">
          <n-button size="small" type="primary" @click="openInference">{{ t('ai.inference') }}</n-button>
          <n-button size="small" @click="fetchModels" :loading="loading">{{ t('common.refresh') }}</n-button>
        </n-space>
      </template>

      <n-data-table
        :columns="columns"
        :data="models"
        :loading="loading"
        :bordered="false"
        size="small"
        :row-key="(row: any) => row.model_id"
      >
        <template #empty>
          <n-empty :description="t('ai.noModels')" />
        </template>
      </n-data-table>
    </n-card>

    <n-grid :cols="3" :x-gap="12" :y-gap="12">
      <n-gi>
        <n-card size="small" :bordered="true">
          <template #header>
            <n-space align="center" :size="8">
              <n-tag type="info" size="small" round>AI</n-tag>
              <span>{{ t('ai.typeAnomaly') }}</span>
            </n-space>
          </template>
          <n-text depth="3">{{ t('ai.presetAnomalyDesc') }}</n-text>
        </n-card>
      </n-gi>
      <n-gi>
        <n-card size="small" :bordered="true">
          <template #header>
            <n-space align="center" :size="8">
              <n-tag type="warning" size="small" round>AI</n-tag>
              <span>{{ t('ai.typeTrend') }}</span>
            </n-space>
          </template>
          <n-text depth="3">{{ t('ai.presetTrendDesc') }}</n-text>
        </n-card>
      </n-gi>
      <n-gi>
        <n-card size="small" :bordered="true">
          <template #header>
            <n-space align="center" :size="8">
              <n-tag type="success" size="small" round>AI</n-tag>
              <span>{{ t('ai.typeThreshold') }}</span>
            </n-space>
          </template>
          <n-text depth="3">{{ t('ai.presetThresholdDesc') }}</n-text>
        </n-card>
      </n-gi>
    </n-grid>

    <n-drawer v-model:show="showDetail" :width="480">
      <n-drawer-content :title="t('ai.title')">
        <template v-if="currentModel">
          <n-descriptions label-placement="left" :column="1" bordered>
            <n-descriptions-item :label="t('ai.modelName')">{{ currentModel.model_name }}</n-descriptions-item>
            <n-descriptions-item :label="t('ai.modelType')">{{ modelTypeLabel(currentModel.model_type) }}</n-descriptions-item>
            <n-descriptions-item :label="t('ai.modelVersion')">{{ currentModel.model_version }}</n-descriptions-item>
            <n-descriptions-item :label="t('ai.status')">{{ statusLabel(currentModel.status) }}</n-descriptions-item>
            <n-descriptions-item :label="t('ai.isPreset')">
              <n-tag :type="currentModel.is_preset ? 'success' : 'default'" size="small">
                {{ currentModel.is_preset ? t('common.confirm') : t('common.cancel') }}
              </n-tag>
            </n-descriptions-item>
            <n-descriptions-item :label="t('ai.modelPath')">{{ currentModel.model_file_path || '-' }}</n-descriptions-item>
            <n-descriptions-item :label="t('ai.inputSchema')">{{ currentModel.input_schema || '-' }}</n-descriptions-item>
            <n-descriptions-item :label="t('ai.outputSchema')">{{ currentModel.output_schema || '-' }}</n-descriptions-item>
            <n-descriptions-item :label="t('ai.inferenceCount')">{{ currentModel.inference_count ?? 0 }}</n-descriptions-item>
            <n-descriptions-item :label="t('ai.avgLatency')">{{ currentModel.avg_latency_ms ?? '-' }}</n-descriptions-item>
            <n-descriptions-item :label="t('ai.lastInference')">{{ currentModel.last_inference_at || '-' }}</n-descriptions-item>
          </n-descriptions>
        </template>
      </n-drawer-content>
    </n-drawer>

    <n-modal v-model:show="showInference" :title="t('ai.inference')" preset="card" style="width: 560px">
      <n-space vertical :size="12">
        <n-form-item :label="t('ai.modelName')">
          <n-select
            :value="inferenceModelId"
            :options="modelOptions"
            @update:value="inferenceModelId = $event"
            :placeholder="t('ai.modelName')"
          />
        </n-form-item>
        <n-form-item :label="t('ai.inputData')">
          <n-input v-model:value="inferenceInput" type="textarea" :rows="4" placeholder="[1.0, 2.0, 3.0]" />
        </n-form-item>
        <n-form-item v-if="inferenceResult" :label="t('ai.outputData')">
          <n-input :value="inferenceResult" type="textarea" :rows="4" readonly />
        </n-form-item>
        <n-form-item v-if="inferenceLatency" :label="t('ai.latencyMs')">
          <n-text>{{ inferenceLatency }}</n-text>
        </n-form-item>
      </n-space>
      <template #action>
        <n-button @click="showInference = false">{{ t('common.cancel') }}</n-button>
        <n-button type="primary" :loading="inferring" @click="handleInference">{{ t('common.confirm') }}</n-button>
      </template>
    </n-modal>
  </n-space>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, h, reactive } from 'vue'
import { NTag, NButton, NSpace, NPopconfirm, useMessage, useDialog } from 'naive-ui'
import { aiApi } from '@/api'
import { t } from '@/i18n'

const message = useMessage()
const dialog = useDialog()
const loading = ref(false)
const models = ref<any[]>([])
const showDetail = ref(false)
const currentModel = ref<any>(null)
const showInference = ref(false)
const inferring = ref(false)
const inferenceModelId = ref<string | null>(null)
const inferenceInput = ref('')
const inferenceResult = ref('')
const inferenceLatency = ref('')
const aiEnabled = ref(false)

const stats = reactive({
  totalCalls: 0,
  totalErrors: 0,
  avgLatencyMs: 0,
})

function modelTypeLabel(type: string) {
  const map: Record<string, string> = {
    anomaly: t('ai.typeAnomaly'),
    trend: t('ai.typeTrend'),
    threshold: t('ai.typeThreshold'),
    custom: t('ai.typeCustom'),
  }
  return map[type] || type
}

function statusLabel(status: string) {
  const map: Record<string, string> = {
    active: t('ai.statusActive'),
    inactive: t('ai.statusInactive'),
    loading: t('ai.statusLoading'),
    error: t('ai.statusError'),
    unavailable: t('ai.statusUnavailable'),
  }
  return map[status] || status
}

function statusTagType(status: string): 'success' | 'default' | 'info' | 'error' | 'warning' {
  const map: Record<string, 'success' | 'default' | 'info' | 'error' | 'warning'> = {
    active: 'success',
    inactive: 'default',
    loading: 'info',
    error: 'error',
    unavailable: 'warning',
  }
  return map[status] || 'default'
}

const modelOptions = computed(() =>
  models.value
    .filter((m: any) => m.status === 'active')
    .map((m: any) => ({ label: m.model_name, value: m.model_id }))
)

const columns = [
  { title: t('ai.modelName'), key: 'model_name', width: 180 },
  {
    title: t('ai.modelType'), key: 'model_type', width: 120,
    render: (row: any) => h(NTag, { size: 'small', type: 'info' }, { default: () => modelTypeLabel(row.model_type) }),
  },
  { title: t('ai.modelVersion'), key: 'model_version', width: 80 },
  {
    title: t('ai.status'), key: 'status', width: 100,
    render: (row: any) => h(NTag, { size: 'small', type: statusTagType(row.status) }, { default: () => statusLabel(row.status) }),
  },
  {
    title: t('ai.isPreset'), key: 'is_preset', width: 100,
    render: (row: any) => h(NTag, { size: 'small', type: row.is_preset ? 'success' : 'default' }, { default: () => row.is_preset ? t('common.confirm') : t('common.cancel') }),
  },
  {
    title: t('ai.inferenceCount'), key: 'inference_count', width: 100,
    render: (row: any) => row.inference_count ?? 0,
  },
  {
    title: t('common.actions'), key: 'action', width: 280,
    render: (row: any) => h(NSpace, { size: 4 }, {
      default: () => [
        row.status === 'active'
          ? h(NButton, { text: true, type: 'warning', size: 'small', onClick: () => handleDisable(row) }, { default: () => t('ai.disable') })
          : h(NButton, { text: true, type: 'success', size: 'small', onClick: () => handleEnable(row) }, { default: () => t('ai.enable') }),
        h(NPopconfirm, { onPositiveClick: () => handleReload(row) }, {
          trigger: () => h(NButton, { text: true, type: 'info', size: 'small' }, { default: () => t('ai.reload') }),
          default: () => t('ai.reloadConfirm'),
        }),
        h(NButton, { text: true, type: 'primary', size: 'small', onClick: () => openDetail(row) }, { default: () => t('common.edit') }),
        !row.is_preset
          ? h(NPopconfirm, { onPositiveClick: () => handleDelete(row) }, {
              trigger: () => h(NButton, { text: true, type: 'error', size: 'small' }, { default: () => t('common.delete') }),
              default: () => t('ai.deleteConfirm'),
            })
          : null,
      ].filter(Boolean),
    }),
  },
]

async function fetchModels() {
  loading.value = true
  try {
    const data = await aiApi.listModels()
    models.value = data?.data ?? []
    aiEnabled.value = models.value.some((m: any) => m.status === 'active')
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || t('common.failed'))
  } finally {
    loading.value = false
  }
}

async function fetchStats() {
  try {
    const data = await aiApi.getStats()
    stats.totalCalls = data?.total_calls ?? 0
    stats.totalErrors = data?.total_errors ?? 0
    stats.avgLatencyMs = data?.avg_latency_ms ?? 0
  } catch {
    stats.totalCalls = 0
    stats.totalErrors = 0
    stats.avgLatencyMs = 0
  }
}

function openDetail(row: any) {
  currentModel.value = row
  showDetail.value = true
}

async function handleEnable(row: any) {
  try {
    await aiApi.enableModel(row.model_id)
    message.success(t('common.success'))
    await fetchModels()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || t('common.failed'))
  }
}

async function handleDisable(row: any) {
  try {
    await aiApi.disableModel(row.model_id)
    message.success(t('common.success'))
    await fetchModels()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || t('common.failed'))
  }
}

async function handleReload(row: any) {
  try {
    await aiApi.reloadModel(row.model_id, row.model_file_path || '')
    message.success(t('common.success'))
    await fetchModels()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || t('common.failed'))
  }
}

async function handleDelete(row: any) {
  if (row.is_preset) {
    message.warning(t('ai.presetCannotDelete'))
    return
  }
  try {
    await aiApi.deleteModel(row.model_id)
    message.success(t('common.success'))
    await fetchModels()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || t('common.failed'))
  }
}

function openInference() {
  inferenceModelId.value = null
  inferenceInput.value = ''
  inferenceResult.value = ''
  inferenceLatency.value = ''
  showInference.value = true
}

async function handleInference() {
  if (!inferenceModelId.value) {
    message.warning(t('ai.modelName'))
    return
  }
  inferring.value = true
  inferenceResult.value = ''
  inferenceLatency.value = ''
  try {
    const inputData = JSON.parse(inferenceInput.value)
    const start = Date.now()
    const result = await aiApi.inference(inferenceModelId.value, inputData)
    inferenceLatency.value = String(Date.now() - start)
    inferenceResult.value = JSON.stringify(result, null, 2)
  } catch (e: any) {
    inferenceResult.value = e?.response?.data?.detail || e?.message || t('common.failed')
    message.error(t('common.failed'))
  } finally {
    inferring.value = false
  }
}

onMounted(() => {
  fetchModels()
  fetchStats()
})
</script>
