<template>
  <div class="platform-config-page">
    <n-card title="平台对接配置">
      <template #header-extra>
        <n-button type="primary" @click="showAddModal = true">添加平台</n-button>
      </template>

      <n-data-table :columns="columns" :data="platforms" :loading="loading" />

      <n-card title="支持的平台" size="small" style="margin-top: 16px">
        <n-grid :cols="3" :x-gap="12" :y-gap="12">
          <n-gi v-for="p in supportedPlatforms" :key="p.name">
            <n-card size="small" hoverable @click="selectPlatform(p)">
              <n-thing :title="p.label" :description="p.description" />
            </n-card>
          </n-gi>
        </n-grid>
      </n-card>
    </n-card>

    <n-modal v-model:show="showAddModal" preset="card" title="添加平台对接" style="width: 600px">
      <n-form ref="formRef" :model="formData" label-placement="left" label-width="120">
        <n-form-item label="平台类型">
          <n-select v-model:value="formData.platform_name" :options="platformOptions" placeholder="选择平台" />
        </n-form-item>
        <n-form-item v-for="field in platformFields" :key="field.name" :label="field.label || field.name">
          <n-input v-if="field.type === 'string'" v-model:value="formData.config[field.name]"
            :type="field.secret ? 'password' : 'text'" :placeholder="field.required ? '必填' : '可选'" />
          <n-input-number v-else-if="field.type === 'integer'" v-model:value="formData.config[field.name]"
            :placeholder="field.default?.toString()" style="width: 100%" />
        </n-form-item>
      </n-form>
      <template #footer>
        <n-space justify="end">
          <n-button @click="showAddModal = false">取消</n-button>
          <n-button type="primary" :loading="connecting" @click="connectPlatform">连接</n-button>
        </n-space>
      </template>
    </n-modal>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, h } from 'vue'
import { NCard, NButton, NDataTable, NTag, NSpace, NModal, NForm, NFormItem, NSelect, NInput, NInputNumber, NGrid, NGi, NThing, useMessage } from 'naive-ui'
import { platformApi } from '@/api'

const message = useMessage()
const platforms = ref<any[]>([])
const supportedPlatforms = ref<any[]>([])
const loading = ref(false)
const showAddModal = ref(false)
const connecting = ref(false)
const platformFields = ref<any[]>([])
const formData = ref<{ platform_name: string; config: Record<string, any> }>({ platform_name: '', config: {} })

const platformOptions = computed(() => supportedPlatforms.value.map(p => ({ label: p.label, value: p.name })))

const columns = [
  { title: '平台', key: 'name', width: 150 },
  { title: '版本', key: 'version', width: 80 },
  { title: '状态', key: 'connected', width: 100, render: (row: any) => h(NTag, { type: row.connected ? 'success' : 'error', size: 'small' }, () => row.connected ? '已连接' : '未连接') },
  { title: '操作', key: 'actions', width: 120, render: (row: any) => h(NButton, { size: 'small', type: 'error', onClick: () => disconnectPlatform(row.name) }, () => '断开') },
]

async function loadPlatforms() {
  loading.value = true
  try {
    const data = await platformApi.list()
    platforms.value = data?.platforms || []
    supportedPlatforms.value = data?.supported || []
  } catch (e) { message.error('加载平台列表失败') }
  finally { loading.value = false }
}

async function selectPlatform(p: any) {
  formData.value.platform_name = p.name
  formData.value.config = {}
  try {
    const data = await platformApi.configSchema(p.name)
    if (data?.schema?.fields) {
      platformFields.value = data.schema.fields
      for (const f of platformFields.value) {
        formData.value.config[f.name] = f.default ?? null
      }
    }
  } catch (e) { message.error('获取配置模板失败') }
  showAddModal.value = true
}

async function connectPlatform() {
  connecting.value = true
  try {
    await platformApi.connect(formData.value.platform_name, formData.value.config)
    showAddModal.value = false
    await loadPlatforms()
    message.success('平台连接成功')
  } catch (e) { message.error('连接失败') }
  finally { connecting.value = false }
}

async function disconnectPlatform(name: string) {
  try {
    await platformApi.disconnect(name)
    await loadPlatforms()
    message.success('已断开')
  } catch (e) { message.error('断开失败') }
}

onMounted(() => { loadPlatforms() })
</script>

<style scoped>
.platform-config-page { padding: 16px; }
</style>
