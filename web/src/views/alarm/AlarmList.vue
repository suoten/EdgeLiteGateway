<template>
  <n-space vertical :size="16">
    <n-grid :cols="4" :x-gap="12" :y-gap="12">
      <n-gi>
        <n-card size="small" :bordered="true" class="ai-stat-card ai-stat-card-purple" style="height:100px">
          <n-statistic :label="t('alarmList.aiAlarmTotal')">
            <template #prefix><span style="font-size:18px">🧠</span></template>
            <n-number-animation :from="0" :to="aiAlarmCount" :duration="500" />
          </n-statistic>
        </n-card>
      </n-gi>
      <n-gi>
        <n-card size="small" :bordered="true" class="ai-stat-card ai-stat-card-indigo" style="height:100px">
          <n-statistic :label="t('alarmList.aiAlarmRatio')">
            <template #prefix><span style="font-size:18px">📊</span></template>
            <template #default>
              <span class="ai-ratio-num">{{ aiAlarmRatio }}%</span>
            </template>
          </n-statistic>
        </n-card>
      </n-gi>
      <n-gi>
        <n-card size="small" :bordered="true" class="ai-stat-card ai-stat-card-cyan" style="height:100px">
          <n-statistic :label="t('alarmList.todayAiInferences')">
            <template #prefix><span style="font-size:18px">⚡</span></template>
            <n-number-animation :from="0" :to="aiStats.total_calls ?? 0" :duration="500" />
          </n-statistic>
        </n-card>
      </n-gi>
      <n-gi>
        <n-card size="small" :bordered="true" class="ai-stat-card ai-stat-card-pink" style="height:100px">
          <n-statistic :label="t('alarmList.avgInferenceLatency')">
            <template #prefix><span style="font-size:18px">⏱</span></template>
            <n-number-animation :from="0" :to="aiStats.avg_latency_ms ?? 0" :duration="500" />
            <template #suffix>ms</template>
          </n-statistic>
        </n-card>
      </n-gi>
    </n-grid>

    <n-space justify="space-between">
      <n-space>
        <n-input v-model:value="searchText" :placeholder="t('alarmList.searchPlaceholder')" clearable style="width: 200px" @update:value="() => { pagination.page = 1; fetchAlarms() }" />
        <n-select v-model:value="filterStatus" :options="statusOptions" :placeholder="t('alarmList.statusFilter')" clearable style="width: 120px" @update:value="() => { pagination.page = 1; fetchAlarms() }" />
        <n-select v-model:value="filterSeverity" :options="severityOptions" :placeholder="t('alarmList.levelFilter')" clearable style="width: 120px" @update:value="() => { pagination.page = 1; fetchAlarms() }" />
        <n-button-group>
          <n-button :type="filterType === 'all' ? 'primary' : 'default'" size="small" @click="filterType = 'all'; fetchAlarms()">{{ t('alarmList.filterAll') }}</n-button>
          <n-button :type="filterType === 'ai' ? 'primary' : 'default'" size="small" @click="filterType = 'ai'; fetchAlarms()">{{ t('alarmList.filterAi') }}</n-button>
          <n-button :type="filterType === 'threshold' ? 'primary' : 'default'" size="small" @click="filterType = 'threshold'; fetchAlarms()">{{ t('alarmList.filterThreshold') }}</n-button>
        </n-button-group>
      </n-space>
      <n-popconfirm v-if="firingAlarms.length" @positive-click="handleBatchAck">
        <template #trigger>
          <n-button type="warning" :loading="batchAcking">{{ t('alarmList.batchAck') }} ({{ firingAlarms.length }})</n-button>
        </template>
        {{ t('alarmList.batchAckConfirm', { count: firingAlarms.length }) }}
      </n-popconfirm>
    </n-space>

    <n-data-table
      :columns="columns" :data="filteredAlarms" :loading="loading"
      :pagination="pagination" :row-key="(r: Alarm) => r.alarm_id"
      :row-class-name="rowClassName"
      v-model:checked-row-keys="checkedKeys"
    >
      <template #empty>
        <n-empty v-if="!loading" :description="t('alarmList.emptyDesc')" style="padding: 40px 0" />
      </template>
    </n-data-table>

    <n-modal v-model:show="showAiDetail" preset="card" style="width: 520px" :title="t('alarmList.aiDetailTitle')">
      <template v-if="selectedAiAlarm">
        <n-descriptions label-placement="left" :column="1" bordered>
          <n-descriptions-item :label="t('alarmList.aiModelName')">{{ (selectedAiAlarm as any).ai_model_name || '-' }}</n-descriptions-item>
          <n-descriptions-item :label="t('alarmList.aiModelVersion')">{{ (selectedAiAlarm as any).ai_model_version || '-' }}</n-descriptions-item>
          <n-descriptions-item :label="t('alarmList.aiInputSummary')">{{ (selectedAiAlarm as any).ai_input_summary || '-' }}</n-descriptions-item>
          <n-descriptions-item :label="t('alarmList.aiAnomalyScore')">
            <n-space align="center" :size="12">
              <span style="font-size:28px;font-weight:700;color:#8b5cf6">{{ aiAnomalyScoreDisplay }}</span>
              <n-progress type="line" :percentage="aiAnomalyScorePercent" :color="aiAnomalyScorePercent > 80 ? '#f56c6c' : '#8b5cf6'" :rail-color="'#e5e7eb'" style="width:160px" :indicator-placement="'inside'" />
            </n-space>
          </n-descriptions-item>
          <n-descriptions-item :label="t('alarmList.aiLatency')">
            <span style="font-weight:600">{{ (selectedAiAlarm as any).ai_latency_ms ?? '-' }}</span>
            <span v-if="(selectedAiAlarm as any).ai_latency_ms != null">ms</span>
          </n-descriptions-item>
        </n-descriptions>
      </template>
    </n-modal>
  </n-space>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, onUnmounted, h } from 'vue'
