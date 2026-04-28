<template>
  <n-space vertical :size="16">
    <n-space justify="space-between">
      <n-space>
        <n-input v-model:value="searchText" placeholder="搜索规则名称/设备ID" clearable style="width: 200px" @update:value="fetchRules" />
        <n-select v-model:value="filterSeverity" :options="severityOptions" placeholder="级别筛选" clearable style="width: 120px" @update:value="fetchRules" />
      </n-space>
      <n-button type="primary" @click="showCreateModal = true">创建规则</n-button>
    </n-space>

    <n-data-table :columns="columns" :data="rules" :loading="loading" :pagination="pagination" :row-key="(r: Rule) => r.rule_id" />

    <n-modal v-model:show="showCreateModal" title="创建告警规则" preset="card" style="width: 640px">
      <n-form :model="createForm" label-placement="left" label-width="90" :rules="createRules" ref="createFormRef">
        <n-form-item label="规则名称" path="name"><n-input v-model:value="createForm.name" placeholder="如：温度超限告警" /></n-form-item>
        <n-form-item label="关联设备" path="device_id">
          <n-select v-model:value="createForm.device_id" :options="deviceOptions" placeholder="选择关联设备" filterable />
        </n-form-item>
        <n-form-item label="逻辑组合">
          <n-radio-group v-model:value="createForm.logic">
            <n-radio value="AND">AND（全部满足）</n-radio>
            <n-radio value="OR">OR（任一满足）</n-radio>
          </n-radio-group>
        </n-form-item>
        <n-form-item label="持续时间"><n-input-number v-model:value="createForm.duration" :min="0" :max="3600" placeholder="0" style="width: 120px" /> 秒（0=立即触发）</n-form-item>
        <n-form-item label="严重级别" path="severity">
          <n-select v-model:value="createForm.severity" :options="severityOptions" placeholder="选择级别" />
        </n-form-item>
        <n-form-item label="通知渠道">
          <n-select v-model:value="createForm.notify_channels" :options="channelOptions" multiple placeholder="选择通知渠道" />
        </n-form-item>
        <n-form-item label="触发条件">
          <n-space vertical style="width: 100%">
            <n-space v-for="(cond, i) in createForm.conditions" :key="i" align="center">
              <n-input v-model:value="cond.point" placeholder="测点名" style="width: 120px" />
              <n-select v-model:value="cond.operator" :options="operatorOptions" style="width: 80px" />
              <n-input-number v-model:value="cond.threshold" placeholder="阈值" style="width: 120px" />
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

    <n-modal v-model:show="showEditModal" title="编辑告警规则" preset="card" style="width: 640px">
      <n-form :model="editForm" label-placement="left" label-width="90" :rules="createRules" ref="editFormRef">
        <n-form-item label="规则名称" path="name"><n-input v-model:value="editForm.name" /></n-form-item>
        <n-form-item label="关联设备" path="device_id">
          <n-select v-model:value="editForm.device_id" :options="deviceOptions" filterable />
        </n-form-item>
        <n-form-item label="逻辑组合">
          <n-radio-group v-model:value="editForm.logic">
            <n-radio value="AND">AND（全部满足）</n-radio>
            <n-radio value="OR">OR（任一满足）</n-radio>
          </n-radio-group>
        </n-form-item>
        <n-form-item label="持续时间"><n-input-number v-model:value="editForm.duration" :min="0" :max="3600" style="width: 120px" /> 秒</n-form-item>
        <n-form-item label="严重级别" path="severity">
          <n-select v-model:value="editForm.severity" :options="severityOptions" />
        </n-form-item>
        <n-form-item label="通知渠道">
          <n-select v-model:value="editForm.notify_channels" :options="channelOptions" multiple />
        </n-form-item>
        <n-form-item label="触发条件">
          <n-space vertical style="width: 100%">
            <n-space v-for="(cond, i) in editForm.conditions" :key="i" align="center">
              <n-input v-model:value="cond.point" placeholder="测点名" style="width: 120px" />
              <n-select v-model:value="cond.operator" :options="operatorOptions" style="width: 80px" />
              <n-input-number v-model:value="cond.threshold" placeholder="阈值" style="width: 120px" />
              <n-button text type="error" @click="editForm.conditions.splice(i, 1)">删除</n-button>
            </n-space>
            <n-button dashed @click="editForm.conditions.push({ point: '', operator: '>', threshold: 0 })">添加条件</n-button>
          </n-space>
        </n-form-item>
      </n-form>
      <template #action>
        <n-button @click="showEditModal = false">取消</n-button>
        <n-button type="primary" :loading="saving" @click="handleEdit">保存</n-button>
      </template>
    </n-modal>
  </n-space>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, h } from 'vue'
