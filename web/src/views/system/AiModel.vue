<template>
  <n-space vertical :size="16">
    <n-card :bordered="false" class="ai-engine-panel">
      <n-grid :cols="24" :x-gap="16" align="center">
        <n-gi :span="8">
          <n-space align="center" :size="16">
            <n-icon :component="SparklesOutline" :size="48" color="#8b5cf6" />
            <div>
              <div style="font-size:20px;font-weight:700;color:#fff">{{ t('ai.engineStatus') }}</div>
              <n-tag :type="engineStatusType" size="small" :bordered="false" round>
                {{ engineStatusText }}
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
          <n-tag :type="engineStatusType" size="small" round>
          {{ engineStatusText }}
        </n-tag>
          <span style="font-size: 16px; font-weight: 600">{{ t('ai.manageModels') }}</span>
        </n-space>
      </template>
      <template #header-extra>
        <n-space :size="8">
          <n-button size="small" type="primary" @click="openInference">{{ t('ai.inference') }}</n-button>
          <n-button size="small" @click="showUploadModal = true">{{ t('aiModel.uploadModel') }}</n-button>
          <n-button size="small" @click="showScheduleModal = true">{{ t('aiModel.scheduleInference') }}</n-button>
          <n-button size="small" @click="fetchModels" :loading="loading">{{ t('common.refresh') }}</n-button>
        </n-space>
      </template>

      <n-space :size="12" style="margin-bottom: 12px">
        <n-input v-model:value="filterName" :placeholder="t('common.search')" clearable size="small" style="width: 200px" />
        <n-select v-model:value="filterType" :options="typeOptions" :placeholder="t('ai.modelType')" clearable size="small" style="width: 140px" />
        <n-select v-model:value="filterStatus" :options="statusOptions" :placeholder="t('ai.status')" clearable size="small" style="width: 140px" />
      </n-space>

      <n-tabs type="line" animated>
        <n-tab-pane name="preset" :tab="t('ai.presetModels')">
          <n-grid :cols="3" :x-gap="12" :y-gap="12">
            <n-gi v-for="m in presetModels" :key="m.model_id">
              <n-card size="small" :bordered="true" hoverable class="model-card">
                <template #header>
                  <n-space align="center" :size="8">
                    <component :is="modelIcon(m.model_type)" />
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
                      :loading="togglingModelId === m.model_id"
                      :disabled="!!togglingModelId"
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
            :data="customModelsPaginated"
            :loading="loading"
            :bordered="false"
            size="small"
            :row-key="(row: any) => row.model_id"
            :pagination="customPaginationProps"
            :scroll-x="1010"
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

    <n-drawer v-model:show="showDetail" :width="520">
      <n-drawer-content :title="t('ai.title')">
        <template v-if="currentModel">
          <n-tabs v-model:value="detailTab" type="line">
            <n-tab-pane name="info" :tab="t('ai.detail')">
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
            </n-tab-pane>
            <n-tab-pane name="logs" :tab="t('ai.inferenceLogs')">
              <n-spin :show="inferenceLogsLoading">
                <n-data-table
                  v-if="inferenceLogs.length > 0"
                  :columns="[
                    { title: t('ai.modelName'), key: 'model_id', width: 120, ellipsis: { tooltip: true } },
                    { title: t('ai.latencyMs'), key: 'latency_ms', width: 80 },
                    { title: t('ai.status'), key: 'status', width: 80 },
                    { title: t('ai.lastInference'), key: 'created_at', width: 140, ellipsis: { tooltip: true } },
                  ]"
                  :data="inferenceLogs"
                  :bordered="false"
                  size="small"
                  :row-key="(row: any) => row.id ?? row.log_id ?? Math.random()"
                />
                <n-empty v-else :description="t('ai.noLogs')" />
                <n-space justify="center" style="margin-top: 12px" v-if="inferenceLogsTotal > 10">
                  <n-pagination
                    v-model:page="inferenceLogsPage"
                    :item-count="inferenceLogsTotal"
                    :page-size="inferenceLogsPageSize"
                    :page-sizes="[10, 20, 50, 100]"
                    :show-size-picker="true"
                    size="small"
                    @update:page="(p: number) => fetchInferenceLogs(currentModel?.model_id, p)"
                    @update:page-size="(s: number) => { inferenceLogsPageSize = s; inferenceLogsPage = 1; fetchInferenceLogs(currentModel?.model_id, 1) }"
                  />
                </n-space>
              </n-spin>
            </n-tab-pane>
            <n-tab-pane name="versions" :tab="t('aiModel.versionHistory')">
              <n-spin :show="versionHistoryLoading">
                <n-data-table
                  v-if="versionHistory.length > 0"
                  :columns="versionColumns"
                  :data="versionHistory"
                  :bordered="false"
                  size="small"
                  :row-key="(row: any) => row.version + row.timestamp"
                />
                <n-empty v-else :description="t('aiModel.noVersionHistory')" />
              </n-spin>
            </n-tab-pane>
          </n-tabs>
        </template>
      </n-drawer-content>
    </n-drawer>

    <n-modal v-model:show="showInference" preset="card" style="width: 640px; max-width: 95vw" :title="t('ai.inference')" :close-on-esc="true" :auto-focus="true" :close-on-esc-aria-label="t('common.closeDialog')">
      <n-space vertical :size="16">
        <n-form-item :label="t('ai.modelName')">
          <n-select
            :value="inferenceModelId"
            :options="modelOptions"
            @update:value="(v: string) => { inferenceModelId = v; validateInput() }"
            :placeholder="t('ai.modelName')"
          />
        </n-form-item>
        <n-form-item :label="t('ai.inputData')" :validation-status="inputValidationStatus" :feedback="inputFeedback">
          <n-space vertical :size="8" style="width:100%">
            <n-input v-model:value="inferenceInput" type="textarea" :rows="4" placeholder="[1.0, 2.0, 3.0]" @update:value="validateInput" />
            <n-space :size="8">
              <n-button size="tiny" @click="fillSimulatedData">{{ t('ai.useSimulatedData') }}</n-button>
              <n-text v-if="inputHint" depth="3" style="font-size:12px">{{ inputHint }}</n-text>
            </n-space>
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

    <!-- Schedule Inference Modal -->
    <n-modal v-model:show="showScheduleModal" preset="card" style="width: 600px; max-width: 95vw" :title="t('aiModel.scheduleInference')" :close-on-esc="true" :auto-focus="true" :close-on-esc-aria-label="t('common.closeDialog')">
      <n-space vertical :size="16">
        <!-- Active schedules -->
        <n-card size="small" :bordered="true" v-if="schedules.length > 0">
          <template #header><span style="font-size:13px;font-weight:600">{{ t('aiModel.scheduleInference') }}</span></template>
          <n-data-table
            :columns="scheduleColumns"
            :data="schedules"
            :bordered="false"
            size="small"
            :row-key="(row: any) => row.model_id"
          />
        </n-card>
        <n-empty v-else :description="t('aiModel.noSchedules')" />
        <!-- New schedule form -->
        <n-divider />
        <n-form-item :label="t('ai.modelName')">
          <n-select
            v-model:value="scheduleModelId"
            :options="modelOptions"
            :placeholder="t('ai.modelName')"
          />
        </n-form-item>
        <n-form-item :label="t('aiModel.intervalSeconds')">
          <n-input-number v-model:value="scheduleInterval" :min="5" :max="3600" style="width: 100%" />
        </n-form-item>
        <n-form-item :label="t('aiModel.inputWindowSize')">
          <n-input-number v-model:value="scheduleWindowSize" :min="1" :max="10000" style="width: 100%" />
        </n-form-item>
      </n-space>
      <template #action>
        <n-button @click="showScheduleModal = false">{{ t('common.cancel') }}</n-button>
        <n-button type="primary" :loading="scheduleLoading" @click="handleStartSchedule">{{ t('aiModel.startSchedule') }}</n-button>
      </template>
    </n-modal>

    <!-- Upload Model Modal -->
    <n-modal v-model:show="showUploadModal" preset="card" style="width: 500px; max-width: 95vw" :title="t('aiModel.uploadModel')" :close-on-esc="true" :auto-focus="true" :close-on-esc-aria-label="t('common.closeDialog')">
      <n-space vertical :size="16">
        <n-form-item :label="t('ai.modelName')">
          <n-input v-model:value="uploadModelName" :placeholder="t('ai.modelName')" />
        </n-form-item>
        <n-form-item :label="t('aiModel.selectFile')">
          <n-upload
            :max="1"
            accept=".onnx,.pt,.pth,.pkl,.h5,.tflite"
            :default-upload="false"
            @change="handleFileChange"
          >
            <n-button>{{ t('aiModel.selectFile') }}</n-button>
          </n-upload>
        </n-form-item>
      </n-space>
      <template #action>
        <n-button @click="showUploadModal = false">{{ t('common.cancel') }}</n-button>
        <n-button type="primary" :loading="uploadLoading" :disabled="!uploadFile" @click="handleUpload">{{ t('aiModel.uploadModel') }}</n-button>
      </template>
    </n-modal>
  </n-space>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, h, reactive, watch, toRaw, markRaw } from 'vue'
