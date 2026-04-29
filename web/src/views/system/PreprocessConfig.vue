<template>
  <n-space vertical :size="16">
    <n-card title="边缘数据预处理">
      <n-switch v-model:value="globalEnabled" @update:value="togglePreprocess" />
      <span style="margin-left: 8px">全局启用</span>
    </n-card>
    <n-card title="测点预处理配置">
      <n-space vertical :size="12">
        <n-data-table :columns="columns" :data="configs" :loading="loading" />
        <n-button type="primary" @click="showAddModal = true">添加配置</n-button>
      </n-space>
    </n-card>
    <n-modal v-model:show="showAddModal" title="添加预处理配置" preset="card" style="width: 500px">
      <n-form :model="addForm" label-placement="left" label-width="120">
        <n-form-item label="设备ID">
          <n-input v-model:value="addForm.device_id" />
        </n-form-item>
        <n-form-item label="测点名称">
          <n-input v-model:value="addForm.point_name" />
        </n-form-item>
        <n-form-item label="死区(绝对)">
          <n-input-number v-model:value="addForm.deadband" :min="0" clearable />
        </n-form-item>
        <n-form-item label="死区(百分比)">
          <n-input-number v-model:value="addForm.deadband_percent" :min="0" :max="100" clearable />
        </n-form-item>
        <n-form-item label="滤波类型">
          <n-select v-model:value="addForm.filter" :options="filterOptions" clearable />
        </n-form-item>
        <n-form-item label="聚合方式">
          <n-select v-model:value="addForm.aggregate" :options="aggregateOptions" clearable />
        </n-form-item>
        <n-form-item label="聚合窗口(秒)">
          <n-input-number v-model:value="addForm.aggregate_window_sec" :min="1" clearable />
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
import { ref, onMounted } from 'vue'
import { NCard, NSwitch, NButton, NDataTable, NModal, NForm, NFormItem, NInput, NInputNumber, NSelect, NSpace, useMessage } from 'naive-ui'

const message = useMessage()
const loading = ref(false)
const globalEnabled = ref(false)
const configs = ref<any[]>([])
const showAddModal = ref(false)
const addForm = ref({
  device_id: '', point_name: '', deadband: null as number | null, deadband_percent: null as number | null,
  filter: null as string | null, aggregate: null as string | null, aggregate_window_sec: null as number | null,
})
const filterOptions = [
  { label: '中值滤波(3)', value: 'median_3' },
  { label: '中值滤波(5)', value: 'median_5' },
  { label: '中值滤波(7)', value: 'median_7' },
]
const aggregateOptions = [
  { label: '平均值', value: 'avg' },
  { label: '最大值', value: 'max' },
  { label: '最小值', value: 'min' },
  { label: '求和', value: 'sum' },
  { label: '最新值', value: 'last' },
]
const columns = [
  { title: '设备', key: 'device_id' },
  { title: '测点', key: 'point_name' },
  { title: '死区', key: 'deadband' },
  { title: '滤波', key: 'filter' },
  { title: '聚合', key: 'aggregate' },
]

function togglePreprocess(val: boolean) { globalEnabled.value = val }
function handleAdd() { showAddModal.value = false; message.success('配置已添加') }
onMounted(() => {})
</script>
