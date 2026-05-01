<template>
  <div class="scada-page">
    <n-card title="Web 组态 (SCADA)">
      <template #header-extra>
        <n-space>
          <n-button @click="showHelp = true">使用说明</n-button>
          <n-button @click="previewMode = !previewMode">{{ previewMode ? '退出预览' : '预览模式' }}</n-button>
          <n-button type="primary" @click="saveProject">保存项目</n-button>
          <n-button @click="loadProject">加载项目</n-button>
        </n-space>
      </template>

      <div class="scada-layout">
        <div class="scada-sidebar">
          <div class="sidebar-title">设备列表</div>
          <n-input v-model:value="deviceSearch" placeholder="搜索设备" size="small" clearable style="margin-bottom: 8px" />
          <div v-if="devices.length === 0" class="sidebar-empty">暂无设备，请先添加设备</div>
          <div
            v-for="device in filteredDevices"
            :key="device.device_id"
            :class="['device-item', { 'device-item--active': selectedDevice?.device_id === device.device_id }]"
            @click="onSelectDevice(device)"
          >
            <div class="device-item__name">{{ device.name }}</div>
            <div class="device-item__meta">
              <n-tag :type="device.status === 'online' ? 'success' : device.status === 'offline' ? 'error' : 'default'" size="tiny">
                {{ deviceStatusLabel[device.status] || device.status }}
              </n-tag>
              <span class="device-item__protocol">{{ protocolLabel[device.protocol] || device.protocol }}</span>
            </div>
            <div v-if="selectedDevice?.device_id === device.device_id && devicePoints.length > 0" class="point-list">
              <div
                v-for="pt in devicePoints"
                :key="pt.name"
                class="point-item"
                @click.stop="addWidgetFromPoint(pt)"
              >
                <span class="point-name">{{ pt.name }}</span>
                <span class="point-value">{{ pointValues[device.device_id]?.[pt.name]?.value ?? '-' }}</span>
                <span class="point-unit">{{ pt.unit || '' }}</span>
                <n-button size="tiny" type="primary" quaternary>添加</n-button>
              </div>
            </div>
            <div v-if="selectedDevice?.device_id === device.device_id && devicePoints.length === 0" class="point-empty">
              该设备暂无数据点
            </div>
          </div>
        </div>

        <div class="scada-main">
          <div class="scada-toolbar" v-if="!previewMode">
            <n-space>
              <n-button size="small" @click="addWidgetManual('gauge')">+ 仪表盘</n-button>
              <n-button size="small" @click="addWidgetManual('indicator')">+ 指示灯</n-button>
              <n-button size="small" @click="addWidgetManual('chart')">+ 趋势图</n-button>
              <n-button size="small" @click="addWidgetManual('switch')">+ 控制开关</n-button>
              <n-button size="small" @click="addWidgetManual('label')">+ 文本标签</n-button>
            </n-space>
          </div>
          <div
            class="scada-canvas"
            ref="canvasRef"
            :class="{ 'preview-mode': previewMode }"
            @click="onCanvasClick"
          >
            <div v-if="widgets.length === 0" class="empty-hint">
              <div class="empty-icon">📊</div>
              <div class="empty-text">从左侧选择设备，点击数据点的「添加」按钮</div>
              <div class="empty-sub">或使用上方工具栏手动添加组件</div>
            </div>
            <div
              v-for="widget in widgets"
              :key="widget.id"
              :class="['scada-widget', { 'widget-selected': selectedWidgetId === widget.id && !previewMode }]"
              :style="{ left: widget.x + 'px', top: widget.y + 'px', width: widget.w + 'px', height: widget.h + 'px' }"
              @mousedown="startDrag($event, widget)"
              @click.stop="selectWidget(widget)"
            >
              <div v-if="widget.type === 'gauge'" class="widget-gauge">
                <div class="gauge-ring" :style="gaugeStyle(widget)">
                  <div class="gauge-value">{{ formatValue(widget) }}</div>
                </div>
                <div class="gauge-label">{{ widget.label }}</div>
                <div class="gauge-device" v-if="widget.deviceName">{{ widget.deviceName }}</div>
              </div>
              <div v-else-if="widget.type === 'indicator'" class="widget-indicator">
                <div :class="['indicator-dot', { on: getWidgetValue(widget) }]" :style="{ background: getWidgetValue(widget) ? '#18a058' : '#d9d9d9', boxShadow: getWidgetValue(widget) ? '0 0 12px #18a05880' : 'none' }"></div>
                <div class="indicator-info">
                  <div class="indicator-label">{{ widget.label }}</div>
                  <div class="indicator-status">{{ getWidgetValue(widget) ? 'ON' : 'OFF' }}</div>
                </div>
              </div>
              <div v-else-if="widget.type === 'chart'" class="widget-chart">
                <div class="chart-header">{{ widget.label }}</div>
                <div class="chart-body">
                  <v-chart :option="getChartOption(widget)" autoresize style="height: 100%" />
                </div>
                <div class="chart-device" v-if="widget.deviceName">{{ widget.deviceName }}</div>
              </div>
              <div v-else-if="widget.type === 'switch'" class="widget-switch">
                <div class="switch-label">{{ widget.label }}</div>
                <n-switch v-model:value="widget.value" @update:value="v => onSwitchChange(widget, v)" :disabled="!previewMode" />
                <div class="switch-status">{{ widget.value ? '已开启' : '已关闭' }}</div>
              </div>
              <div v-else-if="widget.type === 'label'" class="widget-label">
                {{ widget.label }}
              </div>
              <div v-if="!previewMode" class="widget-actions">
                <n-button size="tiny" quaternary @click.stop="editWidget(widget)">⚙</n-button>
                <n-button size="tiny" quaternary type="error" @click.stop="removeWidget(widget.id)">✕</n-button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </n-card>

    <n-modal v-model:show="showEditModal" preset="card" title="组件配置" style="width: 420px">
      <n-form label-placement="left" label-width="80">
        <n-form-item label="标签">
          <n-input v-model:value="editForm.label" />
        </n-form-item>
        <n-form-item label="绑定设备">
          <n-select v-model:value="editForm.deviceId" :options="deviceOptions" placeholder="选择设备" clearable @update:value="onEditDeviceChange" />
        </n-form-item>
        <n-form-item label="绑定数据点">
          <n-select v-model:value="editForm.pointName" :options="editPointOptions" placeholder="选择数据点" clearable />
        </n-form-item>
        <n-form-item v-if="editForm.type === 'gauge'" label="最小值">
          <n-input-number v-model:value="editForm.min" style="width: 100%" />
        </n-form-item>
        <n-form-item v-if="editForm.type === 'gauge'" label="最大值">
          <n-input-number v-model:value="editForm.max" style="width: 100%" />
        </n-form-item>
        <n-form-item v-if="editForm.type === 'gauge'" label="单位">
          <n-input v-model:value="editForm.unit" placeholder="如: ℃, MPa, %" />
        </n-form-item>
      </n-form>
      <template #action>
        <n-space justify="end">
          <n-button @click="showEditModal = false">取消</n-button>
          <n-button type="primary" @click="saveWidgetEdit">保存</n-button>
        </n-space>
      </template>
    </n-modal>

    <n-modal v-model:show="showHelp" preset="card" title="Web 组态使用说明" style="width: 520px">
      <n-space vertical>
        <n-alert type="info" title="什么是 Web 组态？">
          Web 组态（SCADA）是一种可视化监控界面，可以将设备的实时数据以仪表盘、指示灯、趋势图等形式展示，实现一目了然的设备监控。
        </n-alert>
        <div style="font-size: 14px; line-height: 1.8">
          <strong>使用步骤：</strong>
          <ol style="padding-left: 20px; margin: 8px 0">
            <li>在左侧设备列表中选择一个设备</li>
            <li>点击数据点右侧的「添加」按钮，自动创建监控组件</li>
            <li>拖拽组件到合适位置</li>
            <li>点击组件右上角 ⚙ 可修改配置（标签、范围、单位等）</li>
            <li>点击「预览模式」查看实时数据效果</li>
            <li>点击「保存项目」将组态保存到本地</li>
          </ol>
          <strong>组件类型：</strong>
          <ul style="padding-left: 20px; margin: 8px 0">
            <li><strong>仪表盘</strong>：显示数值型数据（温度、压力等）</li>
            <li><strong>指示灯</strong>：显示开关量状态（运行/停止）</li>
            <li><strong>趋势图</strong>：显示数据历史变化趋势</li>
            <li><strong>控制开关</strong>：向设备写入控制指令</li>
            <li><strong>文本标签</strong>：添加说明文字</li>
          </ul>
          <strong>与 3D 数字孪生的关系：</strong>
          <p>3D 数字孪生展示设备的整体空间布局和状态总览，Web 组态则提供详细的实时数据监控面板，两者互补。</p>
        </div>
      </n-space>
    </n-modal>

    <input type="file" ref="fileInputRef" style="display: none" accept=".json" @change="onFileLoad" />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch, nextTick } from 'vue'
