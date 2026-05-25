<template>
  <n-spin :show="pageLoading" :description="t('scada.loading')">
  <div class="scada-page">
    <div class="scada-header">
      <div class="header-left">
        <span class="header-title">{{ t('scada.title') }}</span>
        <n-tag v-if="previewMode" type="warning" size="small" round>{{ t('scada.previewMode') }}</n-tag>
      </div>
      <div class="header-actions">
        <n-button size="small" quaternary style="color: #fff" @click="showHelp = true">{{ t('scada.instructions') }}</n-button>
        <n-button-group size="small">
          <n-button quaternary style="color: #fff" @click="undo" :disabled="historyIndex <= 0">
            <template #icon><n-icon :component="ArrowUndoOutline" /></template>
            {{ t('scada.undo') }}
          </n-button>
            <n-button quaternary style="color: #fff" @click="redo" :disabled="historyIndex >= historyStack.length - 1">
            <template #icon><n-icon :component="ArrowRedoOutline" /></template>
            {{ t('scada.redo') }}
          </n-button>
        </n-button-group>
        <n-button size="small" :type="previewMode ? 'warning' : 'default'" style="color: #fff" @click="previewMode = !previewMode">
          <template #icon><n-icon :component="previewMode ? PlayOutline : PlayCircleSharp" /></template>
          {{ previewMode ? t('scada.exitPreview') : t('scada.preview') }}
        </n-button>
        <n-button size="small" type="primary" @click="saveProject">
          <template #icon><n-icon :component="SaveOutline" /></template>
          {{ t('scada.save') }}
        </n-button>
        <n-button size="small" style="color: #fff" @click="loadProject">
          <template #icon><n-icon :component="Folder" /></template>
          {{ t('scada.load') }}
        </n-button>
        <n-button size="small" quaternary style="color: #fff" @click="exportAsImage">
          <template #icon><n-icon :component="Image" /></template>
          {{ t('scada.exportImage') }}
        </n-button>
      </div>
    </div>

    <div class="scada-body">
      <div v-if="!previewMode" class="toolbox">
        <div class="toolbox-title">{{ t('scada.components') }}</div>
        <div class="toolbox-items">
          <div v-for="comp in componentTypes" :key="comp.type" class="toolbox-item" @click="addWidgetManual(comp.type)">
            <div class="tb-icon" :style="{ background: comp.color }">{{ comp.icon }}</div>
            <div class="tb-label">{{ comp.label }}</div>
          </div>
        </div>
        <div class="toolbox-title" style="margin-top: 16px">{{ t('scada.devices') }}</div>
        <n-input v-model:value="deviceSearch" :placeholder="t('scada.searchDevices')" size="tiny" clearable style="margin-bottom: 6px" />
        <div class="device-tree">
          <div v-for="d in filteredDevices" :key="d.device_id" class="dt-device" @click="onSelectDevice(d)">
            <div :class="['dt-device-header', { active: expandedDevice === d.device_id }]">
              <span class="dt-dot" :style="{ background: d.status === 'online' ? '#18a058' : d.status === 'offline' ? '#d03050' : '#666' }"></span>
              <span class="dt-name">{{ d.name }}</span>
            </div>
            <div v-if="expandedDevice === d.device_id" class="dt-points">
              <div v-for="pt in currentDevicePoints" :key="pt.name" class="dt-point" @click.stop="addWidgetFromPoint(pt)">
                <span>{{ pt.name }}</span>
                <n-button size="tiny" type="primary" quaternary>+</n-button>
              </div>
              <div v-if="!currentDevicePoints.length" class="dt-empty">{{ t('scada.noPoints') }}</div>
            </div>
          </div>
        </div>
      </div>

      <div class="canvas-area" ref="canvasAreaRef">
        <div v-if="!previewMode" class="canvas-toolbar">
          <n-button-group size="tiny">
            <n-button style="color: #fff" @click="zoom = Math.min(zoom + 0.1, 2)">{{ t('scada.zoomIn') }}</n-button>
            <n-button style="color: #fff; cursor: default; background: transparent; border-color: #1a2a3a;">{{ Math.round(zoom * 100) }}%</n-button>
            <n-button style="color: #fff" @click="zoom = Math.max(zoom - 0.1, 0.3)">{{ t('scada.zoomOut') }}</n-button>
          </n-button-group>
        </div>
        <div
          class="scada-canvas"
          ref="canvasRef"
          :class="{ 'preview-mode': previewMode }"
          :style="{ transform: `scale(${zoom})`, transformOrigin: 'top left' }"
          @click="onCanvasClick"
        >
            <div v-if="widgets.length === 0" class="empty-hint">
              <n-icon :component="StatsChartOutline" :size="48" class="empty-icon-svg" />
              <div class="empty-text">{{ t('scada.selectHint') }}</div>
            </div>
          <div
            v-for="widget in widgets" :key="widget.id"
            :class="['scada-widget', `widget-${widget.type}`, { selected: selectedWidgetId === widget.id && !previewMode }]"
            :style="widgetStyle(widget)"
            @mousedown="startDrag($event, widget)"
            @click.stop="selectWidget(widget)"
          >
            <div v-if="widget.type === 'gauge'" class="w-gauge">
              <svg viewBox="0 0 120 120" class="gauge-svg">
                <path d="M 15 95 A 50 50 0 1 1 105 95" fill="none" stroke="#1a2a3a" stroke-width="10" stroke-linecap="round" />
                <path d="M 15 95 A 50 50 0 1 1 105 95" fill="none" :stroke="gaugeColor(widget)" stroke-width="10" stroke-linecap="round"
                  :stroke-dasharray="gaugeDash(widget)" />
                <text x="60" y="62" text-anchor="middle" fill="#e0f0ff" font-size="22" font-weight="bold">{{ formatValue(widget) }}</text>
                <text x="60" y="80" text-anchor="middle" fill="#607d8b" font-size="10">{{ widget.unit || '' }}</text>
                <text x="60" y="110" text-anchor="middle" fill="#4fc3f7" font-size="9">{{ widget.label }}</text>
              </svg>
            </div>
            <div v-else-if="widget.type === 'indicator'" class="w-indicator">
              <div :class="['ind-light', { on: getWidgetValue(widget) }]"
                :style="{ background: getWidgetValue(widget) ? '#18a058' : '#333', boxShadow: getWidgetValue(widget) ? '0 0 20px #18a05880, 0 0 40px #18a05840' : 'none' }">
              </div>
              <div class="ind-label">{{ widget.label }}</div>
              <div class="ind-status">{{ getWidgetValue(widget) ? 'ON' : 'OFF' }}</div>
            </div>
            <div v-else-if="widget.type === 'chart'" class="w-chart">
              <div class="chart-title">{{ widget.label }}</div>
              <div class="chart-body"><v-chart :option="getChartOption(widget)" autoresize style="height: 100%" /></div>
            </div>
            <div v-else-if="widget.type === 'switch'" class="w-switch">
              <div class="sw-label">{{ widget.label }}</div>
              <n-switch v-model:value="widget.value" @update:value="v => onSwitchChange(widget, v)" :disabled="!previewMode" />
              <div :class="['sw-status', { on: widget.value }]">{{ widget.value ? t('scada.on') : t('scada.off') }}</div>
            </div>
            <div v-else-if="widget.type === 'tank'" class="w-tank">
              <div class="tank-body">
                <div class="tank-fill" :style="{ height: tankPercent(widget) + '%', background: tankColor(widget) }"></div>
              </div>
              <div class="tank-val">{{ formatValue(widget) }}</div>
              <div class="tank-label">{{ widget.label }}</div>
            </div>
            <div v-else-if="widget.type === 'label'" class="w-label">
              {{ widget.label }}
            </div>
            <div v-if="!previewMode" class="widget-actions">
              <n-button text class="wa-btn" @click.stop="editWidget(widget)">
                <n-icon :component="AddOutline" :size="14" />
              </n-button>
              <n-button text class="wa-btn wa-del" @click.stop="removeWidget(widget.id)">
                <n-icon :component="CloseOutline" :size="14" />
              </n-button>
            </div>
            <div v-if="!previewMode && selectedWidgetId === widget.id" class="resize-handle" @mousedown.stop="startResize($event, widget)"></div>
          </div>
        </div>
      </div>

      <div v-if="!previewMode && selectedWidgetId" class="props-panel">
        <div class="props-title">{{ t('scada.properties') }}</div>
        <div class="props-body">
          <div class="prop-row"><span class="prop-label">{{ t('scada.type') }}</span><span class="prop-val">{{ componentTypes.find(c => c.type === editForm.type)?.label }}</span></div>
          <div class="prop-row"><span class="prop-label">{{ t('scada.label') }}</span><n-input v-model:value="editForm.label" size="tiny" @update:value="applyProp" /></div>
            <div class="prop-row"><span class="prop-label">{{ t('scada.device') }}</span><n-select v-model:value="editForm.deviceId" :options="deviceOptions" size="tiny" :placeholder="t('scada.selectDevice')" clearable @update:value="onEditDeviceChange" /></div>
            <div class="prop-row"><span class="prop-label">{{ t('scada.point') }}</span><n-select v-model:value="editForm.pointName" :options="editPointOptions" size="tiny" :placeholder="t('scada.selectPoint')" clearable @update:value="applyProp" /></div>
          <template v-if="editForm.type === 'gauge' || editForm.type === 'tank'">
            <div class="prop-row"><span class="prop-label">{{ t('scada.minValue') }}</span><n-input-number v-model:value="editForm.min" size="tiny" @update:value="applyProp" /></div>
              <div class="prop-row"><span class="prop-label">{{ t('scada.maxValue') }}</span><n-input-number v-model:value="editForm.max" size="tiny" @update:value="applyProp" /></div>
            <div class="prop-row"><span class="prop-label">{{ t('scada.unit') }}</span><n-input v-model:value="editForm.unit" size="tiny" placeholder="℃ MPa %" @update:value="applyProp" /></div>
          </template>
          <div class="prop-row"><span class="prop-label">{{ t('scada.width') }}</span><n-input-number v-model:value="editForm.w" size="tiny" :min="60" :step="10" @update:value="applyProp" /></div>
            <div class="prop-row"><span class="prop-label">{{ t('scada.height') }}</span><n-input-number v-model:value="editForm.h" size="tiny" :min="60" :step="10" @update:value="applyProp" /></div>
        </div>
      </div>
    </div>

    <n-modal v-model:show="showHelp" preset="card" :title="t('scada.helpTitle')" style="width: 520px">  <!-- FIXED: 原问题-中文硬编码 -->
      <n-space vertical>
        <n-alert type="info" :title="t('scada.whatIsScada')">{{ t('scada.whatIsScadaDesc') }}</n-alert>  <!-- FIXED: 原问题-中文硬编码 -->
        <div style="font-size: 14px; line-height: 1.8">
          <strong>{{ t('scada.stepsTitle') }}</strong>  <!-- FIXED: 原问题-中文硬编码 -->
          <ol style="padding-left: 20px; margin: 8px 0">
            <li>{{ t('scada.step1') }}</li>  <!-- FIXED: 原问题-中文硬编码 -->
            <li>{{ t('scada.step2') }}</li>  <!-- FIXED: 原问题-中文硬编码 -->
            <li>{{ t('scada.step3') }}</li>  <!-- FIXED: 原问题-中文硬编码 -->
            <li>{{ t('scada.step4') }}</li>  <!-- FIXED: 原问题-中文硬编码 -->
            <li>{{ t('scada.step5') }}</li>  <!-- FIXED: 原问题-中文硬编码 -->
            <li>{{ t('scada.step6') }}</li>  <!-- FIXED: 原问题-中文硬编码 -->
          </ol>
          <strong>{{ t('scada.shortcutsTitle') }}</strong>  <!-- FIXED: 原问题-中文硬编码 -->
          <table style="width: 100%; margin-top: 4px; font-size: 13px">
            <tr><td style="color: #4fc3f7; width: 140px">Ctrl + Z</td><td>{{ t('scada.undo') }}</td></tr>  <!-- FIXED: 原问题-中文硬编码 -->
            <tr><td style="color: #4fc3f7">Ctrl + Y</td><td>{{ t('scada.redo') }}</td></tr>  <!-- FIXED: 原问题-中文硬编码 -->
            <tr><td style="color: #4fc3f7">Ctrl + C</td><td>{{ t('scada.copyComponent') }}</td></tr>  <!-- FIXED: 原问题-中文硬编码 -->
            <tr><td style="color: #4fc3f7">Ctrl + V</td><td>{{ t('scada.pasteComponent') }}</td></tr>  <!-- FIXED: 原问题-中文硬编码 -->
            <tr><td style="color: #4fc3f7">Ctrl + D</td><td>{{ t('scada.duplicateComponent') }}</td></tr>  <!-- FIXED: 原问题-中文硬编码 -->
            <tr><td style="color: #4fc3f7">Ctrl + S</td><td>{{ t('scada.saveProject') }}</td></tr>  <!-- FIXED: 原问题-中文硬编码 -->
            <tr><td style="color: #4fc3f7">Delete</td><td>{{ t('scada.deleteSelected') }}</td></tr>  <!-- FIXED: 原问题-中文硬编码 -->
          </table>
        </div>
      </n-space>
    </n-modal>
    <input type="file" ref="fileInputRef" style="display: none" accept=".json" @change="onFileLoad" />
  </div>
  </n-spin>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch, onBeforeUnmount } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import {
  NButton, NButtonGroup, NSpace, NInput, NSelect, NTag, NSwitch, NModal,
  NInputNumber, NAlert, NSpin, NIcon, useMessage, useDialog,
} from 'naive-ui'
import {
  OptionsSharp, BulbOutline, StatsChartOutline, PowerOutline,
  CubeSharp, Text, SaveOutline, Folder,
  Image, ArrowUndoOutline, ArrowRedoOutline, PlayCircleSharp, RefreshOutline, PlayOutline,
  TimerOutline, AddOutline, CloseOutline
} from '@vicons/ionicons5'
import { use } from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import VChart from 'vue-echarts'
import { deviceApi, scadaApi } from '@/api'
import { protocolLabel } from '@/utils/enumLabels'
import { t } from '@/i18n'  // FIXED: 原问题-#注释导致编译失败，改为//注释

