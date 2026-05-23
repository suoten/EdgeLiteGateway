<template>
  <n-space vertical :size="16">
    <n-card :bordered="false" class="ai-engine-panel">
      <n-grid :cols="24" :x-gap="16" align="center">
        <n-gi :span="8">
          <n-space align="center" :size="16">
            <span style="font-size:48px">🧠</span>
            <div>
              <div style="font-size:20px;font-weight:700;color:#fff">{{ t('ai.engineStatus') }}</div>
              <n-tag :type="aiEnabled ? 'success' : 'default'" size="small" :bordered="false" round>
                {{ aiEnabled ? t('ai.statusActive') : t('ai.statusUnavailable') }}
              </n-tag>
            </div>
          </n-space>
        </n-gi>
        <n-gi :span="16">
          <n-grid :cols="4" :x-gap="16">
            <n-gi>
              <n-statistic :label="t('ai.modelCount')" class="engine-stat">
                <template #default><span class="engine-stat-num">{{ models.length }}</span></template>
              </n-statistic>
            </n-gi>
            <n-gi>
              <n-statistic :label="t('ai.activeModels')" class="engine-stat">
                <template #default><span class="engine-stat-num">{{ activeModelCount }}</span></template>
              </n-statistic>
            </n-gi>
            <n-gi>
              <n-statistic :label="t('ai.totalCalls')" class="engine-stat">
                <template #default><span class="engine-stat-num">{{ stats.totalCalls }}</span></template>
              </n-statistic>
            </n-gi>
            <n-gi>
              <n-statistic :label="t('ai.avgLatency')" class="engine-stat">
                <template #default><span class="engine-stat-num">{{ stats.avgLatencyMs }}</span></template>
                <template #suffix><span class="engine-stat-suffix">ms</span></template>
              </n-statistic>
            </n-gi>
          </n-grid>
        </n-gi>
      </n-grid>
      <div class="engine-footer">
        <n-text class="engine-footer-text">{{ t('ai.engineVersion') }}: ONNX Runtime</n-text>
        <n-divider vertical />
        <n-text class="engine-footer-text">{{ t('ai.engineDevice') }}: CPU</n-text>
      </div>
    </n-card>

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

      <n-tabs type="line" animated>
        <n-tab-pane name="preset" :tab="t('ai.presetModels')">
          <n-grid :cols="3" :x-gap="12" :y-gap="12">
            <n-gi v-for="m in presetModels" :key="m.model_id">
              <n-card size="small" :bordered="true" hoverable class="model-card">
                <template #header>
                  <n-space align="center" :size="8">
                    <span style="font-size:20px">{{ modelIcon(m.model_type) }}</span>
                    <span style="font-weight:600">{{ m.model_name }}</span>
                    <n-tag size="small" :bordered="false" round>{{ m.model_version }}</n-tag>
                  </n-space>
                </template>
                <template #header-extra>
                  <n-space align="center" :size="6">
                    <span :class="['status-dot', `status-dot-${m.status}`]" />
                    <n-text depth="3" style="font-size:12px">{{ statusLabel(m.status) }}</n-text>
                  </n-space>
                </template>
                <n-text depth="3" style="font-size:13px">{{ presetDesc(m.model_id) }}</n-text>
                <template #action>
                  <n-space justify="space-between" align="center">
                    <n-switch
                      :value="m.status === 'active'"
                      @update:value="m.status === 'active' ? handleDisable(m) : handleEnable(m)"
                      size="small"
                    >
                      <template #checked>{{ t('ai.statusActive') }}</template>
                      <template #unchecked>{{ t('ai.statusInactive') }}</template>
                    </n-switch>
                    <n-button size="tiny" type="primary" @click="quickInference(m)">{{ t('ai.quickInference') }}</n-button>
                  </n-space>
                </template>
              </n-card>
            </n-gi>
          </n-grid>
        </n-tab-pane>
        <n-tab-pane name="custom" :tab="t('ai.customModels')">
          <n-data-table
            :columns="customColumns"
            :data="customModels"
            :loading="loading"
            :bordered="false"
            size="small"
            :row-key="(row: any) => row.model_id"
          >
            <template #empty>
              <n-empty :description="t('ai.noModels')" />
            </template>
          </n-data-table>
        </n-tab-pane>
      </n-tabs>
    </n-card>

    <n-card v-if="activeModelsWithStats.length > 0" :bordered="false">
      <template #header>
        <span style="font-size: 16px; font-weight: 600">{{ t('ai.modelPerformance') }}</span>
      </template>
      <n-space vertical :size="12">
        <n-space v-for="m in activeModelsWithStats" :key="m.model_id" align="center" :size="12">
          <n-tag size="small" :bordered="false" round type="info">{{ m.model_name }}</n-tag>
          <n-space align="center" :size="8" style="flex:1">
            <n-text depth="3" style="font-size:12px;white-space:nowrap">{{ t('ai.inferenceCount') }}:</n-text>
            <n-progress type="line" :percentage="m.callPercent" :color="'#8b5cf6'" style="width:120px" :show-indicator="false" />
            <n-text depth="3" style="font-size:12px">{{ m.inference_count ?? 0 }}</n-text>
          </n-space>
          <n-space align="center" :size="8">
            <n-text depth="3" style="font-size:12px;white-space:nowrap">{{ t('ai.errorRate') }}:</n-text>
            <n-progress type="line" :percentage="m.errorPercent" :color="m.errorPercent > 10 ? '#f56c6c' : '#67c23a'" style="width:80px" :show-indicator="false" />
            <n-text depth="3" style="font-size:12px">{{ m.errorPercent }}%</n-text>
          </n-space>
        </n-space>
      </n-space>
    </n-card>

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

    <n-modal v-model:show="showInference" preset="card" style="width: 640px" :title="t('ai.inference')">
      <n-space vertical :size="16">
        <n-form-item :label="t('ai.modelName')">
          <n-select
            :value="inferenceModelId"
            :options="modelOptions"
            @update:value="inferenceModelId = $event"
            :placeholder="t('ai.modelName')"
          />
        </n-form-item>
        <n-form-item :label="t('ai.inputData')">
          <n-space vertical :size="8" style="width:100%">
            <n-input v-model:value="inferenceInput" type="textarea" :rows="4" placeholder="[1.0, 2.0, 3.0]" />
            <n-button size="tiny" @click="fillSimulatedData">{{ t('ai.useSimulatedData') }}</n-button>
          </n-space>
        </n-form-item>
        <template v-if="inferenceResult">
          <n-card size="small" :bordered="true">
            <n-space vertical :size="12">
              <n-space align="center" :size="12">
                <n-text strong style="font-size:13px">{{ t('ai.anomalyScore') }}</n-text>
              </n-space>
              <n-space align="center" :size="16">
                <span style="font-size:36px;font-weight:700;color:#8b5cf6">{{ anomalyScoreDisplay }}</span>
                <n-progress type="circle" :percentage="anomalyScorePercent" :stroke-width="10" :color="anomalyScorePercent > 80 ? '#f56c6c' : '#8b5cf6'" style="--n-size:80px" />
              </n-space>
              <n-space align="center" :size="8">
                <n-text depth="3">{{ t('ai.latencyMs') }}:</n-text>
                <n-text strong>{{ inferenceLatency }}</n-text>
                <n-text depth="3">ms</n-text>
              </n-space>
              <n-collapse>
                <n-collapse-item :title="t('ai.rawOutput')" name="raw">
                  <n-code :code="inferenceResult" language="json" />
                </n-collapse-item>
              </n-collapse>
            </n-space>
          </n-card>
        </template>
      </n-space>
      <template #action>
        <n-button @click="showInference = false">{{ t('common.cancel') }}</n-button>
        <n-button type="primary" :loading="inferring" @click="handleInference">{{ t('ai.inference') }}</n-button>
      </template>
    </n-modal>
  </n-space>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, h, reactive } from 'vue'
