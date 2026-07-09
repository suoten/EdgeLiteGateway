<template>
  <n-spin :show="pageLoading" :description="t('dashboard.loading')">
    <div class="dash-root">
      <!-- 顶部状态栏 -->
      <header class="dash-topbar">
        <div class="topbar-left">
          <div class="topbar-brand">
            <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
              <path d="M12 2L2 7l10 5 10-5-10-5z" />
              <path d="M2 17l10 5 10-5" />
              <path d="M2 12l10 5 10-5" />
            </svg>
            <span class="topbar-title">{{ t('router.dashboard') }}</span>
          </div>
          <span class="topbar-sub">{{ t('dashboard.deviceStatusSummary') }}</span>
        </div>
        <div class="topbar-right">
          <div class="topbar-uptime">
            <span class="uptime-label">{{ t('dashboard.uptime') }}</span>
            <span class="uptime-val">{{ formatUptime(uptime) }}</span>
            <n-tag type="success" size="tiny" :bordered="false">v{{ status?.version ?? '-' }}</n-tag>
          </div>
          <n-tag :type="wsConnected ? 'success' : wsReconnecting ? 'warning' : 'error'" size="small" :bordered="false" round>
            <template #icon>
              <n-icon :component="WifiSharp" :size="14" />
            </template>
            {{ wsConnected ? t('wsStatus.connected') : wsReconnecting ? t('wsStatus.reconnecting') : t('wsStatus.disconnected') }}
          </n-tag>
          <n-button size="small" :loading="exportingSnapshot" @click="handleExportSnapshot">
            <template #icon><n-icon :component="DownloadOutline" /></template>
            {{ t('dashboard.exportSnapshot') }}
          </n-button>
          <!-- 修复11: 自动刷新开关——关闭时停止定时轮询，节省资源 -->
          <n-space align="center" :size="4">
            <n-text depth="3" style="font-size: 12px">{{ t('dashboard.autoRefresh') }}</n-text>
            <n-switch v-model:value="autoRefresh" size="small" @update:value="onAutoRefreshChange" />
          </n-space>
        </div>
      </header>

      <!-- KPI 指标条 -->
      <div class="kpi-strip">
        <div class="kpi-cell kpi-devices">
          <div class="kpi-cell-icon"><n-icon :component="HardwareChipOutline" :size="20" /></div>
          <div class="kpi-cell-body">
            <span class="kpi-cell-label">{{ t('dashboard.deviceTotal') }}</span>
            <span class="kpi-cell-value">{{ status?.device_total ?? 0 }}</span>
          </div>
          <div class="kpi-cell-meta">
            <span class="dot dot-on"></span>{{ status?.device_online ?? 0 }} {{ t('dashboard.online') }}
            <span class="dot dot-off"></span>{{ (status?.device_total ?? 0) - (status?.device_online ?? 0) }} {{ t('dashboard.offline') }}
            <template v-if="deviceDegradedCount > 0">
              <span class="dot dot-warn"></span>{{ deviceDegradedCount }} {{ t('dashboard.degraded') }}
            </template>
          </div>
        </div>

        <div class="kpi-cell kpi-alarms">
          <div class="kpi-cell-icon" :class="(status?.alarm_firing ?? 0) > 0 ? 'icon-danger' : 'icon-ok'">
            <n-icon :component="AlertCircleOutline" :size="20" />
          </div>
          <div class="kpi-cell-body">
            <span class="kpi-cell-label">{{ t('dashboard.activeAlarm') }}</span>
            <span class="kpi-cell-value" :class="(status?.alarm_firing ?? 0) > 0 ? 'val-danger' : ''">{{ status?.alarm_firing ?? 0 }}</span>
          </div>
          <div class="kpi-cell-meta">
            <span v-if="(status?.alarm_firing ?? 0) > 0" style="color:#f56c6c">{{ t('dashboard.needHandle') }}</span>
            <span v-else style="color:#22c55e">{{ t('dashboard.systemNormal') }}</span>
          </div>
        </div>

        <div class="kpi-cell kpi-collect">
          <div class="kpi-cell-icon" :class="collectEngineRunning ? 'icon-ok' : 'icon-muted'">
            <n-icon :component="PulseSharp" :size="20" />
          </div>
          <div class="kpi-cell-body">
            <span class="kpi-cell-label">{{ t('dashboard.collectEngine') }}</span>
            <span class="kpi-cell-value">{{ status?.collect_task_count ?? 0 }}</span>
            <!-- 修复10: 显示今日采集数据点数，避免 todayDataPoints 成为死代码 -->
            <span class="kpi-cell-sub">{{ t('dashboard.todayPoints') }}: {{ todayDataPoints }}</span>
          </div>
          <div class="kpi-cell-meta">
            <n-progress type="line" :percentage="collectSuccessRate" :height="4" :border-radius="2" :show-indicator="false" style="flex:1;max-width:80px" />
            <span>{{ collectSuccessRate }}%</span>
          </div>
        </div>

        <div class="kpi-cell kpi-ai">
          <div class="kpi-cell-icon icon-ai">
            <n-icon :component="SparklesOutline" :size="20" />
          </div>
          <div class="kpi-cell-body">
            <span class="kpi-cell-label">{{ t('ai.engineStatus') }}</span>
            <span class="kpi-cell-value">{{ aiStats.model_count ?? 0 }}</span>
          </div>
          <div class="kpi-cell-meta">
            <n-tag :type="aiEnabled ? 'success' : 'default'" size="tiny" :bordered="false">
              {{ aiEnabled ? t('ai.statusActive') : t('ai.statusUnavailable') }}
            </n-tag>
          </div>
        </div>

        <div class="kpi-cell kpi-rules">
          <div class="kpi-cell-icon icon-rules">
            <n-icon :component="BuildOutline" :size="20" />
          </div>
          <div class="kpi-cell-body">
            <span class="kpi-cell-label">{{ t('dashboard.ruleTotal') }}</span>
            <span class="kpi-cell-value">{{ status?.rule_total ?? 0 }}</span>
          </div>
          <div class="kpi-cell-meta">
            {{ t('dashboard.enabledCount', { count: status?.rule_enabled ?? 0 }) }}
          </div>
        </div>
      </div>

      <!-- 主网格区域 -->
      <div class="dash-main">
          <!-- 资源趋势 -->
          <div class="panel panel-resource">
            <div class="panel-head">
              <span class="panel-title">{{ t('dashboard.resourceTrend') }}</span>
              <div class="panel-badges">
                <span class="badge badge-cpu">CPU {{ status?.cpu_percent ?? 0 }}%</span>
                <span class="badge badge-mem">MEM {{ status?.memory_percent ?? 0 }}%</span>
              </div>
            </div>
            <div class="panel-body"><v-chart ref="resourceTrendChartRef" :option="resourceTrendOption" autoresize style="height:100%" /></div>
          </div>

          <!-- 系统资源仪表 -->
          <div class="panel panel-gauges">
            <div class="panel-head">
              <span class="panel-title">{{ t('dashboard.cpuUsage') }}</span>
            </div>
            <div class="panel-body gauge-body">
              <div class="gauge-item">
                <n-progress type="circle" :percentage="status?.cpu_percent ?? 0" :stroke-width="5" :color="cpuColor" :size="72">
                  <template #default><span class="gauge-val">{{ status?.cpu_percent ?? 0 }}</span></template>
                </n-progress>
                <span class="gauge-label">CPU</span>
              </div>
              <div class="gauge-item">
                <n-progress type="circle" :percentage="status?.memory_percent ?? 0" :stroke-width="5" :color="memColor" :size="72">
                  <template #default><span class="gauge-val">{{ status?.memory_percent ?? 0 }}</span></template>
                </n-progress>
                <span class="gauge-label">MEM</span>
              </div>
              <div class="gauge-item">
                <n-progress type="circle" :percentage="status?.disk_percent ?? 0" :stroke-width="5" :color="diskColor" :size="72">
                  <template #default><span class="gauge-val">{{ status?.disk_percent ?? 0 }}</span></template>
                </n-progress>
                <span class="gauge-label">DISK</span>
              </div>
            </div>
            <div class="gauge-detail">
              {{ formatBytes(status?.memory_used) }} / {{ formatBytes(status?.memory_total) }}
            </div>
          </div>

          <!-- 协议分布 -->
          <div class="panel panel-proto">
            <div class="panel-head">
              <span class="panel-title">{{ t('dashboard.protocolDist') }}</span>
            </div>
            <div class="panel-body"><v-chart :option="protocolOption" autoresize style="height:100%" /></div>
          </div>

          <!-- 告警趋势 -->
          <div class="panel panel-alarm">
            <div class="panel-head">
              <span class="panel-title">{{ t('dashboard.alarmTrend') }}</span>
              <div class="panel-badges" v-if="(status?.alarm_firing ?? 0) > 0">
                <span class="badge badge-danger">{{ status?.alarm_firing ?? 0 }} {{ t('dashboard.alarmFiring') }}</span>
              </div>
            </div>
            <div class="panel-body"><v-chart :option="alarmTrendOption" autoresize style="height:100%" /></div>
          </div>

          <!-- 设备状态总览 -->
          <div class="panel panel-devices">
            <div class="panel-head">
              <span class="panel-title">{{ t('dashboard.deviceStatus') }}</span>
            </div>
            <div class="panel-body device-body">
              <div class="device-chart-wrap">
                <v-chart :option="deviceDonutOption" autoresize style="height:100%" />
              </div>
              <div class="device-legend">
                <div class="legend-row">
                  <span class="legend-dot" style="background:#22c55e"></span>
                  <span class="legend-label">{{ t('dashboard.online') }}</span>
                  <span class="legend-val">{{ status?.device_online ?? 0 }}</span>
                </div>
                <div class="legend-row">
                  <span class="legend-dot" style="background:#64748b"></span>
                  <span class="legend-label">{{ t('dashboard.offline') }}</span>
                  <span class="legend-val">{{ (status?.device_total ?? 0) - (status?.device_online ?? 0) }}</span>
                </div>
                <div class="legend-row" v-if="deviceDegradedCount > 0">
                  <span class="legend-dot" style="background:#e6a23c"></span>
                  <span class="legend-label">{{ t('dashboard.degraded') }}</span>
                  <span class="legend-val">{{ deviceDegradedCount }}</span>
                </div>
                <div class="legend-divider"></div>
                <div class="legend-row legend-total">
                  <span class="legend-label">{{ t('dashboard.deviceTotal') }}</span>
                  <span class="legend-val">{{ status?.device_total ?? 0 }}</span>
                </div>
              </div>
            </div>
          </div>

          <!-- AI 推理统计 -->
          <div class="panel panel-ai">
            <div class="panel-head">
              <span class="ai-badge-sm">AI</span>
              <span class="panel-title">{{ t('ai.engineStatus') }}</span>
              <n-tag :type="aiEnabled ? 'success' : 'default'" size="tiny" :bordered="false">
                {{ aiEnabled ? t('ai.statusActive') : t('ai.statusUnavailable') }}
              </n-tag>
            </div>
            <div class="panel-body ai-body">
              <div class="ai-stat">
                <span class="ai-stat-label">{{ t('ai.modelCount') }}</span>
                <span class="ai-stat-val">{{ aiStats.model_count ?? 0 }}</span>
              </div>
              <div class="ai-stat">
                <span class="ai-stat-label">{{ t('ai.totalCalls') }}</span>
                <span class="ai-stat-val">{{ aiStats.total_calls ?? 0 }}</span>
              </div>
              <div class="ai-stat">
                <span class="ai-stat-label">{{ t('ai.avgLatency') }}</span>
                <span class="ai-stat-val">{{ aiStats.avg_latency_ms ?? '-' }}<small>ms</small></span>
              </div>
              <div class="ai-stat">
                <span class="ai-stat-label">{{ t('ai.totalErrors') }}</span>
                <span class="ai-stat-val" :class="{ 'val-danger': (aiStats.total_errors ?? 0) > 0 }">{{ aiStats.total_errors ?? 0 }}</span>
              </div>
            </div>
          </div>

          <!-- 设备健康 -->
          <div class="panel panel-health">
            <div class="panel-head">
              <span class="panel-title">{{ t('dashboard.deviceHealth') }}</span>
              <span class="health-summary" v-if="unhealthyDevices.length === 0">
                <n-icon :component="CheckmarkCircleOutline" :size="14" style="color:#22c55e" />
                {{ t('dashboard.allDevicesHealthy') }}
              </span>
              <span class="health-summary health-warn" v-else>
                <n-icon :component="WarningOutline" :size="14" style="color:#e6a23c" />
                {{ unhealthyDevices.length }} {{ t('dashboard.degraded') }}
              </span>
            </div>
            <div class="panel-body health-body">
              <div v-if="unhealthyDevices.length > 0" class="health-list">
                <div v-for="d in unhealthyDevices" :key="d.device_id" class="health-item">
                  <span class="health-name">{{ d.device_id }}</span>
                  <n-progress type="line" :percentage="d.score" :height="4" :border-radius="2" :show-indicator="false" :color="d.score < 50 ? '#f56c6c' : d.score < 80 ? '#e6a23c' : '#22c55e'" style="flex:1" />
                  <span class="health-score" :style="{ color: d.score < 50 ? '#f56c6c' : d.score < 80 ? '#e6a23c' : '#22c55e' }">{{ d.score }}</span>
                </div>
              </div>
              <div v-else class="health-all-ok">
                <div class="health-ok-icon">
                  <n-icon :component="CheckmarkCircleOutline" :size="32" style="color:#22c55e" />
                </div>
                <span>{{ t('dashboard.allDevicesHealthy') }}</span>
              </div>
            </div>
          </div>

          <!-- 协议支持 -->
          <div class="panel panel-protocols">
            <div class="panel-head">
              <span class="panel-title">{{ t('dashboard.protocolSupport', { count: supportedProtocols.length }) }}</span>
            </div>
            <div class="panel-body proto-body">
              <div class="proto-tags">
                <span v-for="p in supportedProtocols" :key="p" class="proto-tag">{{ getProtocolLabel(p) || p }}</span>
              </div>
            </div>
          </div>

          <!-- 云同步 -->
          <div class="panel panel-sync">
            <CloudSyncStatus />
          </div>
      </div>
    </div>
  </n-spin>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { t } from '@/i18n'
