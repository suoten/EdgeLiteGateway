<template>
  <n-spin :show="pageLoading" description="加载中...">
  <n-space vertical :size="20">
    <!-- 快速开始引导（设备数为0时显示） -->
    <n-card v-if="showQuickStart" title="快速开始" :bordered="false" class="quick-start-card">
      <n-grid :cols="4" :x-gap="16" :y-gap="16" responsive="screen">
        <n-gi>
          <n-card hoverable class="qs-item" @click="router.push('/devices')">
            <n-space vertical align="center">
              <n-icon size="40" :component="HardwareChip" color="#667eea" />
              <n-text strong>创建设备</n-text>
              <n-text depth="3">接入第一个设备开始采集数据</n-text>
            </n-space>
          </n-card>
        </n-gi>
        <n-gi>
          <n-card hoverable class="qs-item" @click="router.push('/rules')">
            <n-space vertical align="center">
              <n-icon size="40" :component="SettingsOutline" color="#11998e" />
              <n-text strong>配置告警</n-text>
              <n-text depth="3">设置告警规则监控异常</n-text>
            </n-space>
          </n-card>
        </n-gi>
        <n-gi>
          <n-card hoverable class="qs-item" @click="router.push('/system/drivers')">
            <n-space vertical align="center">
              <n-icon size="40" :component="PulseOutline" color="#f093fb" />
              <n-text strong>驱动配置</n-text>
              <n-text depth="3">管理协议驱动和参数</n-text>
            </n-space>
          </n-card>
        </n-gi>
        <n-gi>
          <n-card hoverable class="qs-item" @click="router.push('/system/platforms')">
            <n-space vertical align="center">
              <n-icon size="40" :component="AlertCircleOutline" color="#4facfe" />
              <n-text strong>平台对接</n-text>
              <n-text depth="3">连接北向云平台</n-text>
            </n-space>
          </n-card>
        </n-gi>
      </n-grid>
    </n-card>

    <!-- 顶部统计卡片 -->
    <n-grid :cols="4" :x-gap="16" :y-gap="16" responsive="screen">
      <n-gi>
        <n-card class="stat-card stat-card-primary" :bordered="false">
          <n-statistic label="设备总数" :value="status?.device_total ?? 0">
            <template #prefix><n-icon :component="HardwareChip" /></template>
          </n-statistic>
          <div class="stat-footer">
            <span>在线 {{ status?.device_online ?? 0 }} 台</span>
          </div>
        </n-card>
      </n-gi>
      <n-gi>
        <n-card class="stat-card stat-card-success" :bordered="false">
          <n-statistic label="规则总数" :value="status?.rule_total ?? 0">
            <template #prefix><n-icon :component="SettingsOutline" /></template>
          </n-statistic>
          <div class="stat-footer">
            <span>已启用 {{ status?.rule_enabled ?? 0 }} 条</span>
          </div>
        </n-card>
      </n-gi>
      <n-gi>
        <n-card class="stat-card stat-card-warning" :bordered="false">
          <n-statistic label="活跃告警" :value="status?.alarm_firing ?? 0">
            <template #prefix><n-icon :component="AlertCircleOutline" /></template>
          </n-statistic>
          <div class="stat-footer">
            <span v-if="status?.alarm_firing" class="stat-footer-warning">需要处理</span>
            <span v-else>系统正常</span>
          </div>
        </n-card>
      </n-gi>
      <n-gi>
        <n-card class="stat-card stat-card-info" :bordered="false">
          <n-statistic label="采集任务" :value="status?.collect_task_count ?? 0">
            <template #prefix><n-icon :component="PulseOutline" /></template>
          </n-statistic>
          <div class="stat-footer">
            <span>运行中</span>
          </div>
        </n-card>
      </n-gi>
    </n-grid>

    <!-- ECharts 图表区域 -->
    <n-grid :cols="2" :x-gap="16" :y-gap="16">
      <n-gi>
        <n-card title="设备状态分布" :bordered="false">
          <v-chart :option="deviceStatusOption" autoresize style="height: 280px" />
        </n-card>
      </n-gi>
      <n-gi>
        <n-card title="协议接入分布" :bordered="false">
          <v-chart :option="protocolOption" autoresize style="height: 280px" />
        </n-card>
      </n-gi>
      <n-gi>
        <n-card title="告警趋势（近24小时）" :bordered="false">
          <v-chart :option="alarmTrendOption" autoresize style="height: 280px" />
        </n-card>
      </n-gi>
      <n-gi>
        <n-card title="资源使用趋势" :bordered="false">
          <v-chart :option="resourceTrendOption" autoresize style="height: 280px" />
        </n-card>
      </n-gi>
    </n-grid>

    <!-- 系统资源监控 -->
    <n-grid :cols="3" :x-gap="16" :y-gap="16">
      <n-gi>
        <n-card title="CPU 使用率" :bordered="false" class="resource-card">
          <n-progress type="circle" :percentage="status?.cpu_percent ?? 0" :stroke-width="12" :color="cpuColor" />
          <div class="resource-info">
            <n-text depth="3">当前负载</n-text>
          </div>
        </n-card>
      </n-gi>
      <n-gi>
        <n-card title="内存使用" :bordered="false" class="resource-card">
          <n-progress type="circle" :percentage="status?.memory_percent ?? 0" :stroke-width="12" :color="memColor" />
          <div class="resource-info">
            <n-text depth="3">{{ formatBytes(status?.memory_used) }} / {{ formatBytes(status?.memory_total) }}</n-text>
          </div>
        </n-card>
      </n-gi>
      <n-gi>
        <n-card title="磁盘使用" :bordered="false" class="resource-card">
          <n-progress type="circle" :percentage="status?.disk_percent ?? 0" :stroke-width="12" :color="diskColor" />
          <div class="resource-info">
            <n-text depth="3">{{ formatBytes(status?.disk_used) }} / {{ formatBytes(status?.disk_total) }}</n-text>
          </div>
        </n-card>
      </n-gi>
    </n-grid>

    <!-- 系统信息 -->
    <n-card title="系统信息" :bordered="false">
      <n-grid :cols="2" :x-gap="24" :y-gap="16">
        <n-gi>
          <n-descriptions label-placement="left" :column="1" bordered>
            <n-descriptions-item label="版本">
              <n-tag type="success" size="small">v{{ status?.version ?? '-' }} Community</n-tag>
            </n-descriptions-item>
            <n-descriptions-item label="运行时长">
              <n-time :time="Date.now() - uptime * 1000" type="relative" />
            </n-descriptions-item>
            <n-descriptions-item label="设备">{{ status?.device_total ?? 0 }} 台（在线 {{ status?.device_online ?? 0 }}）</n-descriptions-item>
            <n-descriptions-item label="规则">{{ status?.rule_total ?? 0 }} 条（启用 {{ status?.rule_enabled ?? 0 }}）</n-descriptions-item>
          </n-descriptions>
        </n-gi>
        <n-gi>
          <div class="protocol-section">
            <n-text depth="3" style="font-size: 13px; margin-bottom: 8px; display: block;">协议支持（{{ supportedProtocols.length }} 种）</n-text>
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
import { HardwareChip, SettingsOutline, AlertCircleOutline, PulseOutline } from '@vicons/ionicons5'
import { use } from 'echarts/core'
import { PieChart, LineChart, BarChart } from 'echarts/charts'
import { TitleComponent, TooltipComponent, LegendComponent, GridComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import VChart from 'vue-echarts'
import { systemApi, deviceApi, alarmApi, driverApi, type SystemStatus } from '@/api'
import { protocolLabel as protocolLabelMap } from '@/utils/enumLabels'

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
        { value: online, name: '在线', itemStyle: { color: '#67c23a' } },
        { value: offline, name: '离线', itemStyle: { color: '#909399' } },
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
      { name: '严重', type: 'line', data: hours.map(h => counts[h].critical), smooth: true, itemStyle: { color: '#f56c6c' }, areaStyle: { color: 'rgba(245,108,108,0.1)' } },
      { name: '警告', type: 'line', data: hours.map(h => counts[h].warning), smooth: true, itemStyle: { color: '#e6a23c' }, areaStyle: { color: 'rgba(230,162,60,0.1)' } },
      { name: '信息', type: 'line', data: hours.map(h => counts[h].info), smooth: true, itemStyle: { color: '#909399' }, areaStyle: { color: 'rgba(144,147,153,0.1)' } },
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
      { name: '内存', type: 'line', data: memData, smooth: true, itemStyle: { color: '#11998e' }, areaStyle: { color: 'rgba(17,153,142,0.15)' } },
    ],
  }
})

