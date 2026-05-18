<template>
  <ServiceManager
    service-name="modbus_slave"
    :display-name="t('system.modbusSlave')"
    :running-fields="runningFields"
    @status-loaded="onStatusLoaded"
  /><!-- FIXED: 原问题-中文硬编码，改用i18n -->
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { t } from '@/i18n'  // FIXED: 原问题-中文硬编码，改用i18n
import ServiceManager from '@/components/ServiceManager.vue'

const statusData = ref<any>({})

const runningFields = computed(() => [
  { label: t('system.listenAddress'), value: statusData.value.current_config?.host || '0.0.0.0' },  // FIXED: 原问题-中文硬编码，改用i18n
  { label: t('modbusSlave.port'), value: statusData.value.current_config?.port || 502 },  // FIXED: 原问题-硬编码中文label
  { label: t('modbusSlave.holdingRegisters'), value: statusData.value.current_config?.holding_size || 1000 },  // FIXED: 原问题-硬编码中文label
  { label: t('modbusSlave.inputRegisters'), value: statusData.value.current_config?.input_size || 1000 },  // FIXED: 原问题-硬编码中文label
])

function onStatusLoaded(data: any) {
  statusData.value = data
}
</script>
