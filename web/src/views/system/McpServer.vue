<template>
  <n-space vertical :size="16">
    <n-card :bordered="false">
      <template #header>
        <n-space align="center" :size="12">
          <span style="font-size: 16px; font-weight: 600">MCP Server</span>
          <n-tag :type="stateTagType" size="small">{{ stateLabel }}</n-tag>
        </n-space>
      </template>
      <template #header-extra>
        <n-space :size="8">
          <n-switch
            :value="enabled"
            :loading="toggleLoading"
            @update:value="handleToggle"
          >
            <template #checked>{{ t('mcpServer.enabled') }}</template>
            <template #unchecked>{{ t('mcpServer.disabled') }}</template>
          </n-switch>
          <n-button size="small" @click="fetchStatus" :loading="loading">{{ t('mcpServer.refresh') }}</n-button>
        </n-space>
      </template>

      <n-alert
        v-if="missingDeps.length > 0"
        type="warning"
        :bordered="false"
        style="margin-bottom: 12px"
      >
        <template #header>{{ t('mcpServer.missingDepsTitle') }}</template>
        {{ t('mcpServer.missingDepsDesc') }}{{ missingDeps.map(d => d.package).join(', ') }}
        <n-button
          type="primary"
          size="small"
          style="margin-left: 12px"
          @click="handleInstallDeps"
          :loading="installing"
        >{{ t('mcpServer.oneClickInstall') }}</n-button>
      </n-alert>

      <template v-if="enabled">
        <n-descriptions label-placement="left" :column="2" bordered>
          <n-descriptions-item :label="t('mcpServer.version')">EdgeLite MCP v1.0</n-descriptions-item>
          <n-descriptions-item :label="t('mcpServer.sseEndpoint')">/api/v1/mcp/sse（{{ t('mcpServer.sseHint') }}）</n-descriptions-item>
          <n-descriptions-item :label="t('mcpServer.apiKeyAuth')">
            <n-space align="center" :size="8">
              <n-tag :type="authEnabled ? 'success' : 'default'" size="small">
                {{ authEnabled ? t('mcpServer.authEnabled') : t('mcpServer.authDisabled') }}
              </n-tag>
              <n-text depth="3" style="font-size: 12px">
                {{ authEnabled ? t('mcpServer.authEnabledDesc') : t('mcpServer.authDisabledDesc') }}
              </n-text>
            </n-space>
          </n-descriptions-item>
          <n-descriptions-item :label="t('mcpServer.apiKeyCount')">{{ apiKeys.length }}</n-descriptions-item>
        </n-descriptions>
      </template>

      <n-empty v-else :description="t('mcpServer.notEnabledDesc')" />
    </n-card>

    <template v-if="enabled">
      <n-card :title="t('mcpServer.toolsTitle')" :bordered="false">
        <n-data-table
          :columns="toolColumns"
          :data="tools"
          :loading="loadingTools"
          :bordered="false"
          size="small"
        />
      </n-card>

      <n-card :title="t('mcpServer.resourcesTitle')" :bordered="false">
        <n-data-table
          :columns="resourceColumns"
          :data="resources"
          :loading="loadingResources"
          :bordered="false"
          size="small"
        />
      </n-card>

      <n-card :title="t('mcpServer.promptsTitle')" :bordered="false">
        <n-data-table
          :columns="promptColumns"
          :data="prompts"
          :loading="loadingPrompts"
          :bordered="false"
          size="small"
        />
      </n-card>

      <n-card :title="t('mcpServer.apiKeyTitle')" :bordered="false">
        <template #header-extra>
          <n-button type="primary" size="small" @click="showCreateKeyModal = true">{{ t('mcpServer.createKey') }}</n-button>
        </template>
        <n-data-table
          :columns="keyColumns"
          :data="apiKeys"
          :loading="loadingKeys"
          :bordered="false"
          size="small"
        />

        <n-modal v-model:show="showCreateKeyModal" :title="t('mcpServer.createKeyTitle')" preset="card" style="width: 480px">
          <n-form :model="keyForm" :rules="keyFormRules" ref="keyFormRef" label-placement="left" label-width="100">
            <n-form-item :label="t('mcpServer.keyName')" path="name">
              <n-input v-model:value="keyForm.name" :placeholder="t('mcpServer.keyNamePlaceholder')" />
            </n-form-item>
            <n-form-item :label="t('mcpServer.permission')" path="scopes">
              <n-checkbox-group v-model:value="keyForm.scopes">
                <n-space>
                  <n-checkbox value="read">{{ t('mcpServer.read') }}</n-checkbox>
                  <n-checkbox value="write">{{ t('mcpServer.write') }}</n-checkbox>
                </n-space>
              </n-checkbox-group>
            </n-form-item>
          </n-form>
          <template #action>
            <n-button @click="showCreateKeyModal = false">{{ t('common.cancel') }}</n-button>
            <n-button type="primary" @click="handleCreateKey">{{ t('deviceList.create') }}</n-button>
          </template>
        </n-modal>
      </n-card>
    </template>

    <n-modal v-model:show="showInstallProgress" :title="t('mcpServer.installTitle')" preset="card" style="width: 480px" :closable="false">
      <n-spin :description="installProgress">
        <n-space vertical>
          <p>{{ t('mcpServer.installDesc') }}</p>
          <p v-if="installResult">{{ installResult }}</p>
        </n-space>
      </n-spin>
      <template #action>
        <n-button @click="showInstallProgress = false" :disabled="installing">{{ t('deviceList.close') }}</n-button>
      </template>
    </n-modal>

    <n-modal v-model:show="showToolCallModal" :title="t('mcpServer.callTool') + ' ' + toolCallName" preset="card" style="width: 600px">
      <n-space vertical :size="12">
        <n-form-item :label="t('mcpServer.paramJson')">
          <n-input v-model:value="toolCallArgs" type="textarea" :rows="6" placeholder='{"key": "value"}' />
        </n-form-item>
        <n-form-item v-if="toolCallResult" :label="t('mcpServer.result')">
          <n-input :value="toolCallResult" type="textarea" :rows="8" readonly />
        </n-form-item>
      </n-space>
      <template #action>
        <n-button @click="showToolCallModal = false">{{ t('deviceList.close') }}</n-button>
        <n-button type="primary" :loading="callingTool" @click="handleToolCall">{{ t('common.confirm') }}</n-button>
      </template>
    </n-modal>
  </n-space>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, h } from 'vue'
