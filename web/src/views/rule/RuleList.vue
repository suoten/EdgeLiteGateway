<template>
  <n-space vertical :size="16">
    <n-space justify="space-between">
      <n-text>规则管理</n-text>
      <n-button type="primary" @click="showCreateModal = true">创建规则</n-button>
    </n-space>

    <n-data-table :columns="columns" :data="rules" :loading="loading" :pagination="pagination" :row-key="(r: Rule) => r.rule_id" />

    <n-modal v-model:show="showCreateModal" title="创建规则" preset="card" style="width: 600px">
      <n-form :model="createForm" label-placement="left" label-width="80">
        <n-form-item label="名称"><n-input v-model:value="createForm.name" /></n-form-item>
        <n-form-item label="设备ID"><n-input v-model:value="createForm.device_id" /></n-form-item>
        <n-form-item label="逻辑">
          <n-radio-group v-model:value="createForm.logic">
            <n-radio value="AND">AND</n-radio>
            <n-radio value="OR">OR</n-radio>
          </n-radio-group>
        </n-form-item>
        <n-form-item label="持续时间"><n-input-number v-model:value="createForm.duration" :min="0" :max="3600" /> 秒</n-form-item>
        <n-form-item label="级别">
          <n-select v-model:value="createForm.severity" :options="severityOptions" />
        </n-form-item>
        <n-form-item label="通知渠道">
          <n-select v-model:value="createForm.notify_channels" :options="channelOptions" multiple />
        </n-form-item>
        <n-form-item label="条件">
          <n-space vertical>
            <n-space v-for="(cond, i) in createForm.conditions" :key="i">
              <n-input v-model:value="cond.point" placeholder="测点" style="width: 100px" />
              <n-select v-model:value="cond.operator" :options="operatorOptions" style="width: 80px" />
              <n-input-number v-model:value="cond.threshold" style="width: 120px" />
              <n-button text type="error" @click="createForm.conditions.splice(i, 1)">删除</n-button>
            </n-space>
            <n-button dashed @click="createForm.conditions.push({ point: '', operator: '>', threshold: 0 })">添加条件</n-button>
          </n-space>
        </n-form-item>
      </n-form>
      <template #action>
        <n-button @click="showCreateModal = false">取消</n-button>
        <n-button type="primary" :loading="creating" @click="handleCreate">创建</n-button>
      </template>
    </n-modal>
  </n-space>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, h } from 'vue'
import { NButton, NTag, NSpace, useMessage } from 'naive-ui'
import { ruleApi, type Rule } from '@/api'

const message = useMessage()
const rules = ref<Rule[]>([])
const loading = ref(false)
const showCreateModal = ref(false)
const creating = ref(false)

const pagination = reactive({ page: 1, pageSize: 20, itemCount: 0, onChange: (p: number) => { pagination.page = p; fetchRules() } })

const severityOptions = [
  { label: '严重', value: 'critical' },
  { label: '警告', value: 'warning' },
  { label: '信息', value: 'info' },
]

const channelOptions = [
  { label: '钉钉', value: 'dingtalk' },
  { label: '邮件', value: 'email' },
  { label: '企业微信', value: 'wechat' },
  { label: 'Webhook', value: 'webhook' },
]

const operatorOptions = [
  { label: '>', value: '>' },
  { label: '>=', value: '>=' },
  { label: '<', value: '<' },
  { label: '<=', value: '<=' },
  { label: '==', value: '==' },
]

const severityColor: Record<string, any> = { critical: 'error', warning: 'warning', info: 'info' }

const columns = [
  { title: '规则ID', key: 'rule_id', width: 140 },
  { title: '名称', key: 'name', width: 150 },
  { title: '设备ID', key: 'device_id', width: 160 },
  { title: '逻辑', key: 'logic', width: 60 },
  { title: '持续时间', key: 'duration', width: 80, render: (r: Rule) => `${r.duration}s` },
  {
    title: '级别', key: 'severity', width: 80,
    render: (r: Rule) => h(NTag, { type: severityColor[r.severity] || 'default', size: 'small' }, { default: () => r.severity }),
  },
  {
    title: '状态', key: 'enabled', width: 80,
    render: (r: Rule) => h(NTag, { type: r.enabled ? 'success' : 'default', size: 'small' }, { default: () => r.enabled ? '启用' : '禁用' }),
  },
  { title: '通知', key: 'notify_channels', width: 150, render: (r: Rule) => r.notify_channels?.join(', ') },
  {
    title: '操作', key: 'actions', width: 200,
    render: (r: Rule) =>
      h(NSpace, null, {
        default: () => [
          h(NButton, { text: true, type: r.enabled ? 'warning' : 'success', onClick: () => handleToggle(r) }, { default: () => r.enabled ? '禁用' : '启用' }),
          h(NButton, { text: true, type: 'error', onClick: () => handleDelete(r) }, { default: () => '删除' }),
        ],
      }),
  },
]

const createForm = reactive({
  name: '',
  device_id: '',
  logic: 'AND' as string,
  duration: 0,
  severity: 'warning' as string,
  notify_channels: ['dingtalk'] as string[],
  conditions: [{ point: '', operator: '>', threshold: 0 }],
})

async function fetchRules() {
  loading.value = true
  try {
    const data = await ruleApi.list({ page: pagination.page, size: pagination.pageSize })
    rules.value = data.data
    pagination.itemCount = data.total
  } catch (e: any) {
    message.error(e?.message || '获取规则列表失败')
  } finally {
    loading.value = false
  }
}

async function handleCreate() {
  creating.value = true
  try {
    await ruleApi.create(createForm as any)
    message.success('规则创建成功')
    showCreateModal.value = false
    fetchRules()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || '创建失败')
  } finally {
    creating.value = false
  }
}

async function handleToggle(r: Rule) {
  try {
    if (r.enabled) {
      await ruleApi.disable(r.rule_id)
      message.success('规则已禁用')
    } else {
      await ruleApi.enable(r.rule_id)
      message.success('规则已启用')
    }
    fetchRules()
  } catch (e: any) {
    message.error(e?.message || '操作失败')
  }
}

async function handleDelete(r: Rule) {
  try {
    await ruleApi.delete(r.rule_id)
    message.success('规则已删除')
    fetchRules()
  } catch (e: any) {
    message.error(e?.message || '删除失败')
  }
}

onMounted(fetchRules)
</script>
