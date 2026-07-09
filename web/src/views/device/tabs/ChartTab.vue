<template>
  <n-space vertical>
    <n-space>
      <n-select v-model:value="selectedPoint" :options="pointOptions" style="width: 200px" :placeholder="t('deviceDetail.selectPoint')" />
      <n-button @click="fetchHistory" :loading="loading" :disabled="!selectedPoint">{{ t('common.refresh') }}</n-button>
    </n-space>
    <div v-if="chartData.length" ref="chartRef" style="width: 100%; height: 400px;" />
    <n-empty v-else :description="t('deviceDetail.noChartData')" />
  </n-space>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick, onScopeDispose } from 'vue'
import { useDeviceDetailConsumer } from '../composables/useDeviceDetail'
import { deviceApi, dataApi } from '@/api'
import { t } from '@/i18n'
import { NSpace, NSelect, NButton, NEmpty } from 'naive-ui'

const { device } = useDeviceDetailConsumer()
const selectedPoint = ref<string | null>(null)
const loading = ref(false)
const chartData = ref<any[]>([])
const chartRef = ref<HTMLElement | null>(null)
let chartInstance: any = null

const pointOptions = computed(() => (device.value?.points ?? []).map(p => ({ label: p.name, value: p.name })))

async function fetchHistory() {
  if (!device.value?.device_id || !selectedPoint.value) return
  loading.value = true
  try {
    const now = new Date()
    const start = new Date(now.getTime() - 24 * 3600 * 1000).toISOString()
    const res: any = await dataApi.query({
      device_id: device.value.device_id,
      point_name: selectedPoint.value,
      start,
      limit: 200,
    })
    chartData.value = Array.isArray(res) ? res : (res?.data ?? [])
    await nextTick()
    renderChart()
  } catch {
    chartData.value = []
  } finally {
    loading.value = false
  }
}

function renderChart() {
  if (!chartRef.value || !chartData.value.length) return
  import('echarts').then((echarts) => {
    if (chartInstance) chartInstance.dispose()
    chartInstance = echarts.init(chartRef.value!)
    chartInstance.setOption({
      tooltip: { trigger: 'axis' },
      xAxis: { type: 'time' },
      yAxis: { type: 'value' },
      series: [{ type: 'line', data: chartData.value.map((d: any) => [d.timestamp || d.time, d.value]), smooth: true }],
    })
  })
}

watch(selectedPoint, () => fetchHistory())
onScopeDispose(() => { if (chartInstance) chartInstance.dispose() })
</script>