import { NButton, NTag, NSpace, NTooltip, NPopconfirm, useMessage, useDialog } from 'naive-ui'
import { alarmApi, aiApi, type Alarm } from '@/api'
import { severityLabel, alarmStatusLabel, alarmStatusColor } from '@/utils/enumLabels'
import * as ws from '@/api/websocket'
import { t } from '@/i18n'

const message = useMessage()
const dialog = useDialog()
const alarms = ref<Alarm[]>([])
const loading = ref(false)
const batchAcking = ref(false)
const searchText = ref('')
const filterStatus = ref<string | null>(null)
const filterSeverity = ref<string | null>(null)
const filterType = ref<'all' | 'ai' | 'threshold'>('all')
const checkedKeys = ref<string[]>([])
const showAiDetail = ref(false)
const selectedAiAlarm = ref<Alarm | null>(null)
const aiStats = ref<Record<string, any>>({})

const ackLoading = ref(false)
const firingAlarms = computed(() => alarms.value.filter(a => a.status === 'firing'))

const aiAlarmCount = computed(() => alarms.value.filter(a => (a as any).rule_type === 'ai_inference').length)
const aiAlarmRatio = computed(() => {
  const total = alarms.value.length
  if (total === 0) return 0
  return Math.round((aiAlarmCount.value / total) * 100)
})

const aiAnomalyScoreDisplay = computed(() => {
  const score = (selectedAiAlarm.value as any)?.ai_anomaly_score
  return score != null ? Number(score).toFixed(4) : '-'
})
const aiAnomalyScorePercent = computed(() => {
  const score = (selectedAiAlarm.value as any)?.ai_anomaly_score
  return score != null ? Math.round(Number(score) * 100) : 0
})

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
const statusLabel: Record<string, string> = { firing: t('alarm.firing'), acknowledged: t('alarm.acknowledged'), recovered: t('alarm.recovered') }

const filteredAlarms = computed(() => {
  if (filterType.value === 'all') return alarms.value
  return alarms.value.filter(a => {
    const isAi = (a as any).rule_type === 'ai_inference'
    if (filterType.value === 'ai') return isAi
    return !isAi
  })
})

function rowClassName(row: Alarm) {
  return (row as any).rule_type === 'ai_inference' ? 'ai-alarm-row' : ''
}

const columns = [
  { title: t('alarmList.alarmId'), key: 'alarm_id', width: 140 },
  { title: t('alarmList.ruleId'), key: 'rule_id', width: 140 },
  { title: t('alarmList.deviceId'), key: 'device_id', width: 160 },
  {
    title: t('alarmList.alarmType'), key: 'rule_type', width: 100,
    render: (r: Alarm) => {
      const isAi = (r as any).rule_type === 'ai_inference'
      return h(NTag, {
        size: 'small',
        color: isAi ? { color: '#ede9fe', borderColor: '#8b5cf6', textColor: '#7c3aed' } : undefined,
        type: isAi ? undefined : 'info',
        style: isAi ? 'background:linear-gradient(135deg,#ede9fe,#ddd6fe);border:none;' : undefined,
      }, {
        default: () => isAi ? h(NSpace, { size: 4, align: 'center' }, {
          default: () => [h('span', { style: 'font-size:12px' }, '🧠'), h('span', null, t('alarm.aiAlarm'))]
        }) : t('alarm.thresholdAlarm'),
      })
    },
  },
  {
    title: t('alarmList.level'), key: 'severity', width: 100,
    render: (r: Alarm) => {
      const isAi = (r as any).rule_type === 'ai_inference'
      const isCritical = r.severity === 'critical'
      const children: any[] = [severityLabel.value[r.severity] || r.severity]  // FIXED-P3: computed→.value
      if (isAi && isCritical) {
        children.unshift(h('span', { class: 'pulse-dot', style: 'display:inline-block;width:8px;height:8px;border-radius:50%;background:#f56c6c;margin-right:6px;animation:pulse-anim 1.5s ease-in-out infinite;' }))
      }
      return h(NSpace, { size: 4, align: 'center' }, {
        default: () => [
          h(NTag, { type: severityColor[r.severity] || 'default', size: 'small' }, { default: () => children }),
        ],
      })
    },
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
    title: t('alarmList.actions'), key: 'actions', width: 120,
    render: (r: Alarm) => {
      const isAi = (r as any).rule_type === 'ai_inference'
      const ackBtn = r.status === 'firing'
        ? h(NPopconfirm as any, { onPositiveClick: () => doAck(r.alarm_id) }, {
          trigger: () => h(NButton, { text: true, type: 'primary' }, { default: () => t('alarmList.ack') }),
          default: () => t('alarmList.ackConfirm'),
        })
        : null
      const detailBtn = isAi
        ? h(NButton, { text: true, type: 'info', onClick: () => openAiDetail(r) }, { default: () => t('alarmList.aiDetail') })
        : null
      return h(NSpace, { size: 4 }, { default: () => [ackBtn, detailBtn].filter(Boolean) })
    },
  },
]