import {
  NCard, NButton, NSpace, NInput, NSelect, NTag, NSwitch, NModal,
  NForm, NFormItem, NInputNumber, NAlert, useMessage, useDialog,
} from 'naive-ui'
import { use } from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import VChart from 'vue-echarts'
import { deviceApi } from '@/api'
import { deviceStatusLabel } from '@/utils/enumLabels'

use([LineChart, GridComponent, TooltipComponent, CanvasRenderer])

const message = useMessage()
const dialog = useDialog()
const canvasRef = ref<HTMLElement | null>(null)
const fileInputRef = ref<HTMLInputElement | null>(null)
const previewMode = ref(false)
const showEditModal = ref(false)
const showHelp = ref(false)
const devices = ref<any[]>([])
const deviceSearch = ref('')
const selectedDevice = ref<any>(null)
const devicePoints = ref<any[]>([])
const pointValues = ref<Record<string, Record<string, any>>>({})
const widgets = ref<any[]>([])
const selectedWidgetId = ref<number | null>(null)
const chartData = ref<Map<number, { time: number; value: number }[]>>(new Map())

let widgetIdCounter = 0
let dragging: any = null
let refreshTimer: any = null

const protocolLabel: Record<string, string> = {
  modbus_tcp: 'Modbus TCP', opcua: 'OPC-UA', mqtt: 'MQTT', http: 'HTTP',
  simulator: '模拟器', video: '视频', s7: 'S7', modbus_rtu: 'Modbus RTU',
}

