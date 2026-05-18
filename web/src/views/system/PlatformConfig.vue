<template>
  <div class="platform-config-page">
    <n-card :title="t('platformConfig.title')"> <!-- FIXED: 原问题-中文硬编码 -->
      <template #header-extra>
        <n-button type="primary" @click="openAddModal">{{ t('platformConfig.addPlatform') }}</n-button> <!-- FIXED: 原问题-中文硬编码 -->
      </template>

      <template v-if="platforms.length === 0 && !loading">
        <n-empty :description="t('platformConfig.noPlatformDesc')" style="padding: 48px 0"> <!-- FIXED: 原问题-中文硬编码 -->
          <template #extra>
            <n-button type="primary" @click="openAddModal">{{ t('platformConfig.addPlatform') }}</n-button> <!-- FIXED: 原问题-中文硬编码 -->
          </template>
        </n-empty>
      </template>
      <template v-else>
        <n-data-table :columns="columns" :data="platforms" :loading="loading" />
      </template>
    </n-card>

    <n-modal v-model:show="showAddModal" preset="card" :title="modalTitle" style="width: 640px" :mask-closable="false">
      <n-steps :current="currentStep" size="small" style="margin-bottom: 20px">
        <n-step :title="t('platformConfig.selectPlatform')" /> <!-- FIXED: 原问题-中文硬编码 -->
        <n-step :title="t('platformConfig.fillConfig')" /> <!-- FIXED: 原问题-中文硬编码 -->
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
                <n-tag v-if="isPlatformConnected(p.name)" size="small" type="success">{{ t('platformConfig.connected') }}</n-tag> <!-- FIXED: 原问题-中文硬编码 -->
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
          {{ t('platformConfig.nextStepFillConfig') }} <!-- FIXED: 原问题-中文硬编码 -->
        </n-button>
      </div>

      <div v-if="currentStep === 2">
        <n-button text style="margin-bottom: 12px" @click="currentStep = 1">
          {{ t('platformConfig.backToSelect') }} <!-- FIXED: 原问题-中文硬编码 -->
        </n-button>

        <n-alert v-if="selectedPlatformInfo" type="info" style="margin-bottom: 16px">
          {{ t('platformConfig.configuring') }} <strong>{{ selectedPlatformInfo.label }}</strong> — {{ selectedPlatformInfo.description }} <!-- FIXED: 原问题-中文硬编码 -->
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
                :placeholder="field.required ? t('platformConfig.required') : t('platformConfig.optional')"
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
          <n-button @click="currentStep = 1">{{ t('platformConfig.prevStep') }}</n-button> <!-- FIXED: 原问题-中文硬编码 -->
          <n-button type="primary" :loading="connecting" @click="connectPlatform">{{ t('platformConfig.connect') }}</n-button> <!-- FIXED: 原问题-中文硬编码 -->
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
import { t } from '@/i18n'  // FIXED: 原问题-中文硬编码

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
  if (currentStep.value === 1) return t('platformConfig.addPlatformSelect')  // FIXED: 原问题-中文硬编码
  return t('platformConfig.addPlatformConfig')  // FIXED: 原问题-中文硬编码
})

const selectedPlatformInfo = computed(() => {
  return supportedPlatforms.value.find(p => p.name === formData.value.platform_name)
})

const dynamicFormRules = computed(() => {
  const rules: Record<string, any> = {}
  for (const field of platformFields.value) {
    if (field.required) {
      if (field.type === 'integer' || field.type === 'number') {
        rules['config.' + field.name] = {
          type: 'number',
          required: true,
          message: t('platformConfig.fieldRequired', { field: field.label || field.name }),  // FIXED: 原问题-中文硬编码
          trigger: ['blur', 'change'],
        }
      } else {
        rules['config.' + field.name] = {
          required: true,
          message: t('platformConfig.fieldRequired', { field: field.label || field.name }),  // FIXED: 原问题-中文硬编码
          trigger: ['blur', 'change'],
        }
      }
    }
  }
  return rules
})

