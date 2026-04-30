<template>
  <n-space vertical :size="16">
    <n-card title="OTA升级" :bordered="false">
      <template #header-extra>
        <n-space>
          <n-button @click="checkUpdate" :loading="checking">检查更新</n-button>
        </n-space>
      </template>

      <n-descriptions label-placement="left" :column="2" bordered v-if="updateInfo">
        <n-descriptions-item label="当前版本">{{ updateInfo.current_version || '-' }}</n-descriptions-item>
        <n-descriptions-item label="最新版本">
          <n-tag v-if="updateInfo.has_update" type="warning" size="small">{{ updateInfo.latest_version }}</n-tag>
          <n-tag v-else type="success" size="small">已是最新</n-tag>
        </n-descriptions-item>
        <n-descriptions-item label="更新说明" :span="2">{{ updateInfo.release_notes || '-' }}</n-descriptions-item>
      </n-descriptions>
      <n-empty v-else description="点击检查更新查看可用版本" />

      <n-space style="margin-top: 16px" v-if="updateInfo?.has_update">
        <n-button type="primary" @click="applyUpdate" :loading="applying">应用更新</n-button>
      </n-space>
    </n-card>

    <n-card title="备份版本" :bordered="false">
      <template #header-extra>
        <n-button @click="fetchBackups">刷新</n-button>
      </template>
      <n-data-table :columns="backupColumns" :data="backups" :bordered="false" size="small" />
      <n-empty v-if="!backups.length" description="暂无备份版本" />
    </n-card>
  </n-space>
</template>

<script setup lang="ts">
import { ref, onMounted, h } from 'vue'
import { NButton, useMessage } from 'naive-ui'
import { otaApi } from '@/api'

const message = useMessage()
const checking = ref(false)
const applying = ref(false)
const updateInfo = ref<any>(null)
const backups = ref<any[]>([])

const backupColumns = [
  { title: '版本', key: 'version', width: 150 },
  { title: '备份时间', key: 'created_at', width: 200 },
  { title: '大小', key: 'size', width: 100 },
  {
    title: '操作', key: 'actions', width: 100,
    render: (row: any) =>
      h(NButton, { text: true, type: 'warning', onClick: () => handleRollback(row.version) }, { default: () => '回滚' }),
  },
]

async function checkUpdate() {
  checking.value = true
  try {
    const data = await otaApi.check()
    updateInfo.value = data
  } catch (e: any) {
    message.error(e?.response?.data?.detail || '检查更新失败')
  } finally { checking.value = false }
}

async function applyUpdate() {
  applying.value = true
  try {
    await otaApi.apply()
    message.success('更新已应用，系统将重启')
  } catch (e: any) {
    message.error(e?.response?.data?.detail || '应用更新失败')
  } finally { applying.value = false }
}

async function handleRollback(version: string) {
  try {
    await otaApi.rollback(version)
    message.success('已回滚到版本 ' + version)
    await fetchBackups()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || '回滚失败')
  }
}

async function fetchBackups() {
  try {
    const data = await otaApi.backups()
    backups.value = data?.backups || []
  } catch (e: any) {
    message.error(e?.message || '获取备份失败')
  }
}

onMounted(fetchBackups)
</script>
