<template>
  <n-data-table
    :columns="columns"
    :data="realtimeData"
    size="small"
    :pagination="{ pageSize: 50 }"
  >
    <template #empty>
      <n-empty :description="t('deviceDetail.noRealtimeData')" size="small" />
    </template>
  </n-data-table>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onScopeDispose } from 'vue'
import { useDeviceDetailConsumer } from '../composables/useDeviceDetail'
import { deviceApi, dataApi } from '@/api'
import { t } from '@/i18n'
import { NDataTable, NEmpty } from 'naive-ui'
import { connect, disconnect } from '@/api/websocket'

const { device } = useDeviceDetailConsumer()
const realtimeData = ref<any[]>([])

const columns = computed(() => [
  { title: t('deviceDetail.pointName'), key: 'name', width: 150 },
  { title: t('common.value'), key: 'value', width: 120 },
  { title: t('common.time'), key: 'timestamp', width: 180 },
  { title: t('deviceDetail.unit'), key: 'unit', width: 80 },
])

async function fetchRealtime() {
  if (!device.value?.device_id) return
  try {
    const points = await deviceApi.getPoints(device.value.device_id)
    realtimeData.value = Object.entries(points || {}).map(([name, val]: [string, any]) => ({
      name,
      value: val?.value ?? '-',
      timestamp: val?.timestamp || val?.time || new Date().toISOString(),
      unit: val?.unit || '',
    }))
  } catch {
    realtimeData.value = []
  }
}

function onWsMessage(data: any) {
  if (data?.type === 'point_update' && data?.device_id === device.value?.device_id) {
    const point = data.point_name || data.name
    const existing = realtimeData.value.findIndex(d => d.name === point)
    const row = { name: point, value: data.value, timestamp: data.timestamp || new Date().toISOString(), unit: data.unit || '' }
    if (existing >= 0) {
      realtimeData.value[existing] = row
    } else {
      realtimeData.value.push(row)
    }
  }
}

onMounted(() => {
  fetchRealtime()
  connect('realtime', onWsMessage)
})

onScopeDispose(() => {
  disconnect('realtime', onWsMessage)
})
</script>
