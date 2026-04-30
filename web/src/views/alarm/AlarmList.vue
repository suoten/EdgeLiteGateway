<template>
  <n-space vertical :size="16">
    <n-space>
      <n-input v-model:value="searchText" placeholder="搜索设备ID" clearable style="width: 200px" @update:value="() => { pagination.page = 1; fetchAlarms() }" />
      <n-select v-model:value="filterStatus" :options="statusOptions" placeholder="状态筛选" clearable style="width: 120px" @update:value="() => { pagination.page = 1; fetchAlarms() }" />
      <n-select v-model:value="filterSeverity" :options="severityOptions" placeholder="级别筛选" clearable style="width: 120px" @update:value="() => { pagination.page = 1; fetchAlarms() }" />
    </n-space>

    <n-data-table :columns="columns" :data="alarms" :loading="loading" :pagination="pagination" :row-key="(r: Alarm) => r.alarm_id" />
  </n-space>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, h } from 'vue'
import { NButton, NTag, NSpace, NTooltip, useMessage, useDialog } from 'naive-ui'
import { alarmApi, type Alarm } from '@/api'

const message = useMessage()
const dialog = useDialog()
const alarms = ref<Alarm[]>([])
const loading = ref(false)
const searchText = ref('')
const filterStatus = ref<string | null>(null)
const filterSeverity = ref<string | null>(null)

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
    render: (r: Alarm) => h(NTag, { type: severityColor[r.severity] || 'default', size: 'small' }, { default: () => r.severity }),
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
        ? h(NButton, { text: true, type: 'primary', onClick: () => handleAck(r) }, { default: () => '确认' })
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
    message.error(e?.message || '获取告警列表失败')
  } finally {
    loading.value = false
  }
}

async function handleAck(r: Alarm) {
  dialog.warning({
    title: '确认告警',
    content: `确定确认该告警？`,
    positiveText: '确认',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await alarmApi.ack(r.alarm_id)
        message.success('告警已确认')
        fetchAlarms()
      } catch (e: any) {
        message.error(e?.message || '确认失败')
      }
    },
  })
}

onMounted(fetchAlarms)
</script>