// FIXED: 原问题-ScadaEditor.vue全部中文硬编码，改为i18n

use([LineChart, GridComponent, TooltipComponent, CanvasRenderer])

const message = useMessage()
const dialog = useDialog()
const canvasRef = ref<HTMLElement | null>(null)
const canvasAreaRef = ref<HTMLElement | null>(null)
const fileInputRef = ref<HTMLInputElement | null>(null)
const previewMode = ref(false)
const showHelp = ref(false)
const devices = ref<any[]>([])
const deviceSearch = ref('')
const expandedDevice = ref<string | null>(null)
const currentDevicePoints = ref<any[]>([])
const pointValues = ref<Record<string, Record<string, any>>>({})
const widgets = ref<any[]>([])
const selectedWidgetId = ref<number | null>(null)
const chartData = ref<Map<number, { time: number; value: number }[]>>(new Map())
const zoom = ref(1)
const editForm = ref<any>({})
const editPointOptions = ref<{ label: string; value: string }[]>([])
const pageLoading = ref(true)
const saving = ref(false)
const dirty = ref(false)

let widgetIdCounter = 0
let dragging: any = null
let resizing: any = null
let refreshTimer: any = null
let historyStack: string[] = []
let historyIndex = -1
let clipboard: any = null

function pushHistory() {
  const snapshot = JSON.stringify(widgets.value)
  historyStack = historyStack.slice(0, historyIndex + 1)
  historyStack.push(snapshot)
  if (historyStack.length > 50) historyStack.shift()
  historyIndex = historyStack.length - 1
}

