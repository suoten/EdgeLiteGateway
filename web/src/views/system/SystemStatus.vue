<template>
  <n-space vertical :size="16">
    <n-grid :cols="2" :x-gap="12">
      <n-gi>
        <n-card title="系统状态" size="small">
          <template #header-extra>
            <n-space>
              <n-switch v-model:value="autoRefresh" size="small">
                <template #checked>自动</template>
                <template #unchecked>手动</template>
              </n-switch>
              <n-button text @click="fetchStatus">刷新</n-button>
            </n-space>
          </template>
          <n-descriptions label-placement="left" :column="1" bordered>
            <n-descriptions-item label="CPU使用率">
              <n-progress type="line" :percentage="status?.cpu_percent ?? 0" :indicator-placement="'inside'" :color="cpuColor" />
            </n-descriptions-item>
            <n-descriptions-item label="内存使用率">
              <n-progress type="line" :percentage="status?.memory_percent ?? 0" :indicator-placement="'inside'" :color="memColor" />
            </n-descriptions-item>
            <n-descriptions-item label="磁盘使用率">
              <n-progress type="line" :percentage="status?.disk_percent ?? 0" :indicator-placement="'inside'" :color="diskColor" />
            </n-descriptions-item>
            <n-descriptions-item label="运行时长">{{ formatUptime(status?.uptime ?? 0) }}</n-descriptions-item>
            <n-descriptions-item label="版本">{{ status?.version ?? '-' }}</n-descriptions-item>
          </n-descriptions>
        </n-card>
      </n-gi>
      <n-gi>
        <n-card title="业务统计" size="small">
          <n-descriptions label-placement="left" :column="1" bordered>
            <n-descriptions-item label="设备总数">
              <n-text>{{ status?.device_total ?? '-' }}</n-text>
              <n-text depth="3" style="margin-left: 8px; font-size: 12px">（在线 {{ status?.device_online ?? 0 }}）</n-text>
            </n-descriptions-item>
            <n-descriptions-item label="规则总数">{{ status?.rule_total ?? '-' }}</n-descriptions-item>
            <n-descriptions-item label="活跃告警">
              <n-text :type="(status?.alarm_firing ?? 0) > 0 ? 'error' : undefined">{{ status?.alarm_firing ?? '-' }}</n-text>
            </n-descriptions-item>
            <n-descriptions-item label="采集任务">{{ status?.collect_task_count ?? '-' }}</n-descriptions-item>
          </n-descriptions>
        </n-card>
      </n-gi>
    </n-grid>

    <n-card title="数据备份" size="small">
      <template #header-extra>
        <n-button type="primary" size="small" :loading="backupLoading" @click="handleBackup">创建备份</n-button>
      </template>
      <n-data-table :columns="backupColumns" :data="backups" :bordered="false" size="small" />
    </n-card>
  </n-space>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, h } from 'vue'
import { NButton, useMessage, useDialog } from 'naive-ui'
import { systemApi, type SystemStatus } from '@/api'

const message = useMessage()
const dialog = useDialog()
const status = ref<SystemStatus | null>(null)
const backups = ref<any[]>([])
const backupLoading = ref(false)
const autoRefresh = ref(true)
let timer: number | null = null

const cpuColor = computed(() => {
  const p = status.value?.cpu_percent ?? 0
  return p > 80 ? '#f56c6c' : p > 60 ? '#e6a23c' : '#67c23a'
})
const memColor = computed(() => {
  const p = status.value?.memory_percent ?? 0
  return p > 90 ? '#f56c6c' : p > 70 ? '#e6a23c' : '#67c23a'
})
const diskColor = computed(() => {
  const p = status.value?.disk_percent ?? 0
  return p > 90 ? '#f56c6c' : p > 80 ? '#e6a23c' : '#67c23a'
})

const backupColumns = [
  { title: '备份ID', key: 'backup_id' },
  { title: '文件', key: 'file' },
  { title: '大小', key: 'size', render: (r: any) => r.size ? `${(r.size / 1024).toFixed(1)} KB` : '-' },
  { title: '时间', key: 'created_at' },
  {
    title: '操作', key: 'actions', width: 120,
    render: (r: any) => h(NButton, { text: true, type: 'primary', size: 'small', onClick: () => handleRestore(r) }, { default: () => '恢复' }),
  },
]

function formatUptime(seconds: number) {
  const d = Math.floor(seconds / 86400)
  const h = Math.floor((seconds % 86400) / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = seconds % 60
  if (d > 0) return `${d}天 ${h}时 ${m}分`
  return `${h}时 ${m}分 ${s}秒`
}

async function fetchStatus() {
  try { status.value = await systemApi.getStatus() } catch (e) { console.warn('获取系统状态失败:', e) }
}

async function fetchBackups() {
  try { backups.value = await systemApi.listBackups() } catch (e) { console.warn('获取备份列表失败:', e) }
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

function handleRestore(r: any) {
  dialog.warning({
    title: '确认恢复',
    content: `确定恢复备份 "${r.backup_id}"？恢复后需要重启服务才能生效。`,
    positiveText: '恢复',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await systemApi.restore(r.backup_id)
        message.success('备份恢复成功，请重启服务')
      } catch (e: any) {
        message.error(e?.message || '恢复失败')
      }
    },
  })
}

onMounted(() => {
  fetchStatus(); fetchBackups()
  timer = window.setInterval(() => { if (autoRefresh.value) fetchStatus() }, 5000)
})
onUnmounted(() => { if (timer) clearInterval(timer) })
</script>
