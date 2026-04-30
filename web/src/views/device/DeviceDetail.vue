<template>
  <n-space vertical :size="16">
    <template v-if="notFound">
      <n-result status="404" title="设备不存在" description="该设备可能已被删除或ID无效">
        <template #footer>
          <n-button type="primary" @click="router.push('/devices')">返回设备列表</n-button>
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
      <n-tab-pane name="overview" tab="概览">
        <n-space vertical :size="12">
          <n-space justify="end">
            <n-button v-if="!editing" type="primary" @click="startEdit">编辑设备</n-button>
            <template v-else>
              <n-button type="primary" @click="handleSave" :loading="saving">保存</n-button>
              <n-button @click="cancelEdit">取消</n-button>
            </template>
          </n-space>
          <n-grid :cols="2" :x-gap="16">
          <n-gi>
            <n-card title="设备信息" size="small">
              <n-descriptions v-if="!editing" label-placement="left" :column="1" bordered>
                <n-descriptions-item label="设备ID">{{ device?.device_id }}</n-descriptions-item>
                <n-descriptions-item label="名称">{{ device?.name }}</n-descriptions-item>
                <n-descriptions-item label="协议">{{ protocolLabel[device?.protocol ?? ''] || device?.protocol }}</n-descriptions-item>
                <n-descriptions-item label="状态">
                  <n-tag :type="deviceStatusColor[device?.status ?? ''] || 'default'" size="small">{{ deviceStatusLabel[device?.status ?? ''] || device?.status }}</n-tag>
                </n-descriptions-item>
                <n-descriptions-item label="采集间隔">{{ device?.collect_interval }}s</n-descriptions-item>
                <n-descriptions-item label="创建时间">{{ device?.created_at }}</n-descriptions-item>
                <n-descriptions-item label="更新时间">{{ device?.updated_at }}</n-descriptions-item>
              </n-descriptions>
              <n-form v-else :model="editForm" :rules="editRules" ref="editFormRef" label-placement="left" label-width="80">
                <n-form-item label="名称" path="name"><n-input v-model:value="editForm.name" /></n-form-item>
                <n-form-item label="采集间隔" path="collect_interval"><n-input-number v-model:value="editForm.collect_interval" :min="1" :max="3600" /></n-form-item>
              </n-form>
            </n-card>
          </n-gi>
          <n-gi>
            <n-card title="连接配置" size="small">
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
      <n-tab-pane name="points" tab="测点定义">
        <n-card size="small">
          <n-data-table :columns="pointColumns" :data="device?.points ?? []" :bordered="false" size="small" />
        </n-card>
      </n-tab-pane>

      <!-- 实时数据 -->
      <n-tab-pane name="realtime" tab="实时数据">
        <n-space vertical :size="12">
          <n-space justify="space-between">
            <n-space>
              <n-switch v-model:value="wsConnected" @update:value="toggleWS">
                <template #checked>WebSocket 已连接</template>
                <template #unchecked>WebSocket 未连接</template>
              </n-switch>
              <n-button @click="fetchPoints" :loading="pointsLoading" size="small">手动刷新</n-button>
            </n-space>
            <n-text depth="3" style="font-size: 12px">数据每 {{ device?.collect_interval ?? 5 }}s 采集一次</n-text>
          </n-space>
          <n-data-table :columns="realtimeColumns" :data="realtimeData" :bordered="false" size="small" />
        </n-space>
      </n-tab-pane>

      <!-- 数据下发 -->
      <n-tab-pane name="write" tab="数据下发">
        <n-space vertical :size="12">
          <n-alert type="info" title="数据下发" :bordered="false">
            向设备写入控制值。仅 access_mode 为 w 或 rw 的测点可写入。
          </n-alert>
          <n-data-table :columns="writeColumns" :data="writablePoints" :bordered="false" size="small" />
        </n-space>
      </n-tab-pane>

      <!-- 时序图表 -->
      <n-tab-pane name="chart" tab="时序图表">
        <n-space vertical :size="12">
          <n-space>
            <n-select v-model:value="chartPoint" :options="pointNameOptions" placeholder="选择测点" style="width: 200px" />
            <n-select v-model:value="chartRange" :options="rangeOptions" style="width: 120px" />
            <n-button type="primary" @click="fetchChartData" :loading="chartLoading">查询</n-button>
          </n-space>
          <n-card :bordered="false">
            <v-chart :option="chartOption" autoresize style="height: 400px" />
          </n-card>
        </n-space>
      </n-tab-pane>
      <n-tab-pane v-if="device?.protocol === 'video'" name="video" tab="视频监控">
        <n-space vertical :size="12">
          <n-card title="实时视频" :bordered="false">
            <template #header-extra>
              <n-space>
                <n-button size="small" @click="handleRefreshStream">刷新流</n-button>
              </n-space>
            </template>
            <div v-if="streamUrl" style="width: 100%; max-height: 480px; background: #000; border-radius: 8px; overflow: hidden;">
              <video :src="streamUrl" autoplay controls style="width: 100%; max-height: 480px;" />
            </div>
            <n-empty v-else description="暂无视频流" />
          </n-card>
          <n-card title="云台控制" :bordered="false">
            <n-space>
              <n-button @click="handlePtz('up')">上</n-button>
              <n-button @click="handlePtz('down')">下</n-button>
              <n-button @click="handlePtz('left')">左</n-button>
              <n-button @click="handlePtz('right')">右</n-button>
              <n-button @click="handlePtz('zoom_in')">放大</n-button>
              <n-button @click="handlePtz('zoom_out')">缩小</n-button>
            </n-space>
          </n-card>
        </n-space>
      </n-tab-pane>
    </n-tabs>
    </template>
  </n-space>
