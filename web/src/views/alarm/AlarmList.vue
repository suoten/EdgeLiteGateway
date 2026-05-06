<template>
  <n-space vertical :size="16">
    <n-space justify="space-between">
      <n-space>
        <n-input v-model:value="searchText" placeholder="搜索设备ID" clearable style="width: 200px" @update:value="() => { pagination.page = 1; fetchAlarms() }" />
        <n-select v-model:value="filterStatus" :options="statusOptions" placeholder="状态筛选" clearable style="width: 120px" @update:value="() => { pagination.page = 1; fetchAlarms() }" />
        <n-select v-model:value="filterSeverity" :options="severityOptions" placeholder="级别筛选" clearable style="width: 120px" @update:value="() => { pagination.page = 1; fetchAlarms() }" />
      </n-space>
      <n-popconfirm v-if="firingAlarms.length" @positive-click="handleBatchAck">
        <template #trigger>
          <n-button type="warning" :loading="batchAcking">批量确认 ({{ firingAlarms.length }})</n-button>
        </template>
        确定批量确认 {{ firingAlarms.length }} 条触发中的告警？
      </n-popconfirm>
    </n-space>

    <n-data-table
      :columns="columns" :data="alarms" :loading="loading"
      :pagination="pagination" :row-key="(r: Alarm) => r.alarm_id"
      v-model:checked-row-keys="checkedKeys"
    />
    <n-empty v-if="!loading && alarms.length === 0" description="暂无告警，系统运行正常" style="padding: 40px 0" />
  </n-space>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, onUnmounted, h } from 'vue'
import { NButton, NTag, NSpace, NTooltip, NPopconfirm, useMessage, useDialog } from 'naive-ui'
import { alarmApi, type Alarm } from '@/api'
import { severityLabel, alarmStatusLabel, alarmStatusColor } from '@/utils/enumLabels'
import * as ws from '@/api/websocket'

const message = useMessage()
const dialog = useDialog()
const alarms = ref<Alarm[]>([])
const loading = ref(false)
const batchAcking = ref(false)
const searchText = ref('')
const filterStatus = ref<string | null>(null)
const filterSeverity = ref<string | null>(null)
const checkedKeys = ref<string[]>([])

const firingAlarms = computed(() => alarms.value.filter(a => a.status === 'firing'))

const pagination = reactive({ page: 1, pageSize: 20, itemCount: 0, onChange: (p: number) => { pagination.page = p; fetchAlarms() } })

const statusOptions = [
  { label: '触发中', value: 'firing' },
  { label: '已确认', value: 'acknowledged' },
  { label: '已恢复', value: 'recovered' },
]

const severityOptions = [
  { label: '严重', value: 'critical' },
  { label: '警告', value: 'warning' },
  { label: '信息', value: 'info' },
]

const severityColor: Record<string, any> = { critical: 'error', warning: 'warning', info: 'info' }
const statusColor: Record<string, any> = { firing: 'error', acknowledged: 'warning', recovered: 'success' }
const statusLabel: Record<string, string> = { firing: '触发中', acknowledged: '已确认', recovered: '已恢复' }

const columns = [
  { title: '告警ID', key: 'alarm_id', width: 140 },
  { title: '规则ID', key: 'rule_id', width: 140 },
  { title: '设备ID', key: 'device_id', width: 160 },
  {
    title: '级别', key: 'severity', width: 80,
    render: (r: Alarm) => h(NTag, { type: severityColor[r.severity] || 'default', size: 'small' }, { default: () => severityLabel[r.severity] || r.severity }),
  },
  {
    title: '状态', key: 'status', width: 90,
    render: (r: Alarm) => h(NTag, { type: statusColor[r.status] || 'default', size: 'small' }, { default: () => statusLabel[r.status] || r.status }),
  },
  { title: '触发次数', key: 'trigger_count', width: 80 },
  {
    title: '触发值', key: 'trigger_value', width: 160,
    render: (r: Alarm) => r.trigger_value ? h(NTooltip, {}, { trigger: () => h('span', { style: 'max-width:140px;display:inline-block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap' }, JSON.stringify(r.trigger_value)), default: () => JSON.stringify(r.trigger_value) }) : '-',
  },
  { title: '触发时间', key: 'fired_at', width: 180, sorter: true, defaultSortOrder: 'descend' },
  { title: '确认人', key: 'acknowledged_by', width: 100, render: (r: Alarm) => r.acknowledged_by || '-' },
  { title: '恢复时间', key: 'recovered_at', width: 180, render: (r: Alarm) => r.recovered_at || '-' },
  {
    title: '操作', key: 'actions', width: 100,
    render: (r: Alarm) =>
      r.status === 'firing'
        ? h(NPopconfirm as any, { onPositiveClick: () => doAck(r.alarm_id) }, {
          trigger: () => h(NButton, { text: true, type: 'primary' }, { default: () => '确认' }),
          default: () => '确定确认该告警？',
        })
        : null,
  },
]

async function fetchAlarms() {
  loading.value = true
  try {
    const data = await alarmApi.list({
      page: pagination.page,
      size: pagination.pageSize,
      status: filterStatus.value ?? undefined,
      severity: filterSeverity.value ?? undefined,
      search: searchText.value || undefined,
    })
    alarms.value = data?.data ?? []
    pagination.itemCount = data.total
  } catch (e: any) {
    alarms.value = []
    message.error(e?.response?.data?.detail || e?.message || '获取告警列表失败')
  } finally {
    loading.value = false
  }
}

async function doAck(alarmId: string) {
  try {
    await alarmApi.ack(alarmId)
    message.success('告警已确认')
    fetchAlarms()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || '确认失败')
  }
}

async function handleBatchAck() {
  batchAcking.value = true
  try {
    const ids = firingAlarms.value.map(a => a.alarm_id)
    const results = await Promise.allSettled(ids.map(id => alarmApi.ack(id)))
    const succeeded = results.filter(r => r.status === 'fulfilled').length
    const failed = results.filter(r => r.status === 'rejected').length
    if (failed > 0) {
      message.warning(`成功确认 ${succeeded} 条告警，${failed} 条确认失败`)
    } else {
      message.success(`成功确认 ${succeeded} 条告警`)
    }
    fetchAlarms()
  } finally {
    batchAcking.value = false
  }
}

onMounted(() => {
  fetchAlarms()
  ws.connect('alarm', onAlarmPush)
})
onUnmounted(() => {
  ws.disconnect('alarm', onAlarmPush)
})

function onAlarmPush(data: any) {
  if (data) fetchAlarms()
}
</script>