async function fetchStatus() {
  try {
    status.value = await systemApi.getStatus()
    uptime.value = status.value?.uptime ?? 0
  } catch (e: any) {
    msg.warning('获取系统状态失败')
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
  } catch (e) {
    msg.warning('获取协议列表失败')
    supportedProtocols.value = []
  }
}

async function fetchDevices() {
  try {
    const data = await deviceApi.list({ page: 1, size: 500 })
    devices.value = data?.data ?? []
  } catch (e) {
    msg.warning('获取设备列表失败')
    devices.value = []
  }
}

async function fetchAlarms() {
  try {
    const data = await alarmApi.list({ page: 1, size: 500 })
    alarms.value = data?.data ?? []
  } catch (e) {
    msg.warning('获取告警列表失败')
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
  await Promise.all([fetchStatus(), fetchDevices(), fetchAlarms(), fetchProtocols()])
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
  if (data?.device_id && status.value) {
    updateResourceHistory()
  }
}

function onAlarmMessage(data: any) {
  if (data) {
    fetchAlarms()
    if (status.value) status.value.alarm_firing = (status.value.alarm_firing ?? 0) + 1
  }
}

function onDeviceMessage(data: any) {
  if (data?.device_id) {
    fetchDevices()
    fetchStatus()
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
</style>
