<template>
  <n-spin :show="pageLoading" :description="t('auditLog.loading')">
  <div class="audit-page">
    <n-card :title="t('auditLog.title')">
      <template #header-extra>
        <n-space>
          <n-select v-model:value="filterAction" :options="actionOptions" clearable :placeholder="t('auditLog.operationType')" style="width: 160px" />
          <n-date-picker v-model:value="timeRange" type="datetimerange" clearable />
          <n-button type="primary" @click="loadLogs">{{ t('auditLog.query') }}</n-button>
          <n-button @click="exportCSV">{{ t('auditLog.exportCsv') }}</n-button>
          <n-button @click="verifyIntegrity">{{ t('auditLog.integrity') }}</n-button>
          <n-button type="warning" @click="showCleanupModal = true">{{ t('auditLog.cleanup') }}</n-button>
        </n-space>
      </template>
      <n-data-table :columns="columns" :data="logs" :pagination="pagination" :loading="loading" />
    </n-card>

    <n-modal v-model:show="showCleanupModal" preset="dialog" :title="t('auditLog.cleanupTitle')" :positive-text="t('auditLog.confirmCleanup')" :negative-text="t('common.cancel')" @positive-click="doCleanup">
      <n-form-item :label="t('auditLog.retainDays')">
        <n-input-number v-model:value="retentionDays" :min="1" :max="3650" />
      </n-form-item>
      <p style="color: #999; font-size: 13px">{{ t('auditLog.cleanupHint') }}</p>
    </n-modal>
  </div>
  </n-spin>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, h } from 'vue'
import { NCard, NButton, NSpace, NSelect, NDatePicker, NDataTable, NTag, NModal, NFormItem, NInputNumber, NSpin, useMessage, useDialog } from 'naive-ui'
import { auditApi } from '@/api'
import { auditStatusLabel, auditActionLabel } from '@/utils/enumLabels'
// FIXED: 原问题-添加i18n支持
import { t } from '@/i18n'

const message = useMessage()
const dialog = useDialog()
const logs = ref<any[]>([])
const loading = ref(false)
const pageLoading = ref(true)
const filterAction = ref(null)
const timeRange = ref<[number, number] | null>(null)
const pagination = reactive({ page: 1, pageSize: 20, itemCount: 0, onChange: (page: number) => { pagination.page = page; loadLogs() } })
const showCleanupModal = ref(false)
const retentionDays = ref(90)

const actionOptions = [
  { label: t('auditAction.login'), value: 'login' },
  { label: t('auditAction.logout'), value: 'logout' },
  { label: t('auditAction.login_failed'), value: 'login_failed' },
  { label: t('auditAction.device_create'), value: 'device_create' },
  { label: t('auditAction.device_update'), value: 'device_update' },
  { label: t('auditAction.device_delete'), value: 'device_delete' },
  { label: t('auditAction.rule_create'), value: 'rule_create' },
  { label: t('auditAction.rule_update'), value: 'rule_update' },
  { label: t('auditAction.rule_delete'), value: 'rule_delete' },
  { label: t('auditAction.user_create'), value: 'user_create' },
  { label: t('auditAction.user_update'), value: 'user_update' },
  { label: t('auditAction.user_delete'), value: 'user_delete' },
  { label: t('auditAction.backup_create'), value: 'backup_create' },
  { label: t('auditAction.backup_restore'), value: 'backup_restore' },
]

const columns = [
  { title: t('auditLog.time'), key: 'created_at', width: 180 },
  { title: t('auditLog.user'), key: 'username', width: 100 },
  { title: t('auditLog.operation'), key: 'action', width: 120, render: (row: any) => h(NTag, { size: 'small', type: row.status === 'success' ? 'success' : 'error' }, () => auditActionLabel[row.action] || row.action) },
  { title: t('auditLog.resourceType'), key: 'resource_type', width: 100 },
  { title: t('auditLog.resourceId'), key: 'resource_id', width: 120 },
  { title: t('auditLog.ipAddress'), key: 'ip_address', width: 130 },
  { title: t('auditLog.statusCol'), key: 'status', width: 80, render: (row: any) => h(NTag, { size: 'small', type: row.status === 'success' ? 'success' : 'warning' }, () => auditStatusLabel[row.status] || row.status) },
  { title: t('auditLog.detail'), key: 'details', ellipsis: { tooltip: true } },
]

async function loadLogs() {
  loading.value = true
  try {
    const params: any = { page: pagination.page, size: pagination.pageSize }
    if (filterAction.value) params.action = filterAction.value
    if (timeRange.value) {
      params.start_time = new Date(timeRange.value[0]).toISOString()
      params.end_time = new Date(timeRange.value[1]).toISOString()
    }
    const data = await auditApi.list(params)
    logs.value = data?.logs ?? []
    pagination.itemCount = data?.total ?? 0
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || t('auditLog.loadFailed'))
  } finally { loading.value = false; pageLoading.value = false }
}

async function exportCSV() {
  try {
    const params: any = {}
    if (filterAction.value) params.action = filterAction.value
    if (timeRange.value) {
      params.start_time = new Date(timeRange.value[0]).toISOString()
      params.end_time = new Date(timeRange.value[1]).toISOString()
    }
    const data = await auditApi.exportCsv(params)
    if (data?.content) {
      const blob = new Blob([data.content], { type: 'text/csv' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url; a.download = 'audit_logs.csv'; a.click()
      URL.revokeObjectURL(url)
    }
  } catch (e: any) { message.error(e?.response?.data?.detail || e?.message || t('auditLog.exportFailed')) }
}

async function verifyIntegrity() {
  try {
    const data = await auditApi.integrity()
    if (data) {
      const valid = data.valid ? t('auditLog.passMark') : t('auditLog.failMark')
      const broken = data.broken_at?.length ? data.broken_at.join(', ') : t('auditLog.none')
      dialog.info({
        title: t('auditLog.integrityTitle'),
        content: `${valid}\n${t('auditLog.detail')}: ${data.total}\n${broken}`,
        positiveText: t('common.confirm'),
      })
    }
  } catch (e: any) { message.error(e?.response?.data?.detail || e?.message || t('auditLog.integrityFailed')) }
}

async function doCleanup() {
  try {
    const data = await auditApi.cleanup(retentionDays.value)
    message.success(t('auditLog.cleanupResult', { count: data?.deleted ?? 0 }))
    await loadLogs()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || t('auditLog.cleanupFailed'))
    return false
  }
}

onMounted(() => { loadLogs() })
</script>

<style scoped>
.audit-page { padding: 16px; }
</style>
