<template>
  <div class="scada-page">
    <n-card title="Web 组态 (SCADA)">
      <template #header-extra>
        <n-space>
          <n-button type="primary" @click="saveProject">保存</n-button>
          <n-button @click="previewMode = !previewMode">{{ previewMode ? '编辑模式' : '预览模式' }}</n-button>
          <n-button @click="addWidget('gauge')">仪表盘</n-button>
          <n-button @click="addWidget('chart')">图表</n-button>
          <n-button @click="addWidget('switch')">开关</n-button>
          <n-button @click="addWidget('indicator')">指示灯</n-button>
        </n-space>
      </template>
      <div class="scada-canvas" ref="canvasRef" :class="{ 'preview-mode': previewMode }">
        <div v-if="widgets.length === 0" class="empty-hint">
          点击上方按钮添加组态组件
        </div>
        <div
          v-for="widget in widgets"
          :key="widget.id"
          class="scada-widget"
          :style="{ left: widget.x + 'px', top: widget.y + 'px', width: widget.w + 'px', height: widget.h + 'px' }"
          @mousedown="startDrag($event, widget)"
        >
          <div v-if="widget.type === 'gauge'" class="widget-gauge">
            <div class="gauge-value">{{ widget.value ?? 0 }}</div>
            <div class="gauge-label">{{ widget.label }}</div>
          </div>
          <div v-else-if="widget.type === 'chart'" class="widget-chart">
            <div class="chart-placeholder">📊 {{ widget.label }}</div>
          </div>
          <div v-else-if="widget.type === 'switch'" class="widget-switch">
            <n-switch v-model:value="widget.value" :disabled="previewMode" />
            <span style="margin-left: 8px">{{ widget.label }}</span>
          </div>
          <div v-else-if="widget.type === 'indicator'" class="widget-indicator">
            <div class="indicator-dot" :class="{ on: widget.value }"></div>
            <span style="margin-left: 8px">{{ widget.label }}</span>
          </div>
          <div v-if="!previewMode" class="widget-actions">
            <n-button size="tiny" quaternary @click="removeWidget(widget.id)">✕</n-button>
          </div>
        </div>
      </div>
    </n-card>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { NCard, NButton, NSpace, NSwitch } from 'naive-ui'

const canvasRef = ref<HTMLElement | null>(null)
const previewMode = ref(false)
const widgets = ref<any[]>([])

let widgetIdCounter = 0
let dragging: any = null

function addWidget(type: string) {
  widgetIdCounter++
  widgets.value.push({
    id: widgetIdCounter,
    type,
    label: `组件${widgetIdCounter}`,
    x: 20 + (widgets.value.length % 5) * 160,
    y: 20 + Math.floor(widgets.value.length / 5) * 120,
    w: type === 'chart' ? 300 : 140,
    h: type === 'chart' ? 200 : 100,
    value: type === 'switch' ? false : type === 'indicator' ? false : 0,
  })
}

function removeWidget(id: number) {
  widgets.value = widgets.value.filter(w => w.id !== id)
}

function startDrag(e: MouseEvent, widget: any) {
  if (previewMode.value) return
  const rect = canvasRef.value!.getBoundingClientRect()
  dragging = { widget, offsetX: e.clientX - rect.left - widget.x, offsetY: e.clientY - rect.top - widget.y }
  const onMouseMove = (ev: MouseEvent) => {
    if (dragging) {
      const r = canvasRef.value!.getBoundingClientRect()
      dragging.widget.x = ev.clientX - r.left - dragging.offsetX
      dragging.widget.y = ev.clientY - r.top - dragging.offsetY
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
  const data = JSON.stringify({ widgets: widgets.value }, null, 2)
  const blob = new Blob([data], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = 'scada-project.json'
  a.click()
  URL.revokeObjectURL(url)
}
</script>

<style scoped>
.scada-page { padding: 16px; }
.scada-canvas {
  position: relative; width: 100%; height: 600px;
  background: #f0f2f5; border: 2px dashed #d9d9d9; border-radius: 8px; overflow: hidden;
}
.scada-canvas.preview-mode { border-style: solid; background: #e8e8e8; }
.empty-hint { display: flex; align-items: center; justify-content: center; height: 100%; color: #999; font-size: 16px; }
.scada-widget {
  position: absolute; background: #fff; border: 1px solid #e0e0e0; border-radius: 6px;
  padding: 8px; cursor: move; box-shadow: 0 2px 8px rgba(0,0,0,0.08);
}
.widget-actions { position: absolute; top: 2px; right: 2px; }
.widget-gauge { text-align: center; }
.gauge-value { font-size: 28px; font-weight: bold; color: #18a058; }
.gauge-label { font-size: 12px; color: #666; margin-top: 4px; }
.widget-chart { height: 100%; }
.chart-placeholder { display: flex; align-items: center; justify-content: center; height: 100%; font-size: 14px; color: #666; }
.widget-switch, .widget-indicator { display: flex; align-items: center; height: 100%; }
.indicator-dot {
  width: 16px; height: 16px; border-radius: 50%; background: #d9d9d9; transition: background 0.3s;
}
.indicator-dot.on { background: #18a058; box-shadow: 0 0 8px #18a05880; }
</style>