import { NTag, NButton, NSpace, NPopconfirm, NTooltip, NIcon } from 'naive-ui'
import { SparklesOutline, HardwareChipOutline, AnalyticsOutline, WarningOutline, FlaskOutline, TrendingUpOutline, FlashOutline } from '@vicons/ionicons5'
import { aiApi } from '@/api'
import { t } from '@/i18n'
import { extractError } from '@/utils/errorCodes'
import { connect as wsConnect, disconnect as wsDisconnect } from '@/api/websocket'
import { usePageVisibility } from '@/composables/usePageVisibility'
import { message, dialog } from '@/utils/discreteApi'
// [AUDIT-FIX] 严重级-敏感操作（启停/删除/回滚/调度）需函数级权限校验，与路由级 RBAC 形成双重防护
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

// FIX: 生产构建中 h() 在模板里调用报 "h is not defined"。
// 原因：Vite 模板编译器生成的代码中，h 的引用在生产构建时与 import 变量名不一致。
// 解决：所有 h()-created VNode 在 setup 中创建并通过 defineExpose 暴露给模板。
const _h = h  // 避免模板直接引用 h
const gaugeIcon = markRaw({ anomaly: _h(NIcon, { component: WarningOutline, size: 14, color: '#f56c6c' }), trend: _h(NIcon, { component: TrendingUpOutline, size: 14, color: '#67c23a' }), threshold: _h(NIcon, { component: FlashOutline, size: 14, color: '#e6a23c' }), custom: _h(NIcon, { component: FlaskOutline, size: 14, color: '#8b5cf6' }), default: _h(NIcon, { component: HardwareChipOutline, size: 14 }) })
const modelIcon = (type: string) => (gaugeIcon as Record<string, any>)[type] || (gaugeIcon as Record<string, any>).default

