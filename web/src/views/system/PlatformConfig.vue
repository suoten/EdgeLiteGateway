<template>
  <div class="platform-config-page">
    <n-card :title="t('platformConfig.title')">
      <template #header-extra>
        <n-space>
          <n-button @click="$router.push('/system/platforms/dashboard')">
            {{ t('platformConfig.viewDashboard') }}
          </n-button>
          <n-button type="primary" @click="openAddModal">{{ t('platformConfig.addPlatform') }}</n-button>
        </n-space>
      </template>

      <template v-if="platforms.length === 0 && !loading">
        <n-empty :description="t('platformConfig.noPlatformDesc')" style="padding: 48px 0">
          <template #extra>
            <n-button type="primary" @click="openAddModal">{{ t('platformConfig.addPlatform') }}</n-button>
          </template>
        </n-empty>
      </template>
      <template v-else>
        <n-data-table :columns="columns" :data="platforms" :loading="loading">
          <template #empty>
            <n-empty :description="t('common.noData')" size="small" />
          </template>
        </n-data-table>
      </template>
    </n-card>

    <n-modal v-model:show="showAddModal" preset="card" :title="modalTitle" style="width: 780px; max-width: 95vw; max-height: 85vh; overflow-y: auto" :mask-closable="false" :close-on-esc="true" :auto-focus="true" :close-on-esc-aria-label="t('common.closeDialog')">
      <n-steps :current="currentStep" size="small" style="margin-bottom: 20px">
        <n-step :title="t('platformConfig.selectPlatform')" />
        <n-step :title="t('platformConfig.fillConfig')" />
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
                <n-tag v-if="isPlatformConnected(p.name)" size="small" type="success">{{ t('platformConfig.connected') }}</n-tag>
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
          {{ t('platformConfig.nextStepFillConfig') }}
        </n-button>
      </div>

      <div v-if="currentStep === 2">
        <n-button text style="margin-bottom: 12px" @click="currentStep = 1">
          {{ t('platformConfig.backToSelect') }}
        </n-button>

        <n-alert v-if="selectedPlatformInfo" type="info" style="margin-bottom: 16px">
          {{ t('platformConfig.configuring') }} <strong>{{ selectedPlatformInfo.label }}</strong> — {{ selectedPlatformInfo.description }}
        </n-alert>

        <n-spin :show="schemaLoading">
          <n-form
            ref="formRef"
            :model="formData"
            :rules="dynamicFormRules"
            label-placement="left"
            label-width="140"
          >
            <n-collapse :default-expanded-names="['base', 'mqtt', 'topic', 'payload']">
              <n-collapse-item :title="t('platformConfig.sectionBase')" name="base">
                <n-form-item
                  v-for="field in baseFields"
                  :key="field.name"
                  :label="field.label || field.name"
                  :path="'config.' + field.name"
                >
                  <n-input
                    v-if="field.type === 'string'"
                    v-model:value="formData.config[field.name]"
                    :type="field.secret ? 'password' : 'text'"
                    :placeholder="field.placeholder || (field.required ? t('platformConfig.required') : t('platformConfig.optional'))"
                    show-password-on="click"
                  />
                  <n-input-number
                    v-else-if="field.type === 'integer'"
                    v-model:value="formData.config[field.name]"
                    :placeholder="field.default?.toString()"
                    style="width: 100%"
                  />
                </n-form-item>
              </n-collapse-item>

              <n-collapse-item :title="t('platformConfig.sectionMqtt')" name="mqtt">
                <n-form-item
                  v-for="field in mqttFields"
                  :key="field.name"
                  :label="field.label || field.name"
                  :path="'config.' + field.name"
                >
                  <n-input
                    v-if="field.type === 'string'"
                    v-model:value="formData.config[field.name]"
                    :type="field.secret ? 'password' : 'text'"
                    :placeholder="field.placeholder || field.default"
                    show-password-on="click"
                  />
                  <n-input-number
                    v-else-if="field.type === 'integer'"
                    v-model:value="formData.config[field.name]"
                    :placeholder="field.default?.toString()"
                    style="width: 100%"
                  />
                </n-form-item>
              </n-collapse-item>

              <n-collapse-item :title="t('platformConfig.sectionTls')" name="tls">
                <n-form-item
                  v-for="field in tlsFields"
                  :key="field.name"
                  :label="field.label || field.name"
                  :path="'config.' + field.name"
                >
                  <n-input
                    v-if="field.type === 'string'"
                    v-model:value="formData.config[field.name]"
                    :type="(field.name === 'ca_cert' || field.name === 'client_cert' || field.name === 'client_key') ? 'textarea' : (field.secret ? 'password' : 'text')"
                    :placeholder="field.placeholder || field.default"
                    show-password-on="click"
                  />
                </n-form-item>
              </n-collapse-item>

              <n-collapse-item :title="t('platformConfig.sectionWill')" name="will">
                <n-form-item
                  v-for="field in willFields"
                  :key="field.name"
                  :label="field.label || field.name"
                  :path="'config.' + field.name"
                >
                  <n-input
                    v-if="field.type === 'string'"
                    v-model:value="formData.config[field.name]"
                    :placeholder="field.placeholder || field.default"
                  />
                  <n-input-number
                    v-else-if="field.type === 'integer'"
                    v-model:value="formData.config[field.name]"
                    :placeholder="field.default?.toString()"
                    style="width: 100%"
                  />
                </n-form-item>
              </n-collapse-item>

              <n-collapse-item
                v-if="formData.config.protocol_version === 5"
                :title="t('platformConfig.sectionMqtt5')"
                name="mqtt5"
              >
                <n-form-item
                  v-for="field in mqtt5Fields"
                  :key="field.name"
                  :label="field.label || field.name"
                  :path="'config.' + field.name"
                >
                  <n-input
                    v-if="field.type === 'string'"
                    v-model:value="formData.config[field.name]"
                    :placeholder="field.placeholder"
                  />
                  <n-input-number
                    v-else-if="field.type === 'integer'"
                    v-model:value="formData.config[field.name]"
                    :placeholder="field.placeholder"
                    style="width: 100%"
                  />
                </n-form-item>
              </n-collapse-item>

              <n-collapse-item :title="t('platformConfig.sectionTopic')" name="topic">
                <n-form-item
                  v-for="field in topicFields"
                  :key="field.name"
                  :label="field.label || field.name"
                  :path="'config.' + field.name"
                >
                  <div v-if="field.name === 'topic_template'" style="width: 100%">
                    <n-input
                      v-model:value="formData.config[field.name]"
                      :placeholder="field.placeholder || field.default"
                      @blur="validateTopicTemplate(formData.config[field.name])"
                    />
                    <div v-if="topicValidation" style="margin-top: 4px; font-size: 12px">
                      <span v-if="topicValidation.valid" style="color: #18a058">
                        {{ t('platformConfig.topicValid') }} — {{ t('platformConfig.topicVars') }}: {{ topicValidation.variables.join(', ') }}
                      </span>
                      <span v-else style="color: #d03050">
                        {{ topicValidation.errors.join('; ') }}
                      </span>
                    </div>
                  </div>
                  <n-input
                    v-else
                    v-model:value="formData.config[field.name]"
                    :placeholder="field.placeholder || field.default"
                  />
                </n-form-item>
              </n-collapse-item>

              <n-collapse-item :title="t('platformConfig.sectionPayload')" name="payload">
                <n-form-item
                  v-for="field in payloadFields"
                  :key="field.name"
                  :label="field.label || field.name"
                  :path="'config.' + field.name"
                >
                  <n-select
                    v-if="field.name === 'payload_format'"
                    v-model:value="formData.config[field.name]"
                    :options="payloadFormatOptions"
                    clearable
                  />
                  <n-input
                    v-else-if="field.type === 'string'"
                    v-model:value="formData.config[field.name]"
                    :type="field.name === 'custom_template' ? 'textarea' : 'text'"
                    :placeholder="field.placeholder || field.default"
                  />
                  <n-input-number
                    v-else-if="field.type === 'integer'"
                    v-model:value="formData.config[field.name]"
                    :placeholder="field.default?.toString()"
                    style="width: 100%"
                  />
                </n-form-item>
              </n-collapse-item>

              <n-collapse-item :title="t('platformConfig.sectionQos')" name="qos">
                <n-form-item
                  v-for="field in qosFields"
                  :key="field.name"
                  :label="field.label || field.name"
                  :path="'config.' + field.name"
                >
                  <n-select
                    v-if="field.name === 'default_qos' || field.name === 'alarm_qos'"
                    v-model:value="formData.config[field.name]"
                    :options="qosOptions"
                    clearable
                  />
                  <n-input-number
                    v-else-if="field.type === 'integer'"
                    v-model:value="formData.config[field.name]"
                    :placeholder="field.default?.toString()"
                    style="width: 100%"
                  />
                </n-form-item>
              </n-collapse-item>

              <n-collapse-item :title="t('platformConfig.sectionDedup')" name="dedup">
                <n-form-item
                  v-for="field in dedupFields"
                  :key="field.name"
                  :label="field.label || field.name"
                  :path="'config.' + field.name"
                >
                  <n-input-number
                    v-model:value="formData.config[field.name]"
                    :placeholder="field.default?.toString()"
                    style="width: 100%"
                  />
                </n-form-item>
              </n-collapse-item>
            </n-collapse>
          </n-form>
        </n-spin>

        <n-space justify="end" style="margin-top: 8px">
          <n-button @click="currentStep = 1">{{ t('platformConfig.prevStep') }}</n-button>
          <n-button @click="testConnection" :loading="testing">{{ t('platformConfig.testConnection') }}</n-button>
          <n-button type="primary" :loading="connecting" @click="connectPlatform">{{ t('platformConfig.connect') }}</n-button>
        </n-space>
      </div>
    </n-modal>

    <n-modal v-model:show="showEditModal" preset="card" :title="t('platformConfig.editConfig')" style="width: 780px; max-width: 95vw; max-height: 85vh; overflow-y: auto" :mask-closable="false" :close-on-esc="true" :auto-focus="true" :close-on-esc-aria-label="t('common.closeDialog')">
      <n-alert type="info" style="margin-bottom: 16px">
        {{ t('platformConfig.editConfigHint') }}
      </n-alert>
      <n-spin :show="schemaLoading">
        <n-form
          ref="editFormRef"
          :model="editFormData"
          :rules="editFormRules"
          label-placement="left"
          label-width="140"
        >
          <n-form-item
            v-for="field in editPlatformFields"
            :key="field.name"
            :label="field.label || field.name"
            :path="'config.' + field.name"
          >
            <n-input
              v-if="field.type === 'string'"
              v-model:value="editFormData.config[field.name]"
              :type="field.secret ? 'password' : 'text'"
              :placeholder="field.placeholder || (field.required ? t('platformConfig.required') : t('platformConfig.optional'))"
              show-password-on="click"
            />
            <n-input-number
              v-else-if="field.type === 'integer'"
              v-model:value="editFormData.config[field.name]"
              :placeholder="field.default?.toString()"
              style="width: 100%"
            />
          </n-form-item>
        </n-form>
      </n-spin>
      <n-space justify="end" style="margin-top: 8px">
        <n-button @click="testEditConnection" :loading="testing">{{ t('platformConfig.testConnection') }}</n-button>
        <n-button type="primary" :loading="reloading" @click="reloadConfig">{{ t('platformConfig.saveAndReload') }}</n-button>
      </n-space>
    </n-modal>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, h } from 'vue'
