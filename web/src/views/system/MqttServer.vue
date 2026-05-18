<template>
  <ServiceManager
    service-name="mqtt_server"
    :display-name="t('system.mqttServer')"
    :running-fields="runningFields"
    @status-loaded="onStatusLoaded"
  ><!-- FIXED: 原问题-中文硬编码，改用i18n -->
    <template #extra>
      <n-card v-if="isRunning" :title="t('system.connectionInfo')" :bordered="false" style="margin-top: 12px"><!-- FIXED: 原问题-中文硬编码，改用i18n -->
        <n-descriptions label-placement="left" :column="2" bordered>
          <n-descriptions-item :label="t('system.connectionCount')">{{ connections }}</n-descriptions-item><!-- FIXED: 原问题-中文硬编码，改用i18n -->
        </n-descriptions>
      </n-card>
    </template>
  </ServiceManager>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { t } from '@/i18n'  // FIXED: 原问题-中文硬编码，改用i18n
import ServiceManager from '@/components/ServiceManager.vue'

const connections = ref(0)
const isRunning = ref(false)
const statusData = ref<any>({})

const runningFields = computed(() => [
  { label: t('system.listenAddress'), value: statusData.value.current_config?.host || '0.0.0.0' },  // FIXED: 原问题-中文硬编码，改用i18n
  { label: t('mqttServer.tcpPort'), value: statusData.value.current_config?.port || 1883 },  // FIXED: 原问题-硬编码中文label
  { label: t('mqttServer.wsPort'), value: statusData.value.current_config?.ws_port || 8083 },  // FIXED: 原问题-硬编码中文label
])

function onStatusLoaded(data: any) {
  statusData.value = data
  isRunning.value = data.state === 'running'
  connections.value = data.running_info?.connections || 0
}
</script>
