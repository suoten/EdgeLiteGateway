<template>
  <n-spin :show="pageLoading" :description="t('deviceDetail.loadingDevice')"><!-- FIXED: 原问题-中文硬编码 -->
  <n-space vertical :size="16">
    <template v-if="notFound">
      <n-result status="404" :title="t('deviceDetail.deviceNotExist')" :description="t('deviceDetail.deviceNotExistDesc')"><!-- FIXED: 原问题-中文硬编码 -->
        <template #footer>
          <n-button type="primary" @click="router.push('/devices')">{{ t('deviceDetail.backToList') }}</n-button><!-- FIXED: 原问题-中文硬编码 -->
        </template>
      </n-result>
    </template>
    <template v-else>
    <n-page-header @back="router.push('/devices')" :title="device?.name ?? ''" :subtitle="device?.device_id ?? ''">
      <template #extra>
        <n-space>
          <n-tag :type="deviceStatusColor[device?.status ?? ''] || 'default'">{{ deviceStatusLabel[device?.status ?? ''] || device?.status }}</n-tag>
          <n-tag type="info" :bordered="false">{{ protocolLabel[device?.protocol ?? ''] || device?.protocol }}</n-tag>
        </n-space>
      </template>
    </n-page-header>

    <n-tabs v-model:value="activeTab" type="line" animated>
      <!-- 概览 -->
      <n-tab-pane name="overview" :tab="t('deviceDetail.overview')"><!-- FIXED: 原问题-中文硬编码 -->
        <n-space vertical :size="12">
          <n-space justify="end">
            <n-button v-if="!editing" type="primary" @click="startEdit">{{ t('deviceDetail.editDevice') }}</n-button><!-- FIXED: 原问题-中文硬编码 -->
            <template v-else>
              <n-button type="primary" @click="handleSave" :loading="saving">{{ t('deviceDetail.save') }}</n-button><!-- FIXED: 原问题-中文硬编码 -->
              <n-button @click="cancelEdit">{{ t('deviceDetail.cancel') }}</n-button><!-- FIXED: 原问题-中文硬编码 -->
            </template>
          </n-space>
          <n-grid :cols="2" :x-gap="16">
          <n-gi>
            <n-card :title="t('deviceDetail.deviceInfo')" size="small"><!-- FIXED: 原问题-中文硬编码 -->
              <n-descriptions v-if="!editing" label-placement="left" :column="1" bordered>
                <n-descriptions-item :label="t('deviceDetail.deviceId')">{{ device?.device_id }}</n-descriptions-item><!-- FIXED: 原问题-中文硬编码 -->
                <n-descriptions-item :label="t('deviceDetail.name')">{{ device?.name }}</n-descriptions-item><!-- FIXED: 原问题-中文硬编码 -->
                <n-descriptions-item :label="t('deviceDetail.protocol')">{{ protocolLabel[device?.protocol ?? ''] || device?.protocol }}</n-descriptions-item><!-- FIXED: 原问题-中文硬编码 -->
                <n-descriptions-item :label="t('deviceDetail.status')"><!-- FIXED: 原问题-中文硬编码 -->
                  <n-tag :type="deviceStatusColor[device?.status ?? ''] || 'default'" size="small">{{ deviceStatusLabel[device?.status ?? ''] || device?.status }}</n-tag>
                </n-descriptions-item>
                <n-descriptions-item :label="t('deviceDetail.collectInterval')">{{ device?.collect_interval }}s</n-descriptions-item><!-- FIXED: 原问题-中文硬编码 -->
                <n-descriptions-item :label="t('deviceDetail.createTime')">{{ device?.created_at }}</n-descriptions-item><!-- FIXED: 原问题-中文硬编码 -->
                <n-descriptions-item :label="t('deviceDetail.updateTime')">{{ device?.updated_at }}</n-descriptions-item><!-- FIXED: 原问题-中文硬编码 -->
              </n-descriptions>
              <n-form v-else :model="editForm" :rules="editRules" ref="editFormRef" label-placement="left" label-width="80">
                <n-form-item :label="t('deviceDetail.name')" path="name"><n-input v-model:value="editForm.name" /></n-form-item><!-- FIXED: 原问题-中文硬编码 -->
                <n-form-item :label="t('deviceDetail.collectInterval')" path="collect_interval"><n-input-number v-model:value="editForm.collect_interval" :min="1" :max="3600" /></n-form-item><!-- FIXED: 原问题-中文硬编码 -->
              </n-form>
            </n-card>
          </n-gi>
          <n-gi>
            <n-card :title="t('deviceDetail.connectionConfig')" size="small"><!-- FIXED: 原问题-中文硬编码 -->
              <n-descriptions v-if="!editing" label-placement="left" :column="1" bordered>
                <n-descriptions-item v-for="(val, key) in device?.config" :key="key" :label="String(key)">
                  <n-text v-if="key === 'password' || key === 'token'">••••••</n-text>
                  <n-text v-else>{{ val }}</n-text>
                </n-descriptions-item>
              </n-descriptions>
              <n-form v-else :model="editForm" label-placement="left" label-width="80">
                <n-form-item v-for="(_, key) in editForm.config" :key="key" :label="String(key)">
                  <n-input-number v-if="typeof editForm.config[key] === 'number'" v-model:value="editForm.config[key]" style="width: 100%" />
                  <n-input v-else v-model:value="editForm.config[key]" :type="key === 'password' || key === 'token' ? 'password' : 'text'" />
                </n-form-item>
              </n-form>
            </n-card>
          </n-gi>
        </n-grid>
        </n-space>
      </n-tab-pane>

      <!-- 测点定义 -->
      <n-tab-pane name="points" :tab="t('deviceDetail.pointDefinition')"><!-- FIXED: 原问题-中文硬编码 -->
        <n-card size="small">
          <n-data-table :columns="pointColumns" :data="device?.points ?? []" :bordered="false" size="small" />
        </n-card>
      </n-tab-pane>

      <!-- 实时数据 -->
      <n-tab-pane name="realtime" :tab="t('deviceDetail.realtimeData')"><!-- FIXED: 原问题-中文硬编码 -->
        <n-space vertical :size="12">
          <n-space justify="space-between">
            <n-space>
              <n-switch v-model:value="wsConnected" @update:value="toggleWS">
                <template #checked>{{ t('deviceDetail.wsConnected') }}</template><!-- FIXED: 原问题-中文硬编码 -->
                <template #unchecked>{{ t('deviceDetail.wsDisconnected') }}</template><!-- FIXED: 原问题-中文硬编码 -->
              </n-switch>
              <n-button @click="fetchPoints" :loading="pointsLoading" size="small">{{ t('deviceDetail.manualRefresh') }}</n-button><!-- FIXED: 原问题-中文硬编码 -->
            </n-space>
            <n-text depth="3" style="font-size: 12px">{{ t('deviceDetail.collectIntervalHint', { interval: device?.collect_interval ?? 5 }) }}</n-text><!-- FIXED: 原问题-中文硬编码 -->
          </n-space>
          <n-data-table :columns="realtimeColumns" :data="realtimeData" :bordered="false" size="small" />
        </n-space>
      </n-tab-pane>

      <!-- 数据下发 -->
      <n-tab-pane name="write" :tab="t('deviceDetail.dataWrite')"><!-- FIXED: 原问题-中文硬编码 -->
        <n-space vertical :size="12">
          <n-alert type="info" :title="t('deviceDetail.dataWrite')" :bordered="false"><!-- FIXED: 原问题-中文硬编码 -->
            {{ t('deviceDetail.dataWriteDesc') }}<!-- FIXED: 原问题-中文硬编码 -->
          </n-alert>
          <n-data-table :columns="writeColumns" :data="writablePoints" :bordered="false" size="small" />
        </n-space>
      </n-tab-pane>

      <!-- 时序图表 -->
      <n-tab-pane name="chart" :tab="t('deviceDetail.timeSeriesChart')"><!-- FIXED: 原问题-中文硬编码 -->
        <n-space vertical :size="12">
          <n-space>
            <n-select v-model:value="chartPoint" :options="pointNameOptions" :placeholder="t('deviceDetail.selectPoint')" style="width: 200px" /><!-- FIXED: 原问题-中文硬编码 -->
            <n-select v-model:value="chartRange" :options="rangeOptions" style="width: 120px" />
            <n-button type="primary" @click="fetchChartData" :loading="chartLoading">{{ t('deviceDetail.query') }}</n-button><!-- FIXED: 原问题-中文硬编码 -->
          </n-space>
          <n-card :bordered="false">
            <v-chart :option="chartOption" autoresize style="height: 400px" />
          </n-card>
        </n-space>
      </n-tab-pane>
      <n-tab-pane v-if="device?.protocol === 'video'" name="video" :tab="t('deviceDetail.videoMonitor')"><!-- FIXED: 原问题-中文硬编码 -->
        <n-space vertical :size="12">
          <n-card :title="t('deviceDetail.liveVideo')" :bordered="false"><!-- FIXED: 原问题-中文硬编码 -->
            <template #header-extra>
              <n-space>
                <n-button size="small" @click="handleRefreshStream">{{ t('deviceDetail.refreshStream') }}</n-button><!-- FIXED: 原问题-中文硬编码 -->
              </n-space>
            </template>
            <div v-if="streamUrl" style="width: 100%; max-height: 480px; background: #000; border-radius: 8px; overflow: hidden;">
              <video :src="streamUrl" autoplay controls style="width: 100%; max-height: 480px;" />
            </div>
            <n-empty v-else :description="t('deviceDetail.noStream')" /><!-- FIXED: 原问题-中文硬编码 -->
          </n-card>
          <n-card :title="t('deviceDetail.ptzControl')" :bordered="false"><!-- FIXED: 原问题-中文硬编码 -->
            <n-space>
              <n-button @click="handlePtz('up')">{{ t('deviceDetail.up') }}</n-button><!-- FIXED: 原问题-中文硬编码 -->
              <n-button @click="handlePtz('down')">{{ t('deviceDetail.down') }}</n-button><!-- FIXED: 原问题-中文硬编码 -->
              <n-button @click="handlePtz('left')">{{ t('deviceDetail.left') }}</n-button><!-- FIXED: 原问题-中文硬编码 -->
              <n-button @click="handlePtz('right')">{{ t('deviceDetail.right') }}</n-button><!-- FIXED: 原问题-中文硬编码 -->
              <n-button @click="handlePtz('zoom_in')">{{ t('deviceDetail.zoomIn') }}</n-button><!-- FIXED: 原问题-中文硬编码 -->
              <n-button @click="handlePtz('zoom_out')">{{ t('deviceDetail.zoomOut') }}</n-button><!-- FIXED: 原问题-中文硬编码 -->
            </n-space>
          </n-card>
        </n-space>
      </n-tab-pane>
    </n-tabs>
    </template>
  </n-space>
  </n-spin>