const loading = ref(false)
const models = ref<any[]>([])
const showDetail = ref(false)
const currentModel = ref<any>(null)
const showInference = ref(false)
const inferring = ref(false)
const inferenceModelId = ref<string | null>(null)
const inferenceInput = ref('')
const inputValidationStatus = ref<'error' | 'warning' | undefined>(undefined)
const inputFeedback = ref('')
const inputHint = ref('')
const inferenceResult = ref('')
const inferenceLatency = ref('')
const aiEnabled = ref(false)
// [AUDIT-FIX] 严重级-AI 模型启用/禁用 switch 无 loading，防止并发切换产生竞态
const togglingModelId = ref<string | null>(null)
const engineStatusText = computed(() => {
  if (aiEnabled.value) return t('ai.statusActive')
  const hasInactive = models.value.some((m: any) => m.status === 'inactive')
  const hasUnavailable = models.value.some((m: any) => m.status === 'unavailable')
  if (hasInactive && !hasUnavailable) return t('ai.statusOnnxruntimeMissing')
  if (hasUnavailable && !hasInactive) return t('ai.statusFileMissing')
  if (hasInactive && hasUnavailable) return t('ai.statusFileAndRuntimeMissing')
  return t('ai.statusInactive')
})
const engineStatusType = computed(() => {
  if (aiEnabled.value) return 'success'
  const hasUnavailable = models.value.some((m: any) => m.status === 'unavailable')
  return hasUnavailable ? 'error' : 'warning'
})

const stats = reactive({
  totalCalls: 0,
  totalErrors: 0,
  avgLatencyMs: 0,
})

// 搜索/过滤
const filterName = ref('')
const filterType = ref<string | null>(null)
const filterStatus = ref<string | null>(null)

// 过滤器变化时重置分页
watch([filterName, filterType, filterStatus], () => {
  customPagination.page = 1
})