const filteredDevices = computed(() => {
  if (!deviceSearch.value) return devices.value
  const q = deviceSearch.value.toLowerCase()
  return devices.value.filter(d => d.name.toLowerCase().includes(q) || d.device_id.toLowerCase().includes(q))
})

const deviceOptions = computed(() =>
  devices.value.map(d => ({ label: `${d.name} (${protocolLabel[d.protocol] || d.protocol})`, value: d.device_id }))
)

const editForm = ref<any>({})
const editPointOptions = ref<{ label: string; value: string }[]>([])

function getChartOption(widget: any) {
  const arr = chartData.value.get(widget.id) || []
  const times = arr.map(d => {
    const dt = new Date(d.time)
    return `${String(dt.getHours()).padStart(2, '0')}:${String(dt.getMinutes()).padStart(2, '0')}:${String(dt.getSeconds()).padStart(2, '0')}`
  })
  const values = arr.map(d => d.value)
  return {
    grid: { left: 30, right: 8, top: 8, bottom: 20 },
    tooltip: { trigger: 'axis', formatter: (params: any) => {
      if (!params?.length) return ''
      const p = params[0]
      return `${p.axisValue}<br/>${p.marker} ${Number(p.value).toFixed(2)}`
    }},
    xAxis: { type: 'category', data: times, axisLabel: { fontSize: 9, interval: 'auto' }, show: arr.length > 0 },
    yAxis: { type: 'value', axisLabel: { fontSize: 9 }, show: arr.length > 0 },
    series: [{
      type: 'line', data: values, smooth: true, symbol: 'none',
      itemStyle: { color: '#667eea' }, areaStyle: { color: 'rgba(102,126,234,0.15)' },
    }],
  }
}

function onEditDeviceChange(deviceId: string) {
  editForm.value.pointName = null
  if (!deviceId) { editPointOptions.value = []; return }
  const device = devices.value.find(d => d.device_id === deviceId)
  if (device?.points?.length) {
    editPointOptions.value = device.points.map((p: any) => ({ label: `${p.name} (${p.unit || '-'})`, value: p.name }))
  } else {
    editPointOptions.value = []
  }
}

async function fetchDevices() {
  try {
    const data = await deviceApi.list({ page: 1, size: 200 })
    devices.value = data?.data ?? []
  } catch { /* ignore */ }
}

async function onSelectDevice(device: any) {
  selectedDevice.value = device
  try {
    const data = await deviceApi.getPoints(device.device_id)
    if (Array.isArray(data)) {
      devicePoints.value = data
    } else if (data && typeof data === 'object') {
      devicePoints.value = Object.entries(data).map(([name, info]: [string, any]) => ({
        name,
        ...(typeof info === 'object' ? info : { value: info }),
      }))
    } else {
      devicePoints.value = device.points || []
    }
    await fetchPointValues(device.device_id)
  } catch {
    devicePoints.value = device.points || []
  }
}

