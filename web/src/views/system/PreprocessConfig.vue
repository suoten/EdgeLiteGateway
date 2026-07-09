<template>
  <n-spin :show="pageLoading" :description="t('preprocess.loading')">
  <n-space vertical :size="16">
    <n-card :title="t('preprocess.globalConfig')" :bordered="false">
      <n-form :model="globalForm" ref="globalFormRef" :rules="globalRules" label-placement="left" label-width="140">
        <n-form-item :label="t('preprocess.enablePreprocess')">
          <n-switch v-model:value="globalForm.enabled" />
        </n-form-item>
        <n-form-item :label="t('preprocess.defaultDeadband')" path="default_deadband">
          <n-input-number v-model:value="globalForm.default_deadband" :min="0" :step="0.1" style="width: 200px" />
        </n-form-item>
        <n-form-item :label="t('preprocess.defaultFilterWindow')" path="default_filter_window">
          <n-input-number v-model:value="globalForm.default_filter_window" :min="1" :max="21" style="width: 200px" />
        </n-form-item>
        <n-form-item :label="t('preprocess.defaultAggWindow')" path="default_aggregate_window_sec">
          <n-input-number v-model:value="globalForm.default_aggregate_window_sec" :min="0" style="width: 200px" />
        </n-form-item>
      </n-form>
    </n-card>

    <n-card :title="t('preprocess.pointConfig')" :bordered="false">
      <template #header-extra>
        <n-button type="primary" size="small" @click="showAddModal = true">
          <template #icon><n-icon :component="AddOutline" /></template>
          {{ t('preprocess.addPoint') }}
        </n-button>
      </template>
      <n-data-table
        :columns="columns"
        :data="pointList"
        :loading="pageLoading"
        :bordered="false"
        size="small"
        :scroll-x="970"
      />
    </n-card>

    <n-space>
      <n-button type="primary" :loading="saving" @click="handleSave">{{ t('preprocess.saveConfig') }}</n-button>
      <n-button @click="fetchConfig">{{ t('preprocess.refresh') }}</n-button>
    </n-space>

    <n-modal v-model:show="showAddModal" :title="t('preprocess.addPointTitle')" preset="card" style="width: 500px; max-width: 95vw" :close-on-esc="true" :auto-focus="true" :close-on-esc-aria-label="t('common.closeDialog')">
      <n-form :model="addForm" :rules="addRules" ref="addFormRef" label-placement="left" label-width="120">
        <n-form-item :label="t('preprocess.pointId')" path="point_key">
          <n-input v-model:value="addForm.point_key" maxlength="100" :placeholder="t('preprocess.pointIdPlaceholder')" />
        </n-form-item>
        <n-form-item :label="t('preprocess.deadbandValue')">
          <n-input-number v-model:value="addForm.deadband" :min="0" :step="0.1" style="width: 200px" />
        </n-form-item>
        <n-form-item :label="t('preprocess.deadbandPercent')">
          <n-input-number v-model:value="addForm.deadband_percent" :min="0" :max="100" :step="0.1" style="width: 200px" />
        </n-form-item>
        <n-form-item :label="t('preprocess.filterType')" path="filter">
          <n-select v-model:value="addForm.filter" :options="filterOptions" clearable style="width: 200px" />
        </n-form-item>
        <n-form-item :label="t('preprocess.filterWindow')">
          <n-input-number v-model:value="addForm.filter_window" :min="1" :max="21" style="width: 200px" />
        </n-form-item>
        <n-form-item :label="t('preprocess.aggType')">
          <n-select v-model:value="addForm.aggregate" :options="aggregateOptions" clearable style="width: 200px" />
        </n-form-item>
        <n-form-item :label="t('preprocess.aggWindow')">
          <n-input-number v-model:value="addForm.aggregate_window_sec" :min="1" style="width: 200px" />
        </n-form-item>
      </n-form>
      <template #action>
        <n-button @click="showAddModal = false">{{ t('common.cancel') }}</n-button>
        <n-button type="primary" :loading="adding" @click="handleAdd">{{ t('common.confirm') }}</n-button>
      </template>
    </n-modal>
  </n-space>
  </n-spin>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, h, watch, onBeforeUnmount } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { NButton, NSpace, NTag, NSpin } from 'naive-ui'
