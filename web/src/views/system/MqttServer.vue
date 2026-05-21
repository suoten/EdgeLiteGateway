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

      <n-card :title="t('mqtt.offlineCache')" :bordered="false" style="margin-top: 12px">
        <n-space vertical size="medium">
          <n-space align="center">
            <span>{{ t('mqtt.offlineCacheEnabled') }}</span>
            <n-switch v-model:value="offlineCacheEnabled" />
          </n-space>
          <n-space vertical v-if="offlineCacheEnabled">
            <n-space align="center">
              <span style="min-width: 120px">{{ t('mqtt.offlineDbPath') }}</span>
              <n-input v-model:value="offlineDbPath" placeholder="data/mqtt_offline.db" style="width: 300px" />
            </n-space>
            <n-space align="center">
              <span style="min-width: 120px">{{ t('mqtt.maxQueueSize') }}</span>
              <n-input-number v-model:value="maxQueueSize" :min="1" :max="100000" style="width: 200px" />
            </n-space>
            <n-space align="center">
              <span style="min-width: 120px">{{ t('mqtt.maxRetries') }}</span>
              <n-input-number v-model:value="maxRetries" :min="0" :max="100" style="width: 200px" />
            </n-space>
            <n-space align="center">
              <span style="min-width: 120px">{{ t('mqtt.retryInterval') }}</span>
              <n-input-number v-model:value="retryInterval" :min="100" :max="60000" style="width: 200px" />
            </n-space>
            <n-space style="margin-top: 8px">
              <n-button type="primary" size="small" :loading="saveLoading" @click="handleSaveOfflineConfig">{{ t('common.save') }}</n-button>
            </n-space>
          </n-space>

          <n-descriptions v-if="offlineCacheEnabled" label-placement="left" :column="2" bordered style="margin-top: 8px">
            <n-descriptions-item :label="t('mqtt.pendingCount')">{{ queueStatus.pending_count ?? '-' }}</n-descriptions-item>
            <n-descriptions-item :label="t('mqtt.sentCount')">{{ queueStatus.sent_count ?? '-' }}</n-descriptions-item>
            <n-descriptions-item :label="t('mqtt.dbSize')">{{ queueStatus.db_size ?? '-' }}</n-descriptions-item>
            <n-descriptions-item :label="t('mqtt.oldestTimestamp')">{{ queueStatus.oldest_timestamp ?? '-' }}</n-descriptions-item>
          </n-descriptions>
        </n-space>
      </n-card>
    </template>
  </ServiceManager>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { useMessage } from 'naive-ui'
import { t } from '@/i18n'  // FIXED: 原问题-中文硬编码，改用i18n
import ServiceManager from '@/components/ServiceManager.vue'
import { serviceApi } from '@/api'
import http from '@/api/http'

const msg = useMessage()
const connections = ref(0)
const isRunning = ref(false)
const statusData = ref<any>({})

const offlineCacheEnabled = ref(false)
const offlineDbPath = ref('data/mqtt_offline.db')
const maxQueueSize = ref(10000)
const maxRetries = ref(5)
const retryInterval = ref(5000)
const saveLoading = ref(false)

const queueStatus = ref<{
  pending_count: number | null
  sent_count: number | null
  db_size: string | null
  oldest_timestamp: string | null
}>({
  pending_count: null,
  sent_count: null,
  db_size: null,
  oldest_timestamp: null,
})

const runningFields = computed(() => [
  { label: t('system.listenAddress'), value: statusData.value.current_config?.host || '0.0.0.0' },  // FIXED: 原问题-中文硬编码，改用i18n
  { label: t('mqttServer.tcpPort'), value: statusData.value.current_config?.port || 1883 },  // FIXED: 原问题-硬编码中文label
  { label: t('mqttServer.wsPort'), value: statusData.value.current_config?.ws_port || 8083 },  // FIXED: 原问题-硬编码中文label
])

function onStatusLoaded(data: any) {
  statusData.value = data
  isRunning.value = data.state === 'running'
  connections.value = data.running_info?.connections || 0

  const cfg = data.current_config?.offline_cache
  if (cfg) {
    offlineCacheEnabled.value = cfg.enabled ?? false
    offlineDbPath.value = cfg.db_path ?? 'data/mqtt_offline.db'
    maxQueueSize.value = cfg.max_queue_size ?? 10000
    maxRetries.value = cfg.max_retries ?? 5
    retryInterval.value = cfg.retry_interval ?? 5000
  }

  if (isRunning.value && offlineCacheEnabled.value) {
    fetchQueueStatus()
  }
}

async function fetchQueueStatus() {
  try {
    const resp = await http.get('/mqtt/offline-queue/status')
    queueStatus.value = {
      pending_count: resp.data?.data?.pending_count ?? null,
      sent_count: resp.data?.data?.sent_count ?? null,
      db_size: resp.data?.data?.db_size ?? null,
      oldest_timestamp: resp.data?.data?.oldest_timestamp ?? null,
    }
  } catch {
    // ignore
  }
}

async function handleSaveOfflineConfig() {
  saveLoading.value = true
  try {
    await serviceApi.updateConfig('mqtt_server', {
      offline_cache: {
        enabled: offlineCacheEnabled.value,
        db_path: offlineDbPath.value,
        max_queue_size: maxQueueSize.value,
        max_retries: maxRetries.value,
        retry_interval: retryInterval.value,
      },
    })
    msg.success(t('common.success'))
    if (isRunning.value && offlineCacheEnabled.value) {
      fetchQueueStatus()
    }
  } catch (e: any) {
    msg.error(e?.message || t('common.failed'))
  } finally {
    saveLoading.value = false
  }
}

watch(offlineCacheEnabled, (val) => {
  if (val && isRunning.value) {
    fetchQueueStatus()
  }
})
</script>
