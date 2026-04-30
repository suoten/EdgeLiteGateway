<template>
  <n-space vertical :size="16">
    <n-card title="Grafana监控" :bordered="false">
      <template #header-extra>
        <n-space>
          <n-tag :type="grafanaConfig.enabled ? 'success' : 'default'" size="small">
            {{ grafanaConfig.enabled ? '已启用' : '未启用' }}
          </n-tag>
          <n-button @click="fetchConfig">刷新</n-button>
        </n-space>
      </template>

      <n-descriptions label-placement="left" :column="2" bordered v-if="grafanaConfig.enabled">
        <n-descriptions-item label="Grafana地址">{{ grafanaConfig.url }}</n-descriptions-item>
        <n-descriptions-item label="数据源">{{ grafanaConfig.datasource }}</n-descriptions-item>
      </n-descriptions>

      <n-empty v-else description="Grafana集成未启用，请在配置文件中启用" />
    </n-card>

    <n-card title="仪表板" :bordered="false" v-if="grafanaConfig.enabled">
      <template #header-extra>
        <n-button @click="fetchDashboards">刷新</n-button>
      </template>
      <n-data-table :columns="dashboardColumns" :data="dashboards" :bordered="false" size="small" />
      <n-empty v-if="!dashboards.length" description="暂无仪表板" />
    </n-card>

    <n-card title="Grafana面板" :bordered="false" v-if="grafanaConfig.enabled && embedUrl">
      <iframe :src="embedUrl" style="width: 100%; height: 600px; border: none; border-radius: 8px;" />
    </n-card>
  </n-space>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, h } from 'vue'
import { NButton, useMessage } from 'naive-ui'
import { grafanaApi } from '@/api'

const message = useMessage()
const grafanaConfig = reactive<any>({})
const dashboards = ref<any[]>([])
const embedUrl = ref('')

const dashboardColumns = [
  { title: '标题', key: 'title', width: 200 },
  { title: '类型', key: 'type', width: 100 },
  { title: 'URI', key: 'uri', width: 200 },
  {
    title: '操作', key: 'actions', width: 100,
    render: (row: any) =>
      h(NButton, { text: true, type: 'primary', onClick: () => handleOpen(row.uid) }, { default: () => '打开' }),
  },
]

async function fetchConfig() {
  try {
    const data = await grafanaApi.config()
    Object.assign(grafanaConfig, data)
    if (data.enabled) {
      await fetchDashboards()
    }
  } catch (e: any) {
    message.error(e?.message || '获取Grafana配置失败')
  }
}

async function fetchDashboards() {
  try {
    const data = await grafanaApi.dashboards()
    dashboards.value = data?.dashboards || []
  } catch (e: any) {
    message.error(e?.message || '获取仪表板失败')
  }
}

async function handleOpen(uid?: string) {
  try {
    const data = await grafanaApi.embedUrl(uid)
    embedUrl.value = data.url
  } catch (e: any) {
    message.error(e?.response?.data?.detail || '获取嵌入URL失败')
  }
}

onMounted(fetchConfig)
</script>
