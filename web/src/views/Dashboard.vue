<template>
  <n-space vertical :size="20">
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
      <n-descriptions label-placement="left" :column="3" bordered>
        <n-descriptions-item label="运行时长">
          <n-time :time="Date.now() - uptime * 1000" type="relative" />
        </n-descriptions-item>
        <n-descriptions-item label="版本">{{ status?.version ?? '-' }}</n-descriptions-item>
        <n-descriptions-item label="协议支持">
          <n-space :size="6">
            <n-tag v-for="p in supportedProtocols" :key="p" size="small" type="info">{{ p }}</n-tag>
          </n-space>
        </n-descriptions-item>
      </n-descriptions>
    </n-card>
  </n-space>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { HardwareChip, SettingsOutline, AlertCircleOutline, PulseOutline } from '@vicons/ionicons5'
import { use } from 'echarts/core'
import { PieChart, LineChart, BarChart } from 'echarts/charts'
import { TitleComponent, TooltipComponent, LegendComponent, GridComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import VChart from 'vue-echarts'
import { systemApi, deviceApi, alarmApi, driverApi, type SystemStatus } from '@/api'

use([PieChart, LineChart, BarChart, TitleComponent, TooltipComponent, LegendComponent, GridComponent, CanvasRenderer])

const status = ref<SystemStatus | null>(null)
const devices = ref<any[]>([])
const alarms = ref<any[]>([])
const supportedProtocols = ref<string[]>([])
const resourceHistory = ref<{ time: string; cpu: number; mem: number }[]>([])
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
        value, name: protocolLabel(name), itemStyle: { color: colors[i % colors.length] },
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

function protocolLabel(p: string) {
  const map: Record<string, string> = {
    modbus_tcp: 'Modbus TCP', modbus_rtu: 'Modbus RTU', opcua: 'OPC-UA', opc_da: 'OPC DA',
    mqtt: 'MQTT', mqtt_client: 'MQTT', http: 'HTTP', http_webhook: 'HTTP',
    simulator: 'Simulator', video: 'Video', s7: 'S7', mc: 'MC', fins: 'FINS',
    allen_bradley: 'AB', fanuc: 'FANUC', mtconnect: 'MTConnect', toledo: 'Toledo',
    bacnet: 'BACnet', serial_port: 'Serial', database_source: 'DB', barcode_scanner: 'Scanner',
  }
  return map[p] || p
}

async function fetchStatus() {
  try {
    status.value = await systemApi.getStatus()
    uptime.value = status.value?.uptime ?? 0
  } catch (e: any) {
    console.warn('获取系统状态失败:', e)
  }
}

async function fetchProtocols() {
  try {
    const data = await driverApi.list()
    const drivers = data?.drivers || []
    supportedProtocols.value = drivers.flatMap((d: any) => d.protocols || [])
  } catch (e) {
    console.warn('获取协议列表失败:', e)
    supportedProtocols.value = ['modbus_tcp', 'opcua', 'mqtt', 'http', 'simulator']
  }
}

async function fetchDevices() {
  try {
    const data = await deviceApi.list({ page: 1, size: 500 })
    devices.value = data?.data ?? []
  } catch (e) {
    console.warn('获取设备列表失败:', e)
    devices.value = []
  }
}

async function fetchAlarms() {
  try {
    const data = await alarmApi.list({ page: 1, size: 500 })
    alarms.value = data?.data ?? []
  } catch (e) {
    console.warn('获取告警列表失败:', e)
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

onMounted(() => {
  fetchStatus(); fetchDevices(); fetchAlarms(); fetchProtocols()
  timer = window.setInterval(() => { fetchStatus(); updateResourceHistory() }, 5000)
  uptimeTimer = window.setInterval(() => { uptime.value++ }, 1000)
})
onUnmounted(() => { if (timer) clearInterval(timer); if (uptimeTimer) clearInterval(uptimeTimer) })
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
</style>
