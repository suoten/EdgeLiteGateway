<template>
  <ServiceManager
    service-name="serial_bridge"
    display-name="串口桥接"
    :running-fields="runningFields"
    @status-loaded="onStatusLoaded"
  >
    <template #extra>
      <n-card v-if="running" title="传输统计" :bordered="false" style="margin-top: 12px">
        <n-descriptions label-placement="left" :column="2" bordered>
          <n-descriptions-item label="串口接收">{{ bridgeStats.serial_rx_bytes }} 字节</n-descriptions-item>
          <n-descriptions-item label="串口发送">{{ bridgeStats.serial_tx_bytes }} 字节</n-descriptions-item>
          <n-descriptions-item label="TCP接收">{{ bridgeStats.tcp_rx_bytes }} 字节</n-descriptions-item>
          <n-descriptions-item label="TCP发送">{{ bridgeStats.tcp_tx_bytes }} 字节</n-descriptions-item>
          <n-descriptions-item label="客户端数">{{ bridgeStats.client_count }}</n-descriptions-item>
          <n-descriptions-item label="总连接数">{{ bridgeStats.total_connections }}</n-descriptions-item>
        </n-descriptions>
      </n-card>
    </template>
  </ServiceManager>
</template>

<script setup lang="ts">
import { ref, reactive, computed } from 'vue'
import ServiceManager from '@/components/ServiceManager.vue'

const statusData = ref<any>({})
const running = ref(false)
const bridgeStats = reactive({
  serial_rx_bytes: 0,
  serial_tx_bytes: 0,
  tcp_rx_bytes: 0,
  tcp_tx_bytes: 0,
  client_count: 0,
  total_connections: 0,
})

const runningFields = computed(() => [
  { label: '串口设备', value: statusData.value.serial_port || '/dev/ttyUSB0' },
  { label: '波特率', value: statusData.value.baud_rate || 9600 },
  { label: 'TCP端口', value: statusData.value.tcp_port || 9000 },
])

function onStatusLoaded(data: any) {
  statusData.value = data
  running.value = data.state === 'running'
  if (data.running_info) {
    Object.assign(bridgeStats, data.running_info)
  }
}
</script>