async function fetchPointValues(deviceId: string) {
  try {
    const data = await deviceApi.getPoints(deviceId)
    if (data) pointValues.value = { ...pointValues.value, [deviceId]: data }
  } catch { /* ignore */ }
}

async function refreshAllValues() {
  const deviceIds = new Set<string>()
  widgets.value.forEach(w => { if (w.deviceId) deviceIds.add(w.deviceId) })
  for (const deviceId of deviceIds) {
    await fetchPointValues(deviceId)
  }
  updateWidgetValues()
}

function updateWidgetValues() {
  widgets.value.forEach(w => {
    if (w.deviceId && w.pointName) {
      const val = pointValues.value[w.deviceId]?.[w.pointName]?.value
        ?? pointValues.value[w.deviceId]?.[w.pointName]
      if (val !== undefined && val !== null) {
        if (w.type === 'gauge') w.value = Number(val)
        else if (w.type === 'indicator') w.value = !!val
        else if (w.type === 'chart') {
          const arr = chartData.value.get(w.id) || []
          arr.push({ time: Date.now(), value: Number(val) })
          if (arr.length > 60) arr.splice(0, arr.length - 60)
          chartData.value.set(w.id, arr)
          w.value = Number(val)
        }
      }
    }
  })
}

function addWidgetFromPoint(pt: any) {
  const isBool = pt.type === 'bool' || pt.name.toLowerCase().includes('switch') || pt.name.toLowerCase().includes('status')
  const type: string = isBool ? 'indicator' : 'gauge'
  const device = selectedDevice.value
  widgetIdCounter++
  const widget: any = {
    id: widgetIdCounter,
    type,
    label: pt.name,
    deviceId: device.device_id,
    deviceName: device.name,
    pointName: pt.name,
    unit: pt.unit || '',
    min: 0,
    max: 100,
    x: 20 + (widgets.value.length % 4) * 180,
    y: 20 + Math.floor(widgets.value.length / 4) * 140,
    w: type === 'chart' ? 300 : 160,
    h: type === 'chart' ? 180 : 120,
    value: pointValues.value[device.device_id]?.[pt.name]?.value
      ?? pointValues.value[device.device_id]?.[pt.name]
      ?? (type === 'indicator' ? false : 0),
  }
  widgets.value.push(widget)
  message.success(`已添加「${pt.name}」${type === 'indicator' ? '指示灯' : '仪表盘'}`)
}

function addWidgetManual(type: string) {
  widgetIdCounter++
  const widget: any = {
    id: widgetIdCounter,
    type,
    label: type === 'gauge' ? '仪表盘' : type === 'indicator' ? '指示灯' : type === 'chart' ? '趋势图' : type === 'switch' ? '控制开关' : '文本标签',
    deviceId: null,
    deviceName: '',
    pointName: null,
    unit: '',
    min: 0,
    max: 100,
    x: 20 + (widgets.value.length % 4) * 180,
    y: 20 + Math.floor(widgets.value.length / 4) * 140,
    w: type === 'chart' ? 300 : 160,
    h: type === 'chart' ? 180 : type === 'label' ? 60 : 120,
    value: type === 'switch' ? false : type === 'indicator' ? false : 0,
  }
  widgets.value.push(widget)
  selectedWidgetId.value = widget.id
}

function removeWidget(id: number) {
  widgets.value = widgets.value.filter(w => w.id !== id)
  chartData.value.delete(id)
  if (selectedWidgetId.value === id) selectedWidgetId.value = null
}

function selectWidget(widget: any) {
  if (previewMode.value) return
  selectedWidgetId.value = widget.id
}

function onCanvasClick() {
  selectedWidgetId.value = null
}

function editWidget(widget: any) {
  editForm.value = { ...widget }
  if (widget.deviceId) onEditDeviceChange(widget.deviceId)
  showEditModal.value = true
}

function saveWidgetEdit() {
  const idx = widgets.value.findIndex(w => w.id === editForm.value.id)
  if (idx >= 0) {
    const device = devices.value.find(d => d.device_id === editForm.value.deviceId)
    widgets.value[idx] = {
      ...widgets.value[idx],
      ...editForm.value,
      deviceName: device?.name || '',
    }
  }
  showEditModal.value = false
  message.success('组件配置已更新')
}

