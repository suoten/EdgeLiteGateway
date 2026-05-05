<template>
  <div class="platform-config-page">
    <n-card title="平台对接配置">
      <template #header-extra>
        <n-button type="primary" @click="openAddModal">添加平台</n-button>
      </template>

      <template v-if="platforms.length === 0 && !loading">
        <n-empty description="暂无已连接的平台，点击右上角「添加平台」开始对接" style="padding: 48px 0">
          <template #extra>
            <n-button type="primary" @click="openAddModal">添加平台</n-button>
          </template>
        </n-empty>
      </template>
      <template v-else>
        <n-data-table :columns="columns" :data="platforms" :loading="loading" />
      </template>
    </n-card>

    <n-modal v-model:show="showAddModal" preset="card" :title="modalTitle" style="width: 640px" :mask-closable="false">
      <n-steps :current="currentStep" size="small" style="margin-bottom: 20px">
        <n-step title="选择平台" />
        <n-step title="填写配置" />
      </n-steps>

      <div v-if="currentStep === 1">
        <n-grid :cols="2" :x-gap="12" :y-gap="12">
          <n-gi v-for="p in supportedPlatforms" :key="p.name">
            <n-card
              size="small"
              hoverable
              :class="['platform-card', { 'platform-card--selected': formData.platform_name === p.name }]"
              @click="onSelectPlatform(p)"
            >
              <div class="platform-card__header">
                <span class="platform-card__name">{{ p.label }}</span>
                <n-tag v-if="isPlatformConnected(p.name)" size="small" type="success">已连接</n-tag>
              </div>
              <div class="platform-card__desc">{{ p.description }}</div>
            </n-card>
          </n-gi>
        </n-grid>
        <n-button
          type="primary"
          block
          style="margin-top: 16px"
          :disabled="!formData.platform_name"
          @click="goToStep2"
        >
          下一步：填写配置
        </n-button>
      </div>

      <div v-if="currentStep === 2">
        <n-button text style="margin-bottom: 12px" @click="currentStep = 1">
          ← 返回选择平台
        </n-button>

        <n-alert v-if="selectedPlatformInfo" type="info" style="margin-bottom: 16px">
          正在配置 <strong>{{ selectedPlatformInfo.label }}</strong> — {{ selectedPlatformInfo.description }}
        </n-alert>

        <n-spin :show="schemaLoading">
          <n-form
            ref="formRef"
            :model="formData"
            :rules="dynamicFormRules"
            label-placement="left"
            label-width="120"
          >
            <n-form-item
              v-for="field in platformFields"
              :key="field.name"
              :label="field.label || field.name"
              :path="'config.' + field.name"
            >
              <n-input
                v-if="field.type === 'string'"
                v-model:value="formData.config[field.name]"
                :type="field.secret ? 'password' : 'text'"
                :placeholder="field.required ? '必填' : '可选'"
                show-password-on="click"
              />
              <n-input-number
                v-else-if="field.type === 'integer'"
                v-model:value="formData.config[field.name]"
                :placeholder="field.default?.toString()"
                style="width: 100%"
              />
            </n-form-item>
          </n-form>
        </n-spin>

        <n-space justify="end" style="margin-top: 8px">
          <n-button @click="currentStep = 1">上一步</n-button>
          <n-button type="primary" :loading="connecting" @click="connectPlatform">连接</n-button>
        </n-space>
      </div>
    </n-modal>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, h, watch } from 'vue'
import {
  NCard, NButton, NDataTable, NTag, NSpace, NModal, NForm, NFormItem,
  NInput, NInputNumber, NGrid, NGi, NSteps, NStep, NAlert, NEmpty, NSpin,
  useMessage, useDialog,
} from 'naive-ui'
import { platformApi } from '@/api'

const message = useMessage()
const dialog = useDialog()
const platforms = ref<any[]>([])
const supportedPlatforms = ref<any[]>([])
const loading = ref(false)
const showAddModal = ref(false)
const connecting = ref(false)
const schemaLoading = ref(false)
const platformFields = ref<any[]>([])
const currentStep = ref(1)
const formData = ref<{ platform_name: string; config: Record<string, any> }>({
  platform_name: '',
  config: {},
})
const formRef = ref<any>(null)