// 自定义模型分页
const customPagination = reactive({
  page: 1,
  pageSize: 20,
  itemCount: 0,
  pageSizes: [10, 20, 50, 100],
  onChange: (p: number) => { customPagination.page = p },
  onUpdatePageSize: (s: number) => { customPagination.pageSize = s; customPagination.page = 1 },
})
const typeOptions = computed(() => {
  const types = [...new Set(models.value.map((m: any) => m.model_type))]
  return types.map(t => ({ label: modelTypeLabel(t), value: t }))
})
const statusOptions = computed(() => [
  { label: t('ai.statusActive'), value: 'active' },
  { label: t('ai.statusInactive'), value: 'inactive' },
  { label: t('ai.statusError'), value: 'error' },
  { label: t('ai.statusFileMissing'), value: 'unavailable' },
])

// 推理日志
const inferenceLogs = ref<any[]>([])
const inferenceLogsLoading = ref(false)
const inferenceLogsPage = ref(1)
const inferenceLogsTotal = ref(0)
// FIXED-P1: 分页大小可调，原硬编码 page-size=10 缺少 pageSizes 选项
const inferenceLogsPageSize = ref(10)
const detailTab = ref('info')

const versionHistory = ref<any[]>([])
const versionHistoryLoading = ref(false)

const versionColumns = computed(() => [
  { title: t('aiModel.version'), key: 'version', width: 160 },
  { title: t('ai.status'), key: 'status', width: 80 },
  { title: t('aiModel.versionTimestamp'), key: 'timestamp', width: 180 },
  {
    title: t('common.actions'), key: 'action', width: 100,
    render: (row: any) => h(NPopconfirm, { onPositiveClick: () => handleRollback(row.version) }, {
      trigger: () => h(NButton, { text: true, type: 'warning', size: 'small', disabled: row.version === currentModel.value?.model_version }, { default: () => t('aiModel.rollback') }),
      default: () => t('aiModel.rollbackConfirm'),
    }),
  },
])

// 定时刷新
let statsTimer: ReturnType<typeof setInterval> | null = null
let aiWsHandler: ((data: any) => void) | null = null

// Schedule inference state
const showScheduleModal = ref(false)
const scheduleModelId = ref<string | null>(null)
const scheduleInterval = ref(60)
const scheduleWindowSize = ref(10)
const scheduleLoading = ref(false)
const schedules = ref<any[]>([])

const scheduleColumns = computed(() => [
  { title: t('ai.modelName'), key: 'model_id', width: 180 },
  { title: t('aiModel.intervalSeconds'), key: 'interval_seconds', width: 100 },
  { title: t('aiModel.inputWindowSize'), key: 'input_window_size', width: 120 },
  {
    title: t('common.actions'), key: 'action', width: 100,
    render: (row: any) => h(NPopconfirm, { onPositiveClick: () => handleStopSchedule(toRaw(row).model_id) }, {
      trigger: () => h(NButton, { text: true, type: 'error', size: 'small' }, { default: () => t('aiModel.stopSchedule') }),
      default: () => t('aiModel.stopSchedule') + '?',
    }),
  },
])

// Upload model state
const showUploadModal = ref(false)
const uploadModelName = ref('')
const uploadFile = ref<File | null>(null)
const uploadLoading = ref(false)

const presetModels = computed(() => {
  let list = models.value.filter((m: any) => m.is_preset)
  if (filterName.value) list = list.filter((m: any) => m.model_name.toLowerCase().includes(filterName.value.toLowerCase()))
  if (filterType.value) list = list.filter((m: any) => m.model_type === filterType.value)
  if (filterStatus.value) list = list.filter((m: any) => m.status === filterStatus.value)
  return list
})
const customModels = computed(() => {
  let list = models.value.filter((m: any) => !m.is_preset)
  if (filterName.value) list = list.filter((m: any) => m.model_name.toLowerCase().includes(filterName.value.toLowerCase()))
  if (filterType.value) list = list.filter((m: any) => m.model_type === filterType.value)
  if (filterStatus.value) list = list.filter((m: any) => m.status === filterStatus.value)
  return list
})
const customModelsPaginated = computed(() => {
  const start = (customPagination.page - 1) * customPagination.pageSize
  const end = start + customPagination.pageSize
  return customModels.value.slice(start, end)
})
// FIXED: 使用 computed 派生分页对象，避免在 customModels computed 中产生副作用
const customPaginationProps = computed(() => ({
  ...customPagination,
  itemCount: customModels.value.length,
}))
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
    unavailable: t('ai.statusFileMissing'),
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