async function onSwitchChange(widget: any, value: boolean) {
  if (widget.deviceId && widget.pointName) {
    dialog.warning({
      title: '确认操作',
      content: `即将向设备「${widget.deviceName || widget.deviceId}」的「${widget.label}」写入 ${value ? '开启' : '关闭'}，此操作将直接影响物理设备，是否继续？`,
      positiveText: '确认',
      negativeText: '取消',
      onPositiveClick: async () => {
        try {
          await deviceApi.writePoint(widget.deviceId, widget.pointName, value)
          message.success(`已${value ? '开启' : '关闭'} ${widget.label}`)
        } catch {
          message.error('操作失败')
          widget.value = !value
        }
      },
      onNegativeClick: () => {
        widget.value = !value
      },
    })
  }
}

function getWidgetValue(widget: any): any {
  if (!widget.deviceId || !widget.pointName) return widget.value
  const val = pointValues.value[widget.deviceId]?.[widget.pointName]?.value
    ?? pointValues.value[widget.deviceId]?.[widget.pointName]
  return val !== undefined ? val : widget.value
}

function formatValue(widget: any): string {
  const val = getWidgetValue(widget)
  if (typeof val === 'number') return val.toFixed(1)
  return String(val ?? '-')
}

function gaugeStyle(widget: any) {
  const val = Number(getWidgetValue(widget)) || 0
  const min = widget.min ?? 0
  const max = widget.max ?? 100
  const pct = Math.min(100, Math.max(0, ((val - min) / (max - min)) * 100))
  const angle = (pct / 100) * 270 - 135
  return {
    '--gauge-pct': pct + '%',
    background: `conic-gradient(from -135deg, #18a058 0deg, #f0c040 ${(pct * 2.7)}deg, #e0e0e0 ${(pct * 2.7)}deg, #e0e0e0 270deg, transparent 270deg)`,
  }
}

function startDrag(e: MouseEvent, widget: any) {
  if (previewMode.value) return
  const rect = canvasRef.value!.getBoundingClientRect()
  dragging = { widget, offsetX: e.clientX - rect.left - widget.x, offsetY: e.clientY - rect.top - widget.y }
  const onMouseMove = (ev: MouseEvent) => {
    if (dragging) {
      const r = canvasRef.value!.getBoundingClientRect()
      dragging.widget.x = Math.max(0, ev.clientX - r.left - dragging.offsetX)
      dragging.widget.y = Math.max(0, ev.clientY - r.top - dragging.offsetY)
    }
  }
  const onMouseUp = () => {
    dragging = null
    document.removeEventListener('mousemove', onMouseMove)
    document.removeEventListener('mouseup', onMouseUp)
  }
  document.addEventListener('mousemove', onMouseMove)
  document.addEventListener('mouseup', onMouseUp)
}

function saveProject() {
  const data = { widgets: widgets.value.map(w => ({ ...w })) }
  localStorage.setItem('scada-project', JSON.stringify(data))
  message.success('项目已保存')
}

function loadProject() {
  fileInputRef.value?.click()
}

function onFileLoad(e: Event) {
  const file = (e.target as HTMLInputElement).files?.[0]
  if (!file) return
  const reader = new FileReader()
  reader.onload = (ev) => {
    try {
      const data = JSON.parse(ev.target?.result as string)
      if (data.widgets) {
        widgets.value = data.widgets
        widgetIdCounter = Math.max(...data.widgets.map((w: any) => w.id), 0)
        message.success(`已加载 ${data.widgets.length} 个组件`)
      }
    } catch {
      message.error('文件格式错误')
    }
  }
  reader.readAsText(file)
  ;(e.target as HTMLInputElement).value = ''
}

watch(previewMode, (val) => {
  if (val) {
    selectedWidgetId.value = null
    refreshAllValues()
  }
})

onMounted(async () => {
  await fetchDevices()
  const saved = localStorage.getItem('scada-project')
  if (saved) {
    try {
      const data = JSON.parse(saved)
      if (data.widgets?.length) {
        widgets.value = data.widgets
        widgetIdCounter = Math.max(...data.widgets.map((w: any) => w.id), 0)
      }
    } catch { /* ignore */ }
  }
  refreshTimer = setInterval(refreshAllValues, 5000)
})

onUnmounted(() => {
  if (refreshTimer) clearInterval(refreshTimer)
  localStorage.setItem('scada-project', JSON.stringify({ widgets: widgets.value }))
})
</script>

<style scoped>
.scada-page { padding: 16px; }

.scada-layout {
  display: flex;
  gap: 0;
  height: calc(100vh - 200px);
  min-height: 500px;
  border: 1px solid var(--n-border-color);
  border-radius: 8px;
  overflow: hidden;
}

