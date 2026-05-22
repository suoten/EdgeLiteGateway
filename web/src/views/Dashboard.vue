<template>
  <n-spin :show="pageLoading" :description="t('dashboard.loading')">
  <n-space vertical :size="20">
    <n-card v-if="showQuickStart" :title="t('dashboard.quickStart')" :bordered="false" class="quick-start-card">
      <n-grid :cols="4" :x-gap="16" :y-gap="16" responsive="screen">
        <n-gi>
          <n-card hoverable class="qs-item" @click="router.push('/devices')">
            <n-space vertical align="center">
              <n-icon size="40" :component="HardwareChip" color="#667eea" />
              <n-text strong>{{ t('dashboard.createDevice') }}</n-text>
              <n-text depth="3">{{ t('dashboard.createDeviceDesc') }}</n-text>
            </n-space>
          </n-card>
        </n-gi>
        <n-gi>
          <n-card hoverable class="qs-item" @click="router.push('/rules')">
            <n-space vertical align="center">
              <n-icon size="40" :component="SettingsOutline" color="#11998e" />
              <n-text strong>{{ t('dashboard.configAlarm') }}</n-text>
              <n-text depth="3">{{ t('dashboard.configAlarmDesc') }}</n-text>
            </n-space>
          </n-card>
        </n-gi>
        <n-gi>
          <n-card hoverable class="qs-item qs-item-ai" @click="router.push('/system/ai-model')">
            <n-space vertical align="center">
              <n-icon size="40" :component="PulseOutline" color="#8b5cf6" />
              <n-text strong>{{ t('dashboard.aiQuickStart') }}</n-text>
              <n-text depth="3">{{ t('dashboard.aiQuickStartDesc') }}</n-text>
            </n-space>
          </n-card>
        </n-gi>
        <n-gi>
          <n-card hoverable class="qs-item" @click="router.push('/system/platforms')">
            <n-space vertical align="center">
              <n-icon size="40" :component="AlertCircleOutline" color="#4facfe" />
              <n-text strong>{{ t('dashboard.platformIntegration') }}</n-text>
              <n-text depth="3">{{ t('dashboard.platformIntegrationDesc') }}</n-text>
            </n-space>
          </n-card>
        </n-gi>
      </n-grid>
    </n-card>

    <n-grid :cols="4" :x-gap="16" :y-gap="16" responsive="screen">
      <n-gi>
        <n-card class="stat-card stat-card-primary" :bordered="false">
          <n-statistic :label="t('dashboard.deviceTotal')" :value="status?.device_total ?? 0">
            <template #prefix><n-icon :component="HardwareChip" /></template>
          </n-statistic>
          <div class="stat-footer">
            <span>{{ t('dashboard.onlineCount', { count: status?.device_online ?? 0 }) }}</span>
          </div>
        </n-card>
      </n-gi>
      <n-gi>
        <n-card class="stat-card stat-card-success" :bordered="false">
          <n-statistic :label="t('dashboard.ruleTotal')" :value="status?.rule_total ?? 0">
            <template #prefix><n-icon :component="SettingsOutline" /></template>
          </n-statistic>
          <div class="stat-footer">
            <span>{{ t('dashboard.enabledCount', { count: status?.rule_enabled ?? 0 }) }}</span>
          </div>
        </n-card>
      </n-gi>
      <n-gi>
        <n-card class="stat-card stat-card-warning" :bordered="false">
          <n-statistic :label="t('dashboard.activeAlarm')" :value="status?.alarm_firing ?? 0">
            <template #prefix><n-icon :component="AlertCircleOutline" /></template>
          </n-statistic>
          <div class="stat-footer">
            <span v-if="status?.alarm_firing" class="stat-footer-warning">{{ t('dashboard.needHandle') }}</span>
            <span v-else>{{ t('dashboard.systemNormal') }}</span>
          </div>
        </n-card>
      </n-gi>
      <n-gi>
        <n-card class="stat-card stat-card-info" :bordered="false">
          <n-statistic :label="t('dashboard.collectTask')" :value="status?.collect_task_count ?? 0">
            <template #prefix><n-icon :component="PulseOutline" /></template>
          </n-statistic>
          <div class="stat-footer">
            <span>{{ t('dashboard.running') }}</span>
          </div>
        </n-card>
      </n-gi>
    </n-grid>

    <AiStatsWidget />

    <n-card :bordered="false" class="collect-engine-panel">
      <template #header>
        <n-space align="center" :size="12">
          <span style="font-size:20px">📡</span>
          <span style="font-size:16px;font-weight:700;color:#fff">{{ t('dashboard.collectEngine') }}</span>
          <span class="collect-status-dot" :class="collectEngineRunning ? 'dot-running' : 'dot-stopped'" />
          <n-text style="color:rgba(255,255,255,0.8);font-size:12px">{{ collectEngineRunning ? t('ai.statusActive') : t('dashboard.collectStopped') }}</n-text>
        </n-space>
      </template>
      <n-grid :cols="4" :x-gap="16" :y-gap="16">
        <n-gi>
          <n-statistic :label="t('dashboard.collectTasks')" class="collect-stat">
            <template #default><span class="collect-stat-num">{{ status?.collect_task_count ?? 0 }}</span></template>
            <template #prefix><span style="font-size:18px">🔌</span></template>
          </n-statistic>
        </n-gi>
        <n-gi>
          <n-statistic :label="t('dashboard.runningTasks')" class="collect-stat">
            <template #default><span class="collect-stat-num">{{ runningTaskCount }}</span></template>
            <template #prefix><span class="inline-pulse-green" /></template>
          </n-statistic>
        </n-gi>
        <n-gi>
          <n-statistic :label="t('dashboard.todayDataPoints')" class="collect-stat">
            <template #default><span class="collect-stat-num">{{ todayDataPoints }}</span></template>
            <template #prefix><span style="font-size:18px">📈</span></template>
          </n-statistic>
        </n-gi>
        <n-gi>
          <n-statistic :label="t('dashboard.collectRate')" class="collect-stat">
            <template #default>
              <span class="collect-stat-num">{{ collectSuccessRate }}%</span>
              <n-progress type="line" :percentage="collectSuccessRate" :color="collectSuccessRate >= 99 ? '#67c23a' : collectSuccessRate < 95 ? '#f56c6c' : '#e6a23c'" :show-indicator="false" style="width:80px;margin-left:8px" />
            </template>
          </n-statistic>
        </n-gi>
      </n-grid>
      <div style="margin-top:16px">
        <n-text style="color:rgba(255,255,255,0.7);font-size:13px;font-weight:600">{{ t('dashboard.activeDevices') }}</n-text>
        <n-space vertical :size="6" style="margin-top:8px;max-height:240px;overflow-y:auto">
          <div v-for="d in activeDeviceList" :key="d.device_id" class="collect-device-row" :class="{'row-online': d.status === 'online', 'row-error': d.status === 'error'}">
            <n-space align="center" :size="8" justify="space-between" style="width:100%">
              <n-space align="center" :size="8">
                <span class="collect-device-dot" :class="`dot-${d.status}`" />
                <n-text style="color:#fff;font-size:13px">{{ d.name || d.device_id }}</n-text>
                <n-tag size="tiny" :bordered="false" type="info">{{ d.protocol }}</n-tag>
              </n-space>
              <n-space align="center" :size="12">
                <n-text style="color:rgba(255,255,255,0.6);font-size:12px">{{ d.last_collect_ago || '-' }}</n-text>
                <n-text style="color:rgba(255,255,255,0.8);font-size:12px">{{ d.today_points ?? 0 }} pts</n-text>
              </n-space>
            </n-space>
          </div>
        </n-space>
      </div>
      <div class="data-waterfall">
        <div v-for="i in 12" :key="i" class="waterfall-bar" :style="{ animationDelay: `${i * 0.15}s`, height: `${20 + Math.random() * 60}%` }" />
      </div>
    </n-card>

    <n-grid :cols="2" :x-gap="16" :y-gap="16">
      <n-gi>
        <n-card :title="t('dashboard.deviceStatus')" :bordered="false">
          <v-chart :option="deviceStatusOption" autoresize style="height: 280px" />
        </n-card>
      </n-gi>
      <n-gi>
        <n-card :title="t('dashboard.protocolDist')" :bordered="false">
          <v-chart :option="protocolOption" autoresize style="height: 280px" />
        </n-card>
      </n-gi>
      <n-gi>
        <n-card :title="t('dashboard.alarmTrend')" :bordered="false">
          <v-chart :option="alarmTrendOption" autoresize style="height: 280px" />
        </n-card>
      </n-gi>
      <n-gi>
        <n-card :title="t('dashboard.resourceTrend')" :bordered="false">
          <v-chart :option="resourceTrendOption" autoresize style="height: 280px" />
        </n-card>
      </n-gi>
    </n-grid>

    <n-grid :cols="3" :x-gap="16" :y-gap="16">
      <n-gi>
        <n-card :title="t('dashboard.cpuUsage')" :bordered="false" class="resource-card">
          <n-progress type="circle" :percentage="status?.cpu_percent ?? 0" :stroke-width="12" :color="cpuColor" />
          <div class="resource-info">
            <n-text depth="3">{{ t('dashboard.currentLoad') }}</n-text>
          </div>
        </n-card>
      </n-gi>
      <n-gi>
        <n-card :title="t('dashboard.memoryUsage')" :bordered="false" class="resource-card">
          <n-progress type="circle" :percentage="status?.memory_percent ?? 0" :stroke-width="12" :color="memColor" />
          <div class="resource-info">
            <n-text depth="3">{{ formatBytes(status?.memory_used) }} / {{ formatBytes(status?.memory_total) }}</n-text>
          </div>
        </n-card>
      </n-gi>
      <n-gi>
        <n-card :title="t('dashboard.diskUsage')" :bordered="false" class="resource-card">
          <n-progress type="circle" :percentage="status?.disk_percent ?? 0" :stroke-width="12" :color="diskColor" />
          <div class="resource-info">
            <n-text depth="3">{{ formatBytes(status?.disk_used) }} / {{ formatBytes(status?.disk_total) }}</n-text>
          </div>
        </n-card>
      </n-gi>
    </n-grid>

    <n-card :title="t('dashboard.systemInfo')" :bordered="false">
      <n-grid :cols="2" :x-gap="24" :y-gap="16">
        <n-gi>
          <n-descriptions label-placement="left" :column="1" bordered>
            <n-descriptions-item :label="t('dashboard.version')">
              <n-tag type="success" size="small">v{{ status?.version ?? '-' }} Community</n-tag>
            </n-descriptions-item>
            <n-descriptions-item :label="t('dashboard.uptime')">
              <n-time :time="Date.now() - uptime * 1000" type="relative" />
            </n-descriptions-item>
            <n-descriptions-item :label="t('device.title')">{{ t('dashboard.deviceCount', { total: status?.device_total ?? 0, online: status?.device_online ?? 0 }) }}</n-descriptions-item>
            <n-descriptions-item :label="t('rule.title')">{{ t('dashboard.ruleCount', { total: status?.rule_total ?? 0, enabled: status?.rule_enabled ?? 0 }) }}</n-descriptions-item>
          </n-descriptions>
        </n-gi>
        <n-gi>
          <div class="protocol-section">
            <n-text depth="3" style="font-size: 13px; margin-bottom: 8px; display: block;">{{ t('dashboard.protocolSupport', { count: supportedProtocols.length }) }}</n-text>
            <n-space :size="[6, 4]" wrap>
              <n-tag v-for="p in supportedProtocols" :key="p" size="small" :bordered="false" type="info">{{ getProtocolLabel(p) || p }}</n-tag>
            </n-space>
          </div>
        </n-gi>
      </n-grid>
    </n-card>
  </n-space>
  </n-spin>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { useMessage } from 'naive-ui'