import { NTag, NButton, NSpace, NPopconfirm, NTooltip, useMessage, useDialog } from 'naive-ui'
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

const presetModels = computed(() => models.value.filter((m: any) => m.is_preset))
const customModels = computed(() => models.value.filter((m: any) => !m.is_preset))
const activeModelCount = computed(() => models.value.filter((m: any) => m.status === 'active').length)

const activeModelsWithStats = computed(() => {
  const maxCalls = Math.max(...models.value.map((m: any) => m.inference_count ?? 0), 1)
  return models.value
    .filter((m: any) => m.status === 'active')
    .map((m: any) => ({
      ...m,
      callPercent: Math.round(((m.inference_count ?? 0) / maxCalls) * 100),
      errorPercent: m.inference_count > 0 ? Math.round(((m.error_count ?? 0) / m.inference_count) * 100) : 0,
    }))
})

const anomalyScoreDisplay = computed(() => {
  try {
    const parsed = JSON.parse(inferenceResult.value)
    const score = parsed?.output_0?.[0] ?? parsed?.anomaly_score
    return score != null ? Number(score).toFixed(4) : '-'
  } catch { return '-' }
})
const anomalyScorePercent = computed(() => {
  try {
    const parsed = JSON.parse(inferenceResult.value)
    const score = parsed?.output_0?.[0] ?? parsed?.anomaly_score
    return score != null ? Math.round(Math.min(Math.abs(Number(score)), 1) * 100) : 0
  } catch { return 0 }
})

