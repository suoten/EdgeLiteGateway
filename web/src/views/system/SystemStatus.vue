<template>
  <n-spin :show="pageLoading" :description="t('system.loadingStatus')">
  <n-space vertical :size="16">
    <n-tabs type="line" animated>
      <n-tab-pane name="status" :tab="t('system.systemStatus')">
    <n-grid :cols="2" :x-gap="12">
      <n-gi>
        <n-card :title="t('system.systemStatus')" size="small">
          <template #header-extra>
            <n-space>
              <n-switch v-model:value="autoRefresh" size="small">
                <template #checked>{{ t('system.auto') }}</template>
                <template #unchecked>{{ t('system.manual') }}</template>
              </n-switch>
              <n-button text @click="fetchStatus">{{ t('system.refresh') }}</n-button>
            </n-space>
          </template>
          <n-descriptions label-placement="left" :column="1" bordered>
            <n-descriptions-item :label="t('system.cpuUsage')">
              <n-progress type="line" :percentage="status?.cpu_percent ?? 0" :indicator-placement="'inside'" :color="cpuColor" />
            </n-descriptions-item>
            <n-descriptions-item :label="t('system.memoryUsage')">
              <n-progress type="line" :percentage="status?.memory_percent ?? 0" :indicator-placement="'inside'" :color="memColor" />
            </n-descriptions-item>
            <n-descriptions-item :label="t('system.diskUsage')">
              <n-progress type="line" :percentage="status?.disk_percent ?? 0" :indicator-placement="'inside'" :color="diskColor" />
            </n-descriptions-item>
            <n-descriptions-item :label="t('system.uptime')">{{ formatUptime(status?.uptime ?? 0) }}</n-descriptions-item>
            <n-descriptions-item :label="t('system.version')">{{ status?.version ?? '-' }}</n-descriptions-item>
          </n-descriptions>
        </n-card>
      </n-gi>
      <n-gi>
        <n-card :title="t('system.businessStats')" size="small">
          <n-descriptions label-placement="left" :column="1" bordered>
            <n-descriptions-item :label="t('system.deviceTotal')">
              <n-text>{{ status?.device_total ?? '-' }}</n-text>
              <n-text depth="3" style="margin-left: 8px; font-size: 12px">({{ t('system.onlineCount', { count: status?.device_online ?? 0 }) }})</n-text>
            </n-descriptions-item>
            <n-descriptions-item :label="t('system.ruleTotal')">{{ status?.rule_total ?? '-' }}</n-descriptions-item>
            <n-descriptions-item :label="t('system.activeAlarm')">
              <n-text :type="(status?.alarm_firing ?? 0) > 0 ? 'error' : undefined">{{ status?.alarm_firing ?? '-' }}</n-text>
            </n-descriptions-item>
            <n-descriptions-item :label="t('system.collectTask')">{{ status?.collect_task_count ?? '-' }}</n-descriptions-item>
          </n-descriptions>
        </n-card>
      </n-gi>
    </n-grid>

    <n-card :title="t('system.dataBackup')" size="small">
      <template #header-extra>
        <n-button type="primary" size="small" :loading="backupLoading" @click="handleBackup">{{ t('system.createBackup') }}</n-button>
      </template>
      <n-data-table :columns="backupColumns" :data="backups" :bordered="false" size="small" />
    </n-card>
      </n-tab-pane>
      <n-tab-pane name="cascade" :tab="t('cascade.title')">
        <n-space vertical :size="16">
          <n-card :title="t('cascade.topology')" size="small">
            <template #header-extra>
              <n-button text size="small" @click="fetchTopology">{{ t('common.refresh') }}</n-button>
            </template>
            <n-descriptions label-placement="left" :column="2" bordered>
              <n-descriptions-item :label="t('cascade.role')">{{ topology.status || 'standalone' }}</n-descriptions-item>
              <n-descriptions-item :label="'ID'">{{ topology.local_id || '-' }}</n-descriptions-item>
              <n-descriptions-item :label="t('cascade.parent')">{{ topology.parent_id || '-' }}</n-descriptions-item>
              <n-descriptions-item :label="t('cascade.child')">{{ (topology.children || []).join(', ') || '-' }}</n-descriptions-item>
            </n-descriptions>
          </n-card>
          <n-card :title="t('cascade.neighbors')" size="small">
            <n-data-table v-if="(topology.peers || []).length > 0" :columns="neighborColumns" :data="topology.peers || []" :bordered="false" size="small" />
            <n-empty v-else :description="t('cascade.noNeighbors')" />
          </n-card>
        </n-space>
      </n-tab-pane>
    </n-tabs>
  </n-space>
  </n-spin>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, h } from 'vue'