import { NTag, NButton, NPopconfirm, useMessage, useDialog } from 'naive-ui'
import { serviceApi, mcpApi } from '@/api'
import type { ServiceDependency } from '@/api'
// FIXED: 原问题-添加i18n支持
import { t } from '@/i18n'

const message = useMessage()
const dialog = useDialog()
const loading = ref(false)
const toggleLoading = ref(false)
const installing = ref(false)
const showInstallProgress = ref(false)
const installProgress = ref('')
const installResult = ref('')

const enabled = ref(false)
const state = ref<string>('disabled')
const dependencies = ref<ServiceDependency[]>([])
const authEnabled = ref(false)

const tools = ref<any[]>([])
const resources = ref<any[]>([])
const prompts = ref<any[]>([])
const apiKeys = ref<any[]>([])

const loadingTools = ref(false)
const loadingResources = ref(false)
const loadingPrompts = ref(false)
const loadingKeys = ref(false)
const showCreateKeyModal = ref(false)
const showToolCallModal = ref(false)
const callingTool = ref(false)
const toolCallName = ref('')
const toolCallArgs = ref('{}')
const toolCallResult = ref('')
const keyFormRef = ref<any>(null)

const keyForm = reactive({
  name: '',
  scopes: ['read'] as string[],
})

const keyFormRules = {
  name: { required: true, message: t('mcpServer.keyNamePlaceholder'), trigger: 'blur' },
  scopes: { type: 'array' as const, required: true, message: t('common.required'), trigger: 'change' },
}

const missingDeps = computed(() => dependencies.value.filter(d => !d.installed))

const stateTagType = computed(() => {
  switch (state.value) {
    case 'running': return 'success'
    case 'enabled': return 'info'
    case 'error': return 'error'
    default: return 'default'
  }
})

// FIXED: 原问题-stateLabel中文硬编码，改为i18n
const stateLabel = computed(() => {
  switch (state.value) {
    case 'running': return t('serviceState.running')
    case 'enabled': return t('serviceState.enabled')
    case 'error': return t('serviceState.error')
    case 'disabled': return t('serviceState.disabled')
    default: return state.value
  }
})

// FIXED: 原问题-表格列标题中文硬编码，改为i18n
const toolColumns = [
  { title: t('ruleList.name'), key: 'name', width: 200 },
  { title: t('auditLog.detail'), key: 'description', ellipsis: { tooltip: true } },
  {
    title: t('alarmList.actions'), key: 'action', width: 100,
    render: (row: any) => h(NButton, { text: true, type: 'primary', size: 'small', onClick: () => openToolCall(row) }, { default: () => t('common.confirm') }),
  },
]

const resourceColumns = [
  { title: 'URI', key: 'uri', width: 250 },
  { title: t('ruleList.name'), key: 'name', width: 200 },
  { title: t('auditLog.detail'), key: 'description', ellipsis: { tooltip: true } },
]

const promptColumns = [
  { title: t('ruleList.name'), key: 'name', width: 200 },
  { title: t('auditLog.detail'), key: 'description', ellipsis: { tooltip: true } },
]

const keyColumns = [
  { title: t('mcpServer.keyName'), key: 'name', width: 150 },
  { title: 'Key', key: 'key', width: 200, ellipsis: { tooltip: true } },
  {
    title: t('mcpServer.permission'), key: 'scopes', width: 150,
    render: (row: any) => h(NTag, { size: 'small', type: 'info' }, { default: () => (row.scopes || []).join(', ') }),
  },
  { title: t('auditLog.time'), key: 'created_at', width: 180 },
  {
    title: t('alarmList.actions'), key: 'action', width: 80,
    render: (row: any) => h(NButton, { text: true, type: 'error', size: 'small', onClick: () => handleDeleteKey(row) }, { default: () => t('common.delete') }),
  },
]

