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
            <template #checked>已启用</template>
            <template #unchecked>已停用</template>
          </n-switch>
          <n-button size="small" @click="fetchStatus" :loading="loading">刷新</n-button>
        </n-space>
      </template>

      <n-alert
        v-if="missingDeps.length > 0"
        type="warning"
        :bordered="false"
        style="margin-bottom: 12px"
      >
        <template #header>缺少依赖组件</template>
        以下依赖未安装：{{ missingDeps.map(d => d.package).join(', ') }}
        <n-button
          type="primary"
          size="small"
          style="margin-left: 12px"
          @click="handleInstallDeps"
          :loading="installing"
        >一键安装</n-button>
      </n-alert>

      <template v-if="enabled">
        <n-descriptions label-placement="left" :column="2" bordered>
          <n-descriptions-item label="服务版本">EdgeLite MCP v1.0</n-descriptions-item>
          <n-descriptions-item label="SSE端点">/api/v1/mcp/sse</n-descriptions-item>
          <n-descriptions-item label="API Key认证">
            <n-space align="center" :size="8">
              <n-tag :type="authEnabled ? 'success' : 'default'" size="small">
                {{ authEnabled ? '已启用' : '未启用' }}
              </n-tag>
              <n-text depth="3" style="font-size: 12px">
                {{ authEnabled ? '访问MCP接口需携带API Key' : '当前无需认证即可访问，可创建API Key开启认证' }}
              </n-text>
            </n-space>
          </n-descriptions-item>
          <n-descriptions-item label="API Key数量">{{ apiKeys.length }}</n-descriptions-item>
        </n-descriptions>
      </template>

      <n-empty v-else description="MCP Server未启用，请开启开关启用服务" />
    </n-card>

    <template v-if="enabled">
      <n-card title="可用工具" :bordered="false">
        <n-data-table
          :columns="toolColumns"
          :data="tools"
          :loading="loadingTools"
          :bordered="false"
          size="small"
        />
      </n-card>

      <n-card title="可用资源" :bordered="false">
        <n-data-table
          :columns="resourceColumns"
          :data="resources"
          :loading="loadingResources"
          :bordered="false"
          size="small"
        />
      </n-card>

      <n-card title="提示模板" :bordered="false">
        <n-data-table
          :columns="promptColumns"
          :data="prompts"
          :loading="loadingPrompts"
          :bordered="false"
          size="small"
        />
      </n-card>

      <n-card title="API Key 管理" :bordered="false">
        <template #header-extra>
          <n-button type="primary" size="small" @click="showCreateKeyModal = true">创建Key</n-button>
        </template>
        <n-data-table
          :columns="keyColumns"
          :data="apiKeys"
          :loading="loadingKeys"
          :bordered="false"
          size="small"
        />

        <n-modal v-model:show="showCreateKeyModal" title="创建API Key" preset="card" style="width: 480px">
          <n-form :model="keyForm" label-placement="left" label-width="100">
            <n-form-item label="名称">
              <n-input v-model:value="keyForm.name" placeholder="Key名称" />
            </n-form-item>
            <n-form-item label="权限">
              <n-checkbox-group v-model:value="keyForm.scopes">
                <n-space>
                  <n-checkbox value="read">读取</n-checkbox>
                  <n-checkbox value="write">写入</n-checkbox>
                </n-space>
              </n-checkbox-group>
            </n-form-item>
          </n-form>
          <template #action>
            <n-button @click="showCreateKeyModal = false">取消</n-button>
            <n-button type="primary" @click="handleCreateKey">创建</n-button>
          </template>
        </n-modal>
      </n-card>
    </template>

    <n-modal v-model:show="showInstallProgress" title="安装依赖" preset="card" style="width: 480px" :closable="false">
      <n-spin :description="installProgress">
        <n-space vertical>
          <p>正在安装缺失的依赖组件，请稍候...</p>
          <p v-if="installResult">{{ installResult }}</p>
        </n-space>
      </n-spin>
      <template #action>
        <n-button @click="showInstallProgress = false" :disabled="installing">关闭</n-button>
      </template>
    </n-modal>

    <n-modal v-model:show="showToolCallModal" :title="`调用工具: ${toolCallName}`" preset="card" style="width: 600px">
      <n-space vertical :size="12">
        <n-form-item label="参数 (JSON)">
          <n-input v-model:value="toolCallArgs" type="textarea" :rows="6" placeholder='{"key": "value"}' />
        </n-form-item>
        <n-form-item v-if="toolCallResult" label="返回结果">
          <n-input :value="toolCallResult" type="textarea" :rows="8" readonly />
        </n-form-item>
      </n-space>
      <template #action>
        <n-button @click="showToolCallModal = false">关闭</n-button>
        <n-button type="primary" :loading="callingTool" @click="handleToolCall">执行调用</n-button>
      </template>
    </n-modal>
  </n-space>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, h } from 'vue'
import { NTag, NButton, NPopconfirm, useMessage, useDialog } from 'naive-ui'
import { serviceApi, mcpApi } from '@/api'
import type { ServiceDependency } from '@/api'

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