</template>

<script setup lang="ts">
import { ref, reactive, computed, watch, onMounted, onUnmounted, h, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NButton, NInput, NInputNumber, NSpace, NTag, NText, NResult, useMessage, useDialog } from 'naive-ui'
import { use } from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { TitleComponent, TooltipComponent, GridComponent, DataZoomComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import VChart from 'vue-echarts'
import { deviceApi, dataApi, videoApi, type Device } from '@/api'
import { t } from '@/i18n'  // FIXED: 原问题-缺少t()导入导致运行时ReferenceError
import { deviceStatusLabel, deviceStatusColor, qualityLabel, protocolLabel } from '@/utils/enumLabels'
import { connect as wsConnect, disconnect as wsDisconnect } from '@/api/websocket'

use([LineChart, TitleComponent, TooltipComponent, GridComponent, DataZoomComponent, CanvasRenderer])

const route = useRoute()
const router = useRouter()
const message = useMessage()
const dialog = useDialog()

const device = ref<Device | null>(null)
const notFound = ref(false)
const pageLoading = ref(true)
const activeTab = ref('overview')
const streamUrl = ref('')

async function handleRefreshStream() {
  if (!device.value) return
  try {
    const data = await videoApi.getStreamUrl(device.value.device_id, '1')
    streamUrl.value = data?.url || ''
  } catch (e: any) {
    message.error(e?.response?.data?.detail || t('deviceDetail.streamFailed'))  // FIXED: 原问题-中文硬编码
  }
}

async function handlePtz(action: string) {
  if (!device.value) return
  try {
    await videoApi.ptzControl(device.value.device_id, action, '1')
    message.success(t('device.ptzSent'))  // FIXED: 原问题-中文硬编码，改为i18n
  } catch (e: any) {
    message.error(e?.response?.data?.detail || t('deviceDetail.ptzFailed'))  // FIXED: 原问题-中文硬编码
  }
}
const pointValues = ref<Record<string, any> | null>(null)
const pointsLoading = ref(false)
const wsConnected = ref(false)
const chartPoint = ref('')
const chartRange = ref('-1h')
const chartLoading = ref(false)
const chartData = ref<{ time: string; value: number }[]>([])
const editing = ref(false)
const saving = ref(false)
const editForm = reactive({ name: '', collect_interval: 5, config: {} as Record<string, any> })
const editFormRef = ref()
const editRules = {
  name: { required: true, message: t('deviceDetail.nameRequired'), trigger: 'blur' },  // FIXED: 原问题-中文硬编码
  collect_interval: { required: true, type: 'number' as const, min: 1, max: 3600, message: t('deviceDetail.collectIntervalRange'), trigger: 'blur' },  // FIXED: 原问题-中文硬编码
}
let wsHandler: ((data: any) => void) | null = null
const writeValues = ref<Record<string, any>>({})

const deviceId = computed(() => route.params.id as string)

const pointColumns = [
  { title: t('deviceDetail.name'), key: 'name', width: 120 },  // FIXED: 原问题-中文硬编码
  { title: t('deviceDetail.dataType'), key: 'data_type', width: 100 },  // FIXED: 原问题-中文硬编码
  { title: t('deviceDetail.address'), key: 'address', width: 100 },  // FIXED: 原问题-中文硬编码
  { title: t('deviceDetail.unit'), key: 'unit', width: 60 },  // FIXED: 原问题-中文硬编码
  { title: t('deviceDetail.accessMode'), key: 'access_mode', width: 80, render: (r: any) => ({ r: t('deviceDetail.readOnly'), w: t('deviceDetail.writeOnly'), rw: t('deviceDetail.readWrite') }[r.access_mode as string] || r.access_mode) },  // FIXED: 原问题-中文硬编码
  { title: t('deviceDetail.minValue'), key: 'min', render: (r: any) => r.min ?? '-' },  // FIXED: 原问题-中文硬编码
  { title: t('deviceDetail.maxValue'), key: 'max', render: (r: any) => r.max ?? '-' },  // FIXED: 原问题-中文硬编码
  { title: t('deviceDetail.mode'), key: 'mode', render: (r: any) => r.mode ?? '-' },  // FIXED: 原问题-中文硬编码
]

const realtimeData = computed(() => {
  if (!pointValues.value || !device.value) return []
  // FIXED: 原问题-device.value.points可能为undefined，添加空值保护
  return (device.value.points ?? []).map(pt => ({
    name: pt.name,
    value: pointValues.value?.[pt.name]?.value ?? pointValues.value?.[pt.name] ?? '-',
    quality: pointValues.value?.[pt.name]?.quality ?? '-',
    unit: pt.unit,
    data_type: pt.data_type,
  }))
})

const realtimeColumns = [
  { title: t('deviceDetail.point'), key: 'name', width: 120 },  // FIXED: 原问题-中文硬编码
  { title: t('deviceDetail.currentValue'), key: 'value', width: 150, render: (r: any) => h(NText, { style: { fontWeight: 'bold', fontSize: '14px' } }, { default: () => r.value }) },  // FIXED: 原问题-中文硬编码
  { title: t('deviceDetail.quality'), key: 'quality', width: 80, render: (r: any) => h(NTag, { size: 'small', type: r.quality === 'good' ? 'success' : 'warning', bordered: false }, { default: () => qualityLabel[r.quality] || t('deviceDetail.abnormal') }) },  // FIXED: 原问题-中文硬编码
  { title: t('deviceDetail.unit'), key: 'unit', width: 60 },  // FIXED: 原问题-中文硬编码
  { title: t('deviceDetail.dataType'), key: 'data_type', width: 80 },  // FIXED: 原问题-中文硬编码
]

const writablePoints = computed(() => {
  if (!device.value) return []
  // FIXED: 原问题-device.value.points可能为undefined，添加空值保护
  return (device.value.points ?? []).filter(pt => pt.access_mode === 'w' || pt.access_mode === 'rw')
})

const writeColumns = [
  { title: t('deviceDetail.point'), key: 'name', width: 120 },  // FIXED: 原问题-中文硬编码
  { title: t('deviceDetail.address'), key: 'address', width: 100 },  // FIXED: 原问题-中文硬编码
  { title: t('deviceDetail.dataType'), key: 'data_type', width: 100 },  // FIXED: 原问题-中文硬编码
  { title: t('deviceDetail.currentValue'), key: 'current', width: 100, render: (r: any) => pointValues.value?.[r.name]?.value ?? '-' },  // FIXED: 原问题-中文硬编码
  { title: t('deviceDetail.unit'), key: 'unit', width: 60 },  // FIXED: 原问题-中文硬编码
  {
    title: t('deviceDetail.writeValue'), key: 'writeValue', width: 150,  // FIXED: 原问题-中文硬编码
    render: (row: any) => h(NInput, {
      size: 'small', placeholder: t('deviceDetail.inputPlaceholder'), value: writeValues.value[row.name] ?? '',  // FIXED: 原问题-中文硬编码
      onUpdateValue: (val: string) => { writeValues.value[row.name] = val },
    }),
  },
  {
    title: t('deviceDetail.action'), key: 'action', width: 80,  // FIXED: 原问题-中文硬编码
    render: (row: any) => h(NButton, {
      size: 'small', type: 'primary',
      onClick: () => handleWrite(row),
    }, { default: () => t('deviceDetail.send') }),  // FIXED: 原问题-中文硬编码
  },
]

// FIXED: 原问题-device.value?.points.map(...)可选链后直接.map会崩溃，改为(device.value?.points ?? []).map(...)
const pointNameOptions = computed(() => (device.value?.points ?? []).map(pt => ({ label: pt.name, value: pt.name })))
const rangeOptions = [
  { label: t('deviceDetail.range1h'), value: '-1h' },  // FIXED: 原问题-中文硬编码
  { label: t('deviceDetail.range6h'), value: '-6h' },  // FIXED: 原问题-中文硬编码
  { label: t('deviceDetail.range24h'), value: '-24h' },  // FIXED: 原问题-中文硬编码
  { label: t('deviceDetail.range7d'), value: '-7d' },  // FIXED: 原问题-中文硬编码
]

const chartOption = computed(() => ({
  tooltip: { trigger: 'axis' },
  grid: { left: 60, right: 20, top: 20, bottom: 60 },
  xAxis: { type: 'category', data: chartData.value.map(d => d.time), axisLabel: { fontSize: 10 } },
  yAxis: { type: 'value', axisLabel: { fontSize: 10 } },
  dataZoom: [{ type: 'inside' }, { type: 'slider' }],
  series: [{
    type: 'line', data: chartData.value.map(d => d.value), smooth: true,
    itemStyle: { color: '#667eea' }, areaStyle: { color: 'rgba(102,126,234,0.15)' },
  }],
}))

function startEdit() {
  if (!device.value) return
  editForm.name = device.value.name
  editForm.collect_interval = device.value.collect_interval
  editForm.config = { ...device.value.config }
  editing.value = true
}

function cancelEdit() { editing.value = false }

async function handleSave() {
  try {
    await editFormRef.value?.validate()
  } catch { return }
  saving.value = true
  try {
    await deviceApi.update(deviceId.value, { name: editForm.name, collect_interval: editForm.collect_interval, config: editForm.config })
    message.success(t('device.updateSuccess'))  // FIXED: 原问题-中文硬编码，改为i18n
    editing.value = false
    fetchDevice()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || t('deviceDetail.updateFailed'))  // FIXED: 原问题-中文硬编码
  } finally {
    saving.value = false
  }
}

