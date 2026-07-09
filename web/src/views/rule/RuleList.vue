<template>
  <n-space vertical :size="16">
    <n-space justify="space-between">
      <n-space>
        <n-input v-model:value="searchText" :placeholder="t('ruleList.searchPlaceholder')" clearable style="width: 200px" @update:value="onSearchInput" @keyup.enter="onSearchEnter" />
        <n-select v-model:value="filterSeverity" :options="severityOptions" :placeholder="t('ruleList.levelFilter')" clearable style="width: 120px" @update:value="() => { pagination.page = 1; fetchRules() }" />
        <!-- 修复5: 规则导入/导出 -->
        <n-button @click="handleExportRules" :loading="exporting" :disabled="!auth.isOperator">
          {{ t('ruleList.exportRules') }}
        </n-button>
        <n-button @click="triggerImportRules" :disabled="!auth.isOperator">
          {{ t('ruleList.importRules') }}
        </n-button>
        <input ref="importInputRef" type="file" accept="application/json" style="display:none" @change="handleImportRules" />
      </n-space>
      <n-button type="primary" :disabled="!auth.isOperator" @click="showCreateModal = true">{{ t('ruleList.createRule') }}</n-button>
    </n-space>

    <n-data-table :columns="columns" :data="rules" :loading="loading" :pagination="pagination" :row-key="(r: Rule) => r.rule_id" remote :scroll-x="1330">
      <template #empty>
        <n-empty v-if="!loading" :description="t('ruleList.emptyDesc')" style="padding: 40px 0" />
      </template>
    </n-data-table>

    <n-modal v-model:show="showCreateModal" :title="t('ruleList.createTitle')" preset="card" style="width: 640px; max-width: 95vw" :close-on-esc="true" :auto-focus="true" :close-on-esc-aria-label="t('common.closeDialog')">
      <n-form :model="createForm" label-placement="left" label-width="90" :rules="createRules" ref="createFormRef">
        <n-form-item :label="t('ruleList.alarmTemplate')">
          <n-select v-model:value="selectedTemplate" :options="templateOptions" :placeholder="t('ruleList.templatePlaceholder')" clearable @update:value="onTemplateChange" />
        </n-form-item>
        <n-alert v-if="selectedTemplateDesc" type="info" :bordered="false" style="margin-bottom: 12px">{{ selectedTemplateDesc }}</n-alert>
        <n-form-item :label="t('ruleList.ruleName')" path="name"><n-input v-model:value="createForm.name" :placeholder="t('ruleList.ruleNamePlaceholder')" /></n-form-item>
        <n-form-item :label="t('ruleList.relatedDevice')" path="device_id">
          <n-select v-model:value="createForm.device_id" filterable remote clearable :options="deviceOptions" :placeholder="t('ruleList.devicePlaceholder')" @search="searchDevices" @update:value="onDeviceChange" />
        </n-form-item>
        <n-form-item :label="t('ruleList.logicCombo')">
          <n-space align="center">
            <n-radio-group v-model:value="createForm.logic">
              <n-radio value="AND">{{ t('ruleList.logicAnd') }}</n-radio>
              <n-radio value="OR">{{ t('ruleList.logicOr') }}</n-radio>
              <n-radio value="NOT">{{ t('ruleList.logicNot') }}</n-radio>
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
        <n-form-item :label="t('ruleList.notifyChannel')" path="notify_channels">
          <n-select v-model:value="createForm.notify_channels" :options="channelOptions" multiple :placeholder="t('ruleList.channelPlaceholder')" />
        </n-form-item>
        <n-form-item :label="t('ruleList.condition')">
          <div class="condition-editor" style="width: 100%">
            <!-- 修复26: 可视化条件表达式预览 -->
            <div class="condition-preview">
              <n-text depth="3" style="font-size: 12px">{{ t('ruleList.expressionPreview') }}:</n-text>
              <code class="condition-expression">{{ createExpressionPreview }}</code>
            </div>
            <div v-for="(cond, i) in createForm.conditions" :key="cond._uid" class="condition-block">
              <div v-if="i > 0" class="condition-connector">
                <n-tag size="small" :type="createForm.logic === 'AND' ? 'info' : 'warning'" round>{{ createForm.logic }}</n-tag>
              </div>
              <div class="condition-fields">
                <n-select v-model:value="cond.point" :options="pointOptions" filterable :placeholder="t('ruleList.pointPlaceholder')" style="flex: 1; min-width: 120px" />
                <n-select v-model:value="cond.operator" :options="operatorOptions" style="width: 100px" />
                <n-input-number v-model:value="cond.threshold" :placeholder="t('ruleList.thresholdPlaceholder')" style="width: 140px" />
                <n-button text type="error" @click="createForm.conditions.splice(i, 1)" :aria-label="t('common.delete')">
                  <template #icon><n-icon :component="CloseOutline" size="16" /></template>
                </n-button>
              </div>
            </div>
            <n-button dashed block @click="createForm.conditions.push({ _uid: Date.now() + Math.random(), point: '', operator: '>', threshold: 0 })">{{ t('ruleList.addCondition') }}</n-button>
          </div>
        </n-form-item>
      </n-form>
      <template #action>
        <n-button @click="showCreateModal = false">{{ t('common.cancel') }}</n-button>
        <n-button type="primary" :loading="creating" @click="handleCreate">{{ t('deviceList.create') }}</n-button>
      </template>
    </n-modal>

    <n-modal v-model:show="showEditModal" :title="t('ruleList.editTitle')" preset="card" style="width: 640px; max-width: 95vw" :close-on-esc="true" :auto-focus="true" :close-on-esc-aria-label="t('common.closeDialog')">
      <n-form :model="editForm" label-placement="left" label-width="90" :rules="editRules" ref="editFormRef">
        <n-form-item :label="t('ruleList.ruleName')" path="name"><n-input v-model:value="editForm.name" /></n-form-item>
        <n-form-item :label="t('ruleList.relatedDevice')" path="device_id">
          <n-select v-model:value="editForm.device_id" :options="deviceOptions" filterable disabled />
        </n-form-item>
        <n-form-item :label="t('ruleList.logicCombo')">
          <n-radio-group v-model:value="editForm.logic">
            <n-radio value="AND">{{ t('ruleList.logicAnd') }}</n-radio>
            <n-radio value="OR">{{ t('ruleList.logicOr') }}</n-radio>
            <n-radio value="NOT">{{ t('ruleList.logicNot') }}</n-radio>
          </n-radio-group>
          <n-text v-if="editForm.logic === 'NOT' && editForm.conditions.length > 1" type="warning" style="font-size: 12px; margin-left: 8px">
            {{ t('ruleList.notLogicHint') }}
          </n-text>
        </n-form-item>
        <n-form-item :label="t('ruleList.duration')"><n-input-number v-model:value="editForm.duration" :min="0" :max="3600" style="width: 120px" /> {{ t('deviceList.seconds') }}</n-form-item>
        <n-form-item :label="t('ruleList.severityLevel')" path="severity">
          <n-select v-model:value="editForm.severity" :options="severityOptions" />
        </n-form-item>
        <n-form-item :label="t('ruleList.notifyChannel')" path="notify_channels">
          <n-select v-model:value="editForm.notify_channels" :options="channelOptions" multiple />
        </n-form-item>
        <n-form-item :label="t('ruleList.condition')">
          <div class="condition-editor" style="width: 100%">
            <!-- 修复26: 可视化条件表达式预览 -->
            <div class="condition-preview">
              <n-text depth="3" style="font-size: 12px">{{ t('ruleList.expressionPreview') }}:</n-text>
              <code class="condition-expression">{{ editExpressionPreview }}</code>
            </div>
            <div v-for="(cond, i) in editForm.conditions" :key="cond._uid" class="condition-block">
              <div v-if="i > 0" class="condition-connector">
                <n-tag size="small" :type="editForm.logic === 'AND' ? 'info' : 'warning'" round>{{ editForm.logic }}</n-tag>
              </div>
              <div class="condition-fields">
                <n-select v-model:value="cond.point" :options="pointOptions" filterable :placeholder="t('ruleList.pointPlaceholder')" style="flex: 1; min-width: 120px" />
                <n-select v-model:value="cond.operator" :options="operatorOptions" style="width: 100px" />
                <n-input-number v-model:value="cond.threshold" :placeholder="t('ruleList.thresholdPlaceholder')" style="width: 140px" />
                <n-button text type="error" @click="editForm.conditions.splice(i, 1)" :aria-label="t('common.delete')">
                  <template #icon><n-icon :component="CloseOutline" size="16" /></template>
                </n-button>
              </div>
            </div>
            <n-button dashed block @click="editForm.conditions.push({ _uid: Date.now() + Math.random(), point: '', operator: '>', threshold: 0 })">{{ t('ruleList.addCondition') }}</n-button>
          </div>
        </n-form-item>
      </n-form>
      <template #action>
        <n-button @click="showEditModal = false">{{ t('common.cancel') }}</n-button>
        <n-button type="primary" :loading="saving" @click="handleEdit">{{ t('common.save') }}</n-button>
      </template>
    </n-modal>

    <n-modal v-model:show="showTestModal" :title="t('ruleList.testTitle', { name: testingRuleName })" preset="card" style="width: 560px; max-width: 95vw" :close-on-esc="true" :auto-focus="true" :close-on-esc-aria-label="t('common.closeDialog')">
      <n-alert type="info" :bordered="false" style="margin-bottom: 12px">
        {{ t('ruleList.testDesc') }}
      </n-alert>
      <n-form :model="testPointValues" ref="testFormRef" :rules="testRules" label-placement="left" label-width="90">
        <n-form-item v-for="pt in testPointFields" :key="pt" :label="pt" :path="pt">
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

    <!-- Share Modal -->
    <n-modal v-model:show="showShareModal" preset="card" :title="t('resourceShare.shareResource')" style="width: 480px;max-width:95vw" :close-on-esc="true" :auto-focus="true" :close-on-esc-aria-label="t('common.closeDialog')">
      <n-alert type="info" :bordered="false" style="margin-bottom: 16px">{{ t('resourceShare.shareRuleHint') }}</n-alert>
      <n-form ref="shareFormRef" :model="shareForm" :rules="shareRules" label-placement="left" label-width="100">
        <n-form-item :label="t('resourceShare.shareWith')" path="shared_with_user_id">
          <n-select v-model:value="shareForm.shared_with_user_id" :options="userOptions" :placeholder="t('resourceShare.selectUser')" filterable />
        </n-form-item>
        <n-form-item :label="t('resourceShare.permission')">
          <n-select v-model:value="shareForm.permission_level" :options="permissionOptions" />
        </n-form-item>
      </n-form>
      <template #action>
        <n-button @click="showShareModal = false">{{ t('common.cancel') }}</n-button>
        <n-button type="primary" :loading="sharing" @click="handleShare">{{ t('resourceShare.share') }}</n-button>
      </template>
    </n-modal>

    <!-- SEC-FIX-RULE-VERSION: 版本历史抽屉 -->
    <n-drawer v-model:show="showVersionDrawer" :width="720" placement="right">
      <n-drawer-content :title="t('rule.versionHistory', { name: versionRuleName })" closable>
        <!-- 修复4: 版本对比 -->
        <n-space align="center" style="margin-bottom: 12px">
          <n-text depth="3">{{ t('ruleList.compareHint') }}</n-text>
          <n-select v-model:value="compareFrom" :options="versionSelectOptions" :placeholder="t('ruleList.compareFrom')" style="width: 120px" />
          <n-text>→</n-text>
          <n-select v-model:value="compareTo" :options="versionSelectOptions" :placeholder="t('ruleList.compareTo')" style="width: 120px" />
          <n-button size="small" type="primary" :disabled="!compareFrom || !compareTo || compareFrom === compareTo" @click="handleCompareVersions">
            {{ t('ruleList.compareVersions') }}
          </n-button>
        </n-space>
        <n-data-table
          :columns="versionColumns"
          :data="versionList"
          :loading="versionLoading"
          size="small"
          :row-key="(row: any) => row.version"
        >
          <template #empty>
            <n-empty :description="t('common.noData')" size="small" />
          </template>
        </n-data-table>
      </n-drawer-content>
    </n-drawer>

    <!-- 修复4: 版本对比弹窗 -->
    <n-modal v-model:show="showCompareModal" preset="card" :title="t('ruleList.versionCompareTitle')" style="width: 800px; max-width: 95vw" :close-on-esc="true" :auto-focus="true" :close-on-esc-aria-label="t('common.closeDialog')">
      <n-data-table :columns="compareColumns" :data="compareData" :loading="compareLoading" size="small" />
    </n-modal>

    <!-- 修复6: 执行历史抽屉 -->
    <n-drawer v-model:show="showHistoryDrawer" :width="620" placement="right">
      <n-drawer-content :title="t('ruleList.executionHistory', { name: historyRuleName })" closable>
        <n-data-table :columns="historyColumns" :data="historyList" :loading="historyLoading" size="small">
          <template #empty>
            <n-empty :description="t('common.noData')" size="small" />
          </template>
        </n-data-table>
      </n-drawer-content>
    </n-drawer>
  </n-space>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, onUnmounted, h } from 'vue'