</template>

<script setup lang="ts">
import { ref, reactive, computed, watch, onMounted, onUnmounted, h, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NButton, NInput, NInputNumber, NSpace, NTag, NText, NResult, useMessage } from 'naive-ui'
import { use } from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { TitleComponent, TooltipComponent, GridComponent, DataZoomComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import VChart from 'vue-echarts'
import { deviceApi, dataApi, videoApi, type Device } from '@/api'
import { useAuthStore } from '@/stores/auth'
import { deviceStatusLabel, deviceStatusColor, qualityLabel } from '@/utils/enumLabels'

use([LineChart, TitleComponent, TooltipComponent, GridComponent, DataZoomComponent, CanvasRenderer])

const route = useRoute()
const router = useRouter()
const message = useMessage()
const auth = useAuthStore()

const device = ref<Device | null>(null)
const notFound = ref(false)
const activeTab = ref('overview')
const streamUrl = ref('')

async function handleRefreshStream() {
  if (!device.value) return
  try {
    const data = await videoApi.getStreamUrl(device.value.device_id, '1')
    streamUrl.value = data?.url || ''
  } catch (e: any) {
    message.error(e?.response?.data?.detail || '获取视频流失败')
  }
}

async function handlePtz(action: string) {
  if (!device.value) return
  try {
    await videoApi.ptzControl(device.value.device_id, action, '1')
    message.success('云台控制已发送')
  } catch (e: any) {
    message.error(e?.response?.data?.detail || '云台控制失败')
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
  name: { required: true, message: '请输入设备名称', trigger: 'blur' },
  collect_interval: { required: true, type: 'number' as const, min: 1, max: 3600, message: '采集间隔1-3600秒', trigger: 'blur' },
}
let ws: WebSocket | null = null
let wsReconnectTimer: number | null = null
const writeValues = ref<Record<string, any>>({})
let wsRetryCount = 0
let wsManualClose = false

const protocolLabel: Record<string, string> = {
  modbus_tcp: 'Modbus TCP', opcua: 'OPC-UA', mqtt: 'MQTT', http: 'HTTP',
  simulator: 'Simulator', video: 'Video', s7: 'S7', mc: 'MC', fins: 'FINS',
  allen_bradley: 'AB', opc_da: 'OPC DA', fanuc: 'FANUC', mtconnect: 'MTConnect',
  toledo: 'Toledo', serial_port: 'Serial', database_source: 'DB',
  barcode_scanner: 'Scanner', mqtt_client: 'MQTT Client', http_webhook: 'HTTP Webhook',
  sparkplug_b: 'Sparkplug B', dlt645: 'DL/T 645', iec104: 'IEC 104',
  kuka: 'KUKA', abb_robot: 'ABB', onvif: 'ONVIF',
}

const deviceId = computed(() => route.params.id as string)

const pointColumns = [
  { title: '名称', key: 'name', width: 120 },
  { title: '数据类型', key: 'data_type', width: 100 },
  { title: '地址', key: 'address', width: 100 },
  { title: '单位', key: 'unit', width: 60 },
  { title: '访问模式', key: 'access_mode', width: 80, render: (r: any) => ({ r: '只读', w: '只写', rw: '读写' }[r.access_mode as string] || r.access_mode) },
  { title: '最小值', key: 'min', render: (r: any) => r.min ?? '-' },
  { title: '最大值', key: 'max', render: (r: any) => r.max ?? '-' },
  { title: '模式', key: 'mode', render: (r: any) => r.mode ?? '-' },
]

const realtimeData = computed(() => {
  if (!pointValues.value || !device.value) return []
  return device.value.points.map(pt => ({
    name: pt.name,
    value: pointValues.value?.[pt.name]?.value ?? pointValues.value?.[pt.name] ?? '-',
    quality: pointValues.value?.[pt.name]?.quality ?? '-',
    unit: pt.unit,
    data_type: pt.data_type,
  }))
})

const realtimeColumns = [
  { title: '测点', key: 'name', width: 120 },
  { title: '当前值', key: 'value', width: 150, render: (r: any) => h(NText, { style: { fontWeight: 'bold', fontSize: '14px' } }, { default: () => r.value }) },
  { title: '质量', key: 'quality', width: 80, render: (r: any) => h(NTag, { size: 'small', type: r.quality === 'good' ? 'success' : 'warning', bordered: false }, { default: () => qualityLabel[r.quality] || '异常' }) },
  { title: '单位', key: 'unit', width: 60 },
  { title: '数据类型', key: 'data_type', width: 80 },
]

const writablePoints = computed(() => {
  if (!device.value) return []
  return device.value.points.filter(pt => pt.access_mode === 'w' || pt.access_mode === 'rw')
})

const writeColumns = [
  { title: '测点', key: 'name', width: 120 },
  { title: '地址', key: 'address', width: 100 },
  { title: '数据类型', key: 'data_type', width: 100 },
  { title: '当前值', key: 'current', width: 100, render: (r: any) => pointValues.value?.[r.name]?.value ?? '-' },
  { title: '单位', key: 'unit', width: 60 },
  {
    title: '写入值', key: 'writeValue', width: 150,
    render: (row: any) => h(NInput, {
      size: 'small', placeholder: '输入值', value: writeValues.value[row.name] ?? '',
      onUpdateValue: (val: string) => { writeValues.value[row.name] = val },
    }),
  },
  {
    title: '操作', key: 'action', width: 80,
    render: (row: any) => h(NButton, {
      size: 'small', type: 'primary',
      onClick: () => handleWrite(row),
    }, { default: () => '下发' }),
  },
]

const pointNameOptions = computed(() => device.value?.points.map(pt => ({ label: pt.name, value: pt.name })) ?? [])
const rangeOptions = [
  { label: '近1小时', value: '-1h' },
  { label: '近6小时', value: '-6h' },
  { label: '近24小时', value: '-24h' },
  { label: '近7天', value: '-7d' },
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
    message.success('设备更新成功')
    editing.value = false
    fetchDevice()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || '更新失败')
  } finally {
    saving.value = false
  }
}