import { NButton, NTag, NSpace, useMessage } from 'naive-ui'
import { ruleApi, deviceApi, type Rule, type Device } from '@/api'

const message = useMessage()
const rules = ref<Rule[]>([])
const devices = ref<Device[]>([])
const loading = ref(false)
const showCreateModal = ref(false)
const showEditModal = ref(false)
const creating = ref(false)
const saving = ref(false)
const searchText = ref('')
const filterSeverity = ref<string | null>(null)
const createFormRef = ref<any>(null)
const editFormRef = ref<any>(null)
const editingRuleId = ref('')

const pagination = reactive({ page: 1, pageSize: 20, itemCount: 0, onChange: (p: number) => { pagination.page = p; fetchRules() } })

const deviceOptions = ref<{ label: string; value: string }[]>([])

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

const createRules = {
  name: { required: true, message: '请输入规则名称', trigger: 'blur' },
  device_id: { required: true, message: '请选择关联设备', trigger: 'change' },
  severity: { required: true, message: '请选择严重级别', trigger: 'change' },
}

const columns = [
  { title: '规则ID', key: 'rule_id', width: 140 },
  { title: '名称', key: 'name', width: 150, sorter: true },
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
          h(NButton, { text: true, type: 'primary', onClick: () => openEdit(r) }, { default: () => '编辑' }),
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

const editForm = reactive({
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
    const data = await ruleApi.list({ page: pagination.page, size: pagination.pageSize, search: searchText.value || undefined })
    rules.value = data?.data ?? []
    pagination.itemCount = data.total
  } catch (e: any) {
    rules.value = []
    message.error(e?.message || '获取规则列表失败')
  } finally {
    loading.value = false
  }
}

async function fetchDevices() {
  try {
    const data = await deviceApi.list({ page: 1, size: 100 })
    const devs = data?.data ?? []
    devices.value = devs
    deviceOptions.value = devs.map(d => ({ label: `${d.name} (${d.device_id})`, value: d.device_id }))
  } catch {
    devices.value = []
    deviceOptions.value = []
  }
}

async function handleCreate() {
  try {
    await createFormRef.value?.validate()
  } catch { return }
  creating.value = true
  try {
    await ruleApi.create(createForm as any)
    message.success('规则创建成功')
    showCreateModal.value = false
    createForm.name = ''
    createForm.device_id = ''
    createForm.logic = 'AND'
    createForm.duration = 0
    createForm.severity = 'warning'
    createForm.notify_channels = ['dingtalk']
    createForm.conditions = [{ point: '', operator: '>', threshold: 0 }]
    fetchRules()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || '创建失败')
  } finally {
    creating.value = false
  }
}

function openEdit(r: Rule) {
  editingRuleId.value = r.rule_id
  editForm.name = r.name
  editForm.device_id = r.device_id
  editForm.logic = r.logic
  editForm.duration = r.duration
  editForm.severity = r.severity
  editForm.notify_channels = [...(r.notify_channels || ['dingtalk'])]
  editForm.conditions = r.conditions?.length ? r.conditions.map(c => ({ ...c })) : [{ point: '', operator: '>', threshold: 0 }]
  showEditModal.value = true
}

async function handleEdit() {
  if (!editForm.conditions || !editForm.conditions.length || editForm.conditions.some((c: any) => !c.point)) {
    message.error('请至少添加一个有效的条件')
    return
  }
  saving.value = true
  try {
    await ruleApi.update(editingRuleId.value, editForm as any)
    message.success('规则更新成功')
    showEditModal.value = false
    fetchRules()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || '更新失败')
  } finally {
    saving.value = false
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

function handleDelete(r: Rule) {
  if (!window.confirm('确认删除规则？')) return
  ruleApi.delete(r.rule_id).then(() => {
    message.success('规则已删除')
    fetchRules()
  }).catch((e: any) => {
    message.error(e?.message || '删除失败')
  })
}

onMounted(() => { fetchRules(); fetchDevices() })
</script>
