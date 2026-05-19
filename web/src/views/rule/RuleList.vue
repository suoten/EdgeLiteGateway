<template>
  <n-space vertical :size="16">
    <n-space justify="space-between">
      <n-space>
        <n-input v-model:value="searchText" :placeholder="t('ruleList.searchPlaceholder')" clearable style="width: 200px" @update:value="() => { pagination.page = 1; fetchRules() }" />
        <n-select v-model:value="filterSeverity" :options="severityOptions" :placeholder="t('ruleList.levelFilter')" clearable style="width: 120px" @update:value="() => { pagination.page = 1; fetchRules() }" />
      </n-space>
      <n-button type="primary" @click="showCreateModal = true">{{ t('ruleList.createRule') }}</n-button>
    </n-space>

    <n-data-table :columns="columns" :data="rules" :loading="loading" :pagination="pagination" :row-key="(r: Rule) => r.rule_id">
      <template #empty>
        <n-empty v-if="!loading" :description="t('ruleList.emptyDesc')" style="padding: 40px 0" />
      </template>
    </n-data-table>

    <n-modal v-model:show="showCreateModal" :title="t('ruleList.createTitle')" preset="card" style="width: 640px">
      <n-form :model="createForm" label-placement="left" label-width="90" :rules="createRules" ref="createFormRef">
        <n-form-item :label="t('ruleList.alarmTemplate')">
          <n-select v-model:value="selectedTemplate" :options="templateOptions" :placeholder="t('ruleList.templatePlaceholder')" clearable @update:value="onTemplateChange" />
        </n-form-item>
        <n-alert v-if="selectedTemplateDesc" type="info" :bordered="false" style="margin-bottom: 12px">{{ selectedTemplateDesc }}</n-alert>
        <n-form-item :label="t('ruleList.ruleName')" path="name"><n-input v-model:value="createForm.name" :placeholder="t('ruleList.ruleNamePlaceholder')" /></n-form-item>
        <n-form-item :label="t('ruleList.relatedDevice')" path="device_id">
          <n-select v-model:value="createForm.device_id" :options="deviceOptions" :placeholder="t('ruleList.devicePlaceholder')" filterable @update:value="onDeviceChange" />
        </n-form-item>
        <n-form-item :label="t('ruleList.logicCombo')">
          <n-space align="center">
            <n-radio-group v-model:value="createForm.logic">
              <n-radio value="AND">{{ t('ruleList.logicAnd') }}</n-radio>
              <n-radio value="OR">{{ t('ruleList.logicOr') }}</n-radio>
            </n-radio-group>
            <n-tooltip trigger="hover">
              <template #trigger><n-text depth="3" style="cursor: help">ⓘ</n-text></template>
              {{ t('ruleList.logicHint') }}
            </n-tooltip>
          </n-space>
        </n-form-item>
        <n-form-item :label="t('ruleList.duration')">
          <n-space align="center">
            <n-input-number v-model:value="createForm.duration" :min="0" :max="3600" placeholder="0" style="width: 120px" />
            <n-text>{{ t('deviceList.seconds') }}</n-text>
            <n-tooltip trigger="hover">
              <template #trigger><n-text depth="3" style="cursor: help">ⓘ</n-text></template>
              {{ t('ruleList.durationHint') }}
            </n-tooltip>
          </n-space>
        </n-form-item>
        <n-form-item :label="t('ruleList.severityLevel')" path="severity">
          <n-select v-model:value="createForm.severity" :options="severityOptions" :placeholder="t('ruleList.severityPlaceholder')" />
        </n-form-item>
        <n-form-item :label="t('ruleList.notifyChannel')">
          <n-select v-model:value="createForm.notify_channels" :options="channelOptions" multiple :placeholder="t('ruleList.channelPlaceholder')" />
        </n-form-item>
        <n-form-item :label="t('ruleList.condition')">
          <n-space vertical style="width: 100%">
            <n-space v-for="(cond, i) in createForm.conditions" :key="i" align="center">
              <n-select v-model:value="cond.point" :options="pointOptions" filterable :placeholder="t('ruleList.pointPlaceholder')" style="width: 120px" />
              <n-select v-model:value="cond.operator" :options="operatorOptions" style="width: 100px" />
              <n-input-number v-model:value="cond.threshold" :placeholder="t('ruleList.thresholdPlaceholder')" style="width: 120px" />
              <n-button text type="error" @click="createForm.conditions.splice(i, 1)">{{ t('common.delete') }}</n-button>
            </n-space>
            <n-button dashed @click="createForm.conditions.push({ point: '', operator: '>', threshold: 0 })">{{ t('ruleList.addCondition') }}</n-button>
          </n-space>
        </n-form-item>
      </n-form>
      <template #action>
        <n-button @click="showCreateModal = false">{{ t('common.cancel') }}</n-button>
        <n-button type="primary" :loading="creating" @click="handleCreate">{{ t('deviceList.create') }}</n-button>
      </template>
    </n-modal>

    <n-modal v-model:show="showEditModal" :title="t('ruleList.editTitle')" preset="card" style="width: 640px">
      <n-form :model="editForm" label-placement="left" label-width="90" :rules="createRules" ref="editFormRef">
        <n-form-item :label="t('ruleList.ruleName')" path="name"><n-input v-model:value="editForm.name" /></n-form-item>
        <n-form-item :label="t('ruleList.relatedDevice')" path="device_id">
          <n-select v-model:value="editForm.device_id" :options="deviceOptions" filterable @update:value="onDeviceChange" />
        </n-form-item>
        <n-form-item :label="t('ruleList.logicCombo')">
          <n-radio-group v-model:value="editForm.logic">
            <n-radio value="AND">{{ t('ruleList.logicAnd') }}</n-radio>
            <n-radio value="OR">{{ t('ruleList.logicOr') }}</n-radio>
          </n-radio-group>
        </n-form-item>
        <n-form-item :label="t('ruleList.duration')"><n-input-number v-model:value="editForm.duration" :min="0" :max="3600" style="width: 120px" /> {{ t('deviceList.seconds') }}</n-form-item>
        <n-form-item :label="t('ruleList.severityLevel')" path="severity">
          <n-select v-model:value="editForm.severity" :options="severityOptions" />
        </n-form-item>
        <n-form-item :label="t('ruleList.notifyChannel')">
          <n-select v-model:value="editForm.notify_channels" :options="channelOptions" multiple />
        </n-form-item>
        <n-form-item :label="t('ruleList.condition')">
          <n-space vertical style="width: 100%">
            <n-space v-for="(cond, i) in editForm.conditions" :key="i" align="center">
              <n-select v-model:value="cond.point" :options="pointOptions" filterable :placeholder="t('ruleList.pointPlaceholder')" style="width: 120px" />
              <n-select v-model:value="cond.operator" :options="operatorOptions" style="width: 100px" />
              <n-input-number v-model:value="cond.threshold" :placeholder="t('ruleList.thresholdPlaceholder')" style="width: 120px" />
              <n-button text type="error" @click="editForm.conditions.splice(i, 1)">{{ t('common.delete') }}</n-button>
            </n-space>
            <n-button dashed @click="editForm.conditions.push({ point: '', operator: '>', threshold: 0 })">{{ t('ruleList.addCondition') }}</n-button>
          </n-space>
        </n-form-item>
      </n-form>
      <template #action>
        <n-button @click="showEditModal = false">{{ t('common.cancel') }}</n-button>
        <n-button type="primary" :loading="saving" @click="handleEdit">{{ t('common.save') }}</n-button>
      </template>
    </n-modal>

    <n-modal v-model:show="showTestModal" :title="t('ruleList.testTitle', { name: testingRuleName })" preset="card" style="width: 560px">
      <n-alert type="info" :bordered="false" style="margin-bottom: 12px">
        {{ t('ruleList.testDesc') }}
      </n-alert>
      <n-form label-placement="left" label-width="90">
        <n-form-item v-for="pt in testPointFields" :key="pt" :label="pt">
          <n-input-number v-model:value="testPointValues[pt]" :placeholder="t('ruleList.simValuePlaceholder')" style="width: 200px" />
        </n-form-item>
      </n-form>
      <n-alert v-if="testResult !== null" :type="testResult ? 'warning' : 'success'" :bordered="false">
        {{ testResult ? t('ruleList.willTrigger') : t('ruleList.willNotTrigger') }}
      </n-alert>
      <template #action>
        <n-button @click="showTestModal = false">{{ t('deviceList.close') }}</n-button>
        <n-button type="primary" :loading="testing" @click="handleTest">{{ t('ruleList.runTest') }}</n-button>
      </template>
    </n-modal>
  </n-space>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, h } from 'vue'