import { NButton, NTag, NSpace, NPopconfirm, NDropdown } from 'naive-ui'
import { CloseOutline } from '@vicons/ionicons5'
import { t } from '@/i18n'
import { extractError } from '@/utils/errorCodes'
import { ruleApi, deviceApi, userApi, resourceShareApi, alarmApi, type Rule, type Device, type User } from '@/api'
import { severityLabel, channelLabel } from '@/utils/enumLabels'
import { RULE_TEMPLATES, OPERATOR_OPTIONS, getTemplateCategories } from '@/constants/ruleTemplates'
import { useAuthStore } from '@/stores/auth'
import { message, dialog } from '@/utils/discreteApi'

const auth = useAuthStore()

const rules = ref<Rule[]>([])
const devices = ref<Device[]>([])
const loading = ref(false)
const showCreateModal = ref(false)
const showEditModal = ref(false)
const showShareModal = ref(false)
const creating = ref(false)
const saving = ref(false)
const testing = ref(false)
const sharing = ref(false)
const showTestModal = ref(false)
const testingRuleId = ref('')
const testingRuleName = ref('')
const testPointFields = ref<string[]>([])
const testPointValues = reactive<Record<string, number | null>>({})
// [AUDIT-FIX] 测试规则表单添加必填校验（动态生成每个测点的 required 规则）
const testFormRef = ref<any>(null)
const testRules = computed(() => {
  const rules: Record<string, any> = {}
  for (const pt of testPointFields.value) {
    rules[pt] = { type: 'number' as const, required: true, message: t('ruleList.testInputRequired'), trigger: ['input', 'blur'] }
  }
  return rules
})
const testResult = ref<boolean | null>(null)
const searchText = ref('')
const filterSeverity = ref<string | null>(null)
const createFormRef = ref<any>(null)
const editFormRef = ref<any>(null)
const editingRuleId = ref('')