function undo() {
  if (historyIndex <= 0) return
  historyIndex--
  widgets.value = JSON.parse(historyStack[historyIndex])
  selectedWidgetId.value = null
}

function redo() {
  if (historyIndex >= historyStack.length - 1) return
  historyIndex++
  widgets.value = JSON.parse(historyStack[historyIndex])
  selectedWidgetId.value = null
}

function copyWidget() {
  if (!selectedWidgetId.value) return
  const w = widgets.value.find(w => w.id === selectedWidgetId.value)
  if (w) { clipboard = { ...w }; message.success(t('scada.copied')) }  // FIXED: 原问题-中文硬编码
}

function pasteWidget() {
  if (!clipboard) return
  widgetIdCounter++
  const newWidget = { ...clipboard, id: widgetIdCounter, x: clipboard.x + 20, y: clipboard.y + 20 }
  widgets.value.push(newWidget)
  selectedWidgetId.value = widgetIdCounter
  pushHistory()
  message.success(t('scada.pasted'))  // FIXED: 原问题-中文硬编码
}

function duplicateWidget() {
  const w = widgets.value.find(w => w.id === selectedWidgetId.value)
  if (!w) return
  widgetIdCounter++
  const newWidget = { ...w, id: widgetIdCounter, x: w.x + 20, y: w.y + 20 }
  widgets.value.push(newWidget)
  selectedWidgetId.value = widgetIdCounter
  pushHistory()
}

