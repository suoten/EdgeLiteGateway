<template>
  <n-space vertical :size="16">
    <n-grid :cols="2" :x-gap="12">
      <n-gi>
        <n-card title="系统状态" size="small">
          <template #header-extra>
            <n-button text @click="fetchStatus">刷新</n-button>
          </template>
          <n-descriptions label-placement="left" :column="1" bordered>
            <n-descriptions-item label="CPU使用率">
              <n-progress type="line" :percentage="status?.cpu_percent ?? 0" :indicator-placement="'inside'" />
            </n-descriptions-item>
            <n-descriptions-item label="内存使用率">
              <n-progress type="line" :percentage="status?.memory_percent ?? 0" :indicator-placement="'inside'" />
            </n-descriptions-item>
            <n-descriptions-item label="磁盘使用率">
              <n-progress type="line" :percentage="status?.disk_percent ?? 0" :indicator-placement="'inside'" />
            </n-descriptions-item>
            <n-descriptions-item label="运行时长">{{ formatUptime(status?.uptime ?? 0) }}</n-descriptions-item>
            <n-descriptions-item label="版本">{{ status?.version ?? '-' }}</n-descriptions-item>
          </n-descriptions>
        </n-card>
      </n-gi>
      <n-gi>
        <n-card title="业务统计" size="small">
          <n-descriptions label-placement="left" :column="1" bordered>
            <n-descriptions-item label="设备总数">{{ status?.device_total ?? '-' }}</n-descriptions-item>
            <n-descriptions-item label="在线设备">{{ status?.device_online ?? '-' }}</n-descriptions-item>
            <n-descriptions-item label="规则总数">{{ status?.rule_total ?? '-' }}</n-descriptions-item>
            <n-descriptions-item label="活跃告警">{{ status?.alarm_firing ?? '-' }}</n-descriptions-item>
            <n-descriptions-item label="采集任务">{{ status?.collect_task_count ?? '-' }}</n-descriptions-item>
          </n-descriptions>
        </n-card>
      </n-gi>
    </n-grid>

    <n-card title="数据备份" size="small">
      <n-space>
        <n-button type="primary" :loading="backupLoading" @click="handleBackup">创建备份</n-button>
      </n-space>
      <n-data-table :columns="backupColumns" :data="backups" :bordered="false" size="small" style="margin-top: 12px" />
    </n-card>
  </n-space>
</template>

<script setup lang="ts">
import { ref, onMounted, h } from 'vue'
import { NButton, useMessage } from 'naive-ui'
import { systemApi, type SystemStatus } from '@/api'

const message = useMessage()
const status = ref<SystemStatus | null>(null)
const backups = ref<any[]>([])
const backupLoading = ref(false)

const backupColumns = [
  { title: '备份ID', key: 'backup_id' },
  { title: '文件', key: 'file' },
  { title: '大小', key: 'size', render: (r: any) => r.size ? `${(r.size / 1024).toFixed(1)} KB` : '-' },
]

function formatUptime(seconds: number) {
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = seconds % 60
  return `${h}h ${m}m ${s}s`
}

async function fetchStatus() {
  try {
    status.value = await systemApi.getStatus()
  } catch {}
}

async function fetchBackups() {
  try {
    backups.value = await systemApi.listBackups()
  } catch {}
}

async function handleBackup() {
  backupLoading.value = true
  try {
    await systemApi.createBackup()
    message.success('备份创建成功')
    fetchBackups()
  } catch (e: any) {
    message.error(e?.message || '备份失败')
  } finally {
    backupLoading.value = false
  }
}

onMounted(() => {
  fetchStatus()
  fetchBackups()
})
</script>