import { extractError } from '@/utils/errorCodes'
import { usePageVisibility } from '@/composables/usePageVisibility'
import {
  HardwareChipOutline, AlertCircleOutline, WifiSharp, TimeOutline,
  PulseSharp, SparklesOutline, BuildOutline,
  CheckmarkCircleOutline, WarningOutline, DownloadOutline,
} from '@vicons/ionicons5'
import { use } from 'echarts/core'
import { PieChart, LineChart } from 'echarts/charts'
import { TitleComponent, TooltipComponent, LegendComponent, GridComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import VChart from 'vue-echarts'
import { systemApi, deviceApi, alarmApi, driverApi, dataApi, type SystemStatus } from '@/api'
import { protocolLabel as protocolLabelMap } from '@/utils/enumLabels'
import { aiApi } from '@/api'
import * as ws from '@/api/websocket'
import CloudSyncStatus from '@/components/CloudSyncStatus.vue'
import { message as msg } from '@/utils/discreteApi'
// [AUDIT-FIX] 建议-1: 改用公共 useChartTheme composable 替代内联主题色实现，消除重复代码
import { useChartTheme, type EChartsOption } from '@/composables/useChartTheme'
import { CATEGORICAL_PALETTE, SEMANTIC_COLORS, BRAND_COLORS } from '@/constants/chartPalette'

use([PieChart, LineChart, TitleComponent, TooltipComponent, LegendComponent, GridComponent, CanvasRenderer])

const {
  themeVars,
  chartAxisColor,
  chartSplitLineColor,
  chartValueAxis,
  chartCategoryAxis,
  chartTooltipAxis,
  chartLegend,
  chartLegendColor,
} = useChartTheme()

// Dashboard 专用 legend 布局（在公共 composable 颜色注入基础上补充布局参数）
function dashboardLegend() {
  return chartLegend({
    bottom: 0,
    itemWidth: 10,
    itemHeight: 10,
    textStyle: { fontSize: 11, color: chartLegendColor.value },
  })
}

const status = ref<SystemStatus | null>(null)
const devices = ref<any[]>([])
const alarms = ref<any[]>([])
const supportedProtocols = ref<string[]>([])
const resourceHistory = ref<{ time: string; cpu: number; mem: number }[]>([])
const pageLoading = ref(true)
let timer: number | null = null
let uptimeTimer: number | null = null
const uptime = ref(0)

// 页面可见性检测：页面隐藏时暂停轮询，恢复可见时立即刷新并恢复轮询
const { isVisible } = usePageVisibility()

// 修复11: 自动刷新开关——localStorage 持久化，关闭时停止定时轮询
const AUTO_REFRESH_KEY = 'dashboard_auto_refresh'
const autoRefresh = ref<boolean>(loadAutoRefresh())

function loadAutoRefresh(): boolean {
  try {
    const raw = localStorage.getItem(AUTO_REFRESH_KEY)
    return raw == null ? true : raw === 'true'
  } catch { return true }
}

function saveAutoRefresh() {
  try { localStorage.setItem(AUTO_REFRESH_KEY, String(autoRefresh.value)) } catch { /* ignore */ }
}

function startAutoRefreshTimer() {
  if (timer) { clearInterval(timer); timer = null }
  if (!autoRefresh.value) return
  timer = window.setInterval(() => { fetchStatus(true); updateResourceHistory(); fetchDashboardStats(); fetchAiStats(); fetchDeviceHealth() }, 5000)
}

function stopAutoRefreshTimer() {
  if (timer) { clearInterval(timer); timer = null }
}

function onAutoRefreshChange(val: boolean) {
  saveAutoRefresh()
  if (val) {
    startAutoRefreshTimer()
  } else {
    stopAutoRefreshTimer()
  }
}

// 修复2: 仪表盘导出快照
const resourceTrendChartRef = ref<InstanceType<typeof VChart> | null>(null)
const exportingSnapshot = ref(false)
async function handleExportSnapshot() {
  const chartInst = resourceTrendChartRef.value as any
  if (!chartInst) {
    msg.warning(t('dashboard.snapshotNoChart'))
    return
  }
  exportingSnapshot.value = true
  try {
    // 延迟一帧确保图表已渲染
    await new Promise(resolve => requestAnimationFrame(resolve))
    const dataUrl = chartInst.getDataURL({
      type: 'png',
      pixelRatio: 2,
      backgroundColor: '#fff',
    })
    const a = document.createElement('a')
    a.href = dataUrl
    a.download = `dashboard-snapshot-${new Date().toISOString().replace(/[:.]/g, '-')}.png`
    a.click()
    msg.success(t('dashboard.snapshotExported'))
  } catch (e: any) {
    msg.error(t('dashboard.snapshotExportFailed'))
  } finally {
    exportingSnapshot.value = false
  }
}

const collectEngineRunning = computed(() => (status.value?.collect_task_count ?? 0) > 0)
const todayDataPoints = ref(0)
const collectSuccessRate = ref(0)

const aiStats = ref<Record<string, any>>({})
const aiEnabled = ref(false)
const unhealthyDevices = ref<{ device_id: string; score: number }[]>([])

const wsConnected = ref(false)
const wsReconnecting = ref(false)
const deviceDegradedCount = computed(() => unhealthyDevices.value.length)
// 修复10: 移除未使用的 alarmRecoveredCount computed（死代码）

async function fetchDeviceHealth() {
  try {
    const data = await deviceApi.listHealthAll()
    // 适配1: 兼容 PagedResponse 数组与旧版 device_id→health 对象两种格式
    let entries: { device_id: string; score: number }[] = []
    if (Array.isArray(data)) {
      entries = data
        .map((info: any) => ({ device_id: info?.device_id, score: info?.connection_quality_score ?? 100 }))
        .filter(d => d.device_id && d.score < 100)
        .sort((a, b) => a.score - b.score)
        .slice(0, 5)
    } else if (data && typeof data === 'object') {
      entries = Object.entries(data)
        .map(([id, info]: [string, any]) => ({ device_id: id, score: info?.connection_quality_score ?? 100 }))
        .filter(d => d.score < 100)
        .sort((a, b) => a.score - b.score)
        .slice(0, 5)
    }
    unhealthyDevices.value = entries
  } catch (_e) { /* ignore */ }
}

function formatUptime(seconds: number): string {
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`
  return `${Math.floor(seconds / 86400)}d ${Math.floor((seconds % 86400) / 3600)}h`
}

const cpuColor = computed(() => {
  const p = status.value?.cpu_percent ?? 0
  return p > 80 ? themeVars.value.errorColor : p > 60 ? themeVars.value.warningColor : themeVars.value.successColor
})
const memColor = computed(() => {
  const p = status.value?.memory_percent ?? 0
  return p > 90 ? themeVars.value.errorColor : p > 70 ? themeVars.value.warningColor : themeVars.value.successColor
})
const diskColor = computed(() => {
  const p = status.value?.disk_percent ?? 0
  return p > 90 ? themeVars.value.errorColor : p > 80 ? themeVars.value.warningColor : themeVars.value.successColor
})

const getProtocolLabel = (key: string) => protocolLabelMap.value[key] || ''

const deviceDonutOption = computed(() => {
  const online = status.value?.device_online ?? 0
  const offline = (status.value?.device_total ?? 0) - online
  const degraded = deviceDegradedCount.value
  const seriesData: any[] = [
    { value: online, name: t('dashboard.online'), itemStyle: { color: SEMANTIC_COLORS.online } },
    { value: offline, name: t('dashboard.offline'), itemStyle: { color: SEMANTIC_COLORS.offline } },
  ]
  if (degraded > 0) {
    seriesData.push({ value: degraded, name: t('dashboard.degraded'), itemStyle: { color: SEMANTIC_COLORS.degraded } })
  }
  return {
    tooltip: { show: false },
    series: [{
      type: 'pie', radius: ['55%', '82%'], center: ['50%', '50%'],
      label: { show: false },
      data: seriesData,
      silent: true,
    }],
  }
})

const protocolOption = computed<EChartsOption>(() => {
  const protoMap: Record<string, number> = {}
  devices.value.forEach(d => { protoMap[d.protocol] = (protoMap[d.protocol] || 0) + 1 })
  const colors = CATEGORICAL_PALETTE
  return {
    tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
    legend: dashboardLegend(),
    series: [{
      type: 'pie', radius: ['45%', '72%'], center: ['50%', '45%'],
      label: { show: true, formatter: '{b}\n{c}', fontSize: 11, color: themeVars.value.textColor1 },
      data: Object.entries(protoMap).map(([name, value], i) => ({
        value, name: getProtocolLabel(name) || name, itemStyle: { color: colors[i % colors.length] },
      })),
    }],
  }
})

const alarmTrendOption = computed<EChartsOption>(() => {
  const hours = Array.from({ length: 24 }, (_, i) => `${String(i).padStart(2, '0')}:00`)
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
    tooltip: chartTooltipAxis(),
    legend: dashboardLegend(),
    grid: { left: 36, right: 12, top: 12, bottom: 36 },
    xAxis: chartCategoryAxis({ data: hours, axisLabel: { fontSize: 10, interval: 3, color: chartAxisColor.value } }),
    yAxis: chartValueAxis({ minInterval: 1 }),
    series: [
      { name: t('alarm.critical'), type: 'line', data: hours.map(h => counts[h].critical), smooth: true, symbol: 'none', lineStyle: { width: 1.5 }, itemStyle: { color: SEMANTIC_COLORS.critical }, areaStyle: { color: 'rgba(245,108,108,0.08)' } },
      { name: t('alarm.warning'), type: 'line', data: hours.map(h => counts[h].warning), smooth: true, symbol: 'none', lineStyle: { width: 1.5 }, itemStyle: { color: SEMANTIC_COLORS.warning }, areaStyle: { color: 'rgba(230,162,60,0.08)' } },
      { name: t('alarm.info'), type: 'line', data: hours.map(h => counts[h].info), smooth: true, symbol: 'none', lineStyle: { width: 1.5 }, itemStyle: { color: SEMANTIC_COLORS.info }, areaStyle: { color: 'rgba(144,147,153,0.08)' } },
    ],
  }
})

const resourceTrendOption = computed<EChartsOption>(() => {
  const times = resourceHistory.value.map(r => r.time)
  const cpuData = resourceHistory.value.map(r => r.cpu)
  const memData = resourceHistory.value.map(r => r.mem)
  return {
    tooltip: chartTooltipAxis({
      formatter: (params: any) => {
        let s = params[0].axisValueLabel
        params.forEach((p: any) => { s += `<br/>${p.marker} ${p.seriesName}: ${p.value}%` })
        return s
      },
    }),
    legend: dashboardLegend(),
    grid: { left: 36, right: 12, top: 12, bottom: 36 },
    xAxis: chartCategoryAxis({ data: times, axisLabel: { fontSize: 10, interval: 'auto', color: chartAxisColor.value } }),
    yAxis: chartValueAxis({ max: 100, axisLabel: { fontSize: 10, formatter: '{value}%', color: chartAxisColor.value } }),
    series: [
      { name: 'CPU', type: 'line', data: cpuData, smooth: true, symbol: 'none', lineStyle: { width: 1.5 }, itemStyle: { color: BRAND_COLORS.indigo }, areaStyle: { color: 'rgba(99,102,241,0.1)' } },
      { name: 'MEM', type: 'line', data: memData, smooth: true, symbol: 'none', lineStyle: { width: 1.5 }, itemStyle: { color: SEMANTIC_COLORS.success }, areaStyle: { color: 'rgba(34,197,94,0.1)' } },
    ],
  }
})

async function fetchStatus(silent = false) {
  try {
    status.value = await systemApi.getStatus()
    uptime.value = status.value?.uptime ?? 0
  } catch (e: any) {
    if (!silent) {
      msg.warning(extractError(e, t('dashboard.fetchStatusFailed')))
    }
  }
}

async function fetchProtocols() {
  try {
    const data = await driverApi.list()
    const drivers = data?.drivers || []
    supportedProtocols.value = drivers.map((d: any) => d.name || d.protocols?.[0] || 'unknown')
    if (supportedProtocols.value.length === 0) {
      const protoData = await driverApi.protocols()
      supportedProtocols.value = protoData?.protocols || []
    }
  } catch (e: any) {
    supportedProtocols.value = []
  }
}

async function fetchDevices() {
  // FIXED-General: 仪表盘仅用于统计概览（在线数、协议分布），无需拉取完整设备对象（含 points/config 大字段）
  // 将 size 从 500 降为 200，减少网络与内存压力；超过 200 台时统计可能略有截断，但 KPI 趋势仍准确
  try {
    const data = await deviceApi.list({ page: 1, size: 200 })
    devices.value = data?.data ?? []
  } catch (e: any) { devices.value = [] }
}

async function fetchAlarms() {
  // FIXED-General: 告警趋势图仅需 fired_at 和 severity 做聚合，无需拉取完整告警对象
  // 将 size 从 500 降为 200，减少请求体积
  try {
    const data = await alarmApi.list({ page: 1, size: 200 })
    alarms.value = data?.data ?? []
  } catch (e: any) { alarms.value = [] }
}

async function fetchDashboardStats() {
  try {
    const stats = await dataApi.stats()
    if (stats) {
      todayDataPoints.value = stats.total_points_today ?? 0
      collectSuccessRate.value = stats.success_rate ?? 0
    }
  } catch (_e) { /* ignore */ }
}

async function fetchAiStats() {
  try {
    const data = await aiApi.getStats()
    aiStats.value = data || {}
    aiEnabled.value = true
  } catch (_e) {
    aiStats.value = {}
    aiEnabled.value = false
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

function onRealtimeMessage(_data: any) { updateResourceHistory() }
let _alarmTimer: ReturnType<typeof setTimeout> | null = null
let _deviceTimer: ReturnType<typeof setTimeout> | null = null
function onAlarmMessage(data: any) {
  if (data) {
    if (_alarmTimer) clearTimeout(_alarmTimer)
    _alarmTimer = setTimeout(() => { fetchAlarms(); fetchStatus(); _alarmTimer = null }, 500)
  }
}
function onDeviceMessage(data: any) {
  if (data?.device_id) {
    if (_deviceTimer) clearTimeout(_deviceTimer)
    _deviceTimer = setTimeout(() => { fetchDevices(); fetchStatus(); _deviceTimer = null }, 500)
  }
}

function onWsStatusChange(wsStatus: string) {
  wsConnected.value = wsStatus === 'connected'
  wsReconnecting.value = wsStatus === 'reconnecting'
}

// [AUDIT-FIX] 严重级-组件卸载后异步响应仍更新状态，添加 isMounted 守卫
let isMounted = true

onMounted(async () => {
  await Promise.allSettled([fetchStatus(), fetchDevices(), fetchAlarms(), fetchProtocols(), fetchDashboardStats(), fetchAiStats(), fetchDeviceHealth()])
  if (!isMounted) return
  pageLoading.value = false
  // 修复11: 根据自动刷新开关决定是否启动定时轮询
  startAutoRefreshTimer()
  uptimeTimer = window.setInterval(() => { uptime.value++ }, 1000)
  ws.connect('realtime', onRealtimeMessage)
  ws.connect('alarm', onAlarmMessage)
  ws.connect('device', onDeviceMessage)
  ws.onStatus('realtime', onWsStatusChange)
  ws.onStatus('alarm', onWsStatusChange)
  ws.onStatus('device', onWsStatusChange)
})
onUnmounted(() => {
  isMounted = false
  if (timer) clearInterval(timer)
  if (uptimeTimer) clearInterval(uptimeTimer)
  // FIXED: 清理 WS 消息防抖定时器，避免组件卸载后回调执行访问已销毁的响应式状态
  if (_alarmTimer) clearTimeout(_alarmTimer)
  if (_deviceTimer) clearTimeout(_deviceTimer)
  ws.disconnect('realtime', onRealtimeMessage)
  ws.disconnect('alarm', onAlarmMessage)
  ws.disconnect('device', onDeviceMessage)
  // FIXED: 注销 WS 状态回调，避免 statusHandlers Set 残留导致内存泄漏
  ws.offStatus('realtime', onWsStatusChange)
  ws.offStatus('alarm', onWsStatusChange)
  ws.offStatus('device', onWsStatusChange)
})

// 页面可见性变化：隐藏时暂停轮询，恢复可见时立即刷新并恢复轮询
watch(isVisible, (visible) => {
  if (visible) {
    // 页面恢复可见，立即刷新并恢复轮询
    fetchStatus(true)
    fetchDashboardStats()
    fetchAiStats()
    fetchDeviceHealth()
    if (!timer) {
      startAutoRefreshTimer()
    }
  } else {
    // 页面隐藏，暂停轮询
    stopAutoRefreshTimer()
  }
})
</script>

<style scoped>
.dash-root {
  display: flex;
  flex-direction: column;
  margin: -20px;
  padding: 0;
  background: var(--n-body-color, #f5f5f5);
}

/* ── 顶部状态栏 ── */
.dash-topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 20px;
  border-bottom: 1px solid var(--n-border-color, rgba(0,0,0,0.06));
  background: var(--n-card-color, #fff);
  flex-shrink: 0;
}

.topbar-left {
  display: flex;
  align-items: center;
  gap: 16px;
}

.topbar-brand {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--n-text-color-1);
}

.topbar-title {
  font-size: 18px;
  font-weight: 700;
  letter-spacing: -0.02em;
}

.topbar-sub {
  font-size: 12px;
  opacity: 0.45;
  padding-left: 16px;
  border-left: 1px solid var(--n-border-color, rgba(0,0,0,0.08));
}

.topbar-right {
  display: flex;
  align-items: center;
  gap: 12px;
}

.topbar-uptime {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
}

.uptime-label {
  opacity: 0.5;
}

.uptime-val {
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}

/* ── KPI 指标条 ── */
.kpi-strip {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 12px;
  padding: 12px 16px;
  flex-shrink: 0;
}

.kpi-cell {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 16px;
  background: var(--n-card-color, #fff);
  border-radius: 8px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.06);
  position: relative;
}

.kpi-cell-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  border-radius: 8px;
  background: rgba(99, 102, 241, 0.08);
  color: #6366f1;
  flex-shrink: 0;
}

.kpi-cell-icon.icon-danger {
  background: rgba(245, 108, 108, 0.08);
  color: #f56c6c;
}

.kpi-cell-icon.icon-ok {
  background: rgba(34, 197, 94, 0.08);
  color: #22c55e;
}

.kpi-cell-icon.icon-muted {
  background: rgba(148, 163, 184, 0.08);
  color: #94a3b8;
}

.kpi-cell-icon.icon-ai {
  background: linear-gradient(135deg, rgba(99,102,241,0.1), rgba(139,92,246,0.1));
  color: #8b5cf6;
}

.kpi-cell-icon.icon-rules {
  background: rgba(6, 182, 212, 0.08);
  color: #06b6d4;
}

.kpi-cell-body {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
}

.kpi-cell-label {
  font-size: 11px;
  opacity: 0.5;
  font-weight: 500;
  white-space: nowrap;
}

.kpi-cell-value {
  font-size: 22px;
  font-weight: 800;
  letter-spacing: -0.03em;
  line-height: 1.1;
}

.kpi-cell-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  opacity: 0.55;
  margin-left: auto;
  white-space: nowrap;
}

/* 修复10: 今日数据点副文本样式 */
.kpi-cell-sub {
  font-size: 10px;
  opacity: 0.5;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.dot {
  display: inline-block;
  width: 5px;
  height: 5px;
  border-radius: 50%;
  flex-shrink: 0;
}

.dot-on { background: #22c55e; box-shadow: 0 0 3px rgba(34,197,94,0.4); }
.dot-off { background: #64748b; }
.dot-warn { background: #e6a23c; box-shadow: 0 0 3px rgba(230,162,60,0.4); }

.val-danger { color: #f56c6c; }

/* ── 主网格 ── */
.dash-main {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 12px;
  padding: 0 16px 16px;
}

/* ── 面板通用 ── */
.panel {
  background: var(--n-card-color, #fff);
  display: flex;
  flex-direction: column;
  border-radius: 8px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.06);
  overflow: hidden;
}

.panel-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--n-border-color, rgba(0,0,0,0.04));
  flex-shrink: 0;
}

.panel-title {
  font-size: 12px;
  font-weight: 600;
  letter-spacing: -0.01em;
}

.panel-badges {
  display: flex;
  gap: 6px;
  margin-left: auto;
}

.badge {
  font-size: 10px;
  padding: 2px 6px;
  border-radius: 4px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.badge-cpu {
  background: rgba(99,102,241,0.08);
  color: #6366f1;
}

.badge-mem {
  background: rgba(34,197,94,0.08);
  color: #22c55e;
}

.badge-danger {
  background: rgba(245,108,108,0.1);
  color: #f56c6c;
}

.panel-body {
  flex: 1;
  padding: 6px 10px 10px;
  min-height: 0;
}

/* ── 面板排列顺序与最小高度 ── */
.panel-resource { order: 1; min-height: 340px; }
.panel-alarm { order: 2; min-height: 340px; }
.panel-health { order: 3; min-height: 300px; }
.panel-gauges { order: 4; min-height: 240px; }
.panel-devices { order: 5; min-height: 260px; }
.panel-protocols { order: 6; min-height: 200px; }
.panel-proto { order: 7; min-height: 320px; }
.panel-ai { order: 8; min-height: 200px; }
.panel-sync { order: 9; min-height: 200px; }

/* ── 仪表盘 ── */
.gauge-body {
  display: flex;
  justify-content: space-around;
  align-items: center;
  padding-top: 8px;
}

.gauge-item {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
}

.gauge-val {
  font-size: 12px;
  font-weight: 700;
}

.gauge-label {
  font-size: 10px;
  opacity: 0.5;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}

.gauge-detail {
  text-align: center;
  font-size: 11px;
  opacity: 0.45;
  padding: 0 14px 10px;
}

/* ── 设备状态 ── */
.device-body {
  display: flex;
  align-items: center;
  gap: 8px;
}

.device-chart-wrap {
  width: 120px;
  height: 120px;
  flex-shrink: 0;
}

.device-legend {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.legend-row {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
}

.legend-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}

.legend-label {
  opacity: 0.6;
}

.legend-val {
  margin-left: auto;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}

.legend-divider {
  height: 1px;
  background: var(--n-border-color, rgba(0,0,0,0.06));
  margin: 2px 0;
}

.legend-total .legend-label {
  opacity: 1;
  font-weight: 600;
}

.legend-total .legend-val {
  font-size: 16px;
}

/* ── AI 统计 ── */
.ai-badge-sm {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  border-radius: 5px;
  background: linear-gradient(135deg, rgba(99,102,241,0.95), rgba(139,92,246,0.95));
  color: #fff;
  font-size: 8px;
  font-weight: 800;
  letter-spacing: 0.5px;
}

.ai-body {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  padding: 10px 14px;
}

.ai-stat {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.ai-stat-label {
  font-size: 11px;
  opacity: 0.5;
}

.ai-stat-val {
  font-size: 18px;
  font-weight: 800;
  letter-spacing: -0.02em;
}

.ai-stat-val small {
  font-size: 11px;
  font-weight: 500;
  opacity: 0.5;
}

/* ── 设备健康 ── */
.health-summary {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-left: auto;
  font-size: 11px;
  color: #22c55e;
  font-weight: 500;
}

.health-warn {
  color: #e6a23c;
}

.health-body {
  padding: 8px 14px;
  overflow-y: auto;
}

.health-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.health-item {
  display: flex;
  align-items: center;
  gap: 8px;
}

.health-name {
  font-size: 12px;
  min-width: 60px;
  max-width: 100px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.health-score {
  font-size: 12px;
  font-weight: 600;
  min-width: 28px;
  text-align: right;
}

.health-all-ok {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 24px 0;
  font-size: 13px;
  color: #22c55e;
  font-weight: 500;
}

.health-ok-icon {
  opacity: 1;
}

/* ── 协议支持 ── */
.proto-body {
  padding: 10px 14px;
  overflow-y: auto;
}

.proto-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.proto-tag {
  font-size: 11px;
  padding: 3px 8px;
  border-radius: 5px;
  background: rgba(99, 102, 241, 0.06);
  border: 1px solid rgba(99, 102, 241, 0.1);
  font-weight: 500;
}

/* ── 云同步面板 ── */
.panel-sync :deep(.cloud-sync-card) {
  box-shadow: none;
  border-radius: 0;
  background: transparent;
  height: 100%;
}

.panel-sync :deep(.cloud-sync-card::before) {
  display: none;
}

/* ── 响应式 ── */
@media (max-width: 1200px) {
  .kpi-strip { grid-template-columns: repeat(3, 1fr); }
  .dash-main { grid-template-columns: 1fr 1fr; }
}

@media (max-width: 768px) {
  .kpi-strip { grid-template-columns: repeat(2, 1fr); }
  .dash-main { grid-template-columns: 1fr; }
  .kpi-cell-value { font-size: 18px; }
  .topbar-sub { display: none; }
}
</style>