import { useRouter } from 'vue-router'
import {
  NCard, NButton, NDataTable, NTag, NSpace, NModal, NForm, NFormItem,
  NInput, NInputNumber, NGrid, NGi, NSteps, NStep, NAlert, NEmpty, NSpin,
  NCollapse, NCollapseItem, NSelect,
} from 'naive-ui'
import { platformApi } from '@/api'
import { t } from '@/i18n'
import { extractError } from '@/utils/errorCodes'
import { useAuthStore } from '@/stores/auth'
import { message, dialog } from '@/utils/discreteApi'

const $router = useRouter()
const auth = useAuthStore()
const platforms = ref<any[]>([])
const supportedPlatforms = ref<any[]>([])
const loading = ref(false)
const showAddModal = ref(false)
const showEditModal = ref(false)
const connecting = ref(false)
const reloading = ref(false)
const testing = ref(false)
const schemaLoading = ref(false)
const baseFields = ref<any[]>([])
const mqttFields = ref<any[]>([])
const tlsFields = ref<any[]>([])
const willFields = ref<any[]>([])
const mqtt5Fields = ref<any[]>([])
const topicFields = ref<any[]>([])
const payloadFields = ref<any[]>([])
const qosFields = ref<any[]>([])
const dedupFields = ref<any[]>([])
const editPlatformFields = ref<any[]>([])
const currentStep = ref(1)
const topicValidation = ref<{ valid: boolean; errors: string[]; variables: string[] } | null>(null)
const formData = ref<{ platform_name: string; config: Record<string, any> }>({
  platform_name: '',
  config: {},
})
const editFormData = ref<{ platform_name: string; config: Record<string, any> }>({
  platform_name: '',
  config: {},
})
const formRef = ref<any>(null)
const editFormRef = ref<any>(null)