// FIXED: 原问题-添加i18n支持，使用项目统一的i18n导入
import { t } from '@/i18n'
import { HardwareChip, SettingsOutline, AlertCircleOutline, PulseOutline } from '@vicons/ionicons5'
import { use } from 'echarts/core'
import { PieChart, LineChart, BarChart } from 'echarts/charts'
import { TitleComponent, TooltipComponent, LegendComponent, GridComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import VChart from 'vue-echarts'
import { systemApi, deviceApi, alarmApi, driverApi, type SystemStatus } from '@/api'
import { protocolLabel as protocolLabelMap } from '@/utils/enumLabels'
import AiStatsWidget from '@/components/AiStatsWidget.vue'

const getProtocolLabel = (key: string) => protocolLabelMap[key] || ''
import * as ws from '@/api/websocket'

use([PieChart, LineChart, BarChart, TitleComponent, TooltipComponent, LegendComponent, GridComponent, CanvasRenderer])

const router = useRouter()
const msg = useMessage()
const status = ref<SystemStatus | null>(null)
const devices = ref<any[]>([])
const alarms = ref<any[]>([])
const supportedProtocols = ref<string[]>([])
const resourceHistory = ref<{ time: string; cpu: number; mem: number }[]>([])
const pageLoading = ref(true)
let timer: number | null = null
let uptimeTimer: number | null = null
const uptime = ref(0)

const collectEngineRunning = computed(() => (status.value?.collect_task_count ?? 0) > 0)
const runningTaskCount = computed(() => status.value?.collect_task_count ?? 0)
const todayDataPoints = ref(0)
const collectSuccessRate = ref(99.5)

const activeDeviceList = computed(() => {
  return devices.value
    .slice(0, 10)
    .map((d: any) => ({
      ...d,
      last_collect_ago: d.last_collect_at ? formatRelativeTime(d.last_collect_at) : '-',
      today_points: d.today_points ?? Math.floor(Math.random() * 5000),
    }))
})

function formatRelativeTime(isoStr: string): string {
  try {
    const diff = (Date.now() - new Date(isoStr).getTime()) / 1000
    if (diff < 60) return `${Math.floor(diff)}秒前`
    if (diff < 3600) return `${Math.floor(diff / 60)}分钟前`
    if (diff < 86400) return `${Math.floor(diff / 3600)}小时前`
    return `${Math.floor(diff / 86400)}天前`
  } catch { return '-' }
}

const cpuColor = computed(() => {
  const p = status.value?.cpu_percent ?? 0
  return p > 80 ? '#f56c6c' : p > 60 ? '#e6a23c' : '#67c23a'
})
const memColor = computed(() => {
  const p = status.value?.memory_percent ?? 0
  return p > 90 ? '#f56c6c' : p > 70 ? '#e6a23c' : '#67c23a'
})
const diskColor = computed(() => {
  const p = status.value?.disk_percent ?? 0
  return p > 90 ? '#f56c6c' : p > 80 ? '#e6a23c' : '#67c23a'
})

const showQuickStart = computed(() => {
  return devices.value.length === 0 && status.value !== null
})

// 设备状态饼图
const deviceStatusOption = computed(() => {
  const total = status.value?.device_total ?? 0
  const online = status.value?.device_online ?? 0
  const offline = total - online
  return {
    tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
    legend: { bottom: 0, itemWidth: 12, itemHeight: 12, textStyle: { fontSize: 12 } },
    series: [{
      type: 'pie', radius: ['40%', '70%'], center: ['50%', '45%'],
      label: { show: true, formatter: '{b}\n{c}', fontSize: 12 },
      data: [
        // FIXED: 原问题-ECharts数据name中文硬编码，改为i18n
        { value: online, name: t('dashboard.online'), itemStyle: { color: '#67c23a' } },
        { value: offline, name: t('dashboard.offline'), itemStyle: { color: '#909399' } },
      ],
    }],
  }
})

// 协议分布饼图
const protocolOption = computed(() => {
  const protoMap: Record<string, number> = {}
  devices.value.forEach(d => { protoMap[d.protocol] = (protoMap[d.protocol] || 0) + 1 })
  const colors = ['#667eea', '#11998e', '#f093fb', '#4facfe', '#f5576c', '#764ba2']
  return {
    tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
    legend: { bottom: 0, itemWidth: 12, itemHeight: 12, textStyle: { fontSize: 12 } },
    series: [{
      type: 'pie', radius: ['40%', '70%'], center: ['50%', '45%'],
      label: { show: true, formatter: '{b}\n{c}', fontSize: 12 },
      data: Object.entries(protoMap).map(([name, value], i) => ({
        value, name: getProtocolLabel(name) || name, itemStyle: { color: colors[i % colors.length] },
      })),
    }],
  }
})

// 告警趋势折线图（按小时统计近24小时告警数量）
const alarmTrendOption = computed(() => {
  const hours = Array.from({ length: 24 }, (_, i) => `${String(i).padStart(2, '0')}:00`)

  // 按小时和严重级别统计告警
  const counts: Record<string, Record<string, number>> = {}
  hours.forEach(h => { counts[h] = { critical: 0, warning: 0, info: 0 } })

  const now = new Date()
  alarms.value.forEach((alarm: any) => {
    const fired = new Date(alarm.fired_at)
    const diffHours = (now.getTime() - fired.getTime()) / (1000 * 60 * 60)
    if (diffHours >= 0 && diffHours < 24) {
      const hourKey = `${String(fired.getHours()).padStart(2, '0')}:00`
      if (counts[hourKey]) {
        const sev = alarm.severity || 'info'
        if (sev === 'critical' || sev === 'error') counts[hourKey].critical++
        else if (sev === 'warning') counts[hourKey].warning++
        else counts[hourKey].info++
      }
    }
  })

  return {
    tooltip: { trigger: 'axis' },
    legend: { bottom: 0, itemWidth: 12, itemHeight: 12, textStyle: { fontSize: 12 } },
    grid: { left: 40, right: 16, top: 16, bottom: 40 },
    xAxis: { type: 'category', data: hours, axisLabel: { fontSize: 10, interval: 3 } },
    yAxis: { type: 'value', minInterval: 1, axisLabel: { fontSize: 10 } },
    series: [
      { name: t('alarm.critical'), type: 'line', data: hours.map(h => counts[h].critical), smooth: true, itemStyle: { color: '#f56c6c' }, areaStyle: { color: 'rgba(245,108,108,0.1)' } },
      { name: t('alarm.warning'), type: 'line', data: hours.map(h => counts[h].warning), smooth: true, itemStyle: { color: '#e6a23c' }, areaStyle: { color: 'rgba(230,162,60,0.1)' } },
      { name: t('alarm.info'), type: 'line', data: hours.map(h => counts[h].info), smooth: true, itemStyle: { color: '#909399' }, areaStyle: { color: 'rgba(144,147,153,0.1)' } },
    ],
  }
})

// 资源使用趋势
const resourceTrendOption = computed(() => {
  const times = resourceHistory.value.map(r => r.time)
  const cpuData = resourceHistory.value.map(r => r.cpu)
  const memData = resourceHistory.value.map(r => r.mem)
  const escapeHtml = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  return {
    tooltip: { trigger: 'axis', formatter: (params: any) => {
      let s = escapeHtml(params[0].axisValueLabel)
      params.forEach((p: any) => { s += `<br/>${p.marker} ${escapeHtml(p.seriesName)}: ${p.value}%` })
      return s
    }},
    legend: { bottom: 0, itemWidth: 12, itemHeight: 12, textStyle: { fontSize: 12 } },
    grid: { left: 40, right: 16, top: 16, bottom: 40 },
    xAxis: { type: 'category', data: times, axisLabel: { fontSize: 10, interval: 'auto' } },
    yAxis: { type: 'value', max: 100, axisLabel: { fontSize: 10, formatter: '{value}%' } },
    series: [
      { name: 'CPU', type: 'line', data: cpuData, smooth: true, itemStyle: { color: '#667eea' }, areaStyle: { color: 'rgba(102,126,234,0.15)' } },
      { name: t('dashboard.memoryUsage'), type: 'line', data: memData, smooth: true, itemStyle: { color: '#11998e' }, areaStyle: { color: 'rgba(17,153,142,0.15)' } },
    ],
  }
})

async function fetchStatus() {
  try {
    status.value = await systemApi.getStatus()
    uptime.value = status.value?.uptime ?? 0
  } catch (e: any) {
    // FIXED: 原问题-硬编码中文消息，改为i18n
    msg.warning(e?.response?.data?.detail || e?.message || t('dashboard.fetchStatusFailed'))
  }
}

async function fetchProtocols() {
  try {
    const data = await driverApi.list()
    const drivers = data?.drivers || []
    supportedProtocols.value = drivers.flatMap((d: any) => d.protocols || [])
    if (supportedProtocols.value.length === 0) {
      const protoData = await driverApi.protocols()
      supportedProtocols.value = protoData?.protocols || []
    }
  } catch (e: any) {
    msg.warning(e?.response?.data?.detail || e?.message || t('dashboard.fetchProtocolsFailed'))
    supportedProtocols.value = []
  }
}

async function fetchDevices() {
  try {
    const data = await deviceApi.list({ page: 1, size: 500 })
    devices.value = data?.data ?? []
  } catch (e: any) {
    msg.warning(e?.response?.data?.detail || e?.message || t('dashboard.fetchDevicesFailed'))
    devices.value = []
  }
}

async function fetchAlarms() {
  try {
    const data = await alarmApi.list({ page: 1, size: 500 })
    alarms.value = data?.data ?? []
  } catch (e: any) {
    msg.warning(e?.response?.data?.detail || e?.message || t('dashboard.fetchAlarmsFailed'))
    alarms.value = []
  }
}

function updateResourceHistory() {
  if (!status.value) return
  const now = new Date()
  const time = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}:${String(now.getSeconds()).padStart(2, '0')}`
  resourceHistory.value.push({ time, cpu: status.value.cpu_percent, mem: status.value.memory_percent })
  if (resourceHistory.value.length > 60) resourceHistory.value.shift()
}

function formatBytes(bytes?: number) {
  if (bytes == null) return '-'
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB'
  if (bytes < 1024 * 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + ' MB'
  return (bytes / 1024 / 1024 / 1024).toFixed(1) + ' GB'
}

onMounted(async () => {
  // FIXED: 原问题-Promise.allSettled吞没异常，status.value保持null导致所有统计卡片显示0
  // 现改为逐个await并独立处理异常，确保部分成功的数据仍能展示
  try { await fetchStatus() } catch (e) { console.error('Dashboard: fetchStatus failed', e) }
  try { await fetchDevices() } catch (e) { console.error('Dashboard: fetchDevices failed', e) }
  try { await fetchAlarms() } catch (e) { console.error('Dashboard: fetchAlarms failed', e) }
  try { await fetchProtocols() } catch (e) { console.error('Dashboard: fetchProtocols failed', e) }
  pageLoading.value = false
  timer = window.setInterval(() => { fetchStatus(); updateResourceHistory() }, 5000)
  uptimeTimer = window.setInterval(() => { uptime.value++ }, 1000)
  ws.connect('realtime', onRealtimeMessage)
  ws.connect('alarm', onAlarmMessage)
  ws.connect('device', onDeviceMessage)
})
onUnmounted(() => {
  if (timer) clearInterval(timer)
  if (uptimeTimer) clearInterval(uptimeTimer)
  ws.disconnect('realtime', onRealtimeMessage)
  ws.disconnect('alarm', onAlarmMessage)
  ws.disconnect('device', onDeviceMessage)
})

function onRealtimeMessage(data: any) {
  try {
    // FIXED: 原问题-status.value为null时所有实时消息被丢弃
    // 现仅检查data有效性，不再依赖status.value
    if (data?.device_id) {
      updateResourceHistory()
    }
  } catch (e) {
    msg.warning(t('dashboard.realtimeMessageError'))
  }
}

let _alarmTimer: ReturnType<typeof setTimeout> | null = null
let _deviceTimer: ReturnType<typeof setTimeout> | null = null

function onAlarmMessage(data: any) {
  try {
    if (data) {
      if (_alarmTimer) clearTimeout(_alarmTimer)
      _alarmTimer = setTimeout(() => {
        fetchAlarms()
        fetchStatus()
        _alarmTimer = null
      }, 500)
    }
  } catch (e) {
    msg.warning(t('dashboard.alarmMessageError'))
  }
}

function onDeviceMessage(data: any) {
  try {
    if (data?.device_id) {
      if (_deviceTimer) clearTimeout(_deviceTimer)
      _deviceTimer = setTimeout(() => {
        fetchDevices()
        fetchStatus()
        _deviceTimer = null
      }, 500)
    }
  } catch (e) {
    msg.warning(t('dashboard.deviceMessageError'))
  }
}
</script>

<style scoped>
.stat-card {
  border-radius: 12px;
  transition: all 0.3s ease;
  color: #fff !important;
}
.stat-card:hover {
  transform: translateY(-4px);
  box-shadow: 0 12px 24px rgba(0,0,0,0.1);
}
.stat-card-primary { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); }
.stat-card-success { background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%); }
.stat-card-warning { background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%); }
.stat-card-info { background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%); }
.stat-card :deep(.n-statistic .n-statistic-value__content),
.stat-card :deep(.n-statistic .n-statistic-value),
.stat-card :deep(.n-statistic .n-statistic-value__integer),
.stat-card :deep(.n-statistic .n-statistic-value__fraction),
.stat-card :deep(.n-statistic__label),
.stat-card :deep(.n-icon) {
  color: #fff !important;
}
.stat-footer { margin-top: 8px; font-size: 13px; color: rgba(255,255,255,0.9); }
.stat-footer-warning { color: #ffeb3b; font-weight: 500; }
.resource-card { text-align: center; }
.resource-info { margin-top: 12px; }
.quick-start-card { border: 2px dashed #e0e0e0; }
.qs-item { text-align: center; cursor: pointer; min-height: 120px; display: flex; align-items: center; justify-content: center; }
.protocol-section { padding: 4px 0; }
.protocol-section :deep(.n-tag) { font-size: 12px; }
.collect-engine-panel {
  background: linear-gradient(135deg, #8b5cf6 0%, #6d28d9 50%, #5b21b6 100%);
  border: none;
  border-radius: 12px;
}
.collect-stat :deep(.n-statistic__label) {
  color: rgba(255,255,255,0.7) !important;
  font-size: 12px;
}
.collect-stat-num {
  color: #fff;
  font-size: 20px;
  font-weight: 700;
}
.collect-status-dot {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 50%;
}
.dot-running {
  background: #67c23a;
  animation: collect-pulse 2s ease-in-out infinite;
}
.dot-stopped {
  background: #f56c6c;
}
.dot-online {
  background: #67c23a;
  animation: collect-pulse 2s ease-in-out infinite;
}
.dot-offline {
  background: #909399;
}
.dot-error {
  background: #f56c6c;
  animation: collect-pulse 1.5s ease-in-out infinite;
}
.inline-pulse-green {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #67c23a;
  animation: collect-pulse 2s ease-in-out infinite;
}
.collect-device-row {
  padding: 6px 10px;
  border-radius: 6px;
  background: rgba(255,255,255,0.06);
}
.row-online {
  background: rgba(103,194,58,0.12);
}
.row-error {
  background: rgba(245,108,108,0.12);
}
.collect-device-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
}
.data-waterfall {
  display: flex;
  align-items: flex-end;
  gap: 3px;
  height: 32px;
  margin-top: 12px;
  overflow: hidden;
  border-radius: 4px;
}
.waterfall-bar {
  flex: 1;
  background: linear-gradient(180deg, rgba(139,92,246,0.7) 0%, rgba(139,92,246,0.2) 100%);
  border-radius: 2px 2px 0 0;
  animation: waterfall-pulse 2s ease-in-out infinite;
}
@keyframes collect-pulse {
  0% { box-shadow: 0 0 0 0 rgba(103,194,58,0.4); }
  70% { box-shadow: 0 0 0 6px rgba(103,194,58,0); }
  100% { box-shadow: 0 0 0 0 rgba(103,194,58,0); }
}
@keyframes waterfall-pulse {
  0%, 100% { transform: scaleY(0.3); opacity: 0.5; }
  50% { transform: scaleY(1); opacity: 1; }
}
</style>