function modelIcon(type: string) {
  const map: Record<string, string> = { anomaly: '🔴', trend: '📈', threshold: '⚡', custom: '🧪' }
  return map[type] || '🧪'
}

function presetDesc(modelId: string) {
  const map: Record<string, string> = {
    'preset-anomaly-v1': t('ai.presetAnomalyDesc'),
    'preset-trend-v1': t('ai.presetTrendDesc'),
    'preset-threshold-v1': t('ai.presetThresholdDesc'),
    'preset-vibration-v1': t('ai.presetVibrationDesc'),
    'preset-power-v1': t('ai.presetPowerDesc'),
    'preset-quality-v1': t('ai.presetQualityDesc'),
    'preset-battery-v1': t('ai.presetBatteryDesc'),
    'preset-leak-v1': t('ai.presetLeakDesc'),
  }
  return map[modelId] || ''
}

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
        h(NButton, { text: true, type: 'primary', size: 'small', onClick: () => openDetail(row) }, { default: () => t('ai.detail') }),
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

const customColumns = [
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
  { title: t('ai.modelPath'), key: 'model_file_path', width: 200, ellipsis: { tooltip: true } },
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
          : row.status === 'unavailable'
            ? h(NTooltip, {}, { trigger: () => h(NButton, { text: true, type: 'success', size: 'small', onClick: () => handleEnable(row) }, { default: () => t('ai.enable') }), default: () => t('ai.cannotEnableUnavailable') })
            : h(NButton, { text: true, type: 'success', size: 'small', onClick: () => handleEnable(row) }, { default: () => t('ai.enable') }),
        h(NPopconfirm, { onPositiveClick: () => handleReload(row) }, {
          trigger: () => h(NButton, { text: true, type: 'info', size: 'small' }, { default: () => t('ai.reload') }),
          default: () => t('ai.reloadConfirm'),
        }),
        h(NButton, { text: true, type: 'primary', size: 'small', onClick: () => openDetail(row) }, { default: () => t('ai.detail') }),
        h(NPopconfirm, { onPositiveClick: () => handleDelete(row) }, {
          trigger: () => h(NButton, { text: true, type: 'error', size: 'small' }, { default: () => t('common.delete') }),
          default: () => t('ai.deleteConfirm'),
        }),
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

function quickInference(m: any) {
  inferenceModelId.value = m.model_id
  inferenceInput.value = ''
  inferenceResult.value = ''
  inferenceLatency.value = ''
  showInference.value = true
}

function fillSimulatedData() {
  const target = models.value.find((m: any) => m.model_id === inferenceModelId.value)
  if (!target) {
    inferenceInput.value = JSON.stringify(Array.from({ length: 10 }, () => Math.random()))
    return
  }
  try {
    const schema = typeof target.input_schema === 'string' ? JSON.parse(target.input_schema) : target.input_schema
    const size = schema?.shape?.[1] ?? 10
    inferenceInput.value = JSON.stringify(Array.from({ length: size }, () => Math.round(Math.random() * 100) / 10))
  } catch {
    inferenceInput.value = JSON.stringify(Array.from({ length: 10 }, () => Math.random()))
  }
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

<style scoped>
.ai-engine-panel {
  background: linear-gradient(135deg, #8b5cf6 0%, #6366f1 100%);
  border: none;
  border-radius: 12px;
}
.engine-stat :deep(.n-statistic__label) {
  color: rgba(255,255,255,0.7) !important;
  font-size: 12px;
}
.engine-stat-num {
  color: #fff;
  font-size: 22px;
  font-weight: 700;
}
.engine-stat-suffix {
  color: rgba(255,255,255,0.8);
  font-size: 13px;
}
.engine-footer {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid rgba(255,255,255,0.2);
}
.engine-footer-text {
  color: rgba(255,255,255,0.7) !important;
  font-size: 12px;
}
.model-card {
  transition: all 0.3s ease;
}
.model-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 6px 16px rgba(0,0,0,0.08);
}
.status-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
}
.status-dot-active {
  background: #67c23a;
  animation: status-pulse 2s ease-in-out infinite;
}
.status-dot-inactive { background: #c0c4cc; }
.status-dot-unavailable { background: #f56c6c; }
.status-dot-error { background: #f56c6c; }
.status-dot-loading { background: #e6a23c; }
@keyframes status-pulse {
  0% { box-shadow: 0 0 0 0 rgba(103,194,58,0.4); }
  70% { box-shadow: 0 0 0 6px rgba(103,194,58,0); }
  100% { box-shadow: 0 0 0 0 rgba(103,194,58,0); }
}
</style>
