<template>
  <n-space vertical :size="16">
    <n-card :title="t('dataQuery.title')" :bordered="false">
      <n-space vertical :size="12">
        <n-form inline ref="queryFormRef" :model="queryForm" :rules="queryRules" label-placement="left" label-width="80">
          <n-form-item :label="t('dataQuery.device')" path="device_id">
            <n-select v-model:value="queryForm.device_id" :options="deviceOptions" :placeholder="t('dataQuery.selectDevice')" style="width: 200px" filterable />
          </n-form-item>
          <n-form-item :label="t('dataQuery.point')" path="point_name">
            <n-select v-model:value="queryForm.point_name" :options="pointOptions" :placeholder="t('dataQuery.selectPoint')" style="width: 160px" />
          </n-form-item>
          <n-form-item :label="t('dataQuery.timeRange')" path="range">
            <n-select v-model:value="queryForm.range" :options="rangeOptions" style="width: 120px" />
          </n-form-item>
          <n-form-item v-if="queryForm.range === 'custom'" :label="t('dataQuery.timeRange')">
            <n-date-picker v-model:value="customDateRange" type="datetimerange" clearable style="width: 380px" />
          </n-form-item>
          <n-form-item :label="t('dataQuery.aggregate')">
            <n-select v-model:value="queryForm.aggregate" :options="aggregateOptions" :placeholder="t('dataQuery.none')" clearable style="width: 120px" />
          </n-form-item>
          <n-form-item :label="t('dataQuery.maxRecords')">
            <!-- FIXED-Pagination: 改为 n-select 提供 limit 可选值，max 支持更大值 -->
            <n-select v-model:value="queryForm.limit" :options="limitOptions" style="width: 140px" />
          </n-form-item>
          <n-button type="primary" @click="handleQuery" :loading="loading">{{ t('dataQuery.query') }}</n-button>
          <n-dropdown trigger="click" :options="exportOptions" @select="(key: string) => handleExport(key)">
            <n-button :loading="exporting">{{ t('dataQuery.exportData') }}</n-button>
          </n-dropdown>
          <n-button type="info" @click="handleMultiPointQuery" :loading="multiLoading" :disabled="!queryForm.device_id || multiPointNames.length < 2">{{ t('dataQuery.compareQuery') }}</n-button>
          <n-button type="warning" @click="handleCorrelation" :loading="correlationLoading" :disabled="!queryForm.device_id || multiPointNames.length < 2">{{ t('dataQuery.correlationAnalysis') }}</n-button>
          <!-- 修复5: 保存查询/收藏 -->
          <n-button @click="openSaveQueryModal" :disabled="!queryForm.device_id || !queryForm.point_name">{{ t('dataQuery.saveQuery') }}</n-button>
          <n-select
            v-model:value="selectedSavedQuery"
            :options="savedQueryOptions"
            :placeholder="t('dataQuery.myFavorites')"
            clearable
            size="small"
            style="width: 200px"
            @update:value="onSavedQuerySelect"
          />
          <n-button v-if="selectedSavedQuery" size="small" quaternary type="error" @click="deleteSavedQuery">{{ t('common.delete') }}</n-button>
        </n-form>
        <n-space align="center" style="margin-top: 4px">
          <n-text depth="3">{{ t('dataQuery.multiPointSelect') }}:</n-text>
          <n-select v-model:value="multiPointNames" :options="pointOptions" :placeholder="t('dataQuery.selectMultiPoint')" multiple filterable style="min-width: 400px" />
        </n-space>
        <!-- 修复12: 多设备对比——选择多个设备的同一测点，在同一图表中以不同颜色曲线展示 -->
        <n-space align="center" style="margin-top: 4px">
          <n-text depth="3">{{ t('dataQuery.multiDeviceSelect') }}:</n-text>
          <n-select v-model:value="multiDeviceIds" :options="deviceOptions" :placeholder="t('dataQuery.selectMultiDevice')" multiple filterable style="min-width: 400px" />
          <n-text depth="3">{{ t('dataQuery.multiDevicePoint') }}:</n-text>
          <n-input v-model:value="multiDevicePointName" :placeholder="t('dataQuery.multiDevicePoint')" style="width: 180px" />
          <n-button type="success" @click="handleMultiDeviceCompare" :loading="multiDeviceLoading" :disabled="multiDeviceIds.length < 2 || !multiDevicePointName.trim()">{{ t('dataQuery.multiDeviceCompareBtn') }}</n-button>
        </n-space>
        <!-- 修复8: SQL查询模式切换 -->
        <n-space align="center" style="margin-top: 4px">
          <n-switch v-model:value="sqlMode" size="small" />
          <n-text depth="3" style="font-size: 12px">{{ t('dataQuery.sqlMode') }}</n-text>
          <n-text depth="3" style="font-size: 11px">{{ t('dataQuery.sqlModeHint') }}</n-text>
        </n-space>
        <n-space v-if="sqlMode" align="flex-end" style="margin-top: 4px">
          <n-input v-model:value="sqlText" type="textarea" :placeholder="t('dataQuery.sqlPlaceholder')" :rows="2" style="width: 600px; max-width: 100%; font-family: monospace" @keyup.ctrl.enter="applySqlQuery" />
          <n-button type="primary" @click="applySqlQuery">{{ t('dataQuery.sqlApply') }}</n-button>
        </n-space>
      </n-space>
    </n-card>

    <!-- 修复5: 保存查询弹窗 -->
    <n-modal v-model:show="showSaveQueryModal" preset="dialog" :title="t('dataQuery.saveQuery')" style="width: 400px; max-width: 95vw" :close-on-esc="true" :auto-focus="true" :close-on-esc-aria-label="t('common.closeDialog')">
      <n-input v-model:value="newQueryName" :placeholder="t('dataQuery.queryNamePlaceholder')" @keyup.enter="confirmSaveQuery" />
      <template #action>
        <n-button @click="showSaveQueryModal = false">{{ t('common.cancel') }}</n-button>
        <n-button type="primary" @click="confirmSaveQuery">{{ t('common.save') }}</n-button>
      </template>
    </n-modal>

    <!-- 修复6: 添加注解弹窗 -->
    <n-modal v-model:show="showAnnotationModal" preset="dialog" :title="t('dataQuery.addAnnotation')" style="width: 420px; max-width: 95vw" :close-on-esc="true" :auto-focus="true" :close-on-esc-aria-label="t('common.closeDialog')">
      <n-space vertical :size="12">
        <n-form-item :label="t('dataQuery.annotationTime')">
          <n-input v-model:value="newAnnotationTime" placeholder="HH:MM:SS" />
        </n-form-item>
        <n-form-item :label="t('dataQuery.annotationText')">
          <n-input v-model:value="newAnnotationText" :placeholder="t('dataQuery.annotationPlaceholder')" @keyup.enter="confirmAddAnnotation" />
        </n-form-item>
        <!-- 已有注解列表 -->
        <div v-if="chartAnnotations.length" style="max-height: 200px; overflow-y: auto">
          <n-tag v-for="anno in chartAnnotations" :key="anno.id" closable size="small" style="margin: 2px" @close="removeAnnotation(anno.id)">
            {{ anno.time }}: {{ anno.text }}
          </n-tag>
        </div>
      </n-space>
      <template #action>
        <n-button @click="showAnnotationModal = false">{{ t('common.cancel') }}</n-button>
        <n-button type="primary" @click="confirmAddAnnotation">{{ t('common.save') }}</n-button>
      </template>
    </n-modal>

    <!-- 修复16: 导出列选择弹窗 -->
    <n-modal v-model:show="showExportColumnsModal" preset="dialog" :title="t('dataQuery.selectColumns')" style="width: 420px; max-width: 95vw" :close-on-esc="true" :auto-focus="true" :close-on-esc-aria-label="t('common.closeDialog')">
      <n-space vertical :size="12">
        <n-text depth="3">{{ t('dataQuery.selectColumnsHint') }}</n-text>
        <n-checkbox-group v-model:value="selectedExportColumns">
          <n-space>
            <n-checkbox v-for="opt in exportColumnOptions" :key="opt.key" :value="opt.key" :label="opt.label" />
          </n-space>
        </n-checkbox-group>
      </n-space>
      <template #action>
        <n-button @click="showExportColumnsModal = false">{{ t('common.cancel') }}</n-button>
        <n-button type="primary" @click="confirmExportWithColumns">{{ t('common.confirm') }}</n-button>
      </template>
    </n-modal>

    <n-grid :cols="2" :x-gap="16">
      <n-gi>
        <n-card :bordered="false">
          <template #header>
            <n-space align="center" justify="space-between" style="width: 100%">
              <n-space align="center" size="small">
                <span>{{ t('dataQuery.chart') }}</span>
                <!-- FIXED-Downsample: 降采样提示 -->
                <n-tag v-if="downsampleInfo" size="small" type="info" round>
                  {{ downsampleHintText }}
                </n-tag>
              </n-space>
              <n-space align="center" size="small">
                <!-- 修复10: 图表类型切换 -->
                <n-button-group size="small">
                  <n-button v-for="opt in chartTypeOptions" :key="opt.value" :type="chartType === opt.value ? 'primary' : 'default'" @click="chartType = opt.value as any">{{ opt.label }}</n-button>
                </n-button-group>
                <!-- 修复11: 时间对比开关 -->
                <n-text depth="3" style="font-size: 12px">{{ t('dataQuery.timeCompare') }}</n-text>
                <n-switch v-model:value="compareEnabled" size="small" />
                <n-select v-if="compareEnabled" v-model:value="compareRange" :options="rangeOptions.filter(r => r.value !== 'custom')" size="small" style="width: 100px" />
                <!-- 修复6: 添加注解按钮 -->
                <n-button size="small" @click="openAnnotationModal">{{ t('dataQuery.addAnnotation') }}</n-button>
                <n-button v-if="chartAnnotations.length" size="small" quaternary type="error" @click="clearAllAnnotations">{{ t('dataQuery.clearAnnotations') }}</n-button>
              </n-space>
            </n-space>
          </template>
          <v-chart ref="chartRef" :option="chartOption" autoresize style="height: 360px" />
        </n-card>
      </n-gi>
      <n-gi>
        <n-card :title="t('dataQuery.stats')" :bordered="false">
          <n-descriptions label-placement="left" :column="1" bordered v-if="stats">
            <n-descriptions-item :label="t('dataQuery.dataPointCount')">{{ stats.count }}</n-descriptions-item>
            <n-descriptions-item :label="t('dataQuery.maxValue')">{{ stats.max?.toFixed(4) }}</n-descriptions-item>
            <n-descriptions-item :label="t('dataQuery.minValue')">{{ stats.min?.toFixed(4) }}</n-descriptions-item>
            <n-descriptions-item :label="t('dataQuery.avgValue')">{{ stats.avg?.toFixed(4) }}</n-descriptions-item>
            <n-descriptions-item :label="t('dataQuery.stddevValue')">{{ stats.stddev?.toFixed(4) }}</n-descriptions-item>
            <n-descriptions-item :label="t('dataQuery.varianceValue')">{{ stats.variance?.toFixed(4) }}</n-descriptions-item>
            <n-descriptions-item :label="t('dataQuery.p95Value')">{{ stats.p95?.toFixed(4) }}</n-descriptions-item>
            <n-descriptions-item :label="t('dataQuery.lastValue')">{{ stats.last?.toFixed(4) }}</n-descriptions-item>
          </n-descriptions>
          <n-empty v-else :description="t('dataQuery.pleaseQuery')" />
        </n-card>
      </n-gi>
    </n-grid>

    <n-card :title="t('dataQuery.rawData')" :bordered="false">
      <n-data-table :columns="dataColumns" :data="tableData" :bordered="false" size="small" :pagination="pagination" :loading="loading" remote>
        <template #empty>
          <n-empty v-if="!loading" :description="queryResult.length ? t('common.noData') : t('dataQuery.pleaseQuery')" style="padding: 24px 0" />
        </template>
      </n-data-table>
    </n-card>

    <n-card v-if="multiPointResult.length" :title="t('dataQuery.compareChart')" :bordered="false">
      <v-chart :option="multiPointChartOption" autoresize style="height: 360px" />
    </n-card>

    <!-- 修复12: 多设备对比图 -->
    <n-card v-if="multiDeviceResult.length" :title="t('dataQuery.multiDeviceChart')" :bordered="false">
      <template #header-extra>
        <n-text depth="3" style="font-size: 12px">{{ multiDeviceSummaryText }}</n-text>
      </template>
      <v-chart :option="multiDeviceChartOption" autoresize style="height: 360px" />
    </n-card>

    <n-card v-if="correlationResult" :title="t('dataQuery.correlationMatrix')" :bordered="false">
      <v-chart :option="correlationChartOption" autoresize style="height: 400px" />
    </n-card>
  </n-space>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { use } from 'echarts/core'
