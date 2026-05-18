<template>
  <div class="driver-config-page">
    <n-card :title="t('driver.title')">
      <template #header-extra>
        <n-button type="primary" @click="loadDrivers">{{ t('driver.refresh') }}</n-button>
      </template>
      <n-data-table :columns="columns" :data="drivers" :loading="loading" />
    </n-card>

    <n-modal v-model:show="showSchemaModal" preset="card" :title="currentSchemaTitle" style="width: 700px">
      <n-alert v-if="currentSchema?.description" type="info" :show-icon="false" style="margin-bottom: 16px">
        {{ currentSchema.description }}
      </n-alert>
      <n-empty v-if="!currentSchema?.fields?.length" :description="t('driver.noSchema')" />
      <n-descriptions v-else bordered :column="1" label-placement="left">
        <n-descriptions-item v-for="field in currentSchema.fields" :key="field.name" :label="field.label || field.name">
          <n-space vertical :size="4">
            <n-space align="center" :size="8">
              <n-tag size="small" :type="field.required ? 'error' : 'default'">{{ field.required ? t('driver.required') : t('driver.optional') }}</n-tag>
              <n-tag size="small" type="info">{{ typeMap[field.type] || field.type }}</n-tag>
              <n-tag v-if="field.secret" size="small" type="warning">{{ t('driver.sensitive') }}</n-tag>
            </n-space>
            <n-text v-if="field.description" depth="3" style="font-size: 13px">
              {{ field.description }}
            </n-text>
            <n-text v-if="field.default !== undefined && field.default !== ''" depth="3" style="font-size: 13px">
              {{ t('driver.defaultValue') }} <n-text code style="font-size: 12px">{{ field.default }}</n-text>
            </n-text>
            <n-space v-if="field.options" :size="8" style="flex-wrap: wrap">
              <n-tag v-for="opt in field.options" :key="opt" size="small" type="success">{{ opt }}</n-tag>
            </n-space>
          </n-space>
        </n-descriptions-item>
      </n-descriptions>
    </n-modal>

    <n-modal v-model:show="showDiscoverModal" preset="card" :title="t('driver.deviceDiscover')" style="width: 500px">
      <n-spin :show="discovering">
        <n-empty v-if="discoveredDevices.length === 0 && !discovering" :description="t('driver.discoverHint')" />
        <n-list v-else>
          <n-list-item v-for="dev in discoveredDevices" :key="dev.device_id">
            <n-thing :title="dev.name">
              <template #description>
                <n-space>
                  <n-tag size="small">{{ dev.protocol }}</n-tag>
                  <span style="font-size: 12px; color: #666">{{ dev.ip }}</span>
                </n-space>
              </template>
            </n-thing>
          </n-list-item>
        </n-list>
      </n-spin>
      <template #footer>
        <n-button type="primary" :loading="discovering" @click="doDiscover">{{ t('driver.startDiscover') }}</n-button>
      </template>
    </n-modal>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, h } from 'vue'
import { NCard, NButton, NDataTable, NTag, NSpace, NModal, NDescriptions, NDescriptionsItem, NList, NListItem, NThing, NEmpty, NSpin, NAlert, NText, useMessage } from 'naive-ui'
import { t } from '@/i18n'  // FIXED: 原问题-#注释导致编译失败，改为//注释
import { driverApi } from '@/api'

// FIXED: 原问题-DriverConfig.vue全部中文硬编码，改为i18n
const message = useMessage()
const drivers = ref<any[]>([])
const loading = ref(false)
const showSchemaModal = ref(false)
const currentSchema = ref<any>(null)
const currentDriverName = ref('')
const showDiscoverModal = ref(false)
const discovering = ref(false)
const discoveredDevices = ref<any[]>([])

const currentSchemaTitle = computed(() => {
  const name = currentDriverName.value
  return `${name} - ${t('driver.configTemplate')}`
})

const typeMap = computed<Record<string, string>>(() => ({
  string: t('driver.typeText'),
  integer: t('driver.typeInteger'),
  number: t('driver.typeNumber'),
  boolean: t('driver.typeBoolean'),
  array: t('driver.typeArray'),
  object: t('driver.typeObject'),
}))

const columns = computed(() => [
  { title: t('driver.colName'), key: 'name', width: 160 },
  { title: t('driver.colVersion'), key: 'version', width: 80 },
  { title: t('driver.colProtocol'), key: 'protocols', render: (row: any) => h(NSpace, { size: 4 }, () => (row.protocols ?? []).map((p: string) => h(NTag, { size: 'small', type: 'info' }, () => p))) },
  { title: t('driver.colActions'), key: 'actions', width: 200, render: (row: any) => h(NSpace, {}, () => [
    h(NButton, { size: 'small', onClick: () => viewSchema(row.name) }, () => t('driver.colConfig')),
    h(NButton, { size: 'small', type: 'primary', onClick: () => startDiscover(row.name) }, () => t('driver.colDiscover')),
  ]) },
])

async function loadDrivers() {
  loading.value = true
  try {
    const data = await driverApi.list()
    drivers.value = data?.drivers || []
  } catch (e: any) { message.error(e?.response?.data?.detail || e?.message || t('driver.loadListFailed')) }
  finally { loading.value = false }
}

async function viewSchema(name: string) {
  currentDriverName.value = name
  try {
    const data = await driverApi.configSchema(name)
    if (data) {
      currentSchema.value = data.config_schema
      showSchemaModal.value = true
    }
  } catch (e: any) { message.error(e?.response?.data?.detail || e?.message || t('driver.loadSchemaFailed')) }
}

function startDiscover(name: string) {
  currentDriverName.value = name
  discoveredDevices.value = []
  showDiscoverModal.value = true
}

async function doDiscover() {
  discovering.value = true
  try {
    const data = await driverApi.discover(currentDriverName.value)
    discoveredDevices.value = data?.devices || []
  } catch (e: any) { message.error(e?.response?.data?.detail || e?.message || t('driver.discoverFailed')) }
  finally { discovering.value = false }
}

onMounted(() => { loadDrivers() })
</script>

<style scoped>
.driver-config-page { padding: 16px; }
</style>
