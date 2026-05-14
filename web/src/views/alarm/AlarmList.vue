<template>
  <n-space vertical :size="16">
    <n-space justify="space-between">
      <n-space>
        <n-input v-model:value="searchText" :placeholder="t('alarmList.searchPlaceholder')" clearable style="width: 200px" @update:value="() => { pagination.page = 1; fetchAlarms() }" />
        <n-select v-model:value="filterStatus" :options="statusOptions" :placeholder="t('alarmList.statusFilter')" clearable style="width: 120px" @update:value="() => { pagination.page = 1; fetchAlarms() }" />
        <n-select v-model:value="filterSeverity" :options="severityOptions" :placeholder="t('alarmList.levelFilter')" clearable style="width: 120px" @update:value="() => { pagination.page = 1; fetchAlarms() }" />
      </n-space>
      <n-popconfirm v-if="firingAlarms.length" @positive-click="handleBatchAck">
        <template #trigger>
          <n-button type="warning" :loading="batchAcking">{{ t('alarmList.batchAck') }} ({{ firingAlarms.length }})</n-button>
        </template>
        {{ t('alarmList.batchAckConfirm', { count: firingAlarms.length }) }}
      </n-popconfirm>
    </n-space>

    <n-data-table
      :columns="columns" :data="alarms" :loading="loading"
      :pagination="pagination" :row-key="(r: Alarm) => r.alarm_id"
      v-model:checked-row-keys="checkedKeys"
    >
      <template #empty>
        <n-empty v-if="!loading" :description="t('alarmList.emptyDesc')" style="padding: 40px 0" />
      </template>
    </n-data-table>
  </n-space>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, onUnmounted, h } from 'vue'
import { NButton, NTag, NSpace, NTooltip, NPopconfirm, useMessage, useDialog } from 'naive-ui'
import { alarmApi, type Alarm } from '@/api'
import { severityLabel, alarmStatusLabel, alarmStatusColor } from '@/utils/enumLabels'
import * as ws from '@/api/websocket'
// FIXED: 原问题-添加i18n支持
import { t } from '@/i18n'

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
  { label: t('alarm.firing'), value: 'firing' },
  { label: t('alarm.acknowledged'), value: 'acknowledged' },
  { label: t('alarm.recovered'), value: 'recovered' },
]

const severityOptions = [
  { label: t('alarm.critical'), value: 'critical' },
  { label: t('alarm.warning'), value: 'warning' },
  { label: t('alarm.info'), value: 'info' },
]

const severityColor: Record<string, any> = { critical: 'error', warning: 'warning', info: 'info' }
const statusColor: Record<string, any> = { firing: 'error', acknowledged: 'warning', recovered: 'success' }
// FIXED: 原问题-statusLabel中文硬编码，改为i18n
const statusLabel: Record<string, string> = { firing: t('alarm.firing'), acknowledged: t('alarm.acknowledged'), recovered: t('alarm.recovered') }

const columns = [
  { title: t('alarmList.alarmId'), key: 'alarm_id', width: 140 },
  { title: t('alarmList.ruleId'), key: 'rule_id', width: 140 },
  { title: t('alarmList.deviceId'), key: 'device_id', width: 160 },
  {
    title: t('alarmList.level'), key: 'severity', width: 80,
    render: (r: Alarm) => h(NTag, { type: severityColor[r.severity] || 'default', size: 'small' }, { default: () => severityLabel[r.severity] || r.severity }),
  },
  {
    title: t('alarmList.status'), key: 'status', width: 90,
    render: (r: Alarm) => h(NTag, { type: statusColor[r.status] || 'default', size: 'small' }, { default: () => statusLabel[r.status] || r.status }),
  },
  { title: t('alarmList.triggerCount'), key: 'trigger_count', width: 80 },
  {
    title: t('alarmList.triggerValue'), key: 'trigger_value', width: 160,
    render: (r: Alarm) => r.trigger_value ? h(NTooltip, {}, { trigger: () => h('span', { style: 'max-width:140px;display:inline-block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap' }, JSON.stringify(r.trigger_value)), default: () => JSON.stringify(r.trigger_value) }) : '-',
  },
  { title: t('alarmList.triggerTime'), key: 'fired_at', width: 180, sorter: true, defaultSortOrder: 'descend' },
  { title: t('alarmList.ackBy'), key: 'acknowledged_by', width: 100, render: (r: Alarm) => r.acknowledged_by || '-' },
  { title: t('alarmList.recoverTime'), key: 'recovered_at', width: 180, render: (r: Alarm) => r.recovered_at || '-' },
  {
    title: t('alarmList.actions'), key: 'actions', width: 100,
    render: (r: Alarm) =>
      r.status === 'firing'
        ? h(NPopconfirm as any, { onPositiveClick: () => doAck(r.alarm_id) }, {
          trigger: () => h(NButton, { text: true, type: 'primary' }, { default: () => t('alarmList.ack') }),
          default: () => t('alarmList.ackConfirm'),
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
    pagination.itemCount = data?.total ?? 0
  } catch (e: any) {
    alarms.value = []
    message.error(e?.response?.data?.detail || e?.message || t('alarmList.fetchFailed'))
  } finally {
    loading.value = false
  }
}

async function doAck(alarmId: string) {
  try {
    await alarmApi.ack(alarmId)
    message.success(t('alarmList.ackSuccess'))
    fetchAlarms()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || t('alarmList.ackFailed'))
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
      message.warning(t('alarmList.batchAckResult', { success: succeeded, failed }))
    } else {
      message.success(t('alarmList.batchAckResultAll', { success: succeeded }))
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