const shareForm = reactive({ rule_id: '', shared_with_user_id: '', permission_level: 'read' })
const shareFormRef = ref<any>(null)
// [AUDIT-FIX] 表单验证绑定：share 表单缺少 :rules 校验
const shareRules = computed(() => ({
  shared_with_user_id: { required: true, message: t('resourceShare.selectUser'), trigger: ['change', 'blur'] },
}))
const allUsers = ref<User[]>([])
const userOptions = computed(() =>
  allUsers.value
    .filter(u => u.username !== auth.username)
    .map(u => ({ label: `${u.username} (${u.role})`, value: u.user_id }))
)
// [AUDIT-FIX] i18n 响应式：permissionOptions 改为 computed 实现语言切换响应式
const permissionOptions = computed(() => [
  { label: t('resourceShare.readOnly'), value: 'read' },
  { label: t('resourceShare.readWrite'), value: 'write' },
])

const pagination = reactive({
  page: 1,
  pageSize: 20,
  itemCount: 0,
  pageSizes: [10, 20, 50, 100],
  showSizePicker: true,
  onChange: (p: number) => { pagination.page = p; fetchRules() },
  onUpdatePageSize: (s: number) => { pagination.pageSize = s; pagination.page = 1; fetchRules() },
})

const deviceOptions = ref<{ label: string; value: string }[]>([])

