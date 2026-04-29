<template>
  <n-space vertical :size="16">
    <n-card title="全局配置" :bordered="false">
      <n-form :model="globalForm" label-placement="left" label-width="140">
        <n-form-item label="启用预处理">
          <n-switch v-model:value="globalForm.enabled" />
        </n-form-item>
        <n-form-item label="默认死区值">
          <n-input-number v-model:value="globalForm.default_deadband" :min="0" :step="0.1" style="width: 200px" />
        </n-form-item>
        <n-form-item label="默认滤波窗口">
          <n-input-number v-model:value="globalForm.default_filter_window" :min="1" :max="21" style="width: 200px" />
        </n-form-item>
        <n-form-item label="默认聚合窗口(秒)">
          <n-input-number v-model:value="globalForm.default_aggregate_window_sec" :min="0" style="width: 200px" />
        </n-form-item>
      </n-form>
    </n-card>

    <n-card title="测点预处理配置" :bordered="false">
      <template #header-extra>
        <n-button type="primary" size="small" @click="showAddModal = true">
          <template #icon><n-icon :component="AddOutline" /></template>
          添加测点
        </n-button>
      </template>
      <n-data-table
        :columns="columns"
        :data="pointList"
        :bordered="false"
        size="small"
      />
    </n-card>

    <n-space>
      <n-button type="primary" :loading="saving" @click="handleSave">保存配置</n-button>
      <n-button @click="fetchConfig">刷新</n-button>
    </n-space>

    <n-modal v-model:show="showAddModal" title="添加测点配置" preset="card" style="width: 500px">
      <n-form :model="addForm" :rules="addRules" ref="addFormRef" label-placement="left" label-width="120">
        <n-form-item label="测点标识" path="point_key">
          <n-input v-model:value="addForm.point_key" placeholder="如: device1.point1" />
        </n-form-item>
        <n-form-item label="死区值">
          <n-input-number v-model:value="addForm.deadband" :min="0" :step="0.1" style="width: 200px" />
        </n-form-item>
        <n-form-item label="死区百分比">
          <n-input-number v-model:value="addForm.deadband_percent" :min="0" :step="0.1" style="width: 200px" />
        </n-form-item>
        <n-form-item label="滤波类型">
          <n-select v-model:value="addForm.filter" :options="filterOptions" clearable style="width: 200px" />
        </n-form-item>
        <n-form-item label="滤波窗口">
          <n-input-number v-model:value="addForm.filter_window" :min="1" :max="21" style="width: 200px" />
        </n-form-item>
        <n-form-item label="聚合类型">
          <n-select v-model:value="addForm.aggregate" :options="aggregateOptions" clearable style="width: 200px" />
        </n-form-item>
        <n-form-item label="聚合窗口(秒)">
          <n-input-number v-model:value="addForm.aggregate_window_sec" :min="1" style="width: 200px" />
        </n-form-item>
      </n-form>
      <template #action>
        <n-button @click="showAddModal = false">取消</n-button>
        <n-button type="primary" @click="handleAdd">确定</n-button>
      </template>
    </n-modal>
  </n-space>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, h } from 'vue'
import { NButton, NSpace, NTag, useMessage } from 'naive-ui'
import { AddOutline, TrashOutline } from '@vicons/ionicons5'
import { preprocessApi } from '@/api'

const message = useMessage()

const globalForm = reactive({
  enabled: false,
  default_deadband: 0,
  default_filter_window: 3,
  default_aggregate_window_sec: 0,
})

const pointConfigs = ref<Record<string, any>>({})
const pointList = ref<any[]>([])
const saving = ref(false)
const showAddModal = ref(false)
const addFormRef = ref<any>(null)

const addForm = reactive({
  point_key: '',
  deadband: 0,
  deadband_percent: 0,
  filter: null as string | null,
  filter_window: 3,
  aggregate: null as string | null,
  aggregate_window_sec: 60,
})

const addRules = {
  point_key: { required: true, message: '请输入测点标识', trigger: 'blur' },
}

const filterOptions = [
  { label: '中值滤波-3', value: 'median_3' },
  { label: '中值滤波-5', value: 'median_5' },
  { label: '中值滤波-7', value: 'median_7' },
]

const aggregateOptions = [
  { label: '平均值', value: 'avg' },
  { label: '最大值', value: 'max' },
  { label: '最小值', value: 'min' },
  { label: '求和', value: 'sum' },
  { label: '最后值', value: 'last' },
]

const columns = [
  { title: '测点标识', key: 'point_key', width: 200 },
  { title: '死区值', key: 'deadband', width: 100 },
  { title: '死区%', key: 'deadband_percent', width: 100 },
  { title: '滤波', key: 'filter', width: 120 },
  { title: '滤波窗口', key: 'filter_window', width: 100 },
  { title: '聚合', key: 'aggregate', width: 100 },
  { title: '聚合窗口(秒)', key: 'aggregate_window_sec', width: 120 },
  {
    title: '操作', key: 'actions', width: 80,
    render: (row: any) =>
      h(NButton, { text: true, type: 'error', onClick: () => handleDelete(row.point_key) }, {
        icon: () => h(NTag, { type: 'error' }, { default: () => '删除' }),
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
    message.error(e?.message || '获取配置失败')
  }
}

async function handleSave() {
  saving.value = true
  try {
    await preprocessApi.updateConfig({
      global: { ...globalForm },
      points: pointConfigs.value,
    })
    message.success('配置已保存')
  } catch (e: any) {
    message.error(e?.message || '保存失败')
  } finally {
    saving.value = false
  }
}

async function handleAdd() {
  try {
    await addFormRef.value?.validate()
  } catch { return }

  const config: any = {}
  if (addForm.deadband > 0) config.deadband = addForm.deadband
  if (addForm.deadband_percent > 0) config.deadband_percent = addForm.deadband_percent
  if (addForm.filter) config.filter = addForm.filter
  if (addForm.filter_window) config.filter_window = addForm.filter_window
  if (addForm.aggregate) config.aggregate = addForm.aggregate
  if (addForm.aggregate_window_sec) config.aggregate_window_sec = addForm.aggregate_window_sec

  pointConfigs.value[addForm.point_key] = config
  updatePointList()

  addForm.point_key = ''
  addForm.deadband = 0
  addForm.deadband_percent = 0
  addForm.filter = null
  addForm.filter_window = 3
  addForm.aggregate = null
  addForm.aggregate_window_sec = 60
  showAddModal.value = false
  message.success('测点已添加')
}

function handleDelete(pointKey: string) {
  delete pointConfigs.value[pointKey]
  updatePointList()
  message.success('测点已删除')
}

onMounted(fetchConfig)
</script>
