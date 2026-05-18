<template>
  <n-space vertical :size="16">
    <n-card :title="t('dataQuery.title')" :bordered="false">
      <n-space vertical :size="12">
        <n-form inline :model="queryForm" label-placement="left" label-width="80">
          <n-form-item :label="t('dataQuery.device')">
            <n-select v-model:value="queryForm.device_id" :options="deviceOptions" :placeholder="t('dataQuery.selectDevice')" style="width: 200px" filterable />
          </n-form-item>
          <n-form-item :label="t('dataQuery.point')">
            <n-select v-model:value="queryForm.point_name" :options="pointOptions" :placeholder="t('dataQuery.selectPoint')" style="width: 160px" />
          </n-form-item>
          <n-form-item :label="t('dataQuery.timeRange')">
            <n-select v-model:value="queryForm.range" :options="rangeOptions" style="width: 120px" />
          </n-form-item>
          <n-form-item :label="t('dataQuery.aggregate')">
            <n-select v-model:value="queryForm.aggregate" :options="aggregateOptions" :placeholder="t('dataQuery.none')" clearable style="width: 120px" />
          </n-form-item>
          <n-button type="primary" @click="handleQuery" :loading="loading">{{ t('dataQuery.query') }}</n-button>
          <n-button @click="handleExport" :loading="exporting">{{ t('dataQuery.exportCsv') }}</n-button>
        </n-form>
      </n-space>
    </n-card>

    <n-grid :cols="2" :x-gap="16">
      <n-gi>
        <n-card :title="t('dataQuery.chart')" :bordered="false">
          <v-chart :option="chartOption" autoresize style="height: 360px" />
        </n-card>
      </n-gi>
      <n-gi>
        <n-card :title="t('dataQuery.stats')" :bordered="false">
          <n-descriptions label-placement="left" :column="1" bordered v-if="stats">
            <n-descriptions-item :label="t('dataQuery.dataPointCount')">{{ stats.count }}</n-descriptions-item>
            <n-descriptions-item :label="t('dataQuery.maxValue')">{{ stats.max?.toFixed(4) }}</n-descriptions-item>
            <n-descriptions-item :label="t('dataQuery.minValue')">{{ stats.min?.toFixed(4) }}</n-descriptions-item>
            <n-descriptions-item :label="t('dataQuery.avgValue')">{{ stats.avg?.toFixed(4) }}</n-descriptions-item>
            <n-descriptions-item :label="t('dataQuery.lastValue')">{{ stats.last?.toFixed(4) }}</n-descriptions-item>
          </n-descriptions>
          <n-empty v-else :description="t('dataQuery.pleaseQuery')" />
        </n-card>
      </n-gi>
    </n-grid>

    <n-card :title="t('dataQuery.rawData')" :bordered="false">
      <n-data-table :columns="dataColumns" :data="tableData" :bordered="false" size="small" :pagination="{ pageSize: 50 }" />
    </n-card>
  </n-space>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { useMessage } from 'naive-ui'