const columns = computed(() => [
  { title: t('ai.modelName'), key: 'model_name', width: 180 },
  {
    title: t('ai.modelType'), key: 'model_type', width: 120,
    render: (row: any) => h(NTag, { size: 'small', type: 'info' }, { default: () => modelTypeLabel(toRaw(row).model_type) }),
  },
  { title: t('ai.modelVersion'), key: 'model_version', width: 80 },
  {
    title: t('ai.status'), key: 'status', width: 100,
    render: (row: any) => h(NTag, { size: 'small', type: statusTagType(toRaw(row).status) }, { default: () => statusLabel(toRaw(row).status) }),
  },
  {
    title: t('ai.isPreset'), key: 'is_preset', width: 100,
    render: (row: any) => h(NTag, { size: 'small', type: toRaw(row).is_preset ? 'success' : 'default' }, { default: () => toRaw(row).is_preset ? t('common.confirm') : t('common.cancel') }),
  },
  {
    title: t('ai.inferenceCount'), key: 'inference_count', width: 100,
    render: (row: any) => toRaw(row).inference_count ?? 0,
  },
  {
    title: t('common.actions'), key: 'action', width: 280,
    render: (row: any) => {
      const r = toRaw(row)
      return h(NSpace, { size: 4 }, {
        default: () => [
          r.status === 'active'
            ? h(NButton, { text: true, type: 'warning', size: 'small', onClick: () => handleDisable(toRaw(row)) }, { default: () => t('ai.disable') })
            : r.status === 'unavailable'
              ? h(NTooltip, {}, { trigger: () => h(NButton, { text: true, type: 'success', size: 'small', onClick: () => handleEnable(toRaw(row)) }, { default: () => t('ai.enable') }), default: () => t('ai.cannotEnableUnavailable') })
              : r.status === 'inactive'
                ? h(NTooltip, {}, { trigger: () => h(NButton, { text: true, type: 'success', size: 'small', onClick: () => handleEnable(toRaw(row)) }, { default: () => t('ai.enable') }), default: () => t('ai.installOnnxruntimeTip') })
                : h(NButton, { text: true, type: 'success', size: 'small', onClick: () => handleEnable(toRaw(row)) }, { default: () => t('ai.enable') }),
          h(NPopconfirm, { onPositiveClick: () => handleReload(toRaw(row)) }, {
            trigger: () => h(NButton, { text: true, type: 'info', size: 'small' }, { default: () => t('ai.reload') }),
            default: () => t('ai.reloadConfirm'),
          }),
          h(NButton, { text: true, type: 'primary', size: 'small', onClick: () => openDetail(toRaw(row)) }, { default: () => t('ai.detail') }),
          !r.is_preset
            ? h(NPopconfirm, { onPositiveClick: () => handleDelete(toRaw(row)) }, {
                trigger: () => h(NButton, { text: true, type: 'error', size: 'small' }, { default: () => t('common.delete') }),
                default: () => t('ai.deleteConfirm'),
              })
            : null,
        ].filter(Boolean),
      })
    },
  },
])