function deleteSelected() {
  if (!selectedWidgetId.value || previewMode.value) return
  widgets.value = widgets.value.filter(w => w.id !== selectedWidgetId.value)
  chartData.value.delete(selectedWidgetId.value)
  selectedWidgetId.value = null
  pushHistory()
}

function exportAsImage() {
  if (!canvasRef.value) return
  const canvas = document.createElement('canvas')
  const rect = canvasRef.value.getBoundingClientRect()
  canvas.width = rect.width * 2
  canvas.height = rect.height * 2
  const ctx = canvas.getContext('2d')!
  ctx.scale(2, 2)
  ctx.fillStyle = '#0a0f1a'
  ctx.fillRect(0, 0, rect.width, rect.height)
  ctx.fillStyle = '#1a2a3a'
  for (let x = 0; x < rect.width; x += 20) {
    for (let y = 0; y < rect.height; y += 20) {
      ctx.beginPath()
      ctx.arc(x, y, 0.8, 0, Math.PI * 2)
      ctx.fill()
    }
  }
  widgets.value.forEach(w => {
    ctx.fillStyle = '#0d1520'
    ctx.strokeStyle = '#1a2a3a'
    ctx.lineWidth = 1
    ctx.beginPath()
    ctx.roundRect(w.x, w.y, w.w, w.h, 8)
    ctx.fill()
    ctx.stroke()
    ctx.fillStyle = '#e0f0ff'
    ctx.font = '12px sans-serif'
    ctx.textAlign = 'center'
    ctx.fillText(w.label, w.x + w.w / 2, w.y + w.h / 2)
    if (w.type === 'gauge' || w.type === 'tank') {
      const val = formatValue(w)
      ctx.font = 'bold 18px sans-serif'
      ctx.fillText(val, w.x + w.w / 2, w.y + w.h / 2 + 18)
    }
  })
  const url = canvas.toDataURL('image/png')
  const a = document.createElement('a')
  a.href = url
  a.download = `scada-${Date.now()}.png`
  a.click()
}

function onKeyDown(e: KeyboardEvent) {
  if (previewMode.value) return
  if (e.key === 'Delete' || e.key === 'Backspace') { if (selectedWidgetId.value) { e.preventDefault(); deleteSelected() } }
  else if (e.ctrlKey && e.key === 'z') { e.preventDefault(); undo() }
  else if (e.ctrlKey && e.key === 'y') { e.preventDefault(); redo() }
  else if (e.ctrlKey && e.key === 'c') { copyWidget() }
  else if (e.ctrlKey && e.key === 'v') { pasteWidget() }
  else if (e.ctrlKey && e.key === 'd') { e.preventDefault(); duplicateWidget() }
  else if (e.ctrlKey && e.key === 's') { e.preventDefault(); saveProject() }
}

const componentTypes = [
  { type: 'gauge', label: t('scada.gauge'), icon: h(NIcon, { component: OptionsSharp, size: 18 }), color: '#18a058' },
  { type: 'indicator', label: t('scada.indicator'), icon: h(NIcon, { component: BulbOutline, size: 18 }), color: '#f0c040' },
  { type: 'chart', label: t('scada.chart'), icon: h(NIcon, { component: StatsChartOutline, size: 18 }), color: '#667eea' },
  { type: 'switch', label: t('scada.switchCtrl'), icon: h(NIcon, { component: PowerOutline, size: 18 }), color: '#e8804c' },
  { type: 'tank', label: t('scada.tank'), icon: h(NIcon, { component: CubeSharp, size: 18 }), color: '#4fc3f7' },
  { type: 'label', label: t('scada.textLabel'), icon: h(NIcon, { component: Text, size: 18 }), color: '#90a4ae' },
]

const filteredDevices = computed(() => {
  if (!deviceSearch.value) return devices.value
  const q = deviceSearch.value.toLowerCase()
  return devices.value.filter(d => d.name.toLowerCase().includes(q) || d.device_id.toLowerCase().includes(q))
})