function isPlatformConnected(name: string) {
  return platforms.value.some(p => p.name === name && p.connected)
}

const columns = [
  { title: t('platformConfig.platform'), key: 'name', width: 150 },  // FIXED: 原问题-中文硬编码
  { title: t('platformConfig.version'), key: 'version', width: 80 },  // FIXED: 原问题-中文硬编码
  {
    title: t('platformConfig.status'),  // FIXED: 原问题-中文硬编码
    key: 'connected',
    width: 100,
    render: (row: any) =>
      h(NTag, { type: row.connected ? 'success' : 'error', size: 'small' }, () =>
        row.connected ? t('platformConfig.connected') : t('platformConfig.notConnected')  // FIXED: 原问题-中文硬编码
      ),
  },
  {
    title: t('platformConfig.actions'),  // FIXED: 原问题-中文硬编码
    key: 'actions',
    width: 120,
    render: (row: any) =>
      h(NButton, { size: 'small', type: 'error', onClick: () => disconnectPlatform(row.name) }, () => t('platformConfig.disconnect')),  // FIXED: 原问题-中文硬编码
  },
]

async function loadPlatforms() {
  loading.value = true
  try {
    const data = await platformApi.list()
    platforms.value = data?.platforms || []
    supportedPlatforms.value = data?.supported || []
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || t('platformConfig.loadListFailed'))  // FIXED: 原问题-中文硬编码
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
    message.warning(t('platformConfig.alreadyConnected', { label: p.label }))  // FIXED: 原问题-中文硬编码
    return
  }
  formData.value.platform_name = p.name
  formData.value.config = {}
  platformFields.value = []
}

async function goToStep2() {
  if (!formData.value.platform_name) {
    message.warning(t('platformConfig.selectPlatformFirst'))  // FIXED: 原问题-中文硬编码
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
    if (data?.config_schema?.fields) {
      platformFields.value = data.config_schema.fields
      const newConfig: Record<string, any> = {}
      for (const f of platformFields.value) {
        if (f.type === 'integer' || f.type === 'number') {
          newConfig[f.name] = f.default !== undefined ? f.default : undefined
        } else {
          newConfig[f.name] = f.default ?? null
        }
      }
      formData.value.config = newConfig
    }
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || t('platformConfig.fetchSchemaFailed'))  // FIXED: 原问题-中文硬编码
  } finally {
    schemaLoading.value = false
  }
}

async function connectPlatform() {
  try {
    await formRef.value?.validate()
  } catch {
    message.warning(t('platformConfig.fillRequired'))  // FIXED: 原问题-中文硬编码
    return
  }

  connecting.value = true
  try {
    await platformApi.connect(formData.value.platform_name, formData.value.config)
    showAddModal.value = false
    await loadPlatforms()
    message.success(t('platformConfig.connectSuccess'))  // FIXED: 原问题-中文硬编码
  } catch (e: any) {
    const detail = e?.response?.data?.detail || e?.message || t('platformConfig.connectFailed')  // FIXED: 原问题-中文硬编码
    message.error(detail)
  } finally {
    connecting.value = false
  }
}

async function disconnectPlatform(name: string) {
  dialog.warning({
    title: t('platformConfig.confirmDisconnect'),  // FIXED: 原问题-中文硬编码
    content: t('platformConfig.confirmDisconnectContent', { name }),  // FIXED: 原问题-中文硬编码
    positiveText: t('platformConfig.disconnect'),  // FIXED: 原问题-中文硬编码
    negativeText: t('common.cancel'),  // FIXED: 原问题-中文硬编码
    onPositiveClick: async () => {
      try {
        await platformApi.disconnect(name)
        await loadPlatforms()
        message.success(t('platformConfig.disconnected'))  // FIXED: 原问题-中文硬编码
      } catch (e: any) {
        message.error(e?.response?.data?.detail || e?.message || t('platformConfig.disconnectFailed'))  // FIXED: 原问题-中文硬编码
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
