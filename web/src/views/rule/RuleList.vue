<template>
  <n-space vertical :size="16">
    <n-space justify="space-between">
      <n-space>
        <n-input v-model:value="searchText" placeholder="搜索规则名称/设备ID" clearable style="width: 200px" @update:value="() => { pagination.page = 1; fetchRules() }" />
        <n-select v-model:value="filterSeverity" :options="severityOptions" placeholder="级别筛选" clearable style="width: 120px" @update:value="() => { pagination.page = 1; fetchRules() }" />
      </n-space>
      <n-button type="primary" @click="showCreateModal = true">创建规则</n-button>
    </n-space>

    <n-data-table :columns="columns" :data="rules" :loading="loading" :pagination="pagination" :row-key="(r: Rule) => r.rule_id" />
    <n-empty v-if="!loading && rules.length === 0" description="暂无规则，点击「创建规则」添加告警规则" style="padding: 40px 0" />

    <n-modal v-model:show="showCreateModal" title="创建告警规则" preset="card" style="width: 640px">
      <n-form :model="createForm" label-placement="left" label-width="90" :rules="createRules" ref="createFormRef">
        <n-form-item label="告警模板">
          <n-select v-model:value="selectedTemplate" :options="templateOptions" placeholder="选择模板快速填充（可选）" clearable @update:value="onTemplateChange" />
        </n-form-item>
        <n-alert v-if="selectedTemplateDesc" type="info" :bordered="false" style="margin-bottom: 12px">{{ selectedTemplateDesc }}</n-alert>
        <n-form-item label="规则名称" path="name"><n-input v-model:value="createForm.name" placeholder="如：温度超限告警" /></n-form-item>
        <n-form-item label="关联设备" path="device_id">
          <n-select v-model:value="createForm.device_id" :options="deviceOptions" placeholder="选择关联设备" filterable @update:value="onDeviceChange" />
        </n-form-item>
        <n-form-item label="逻辑组合">
          <n-space align="center">
            <n-radio-group v-model:value="createForm.logic">
              <n-radio value="AND">AND（全部满足）</n-radio>
              <n-radio value="OR">OR（任一满足）</n-radio>
            </n-radio-group>
            <n-tooltip trigger="hover">
              <template #trigger><n-text depth="3" style="cursor: help">ⓘ</n-text></template>
              AND: 所有条件同时满足才触发; OR: 任一条件满足即触发
            </n-tooltip>
          </n-space>
        </n-form-item>
        <n-form-item label="持续时间">
          <n-space align="center">
            <n-input-number v-model:value="createForm.duration" :min="0" :max="3600" placeholder="0" style="width: 120px" />
            <n-text>秒</n-text>
            <n-tooltip trigger="hover">
              <template #trigger><n-text depth="3" style="cursor: help">ⓘ</n-text></template>
              条件持续满足该时长后才触发告警，0表示立即触发
            </n-tooltip>
          </n-space>
        </n-form-item>
        <n-form-item label="严重级别" path="severity">
          <n-select v-model:value="createForm.severity" :options="severityOptions" placeholder="选择级别" />
        </n-form-item>
        <n-form-item label="通知渠道">
          <n-select v-model:value="createForm.notify_channels" :options="channelOptions" multiple placeholder="选择通知渠道" />
        </n-form-item>
        <n-form-item label="触发条件">
          <n-space vertical style="width: 100%">
            <n-space v-for="(cond, i) in createForm.conditions" :key="i" align="center">
              <n-select v-model:value="cond.point" :options="pointOptions" filterable placeholder="测点" style="width: 120px" />
              <n-select v-model:value="cond.operator" :options="operatorOptions" style="width: 100px" />
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
          <n-select v-model:value="editForm.device_id" :options="deviceOptions" filterable @update:value="onDeviceChange" />
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
              <n-select v-model:value="cond.point" :options="pointOptions" filterable placeholder="测点" style="width: 120px" />
              <n-select v-model:value="cond.operator" :options="operatorOptions" style="width: 100px" />
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

    <n-modal v-model:show="showTestModal" :title="`测试规则: ${testingRuleName}`" preset="card" style="width: 560px">
      <n-alert type="info" :bordered="false" style="margin-bottom: 12px">
        输入模拟测点值，验证规则是否按预期触发。系统将根据当前规则条件判断是否触发告警。
      </n-alert>
      <n-form label-placement="left" label-width="90">
        <n-form-item v-for="pt in testPointFields" :key="pt" :label="pt">
          <n-input-number v-model:value="testPointValues[pt]" placeholder="输入模拟值" style="width: 200px" />
        </n-form-item>
      </n-form>
      <n-alert v-if="testResult !== null" :type="testResult ? 'warning' : 'success'" :bordered="false">
        {{ testResult ? '规则将触发告警！' : '规则不会触发告警' }}
      </n-alert>
      <template #action>
        <n-button @click="showTestModal = false">关闭</n-button>
        <n-button type="primary" :loading="testing" @click="handleTest">执行测试</n-button>
      </template>
    </n-modal>
  </n-space>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, h } from 'vue'
import { NButton, NTag, NSpace, NPopconfirm, useMessage, useDialog } from 'naive-ui'
import { ruleApi, deviceApi, type Rule, type Device } from '@/api'
import { severityLabel, channelLabel } from '@/utils/enumLabels'
import { RULE_TEMPLATES, OPERATOR_OPTIONS, getTemplateCategories } from '@/constants/ruleTemplates'