import { use } from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { TitleComponent, TooltipComponent, GridComponent, DataZoomComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import VChart from 'vue-echarts'
import { deviceApi, dataApi, type Device } from '@/api'
import { t } from '@/i18n'  // FIXED: 原问题-中文硬编码

use([LineChart, TitleComponent, TooltipComponent, GridComponent, DataZoomComponent, CanvasRenderer])

const message = useMessage()
const loading = ref(false)
const exporting = ref(false)
const devices = ref<Device[]>([])
const queryResult = ref<any[]>([])

const queryForm = reactive({
  device_id: '' as string,
  point_name: '' as string,
  range: '-1h',
  aggregate: '' as string,
})

const deviceOptions = computed(() => devices.value.map(d => ({ label: `${d.name} (${d.device_id})`, value: d.device_id })))
const pointOptions = computed(() => {
  const dev = devices.value.find(d => d.device_id === queryForm.device_id)
  // FIXED: 原问题-dev?.points.map(...)可选链后直接.map会崩溃，改为(dev?.points ?? []).map(...)
  return (dev?.points ?? []).map(p => ({ label: `${p.name} (${p.unit || '-'})`, value: p.name }))
})

watch(() => queryForm.device_id, () => { queryForm.point_name = '' })

const rangeOptions = [
  { label: t('dataQuery.range1h'), value: '-1h' },  // FIXED: 原问题-中文硬编码
  { label: t('dataQuery.range6h'), value: '-6h' },  // FIXED: 原问题-中文硬编码
  { label: t('dataQuery.range24h'), value: '-24h' },  // FIXED: 原问题-中文硬编码
  { label: t('dataQuery.range7d'), value: '-7d' },  // FIXED: 原问题-中文硬编码
  { label: t('dataQuery.range30d'), value: '-30d' },  // FIXED: 原问题-中文硬编码
]

const aggregateOptions = [
  { label: t('dataQuery.aggMean'), value: 'mean' },  // FIXED: 原问题-中文硬编码
  { label: t('dataQuery.aggMax'), value: 'max' },  // FIXED: 原问题-中文硬编码
  { label: t('dataQuery.aggMin'), value: 'min' },  // FIXED: 原问题-中文硬编码
  { label: t('dataQuery.aggLast'), value: 'last' },  // FIXED: 原问题-中文硬编码
  { label: t('dataQuery.aggFirst'), value: 'first' },  // FIXED: 原问题-中文硬编码
  { label: t('dataQuery.aggSum'), value: 'sum' },  // FIXED: 原问题-中文硬编码
  { label: t('dataQuery.aggCount'), value: 'count' },  // FIXED: 原问题-中文硬编码
  { label: t('dataQuery.aggMedian'), value: 'median' },  // FIXED: 原问题-中文硬编码
  { label: t('dataQuery.aggStddev'), value: 'stddev' },  // FIXED: 原问题-中文硬编码
]

const stats = computed(() => {
  if (!queryResult.value.length) return null
  const values = queryResult.value.map(d => d.value ?? d._value ?? 0)
  if (!values.length) return null
  return {
    count: values.length,
    max: values.reduce((a, b) => Math.max(a, b), -Infinity),
    min: values.reduce((a, b) => Math.min(a, b), Infinity),
    avg: values.reduce((a, b) => a + b, 0) / values.length,
    last: values[values.length - 1],
  }
})

const chartOption = computed(() => {
  const times = queryResult.value.map(d => (d.time || d._time || '').substring(11, 19))
  const values = queryResult.value.map(d => d.value ?? d._value ?? 0)
  return {
    tooltip: { trigger: 'axis' },
    grid: { left: 60, right: 20, top: 20, bottom: 60 },
    xAxis: { type: 'category', data: times, axisLabel: { fontSize: 10 } },
    yAxis: { type: 'value', axisLabel: { fontSize: 10 } },
    dataZoom: [{ type: 'inside' }, { type: 'slider' }],
    series: [{
      type: 'line', data: values, smooth: true, symbol: 'none',
      itemStyle: { color: '#667eea' }, areaStyle: { color: 'rgba(102,126,234,0.15)' },
    }],
  }
})

const dataColumns = [
  { title: t('dataQuery.colTime'), key: 'time', width: 200, render: (r: any) => r.time || r._time || '-' },  // FIXED: 原问题-中文硬编码
  { title: t('dataQuery.colValue'), key: 'value', render: (r: any) => {  // FIXED: 原问题-中文硬编码
    const v = r.value ?? r._value
    return v != null ? (typeof v === 'number' ? v.toFixed(4) : v) : '-'
  }},
  { title: t('dataQuery.colQuality'), key: 'quality', width: 80, render: (r: any) => r.quality || '-' },  // FIXED: 原问题-中文硬编码
]

const tableData = computed(() => [...queryResult.value].reverse())

async function fetchDevices() {
  try {
    const data = await deviceApi.list({ page: 1, size: 100 })
    devices.value = data?.data ?? []
  } catch (e: any) {
    message.error(e?.response?.data?.detail || t('dataQuery.loadDevicesFailed'))  // FIXED: 原问题-中文硬编码
    devices.value = []
  }
}

async function handleQuery() {
  if (!queryForm.device_id || !queryForm.point_name) {
    message.warning(t('dataQuery.selectDeviceAndPoint'))  // FIXED: 原问题-中文硬编码
    return
  }
  loading.value = true
  try {
    const result = await dataApi.query({
      device_id: queryForm.device_id,
      point_name: queryForm.point_name,
      start: queryForm.range,
      aggregate: queryForm.aggregate || undefined,
    })
    // FIXED: 原问题-查询成功但返回空数据时未清空旧数据
    if (!result || (Array.isArray(result) && result.length === 0)) {
      queryResult.value = []
      message.info(t('dataQuery.noData'))
    } else {
      queryResult.value = result
    }
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || t('dataQuery.queryFailed'))  // FIXED: 原问题-中文硬编码
  } finally {
    loading.value = false
  }
}

async function handleExport() {
  if (!queryForm.device_id || !queryForm.point_name) {
    message.warning(t('dataQuery.queryFirst'))  // FIXED: 原问题-中文硬编码
    return
  }
  exporting.value = true
  try {
    const resp = await dataApi.export({
      device_id: queryForm.device_id,
      point_name: queryForm.point_name,
      start: queryForm.range,
      format: 'csv',
    })
    const blob = new Blob([resp.data as any], { type: 'text/csv' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${queryForm.device_id}_${queryForm.point_name}.csv`
    a.click()
    URL.revokeObjectURL(url)
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || t('dataQuery.exportFailed'))  // FIXED: 原问题-中文硬编码
  } finally {
    exporting.value = false
  }
}

onMounted(fetchDevices)
</script>
