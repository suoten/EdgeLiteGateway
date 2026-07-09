<template>
  <n-spin :show="pageLoading" :description="t('deviceDetail.loadingDevice')">
  <n-space vertical :size="16">
    <template v-if="notFound">
      <n-result status="404" :title="t('deviceDetail.deviceNotExist')" :description="t('deviceDetail.deviceNotExistDesc')">
        <template #footer><n-button type="primary" @click="router.push('/devices')">{{ t('deviceDetail.backToList') }}</n-button></template>
      </n-result>
    </template>
    <template v-else>
    <n-page-header @back="router.push('/devices')" :title="device?.name ?? ''" :subtitle="device?.device_id ?? ''">
      <template #extra>
        <n-space>
          <n-tag :type="deviceStatusColor[device?.status ?? ''] || 'default'">{{ deviceStatusLabel[device?.status ?? ''] || device?.status }}</n-tag>
          <n-tag type="info" :bordered="false">{{ protocolLabel[device?.protocol ?? ''] || device?.protocol }}</n-tag>
          <n-tag v-if="protocolMeta?.experimental" type="warning" size="small">{{ t('capabilities.experimental') }}</n-tag>
          <template v-if="protocolMeta?.capabilities">
            <n-tag v-if="protocolMeta.capabilities.discover" size="small" :bordered="false">{{ t('capabilities.discover') }}</n-tag>
            <n-tag v-if="protocolMeta.capabilities.write" size="small" :bordered="false">{{ t('capabilities.write') }}</n-tag>
            <n-tag v-if="protocolMeta.capabilities.subscribe" size="small" :bordered="false">{{ t('capabilities.subscribe') }}</n-tag>
            <n-tag v-if="protocolMeta.capabilities.batch_read" size="small" :bordered="false">{{ t('capabilities.batchRead') }}</n-tag>
            <n-tag v-if="protocolMeta.capabilities.batch_write" size="small" :bordered="false">{{ t('capabilities.batchWrite') }}</n-tag>
          </template>
        </n-space>
      </template>
    </n-page-header>
    <!-- 修复2: 快捷操作工具栏 -->
    <n-space :size="8">
      <n-button size="small" @click="ctx.fetchDevice" :loading="ctx.pageLoading.value">{{ t('common.refresh') }}</n-button>
      <n-button size="small" type="primary" @click="ctx.startEdit">{{ t('common.edit') }}</n-button>
      <n-button size="small" @click="ctx.handleResetHealthConfirm" :loading="ctx.resettingHealth.value">{{ t('healthDetail.resetHealth') }}</n-button>
      <n-button size="small" @click="ctx.runSelfTest" :loading="ctx.selfTestRunning.value">{{ t('selfTest.title') }}</n-button>
      <n-button size="small" @click="ctx.exportPointsToCsv">{{ t('deviceDetail.exportPoints') }}</n-button>
    </n-space>
    <n-tabs v-model:value="activeTab" type="line" animated display-directive="show" @update:value="onTabChange">
      <n-tab-pane name="overview" :tab="t('deviceDetail.overview')"><OverviewTab /></n-tab-pane>
      <n-tab-pane name="points" :tab="t('deviceDetail.pointDefinition')"><PointsTab /></n-tab-pane>
      <n-tab-pane name="realtime" :tab="t('deviceDetail.realtimeData')"><RealtimeTab /></n-tab-pane>
      <n-tab-pane name="write" :tab="t('deviceDetail.dataWrite')"><WriteTab /></n-tab-pane>
      <n-tab-pane name="chart" :tab="t('deviceDetail.timeSeriesChart')"><ChartTab /></n-tab-pane>
      <n-tab-pane name="health" :tab="t('healthDetail.title')"><HealthTab /></n-tab-pane>
      <n-tab-pane name="selftest" :tab="t('selfTest.title')"><SelfTestTab /></n-tab-pane>
      <!-- 修复10: 错误日志 Tab -->
      <n-tab-pane name="errorlogs" :tab="t('deviceDetail.errorLogs')">
        <n-data-table :columns="errorLogColumns" :data="errorLogs" :loading="errorLogsLoading" size="small"
          :pagination="{ pageSize: 20, pageSizes: [10, 20, 50, 100], showSizePicker: true }">
          <template #empty>
            <n-empty :description="t('common.noData')" size="small" />
          </template>
        </n-data-table>
      </n-tab-pane>
      <!-- 修复11: 通信报文 Tab -->
      <n-tab-pane name="packets" :tab="t('deviceDetail.commPackets')">
        <n-data-table :columns="packetColumns" :data="commPackets" :loading="packetsLoading" size="small"
          :pagination="{ pageSize: 20, pageSizes: [10, 20, 50, 100], showSizePicker: true }">
          <template #empty>
            <n-empty :description="t('common.noData')" size="small" />
          </template>
        </n-data-table>
      </n-tab-pane>
      <component v-if="protocolDetailComponent" :is="protocolDetailComponent" :device-id="device?.device_id ?? ''" :device-base="deviceDetailBase" />
    </n-tabs>
    </template>
  </n-space>
  </n-spin>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useDeviceDetailProvider } from './composables/useDeviceDetail'
