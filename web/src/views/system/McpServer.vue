<template>
  <n-space vertical :size="16">
    <n-card title="MCP Server 状态" :bordered="false">
      <template #header-extra>
        <n-tag :type="connected ? 'success' : 'default'" size="small">
          {{ connected ? '已连接' : '未连接' }}
        </n-tag>
      </template>
      <n-descriptions label-placement="left" :column="2" bordered>
        <n-descriptions-item label="服务版本">EdgeLite MCP v1.0</n-descriptions-item>
        <n-descriptions-item label="SSE端点">/api/v1/mcp/sse</n-descriptions-item>
        <n-descriptions-item label="认证状态">
          <n-tag :type="authEnabled ? 'success' : 'warning'" size="small">
            {{ authEnabled ? '已启用' : '未启用' }}
          </n-tag>
        </n-descriptions-item>
        <n-descriptions-item label="API Key数量">{{ apiKeys.length }}</n-descriptions-item>
      </n-descriptions>
    </n-card>

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
  </n-space>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, h } from 'vue'
import { NTag, NButton, useMessage } from 'naive-ui'
import { mcpApi } from '@/api'

const message = useMessage()
const connected = ref(false)
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

const keyForm = reactive({
  name: '',
  scopes: ['read'] as string[],
})

const toolColumns = [
  { title: '名称', key: 'name', width: 200 },
  { title: '描述', key: 'description', ellipsis: { tooltip: true } },
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
]

async function fetchTools() {
  loadingTools.value = true
  try {
    const data = await mcpApi.tools()
    tools.value = data?.tools || []
    connected.value = true
  } catch {
    connected.value = false
  } finally {
    loadingTools.value = false
  }
}

async function fetchResources() {
  loadingResources.value = true
  try {
    const data = await mcpApi.resources()
    resources.value = data?.resources || []
  } catch {} finally {
    loadingResources.value = false
  }
}

async function fetchPrompts() {
  loadingPrompts.value = true
  try {
    const data = await mcpApi.prompts()
    prompts.value = data?.prompts || []
  } catch {} finally {
    loadingPrompts.value = false
  }
}

async function fetchApiKeys() {
  loadingKeys.value = true
  try {
    const data = await mcpApi.authKeys()
    apiKeys.value = data?.keys || []
    authEnabled.value = data?.enabled ?? false
  } catch {} finally {
    loadingKeys.value = false
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

onMounted(() => {
  fetchTools()
  fetchResources()
  fetchPrompts()
  fetchApiKeys()
})
</script>