const modalTitle = computed(() => {
  if (currentStep.value === 1) return '添加平台对接 — 选择平台'
  return '添加平台对接 — 填写配置'
})

const selectedPlatformInfo = computed(() => {
  return supportedPlatforms.value.find(p => p.name === formData.value.platform_name)
})

const dynamicFormRules = computed(() => {
  const rules: Record<string, any> = {}
  for (const field of platformFields.value) {
    if (field.required) {
      rules['config.' + field.name] = {
        required: true,
        message: `${field.label || field.name}不能为空`,
        trigger: ['blur', 'change'],
      }
    }
  }
  return rules
})

function isPlatformConnected(name: string) {
  return platforms.value.some(p => p.name === name && p.connected)
}

const columns = [
  { title: '平台', key: 'name', width: 150 },
  { title: '版本', key: 'version', width: 80 },
  {
    title: '状态',
    key: 'connected',
    width: 100,
    render: (row: any) =>
      h(NTag, { type: row.connected ? 'success' : 'error', size: 'small' }, () =>
        row.connected ? '已连接' : '未连接'
      ),
  },
  {
    title: '操作',
    key: 'actions',
    width: 120,
    render: (row: any) =>
      h(NButton, { size: 'small', type: 'error', onClick: () => disconnectPlatform(row.name) }, () => '断开'),
  },
]

async function loadPlatforms() {
  loading.value = true
  try {
    const data = await platformApi.list()
    platforms.value = data?.platforms || []
    supportedPlatforms.value = data?.supported || []
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || '加载平台列表失败')
  } finally {
    loading.value = false
  }
}

function openAddModal() {
  formData.value = { platform_name: '', config: {} }
  platformFields.value = []
  currentStep.value = 1
  showAddModal.value = true
}

async function onSelectPlatform(p: any) {
  if (isPlatformConnected(p.name)) {
    message.warning(`${p.label} 已连接，如需重新配置请先断开`)
    return
  }
  formData.value.platform_name = p.name
  formData.value.config = {}
  platformFields.value = []
}

async function goToStep2() {
  if (!formData.value.platform_name) {
    message.warning('请先选择一个平台')
    return
  }
  currentStep.value = 2
  await loadConfigSchema()
}

async function loadConfigSchema() {
  if (!formData.value.platform_name) return
  schemaLoading.value = true
  try {
    const data = await platformApi.configSchema(formData.value.platform_name)
    if (data?.schema?.fields) {
      platformFields.value = data.schema.fields
      const newConfig: Record<string, any> = {}
      for (const f of platformFields.value) {
        newConfig[f.name] = f.default ?? null
      }
      formData.value.config = newConfig
    }
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || '获取配置模板失败')
  } finally {
    schemaLoading.value = false
  }
}

async function connectPlatform() {
  try {
    await formRef.value?.validate()
  } catch {
    message.warning('请填写所有必填配置项')
    return
  }

  connecting.value = true
  try {
    await platformApi.connect(formData.value.platform_name, formData.value.config)
    showAddModal.value = false
    await loadPlatforms()
    message.success('平台连接成功')
  } catch (e: any) {
    const detail = e?.response?.data?.detail || e?.message || '连接失败'
    message.error(detail)
  } finally {
    connecting.value = false
  }
}

async function disconnectPlatform(name: string) {
  dialog.warning({
    title: '确认断开',
    content: `确定断开平台 "${name}" 的连接？断开后数据上报将中断。`,
    positiveText: '断开',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await platformApi.disconnect(name)
        await loadPlatforms()
        message.success('已断开')
      } catch (e: any) {
        message.error(e?.response?.data?.detail || e?.message || '断开失败')
      }
    },
  })
}

onMounted(() => {
  loadPlatforms()
})
</script>

<style scoped>
.platform-config-page {
  padding: 16px;
}

.platform-card {
  cursor: pointer;
  transition: all 0.2s ease;
  border: 2px solid transparent;
}

.platform-card:hover {
  border-color: var(--n-border-color-hover);
}

.platform-card--selected {
  border-color: var(--n-primary-color);
  background-color: var(--n-primary-color-suppl);
}

.platform-card__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 4px;
}

.platform-card__name {
  font-weight: 600;
  font-size: 14px;
}

.platform-card__desc {
  font-size: 12px;
  color: var(--n-text-color-3);
}
</style>
