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
        <n-form ref="offlineFormRef" :model="offlineForm" :rules="offlineFormRules" :show-label="false">
          <n-space vertical size="medium">
            <n-space align="center">
              <span>{{ t('mqtt.offlineCacheEnabled') }}</span>
              <n-switch v-model:value="offlineCacheEnabled" />
            </n-space>
            <n-space vertical v-if="offlineCacheEnabled">
              <n-form-item path="offlineDbPath">
                <n-space align="center">
                  <span style="min-width: 120px">{{ t('mqtt.offlineDbPath') }}</span>
                  <n-input v-model:value="offlineDbPath" placeholder="data/mqtt_offline.db" style="width: 300px" />
                </n-space>
              </n-form-item>
              <n-form-item path="maxQueueSize">
                <n-space align="center">
                  <span style="min-width: 120px">{{ t('mqtt.maxQueueSize') }}</span>
                  <n-input-number v-model:value="maxQueueSize" :min="1" :max="100000" style="width: 200px" />
                </n-space>
              </n-form-item>
              <n-form-item path="maxRetries">
                <n-space align="center">
                  <span style="min-width: 120px">{{ t('mqtt.maxRetries') }}</span>
                  <n-input-number v-model:value="maxRetries" :min="0" :max="100" style="width: 200px" />
                </n-space>
              </n-form-item>
              <n-form-item path="retryInterval">
                <n-space align="center">
                  <span style="min-width: 120px">{{ t('mqtt.retryInterval') }}</span>
                  <n-input-number v-model:value="retryInterval" :min="100" :max="60000" style="width: 200px" />
                </n-space>
              </n-form-item>
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
        </n-form>
      </n-card>
    </template>
  </ServiceManager>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { t } from '@/i18n'  // FIXED: 原问题-中文硬编码，改用i18n
import { extractError } from '@/utils/errorCodes'
import ServiceManager from '@/components/ServiceManager.vue'
import { serviceApi, mqttForwarderApi, systemApi } from '@/api'
import { message as msg } from '@/utils/discreteApi'
// [AUDIT-FIX] 严重级-MQTT 离线转发配置属敏感写操作，需函数级权限校验
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

const connections = ref(0)
const isRunning = ref(false)
const statusData = ref<any>({})

const offlineFormRef = ref<any>(null)
const offlineCacheEnabled = ref(false)
const offlineDbPath = ref('data/mqtt_offline.db')
const maxQueueSize = ref(10000)
const maxRetries = ref(5)
const retryInterval = ref(5000)
const saveLoading = ref(false)

const offlineForm = computed(() => ({
  offlineDbPath: offlineDbPath.value,
  maxQueueSize: maxQueueSize.value,
  maxRetries: maxRetries.value,
  retryInterval: retryInterval.value,
}))

const offlineFormRules = computed(() => ({
  offlineDbPath: { required: true, message: t('mqttServer.pathRequired'), trigger: ['input', 'blur'] },
  maxQueueSize: { type: 'number', required: true, min: 1, max: 1000000, message: t('mqttServer.queueSizeRange'), trigger: ['input', 'blur'] },
  maxRetries: { type: 'number', required: true, min: 0, max: 100, message: t('mqttServer.retriesRange'), trigger: ['input', 'blur'] },
  retryInterval: { type: 'number', required: true, min: 1, max: 3600, message: t('mqttServer.retryIntervalRange'), trigger: ['input', 'blur'] },
}))

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

  // offline_cache config belongs to the 'mqtt' config section (MQTTConfig),
  // not 'mqtt_server' (MqttServerConfig). Fetch it separately.
  fetchOfflineCacheConfig()

  if (isRunning.value && offlineCacheEnabled.value) {
    fetchQueueStatus()
  }
}

async function fetchOfflineCacheConfig() {
  try {
    // offline_cache fields belong to the 'mqtt' config section (MQTTConfig),
    // not 'mqtt_server' (MqttServerConfig). Read from full config.
    const fullConfig = await systemApi.getConfig()
    const cfg = fullConfig?.mqtt
    if (cfg) {
      offlineCacheEnabled.value = cfg.offline_cache_enabled ?? false
      offlineDbPath.value = cfg.offline_db_path ?? 'data/mqtt_offline.db'
      maxQueueSize.value = cfg.max_queue_size ?? 10000
      maxRetries.value = cfg.max_retries ?? 5
      retryInterval.value = cfg.retry_interval ?? 5000
    }
  } catch {
    // use defaults
  }
}

async function fetchQueueStatus() {
  try {
    const data = await mqttForwarderApi.getOfflineQueueStatus()
    queueStatus.value = {
      pending_count: data?.pending_count ?? null,
      sent_count: data?.sent_count ?? null,
      db_size: data?.db_size_bytes != null ? String(data.db_size_bytes) : null,
      oldest_timestamp: data?.oldest_timestamp ?? null,
    }
  } catch {
    // ignore
  }
}

async function handleSaveOfflineConfig() {
  if (!auth.isOperator) { msg.warning(t('common.permissionDenied')); return }
  try {
    await offlineFormRef.value?.validate()
  } catch {
    return
  }
  saveLoading.value = true
  try {
    // offline_cache fields belong to the 'mqtt' config section (MQTTConfig),
    // not 'mqtt_server' (MqttServerConfig). Save to the correct section.
    await systemApi.updateConfigSection('mqtt', {
      offline_cache_enabled: offlineCacheEnabled.value,
      offline_db_path: offlineDbPath.value,
      max_queue_size: maxQueueSize.value,
      max_retries: maxRetries.value,
      retry_interval: retryInterval.value,
    })
    msg.success(t('common.success'))
    if (isRunning.value && offlineCacheEnabled.value) {
      fetchQueueStatus()
    }
  } catch (e: any) {
    msg.error(extractError(e, t('common.failed')))
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