.scada-sidebar {
  width: 260px;
  min-width: 260px;
  background: var(--n-color);
  border-right: 1px solid var(--n-border-color);
  overflow-y: auto;
  padding: 12px;
}

.sidebar-title {
  font-weight: 600;
  font-size: 14px;
  margin-bottom: 8px;
  color: var(--n-text-color);
}

.sidebar-empty {
  text-align: center;
  color: var(--n-text-color-3);
  font-size: 13px;
  padding: 24px 0;
}

.device-item {
  padding: 8px 10px;
  border-radius: 6px;
  cursor: pointer;
  margin-bottom: 4px;
  border: 1px solid transparent;
  transition: all 0.2s;
}

.device-item:hover { background: var(--n-color-hover); }

.device-item--active {
  background: var(--n-color-hover);
  border-color: var(--n-primary-color);
}

.device-item__name {
  font-weight: 600;
  font-size: 13px;
  margin-bottom: 4px;
}

.device-item__meta {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: var(--n-text-color-3);
}

.device-item__protocol { font-size: 11px; }

.point-list {
  margin-top: 6px;
  padding: 4px 0;
  border-top: 1px solid var(--n-border-color);
}

.point-item {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 4px 6px;
  border-radius: 4px;
  font-size: 12px;
  cursor: pointer;
  transition: background 0.15s;
}

.point-item:hover { background: var(--n-primary-color-suppl); }

.point-name { flex: 1; color: var(--n-text-color-2); }
.point-value { font-weight: 600; color: var(--n-primary-color); min-width: 30px; text-align: right; }
.point-unit { color: var(--n-text-color-3); min-width: 20px; }

.point-empty {
  font-size: 12px;
  color: var(--n-text-color-3);
  padding: 8px 6px;
  text-align: center;
}

.scada-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.scada-toolbar {
  padding: 8px 12px;
  border-bottom: 1px solid var(--n-border-color);
  background: var(--n-color);
}

.scada-canvas {
  flex: 1;
  position: relative;
  background:
    linear-gradient(90deg, var(--n-border-color) 1px, transparent 1px),
    linear-gradient(var(--n-border-color) 1px, transparent 1px);
  background-size: 20px 20px;
  background-color: #f8f9fa;
  overflow: auto;
}

.scada-canvas.preview-mode {
  background-color: #eef0f2;
}

.empty-hint {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  text-align: center;
  color: #999;
}

.empty-icon { font-size: 48px; margin-bottom: 12px; }
.empty-text { font-size: 16px; margin-bottom: 4px; }
.empty-sub { font-size: 13px; color: #bbb; }

.scada-widget {
  position: absolute;
  background: #fff;
  border: 2px solid transparent;
  border-radius: 8px;
  padding: 10px;
  cursor: move;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.08);
  transition: border-color 0.2s;
  user-select: none;
}

.scada-widget:hover { border-color: var(--n-primary-color-suppl); }

.widget-selected { border-color: var(--n-primary-color) !important; }

.widget-actions {
  position: absolute;
  top: 2px;
  right: 2px;
  display: flex;
  gap: 2px;
}

.widget-gauge { text-align: center; height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center; }

.gauge-ring {
  width: 80px;
  height: 80px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  position: relative;
}

.gauge-value {
  font-size: 20px;
  font-weight: bold;
  color: #18a058;
  background: #fff;
  width: 60px;
  height: 60px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  position: absolute;
}

.gauge-label { font-size: 12px; color: #666; margin-top: 4px; }
.gauge-device { font-size: 10px; color: #999; }

.widget-indicator {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 100%;
}

.indicator-dot {
  width: 20px;
  height: 20px;
  border-radius: 50%;
  transition: all 0.3s;
  flex-shrink: 0;
}

.indicator-info { flex: 1; }
.indicator-label { font-size: 13px; font-weight: 600; }
.indicator-status { font-size: 11px; color: #999; margin-top: 2px; }

.widget-chart { height: 100%; display: flex; flex-direction: column; }
.chart-header { font-size: 12px; font-weight: 600; margin-bottom: 4px; }
.chart-body { flex: 1; min-height: 80px; background: #fafafa; border-radius: 4px; position: relative; }
.chart-device { font-size: 10px; color: #999; margin-top: 2px; }

.widget-switch {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  height: 100%;
}

.switch-label { font-size: 13px; font-weight: 600; }
.switch-status { font-size: 11px; color: #999; }

.widget-label {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  font-size: 14px;
  font-weight: 600;
  color: #333;
}
</style>