const deviceOptions = computed(() =>
  devices.value.map(d => ({ label: `${d.name} (${protocolLabel.value[d.protocol] || d.protocol})`, value: d.device_id }))
)

function widgetStyle(widget: any) {
  return { left: widget.x + 'px', top: widget.y + 'px', width: widget.w + 'px', height: widget.h + 'px' }
}

function gaugeColor(widget: any) {
  const pct = gaugePercent(widget)
  if (pct > 80) return '#d03050'
  if (pct > 60) return '#f0c040'
  return '#18a058'
}

function gaugePercent(widget: any) {
  const val = Number(getWidgetValue(widget)) || 0
  const min = widget.min ?? 0
  const max = widget.max ?? 100
  return Math.min(100, Math.max(0, ((val - min) / (max - min)) * 100))
}

function gaugeDash(widget: any) {
  const pct = gaugePercent(widget)
  const arc = 235.5
  return `${arc * pct / 100} ${arc}`
}

function tankPercent(widget: any) {
  return gaugePercent(widget)
}

function tankColor(widget: any) {
  const pct = tankPercent(widget)
  if (pct > 80) return '#d03050'
  if (pct > 60) return '#f0c040'
  return '#4fc3f7'
}

function getChartOption(widget: any) {
  const arr = chartData.value.get(widget.id) || []
  const times = arr.map(d => {
    const dt = new Date(d.time)
    return `${String(dt.getHours()).padStart(2, '0')}:${String(dt.getMinutes()).padStart(2, '0')}:${String(dt.getSeconds()).padStart(2, '0')}`
  })
  const values = arr.map(d => d.value)
  return {
    grid: { left: 35, right: 8, top: 8, bottom: 22 },
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: times, axisLabel: { fontSize: 9, color: '#607d8b' }, show: arr.length > 0 },
    yAxis: { type: 'value', axisLabel: { fontSize: 9, color: '#607d8b' }, splitLine: { lineStyle: { color: '#1a2a3a' } }, show: arr.length > 0 },
    series: [{
      type: 'line', data: values, smooth: true, symbol: 'none',
      itemStyle: { color: '#667eea' }, areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: 'rgba(102,126,234,0.3)' }, { offset: 1, color: 'rgba(102,126,234,0.02)' }] } },
    }],
  }
}

function onEditDeviceChange(deviceId: string) {
  editForm.value.pointName = null
  if (!deviceId) { editPointOptions.value = []; return }
  const device = devices.value.find(d => d.device_id === deviceId)
  // FIXED: 原问题-device?.points?.map(...)后链式调用不安全，改为(device?.points ?? []).map(...)
  editPointOptions.value = (device?.points ?? []).map((p: any) => ({ label: `${p.name} (${p.unit || '-'})`, value: p.name }))
  applyProp()
}

function applyProp() {
  const idx = widgets.value.findIndex(w => w.id === editForm.value.id)
  if (idx >= 0) {
    const device = devices.value.find(d => d.device_id === editForm.value.deviceId)
    widgets.value[idx] = { ...widgets.value[idx], ...editForm.value, deviceName: device?.name || '' }
  }
}

async function fetchDevices() {
  try {
    const data = await deviceApi.list({ page: 1, size: 200 })
    devices.value = data?.data ?? []
  } catch {
    message.warning(t('scada.fetchDevicesFailed'))  // FIXED: 原问题-中文硬编码
  }
}

async function onSelectDevice(device: any) {
  expandedDevice.value = expandedDevice.value === device.device_id ? null : device.device_id
  if (expandedDevice.value !== device.device_id) return
  try {
    const data = await deviceApi.getPoints(device.device_id)
    if (Array.isArray(data)) currentDevicePoints.value = data
    else if (data && typeof data === 'object') currentDevicePoints.value = Object.entries(data).map(([name, info]: [string, any]) => ({ name, ...(typeof info === 'object' ? info : { value: info }) }))
    else currentDevicePoints.value = device.points || []
    await fetchPointValues(device.device_id)
  } catch (e) {
    currentDevicePoints.value = device.points || []
    console.warn('Failed to fetch device points:', e)  // FIXED: 原问题-硬编码中文label
  }
}

async function fetchPointValues(deviceId: string) {
  try {
    const data = await deviceApi.getPoints(deviceId)
    if (data) pointValues.value = { ...pointValues.value, [deviceId]: data }
  } catch (e) {
    console.warn('获取设备测点值失败:', deviceId, e)
  }
}

async function refreshAllValues() {
  const deviceIds = new Set<string>()
  widgets.value.forEach(w => { if (w.deviceId) deviceIds.add(w.deviceId) })
  for (const deviceId of deviceIds) await fetchPointValues(deviceId)
  updateWidgetValues()
}