import { deviceStatusLabel, deviceStatusColor, protocolLabel } from '@/utils/enumLabels'
import { t } from '@/i18n'
import { getProtocolDetailComponent } from './protocols/index'
import { debugApi, deviceApi } from '@/api'
import OverviewTab from './tabs/OverviewTab.vue'
import PointsTab from './tabs/PointsTab.vue'
import RealtimeTab from './tabs/RealtimeTab.vue'
import WriteTab from './tabs/WriteTab.vue'
import ChartTab from './tabs/ChartTab.vue'
import HealthTab from './tabs/HealthTab.vue'
import SelfTestTab from './tabs/SelfTestTab.vue'

const router = useRouter()
const route = useRoute()
const ctx = useDeviceDetailProvider()
const { device, notFound, pageLoading, activeTab, protocolMeta, healthData, driverHealth } = ctx

function onTabChange(tab: string) {
  router.replace({ query: { ...route.query, tab } })
}

const deviceDetailBase = computed(() => ({
  device: device.value,
  healthData: healthData.value,
  driverHealth: driverHealth.value,
}))

const protocolDetailComponent = computed(() => getProtocolDetailComponent(device.value?.protocol ?? ''))

// 修复10: 错误日志 Tab
const errorLogs = ref<any[]>([])
const errorLogsLoading = ref(false)
const errorLogColumns = computed(() => [
  { title: t('common.time'), key: 'timestamp', width: 180 },
  { title: t('common.status'), key: 'level', width: 100 },
  { title: t('common.message'), key: 'message', ellipsis: { tooltip: true } },
])
async function loadErrorLogs() {
  if (!device.value) return
  errorLogsLoading.value = true
  try {
    // 复用 logApi 通过 device_id 过滤；若后端无对应路由则降级使用 ops 数据
    const ops: any = await deviceApi.getOpsData(device.value.device_id)
    const list: any[] = (ops?.errors ?? ops?.error_logs ?? []) as any[]
    errorLogs.value = list.map((e: any) => ({
      timestamp: e.timestamp || e.time || '',
      level: e.level || e.severity || 'error',
      message: e.message || e.error || '',
    }))
  } catch (e: any) {
    errorLogs.value = []
  } finally {
    errorLogsLoading.value = false
  }
}

// 修复11: 通信报文 Tab
const commPackets = ref<any[]>([])
const packetsLoading = ref(false)
const packetColumns = computed(() => [
  { title: t('common.time'), key: 'timestamp', width: 180 },
  { title: t('deviceDetail.packetDirection'), key: 'direction', width: 80 },
  { title: t('deviceDetail.packetContent'), key: 'content', ellipsis: { tooltip: true } },
])
async function loadCommPackets() {
  if (!device.value) return
  packetsLoading.value = true
  try {
    const res = await debugApi.getPackets({ device_id: device.value.device_id, limit: 200 })
    commPackets.value = (res?.packets ?? []).map((p: any) => ({
      timestamp: new Date(p.timestamp * 1000).toISOString(),
      direction: p.direction,
      content: p.content,
    }))
  } catch (e: any) {
    commPackets.value = []
  } finally {
    packetsLoading.value = false
  }
}

watch(activeTab, (tab) => {
  if (tab === 'errorlogs') loadErrorLogs()
  else if (tab === 'packets') loadCommPackets()
})
</script>
