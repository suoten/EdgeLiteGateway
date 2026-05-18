<template>
  <ServiceManager
    service-name="serial_bridge"
    :display-name="t('system.serialBridge.title')"
    :running-fields="runningFields"
    @status-loaded="onStatusLoaded"
  ><!-- FIXED: 原问题-中文硬编码，改用i18n -->
    <template #extra>
      <n-card v-if="running" :title="t('system.serialBridge.transferStats')" :bordered="false" style="margin-top: 12px"><!-- FIXED: 原问题-中文硬编码，改用i18n -->
        <n-descriptions label-placement="left" :column="2" bordered>
          <n-descriptions-item :label="t('system.serialBridge.serialRx')">{{ bridgeStats.serial_rx_bytes }} {{ t('system.serialBridge.bytes') }}</n-descriptions-item><!-- FIXED: 原问题-中文硬编码，改用i18n -->
          <n-descriptions-item :label="t('system.serialBridge.serialTx')">{{ bridgeStats.serial_tx_bytes }} {{ t('system.serialBridge.bytes') }}</n-descriptions-item><!-- FIXED: 原问题-中文硬编码，改用i18n -->
          <n-descriptions-item :label="t('system.serialBridge.tcpRx')">{{ bridgeStats.tcp_rx_bytes }} {{ t('system.serialBridge.bytes') }}</n-descriptions-item><!-- FIXED: 原问题-中文硬编码，改用i18n -->
          <n-descriptions-item :label="t('system.serialBridge.tcpTx')">{{ bridgeStats.tcp_tx_bytes }} {{ t('system.serialBridge.bytes') }}</n-descriptions-item><!-- FIXED: 原问题-中文硬编码，改用i18n -->
          <n-descriptions-item :label="t('system.serialBridge.clientCount')">{{ bridgeStats.client_count }}</n-descriptions-item><!-- FIXED: 原问题-中文硬编码，改用i18n -->
          <n-descriptions-item :label="t('system.serialBridge.totalConnections')">{{ bridgeStats.total_connections }}</n-descriptions-item><!-- FIXED: 原问题-中文硬编码，改用i18n -->
        </n-descriptions>
      </n-card>
    </template>
  </ServiceManager>
</template>

<script setup lang="ts">
import { ref, reactive, computed } from 'vue'
import { t } from '@/i18n'  // FIXED: 原问题-中文硬编码，改用i18n
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
  { label: t('serialBridge.serialDevice'), value: statusData.value.current_config?.serial_port || '/dev/ttyUSB0' },  // FIXED: 原问题-硬编码中文label
  { label: t('serialBridge.baudRate'), value: statusData.value.current_config?.baud_rate || 9600 },  // FIXED: 原问题-硬编码中文label
  { label: t('serialBridge.tcpPort'), value: statusData.value.current_config?.tcp_port || 9000 },  // FIXED: 原问题-硬编码中文label
])

function onStatusLoaded(data: any) {
  statusData.value = data
  running.value = data.state === 'running'
  if (data.running_info) {
    Object.assign(bridgeStats, data.running_info)
  }
}
</script>