import { AddOutline, TrashOutline } from '@vicons/ionicons5'
import { preprocessApi } from '@/api'
// FIXED: 原问题-添加i18n支持
import { t } from '@/i18n'
import { extractError } from '@/utils/errorCodes'
import { message, dialog } from '@/utils/discreteApi'
// [AUDIT-FIX] 严重级-预处理配置的保存与新增点位属敏感写操作，需函数级权限校验
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

const pageLoading = ref(true)

const globalForm = reactive({
  enabled: false,
  default_deadband: 0,
  default_filter_window: 3,
  default_aggregate_window_sec: 0,
})
// [AUDIT-FIX] 全局配置表单添加数值范围校验
const globalFormRef = ref<any>(null)
const globalRules = computed(() => ({
  default_deadband: [{ type: 'number' as const, min: 0, message: t('preprocess.valueMustBeNonNegative'), trigger: ['input', 'blur'] }],
  default_filter_window: [{ type: 'number' as const, min: 1, max: 21, message: t('preprocess.filterWindowRange'), trigger: ['input', 'blur'] }],
  default_aggregate_window_sec: [{ type: 'number' as const, required: true, min: 0, message: t('preprocess.valueMustBeNonNegative'), trigger: ['input', 'blur'] }],
}))

const pointConfigs = ref<Record<string, any>>({})
const pointList = ref<any[]>([])
const saving = ref(false)
const dirty = ref(false)
const showAddModal = ref(false)
const addFormRef = ref<any>(null)
const adding = ref(false)

const addForm = reactive({
  point_key: '',
  deadband: 0,
  deadband_percent: 0,
  filter: null as string | null,
  filter_window: 3,
  aggregate: null as string | null,
  aggregate_window_sec: 60,
})

const addRules = computed(() => ({
  point_key: { required: true, message: t('preprocess.pointIdRequired'), trigger: ['input', 'blur'] },
  filter: { required: true, type: 'string' as const, message: t('preprocess.filterTypeRequired'), trigger: ['change', 'blur'] },
}))

const filterOptions = [
  { label: t('preprocess.filterMedian3'), value: 'median_3' },
  { label: t('preprocess.filterMedian5'), value: 'median_5' },
  { label: t('preprocess.filterMedian7'), value: 'median_7' },
]

const aggregateOptions = [
  { label: t('preprocess.aggAvg'), value: 'avg' },
  { label: t('preprocess.aggMax'), value: 'max' },
  { label: t('preprocess.aggMin'), value: 'min' },
  { label: t('preprocess.aggSum'), value: 'sum' },
  { label: t('preprocess.aggLast'), value: 'last' },
]

// FIXED: 原问题-表格列标题中文硬编码，改为i18n
const columns = [
  { title: t('preprocess.pointId'), key: 'point_key', width: 200 },
  { title: t('preprocess.deadbandValue'), key: 'deadband', width: 100 },
  { title: t('preprocess.deadbandPercent'), key: 'deadband_percent', width: 100 },
  { title: t('preprocess.filterType'), key: 'filter', width: 120 },
  { title: t('preprocess.filterWindow'), key: 'filter_window', width: 100 },
  { title: t('preprocess.aggType'), key: 'aggregate', width: 100 },
  { title: t('preprocess.aggWindow'), key: 'aggregate_window_sec', width: 120 },
  {
    title: t('common.actions'), key: 'actions', width: 80,  // FIXED: 原问题-跨域误用alarmList.actions
    render: (row: any) =>
      h(NButton, { text: true, type: 'error', onClick: () => handleDelete(row.point_key) }, {
        default: () => t('common.delete'),
      }),
  },
]

function updatePointList() {
  pointList.value = Object.entries(pointConfigs.value).map(([key, config]) => ({
    point_key: key,
    ...config,
  }))
}

