<template>
  <n-spin :show="pageLoading" :description="t('common.loading')">
  <n-space vertical :size="16">
    <n-card :title="t('ota.title')" :bordered="false">
      <template #header-extra>
        <n-space>
          <n-button @click="checkUpdate" :loading="checking">{{ t('ota.checkUpdate') }}</n-button>
        </n-space>
      </template>

      <n-descriptions label-placement="left" :column="2" bordered v-if="updateInfo">
        <n-descriptions-item :label="t('ota.currentVersion')">{{ updateInfo.current_version || '-' }}</n-descriptions-item>
        <n-descriptions-item :label="t('ota.latestVersion')">
          <n-tag v-if="updateInfo.has_update" type="warning" size="small">{{ updateInfo.latest_version }}</n-tag>
          <n-tag v-else type="success" size="small">{{ t('otaState.up_to_date') }}</n-tag>
        </n-descriptions-item>
        <n-descriptions-item :label="t('ota.releaseNotes')" :span="2">{{ updateInfo.release_notes || '-' }}</n-descriptions-item>
      </n-descriptions>
      <n-empty v-else :description="t('ota.checkHint')" />

      <n-space style="margin-top: 16px" v-if="updateInfo?.has_update">
        <n-popconfirm @positive-click="applyUpdate">
          <template #trigger>
            <n-button type="error" :loading="applying">{{ t('ota.applyUpdate') }}</n-button>
          </template>
          <div style="max-width: 320px">
            <n-text strong>{{ t('ota.confirmApply') }}</n-text>
            <n-p>{{ t('ota.applyWarning') }}</n-p>
            <n-p v-if="updateInfo?.latest_version">{{ t('ota.targetVersion') }}：{{ updateInfo.latest_version }}</n-p>
          </div>
        </n-popconfirm>
      </n-space>
    </n-card>

    <n-card :title="t('ota.backupVersions')" :bordered="false">
      <template #header-extra>
        <n-button @click="fetchBackups" :loading="fetchingBackups">{{ t('common.refresh') || 'Refresh' }}</n-button>
      </template>
      <n-data-table :columns="backupColumns" :data="backups" :bordered="false" size="small">
        <template #empty>
          <n-empty :description="t('ota.noBackup')" />
        </template>
      </n-data-table>
    </n-card>
  </n-space>
  </n-spin>
</template>

<script setup lang="ts">
import { ref, onMounted, h } from 'vue'
import { NButton, NPopconfirm, NSpin, useMessage, useDialog } from 'naive-ui'
import { otaApi } from '@/api'
// FIXED: 原问题-添加i18n支持
import { t } from '@/i18n'
import { extractError } from '@/utils/errorCodes'

const message = useMessage()
const dialog = useDialog()
const checking = ref(false)
const applying = ref(false)
const rollingBack = ref(false)
const fetchingBackups = ref(false)
const updateInfo = ref<any>(null)
const backups = ref<any[]>([])
const pageLoading = ref(true)

const backupColumns = [
  { title: t('ota.version'), key: 'version', width: 150 },
  { title: t('ota.backupTime'), key: 'created_at', width: 200 },
  { title: t('ota.size'), key: 'size', width: 100 },
  {
    title: t('common.actions'), key: 'actions', width: 100,  // FIXED: 原问题-跨域误用alarmList.actions
    render: (row: any) =>
      h(NPopconfirm as any, { onPositiveClick: () => handleRollback(row.version) }, {
        trigger: () => h(NButton, { text: true, type: 'warning', loading: rollingBack.value }, { default: () => t('ota.rollback') }),
        default: () => t('ota.rollbackConfirm', { version: row.version }),
      }),
  },
]

async function checkUpdate() {
  checking.value = true
  try {
    const data = await otaApi.check()
    updateInfo.value = data
  } catch (e: any) {
    message.error(extractError(e, t('ota.checkFailed')))
  } finally { checking.value = false }
}

// FIXED: 原问题-固定10秒reload，实际重启时间不确定，改为轮询/health端点
const HEALTH_POLL_INTERVAL_MS = 2000
const HEALTH_POLL_MAX_ATTEMPTS = 30

function pollHealthAndReload() {
  let attempts = 0
  const apiBase = import.meta.env.VITE_API_BASE_URL || '/api/v1'
  const timer = setInterval(async () => {
    attempts++
    if (attempts > HEALTH_POLL_MAX_ATTEMPTS) {
      clearInterval(timer)
      console.warn('[OTA] Health check timed out after 60s, forcing reload')
      window.location.reload()
      return
    }
    try {
      const resp = await fetch(`${apiBase}/system/status`, { method: 'GET' })
      if (resp.ok) {
        clearInterval(timer)
        window.location.reload()
      }
    } catch {
      // Server not ready yet, continue polling
    }
  }, HEALTH_POLL_INTERVAL_MS)
}

async function applyUpdate() {
  applying.value = true
  try {
    await otaApi.apply()
    // FIXED: 原问题-OTA升级成功后无系统重启提示
    message.success(t('ota.applySuccess') + t('ota.restartSoon'))  // FIXED: 原问题-硬编码中文，改用i18n
    pollHealthAndReload()
  } catch (e: any) {
    message.error(extractError(e, t('ota.applyFailed')))
  } finally { applying.value = false }
}

async function handleRollback(version: string) {
  rollingBack.value = true
  try {
    await otaApi.rollback(version)
    // FIXED: 原问题-OTA回滚成功后无系统重启提示
    message.success(t('otaUpdate.rollbackSuccess', { version }) + t('ota.restartSoon'))  // FIXED: 原问题-硬编码中文label
    // FIXED: 原问题-固定10秒reload，改为轮询/health端点
    pollHealthAndReload()
  } catch (e: any) {
    message.error(extractError(e, t('otaUpdate.rollbackFailed')))
  } finally {
    rollingBack.value = false
  }
}

async function fetchBackups() {
  fetchingBackups.value = true
  try {
    const data = await otaApi.backups()
    backups.value = data?.backups || []
  } catch (e: any) {
    message.error(extractError(e, t('otaUpdate.fetchBackupFailed')))
  } finally {
    pageLoading.value = false
    fetchingBackups.value = false
  }
}

onMounted(fetchBackups)
</script>