async function fetchDevice() {
  notFound.value = false
  try {
    device.value = await deviceApi.get(deviceId.value)
    if (!device.value) { notFound.value = true; return }
    if (device.value?.points?.length) {
    const pointNames = device.value.points.map((p: any) => p.name)
    if (!chartPoint.value || !pointNames.includes(chartPoint.value)) {
      chartPoint.value = device.value.points[0].name
    }
  }
    if (route.query.tab) activeTab.value = route.query.tab as string
  } catch (e: any) {
    notFound.value = true
  }
}

async function fetchPoints() {
  pointsLoading.value = true
  try {
    pointValues.value = await deviceApi.getPoints(deviceId.value)
  } catch (e: any) {
    message.error('获取实时数据失败')
  } finally {
    pointsLoading.value = false
  }
}

async function handleWrite(pt: any) {
  let val: any = writeValues.value[pt.name]
  if (val === null || val === undefined || val === '') {
    message.warning('请输入写入值')
    return
  }
  if (pt.data_type === 'int16' || pt.data_type === 'int32' || pt.data_type === 'uint16' || pt.data_type === 'uint32') {
    val = parseInt(String(val), 10)
    if (isNaN(val)) { message.warning('请输入有效整数'); return }
  } else if (pt.data_type === 'float32' || pt.data_type === 'float64' || pt.data_type === 'double') {
    val = parseFloat(String(val))
    if (isNaN(val)) { message.warning('请输入有效数字'); return }
  } else if (pt.data_type === 'bool') {
    val = val === 'true' || val === '1' || val === true
  }
  try {
    await deviceApi.writePoint(deviceId.value, pt.name, val)
    message.success(`${pt.name} 下发成功: ${val}`)
    fetchPoints()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || '下发失败')
  }
}