import { NButton, NTag, useMessage, useDialog } from 'naive-ui'
import { t } from '@/i18n'  // FIXED: 原问题-#注释导致编译失败，改为//注释
import { systemApi, type SystemStatus } from '@/api'
const message = useMessage()
const dialog = useDialog()
const status = ref<SystemStatus | null>(null)
const backups = ref<any[]>([])
const backupLoading = ref(false)
const autoRefresh = ref(true)
const pageLoading = ref(true)
const topology = ref<any>({ status: 'standalone', local_id: '', parent_id: null, children: [], peers: [] })
let timer: number | null = null
// FIXED: 原问题-定时器持续失败时每5秒弹错误消息刷屏，加错误退避
let consecutiveErrors = 0

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
  { title: t('system.backupId'), key: 'backup_id' },
  { title: t('system.file'), key: 'file' },
  { title: t('system.size'), key: 'size', render: (r: any) => r.size ? `${(r.size / 1024).toFixed(1)} KB` : '-' },
  { title: t('system.time'), key: 'created_at' },
  {
    title: t('system.actions'), key: 'actions', width: 120,
    render: (r: any) => h(NButton, { text: true, type: 'primary', size: 'small', onClick: () => handleRestore(r) }, { default: () => t('system.restore') }),
  },
]

function formatUptime(seconds: number) {
  const d = Math.floor(seconds / 86400)
  const hr = Math.floor((seconds % 86400) / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = seconds % 60
  if (d > 0) return `${d}${t('system.days')} ${hr}${t('system.hours')} ${m}${t('system.minutes')}`
  return `${hr}${t('system.hours')} ${m}${t('system.minutes')} ${s}${t('system.seconds')}`
}

async function fetchStatus() {
  try {
    status.value = await systemApi.getStatus()
    consecutiveErrors = 0
  } catch (e: any) {
    consecutiveErrors++
    // FIXED: 原问题-定时器持续失败时每5秒弹错误消息刷屏，连续3次以上不再弹
    if (consecutiveErrors <= 3) {
      message.error(e?.response?.data?.detail || e?.message || t('system.fetchStatusFailed'))
    }
  } finally {
    pageLoading.value = false
  }
}

async function fetchBackups() {
  try { backups.value = await systemApi.listBackups() } catch (e: any) { message.error(e?.response?.data?.detail || e?.message || t('system.fetchBackupFailed')) }
}

async function handleBackup() {
  backupLoading.value = true
  try {
    await systemApi.createBackup()
    message.success(t('system.backupSuccess'))  // FIXED: 原问题-中文硬编码，改为i18n
    fetchBackups()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || t('system.backupFailed'))  // FIXED: 原问题-中文硬编码，改为i18n
  } finally {
    backupLoading.value = false
  }
}

function handleRestore(r: any) {
  dialog.warning({
    title: t('system.restoreConfirmTitle'),
    content: t('system.restoreConfirm', { id: r.backup_id }),  // FIXED: 原问题-中文硬编码，改为i18n
    positiveText: t('system.restore'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      try {
        await systemApi.restore(r.backup_id)
        message.success(t('system.restoreSuccess'))  // FIXED: 原问题-中文硬编码，改为i18n
        // FIXED: 原问题-恢复成功后未刷新状态，添加刷新调用
        fetchStatus()
        fetchBackups()
      } catch (e: any) {
        message.error(e?.response?.data?.detail || e?.message || t('system.restoreFailed'))  // FIXED: 原问题-中文硬编码，改为i18n
      }
    },
  })
}

onMounted(() => {
  fetchStatus(); fetchBackups(); fetchTopology()
  timer = window.setInterval(() => { if (autoRefresh.value) fetchStatus() }, 5000)
})
onUnmounted(() => { if (timer) clearInterval(timer) })

const neighborColumns = [
  { title: 'ID', key: 'neighbor_id' },
  { title: 'Host', key: 'host' },
  { title: 'Port', key: 'port' },
  { title: t('cascade.role'), key: 'role', render: (r: any) => h(NTag, { size: 'small' }, { default: () => r.role || 'peer' }) },
]

async function fetchTopology() {
  try {
    topology.value = await systemApi.getCascadeTopology()
  } catch { /* ignore */ }
}
</script>