const keyForm = reactive({
  name: '',
  scopes: ['read'] as string[],
})

const missingDeps = computed(() => dependencies.value.filter(d => !d.installed))

const stateTagType = computed(() => {
  switch (state.value) {
    case 'running': return 'success'
    case 'enabled': return 'info'
    case 'error': return 'error'
    default: return 'default'
  }
})

const stateLabel = computed(() => {
  switch (state.value) {
    case 'running': return '运行中'
    case 'enabled': return '已启用'
    case 'error': return '异常'
    case 'disabled': return '未启用'
    default: return state.value
  }
})

const toolColumns = [
  { title: '名称', key: 'name', width: 200 },
  { title: '描述', key: 'description', ellipsis: { tooltip: true } },
  {
    title: '操作', key: 'action', width: 100,
    render: (row: any) => h(NButton, { text: true, type: 'primary', size: 'small', onClick: () => openToolCall(row) }, { default: () => '调用' }),
  },
]

const resourceColumns = [
  { title: 'URI', key: 'uri', width: 250 },
  { title: '名称', key: 'name', width: 200 },
  { title: '描述', key: 'description', ellipsis: { tooltip: true } },
]

const promptColumns = [
  { title: '名称', key: 'name', width: 200 },
  { title: '描述', key: 'description', ellipsis: { tooltip: true } },
]

const keyColumns = [
  { title: '名称', key: 'name', width: 150 },
  { title: 'Key', key: 'key', width: 200, ellipsis: { tooltip: true } },
  {
    title: '权限', key: 'scopes', width: 150,
    render: (row: any) => h(NTag, { size: 'small', type: 'info' }, { default: () => (row.scopes || []).join(', ') }),
  },
  { title: '创建时间', key: 'created_at', width: 180 },
  {
    title: '操作', key: 'action', width: 80,
    render: (row: any) => h(NButton, { text: true, type: 'error', size: 'small', onClick: () => handleDeleteKey(row) }, { default: () => '删除' }),
  },
]

async function fetchStatus() {
  loading.value = true
  try {
    const data = await serviceApi.status('mcp_server')
    enabled.value = data.state !== 'disabled'
    state.value = data.state
    dependencies.value = data.dependencies || []

    if (enabled.value) {
      await Promise.all([fetchTools(), fetchResources(), fetchPrompts(), fetchApiKeys()])
    }
  } catch (e: any) {
    if (e?.response?.status !== 404) message.error(e?.message || '获取状态失败')
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
    console.warn('获取MCP工具列表失败:', e?.message)
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
    console.warn('获取MCP资源列表失败:', e?.message)
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
    console.warn('获取MCP提示列表失败:', e?.message)
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
    console.warn('获取MCP密钥列表失败:', e?.message)
  } finally {
    loadingKeys.value = false
  }
}

async function handleToggle(val: boolean) {
  toggleLoading.value = true
  try {
    if (val) {
      await serviceApi.enable('mcp_server')
      message.success('MCP Server已启用')
    } else {
      await serviceApi.disable('mcp_server')
      message.success('MCP Server已停用')
    }
    await fetchStatus()
  } catch (e: any) {
    const detail = e?.response?.data?.detail
    if (typeof detail === 'object' && detail?.missing_dependencies) {
      message.warning(detail.message || '缺少依赖，请先安装')
    } else {
      message.error(typeof detail === 'string' ? detail : (e?.message || '操作失败'))
    }
  } finally {
    toggleLoading.value = false
  }
}

async function handleInstallDeps() {
  installing.value = true
  showInstallProgress.value = true
  installProgress.value = '正在安装依赖...'
  installResult.value = ''
  try {
    await serviceApi.installDeps('mcp_server')
    installResult.value = '依赖安装成功！'
    message.success('依赖安装成功')
    await fetchStatus()
  } catch (e: any) {
    installResult.value = `安装失败: ${e?.response?.data?.detail || e?.message}`
    message.error('依赖安装失败')
  } finally {
    installing.value = false
    installProgress.value = ''
  }
}

async function handleCreateKey() {
  if (!keyForm.name) {
    message.warning('请输入Key名称')
    return
  }
  try {
    await mcpApi.createKey(keyForm)
    message.success('API Key创建成功')
    showCreateKeyModal.value = false
    keyForm.name = ''
    keyForm.scopes = ['read']
    fetchApiKeys()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || '创建失败')
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
    message.success('工具调用成功')
  } catch (e: any) {
    toolCallResult.value = e?.response?.data?.detail || e?.message || '调用失败'
    message.error('工具调用失败')
  } finally {
    callingTool.value = false
  }
}

function handleDeleteKey(key: any) {
  dialog.warning({
    title: '确认删除',
    content: `确定删除API Key「${key.name}」？删除后使用该Key的客户端将无法访问。`,
    positiveText: '确认删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await mcpApi.deleteKey(key.key_id || key.id)
        message.success('API Key已删除')
        fetchApiKeys()
      } catch (e: any) {
        message.error(e?.response?.data?.detail || '删除失败')
      }
    },
  })
}

onMounted(fetchStatus)
</script>