const message = useMessage()
const dialog = useDialog()
const rules = ref<Rule[]>([])
const devices = ref<Device[]>([])
const loading = ref(false)
const showCreateModal = ref(false)
const showEditModal = ref(false)
const creating = ref(false)
const saving = ref(false)
const testing = ref(false)
const showTestModal = ref(false)
const testingRuleId = ref('')
const testingRuleName = ref('')
const testPointFields = ref<string[]>([])
const testPointValues = reactive<Record<string, number | null>>({})
const testResult = ref<boolean | null>(null)
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

const operatorOptions = OPERATOR_OPTIONS

const selectedTemplate = ref<string | null>(null)
const selectedTemplateDesc = computed(() => {
  if (!selectedTemplate.value) return ''
  return RULE_TEMPLATES.find(t => t.id === selectedTemplate.value)?.description || ''
})
const templateOptions = computed(() => {
  const categories = getTemplateCategories()
  return categories.map(cat => ({
    type: 'group' as const,
    label: cat,
    key: cat,
    children: RULE_TEMPLATES.filter(t => t.category === cat).map(t => ({
      label: t.name,
      value: t.id,
    })),
  }))
})
const pointOptions = computed(() => {
  if (!createForm.device_id) return []
  const dev = devices.value.find(d => d.device_id === createForm.device_id)
  return dev?.points?.map((p: any) => ({ label: `${p.name}${p.unit ? ' (' + p.unit + ')' : ''}`, value: p.name })) || []
})

function onTemplateChange(val: string | null) {
  if (!val) return
  const tmpl = RULE_TEMPLATES.find(t => t.id === val)
  if (!tmpl) return
  createForm.name = tmpl.name
  createForm.severity = tmpl.severity
  createForm.logic = tmpl.logic
  createForm.duration = tmpl.duration
  createForm.conditions = tmpl.conditions.map(c => ({ point: c.point, operator: c.operator, threshold: c.threshold }))
}

function onDeviceChange() {
  // points will be recomputed via computed
}

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
    render: (r: Rule) => h(NTag, { type: severityColor[r.severity] || 'default', size: 'small' }, { default: () => severityLabel[r.severity] || r.severity }),
  },
  {
    title: '状态', key: 'enabled', width: 80,
    render: (r: Rule) => h(NTag, { type: r.enabled ? 'success' : 'default', size: 'small' }, { default: () => r.enabled ? '启用' : '禁用' }),
  },
  { title: '通知', key: 'notify_channels', width: 150, render: (r: Rule) => r.notify_channels?.map((c: string) => channelLabel[c] || c).join(', ') },
  {
    title: '操作', key: 'actions', width: 240,
    render: (r: Rule) =>
      h(NSpace, null, {
        default: () => [
          h(NButton, { text: true, type: 'primary', onClick: () => openEdit(r) }, { default: () => '编辑' }),
          h(NButton, { text: true, type: 'info', onClick: () => openTest(r) }, { default: () => '测试' }),
          h(NButton, { text: true, type: r.enabled ? 'warning' : 'success', onClick: () => handleToggle(r) }, { default: () => r.enabled ? '禁用' : '启用' }),
          h(NPopconfirm as any, { onPositiveClick: () => doDelete(r) }, {
            trigger: () => h(NButton, { text: true, type: 'error' }, { default: () => '删除' }),
            default: () => `确定删除规则"${r.name}"？`,
          }),
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
    const data = await ruleApi.list({ page: pagination.page, size: pagination.pageSize, search: searchText.value || undefined, severity: filterSeverity.value ?? undefined })
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
  if (!createForm.conditions.length || createForm.conditions.some((c: any) => !c.point || c.threshold === undefined || c.threshold === null)) {
    message.error('请至少添加一个有效的条件（测点和阈值不能为空）')
    return
  }
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
  if (r.enabled) {
    dialog.warning({
      title: '确认禁用规则',
      content: `禁用规则「${r.name}」后，该规则将不再触发告警通知。确定继续？`,
      positiveText: '确认禁用',
      negativeText: '取消',
      onPositiveClick: async () => {
        try {
          await ruleApi.disable(r.rule_id)
          message.success('规则已禁用')
          fetchRules()
        } catch (e: any) {
          message.error(e?.message || '操作失败')
        }
      },
    })
  } else {
    try {
      await ruleApi.enable(r.rule_id)
      message.success('规则已启用')
      fetchRules()
    } catch (e: any) {
      message.error(e?.message || '操作失败')
    }
  }
}

async function doDelete(r: Rule) {
  try {
    await ruleApi.delete(r.rule_id)
    message.success('规则已删除')
    fetchRules()
  } catch (e: any) {
    message.error(e?.message || '删除失败')
  }
}

function openTest(r: Rule) {
  testingRuleId.value = r.rule_id
  testingRuleName.value = r.name
  const points = r.conditions?.map(c => c.point).filter(Boolean) || []
  testPointFields.value = [...new Set(points)]
  for (const key of Object.keys(testPointValues)) { delete testPointValues[key] }
  for (const pt of testPointFields.value) { testPointValues[pt] = null }
  testResult.value = null
  showTestModal.value = true
}

async function handleTest() {
  const values: Record<string, number> = {}
  for (const pt of testPointFields.value) {
    if (testPointValues[pt] !== null && testPointValues[pt] !== undefined) {
      values[pt] = testPointValues[pt] as number
    }
  }
  if (Object.keys(values).length === 0) {
    message.warning('请至少输入一个测点的模拟值')
    return
  }
  testing.value = true
  try {
    const result = await ruleApi.test(testingRuleId.value, values)
    testResult.value = result?.triggered ?? result?.fired ?? false
    message.success('规则测试完成')
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || '测试失败')
  } finally {
    testing.value = false
  }
}

onMounted(() => { fetchRules(); fetchDevices() })
</script>