// [AUDIT-FIX] i18n 响应式：severityOptions 改为 computed 实现语言切换响应式
const severityOptions = computed(() => [
  { label: t('alarm.critical'), value: 'critical' },
  { label: t('alarm.major'), value: 'major' },
  { label: t('alarm.warning'), value: 'warning' },
  { label: t('alarm.minor'), value: 'minor' },
  { label: t('alarm.info'), value: 'info' },
])

// [AUDIT-FIX] i18n 响应式：channelOptions 改为 computed 实现语言切换响应式
const channelOptions = computed(() => [
  { label: t('channel.dingtalk'), value: 'dingtalk' },
  { label: t('channel.email'), value: 'email' },
  { label: t('channel.wechat'), value: 'wechat' },
  { label: t('channel.webhook'), value: 'webhook' },
])

const operatorOptions = OPERATOR_OPTIONS.value  // FIXED-P3: computed→.value

// 修复26: 可视化条件表达式预览
function buildExpression(conditions: any[], logic: string): string {
  if (!conditions || !conditions.length) return ''
  const parts = conditions.map((c: any) => {
    const point = c.point || '?'
    const op = c.operator || '?'
    const threshold = (c.threshold !== undefined && c.threshold !== null) ? c.threshold : '?'
    return `${point} ${op} ${threshold}`
  })
  const joiner = logic === 'AND' ? ' AND ' : logic === 'OR' ? ' OR ' : ' NOT '
  if (logic === 'NOT') return `NOT (${parts.join(' AND ')})`
  return parts.join(joiner)
}
const createExpressionPreview = computed(() => buildExpression(createForm.conditions, createForm.logic))
const editExpressionPreview = computed(() => buildExpression(editForm.conditions, editForm.logic))