const customColumns = computed(() => [
  { title: t('ai.modelName'), key: 'model_name', width: 180 },
  {
    title: t('ai.modelType'), key: 'model_type', width: 120,
    render: (row: any) => h(NTag, { size: 'small', type: 'info' }, { default: () => modelTypeLabel(toRaw(row).model_type) }),
  },
  { title: t('ai.modelVersion'), key: 'model_version', width: 80 },
  {
    title: t('ai.status'), key: 'status', width: 100,
    render: (row: any) => h(NTag, { size: 'small', type: statusTagType(toRaw(row).status) }, { default: () => statusLabel(toRaw(row).status) }),
  },
  { title: t('ai.modelPath'), key: 'model_file_path', width: 200, ellipsis: { tooltip: true } },
  {
    title: t('ai.inferenceCount'), key: 'inference_count', width: 100,
    render: (row: any) => toRaw(row).inference_count ?? 0,
  },
  {
    title: t('common.actions'), key: 'action', width: 280,
    render: (row: any) => {
      const r = toRaw(row)
      return h(NSpace, { size: 4 }, {
        default: () => [
          r.status === 'active'
            ? h(NButton, { text: true, type: 'warning', size: 'small', onClick: () => handleDisable(toRaw(row)) }, { default: () => t('ai.disable') })
            : r.status === 'unavailable'
              ? h(NTooltip, {}, { trigger: () => h(NButton, { text: true, type: 'success', size: 'small', onClick: () => handleEnable(toRaw(row)) }, { default: () => t('ai.enable') }), default: () => t('ai.cannotEnableUnavailable') })
              : h(NButton, { text: true, type: 'success', size: 'small', onClick: () => handleEnable(toRaw(row)) }, { default: () => t('ai.enable') }),
          h(NPopconfirm, { onPositiveClick: () => handleReload(toRaw(row)) }, {
            trigger: () => h(NButton, { text: true, type: 'info', size: 'small' }, { default: () => t('ai.reload') }),
            default: () => t('ai.reloadConfirm'),
          }),
          h(NButton, { text: true, type: 'primary', size: 'small', onClick: () => openDetail(toRaw(row)) }, { default: () => t('ai.detail') }),
          h(NPopconfirm, { onPositiveClick: () => handleDelete(toRaw(row)) }, {
            trigger: () => h(NButton, { text: true, type: 'error', size: 'small' }, { default: () => t('common.delete') }),
            default: () => t('ai.deleteConfirm'),
          }),
        ].filter(Boolean),
      })
    },
  },
])