function updateWidgetValues() {
  widgets.value.forEach(w => {
    if (w.deviceId && w.pointName) {
      const val = pointValues.value[w.deviceId]?.[w.pointName]?.value ?? pointValues.value[w.deviceId]?.[w.pointName]
      if (val !== undefined && val !== null) {
        if (w.type === 'gauge' || w.type === 'tank') w.value = Number(val)
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
  const type = isBool ? 'indicator' : 'gauge'
  const device = devices.value.find(d => d.device_id === expandedDevice.value)
  if (!device) return
  widgetIdCounter++
  widgets.value.push({
    id: widgetIdCounter, type, label: pt.name, deviceId: device.device_id, deviceName: device.name,
    pointName: pt.name, unit: pt.unit || '', min: 0, max: 100,
    x: 20 + (widgets.value.length % 5) * 170, y: 20 + Math.floor(widgets.value.length / 5) * 150,
    w: 150, h: 150,
    value: pointValues.value[device.device_id]?.[pt.name]?.value ?? (isBool ? false : 0),
  })
  pushHistory()
  message.success(t('scada.addedPoint', { name: pt.name }))  // FIXED: 原问题-中文硬编码
}

function addWidgetManual(type: string) {
  widgetIdCounter++
  const comp = componentTypes.find(c => c.type === type)
  widgets.value.push({
    id: widgetIdCounter, type, label: comp?.label || type, deviceId: null, deviceName: '',
    pointName: null, unit: '', min: 0, max: 100,
    x: 20 + (widgets.value.length % 5) * 170, y: 20 + Math.floor(widgets.value.length / 5) * 150,
    w: type === 'chart' ? 300 : type === 'label' ? 120 : 150, h: type === 'chart' ? 180 : type === 'label' ? 40 : 150,
    value: type === 'switch' ? false : type === 'indicator' ? false : 0,
  })
  selectedWidgetId.value = widgetIdCounter
  pushHistory()
}

function removeWidget(id: number) {
  const widget = widgets.value.find(w => w.id === id)
  dialog.warning({  // FIXED: 原问题-中文硬编码
    title: t('scada.confirmDelete'),
    content: t('scada.confirmDeleteContent', { name: widget?.label || String(id) }),
    positiveText: t('scada.confirmDelete'),
    negativeText: t('scada.cancel'),
    onPositiveClick: () => {
      widgets.value = widgets.value.filter(w => w.id !== id)
      chartData.value.delete(id)
      if (selectedWidgetId.value === id) selectedWidgetId.value = null
      pushHistory()
    },
  })
}

function selectWidget(widget: any) {
  if (previewMode.value) return
  selectedWidgetId.value = widget.id
  editForm.value = { ...widget }
  if (widget.deviceId) onEditDeviceChange(widget.deviceId)
}

function onCanvasClick() { selectedWidgetId.value = null }

function editWidget(widget: any) {
  selectedWidgetId.value = widget.id
  editForm.value = { ...widget }
  if (widget.deviceId) onEditDeviceChange(widget.deviceId)
}

async function onSwitchChange(widget: any, value: boolean) {
  if (widget.deviceId && widget.pointName) {
    dialog.warning({  // FIXED: 原问题-中文硬编码
      title: t('scada.confirmOperation'), content: t('scada.writeConfirmContent', { device: widget.deviceName || widget.deviceId, label: widget.label, action: value ? t('scada.actionOn') : t('scada.actionOff') }),
      positiveText: t('scada.confirm'), negativeText: t('scada.cancel'),
      onPositiveClick: async () => {
        try { await deviceApi.writePoint(widget.deviceId, widget.pointName, value); message.success(t('scada.operationSuccess', { action: value ? t('scada.actionOn') : t('scada.actionOff'), label: widget.label })) }  // FIXED: 原问题-中文硬编码
        catch { message.error(t('scada.operationFailed')); widget.value = !value }  // FIXED: 原问题-中文硬编码
      },
      onNegativeClick: () => { widget.value = !value },
    })
  }
}

function getWidgetValue(widget: any): any {
  if (!widget.deviceId || !widget.pointName) return widget.value
  const val = pointValues.value[widget.deviceId]?.[widget.pointName]?.value ?? pointValues.value[widget.deviceId]?.[widget.pointName]
  return val !== undefined ? val : widget.value
}

function formatValue(widget: any): string {
  const val = getWidgetValue(widget)
  if (typeof val === 'number') return val.toFixed(1)
  return String(val ?? '-')
}

function startDrag(e: MouseEvent, widget: any) {
  if (previewMode.value) return
  const rect = canvasRef.value!.getBoundingClientRect()
  dragging = { widget, offsetX: e.clientX / zoom.value - rect.left / zoom.value - widget.x, offsetY: e.clientY / zoom.value - rect.top / zoom.value - widget.y }
  const onMouseMove = (ev: MouseEvent) => {
    if (dragging) {
      const r = canvasRef.value!.getBoundingClientRect()
      dragging.widget.x = Math.max(0, Math.round((ev.clientX / zoom.value - r.left / zoom.value - dragging.offsetX) / 10) * 10)
      dragging.widget.y = Math.max(0, Math.round((ev.clientY / zoom.value - r.top / zoom.value - dragging.offsetY) / 10) * 10)
    }
  }
  const onMouseUp = () => { dragging = null; document.removeEventListener('mousemove', onMouseMove); document.removeEventListener('mouseup', onMouseUp) }
  document.addEventListener('mousemove', onMouseMove)
  document.addEventListener('mouseup', onMouseUp)
}

function startResize(e: MouseEvent, widget: any) {
  const startW = widget.w, startH = widget.h, startX = e.clientX, startY = e.clientY
  const onMouseMove = (ev: MouseEvent) => {
    widget.w = Math.max(60, Math.round((startW + (ev.clientX - startX) / zoom.value) / 10) * 10)
    widget.h = Math.max(40, Math.round((startH + (ev.clientY - startY) / zoom.value) / 10) * 10)
  }
  const onMouseUp = () => { document.removeEventListener('mousemove', onMouseMove); document.removeEventListener('mouseup', onMouseUp) }
  document.addEventListener('mousemove', onMouseMove)
  document.addEventListener('mouseup', onMouseUp)
}

async function saveProject() {
  saving.value = true
  try {
    await scadaApi.saveProject({ name: 'default', widgets: widgets.value })
    localStorage.setItem('scada-project', JSON.stringify({ widgets: widgets.value }))
    dirty.value = false
    message.success(t('scada.savedToServer'))  // FIXED: 原问题-中文硬编码
  } catch {
    localStorage.setItem('scada-project', JSON.stringify({ widgets: widgets.value }))
    dirty.value = false
    message.warning(t('scada.saveFailedLocal'))  // FIXED: 原问题-中文硬编码
  } finally {
    saving.value = false
  }
}

function loadProject() { fileInputRef.value?.click() }

function onFileLoad(e: Event) {
  const file = (e.target as HTMLInputElement).files?.[0]
  if (!file) return
  const reader = new FileReader()
  reader.onload = (ev) => {
    try {
      const data = JSON.parse(ev.target?.result as string)
      // FIXED: 原问题-data.widgets可能不是数组，添加Array.isArray检查
      if (Array.isArray(data.widgets) && data.widgets.length) { widgets.value = data.widgets; widgetIdCounter = Math.max(...data.widgets.map((w: any) => w.id), 0); message.success(t('scada.loadedComponents', { count: data.widgets.length })) }  // FIXED: 原问题-中文硬编码
    } catch { message.error(t('scada.fileFormatError')) }  // FIXED: 原问题-中文硬编码
  }
  reader.readAsText(file)
  ;(e.target as HTMLInputElement).value = ''
}

watch(previewMode, (val) => { if (val) { selectedWidgetId.value = null; refreshAllValues() } })

onMounted(async () => {
  await fetchDevices()
  try {
    const data = await scadaApi.getProject('default')
    if (data?.widgets?.length) {
      widgets.value = data.widgets
      // FIXED: 原问题-data.widgets.map(...)添加空值保护
      widgetIdCounter = Math.max(...(data.widgets ?? []).map((w: any) => w.id), 0)
    }
  } catch {
    const saved = localStorage.getItem('scada-project')
    if (saved) {
      try {
        const data = JSON.parse(saved)
        if (data.widgets?.length) {
          widgets.value = data.widgets
          // FIXED: 原问题-data.widgets.map(...)添加空值保护
          widgetIdCounter = Math.max(...(data.widgets ?? []).map((w: any) => w.id), 0)
        }
      } catch (e) {
        console.warn('解析本地存储项目失败:', e)
      }
    }
  }
  pageLoading.value = false
  pushHistory()
  refreshTimer = setInterval(refreshAllValues, 5000)
  document.addEventListener('keydown', onKeyDown)
})

onUnmounted(() => {
  if (refreshTimer) clearInterval(refreshTimer)
  document.removeEventListener('keydown', onKeyDown)
  localStorage.setItem('scada-project', JSON.stringify({ widgets: widgets.value }))
})

watch(widgets, () => { dirty.value = true }, { deep: true })

onBeforeRouteLeave((_to, _from, next) => {
  if (dirty.value) {
    dialog.warning({  // FIXED: 原问题-中文硬编码
      title: t('scada.unsavedChanges'),
      content: t('scada.unsavedChangesContent'),
      positiveText: t('scada.leave'),
      negativeText: t('scada.stay'),
      onPositiveClick: () => next(),
      onNegativeClick: () => next(false),
    })
  } else {
    next()
  }
})

onBeforeUnmount(() => {
  window.onbeforeunload = null
})

if (typeof window !== 'undefined') {
  window.onbeforeunload = (e) => {
    if (dirty.value) {
      e.preventDefault()
    }
  }
}
</script>

<style scoped>
.scada-page {
  display: flex;
  flex-direction: column;
  height: calc(100vh - 80px);
  background: #0a0f1a;
  border-radius: 8px;
  overflow: hidden;
}

.scada-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 16px;
  background: #0d1520;
  border-bottom: 1px solid #1a2a3a;
}

.header-left { display: flex; align-items: center; gap: 8px; }
.header-title { font-weight: 700; font-size: 15px; color: #e0f0ff; }
.header-actions { display: flex; gap: 6px; }

.scada-body { display: flex; flex: 1; overflow: hidden; }

.toolbox {
  width: 200px;
  min-width: 200px;
  background: #0d1520;
  border-right: 1px solid #1a2a3a;
  padding: 12px;
  overflow-y: auto;
}

.toolbox-title { font-size: 11px; font-weight: 600; color: #e0f0ff; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; }

.toolbox-items { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }

.toolbox-item {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  padding: 8px 4px;
  border-radius: 6px;
  cursor: pointer;
  transition: background 0.15s;
  border: 1px solid transparent;
}
.toolbox-item:hover { background: rgba(79, 195, 247, 0.08); border-color: rgba(79, 195, 247, 0.15); }

.tb-icon { width: 32px; height: 32px; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-size: 16px; color: #e0f0ff; }
.tb-label { font-size: 10px; color: #e0f0ff; }

.device-tree { max-height: 300px; overflow-y: auto; }

.dt-device-header {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  border-radius: 4px;
  cursor: pointer;
  transition: background 0.15s;
  font-size: 12px;
  color: #e0f0ff;
}
.dt-device-header:hover { background: rgba(79, 195, 247, 0.08); }
.dt-device-header.active { background: rgba(79, 195, 247, 0.12); }

.dt-dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
.dt-name { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

.dt-points { padding-left: 16px; }
.dt-point {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 4px 8px;
  border-radius: 4px;
  font-size: 11px;
  color: #e0f0ff;
  cursor: pointer;
  transition: background 0.15s;
}
.dt-point:hover { background: rgba(79, 195, 247, 0.08); color: #e0f0ff; }
.dt-empty { font-size: 11px; color: #e0f0ff; padding: 8px; text-align: center; }

.canvas-area { flex: 1; display: flex; flex-direction: column; overflow: hidden; }

.canvas-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 12px;
  background: #0d1520;
  border-bottom: 1px solid #1a2a3a;
}

.zoom-label { font-size: 11px; color: #e0f0ff; }

.scada-canvas {
  flex: 1;
  position: relative;
  background-image: radial-gradient(circle, #1a2a3a 1px, transparent 1px);
  background-size: 20px 20px;
  background-color: #0a0f1a;
  overflow: auto;
  min-height: 600px;
}

.scada-canvas.preview-mode { background-color: #080c14; }

.empty-hint { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); text-align: center; color: #8faabe; }
.empty-icon-svg { font-size: 48px; margin-bottom: 12px; color: #4fc3f7; }
.empty-text { font-size: 14px; }

.scada-widget {
  position: absolute;
  background: #0d1520;
  border: 1px solid #1a2a3a;
  border-radius: 8px;
  padding: 8px;
  cursor: move;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.3);
  transition: border-color 0.2s;
  user-select: none;
  overflow: hidden;
}
.scada-widget:hover { border-color: rgba(79, 195, 247, 0.3); }
.scada-widget.selected { border-color: #4fc3f7; box-shadow: 0 0 0 1px #4fc3f7, 0 2px 16px rgba(79, 195, 247, 0.2); }

.widget-actions {
  position: absolute;
  top: 2px;
  right: 2px;
  display: flex;
  gap: 2px;
}
.wa-btn { width: 20px; height: 20px; display: flex; align-items: center; justify-content: center; border-radius: 4px; cursor: pointer; font-size: 11px; color: #8faabe; transition: all 0.15s; padding: 2px; }
.wa-btn:hover { color: #e0f0ff; background: rgba(79, 195, 247, 0.15); }
.wa-del:hover { color: #d03050; background: rgba(208, 48, 80, 0.15); }

.resize-handle {
  position: absolute;
  bottom: 0;
  right: 0;
  width: 14px;
  height: 14px;
  cursor: nwse-resize;
  background: linear-gradient(135deg, transparent 50%, #4fc3f7 50%);
  border-radius: 0 0 8px 0;
  opacity: 0.5;
}
.resize-handle:hover { opacity: 1; }

.w-gauge { height: 100%; display: flex; align-items: center; justify-content: center; }
.gauge-svg { width: 100%; height: 100%; max-width: 140px; }

.w-indicator { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 6px; height: 100%; }
.ind-light { width: 36px; height: 36px; border-radius: 50%; transition: all 0.3s; }
.ind-label { font-size: 12px; color: #e0f0ff; font-weight: 600; }
.ind-status { font-size: 10px; color: #8faabe; }

.w-chart { height: 100%; display: flex; flex-direction: column; }
.chart-title { font-size: 11px; font-weight: 600; color: #e0f0ff; margin-bottom: 4px; }
.chart-body { flex: 1; min-height: 60px; background: #080c14; border-radius: 4px; }

.w-switch { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px; height: 100%; }
.sw-label { font-size: 12px; color: #e0f0ff; font-weight: 600; }
.sw-status { font-size: 10px; color: #8faabe; transition: color 0.2s; }
.sw-status.on { color: #18a058; }

.w-tank { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 4px; height: 100%; }
.tank-body { width: 50px; height: 60px; border: 2px solid #1a3a5a; border-radius: 4px 4px 8px 8px; position: relative; overflow: hidden; background: #080c14; }
.tank-fill { position: absolute; bottom: 0; left: 0; right: 0; transition: height 0.5s ease, background 0.3s; border-radius: 0 0 6px 6px; }
.tank-val { font-size: 16px; font-weight: bold; color: #e0f0ff; }
.tank-label { font-size: 10px; color: #8faabe; }

.w-label { display: flex; align-items: center; justify-content: center; height: 100%; font-size: 14px; font-weight: 600; color: #e0f0ff; }

.props-panel {
  width: 220px;
  min-width: 220px;
  background: #0d1520;
  border-left: 1px solid #1a2a3a;
  padding: 12px;
  overflow-y: auto;
}

.props-title { font-size: 11px; font-weight: 600; color: #8faabe; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 12px; }
.props-body { display: flex; flex-direction: column; gap: 8px; }

.prop-row { display: flex; flex-direction: column; gap: 2px; }
.prop-label { font-size: 10px; color: #8faabe; }
.prop-val { font-size: 12px; color: #b8c9d1; }
</style>