const payloadFormatOptions = computed(() => [
  { label: 'JSON', value: 'json' },
  { label: t('platformConfig.cborBinary'), value: 'cbor' },
  { label: 'Protobuf', value: 'protobuf' },
  { label: t('platformConfig.customTemplate'), value: 'custom' },
])

const qosOptions = computed(() => [
  { label: t('platformConfig.qos0'), value: 0 },
  { label: t('platformConfig.qos1'), value: 1 },
  { label: t('platformConfig.qos2'), value: 2 },
])

const modalTitle = computed(() => {
  if (currentStep.value === 1) return t('platformConfig.addPlatformSelect')
  return t('platformConfig.addPlatformConfig')
})

const selectedPlatformInfo = computed(() => {
  return supportedPlatforms.value.find(p => p.name === formData.value.platform_name)
})

function buildFormRules(fields: any[]) {
  const rules: Record<string, any> = {}
  for (const field of fields) {
    if (field.required) {
      if (field.type === 'integer' || field.type === 'number') {
        rules['config.' + field.name] = {
          type: 'number',
          required: true,
          message: t('platformConfig.fieldRequired', { field: field.label || field.name }),
          trigger: ['blur', 'change'],
        }
      } else {
        rules['config.' + field.name] = {
          required: true,
          message: t('platformConfig.fieldRequired', { field: field.label || field.name }),
          trigger: ['blur', 'change'],
        }
      }
    }
    if (field.name === 'broker' || field.name === 'host' || field.name === 'url') {
      rules['config.' + field.name] = rules['config.' + field.name] || {}
      rules['config.' + field.name].validator = (_rule: any, value: string) => {
        if (!value) return true
        const cleanVal = value.replace(/^mqtts?:\/\//, '')
        if (/^[a-zA-Z0-9]/.test(cleanVal)) return true
        return new Error(t('platformConfig.invalidUrlFormat'))
      }
    }
    if (field.name === 'port') {
      rules['config.' + field.name] = rules['config.' + field.name] || {}
      rules['config.' + field.name].validator = (_rule: any, value: number) => {
        if (value == null) return true
        if (value >= 1 && value <= 65535) return true
        return new Error(t('platformConfig.portRangeError'))
      }
    }
  }
  return rules
}

const allFields = computed(() => [
  ...baseFields.value, ...mqttFields.value, ...tlsFields.value,
  ...willFields.value, ...mqtt5Fields.value, ...topicFields.value,
  ...payloadFields.value, ...qosFields.value, ...dedupFields.value,
])

const dynamicFormRules = computed(() => buildFormRules(allFields.value))
const editFormRules = computed(() => buildFormRules(editPlatformFields.value))

function isPlatformConnected(name: string) {
  return platforms.value.some(p => p.name === name && p.connected)
}

const columns = [
  { title: t('platformConfig.platform'), key: 'name', width: 150 },
  { title: t('platformConfig.version'), key: 'version', width: 80 },
  {
    title: t('platformConfig.status'),
    key: 'connected',
    width: 100,
    render: (row: any) =>
      h(NTag, { type: row.connected ? 'success' : 'error', size: 'small' }, () =>
        row.connected ? t('platformConfig.connected') : t('platformConfig.notConnected')
      ),
  },
  {
    title: t('platformConfig.state'),
    key: 'state',
    width: 120,
    render: (row: any) => {
      if (!row.state || row.state === 'unknown') return h('span', { style: 'color: var(--n-text-color-3)' }, '--')
      const typeMap: Record<string, string> = {
        connected: 'success', publishing: 'success',
        connecting: 'warning', reconnecting: 'warning',
        error: 'error', disconnected: 'default',
      }
      return h(NTag, { type: (typeMap[row.state] || 'default') as 'default' | 'info' | 'success' | 'warning' | 'error' | 'primary', size: 'small' }, () => row.state)
    },
  },
  {
    title: t('platformConfig.actions'),
    key: 'actions',
    width: 200,
    render: (row: any) =>
      h(NSpace, { size: 'small' }, () => [
        h(NButton, { size: 'small', onClick: () => openEditModal(row.name) }, () => t('platformConfig.edit')),
        row.name === 'custom' ? h(NButton, { size: 'small', type: 'info', onClick: () => $router.push(`/system/platforms/custom-mqtt/${row.name}`) }, () => t('platformConfig.advancedConfig') ) : null,
        h(NButton, { size: 'small', type: 'error', onClick: () => disconnectPlatform(row.name) }, () => t('platformConfig.disconnect')),
      ]),
  },
]

async function validateTopicTemplate(template: string) {
  if (!template) { topicValidation.value = null; return }
  try {
    const result = await platformApi.validateTopic(template)
    topicValidation.value = result
  } catch {
    topicValidation.value = null
  }
}

function applyDefaults(fields: any[], config: Record<string, any>) {
  for (const f of fields) {
    if (f.type === 'integer' || f.type === 'number') {
      config[f.name] = f.default !== undefined ? f.default : undefined
    } else {
      config[f.name] = f.default ?? null
    }
  }
}

function cleanConfig(config: Record<string, any>, fields: any[]): Record<string, any> {
  const cleaned = { ...config }
  for (const f of fields) {
    if ((f.type === 'integer' || f.type === 'number') && (cleaned[f.name] === null || cleaned[f.name] === undefined)) {
      cleaned[f.name] = f.default ?? 0
    }
  }
  return cleaned
}

async function loadPlatforms() {
  loading.value = true
  try {
    const data = await platformApi.list()
    platforms.value = data?.platforms || []
    supportedPlatforms.value = data?.supported || []
  } catch (e: any) {
    message.error(extractError(e, t('platformConfig.loadListFailed')))
  } finally {
    loading.value = false
  }
}

function openAddModal() {
  formData.value = { platform_name: '', config: {} }
  baseFields.value = []
  mqttFields.value = []
  tlsFields.value = []
  willFields.value = []
  mqtt5Fields.value = []
  topicFields.value = []
  payloadFields.value = []
  qosFields.value = []
  dedupFields.value = []
  topicValidation.value = null
  currentStep.value = 1
  showAddModal.value = true
}

async function openEditModal(platformName: string) {
  editFormData.value = { platform_name: platformName, config: {} }
  editPlatformFields.value = []
  schemaLoading.value = true
  showEditModal.value = true
  try {
    // FIXED: 原问题-编辑模态框只应用 schema 默认值，不加载平台当前实际配置，
    // 导致用户编辑时看到空表单/默认值，保存后会覆盖现有配置为默认值（数据丢失风险）。
    // 现先获取 schema 默认值，再通过 exportConfig 接口加载当前配置覆盖默认值。
    const [schemaData, exportData] = await Promise.all([
      platformApi.configSchema(platformName).catch(() => null),
      platformApi.exportConfig(platformName).catch(() => null),
    ])
    if (schemaData?.config_schema?.fields) {
      editPlatformFields.value = schemaData.config_schema.fields
      const newConfig: Record<string, any> = {}
      applyDefaults(editPlatformFields.value, newConfig)
      // 用当前已保存的配置覆盖默认值，确保编辑表单显示真实当前值
      const currentConfig = exportData?.config
      if (currentConfig && typeof currentConfig === 'object') {
        // exportConfig 返回的是 NorthConfig 的 model_dump，可能是嵌套结构；
        // 优先使用扁平字段覆盖，未覆盖的保留默认值
        for (const key of Object.keys(newConfig)) {
          if (key in currentConfig) {
            newConfig[key] = currentConfig[key]
          }
        }
        // 也合并当前配置中存在但 schema 默认值未覆盖的字段
        for (const key of Object.keys(currentConfig)) {
          if (!(key in newConfig)) {
            newConfig[key] = currentConfig[key]
          }
        }
      }
      editFormData.value.config = newConfig
    }
  } catch (e: any) {
    message.error(extractError(e, t('platformConfig.fetchSchemaFailed')))
  } finally {
    schemaLoading.value = false
  }
}

async function onSelectPlatform(p: any) {
  if (isPlatformConnected(p.name)) {
    message.warning(t('platformConfig.alreadyConnected', { label: p.label }))
    return
  }
  formData.value.platform_name = p.name
  formData.value.config = {}
  baseFields.value = []
  mqttFields.value = []
}

async function goToStep2() {
  if (!formData.value.platform_name) {
    message.warning(t('platformConfig.selectPlatformFirst'))
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
    if (data?.config_schema) {
      const schema = data.config_schema
      baseFields.value = schema.fields || []
      const sections = schema.sections || []
      for (const section of sections) {
        const fields = section.fields || []
        switch (section.title) {
          case 'MQTT Connection': mqttFields.value = fields; break
          case 'TLS/SSL': tlsFields.value = fields; break
          case 'Last Will': willFields.value = fields; break
          case 'MQTT 5.0 Properties': mqtt5Fields.value = fields; break
          case 'Topic Template': topicFields.value = fields; break
          case 'Payload Format': payloadFields.value = fields; break
          case 'QoS Policy': qosFields.value = fields; break
          case 'Deduplication': dedupFields.value = fields; break
        }
      }
      const newConfig: Record<string, any> = {}
      applyDefaults(baseFields.value, newConfig)
      applyDefaults(mqttFields.value, newConfig)
      applyDefaults(tlsFields.value, newConfig)
      applyDefaults(willFields.value, newConfig)
      applyDefaults(mqtt5Fields.value, newConfig)
      applyDefaults(topicFields.value, newConfig)
      applyDefaults(payloadFields.value, newConfig)
      applyDefaults(qosFields.value, newConfig)
      applyDefaults(dedupFields.value, newConfig)
      formData.value.config = newConfig
    }
  } catch (e: any) {
    message.error(extractError(e, t('platformConfig.fetchSchemaFailed')))
  } finally {
    schemaLoading.value = false
  }
}

async function connectPlatform() {
  if (!auth.isOperator) { message.warning(t('common.permissionDenied')); return }
  try {
    await formRef.value?.validate()
  } catch {
    message.warning(t('platformConfig.fillRequired'))
    return
  }
  connecting.value = true
  try {
    await platformApi.connect(formData.value.platform_name, cleanConfig(formData.value.config, allFields.value))
    showAddModal.value = false
    await loadPlatforms()
    message.success(t('platformConfig.connectSuccess'))
  } catch (e: any) {
    message.error(extractError(e, t('platformConfig.connectFailed')))
  } finally {
    connecting.value = false
  }
}

async function testConnection() {
  try {
    await formRef.value?.validate()
  } catch {
    message.warning(t('platformConfig.fillRequired'))
    return
  }
  testing.value = true
  try {
    const result = await platformApi.testConnection(formData.value.platform_name, cleanConfig(formData.value.config, allFields.value))
    if (result?.success) {
      message.success(result.message || t('platformConfig.testConnectionSuccess'))
    } else {
      message.error(result?.message || t('platformConfig.testConnectionFailed'))
    }
  } catch (e: any) {
    message.error(extractError(e, t('platformConfig.testConnectionFailed')))
  } finally {
    testing.value = false
  }
}

async function testEditConnection() {
  try {
    await editFormRef.value?.validate()
  } catch {
    message.warning(t('platformConfig.fillRequired'))
    return
  }
  testing.value = true
  try {
    const result = await platformApi.testConnection(editFormData.value.platform_name, cleanConfig(editFormData.value.config, editPlatformFields.value))
    if (result?.success) {
      message.success(result.message || t('platformConfig.testConnectionSuccess'))
    } else {
      message.error(result?.message || t('platformConfig.testConnectionFailed'))
    }
  } catch (e: any) {
    message.error(extractError(e, t('platformConfig.testConnectionFailed')))
  } finally {
    testing.value = false
  }
}

async function reloadConfig() {
  if (!auth.isOperator) { message.warning(t('common.permissionDenied')); return }
  try {
    await editFormRef.value?.validate()
  } catch {
    message.warning(t('platformConfig.fillRequired'))
    return
  }
  reloading.value = true
  try {
    await platformApi.reload(editFormData.value.platform_name, cleanConfig(editFormData.value.config, editPlatformFields.value))
    showEditModal.value = false
    await loadPlatforms()
    message.success(t('platformConfig.reloadSuccess'))
  } catch (e: any) {
    message.error(extractError(e, t('platformConfig.reloadFailed')))
  } finally {
    reloading.value = false
  }
}

async function disconnectPlatform(name: string) {
  if (!auth.isOperator) { message.warning(t('common.permissionDenied')); return }
  dialog.warning({
    title: t('platformConfig.confirmDisconnect'),
    content: t('platformConfig.confirmDisconnectContent', { name }),
    positiveText: t('platformConfig.disconnect'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      try {
        await platformApi.disconnect(name)
        await loadPlatforms()
        message.success(t('platformConfig.disconnected'))
      } catch (e: any) {
        message.error(extractError(e, t('platformConfig.disconnectFailed')))
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