async function fetchConfig() {
  try {
    const data = await preprocessApi.getConfig()
    globalForm.enabled = data.enabled ?? false
    globalForm.default_deadband = data.default_deadband ?? 0
    globalForm.default_filter_window = data.default_filter_window ?? 3
    globalForm.default_aggregate_window_sec = data.default_aggregate_window_sec ?? 0
    pointConfigs.value = data.point_configs ?? {}
    updatePointList()
  } catch (e: any) {
    message.error(extractError(e, t('http.requestFailed')))
  } finally {
    pageLoading.value = false
  }
}

async function handleSave() {
  if (!auth.isOperator) { message.warning(t('common.permissionDenied')); return }
  try {
    await globalFormRef.value?.validate()
  } catch {
    return
  }
  saving.value = true
  try {
    await preprocessApi.updateConfig({
      global: { ...globalForm },
      points: pointConfigs.value,
    })
    message.success(t('common.success'))
    dirty.value = false
  } catch (e: any) {
    message.error(extractError(e, t('common.failed')))
  } finally {
    saving.value = false
  }
}

async function handleAdd() {
  if (!auth.isOperator) { message.warning(t('common.permissionDenied')); return }
  try {
    await addFormRef.value?.validate()
  } catch { return }

  if (addForm.deadband > 0 && addForm.deadband_percent > 0) {
    message.warning(t('preprocess.deadbandConflict'))
    return
  }

  const config: any = {}
  if (addForm.deadband > 0) config.deadband = addForm.deadband
  if (addForm.deadband_percent > 0) config.deadband_percent = addForm.deadband_percent
  if (addForm.filter) config.filter = addForm.filter
  if (addForm.filter_window) config.filter_window = addForm.filter_window
  if (addForm.aggregate) config.aggregate = addForm.aggregate
  if (addForm.aggregate_window_sec) config.aggregate_window_sec = addForm.aggregate_window_sec

  pointConfigs.value[addForm.point_key] = config
  updatePointList()

  adding.value = true
  try {
    await preprocessApi.updateConfig({
      global: { ...globalForm },
      points: { [addForm.point_key]: config },
    })
    dirty.value = false
    message.success(t('common.success'))
  } catch (e: any) {
    message.error(extractError(e, t('common.failed')))
  } finally {
    adding.value = false
  }

  addForm.point_key = ''
  addForm.deadband = 0
  addForm.deadband_percent = 0
  addForm.filter = null
  addForm.filter_window = 3
  addForm.aggregate = null
  addForm.aggregate_window_sec = 60
  showAddModal.value = false
}

function handleDelete(pointKey: string) {
  dialog.warning({
    title: t('common.confirm'),
    content: t('deviceList.deleteConfirm', { name: pointKey }),
    positiveText: t('common.delete'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      delete pointConfigs.value[pointKey]
      updatePointList()
      try {
        const remaining = { ...pointConfigs.value }
        delete remaining[pointKey]
        await preprocessApi.updateConfig({
          global: { ...globalForm },
          points: pointConfigs.value,
        })
        dirty.value = false
        message.success(t('common.success'))
      } catch (e: any) {
        message.error(extractError(e, t('common.failed')))
      }
    },
  })
}

onMounted(fetchConfig)

watch([() => ({ ...globalForm }), pointConfigs], () => { dirty.value = true }, { deep: true })

onBeforeRouteLeave((_to, _from, next) => {
  if (dirty.value) {
    dialog.warning({
      title: t('common.confirm'),
      content: t('common.required'),
      positiveText: t('common.confirm'),
      negativeText: t('common.cancel'),
      onPositiveClick: () => next(),
      onNegativeClick: () => next(false),
    })
  } else {
    next()
  }
})

onBeforeUnmount(() => { window.onbeforeunload = null })

if (typeof window !== 'undefined') {
  window.onbeforeunload = (e) => { if (dirty.value) e.preventDefault() }
}
</script>