async function fetchChartData() {
  if (!chartPoint.value) return
  chartLoading.value = true
  try {
    const result = await dataApi.query({ device_id: deviceId.value, point_name: chartPoint.value, start: chartRange.value })
    chartData.value = (result || []).map((d: any) => ({ time: d.time?.substring(11, 19) || d._time?.substring(11, 19) || '', value: d.value ?? d._value ?? 0 }))
  } catch (e: any) {
    message.error('查询时序数据失败')
  } finally {
    chartLoading.value = false
  }
}

function toggleWS(val: boolean) {
  if (val) {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
    ws = new WebSocket(`${protocol}//${location.host}/ws/v1/realtime`)
    ws.onopen = () => { wsConnected.value = true; wsRetryCount = 0; message.success('WebSocket 已连接') }
    ws.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data)
        if (data.device_id === deviceId.value) {
          if (!pointValues.value) pointValues.value = {}
          pointValues.value[data.point_name] = { value: data.value, quality: data.quality || 'good' }
        }
      } catch (err) {
        console.warn('WebSocket消息解析失败:', err)
      }
    }
    ws.onclose = () => {
      wsConnected.value = false
      if (wsManualClose) { wsManualClose = false; return }
      if (wsRetryCount > 10) {
        message.warning('WebSocket重连次数过多，已停止重连')
        return
      }
      if (wsReconnectTimer) clearTimeout(wsReconnectTimer)
      const backoff = Math.min(2000 * Math.pow(2, wsRetryCount), 60000) + Math.random() * 1000
      wsRetryCount++
      wsReconnectTimer = window.setTimeout(() => {
        if (!wsConnected.value) toggleWS(true)
      }, backoff)
    }
    ws.onerror = () => { wsConnected.value = false; message.error('WebSocket 连接失败') }
  } else {
    wsManualClose = true
    ws?.close()
    ws = null
    wsConnected.value = false
  }
}

onMounted(() => { fetchDevice(); fetchPoints() })
onUnmounted(() => {
  wsManualClose = true
  ws?.close()
  if (wsReconnectTimer) clearTimeout(wsReconnectTimer)
  wsReconnectTimer = null
})

watch(deviceId, () => {
  wsManualClose = true
  ws?.close()
  ws = null
  wsConnected.value = false
  wsRetryCount = 0
  if (wsReconnectTimer) clearTimeout(wsReconnectTimer)
  wsReconnectTimer = null
  chartData.value = []
  pointValues.value = null
  notFound.value = false
  fetchDevice()
  fetchPoints()
})
</script>
