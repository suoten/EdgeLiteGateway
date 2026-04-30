<template>
  <ServiceManager
    service-name="modbus_slave"
    display-name="内置 Modbus Slave"
    :running-fields="runningFields"
    @status-loaded="onStatusLoaded"
  />
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import ServiceManager from '@/components/ServiceManager.vue'

const statusData = ref<any>({})

const runningFields = computed(() => [
  { label: '监听地址', value: statusData.value.host || '0.0.0.0' },
  { label: '端口', value: statusData.value.port || 502 },
  { label: '保持寄存器', value: statusData.value.holding_size || 100 },
  { label: '输入寄存器', value: statusData.value.input_size || 100 },
])

function onStatusLoaded(data: any) {
  statusData.value = data
}
</script>