function openAiDetail(r: Alarm) {
  selectedAiAlarm.value = r
  showAiDetail.value = true
}

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

async function fetchAiStats() {
  try {
    const data = await aiApi.getStats()
    aiStats.value = data || {}
  } catch {
    aiStats.value = {}
  }
}

async function doAck(alarmId: string) {
  if (ackLoading.value) return
  ackLoading.value = true
  try {
    await alarmApi.ack(alarmId)
    message.success(t('alarmList.ackSuccess'))
    fetchAlarms()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || t('alarmList.ackFailed'))
  } finally {
    ackLoading.value = false
  }
}

async function handleBatchAck() {
  const snapshotIds = firingAlarms.value.map(a => a.alarm_id).filter(Boolean)
  if (snapshotIds.length === 0) return
  batchAcking.value = true
  try {
    const results = await Promise.allSettled(snapshotIds.map(id => alarmApi.ack(id)))
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
  fetchAiStats()
  ws.connect('alarm', onAlarmPush)
  ws.onStatus('alarm', (status: string) => {  // FIXED-P2: onStatus回调签名为(status:'connected'|'disconnected'|'error', reason?)，之前将status当布尔值判断永远truthy导致断线提示永不触发
    if (status === 'disconnected' || status === 'error') {
      message.warning(t('alarmList.wsDisconnected'))
    }
  })
})
onUnmounted(() => {
  ws.disconnect('alarm', onAlarmPush)
  if (alarmDebounceTimer) { clearTimeout(alarmDebounceTimer); alarmDebounceTimer = null }
})

let alarmDebounceTimer: ReturnType<typeof setTimeout> | null = null

function onAlarmPush(data: any) {
  if (!data) return
  if (alarmDebounceTimer) clearTimeout(alarmDebounceTimer)
  alarmDebounceTimer = setTimeout(() => {
    fetchAlarms()
    fetchAiStats()
    alarmDebounceTimer = null
  }, 500)
}
</script>

<style scoped>
.ai-alarm-row {
  background: #f5f3ff;
}
.ai-stat-card {
  border-radius: 8px;
  transition: all 0.3s ease;
  color: #fff !important;
}
.ai-stat-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 8px 16px rgba(0,0,0,0.1);
}
.ai-stat-card-purple { background: linear-gradient(135deg, #8b5cf6 0%, #7c3aed 100%); }
.ai-stat-card-indigo { background: linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%); }
.ai-stat-card-cyan { background: linear-gradient(135deg, #06b6d4 0%, #8b5cf6 100%); }
.ai-stat-card-pink { background: linear-gradient(135deg, #ec4899 0%, #8b5cf6 100%); }
.ai-ratio-num {
  color: #fff;
  font-size: 22px;
  font-weight: 700;
}
.ai-stat-card :deep(.n-statistic .n-statistic-value__content),
.ai-stat-card :deep(.n-statistic .n-statistic-value),
.ai-stat-card :deep(.n-statistic .n-statistic-value__integer),
.ai-stat-card :deep(.n-statistic .n-statistic-value__fraction),
.ai-stat-card :deep(.n-statistic__label),
.ai-stat-card :deep(.n-icon) {
  color: #fff !important;
}
@keyframes pulse-anim {
  0% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.4; transform: scale(1.4); }
  100% { opacity: 1; transform: scale(1); }
}
.pulse-dot {
  animation: pulse-anim 1.5s ease-in-out infinite;
}
</style>