import { LineChart, BarChart, HeatmapChart } from 'echarts/charts'
import { TitleComponent, TooltipComponent, GridComponent, DataZoomComponent, VisualMapComponent, MarkPointComponent, MarkAreaComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import VChart from 'vue-echarts'
import { deviceApi, dataApi, type Device } from '@/api'
import { t } from '@/i18n'
import { extractError } from '@/utils/errorCodes'
import { message, dialog } from '@/utils/discreteApi'
import { formatDateTime } from '@/utils/datetime'
// [AUDIT-FIX] 严重-1: 暗色模式适配
import { useChartTheme } from '@/composables/useChartTheme'
import { CATEGORICAL_PALETTE, SEMANTIC_COLORS, BRAND_COLORS, CHART_ROLE_COLORS, DIVERGING_PALETTE } from '@/constants/chartPalette'

use([LineChart, BarChart, HeatmapChart, TitleComponent, TooltipComponent, GridComponent, DataZoomComponent, VisualMapComponent, MarkPointComponent, MarkAreaComponent, CanvasRenderer])

// [AUDIT-FIX] 严重-1: 暗色模式适配
const { chartAxisColor, chartValueAxis, chartCategoryAxis, chartTooltipAxis, chartTooltip, chartLegend } = useChartTheme()

const loading = ref(false)
const exporting = ref(false)
const devices = ref<Device[]>([])
const queryResult = ref<any[]>([])

// 修复3: 图表实例引用，用于 PNG 导出
const chartRef = ref<InstanceType<typeof VChart> | null>(null)

// 修复10: 图表类型切换（line/bar/area/step）
const chartType = ref<'line' | 'bar' | 'area' | 'step'>('line')
const chartTypeOptions = computed(() => [
  { label: t('dataQuery.chartLine'), value: 'line' },
  { label: t('dataQuery.chartBar'), value: 'bar' },
  { label: t('dataQuery.chartArea'), value: 'area' },
  { label: t('dataQuery.chartStep'), value: 'step' },
])

// 修复11: 时间对比——叠加两个时间段的曲线
const compareEnabled = ref(false)
const compareRange = ref<string>('-24h')
const compareResult = ref<any[]>([])
const compareLoading = ref(false)

const queryForm = reactive({
  device_id: '' as string,
  point_name: '' as string,
  range: '-1h',
  aggregate: '' as string,
  limit: 1000,
})

const queryFormRef = ref<any>(null)
const queryRules = computed(() => ({
  device_id: { required: true, message: t('dataQuery.selectDevice'), trigger: ['change', 'blur'] },
  point_name: { required: true, message: t('dataQuery.selectPoint'), trigger: ['change', 'blur'] },
  range: { required: true, message: t('dataQuery.selectTimeRange'), trigger: ['change', 'blur'] },
}))

// FIXED-Pagination: limit 可选值，max 支持更大值（50000），默认值保持较小（1000）
const limitOptions = [
  { label: '500', value: 500 },
  { label: '1000', value: 1000 },
  { label: '2000', value: 2000 },
  { label: '5000', value: 5000 },
  { label: '10000', value: 10000 },
  { label: '50000', value: 50000 },
]

// 自定义时间范围（时间戳数组 [start, end]）
const customDateRange = ref<[number, number] | null>(null)

const deviceOptions = computed(() => devices.value.map(d => ({ label: `${d.name} (${d.device_id})`, value: d.device_id })))
const pointOptions = computed(() => {
  const dev = devices.value.find(d => d.device_id === queryForm.device_id)
  // FIXED: 原问题-dev?.points.map(...)可选链后直接.map会崩溃，改为(dev?.points ?? []).map(...)
  return (dev?.points ?? []).map(p => ({ label: `${p.name} (${p.unit || '-'})`, value: p.name }))
})

watch(() => queryForm.device_id, () => { queryForm.point_name = '' })

const rangeOptions = computed(() => [
  { label: t('dataQuery.range1h'), value: '-1h' },  // FIXED: 原问题-中文硬编码
  { label: t('dataQuery.range6h'), value: '-6h' },  // FIXED: 原问题-中文硬编码
  { label: t('dataQuery.range24h'), value: '-24h' },  // FIXED: 原问题-中文硬编码
  { label: t('dataQuery.range7d'), value: '-7d' },  // FIXED: 原问题-中文硬编码
  { label: t('dataQuery.range30d'), value: '-30d' },  // FIXED: 原问题-中文硬编码
  { label: t('dataQuery.rangeCustom'), value: 'custom' },
])

const exportOptions = computed(() => [
  { label: t('dataQuery.exportCsv'), key: 'csv' },
  { label: t('dataQuery.exportJson'), key: 'json' },
  { label: t('dataQuery.exportXlsx'), key: 'xlsx' },
  { label: t('dataQuery.exportPng'), key: 'png' },
])

// 修复16: 导出列选择
const showExportColumnsModal = ref(false)
const pendingExportFormat = ref('csv')
const exportColumnOptions = computed(() => [
  { label: t('dataQuery.colTime'), key: 'time' },
  { label: t('dataQuery.colValue'), key: 'value' },
  { label: t('dataQuery.colQuality'), key: 'quality' },
])
const selectedExportColumns = ref<string[]>(['time', 'value', 'quality'])

const aggregateOptions = computed(() => [
  { label: t('dataQuery.aggMean'), value: 'mean' },  // FIXED: 原问题-中文硬编码
  { label: t('dataQuery.aggMax'), value: 'max' },  // FIXED: 原问题-中文硬编码
  { label: t('dataQuery.aggMin'), value: 'min' },  // FIXED: 原问题-中文硬编码
  { label: t('dataQuery.aggLast'), value: 'last' },  // FIXED: 原问题-中文硬编码
  { label: t('dataQuery.aggFirst'), value: 'first' },  // FIXED: 原问题-中文硬编码
  { label: t('dataQuery.aggSum'), value: 'sum' },  // FIXED: 原问题-中文硬编码
  { label: t('dataQuery.aggCount'), value: 'count' },  // FIXED: 原问题-中文硬编码
  { label: t('dataQuery.aggMedian'), value: 'median' },  // FIXED: 原问题-中文硬编码
  { label: t('dataQuery.aggStddev'), value: 'stddev' },  // FIXED: 原问题-中文硬编码
])

const stats = computed(() => {
  if (!queryResult.value.length) return null
  const values = queryResult.value.map(d => d.value ?? d._value ?? 0)
  if (!values.length) return null
  const count = values.length
  const max = values.reduce((a, b) => Math.max(a, b), -Infinity)
  const min = values.reduce((a, b) => Math.min(a, b), Infinity)
  const avg = values.reduce((a, b) => a + b, 0) / count
  // 修复7: 前端计算标准差、方差、P95 分位数
  const variance = values.reduce((a, b) => a + (b - avg) ** 2, 0) / count
  const stddev = Math.sqrt(variance)
  const sorted = [...values].sort((a, b) => a - b)
  const p95Idx = Math.min(Math.floor(count * 0.95), count - 1)
  const p95 = sorted[p95Idx]
  return {
    count,
    max,
    min,
    avg,
    last: values[values.length - 1],
    stddev,
    variance,
    p95,
  }
})

// 修复6: 图表自定义注解——存入 localStorage，在图表上以 markLine 标记线和文字展示
interface ChartAnnotation {
  id: string
  time: string  // HH:MM:SS 格式，对应 xAxis 类目
  text: string
  createdAt: number
}
const ANNOTATION_KEY = 'data_query_annotations'
const chartAnnotations = ref<ChartAnnotation[]>(loadAnnotations())
const showAnnotationModal = ref(false)
const newAnnotationText = ref('')
const newAnnotationTime = ref('')

function loadAnnotations(): ChartAnnotation[] {
  try {
    const raw = localStorage.getItem(ANNOTATION_KEY)
    return raw ? JSON.parse(raw) : []
  } catch { return [] }
}
function persistAnnotations() {
  try { localStorage.setItem(ANNOTATION_KEY, JSON.stringify(chartAnnotations.value)) } catch { /* ignore */ }
}
function openAnnotationModal() {
  if (!queryResult.value.length) {
    message.warning(t('dataQuery.pleaseQuery'))
    return
  }
  // 默认时间点取数据中间位置
  const mid = queryResult.value[Math.floor(queryResult.value.length / 2)]
  newAnnotationTime.value = (mid.time || mid._time || '').substring(11, 19)
  newAnnotationText.value = ''
  showAnnotationModal.value = true
}
function confirmAddAnnotation() {
  const text = newAnnotationText.value.trim()
  if (!text) {
    message.warning(t('dataQuery.annotationPlaceholder'))
    return
  }
  if (!newAnnotationTime.value) {
    message.warning(t('dataQuery.annotationTime'))
    return
  }
  chartAnnotations.value.push({
    id: `anno_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`,
    time: newAnnotationTime.value,
    text,
    createdAt: Date.now(),
  })
  persistAnnotations()
  showAnnotationModal.value = false
  message.success(t('common.saveSuccess'))
}
function removeAnnotation(id: string) {
  chartAnnotations.value = chartAnnotations.value.filter(a => a.id !== id)
  persistAnnotations()
}
function clearAllAnnotations() {
  chartAnnotations.value = []
  persistAnnotations()
}

// 修复12: 计算异常标记——数据缺口、离群点、冻结段
function computeAnomalyMarks(data: any[]) {
  if (data.length < 3) return { markAreas: [] as any[], markPoints: [] as any[] }
  const times = data.map(d => new Date(d.time || d._time || '').getTime()).filter(t => !isNaN(t))
  const values = data.map(d => d.value ?? d._value ?? 0)
  const markAreas: any[] = []
  const markPoints: any[] = []

  // 1. 数据缺口：相邻时间间隔 > 2倍中位数间隔
  if (times.length >= 3) {
    const intervals: number[] = []
    for (let i = 1; i < times.length; i++) intervals.push(times[i] - times[i - 1])
    intervals.sort((a, b) => a - b)
    const medianInterval = intervals[Math.floor(intervals.length / 2)] || 0
    if (medianInterval > 0) {
      for (let i = 1; i < times.length; i++) {
        const gap = times[i] - times[i - 1]
        if (gap > medianInterval * 2) {
          markAreas.push([
            { xAxis: new Date(times[i - 1]).toISOString().substring(11, 19) },
            { xAxis: new Date(times[i]).toISOString().substring(11, 19) },
          ])
        }
      }
    }
  }

  // 2. 离群点：值偏离均值超过 3 倍标准差
  if (values.length >= 4) {
    const mean = values.reduce((a, b) => a + b, 0) / values.length
    const variance = values.reduce((a, b) => a + (b - mean) ** 2, 0) / values.length
    const std = Math.sqrt(variance)
    if (std > 0) {
      const timesStr = data.map(d => (d.time || d._time || '').substring(11, 19))
      for (let i = 0; i < values.length; i++) {
        if (Math.abs(values[i] - mean) > 3 * std) {
          markPoints.push({ name: t('dataQuery.outlier'), coord: [timesStr[i], values[i]], value: values[i].toFixed(2), itemStyle: { color: CHART_ROLE_COLORS.anomaly } })
        }
      }
    }
  }

  // 3. 冻结段：连续相同值超过 5 个点
  const timesStr = data.map(d => (d.time || d._time || '').substring(11, 19))
  let freezeStart = -1
  let freezeVal: number | null = null
  for (let i = 0; i <= values.length; i++) {
    if (i < values.length && freezeVal !== null && values[i] === freezeVal) {
      // 继续冻结
    } else {
      if (freezeStart >= 0 && i - freezeStart >= 5) {
        markAreas.push([
          { xAxis: timesStr[freezeStart] },
          { xAxis: timesStr[i - 1] },
        ])
      }
      freezeStart = i < values.length ? i : -1
      freezeVal = i < values.length ? values[i] : null
    }
  }

  return { markAreas, markPoints }
}

// FIXED-Downsample: 图表自动降采样，避免大数据量渲染卡顿
const MAX_CHART_POINTS = 800

/**
 * 等间隔抽样降采样
 * 当数据量超过 MAX_CHART_POINTS 时，每隔 step 取一个点，保留首尾点
 */
function downsampleData<T>(data: T[]): { data: T[]; step: number } {
  if (data.length <= MAX_CHART_POINTS) return { data, step: 1 }
  const step = Math.ceil(data.length / MAX_CHART_POINTS)
  const result: T[] = []
  for (let i = 0; i < data.length; i += step) {
    result.push(data[i])
  }
  // 确保保留最后一个点
  if (result[result.length - 1] !== data[data.length - 1]) {
    result.push(data[data.length - 1])
  }
  return { data: result, step }
}

// 降采样信息（用于 UI 提示）
const downsampleInfo = computed(() => {
  const total = queryResult.value.length
  if (total <= MAX_CHART_POINTS) return null
  const { data: sampled, step } = downsampleData(queryResult.value)
  return { sampled: sampled.length, total, step }
})

// FIXED-Downsample: 降采样提示文案（t() 不支持同时传 fallback 和 params，故单独构建）
const downsampleHintText = computed(() => {
  if (!downsampleInfo.value) return ''
  const key = 'dataQuery.downsampleHint'
  const translated = t(key, { sampled: downsampleInfo.value.sampled, total: downsampleInfo.value.total })
  // 若 key 未找到，t() 返回 key 本身；此时使用 fallback
  if (translated === key) {
    return `Data auto-downsampled, showing ${downsampleInfo.value.sampled}/${downsampleInfo.value.total} points`
  }
  return translated
})

const chartOption = computed(() => {
  // FIXED-Downsample: 图表数据降采样，保留原始数据用于表格展示
  const { data: chartData } = downsampleData(queryResult.value)
  const times = chartData.map(d => (d.time || d._time || '').substring(11, 19))
  const values = chartData.map(d => d.value ?? d._value ?? 0)
  // 修复10: 根据 chartType 构建不同 series
  const isBar = chartType.value === 'bar'
  const isStep = chartType.value === 'step'
  const isArea = chartType.value === 'area'
  const series: any = {
    type: isBar ? 'bar' : 'line',
    data: values,
    smooth: !isStep && !isBar,
    symbol: 'none',
    step: isStep ? 'middle' as const : false,
    itemStyle: { color: BRAND_COLORS.panelBase },
    areaStyle: (isArea || (!isBar && !isStep)) ? { color: 'rgba(102,126,234,0.15)' } : undefined,
  }
  // 修复12: 异常标记（基于降采样后的数据计算，减少计算量）
  const { markAreas, markPoints } = computeAnomalyMarks(chartData)
  if (markAreas.length) series.markArea = { silent: true, itemStyle: { color: 'rgba(208,48,80,0.08)' }, data: markAreas }
  if (markPoints.length) series.markPoint = { symbol: 'pin', symbolSize: 40, data: markPoints }

  // 修复6: 自定义注解——以 markLine 垂直标记线 + 文字展示
  if (chartAnnotations.value.length) {
    const annoLines = chartAnnotations.value.map(a => ({
      xAxis: a.time,
      label: { show: true, formatter: a.text, position: 'insideEndTop', color: SEMANTIC_COLORS.critical, fontSize: 11 },
      lineStyle: { color: SEMANTIC_COLORS.critical, type: 'dashed', width: 1.5 },
    }))
    series.markLine = { silent: true, symbol: 'none', data: annoLines }
  }

  // 修复11: 时间对比——叠加对比曲线
  const compareSeries: any[] = []
  if (compareEnabled.value && compareResult.value.length) {
    // 对比数据也降采样
    const { data: cmpChartData } = downsampleData(compareResult.value)
    const cmpValues = cmpChartData.map(d => d.value ?? d._value ?? 0)
    compareSeries.push({
      type: isBar ? 'bar' : 'line',
      name: t('dataQuery.compareSeries'),
      data: cmpValues,
      smooth: !isStep && !isBar,
      symbol: 'none',
      step: isStep ? 'middle' as const : false,
      itemStyle: { color: SEMANTIC_COLORS.warning },
      lineStyle: { type: 'dashed' },
      areaStyle: isArea ? { color: 'rgba(240,160,32,0.1)' } : undefined,
    })
  }

  return {
    // [AUDIT-FIX] 严重-1: 暗色模式适配
    tooltip: chartTooltipAxis(),
    legend: compareSeries.length ? chartLegend({ data: [t('dataQuery.currentSeries'), t('dataQuery.compareSeries')] }) : undefined,
    grid: { left: 60, right: 20, top: compareSeries.length ? 40 : 20, bottom: 60 },
    xAxis: chartCategoryAxis({ data: times }),
    yAxis: chartValueAxis(),
    dataZoom: [{ type: 'inside' }, { type: 'slider' }],
    series: [series, ...compareSeries],
  }
})

const dataColumns = [
  { title: t('dataQuery.colTime'), key: 'time', width: 200, render: (r: any) => formatDateTime(r.time || r._time) },  // FIXED: 原问题-中文硬编码
  { title: t('dataQuery.colValue'), key: 'value', render: (r: any) => {  // FIXED: 原问题-中文硬编码
    const v = r.value ?? r._value
    return v != null ? (typeof v === 'number' ? v.toFixed(4) : v) : '-'
  }},
  { title: t('dataQuery.colQuality'), key: 'quality', width: 80, render: (r: any) => r.quality || '-' },  // FIXED: 原问题-中文硬编码
]

const pagination = reactive({
  page: 1,
  pageSize: 50,
  itemCount: 0,
  pageSizes: [20, 50, 100, 200],
  showSizePicker: true,
  onChange: (p: number) => { pagination.page = p },
  onUpdatePageSize: (s: number) => { pagination.pageSize = s; pagination.page = 1 },
})

// FIXED: 原问题-computed 中执行 pagination.itemCount = ... 是副作用，违反 Vue 响应式最佳实践，
// 可能导致无限更新循环。改为在 handleQuery 成功后设置 itemCount。
// FIXED: 原问题-[...queryResult.value].reverse().slice(start, end) 对大数据集（最大 100000 条）
// 每次分页变化都执行 O(n) 全量复制+反转，造成卡顿。改为通过索引从尾部切片，避免全量复制。
const tableData = computed(() => {
  const total = queryResult.value.length
  if (!total) return []
  // 倒序展示：最新数据在前。通过索引计算从尾部切片，避免全量反转
  const start = total - pagination.page * pagination.pageSize
  const end = start + pagination.pageSize
  return queryResult.value.slice(Math.max(0, start), end)
})

async function fetchDevices() {
  try {
    // 加载设备列表（限制 200 条避免全量加载；如设备量极大可改远程搜索）
    const data = await deviceApi.list({ page: 1, size: 200 })
    devices.value = data?.data ?? []
  } catch (e: any) {
    message.error(extractError(e, t('dataQuery.loadDevicesFailed')))  // FIXED: 原问题-中文硬编码
    devices.value = []
  }
}

// 根据时间范围选择返回 start/stop 参数：预设值传 '-1h' 等，custom 传 ISO 字符串
function getTimeRangeParams(): { start: string; stop?: string } {
  if (queryForm.range === 'custom') {
    if (customDateRange.value && customDateRange.value.length === 2) {
      return {
        start: new Date(customDateRange.value[0]).toISOString(),
        stop: new Date(customDateRange.value[1]).toISOString(),
      }
    }
    return { start: '-1h' }
  }
  return { start: queryForm.range }
}

async function handleQuery() {
  try { await queryFormRef.value?.validate() } catch { return }
  if (!queryForm.device_id || !queryForm.point_name) {
    message.warning(t('dataQuery.selectDeviceAndPoint'))  // FIXED: 原问题-中文硬编码
    return
  }
  if (queryForm.range === 'custom' && (!customDateRange.value || customDateRange.value.length !== 2)) {
    message.warning(t('dataQuery.selectTimeRange'))
    return
  }
  pagination.page = 1
  loading.value = true
  try {
    const { start, stop } = getTimeRangeParams()
    const result = await dataApi.query({
      device_id: queryForm.device_id,
      point_name: queryForm.point_name,
      start,
      stop,
      aggregate: queryForm.aggregate || undefined,
      limit: queryForm.limit ?? 1000,
    })
    // FIXED: 原问题-查询成功但返回空数据时未清空旧数据
    if (!result || (Array.isArray(result) && result.length === 0)) {
      queryResult.value = []
      pagination.itemCount = 0
      message.info(t('dataQuery.noData'))
    } else {
      queryResult.value = result
      // FIXED: 将 itemCount 设置从 computed 副作用移到此处
      pagination.itemCount = result.length
      // FIXED-Pagination: 结果达到 limit 时提示数据可能不完整
      if (result.length >= queryForm.limit) {
        message.warning(t('dataQuery.limitWarning'))
      }
    }
    // 修复11: 启用对比时同时加载对比数据
    if (compareEnabled.value) await loadCompareData()
  } catch (e: any) {
    message.error(extractError(e, t('dataQuery.queryFailed')))  // FIXED: 原问题-中文硬编码
  } finally {
    loading.value = false
  }
}

// 修复11: 加载对比时间段数据
async function loadCompareData() {
  if (!compareEnabled.value || !queryForm.device_id || !queryForm.point_name) {
    compareResult.value = []
    return
  }
  compareLoading.value = true
  try {
    const result = await dataApi.query({
      device_id: queryForm.device_id,
      point_name: queryForm.point_name,
      start: compareRange.value,
      aggregate: queryForm.aggregate || undefined,
      limit: queryForm.limit ?? 1000,
    })
    compareResult.value = Array.isArray(result) ? result : []
  } catch {
    compareResult.value = []
  } finally {
    compareLoading.value = false
  }
}

async function handleExport(format: string = 'csv') {
  if (!queryForm.device_id || !queryForm.point_name) {
    message.warning(t('dataQuery.queryFirst'))  // FIXED: 原问题-中文硬编码
    return
  }
  if (queryForm.range === 'custom' && (!customDateRange.value || customDateRange.value.length !== 2)) {
    message.warning(t('dataQuery.selectTimeRange'))
    return
  }
  // 修复3: PNG 导出直接调用 ECharts getDataURL，不走后端
  if (format === 'png') {
    const chartInst = chartRef.value as any
    if (!chartInst) {
      message.warning(t('dataQuery.queryFirst'))
      return
    }
    exporting.value = true
    try {
      await new Promise(resolve => requestAnimationFrame(resolve))
      const dataUrl = chartInst.getDataURL({
        type: 'png',
        pixelRatio: 2,
        backgroundColor: '#fff',
      })
      const a = document.createElement('a')
      a.href = dataUrl
      a.download = `${queryForm.device_id}_${queryForm.point_name}.png`
      a.click()
    } catch (e: any) {
      message.error(extractError(e, t('dataQuery.exportFailed')))
    } finally {
      exporting.value = false
    }
    return
  }
  // 修复16: 打开列选择弹窗
  pendingExportFormat.value = format
  if (!queryResult.value.length) {
    message.warning(t('dataQuery.queryFirst'))
    return
  }
  showExportColumnsModal.value = true
}

// 修复16/17: 确认导出（带列选择 + xlsx 支持）
async function confirmExportWithColumns() {
  if (!selectedExportColumns.value.length) {
    message.warning(t('dataQuery.selectColumns'))
    return
  }
  // [AUDIT-FIX] 一般级-导出限量检查，避免浏览器 OOM
  const MAX_EXPORT_ROWS = 100000
  if (queryResult.value.length > MAX_EXPORT_ROWS) {
    message.warning(t('dataQuery.exportLimit', { max: MAX_EXPORT_ROWS }))
    return
  }
  const format = pendingExportFormat.value
  showExportColumnsModal.value = false
  exporting.value = true
  try {
    const cols = selectedExportColumns.value
    const rows = queryResult.value.map(d => {
      const row: Record<string, any> = {}
      if (cols.includes('time')) row[t('dataQuery.colTime')] = formatDateTime(d.time || d._time)
      if (cols.includes('value')) row[t('dataQuery.colValue')] = d.value ?? d._value ?? ''
      if (cols.includes('quality')) row[t('dataQuery.colQuality')] = d.quality ?? ''
      return row
    })
    if (format === 'xlsx') {
      // 修复17: 使用 xlsx 库导出 Excel
      const XLSX = await import('xlsx')
      const ws = XLSX.utils.json_to_sheet(rows)
      const wb = XLSX.utils.book_new()
      XLSX.utils.book_append_sheet(wb, ws, 'Data')
      XLSX.writeFile(wb, `${queryForm.device_id}_${queryForm.point_name}.xlsx`)
    } else if (format === 'json') {
      const blob = new Blob([JSON.stringify(rows, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `${queryForm.device_id}_${queryForm.point_name}.json`
      a.click()
      URL.revokeObjectURL(url)
    } else {
      // csv
      const headers = Object.keys(rows[0] || {})
      const csvLines = [headers.join(',')]
      for (const r of rows) {
        csvLines.push(headers.map(h => `"${String(r[h] ?? '').replace(/"/g, '""')}"`).join(','))
      }
      const blob = new Blob([`\ufeff${csvLines.join('\n')}`], { type: 'text/csv;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `${queryForm.device_id}_${queryForm.point_name}.csv`
      a.click()
      URL.revokeObjectURL(url)
    }
    message.success(t('dataQuery.exportSuccess'))
  } catch (e: any) {
    message.error(extractError(e, t('dataQuery.exportFailed')))  // FIXED: 原问题-中文硬编码
  } finally {
    exporting.value = false
  }
}

const multiPointNames = ref<string[]>([])
const multiLoading = ref(false)
const multiPointResult = ref<any[]>([])
const correlationLoading = ref(false)
const correlationResult = ref<any>(null)

const COLORS = CATEGORICAL_PALETTE

const multiPointChartOption = computed(() => {
  const pointMap: Record<string, any[]> = {}
  for (const item of multiPointResult.value) {
    const name = item.point_name || item._field || 'unknown'
    if (!pointMap[name]) pointMap[name] = []
    pointMap[name].push(item)
  }
  const names = Object.keys(pointMap)
  if (!names.length) return {}
  const times = pointMap[names[0]].map(d => (d.time || d._time || '').substring(11, 19))
  const series = names.map((name, i) => ({
    name,
    type: 'line' as const,
    data: pointMap[name].map(d => d.value ?? d._value ?? 0),
    smooth: true,
    symbol: 'none',
    itemStyle: { color: COLORS[i % COLORS.length] },
  }))
  return {
    // [AUDIT-FIX] 严重-1: 暗色模式适配
    tooltip: chartTooltipAxis(),
    legend: chartLegend({ data: names }),
    grid: { left: 60, right: 20, top: 40, bottom: 60 },
    xAxis: chartCategoryAxis({ data: times }),
    yAxis: chartValueAxis(),
    dataZoom: [{ type: 'inside' as const }, { type: 'slider' as const }],
    series,
  }
})

const correlationChartOption = computed(() => {
  const cr = correlationResult.value
  if (!cr) return {}
  const labels = cr.labels || cr.point_names || []
  const matrix = cr.matrix || cr.correlation_matrix || []
  const data: number[][] = []
  for (let i = 0; i < labels.length; i++) {
    for (let j = 0; j < labels.length; j++) {
      data.push([i, j, matrix[i]?.[j] ?? 0])
    }
  }
  return {
    // [AUDIT-FIX] 严重-1: 暗色模式适配
    tooltip: chartTooltip({
      formatter: (p: any) => {
        const val = p.value[2]
        return `${labels[p.value[0]]} vs ${labels[p.value[1]]}: ${typeof val === 'number' ? val.toFixed(4) : val}`
      },
    }),
    grid: { left: 120, right: 40, top: 10, bottom: 80 },
    xAxis: chartCategoryAxis({ data: labels, axisLabel: { rotate: 45, fontSize: 10, color: chartAxisColor.value } }),
    yAxis: chartCategoryAxis({ data: labels }),
    visualMap: { min: -1, max: 1, calculable: true, orient: 'horizontal' as const, left: 'center' as const, bottom: 0, inRange: { color: [...DIVERGING_PALETTE] } },
    series: [{
      type: 'heatmap' as const,
      data,
      label: { show: true, formatter: (p: any) => typeof p.value[2] === 'number' ? p.value[2].toFixed(2) : p.value[2], fontSize: 10 },
    }],
  }
})

async function handleMultiPointQuery() {
  if (!queryForm.device_id || multiPointNames.value.length < 2) return
  multiLoading.value = true
  try {
    const { start, stop } = getTimeRangeParams()
    const result = await dataApi.multiPoint({
      device_id: queryForm.device_id,
      point_names: multiPointNames.value.join(','),
      start,
      stop,
    })
    multiPointResult.value = result || []
    correlationResult.value = null
  } catch (e: any) {
    message.error(extractError(e, t('dataQuery.compareFailed')))
  } finally {
    multiLoading.value = false
  }
}

async function handleCorrelation() {
  if (!queryForm.device_id || multiPointNames.value.length < 2) return
  if (multiPointNames.value.length > 2) {
    message.warning(t('dataQuery.correlationOnlyFirstTwo'))
  }
  correlationLoading.value = true
  try {
    const { start, stop } = getTimeRangeParams()
    const result = await dataApi.correlation({
      device_id: queryForm.device_id,
      point1: multiPointNames.value[0] || '',
      point2: multiPointNames.value[1] || '',
      start,
      stop,
    })
    correlationResult.value = result || null
  } catch (e: any) {
    message.error(extractError(e, t('dataQuery.correlationFailed')))
  } finally {
    correlationLoading.value = false
  }
}

// 修复12: 多设备对比——并发查询多个设备的同一测点，合并到同一图表
const multiDeviceIds = ref<string[]>([])
const multiDevicePointName = ref('')
const multiDeviceLoading = ref(false)
// 每个设备一条曲线：{ deviceId, deviceName, data: [{time, value}] }
const multiDeviceResult = ref<{ deviceId: string; deviceName: string; data: any[] }[]>([])
const multiDeviceSuccessCount = ref(0)

const multiDeviceSummaryText = computed(() => {
  if (!multiDeviceResult.value.length) return ''
  const key = 'dataQuery.multiDeviceResultSummary'
  const translated = t(key, { count: multiDeviceIds.value.length, success: multiDeviceSuccessCount.value })
  if (translated === key) {
    return `${multiDeviceIds.value.length} devices total, ${multiDeviceSuccessCount.value} succeeded`
  }
  return translated
})

const multiDeviceChartOption = computed(() => {
  const results = multiDeviceResult.value
  if (!results.length) return {}
  // 合并所有时间戳作为 X 轴
  const timeSet = new Set<string>()
  for (const r of results) {
    for (const d of r.data) {
      timeSet.add((d.time || d._time || '').substring(11, 19))
    }
  }
  const times = Array.from(timeSet).sort()
  // 为每个设备构建 series，按时间索引对齐
  const series = results.map((r, i) => {
    const valueMap = new Map<string, number>()
    for (const d of r.data) {
      const ts = (d.time || d._time || '').substring(11, 19)
      valueMap.set(ts, d.value ?? d._value ?? 0)
    }
    return {
      name: r.deviceName,
      type: 'line' as const,
      data: times.map(ts => valueMap.has(ts) ? valueMap.get(ts) : null),
      smooth: true,
      symbol: 'none',
      connectNulls: true,
      itemStyle: { color: COLORS[i % COLORS.length] },
      lineStyle: { width: 1.5 },
    }
  })
  return {
    // [AUDIT-FIX] 严重-1: 暗色模式适配
    tooltip: chartTooltipAxis(),
    legend: chartLegend({ data: results.map(r => r.deviceName) }),
    grid: { left: 60, right: 20, top: 40, bottom: 60 },
    xAxis: chartCategoryAxis({ data: times }),
    yAxis: chartValueAxis(),
    dataZoom: [{ type: 'inside' as const }, { type: 'slider' as const }],
    series,
  }
})

async function handleMultiDeviceCompare() {
  if (multiDeviceIds.value.length < 2) {
    message.warning(t('dataQuery.multiDeviceSelectAtLeast2'))
    return
  }
  const pointName = multiDevicePointName.value.trim()
  if (!pointName) {
    message.warning(t('dataQuery.multiDevicePointRequired'))
    return
  }
  multiDeviceLoading.value = true
  multiDeviceResult.value = []
  try {
    const { start, stop } = getTimeRangeParams()
    // 并发查询每个设备
    const tasks = multiDeviceIds.value.map(async (deviceId) => {
      const dev = devices.value.find(d => d.device_id === deviceId)
      const deviceName = dev ? `${dev.name} (${deviceId})` : deviceId
      try {
        const result = await dataApi.query({
          device_id: deviceId,
          point_name: pointName,
          start,
          stop,
          aggregate: queryForm.aggregate || undefined,
          limit: queryForm.limit ?? 1000,
        })
        return { deviceId, deviceName, data: Array.isArray(result) ? result : [] }
      } catch {
        // 单个设备失败不影响其他设备
        return { deviceId, deviceName, data: [] as any[] }
      }
    })
    const results = await Promise.all(tasks)
    multiDeviceResult.value = results.filter(r => r.data.length > 0)
    multiDeviceSuccessCount.value = multiDeviceResult.value.length
    if (multiDeviceResult.value.length < results.length) {
      message.warning(t('dataQuery.multiDeviceNoCommonPoint'))
    }
    if (multiDeviceResult.value.length === 0) {
      message.info(t('dataQuery.noData'))
    } else {
      message.success(t('dataQuery.multiDeviceResultSummary', { count: multiDeviceIds.value.length, success: multiDeviceSuccessCount.value }))
    }
  } catch (e: any) {
    message.error(extractError(e, t('dataQuery.multiDeviceCompareFailed')))
  } finally {
    multiDeviceLoading.value = false
  }
}

// 修复5: 保存查询/收藏——localStorage 持久化
interface SavedQuery {
  id: string
  name: string
  device_id: string
  point_name: string
  range: string
  aggregate: string
  limit: number
  customDateRange: [number, number] | null
  createdAt: number
}
const SAVED_QUERY_KEY = 'data_query_favorites'
const savedQueries = ref<SavedQuery[]>(loadSavedQueries())
const showSaveQueryModal = ref(false)
const newQueryName = ref('')
const selectedSavedQuery = ref<string | null>(null)

function loadSavedQueries(): SavedQuery[] {
  try {
    const raw = localStorage.getItem(SAVED_QUERY_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed : []
  } catch {
    return []
  }
}

function persistSavedQueries() {
  try {
    localStorage.setItem(SAVED_QUERY_KEY, JSON.stringify(savedQueries.value))
  } catch {
    // 忽略写入失败（如隐私模式）
  }
}

const savedQueryOptions = computed(() => savedQueries.value.map(q => ({
  label: q.name,
  value: q.id,
})))

function openSaveQueryModal() {
  if (!queryForm.device_id || !queryForm.point_name) {
    message.warning(t('dataQuery.selectDeviceAndPoint'))
    return
  }
  newQueryName.value = ''
  showSaveQueryModal.value = true
}

function confirmSaveQuery() {
  const name = newQueryName.value.trim()
  if (!name) {
    message.warning(t('dataQuery.queryNamePlaceholder'))
    return
  }
  const item: SavedQuery = {
    id: `q_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
    name,
    device_id: queryForm.device_id,
    point_name: queryForm.point_name,
    range: queryForm.range,
    aggregate: queryForm.aggregate,
    limit: queryForm.limit,
    customDateRange: customDateRange.value ? [...customDateRange.value] as [number, number] : null,
    createdAt: Date.now(),
  }
  savedQueries.value.push(item)
  persistSavedQueries()
  selectedSavedQuery.value = item.id
  showSaveQueryModal.value = false
  message.success(t('common.saveSuccess'))
}

function onSavedQuerySelect(id: string | null) {
  if (!id) return
  const item = savedQueries.value.find(q => q.id === id)
  if (!item) return
  queryForm.device_id = item.device_id
  queryForm.point_name = item.point_name
  queryForm.range = item.range
  queryForm.aggregate = item.aggregate
  queryForm.limit = item.limit
  customDateRange.value = item.customDateRange ? [...item.customDateRange] as [number, number] : null
  // 自动触发查询
  handleQuery()
}

function deleteSavedQuery() {
  if (!selectedSavedQuery.value) return
  const id = selectedSavedQuery.value
  // 严重-2: 通过 id 查找查询名称用于二次确认
  const target = savedQueries.value.find(q => q.id === id)
  const name = target?.name || id
  // 严重-2: 删除前二次确认，防止误删
  dialog.warning({
    title: t('common.confirm'),
    content: t('common.confirmDeleteName', { name }),  // FIXED-i18n: 移除硬编码中文回退，改用 i18n key
    positiveText: t('common.confirm'),
    negativeText: t('common.cancel'),
    onPositiveClick: () => {
      savedQueries.value = savedQueries.value.filter(q => q.id !== id)
      persistSavedQueries()
      selectedSavedQuery.value = null
      message.success(t('common.deleteSuccess'))
    },
  })
}

// 修复8: SQL查询模式——简化实现：解析简单SQL条件，转换为API参数
const sqlMode = ref(false)
const sqlText = ref('')
function applySqlQuery() {
  const sql = sqlText.value.trim()
  if (!sql) {
    message.warning(t('dataQuery.sqlParseError'))
    return
  }
  try {
    // 解析简单 WHERE 条件：提取 device_id, point_name, range
    const deviceMatch = sql.match(/device_id\s*=\s*['"]([^'"]+)['"]/i)
    const pointMatch = sql.match(/point_name\s*=\s*['"]([^'"]+)['"]/i)
    const rangeMatch = sql.match(/range\s*=\s*['"](-?\d+[hmd])['"]/i) || sql.match(/time\s*>\s*now\(\)\s*-\s*(\d+[hmd])/i)
    if (deviceMatch) queryForm.device_id = deviceMatch[1]
    if (pointMatch) queryForm.point_name = pointMatch[1]
    if (rangeMatch) queryForm.range = '-' + rangeMatch[1]
    if (!deviceMatch && !pointMatch) {
      message.warning(t('dataQuery.sqlParseError'))
      return
    }
    message.success(t('common.success'))
    handleQuery()
  } catch {
    message.error(t('dataQuery.sqlParseError'))
  }
}

onMounted(fetchDevices)

// 修复11: 对比开关或对比时间段变化时重新加载对比数据
watch([compareEnabled, compareRange], () => {
  if (compareEnabled.value && queryResult.value.length) loadCompareData()
  else if (!compareEnabled.value) compareResult.value = []
})
</script>