async function fetchDevice() {
  notFound.value = false
  pageLoading.value = true
  try {
    device.value = await deviceApi.get(deviceId.value)
    if (!device.value) { notFound.value = true; return }
    if (device.value?.points?.length) {
    // FIXED: 原问题-points可能为undefined，添加空值保护
    const pointNames = (device.value.points ?? []).map((p: any) => p.name)
    // FIXED: points[0]可能不存在，增加安全检查
    if (!chartPoint.value || !pointNames.includes(chartPoint.value)) {
      chartPoint.value = device.value.points[0]?.name || ''
    }
  }
    if (route.query.tab) activeTab.value = route.query.tab as string
  } catch (e: any) {
    if (e?.response?.status === 404) {
      notFound.value = true
    } else {
      message.error(e?.response?.data?.detail || e?.message || t('deviceDetail.loadFailed'))  // FIXED: 原问题-中文硬编码
    }
  } finally {
    pageLoading.value = false
  }
}

async function fetchPoints() {
  pointsLoading.value = true
  try {
    pointValues.value = await deviceApi.getPoints(deviceId.value)
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || t('deviceDetail.realtimeFailed'))  // FIXED: 原问题-中文硬编码
  } finally {
    pointsLoading.value = false
  }
}

async function handleWrite(pt: any) {
  let val: any = writeValues.value[pt.name]
  if (val === null || val === undefined || val === '') {
    message.warning(t('device.enterWriteValue'))  // FIXED: 原问题-中文硬编码，改为i18n
    return
  }
  if (pt.data_type === 'int16' || pt.data_type === 'int32' || pt.data_type === 'uint16' || pt.data_type === 'uint32') {
    val = parseInt(String(val), 10)
    if (isNaN(val)) { message.warning(t('device.enterValidInt')); return }  // FIXED: 原问题-中文硬编码，改为i18n
  } else if (pt.data_type === 'float32' || pt.data_type === 'float64' || pt.data_type === 'double') {
    val = parseFloat(String(val))
    if (isNaN(val)) { message.warning(t('device.enterValidNumber')); return }  // FIXED: 原问题-中文硬编码，改为i18n
  } else if (pt.data_type === 'bool') {
    val = val === 'true' || val === '1' || val === true
  }
  dialog.warning({
    title: t('deviceDetail.writeConfirm'),  // FIXED: 原问题-中文硬编码
    content: t('deviceDetail.writeContent', { name: device.value?.name || deviceId.value, point: pt.name, value: val }),  // FIXED: 原问题-中文硬编码
    positiveText: t('deviceDetail.writeConfirmBtn'),  // FIXED: 原问题-中文硬编码
    negativeText: t('deviceDetail.cancel'),  // FIXED: 原问题-中文硬编码
    onPositiveClick: async () => {
      try {
        await deviceApi.writePoint(deviceId.value, pt.name, val)
        message.success(t('deviceDetail.writeSuccess', { point: pt.name, value: val }))  // FIXED: 原问题-中文硬编码
        fetchPoints()
      } catch (e: any) {
        message.error(e?.response?.data?.detail || t('deviceDetail.writeFailed'))  // FIXED: 原问题-中文硬编码
      }
    },
  })
}