async function fetchStatus() {
  loading.value = true
  try {
    const data = await serviceApi.status('mcp_server')
    enabled.value = data?.state === 'running'
    state.value = data.state
    dependencies.value = data.dependencies || []

    if (enabled.value) {
      await Promise.all([fetchTools(), fetchResources(), fetchPrompts(), fetchApiKeys()])
    }
  } catch (e: any) {
    if (e?.response?.status !== 404) message.error(e?.message || t('http.requestFailed'))
  } finally {
    loading.value = false
  }
}

async function fetchTools() {
  loadingTools.value = true
  try {
    const data = await mcpApi.tools()
    tools.value = data?.tools || []
  } catch (e: any) {
    message.error(t('http.requestFailed'))
  } finally {
    loadingTools.value = false
  }
}

async function fetchResources() {
  loadingResources.value = true
  try {
    const data = await mcpApi.resources()
    resources.value = data?.resources || []
  } catch (e: any) {
    message.error(t('http.requestFailed'))
  } finally {
    loadingResources.value = false
  }
}

async function fetchPrompts() {
  loadingPrompts.value = true
  try {
    const data = await mcpApi.prompts()
    prompts.value = data?.prompts || []
  } catch (e: any) {
    message.error(t('http.requestFailed'))
  } finally {
    loadingPrompts.value = false
  }
}

async function fetchApiKeys() {
  loadingKeys.value = true
  try {
    const data = await mcpApi.authKeys()
    apiKeys.value = data?.keys || []
    authEnabled.value = data?.enabled ?? false
  } catch (e: any) {
    message.error(t('http.requestFailed'))
  } finally {
    loadingKeys.value = false
  }
}

async function handleToggle(val: boolean) {
  if (!val) {
    dialog.warning({
      title: t('serviceOverview.disableTitle'),
      content: t('serviceOverview.disableContent', { name: 'MCP Server' }),
      positiveText: t('serviceOverview.confirmDisable'),
      negativeText: t('common.cancel'),
      onPositiveClick: () => doToggleMcp(false),
    })
    return
  }
  await doToggleMcp(true)
}

async function doToggleMcp(val: boolean) {
  toggleLoading.value = true
  try {
    if (val) {
      await serviceApi.enable('mcp_server')
      message.success(t('serviceOverview.enableSuccess'))
    } else {
      await serviceApi.disable('mcp_server')
      message.success(t('serviceOverview.disableSuccess'))
    }
    await fetchStatus()
  } catch (e: any) {
    const detail = e?.response?.data?.detail
    if (typeof detail === 'object' && detail?.missing_dependencies) {
      message.warning(detail.message || t('serviceManager.depMissing'))
    } else {
      message.error(typeof detail === 'string' ? detail : (e?.message || t('serviceOverview.operationFailed')))
    }
  } finally {
    toggleLoading.value = false
  }
}

async function handleInstallDeps() {
  installing.value = true
  showInstallProgress.value = true
  installProgress.value = t('mcpServer.installDesc')
  installResult.value = ''
  try {
    await serviceApi.installDeps('mcp_server')
    installResult.value = t('serviceOverview.installSuccess')
    message.success(t('serviceOverview.installSuccess'))
    await fetchStatus()
  } catch (e: any) {
    installResult.value = `${t('serviceOverview.installFailed')}: ${e?.response?.data?.detail || e?.message}`
    message.error(t('serviceOverview.installFailed'))
  } finally {
    installing.value = false
    installProgress.value = ''
  }
}

async function handleCreateKey() {
  try {
    await keyFormRef.value?.validate()
  } catch { return }
  try {
    await mcpApi.createKey(keyForm)
    message.success(t('common.success'))
    showCreateKeyModal.value = false
    keyForm.name = ''
    keyForm.scopes = ['read']
    fetchApiKeys()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || t('common.failed'))
  }
}

function openToolCall(tool: any) {
  toolCallName.value = tool.name
  toolCallArgs.value = '{}'
  toolCallResult.value = ''
  showToolCallModal.value = true
}

async function handleToolCall() {
  callingTool.value = true
  toolCallResult.value = ''
  try {
    const args = JSON.parse(toolCallArgs.value)
    const result = await mcpApi.callTool(toolCallName.value, args)
    toolCallResult.value = JSON.stringify(result, null, 2)
    message.success(t('common.success'))
  } catch (e: any) {
    toolCallResult.value = e?.response?.data?.detail || e?.message || t('common.failed')
    message.error(t('common.failed'))
  } finally {
    callingTool.value = false
  }
}

function handleDeleteKey(key: any) {
  dialog.warning({
    title: t('common.confirm'),
    content: t('deviceList.deleteConfirm', { name: key.name }),
    positiveText: t('common.delete'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      try {
        await mcpApi.deleteKey(key.key_id || key.id)
        message.success(t('common.success'))
        fetchApiKeys()
      } catch (e: any) {
        message.error(e?.response?.data?.detail || t('common.failed'))
      }
    },
  })
}

onMounted(fetchStatus)
</script>