import { NButton, NTag, NSpace, NPopconfirm, useMessage, useDialog } from 'naive-ui'
// FIXED: 原问题-添加i18n支持
import { t } from '@/i18n'
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
  { label: t('alarm.critical'), value: 'critical' },
  { label: t('alarm.warning'), value: 'warning' },
  { label: t('alarm.info'), value: 'info' },
]

const channelOptions = [
  { label: t('channel.dingtalk'), value: 'dingtalk' },
  { label: t('channel.email'), value: 'email' },
  { label: t('channel.wechat'), value: 'wechat' },
  { label: t('channel.webhook'), value: 'webhook' },
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
// FIXED: 原问题-pointOptions仅依赖createForm，编辑弹窗测点下拉框始终为空
// 现根据当前活跃表单（创建或编辑）动态计算测点选项
const activeDeviceId = computed(() => {
  if (showEditModal.value) return editForm.device_id
  if (showCreateModal.value) return createForm.device_id
  return ''
})
const pointOptions = computed(() => {
  if (!activeDeviceId.value) return []
  const dev = devices.value.find(d => d.device_id === activeDeviceId.value)
  // FIXED: 原问题-dev?.points?.map(...)后链式调用不安全，改为(dev?.points ?? []).map(...)
  return (dev?.points ?? []).map((p: any) => ({ label: `${p.name}${p.unit ? ' (' + p.unit + ')' : ''}`, value: p.name }))
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
  name: { required: true, message: t('ruleList.nameRequired'), trigger: 'blur' },
  device_id: { required: true, message: t('ruleList.deviceRequired'), trigger: 'change' },
  severity: { required: true, message: t('ruleList.severityRequired'), trigger: 'change' },
}

const columns = [
  { title: t('ruleList.ruleId'), key: 'rule_id', width: 140 },
  { title: t('ruleList.name'), key: 'name', width: 150, sorter: true },
  { title: t('ruleList.deviceId'), key: 'device_id', width: 160 },
  { title: t('ruleList.logic'), key: 'logic', width: 60 },
  { title: t('ruleList.durationCol'), key: 'duration', width: 80, render: (r: Rule) => `${r.duration}s` },
  {
    title: t('ruleList.level'), key: 'severity', width: 80,
    render: (r: Rule) => h(NTag, { type: severityColor[r.severity] || 'default', size: 'small' }, { default: () => severityLabel[r.severity] || r.severity }),
  },
  {
    title: t('ruleList.status'), key: 'enabled', width: 80,
    render: (r: Rule) => h(NTag, { type: r.enabled ? 'success' : 'default', size: 'small' }, { default: () => r.enabled ? t('ruleList.enabled') : t('ruleList.disabled') }),
  },
  { title: t('ruleList.notify'), key: 'notify_channels', width: 150, render: (r: Rule) => (r.notify_channels ?? []).map((c: string) => channelLabel[c] || c).join(', ') },
  {
    title: t('ruleList.actions'), key: 'actions', width: 240,
    render: (r: Rule) =>
      h(NSpace, null, {
        default: () => [
          h(NButton, { text: true, type: 'primary', onClick: () => openEdit(r) }, { default: () => t('common.edit') }),
          h(NButton, { text: true, type: 'info', onClick: () => openTest(r) }, { default: () => t('ruleList.testRule') }),
          h(NButton, { text: true, type: r.enabled ? 'warning' : 'success', onClick: () => handleToggle(r) }, { default: () => r.enabled ? t('ruleList.disabled') : t('ruleList.enabled') }),
          h(NPopconfirm as any, { onPositiveClick: () => doDelete(r) }, {
            trigger: () => h(NButton, { text: true, type: 'error' }, { default: () => t('common.delete') }),
            default: () => t('ruleList.deleteConfirm', { name: r.name }),
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
    // FIXED: 原问题-pagination.itemCount无空值保护，可能显示NaN
    pagination.itemCount = data?.total ?? 0
  } catch (e: any) {
    rules.value = []
    message.error(e?.response?.data?.detail || e?.message || t('ruleList.fetchFailed'))
  } finally {
    loading.value = false
  }
}

async function fetchDevices() {
  try {
    const data = await deviceApi.list({ page: 1, size: 5000 })  // FIXED: 原问题-size:9999不合理，改为5000匹配后端_MAX_QUERY_SIZE
    const devs = data?.data ?? []
    devices.value = devs
    deviceOptions.value = devs.map(d => ({ label: `${d.name} (${d.device_id})`, value: d.device_id }))
  } catch (e: any) {
    message.error(e?.response?.data?.detail || t('ruleList.fetchDeviceFailed'))
    devices.value = []
    deviceOptions.value = []
  }
}

async function handleCreate() {
  try {
    await createFormRef.value?.validate()
  } catch { return }
  if (!createForm.conditions.length || createForm.conditions.some((c: any) => !c.point || c.threshold === undefined || c.threshold === null)) {
    message.error(t('ruleList.conditionRequired'))
    return
  }
  creating.value = true
  try {
    await ruleApi.create(createForm as any)
    message.success(t('ruleList.createSuccess'))
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
    message.error(e?.response?.data?.detail || e?.message || t('ruleList.createFailed'))
  } finally {
    creating.value = false
  }
}

function openEdit(r: Rule) {
  editingRuleId.value = r.rule_id
  editForm.name = r.name
  editForm.device_id = r.device_id ?? ''
  editForm.logic = r.logic
  editForm.duration = r.duration
  editForm.severity = r.severity
  editForm.notify_channels = [...(r.notify_channels || ['dingtalk'])]
  editForm.conditions = r.conditions?.length ? r.conditions.map(c => ({ ...c })) : [{ point: '', operator: '>', threshold: 0 }]
  showEditModal.value = true
}

async function handleEdit() {
  // FIXED: 原问题-编辑表单未做验证，必填字段可提交空值
  try {
    await editFormRef.value?.validate()
  } catch { return }
  if (!editForm.conditions || !editForm.conditions.length || editForm.conditions.some((c: any) => !c.point)) {
    message.error(t('ruleList.conditionRequired'))
    return
  }
  saving.value = true
  try {
    await ruleApi.update(editingRuleId.value, editForm as any)  // FIXED: 原问题-editForm.rule_id未定义导致API路径为/rules/undefined
    message.success(t('ruleList.updateSuccess'))
    showEditModal.value = false
    fetchRules()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || t('ruleList.updateFailed'))
  } finally {
    saving.value = false
  }
}

async function handleToggle(r: Rule) {
  if (r.enabled) {
    dialog.warning({
    title: t('ruleList.disableTitle'),
    content: t('ruleList.disableContent', { name: r.name }),
    positiveText: t('ruleList.confirmDisable'),
    negativeText: t('common.cancel'),
      onPositiveClick: async () => {
        try {
          await ruleApi.disable(r.rule_id)
          message.success(t('ruleList.disableSuccess'))
          fetchRules()
        } catch (e: any) {
          message.error(e?.response?.data?.detail || e?.message || t('ruleList.operationFailed'))
        }
      },
    })
  } else {
    try {
      await ruleApi.enable(r.rule_id)
      message.success(t('ruleList.enableSuccess'))
      fetchRules()
    } catch (e: any) {
      message.error(e?.response?.data?.detail || t('ruleList.operationFailed'))
    }
  }
}

async function doDelete(r: Rule) {
  try {
    await ruleApi.delete(r.rule_id)
    message.success(t('ruleList.deleteSuccess'))
      fetchRules()
    } catch (e: any) {
      message.error(e?.response?.data?.detail || t('ruleList.deleteFailed'))
  }
}

function openTest(r: Rule) {
  testingRuleId.value = r.rule_id
  testingRuleName.value = r.name
  // FIXED: 原问题-r.conditions?.map(...).filter(...)可选链后链式调用会崩溃
  const points = (r.conditions ?? []).map(c => c.point).filter(Boolean)
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
    message.warning(t('ruleList.testInputRequired'))
    return
  }
  testing.value = true
  try {
    const result = await ruleApi.test(testingRuleId.value, values)
    testResult.value = result?.triggered ?? result?.fired ?? false
    message.success(t('ruleList.testComplete'))
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || t('ruleList.testFailed'))
  } finally {
    testing.value = false
  }
}

onMounted(() => { fetchRules(); fetchDevices() })
</script>