async function fetchModels() {
  loading.value = true
  try {
    const data = await aiApi.listModels()
    // FIX: API返回数据中的非序列化字段（如类的实例、循环引用）直接赋值给响应式ref，
    // 会导致 JSON.stringify 循环引用报错。使用结构化克隆创建纯净对象。
    const rawList = JSON.parse(JSON.stringify(data?.data ?? []))
    models.value = rawList
    aiEnabled.value = models.value.some((m: any) => m.status === 'active')
    customPagination.page = 1
  } catch (e: any) {
    message.error(extractError(e, t('common.failed')))
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
  detailTab.value = 'info'
  showDetail.value = true
  fetchInferenceLogs(row.model_id)
  fetchVersionHistory(row.model_id)
}

async function fetchVersionHistory(modelId: string) {
  versionHistoryLoading.value = true
  try {
    const data = await aiApi.getModelVersions(modelId)
    versionHistory.value = data || []
  } catch {
    versionHistory.value = []
  } finally {
    versionHistoryLoading.value = false
  }
}

async function handleRollback(targetVersion: string) {
  if (!auth.isOperator) { message.warning(t('common.permissionDenied')); return }
  if (!currentModel.value) return
  try {
    await aiApi.rollbackModel(currentModel.value.model_id, targetVersion)
    message.success(t('common.success'))
    await fetchModels()
    await fetchVersionHistory(currentModel.value.model_id)
  } catch (e: any) {
    message.error(extractError(e, t('common.failed')))
  }
}

async function fetchInferenceLogs(modelId?: string, page = 1) {
  inferenceLogsLoading.value = true
  try {
    const data = await aiApi.getInferenceLogs(modelId, page, inferenceLogsPageSize.value)
    inferenceLogs.value = data?.data ?? []
    inferenceLogsTotal.value = data?.total ?? 0
    inferenceLogsPage.value = page
  } catch {
    inferenceLogs.value = []
    inferenceLogsTotal.value = 0
  } finally {
    inferenceLogsLoading.value = false
  }
}

async function handleEnable(row: any) {
  if (!auth.isOperator) { message.warning(t('common.permissionDenied')); return }
  if (togglingModelId.value) return  // [AUDIT-FIX] 防止并发切换
  togglingModelId.value = row.model_id
  try {
    await aiApi.enableModel(row.model_id)
    message.success(t('ai.enableSuccess'))
    await fetchModels()
  } catch (e: any) {
    message.error(extractError(e, t('ai.enableFailed')))
  } finally {
    togglingModelId.value = null
  }
}

async function handleDisable(row: any) {
  if (!auth.isOperator) { message.warning(t('common.permissionDenied')); return }
  if (togglingModelId.value) return  // [AUDIT-FIX] 防止并发切换
  togglingModelId.value = row.model_id
  try {
    await aiApi.disableModel(row.model_id)
    message.success(t('common.success'))
    await fetchModels()
  } catch (e: any) {
    message.error(extractError(e, t('common.failed')))
  } finally {
    togglingModelId.value = null
  }
}

async function handleReload(row: any) {
  try {
    await aiApi.reloadModel(row.model_id, row.model_file_path || '')
    message.success(t('common.success'))
    await fetchModels()
  } catch (e: any) {
    message.error(extractError(e, t('common.failed')))
  }
}

async function handleDelete(row: any) {
  if (!auth.isOperator) { message.warning(t('common.permissionDenied')); return }
  if (row.is_preset) {
    message.warning(t('ai.presetCannotDelete'))
    return
  }
  try {
    await aiApi.deleteModel(row.model_id)
    message.success(t('common.success'))
    await fetchModels()
  } catch (e: any) {
    message.error(extractError(e, t('common.failed')))
  }
}

function openInference() {
  inferenceModelId.value = null
  inferenceInput.value = ''
  inferenceResult.value = ''
  inferenceLatency.value = ''
  inputValidationStatus.value = undefined
  inputFeedback.value = ''
  inputHint.value = ''
  showInference.value = true
}

function quickInference(m: any) {
  inferenceModelId.value = m.model_id
  inferenceInput.value = ''
  inferenceResult.value = ''
  inferenceLatency.value = ''
  inputValidationStatus.value = undefined
  inputFeedback.value = ''
  inputHint.value = ''
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

function validateInput() {
  inputValidationStatus.value = undefined
  inputFeedback.value = ''
  inputHint.value = ''
  const val = inferenceInput.value.trim()
  if (!val) return false
  let parsed: any
  try {
    parsed = JSON.parse(val)
  } catch {
    inputValidationStatus.value = 'error'
    inputFeedback.value = t('ai.inputInvalidJson')
    return false
  }
  if (!Array.isArray(parsed)) {
    inputValidationStatus.value = 'error'
    inputFeedback.value = t('ai.inputMustBeArray')
    return false
  }
  if (parsed.length === 0) {
    inputValidationStatus.value = 'error'
    inputFeedback.value = t('ai.inputEmptyArray')
    return false
  }
  for (let i = 0; i < parsed.length; i++) {
    if (typeof parsed[i] !== 'number' || !isFinite(parsed[i])) {
      inputValidationStatus.value = 'error'
      inputFeedback.value = t('ai.inputMustBeNumbers', { index: i })
      return false
    }
  }
  // 检查与模型schema的匹配
  const target = models.value.find((m: any) => m.model_id === inferenceModelId.value)
  if (target) {
    try {
      const schema = typeof target.input_schema === 'string' ? JSON.parse(target.input_schema) : target.input_schema
      const expectedSize = schema?.shape?.[1]
      if (expectedSize != null && parsed.length !== expectedSize) {
        inputValidationStatus.value = 'warning'
        inputFeedback.value = t('ai.inputSizeMismatch', { actual: parsed.length, expected: expectedSize })
      } else {
        inputHint.value = t('ai.inputSizeOk', { size: parsed.length })
      }
    } catch { /* ignore schema parse error */ }
  }
  return true
}

async function handleInference() {
  if (!inferenceModelId.value) {
    message.warning(t('ai.modelNameRequired'))
    return
  }
  if (!validateInput()) {
    message.error(inputFeedback.value || t('ai.inputInvalid'))
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
    // FIX: result可能是Proxy对象，直接JSON.stringify会报循环引用。
    // 先用结构化克隆剥离Proxy，再序列化显示。
    try {
      inferenceResult.value = JSON.stringify(JSON.parse(JSON.stringify(result)), null, 2)
    } catch {
      inferenceResult.value = String(result)
    }
  } catch (e: any) {
    inferenceResult.value = extractError(e, t('common.failed'))
    message.error(inferenceResult.value)
  } finally {
    inferring.value = false
    fetchStats()
  }
}

async function fetchSchedules() {
  try {
    const data = await aiApi.listSchedules()
    schedules.value = Array.isArray(data) ? data : []
  } catch {
    schedules.value = []
  }
}

async function handleStartSchedule() {
  if (!auth.isOperator) { message.warning(t('common.permissionDenied')); return }
  if (!scheduleModelId.value) {
    message.warning(t('ai.modelNameRequired'))
    return
  }
  // FIXED: 校验范围与 n-input-number 控件 (min=5, max=3600) 保持一致
  if (!scheduleInterval.value || scheduleInterval.value < 5 || scheduleInterval.value > 3600) {
    message.warning(t('ai.intervalRange'))
    return
  }
  // FIXED: 校验范围与 n-input-number 控件 (max=10000) 保持一致
  if (!scheduleWindowSize.value || scheduleWindowSize.value < 1 || scheduleWindowSize.value > 10000) {
    message.warning(t('ai.windowSizeRange'))
    return
  }
  scheduleLoading.value = true
  try {
    await aiApi.startSchedule(scheduleModelId.value, scheduleInterval.value, scheduleWindowSize.value)
    message.success(t('common.success'))
    await fetchSchedules()
  } catch (e: any) {
    message.error(extractError(e, t('common.failed')))
  } finally {
    scheduleLoading.value = false
  }
}

async function handleStopSchedule(modelId: string) {
  if (!auth.isOperator) { message.warning(t('common.permissionDenied')); return }
  try {
    await aiApi.stopSchedule(modelId)
    message.success(t('common.success'))
    await fetchSchedules()
  } catch (e: any) {
    message.error(extractError(e, t('common.failed')))
  }
}

function handleFileChange(data: { file: any }) {
  const fileListEntry = data?.file
  uploadFile.value = fileListEntry?.file || null
}

async function handleUpload() {
  if (!uploadFile.value) return
  if (!uploadModelName.value?.trim()) {
    message.warning(t('ai.modelNameRequired'))
    return
  }
  // FIXED-Severe: 模型上传大小与类型校验，防止超大文件耗尽磁盘或上传恶意文件
  const MAX_MODEL_SIZE = 500 * 1024 * 1024 // 500MB
  if (uploadFile.value.size > MAX_MODEL_SIZE) {
    message.error(t('aiModel.fileTooLarge') || 'File too large (max 500MB)')
    return
  }
  // FIXED: 与 n-upload accept 一致，补充 .pkl 类型
  const allowedExts = ['.onnx', '.pt', '.pth', '.pkl', '.h5', '.tflite']
  const ext = uploadFile.value.name.toLowerCase().match(/\.[^.]+$/)?.[0]
  if (!ext || !allowedExts.includes(ext)) {
    message.error(t('aiModel.invalidFileType') || 'Invalid file type')
    return
  }
  uploadLoading.value = true
  try {
    await aiApi.uploadModel(uploadFile.value, uploadModelName.value || undefined)
    message.success(t('common.success'))
    showUploadModal.value = false
    uploadModelName.value = ''
    uploadFile.value = null
    await fetchModels()
  } catch (e: any) {
    message.error(extractError(e, t('common.failed')))
  } finally {
    uploadLoading.value = false
  }
}

// FIX-PERF5: 添加 isMounted 守卫，避免组件卸载后异步回调仍更新状态
let isMounted = true
// FIX-PERF12: 页面隐藏时暂停轮询，恢复时立即拉取并重启轮询
const { isVisible } = usePageVisibility()
watch(isVisible, (visible) => {
  if (visible) {
    fetchStats()
    if (statsTimer) clearInterval(statsTimer)
    statsTimer = setInterval(fetchStats, 30000)
  } else {
    if (statsTimer) { clearInterval(statsTimer); statsTimer = null }
  }
})

onMounted(async () => {
  await Promise.all([fetchModels(), fetchStats(), fetchSchedules()])
  if (!isMounted) return
  statsTimer = setInterval(fetchStats, 30000)
  aiWsHandler = (data: any) => {
    if (data.type === 'inference_result' || data.model_id) {
      fetchStats()
      if (currentModel.value && data.model_id === currentModel.value.model_id) {
        fetchInferenceLogs(data.model_id, inferenceLogsPage.value)
      }
    }
  }
  wsConnect('ai', aiWsHandler)
})

onUnmounted(() => {
  isMounted = false
  if (statsTimer) {
    clearInterval(statsTimer)
    statsTimer = null
  }
  if (aiWsHandler) {
    wsDisconnect('ai', aiWsHandler)
    aiWsHandler = null
  }
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
