<template>
  <ServiceManager
    service-name="mqtt_server"
    display-name="内置 MQTT Server"
    :running-fields="runningFields"
    @status-loaded="onStatusLoaded"
  >
    <template #extra>
      <n-card v-if="isRunning" title="连接信息" :bordered="false" style="margin-top: 12px">
        <n-descriptions label-placement="left" :column="2" bordered>
          <n-descriptions-item label="连接数">{{ connections }}</n-descriptions-item>
        </n-descriptions>
      </n-card>
    </template>
  </ServiceManager>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import ServiceManager from '@/components/ServiceManager.vue'

const connections = ref(0)
const isRunning = ref(false)
const statusData = ref<any>({})

const runningFields = computed(() => [
  { label: '监听地址', value: statusData.value.current_config?.host || '0.0.0.0' },
  { label: 'TCP端口', value: statusData.value.current_config?.port || 1883 },
  { label: 'WebSocket端口', value: statusData.value.current_config?.ws_port || 8083 },
])

function onStatusLoaded(data: any) {
  statusData.value = data
  isRunning.value = data.state === 'running'
  connections.value = data.running_info?.connections || 0
}
</script>
