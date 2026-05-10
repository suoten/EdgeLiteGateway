<template>
  <div class="driver-config-page">
    <n-card title="驱动配置">
      <template #header-extra>
        <n-button type="primary" @click="loadDrivers">刷新</n-button>
      </template>
      <n-data-table :columns="columns" :data="drivers" :loading="loading" />
    </n-card>

    <!-- 配置模板弹窗 -->
    <n-modal v-model:show="showSchemaModal" preset="card" :title="currentSchemaTitle" style="width: 700px">
      <n-alert v-if="currentSchema?.description" type="info" :show-icon="false" style="margin-bottom: 16px">
        {{ currentSchema.description }}
      </n-alert>
      <n-empty v-if="!currentSchema?.fields?.length" description="暂无配置说明" />
      <n-descriptions v-else bordered :column="1" label-placement="left">
        <n-descriptions-item v-for="field in currentSchema.fields" :key="field.name" :label="field.label || field.name">
          <n-space vertical :size="4">
            <!-- 参数说明 -->
            <n-space align="center" :size="8">
              <n-tag size="small" :type="field.required ? 'error' : 'default'">{{ field.required ? '必填' : '可选' }}</n-tag>
              <n-tag size="small" type="info">{{ typeMap[field.type] || field.type }}</n-tag>
              <n-tag v-if="field.secret" size="small" type="warning">敏感</n-tag>
            </n-space>
            <!-- 描述文字 -->
            <n-text v-if="field.description" depth="3" style="font-size: 13px">
              {{ field.description }}
            </n-text>
            <!-- 默认值 -->
            <n-text v-if="field.default !== undefined && field.default !== ''" depth="3" style="font-size: 13px">
              默认值: <n-text code style="font-size: 12px">{{ field.default }}</n-text>
            </n-text>
            <!-- 可选项 -->
            <n-space v-if="field.options" :size="8" style="flex-wrap: wrap">
              <n-tag v-for="opt in field.options" :key="opt" size="small" type="success">{{ opt }}</n-tag>
            </n-space>
          </n-space>
        </n-descriptions-item>
      </n-descriptions>
    </n-modal>

    <!-- 设备发现弹窗 -->
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
import { ref, computed, onMounted, h } from 'vue'
import { NCard, NButton, NDataTable, NTag, NSpace, NModal, NDescriptions, NDescriptionsItem, NList, NListItem, NThing, NEmpty, NSpin, NAlert, NText, useMessage } from 'naive-ui'
import { driverApi } from '@/api'

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
  const schema = currentSchema.value
  if (schema?.description) {
    return `${name} - 配置模板`
  }
  return `${name} - 配置模板`
})

// 类型映射：把技术类型转为中文
const typeMap: Record<string, string> = {
  string: '文本',
  integer: '整数',
  number: '数值',
  boolean: '布尔',
  array: '数组',
  object: '对象',
}

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
  } catch (e: any) { message.error(e?.response?.data?.detail || e?.message || '加载驱动列表失败') }
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
  } catch (e: any) { message.error(e?.response?.data?.detail || e?.message || '获取配置模板失败') }
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
  } catch (e: any) { message.error(e?.response?.data?.detail || e?.message || '设备发现失败') }
  finally { discovering.value = false }
}

onMounted(() => { loadDrivers() })
</script>

<style scoped>
.driver-config-page { padding: 16px; }
</style>
