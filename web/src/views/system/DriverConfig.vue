<template>
  <div class="driver-config-page">
    <n-card title="驱动配置">
      <template #header-extra>
        <n-button type="primary" @click="loadDrivers">刷新</n-button>
      </template>
      <n-data-table :columns="columns" :data="drivers" :loading="loading" />
    </n-card>

    <n-modal v-model:show="showSchemaModal" preset="card" title="驱动配置模板" style="width: 600px">
      <n-descriptions bordered :column="1" label-placement="left" v-if="currentSchema">
        <n-descriptions-item v-for="field in currentSchema.fields" :key="field.name" :label="field.label || field.name">
          <n-space align="center">
            <n-tag size="small" :type="field.required ? 'error' : 'default'">{{ field.required ? '必填' : '可选' }}</n-tag>
            <n-tag size="small">{{ field.type }}</n-tag>
            <span v-if="field.default" style="color: #999">默认: {{ field.default }}</span>
            <span v-if="field.options" style="color: #666">选项: {{ field.options.join(' / ') }}</span>
          </n-space>
        </n-descriptions-item>
      </n-descriptions>
    </n-modal>

    <n-modal v-model:show="showDiscoverModal" preset="card" title="设备发现" style="width: 500px">
      <n-spin :show="discovering">
        <n-empty v-if="discoveredDevices.length === 0 && !discovering" description="点击发现按钮搜索设备" />
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
        <n-button type="primary" :loading="discovering" @click="doDiscover">开始发现</n-button>
      </template>
    </n-modal>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, h } from 'vue'
import { NCard, NButton, NDataTable, NTag, NSpace, NModal, NDescriptions, NDescriptionsItem, NList, NListItem, NThing, NEmpty, NSpin, useMessage } from 'naive-ui'
import { driverApi } from '../../api'

const message = useMessage()
const drivers = ref<any[]>([])
const loading = ref(false)
const showSchemaModal = ref(false)
const currentSchema = ref<any>(null)
const showDiscoverModal = ref(false)
const discovering = ref(false)
const discoveredDevices = ref<any[]>([])
const currentDriverName = ref('')

const columns = [
  { title: '驱动名称', key: 'name', width: 160 },
  { title: '版本', key: 'version', width: 80 },
  { title: '支持协议', key: 'protocols', render: (row: any) => h(NSpace, { size: 4 }, () => row.protocols.map((p: string) => h(NTag, { size: 'small', type: 'info' }, () => p))) },
  { title: '操作', key: 'actions', width: 200, render: (row: any) => h(NSpace, {}, () => [
    h(NButton, { size: 'small', onClick: () => viewSchema(row.name) }, () => '配置模板'),
    h(NButton, { size: 'small', type: 'primary', onClick: () => startDiscover(row.name) }, () => '发现设备'),
  ]) },
]

async function loadDrivers() {
  loading.value = true
  try {
    const data = await driverApi.list()
    drivers.value = data?.drivers || []
  } catch (e) { message.error('加载驱动列表失败') }
  finally { loading.value = false }
}

async function viewSchema(name: string) {
  try {
    const data = await driverApi.configSchema(name)
    if (data) { currentSchema.value = data.schema; showSchemaModal.value = true }
  } catch (e) { message.error('获取配置模板失败') }
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
  } catch (e) { message.error('设备发现失败') }
  finally { discovering.value = false }
}

onMounted(() => { loadDrivers() })
</script>

<style scoped>
.driver-config-page { padding: 16px; }
</style>
