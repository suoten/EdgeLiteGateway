<template>
  <div class="audit-page">
    <n-card title="审计日志">
      <template #header-extra>
        <n-space>
          <n-select v-model:value="filterAction" :options="actionOptions" clearable placeholder="操作类型" style="width: 160px" />
          <n-date-picker v-model:value="timeRange" type="datetimerange" clearable />
          <n-button type="primary" @click="loadLogs">查询</n-button>
          <n-button @click="exportCSV">导出CSV</n-button>
          <n-button @click="verifyIntegrity">完整性校验</n-button>
          <n-button type="warning" @click="showCleanupModal = true">清理日志</n-button>
        </n-space>
      </template>
      <n-data-table :columns="columns" :data="logs" :pagination="pagination" :loading="loading" />
    </n-card>

    <n-modal v-model:show="showCleanupModal" preset="dialog" title="清理过期审计日志" positive-text="确认清理" negative-text="取消" @positive-click="doCleanup">
      <n-form-item label="保留天数">
        <n-input-number v-model:value="retentionDays" :min="1" :max="3650" />
      </n-form-item>
      <p style="color: #999; font-size: 13px">将删除超过保留天数的审计日志记录，此操作不可恢复。</p>
    </n-modal>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, h } from 'vue'
import { NCard, NButton, NSpace, NSelect, NDatePicker, NDataTable, NTag, NModal, NFormItem, NInputNumber, useMessage, useDialog } from 'naive-ui'
import { auditApi } from '@/api'

const message = useMessage()
const dialog = useDialog()
const logs = ref<any[]>([])
const loading = ref(false)
const filterAction = ref(null)
const timeRange = ref<[number, number] | null>(null)
const pagination = reactive({ page: 1, pageSize: 20, itemCount: 0, onChange: (page: number) => { pagination.page = page; loadLogs() } })
const showCleanupModal = ref(false)
const retentionDays = ref(90)

const actionOptions = [
  { label: '登录', value: 'login' },
  { label: '登出', value: 'logout' },
  { label: '登录失败', value: 'login_failed' },
  { label: '设备创建', value: 'device_create' },
  { label: '设备更新', value: 'device_update' },
  { label: '设备删除', value: 'device_delete' },
  { label: '规则创建', value: 'rule_create' },
  { label: '规则更新', value: 'rule_update' },
  { label: '规则删除', value: 'rule_delete' },
  { label: '用户创建', value: 'user_create' },
  { label: '用户更新', value: 'user_update' },
  { label: '用户删除', value: 'user_delete' },
  { label: '备份创建', value: 'backup_create' },
  { label: '备份恢复', value: 'backup_restore' },
]

const columns = [
  { title: '时间', key: 'created_at', width: 180 },
  { title: '用户', key: 'username', width: 100 },
  { title: '操作', key: 'action', width: 120, render: (row: any) => h(NTag, { size: 'small', type: row.status === 'success' ? 'success' : 'error' }, () => row.action) },
  { title: '资源类型', key: 'resource_type', width: 100 },
  { title: '资源ID', key: 'resource_id', width: 120 },
  { title: 'IP地址', key: 'ip_address', width: 130 },
  { title: '状态', key: 'status', width: 80, render: (row: any) => h(NTag, { size: 'small', type: row.status === 'success' ? 'success' : 'warning' }, () => row.status) },
  { title: '详情', key: 'details', ellipsis: { tooltip: true } },
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
    logs.value = data?.logs || []
    pagination.itemCount = data?.total || 0
  } catch (e) {
    message.error('加载审计日志失败')
  } finally { loading.value = false }
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
  } catch (e) { message.error('导出失败') }
}

async function verifyIntegrity() {
  try {
    const data = await auditApi.integrity()
    if (data) {
      const valid = data.valid ? '通过 ✓' : '未通过 ✗'
      const broken = data.broken_at?.length ? data.broken_at.join(', ') : '无'
      dialog.info({
        title: '完整性校验结果',
        content: `校验: ${valid}\n总记录: ${data.total}\n断裂位置: ${broken}`,
        positiveText: '确定',
      })
    }
  } catch (e) { message.error('校验失败') }
}

async function doCleanup() {
  try {
    const data = await auditApi.cleanup(retentionDays.value)
    message.success(`已清理 ${data?.deleted ?? 0} 条过期日志`)
    await loadLogs()
  } catch (e) {
    message.error('清理失败')
    return false
  }
}

onMounted(() => { loadLogs() })
</script>

<style scoped>
.audit-page { padding: 16px; }
</style>
