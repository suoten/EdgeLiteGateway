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
        </n-space>
      </template>
      <n-data-table :columns="columns" :data="logs" :pagination="pagination" :loading="loading" />
    </n-card>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, h } from 'vue'
import { NCard, NButton, NSpace, NSelect, NDatePicker, NDataTable, NTag } from 'naive-ui'
import http from '../../api/http'

const logs = ref<any[]>([])
const loading = ref(false)
const filterAction = ref(null)
const timeRange = ref<[number, number] | null>(null)
const pagination = ref({ page: 1, pageSize: 20, itemCount: 0 })

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
  { title: '时间', key: 'timestamp', width: 180 },
  { title: '用户', key: 'username', width: 100 },
  { title: '操作', key: 'action', width: 120, render: (row: any) => h(NTag, { size: 'small', type: row.status === 'success' ? 'success' : 'error' }, { default: () => row.action }) },
  { title: '资源类型', key: 'resource_type', width: 100 },
  { title: '资源ID', key: 'resource_id', width: 120 },
  { title: 'IP地址', key: 'ip_address', width: 130 },
  { title: '状态', key: 'status', width: 80, render: (row: any) => h(NTag, { size: 'small', type: row.status === 'success' ? 'success' : 'warning' }, { default: () => row.status }) },
  { title: '详情', key: 'details', ellipsis: { tooltip: true } },
]

async function loadLogs() {
  loading.value = true
  try {
    const params: any = { page: pagination.value.page, size: pagination.value.pageSize }
    if (filterAction.value) params.action = filterAction.value
    if (timeRange.value) {
      params.start_time = new Date(timeRange.value[0]).toISOString()
      params.end_time = new Date(timeRange.value[1]).toISOString()
    }
    const res = await http.get('/api/v1/audit/logs', { params })
    if (res.data?.data) {
      logs.value = res.data.data.logs || []
      pagination.value.itemCount = res.data.data.total || 0
    }
  } catch (e) {
    console.error('加载审计日志失败:', e)
  } finally {
    loading.value = false
  }
}

async function exportCSV() {
  try {
    const params: any = {}
    if (filterAction.value) params.action = filterAction.value
    if (timeRange.value) {
      params.start_time = new Date(timeRange.value[0]).toISOString()
      params.end_time = new Date(timeRange.value[1]).toISOString()
    }
    const res = await http.get('/api/v1/audit/export/csv', { params })
    if (res.data?.data?.content) {
      const blob = new Blob([res.data.data.content], { type: 'text/csv' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url; a.download = 'audit_logs.csv'; a.click()
      URL.revokeObjectURL(url)
    }
  } catch (e) {
    console.error('导出失败:', e)
  }
}

async function verifyIntegrity() {
  try {
    const res = await http.get('/api/v1/audit/integrity')
    if (res.data?.data) {
      const { valid, total, broken_at } = res.data.data
      alert(`完整性校验: ${valid ? '通过' : '未通过'}\n总记录: ${total}\n断裂位置: ${broken_at.length ? broken_at.join(', ') : '无'}`)
    }
  } catch (e) {
    console.error('校验失败:', e)
  }
}

onMounted(() => { loadLogs() })
</script>

<style scoped>
.audit-page { padding: 16px; }
</style>