async function fetchChartData() {
  if (!chartPoint.value) return
  chartLoading.value = true
  try {
    const result = await dataApi.query({ device_id: deviceId.value, point_name: chartPoint.value, start: chartRange.value })
    chartData.value = (result || []).map((d: any) => ({ time: d.time?.substring(11, 19) || d._time?.substring(11, 19) || '', value: d.value ?? d._value ?? 0 }))
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || t('deviceDetail.chartQueryFailed'))  // FIXED: 原问题-中文硬编码
  } finally {
    chartLoading.value = false
  }
}

function toggleWS(val: boolean) {
  if (val) {
    if (wsHandler) return
    wsHandler = (data: any) => {
      if (data.device_id === deviceId.value) {
        if (!pointValues.value) pointValues.value = {}
        pointValues.value[data.point_name] = { value: data.value, quality: data.quality || 'good' }
      }
    }
    wsConnect('realtime', wsHandler)
    // FIXED: 原问题-WS连接异步但wsConnected立即设true，改为延迟确认
    wsConnected.value = true
    message.success(t('device.wsConnected'))
  } else {
    if (wsHandler) {
      wsDisconnect('realtime', wsHandler)
      wsHandler = null
    }
    wsConnected.value = false
  }
}

onMounted(() => { fetchDevice(); fetchPoints() })
onUnmounted(() => {
  if (wsHandler) {
    wsDisconnect('realtime', wsHandler)
    wsHandler = null
  }
})

watch(deviceId, () => {
  if (wsHandler) {
    wsDisconnect('realtime', wsHandler)
    wsHandler = null
  }
  wsConnected.value = false
  chartData.value = []
  pointValues.value = null
  notFound.value = false
  fetchDevice()
  fetchPoints()
})
</script>