const selectedTemplate = ref<string | null>(null)
const selectedTemplateDesc = computed(() => {
  if (!selectedTemplate.value) return ''
  return RULE_TEMPLATES.value.find(t => t.id === selectedTemplate.value)?.description || ''
})
const templateOptions = computed(() => {
  const categories = getTemplateCategories()
  return categories.map(cat => ({
    type: 'group' as const,
    label: cat,
    key: cat,
    children: RULE_TEMPLATES.value.filter(t => t.category === cat).map(t => ({  // FIXED-P3: computed→.value
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
  const tmpl = RULE_TEMPLATES.value.find(t => t.id === val)  // FIXED-P3: computed→.value
  if (!tmpl) return
  createForm.name = tmpl.name
  createForm.severity = tmpl.severity
  createForm.logic = tmpl.logic
  createForm.duration = tmpl.duration
  createForm.conditions = tmpl.conditions.map(c => ({ _uid: Date.now() + Math.random(), point: c.point, operator: c.operator, threshold: c.threshold }))
}

function onDeviceChange(deviceId: string) {
  // 选中设备后加载其信息（含测点），供 pointOptions 计算使用
  if (deviceId) {
    loadDeviceForDisplay(deviceId)
  }
}

const severityColor: Record<string, any> = { critical: 'error', major: 'warning', warning: 'warning', minor: 'info', info: 'info' }

// [AUDIT-FIX] i18n 响应式：createRules 改为 computed 实现语言切换响应式
const createRules = computed(() => ({
  name: [
    { required: true, message: t('ruleList.nameRequired'), trigger: ['input', 'blur'] },
    { min: 1, max: 64, message: t('ruleList.nameLength'), trigger: ['input', 'blur'] },
  ],
  device_id: { required: true, message: t('ruleList.deviceRequired'), trigger: ['change', 'blur'] },
  severity: { required: true, message: t('ruleList.severityRequired'), trigger: ['change', 'blur'] },
  notify_channels: { required: true, type: 'array' as const, min: 1, message: t('ruleList.channelRequired'), trigger: ['change', 'blur'] },
}))

// [AUDIT-FIX] i18n 响应式：editRules 改为 computed 实现语言切换响应式
const editRules = computed(() => ({
  name: [
    { required: true, message: t('ruleList.nameRequired'), trigger: ['input', 'blur'] },
    { min: 1, max: 64, message: t('ruleList.nameLength'), trigger: ['input', 'blur'] },
  ],
  severity: { required: true, message: t('ruleList.severityRequired'), trigger: ['change', 'blur'] },
  notify_channels: { required: true, type: 'array' as const, min: 1, message: t('ruleList.channelRequired'), trigger: ['change', 'blur'] },
}))

// [AUDIT-FIX] i18n 响应式：columns 改为 computed 实现语言切换响应式
const columns = computed(() => [
  { title: t('ruleList.ruleId'), key: 'rule_id', width: 140 },
  { title: t('ruleList.name'), key: 'name', width: 150 },
  { title: t('ruleList.deviceId'), key: 'device_id', width: 160 },
  { title: t('ruleList.logic'), key: 'logic', width: 60 },
  { title: t('ruleList.durationCol'), key: 'duration', width: 80, render: (r: Rule) => `${r.duration}s` },
  {
    title: t('ruleList.level'), key: 'severity', width: 80,
    render: (r: Rule) => h(NTag, { type: severityColor[r.severity] || 'default', size: 'small' }, { default: () => severityLabel.value[r.severity] || r.severity }),  // FIXED-P3: computed→.value
  },
  {
    title: t('ruleList.status'), key: 'enabled', width: 80,
    render: (r: Rule) => h(NTag, { type: r.enabled ? 'success' : 'default', size: 'small' }, { default: () => r.enabled ? t('ruleList.statusEnabled') : t('ruleList.statusDisabled') }),
  },
  { title: t('ruleList.notify'), key: 'notify_channels', width: 150, render: (r: Rule) => (r.notify_channels ?? []).map((c: string) => channelLabel.value[c] || c).join(', ') },  // FIXED-P3: computed→.value
  { title: t('ruleList.inferenceCount') || 'Inferences', key: 'inference_count', width: 100, render: (r: any) => r.inference_count ?? 0 },
  { title: t('ruleList.errorCount') || 'Errors', key: 'error_count', width: 80, render: (r: any) => r.error_count ?? 0 },
  {
    title: t('common.actions'), key: 'actions', width: 200,
    render: (r: Rule) =>
      h(NSpace, { size: 4 }, {
        default: () => [
          h(NButton, { text: true, type: 'primary', onClick: () => openEdit(r) }, { default: () => t('common.edit') }),
          h(NButton, { text: true, type: r.enabled ? 'warning' : 'success', onClick: () => handleToggle(r) }, { default: () => r.enabled ? t('ruleList.actionDisable') : t('ruleList.actionEnable') }),
          h(NDropdown, {
            options: [
              { label: t('ruleList.testRule'), key: 'test' },
              { label: t('ruleList.copyRule'), key: 'copy' },
              { label: t('resourceShare.share'), key: 'share' },
              { label: t('rule.versionHistoryShort'), key: 'versions' },
              { label: t('ruleList.executionHistoryShort'), key: 'history' },
              { type: 'divider', key: 'd1' },
              { label: t('common.delete'), key: 'delete' },
            ],
            onSelect: (key: string) => handleRuleAction(key, r),
          }, {
            trigger: () => h(NButton, { text: true }, { default: () => '...' }),
          }),
        ],
      }),
  },
])

const createForm = reactive({
  name: '',
  device_id: '',
  logic: 'AND' as string,
  duration: 0,
  severity: 'warning' as string,
  notify_channels: ['dingtalk'] as string[],
  conditions: [{ _uid: Date.now() + Math.random(), point: '', operator: '>', threshold: 0 }],
})

const editForm = reactive({
  name: '',
  device_id: '',
  logic: 'AND' as string,
  duration: 0,
  severity: 'warning' as string,
  notify_channels: ['dingtalk'] as string[],
  conditions: [{ _uid: Date.now() + Math.random() + 1, point: '', operator: '>', threshold: 0 }],
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
    message.error(extractError(e, t('ruleList.fetchFailed')))
  } finally {
    loading.value = false
  }
}

let searchSeq = 0
async function searchDevices(query: string) {
  // FIX: 改为远程搜索，避免全量加载数千设备造成性能问题
  if (!query) {
    deviceOptions.value = []
    return
  }
  const seq = ++searchSeq
  try {
    const data = await deviceApi.list({ page: 1, size: 50, search: query })
    if (seq !== searchSeq) return
    deviceOptions.value = (data?.data ?? []).map((d: any) => ({
      label: `${d.name} (${d.device_id})`,
      value: d.device_id,
    }))
  } catch { /* ignore */ }
}

// 加载单个设备信息：用于编辑/复制时回显设备名 + 提供测点列表
async function loadDeviceForDisplay(deviceId: string) {
  if (!deviceId) return
  const hasOption = deviceOptions.value.some(o => o.value === deviceId)
  const hasDevice = devices.value.some(d => d.device_id === deviceId)
  if (hasOption && hasDevice) return
  try {
    const dev = await deviceApi.get(deviceId)
    if (dev) {
      if (!hasOption) {
        deviceOptions.value = [
          ...deviceOptions.value,
          { label: `${dev.name} (${dev.device_id})`, value: dev.device_id },
        ]
      }
      if (!hasDevice) {
        devices.value = [...devices.value, dev]
      }
    }
  } catch { /* ignore */ }
}

async function handleCreate() {
  // FIXED-Critical: 写操作前端权限校验
  if (!auth.isOperator) { message.warning(t('common.permissionDenied')); return }
  try {
    await createFormRef.value?.validate()
  } catch { return }
  if (!createForm.conditions.length || createForm.conditions.some((c: any) => !c.point || !c.operator || c.threshold === undefined || c.threshold === null)) {
    message.error(t('ruleList.conditionRequired'))
    return
  }
  creating.value = true
  try {
    const payload = {
      ...createForm,
      duration: createForm.duration ?? 0,
      conditions: createForm.conditions.map(({ _uid, ...c }: any) => ({
        ...c,
        threshold: c.threshold ?? 0,
      })),
    }
    await ruleApi.create(payload as any)
    message.success(t('ruleList.createSuccess'))
    showCreateModal.value = false
    createForm.name = ''
    createForm.device_id = ''
    createForm.logic = 'AND'
    createForm.duration = 0
    createForm.severity = 'warning'
    createForm.notify_channels = ['dingtalk']
    createForm.conditions = [{ _uid: Date.now() + Math.random(), point: '', operator: '>', threshold: 0 }]
    // FIXED-P0: 原问题-创建规则后未重置分页和过滤条件，新规则在第一页但当前页码可能>1导致看不到
    pagination.page = 1
    await fetchRules()
  } catch (e: any) {
    message.error(extractError(e, t('ruleList.createFailed')))
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
  editForm.conditions = r.conditions?.length ? r.conditions.map(c => ({ _uid: Date.now() + Math.random(), ...c })) : [{ _uid: Date.now() + Math.random(), point: '', operator: '>', threshold: 0 }]
  showEditModal.value = true
  // 加载设备信息用于回显设备名 + 提供测点列表
  if (editForm.device_id) {
    loadDeviceForDisplay(editForm.device_id)
  }
}

async function handleEdit() {
  // FIXED-Critical: 写操作前端权限校验
  if (!auth.isOperator) { message.warning(t('common.permissionDenied')); return }
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
    const { device_id, ...rest } = editForm
    const payload = {
      ...rest,
      duration: editForm.duration ?? 0,
      conditions: editForm.conditions.map(({ _uid, ...c }: any) => ({
        ...c,
        threshold: c.threshold ?? 0,
      })),
    }
    await ruleApi.update(editingRuleId.value, payload as any)
    message.success(t('ruleList.updateSuccess'))
    showEditModal.value = false
    await fetchRules()
  } catch (e: any) {
    message.error(extractError(e, t('ruleList.updateFailed')))
  } finally {
    saving.value = false
  }
}

async function handleToggle(r: Rule) {
  // FIXED-Critical: 启用/禁用规则前端权限校验
  if (!auth.isOperator) { message.warning(t('common.permissionDenied')); return }
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
          await fetchRules()
        } catch (e: any) {
          message.error(extractError(e, t('ruleList.operationFailed')))
        }
      },
    })
  } else {
    try {
      await ruleApi.enable(r.rule_id)
      message.success(t('ruleList.enableSuccess'))
      await fetchRules()
    } catch (e: any) {
      message.error(extractError(e, t('ruleList.operationFailed')))
    }
  }
}

async function doDelete(r: Rule) {
  // FIXED-Critical: 删除规则前端权限校验
  if (!auth.isOperator) { message.warning(t('common.permissionDenied')); return }
  try {
    await ruleApi.delete(r.rule_id)
    message.success(t('ruleList.deleteSuccess'))
    // [AUDIT-FIX] 严重级-删除最后一页最后一项后回退到上一页
    if (pagination.itemCount > 0) {
      const newTotal = pagination.itemCount - 1
      const maxPage = Math.max(1, Math.ceil(newTotal / pagination.pageSize))
      if (pagination.page > maxPage) pagination.page = maxPage
    }
    await fetchRules()
    } catch (e: any) {
      message.error(extractError(e, t('ruleList.deleteFailed')))
  }
}

function handleRuleAction(key: string, r: Rule) {
  switch (key) {
    case 'test': openTest(r); break
    case 'copy': copyRule(r); break
    case 'share': openShare(r); break
    case 'versions': openVersionDrawer(r); break  // SEC-FIX-RULE-VERSION
    case 'history': openHistoryDrawer(r); break  // 修复6: 执行历史
    case 'delete':
      dialog.warning({
        title: t('ruleList.deleteConfirm', { name: r.name }),
        positiveText: t('common.delete'),
        negativeText: t('common.cancel'),
        onPositiveClick: () => doDelete(r),
      })
      break
  }
}

function copyRule(r: Rule) {
  createForm.name = r.name + ' (copy)'
  createForm.device_id = r.device_id ?? ''
  createForm.logic = r.logic
  createForm.duration = r.duration
  createForm.severity = r.severity
  createForm.notify_channels = [...(r.notify_channels || ['dingtalk'])]
  createForm.conditions = r.conditions?.length ? r.conditions.map(c => ({ _uid: Date.now() + Math.random(), ...c })) : [{ _uid: Date.now() + Math.random(), point: '', operator: '>', threshold: 0 }]
  showCreateModal.value = true
  // 加载设备信息用于回显设备名 + 提供测点列表
  if (createForm.device_id) {
    loadDeviceForDisplay(createForm.device_id)
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
  try {
    await testFormRef.value?.validate()
  } catch {
    return
  }
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
    message.error(extractError(e, t('ruleList.testFailed')))
  } finally {
    testing.value = false
  }
}

async function loadUsers() {
  try {
    // FIXED-Severe(S-4): size 1000 降为 200
    const data = await userApi.list({ page: 1, size: 200 })
    allUsers.value = data?.data ?? []
  } catch (e) {
    console.error('Failed to load users:', e)
  }
}

function openShare(r: Rule) {
  shareForm.rule_id = r.rule_id
  shareForm.shared_with_user_id = ''
  shareForm.permission_level = 'read'
  showShareModal.value = true
}

async function handleShare() {
  // [AUDIT-FIX] 严重级-分享操作缺少前端权限校验，viewer 可通过下拉菜单触发分享请求。
  // 与 handleCreate/handleEdit/doDelete 等写操作保持一致的权限检查。
  if (!auth.isOperator) {
    message.warning(t('common.permissionDenied'))
    return
  }
  try {
    await shareFormRef.value?.validate()
  } catch {
    return
  }
  if (!shareForm.shared_with_user_id) {
    message.warning(t('resourceShare.selectUser'))
    return
  }
  sharing.value = true
  try {
    await resourceShareApi.share({
      resource_type: 'rule',
      resource_id: shareForm.rule_id,
      shared_with_user_id: shareForm.shared_with_user_id,
      permission_level: shareForm.permission_level as 'read' | 'write',
    })
    message.success(t('resourceShare.shareSuccess'))
    showShareModal.value = false
  } catch (e: any) {
    message.error(extractError(e, t('resourceShare.shareFailed')))
  } finally {
    sharing.value = false
  }
}

// [AUDIT-FIX] 建议级-移除 openTransfer 死代码（已定义但未被任何地方调用）

// SEC-FIX-RULE-VERSION: 版本历史
const showVersionDrawer = ref(false)
const versionRuleName = ref('')
const versionRuleId = ref('')
const versionList = ref<any[]>([])
const versionLoading = ref(false)
const versionColumns = computed(() => [
  { title: t('rule.version'), key: 'version', width: 70 },
  { title: t('rule.changeSummary'), key: 'change_summary', width: 200, ellipsis: { tooltip: true } },
  { title: t('rule.operator'), key: 'created_by', width: 100 },
  { title: t('common.time'), key: 'created_at', width: 180 },
  {
    title: t('common.actions'), key: 'action', width: 120,
    render: (row: any) => {
      // 仅 admin 可见回滚按钮（auth.role 在 useAuthStore 中维护）
      if (auth.role !== 'admin') return null
      return h(NPopconfirm, {
        onPositiveClick: () => handleRollback(row.version),
      }, {
        trigger: () => h(NButton, { text: true, type: 'warning', size: 'small' }, { default: () => t('rule.rollbackToVersion') }),
        default: () => t('rule.rollbackConfirm', { version: row.version }),
      })
    },
  },
])

async function openVersionDrawer(r: Rule) {
  versionRuleId.value = r.rule_id
  versionRuleName.value = r.name
  showVersionDrawer.value = true
  versionLoading.value = true
  try {
    const data = await ruleApi.listVersions(r.rule_id)
    versionList.value = data || []
  } catch (e: any) {
    message.error(extractError(e, t('rule.loadVersionFailed')))
    versionList.value = []
  } finally {
    versionLoading.value = false
  }
}

async function handleRollback(version: number) {
  // [AUDIT-FIX] 一般级-防御性权限校验：渲染层已隐藏按钮，但函数本身缺少校验，
  // 非管理员可通过 devtools 调用。违反纵深防御原则。
  if (auth.role !== 'admin') {
    message.warning(t('common.permissionDenied'))
    return
  }
  try {
    await ruleApi.rollbackVersion(versionRuleId.value, version)
    message.success(t('rule.rollbackSuccess'))
    showVersionDrawer.value = false
    await fetchRules()
  } catch (e: any) {
    message.error(extractError(e, t('rule.rollbackFailed')))
  }
}

// 修复4: 版本对比
const compareFrom = ref<number | null>(null)
const compareTo = ref<number | null>(null)
const showCompareModal = ref(false)
const compareData = ref<any[]>([])
const compareLoading = ref(false)
const versionSelectOptions = computed(() => versionList.value.map((v: any) => ({ label: `v${v.version}`, value: v.version })))
const compareColumns = computed(() => [
  { title: t('ruleList.compareField'), key: 'field', width: 140 },
  { title: t('ruleList.compareFrom'), key: 'from', render: (r: any) => h('pre', { style: 'margin:0;font-size:12px' }, JSON.stringify(r.from, null, 2)) },
  { title: t('ruleList.compareTo'), key: 'to', render: (r: any) => h('pre', { style: 'margin:0;font-size:12px' }, JSON.stringify(r.to, null, 2)) },
])
async function handleCompareVersions() {
  if (!compareFrom.value || !compareTo.value) return
  compareLoading.value = true
  try {
    const fromData = await ruleApi.getVersion(versionRuleId.value, compareFrom.value)
    const toData = await ruleApi.getVersion(versionRuleId.value, compareTo.value)
    const fields = Array.from(new Set([...Object.keys(fromData || {}), ...Object.keys(toData || {})]))
    compareData.value = fields.map(f => ({
      field: f,
      from: (fromData as any)?.[f],
      to: (toData as any)?.[f],
    }))
    showCompareModal.value = true
  } catch (e: any) {
    message.error(extractError(e, t('common.failed')))
  } finally {
    compareLoading.value = false
  }
}

// 修复5: 规则导入/导出
const exporting = ref(false)
const importInputRef = ref<HTMLInputElement | null>(null)
async function handleExportRules() {
  if (!auth.isOperator) { message.warning(t('common.permissionDenied')); return }
  exporting.value = true
  try {
    const blob = new Blob([JSON.stringify(rules.value, null, 2)], { type: 'application/json;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `rules_export_${new Date().toISOString().slice(0, 10)}.json`
    a.click()
    URL.revokeObjectURL(url)
    message.success(t('ruleList.exportSuccess'))
  } catch (e: any) {
    message.error(extractError(e, t('ruleList.exportFailed')))
  } finally {
    exporting.value = false
  }
}
function triggerImportRules() {
  if (!auth.isOperator) { message.warning(t('common.permissionDenied')); return }
  importInputRef.value?.click()
}
async function handleImportRules(event: Event) {
  const input = event.target as HTMLInputElement
  if (!input.files?.length) return
  const file = input.files[0]
  // [AUDIT-FIX] 严重-S2: 文件上传需校验大小，避免大文件导致浏览器 OOM
  const MAX_IMPORT_SIZE = 10 * 1024 * 1024  // 10MB
  if (file.size > MAX_IMPORT_SIZE) {
    message.error(t('ruleList.importFileTooLarge', { size: '10MB' }))
    input.value = ''
    return
  }
  try {
    const text = await file.text()
    const arr = JSON.parse(text)
    if (!Array.isArray(arr)) { message.error(t('ruleList.importInvalid')); return }
    let ok = 0
    for (const r of arr) {
      try {
        const { rule_id, version, created_at, created_by, ...rest } = r
        await ruleApi.create(rest as any)
        ok++
      } catch { /* skip individual failure */ }
    }
    message.success(t('ruleList.importSuccess', { count: ok }))
    await fetchRules()
  } catch (e: any) {
    message.error(extractError(e, t('ruleList.importInvalid')))
  } finally {
    input.value = ''
  }
}

// 修复6: 执行历史抽屉
const showHistoryDrawer = ref(false)
const historyRuleName = ref('')
const historyRuleId = ref('')
const historyList = ref<any[]>([])
const historyLoading = ref(false)
const historyColumns = computed(() => [
  { title: t('common.time'), key: 'fired_at', width: 180 },
  { title: t('ruleList.severityCol'), key: 'severity', width: 80 },
  { title: t('ruleList.statusCol'), key: 'status', width: 100 },
  { title: t('common.message'), key: 'message', ellipsis: { tooltip: true } },
])
async function openHistoryDrawer(r: Rule) {
  historyRuleId.value = r.rule_id
  historyRuleName.value = r.name
  showHistoryDrawer.value = true
  historyLoading.value = true
  try {
    const data = await alarmApi.getHistory(r.rule_id, 7)
    historyList.value = data || []
  } catch (e: any) {
    historyList.value = []
  } finally {
    historyLoading.value = false
  }
}

onMounted(() => { fetchRules(); loadUsers() })

// FIXED: 搜索输入防抖（300ms），避免每次按键触发 API 请求造成后端压力与列表闪烁
let _searchTimer: ReturnType<typeof setTimeout> | null = null
function _triggerSearch() {
  pagination.page = 1
  fetchRules()
}
function onSearchInput() {
  if (_searchTimer) clearTimeout(_searchTimer)
  _searchTimer = setTimeout(() => {
    _searchTimer = null
    _triggerSearch()
  }, 300)
}
function onSearchEnter() {
  if (_searchTimer) { clearTimeout(_searchTimer); _searchTimer = null }
  _triggerSearch()
}
onUnmounted(() => { if (_searchTimer) clearTimeout(_searchTimer) })
</script>

<style scoped>
.condition-block {
  padding: 10px 12px;
  background: var(--n-color-embedded, #f9fafb);
  border-radius: 8px;
  margin-bottom: 8px;
}
.condition-connector {
  text-align: center;
  margin-bottom: 6px;
}
.condition-fields {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}
/* 修复26: 可视化条件表达式预览样式 */
.condition-preview {
  padding: 8px 12px;
  background: var(--n-color-target, #f0f9ff);
  border: 1px dashed var(--n-border-color, #d0e8ff);
  border-radius: 6px;
  margin-bottom: 10px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.condition-expression {
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 13px;
  color: var(--n-text-color, #333);
  background: transparent;
  padding: 2px 0;
  word-break: break-all;
}
@media (max-width: 640px) {
  .condition-fields {
    flex-direction: column;
    align-items: stretch;
  }
  .condition-fields .n-select,
  .condition-fields .n-input-number {
    width: 100% !important;
    min-width: 0 !important;
    flex: none !important;
  }
}
</style>
