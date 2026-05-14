<template>
  <n-space vertical :size="16">
    <n-card title="时序数据查询" :bordered="false">
      <n-space vertical :size="12">
        <n-form inline :model="queryForm" label-placement="left" label-width="80">
          <n-form-item label="设备">
            <n-select v-model:value="queryForm.device_id" :options="deviceOptions" placeholder="选择设备" style="width: 200px" filterable />
          </n-form-item>
          <n-form-item label="测点">
            <n-select v-model:value="queryForm.point_name" :options="pointOptions" placeholder="选择测点" style="width: 160px" />
          </n-form-item>
          <n-form-item label="时间范围">
            <n-select v-model:value="queryForm.range" :options="rangeOptions" style="width: 120px" />
          </n-form-item>
          <n-form-item label="聚合">
            <n-select v-model:value="queryForm.aggregate" :options="aggregateOptions" placeholder="无" clearable style="width: 120px" />
          </n-form-item>
          <n-button type="primary" @click="handleQuery" :loading="loading">查询</n-button>
          <n-button @click="handleExport" :loading="exporting">导出 CSV</n-button>
        </n-form>
      </n-space>
    </n-card>

    <n-grid :cols="2" :x-gap="16">
      <n-gi>
        <n-card title="数据图表" :bordered="false">
          <v-chart :option="chartOption" autoresize style="height: 360px" />
        </n-card>
      </n-gi>
      <n-gi>
        <n-card title="统计信息" :bordered="false">
          <n-descriptions label-placement="left" :column="1" bordered v-if="stats">
            <n-descriptions-item label="数据点数">{{ stats.count }}</n-descriptions-item>
            <n-descriptions-item label="最大值">{{ stats.max?.toFixed(4) }}</n-descriptions-item>
            <n-descriptions-item label="最小值">{{ stats.min?.toFixed(4) }}</n-descriptions-item>
            <n-descriptions-item label="平均值">{{ stats.avg?.toFixed(4) }}</n-descriptions-item>
            <n-descriptions-item label="最新值">{{ stats.last?.toFixed(4) }}</n-descriptions-item>
          </n-descriptions>
          <n-empty v-else description="请查询数据" />
        </n-card>
      </n-gi>
    </n-grid>

    <n-card title="原始数据" :bordered="false">
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
  { label: '近1小时', value: '-1h' },
  { label: '近6小时', value: '-6h' },
  { label: '近24小时', value: '-24h' },
  { label: '近7天', value: '-7d' },
  { label: '近30天', value: '-30d' },
]

const aggregateOptions = [
  { label: '均值', value: 'mean' },
  { label: '最大值', value: 'max' },
  { label: '最小值', value: 'min' },
  { label: '最新值', value: 'last' },
  { label: '最早值', value: 'first' },
  { label: '求和', value: 'sum' },
  { label: '计数', value: 'count' },
  { label: '中位数', value: 'median' },
  { label: '标准差', value: 'stddev' },
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
  { title: '时间', key: 'time', width: 200, render: (r: any) => r.time || r._time || '-' },
  { title: '值', key: 'value', render: (r: any) => {
    const v = r.value ?? r._value
    return v != null ? (typeof v === 'number' ? v.toFixed(4) : v) : '-'
  }},
  { title: '质量', key: 'quality', width: 80, render: (r: any) => r.quality || '-' },
]

const tableData = computed(() => [...queryResult.value].reverse())

async function fetchDevices() {
  try {
    const data = await deviceApi.list({ page: 1, size: 100 })
    devices.value = data?.data ?? []
  } catch (e: any) {
    message.error(e?.response?.data?.detail || '加载设备列表失败')
    devices.value = []
  }
}

async function handleQuery() {
  if (!queryForm.device_id || !queryForm.point_name) {
    message.warning('请选择设备和测点')
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
    queryResult.value = result || []
    if (!queryResult.value.length) message.info('无数据')
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || '查询失败')
  } finally {
    loading.value = false
  }
}

async function handleExport() {
  if (!queryForm.device_id || !queryForm.point_name) {
    message.warning('请先查询数据')
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
    message.error(e?.response?.data?.detail || e?.message || '导出失败')
  } finally {
    exporting.value = false
  }
}

onMounted(fetchDevices)
</script>
