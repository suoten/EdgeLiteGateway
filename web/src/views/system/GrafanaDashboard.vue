<template>
  <n-space vertical :size="16">
    <n-card :bordered="false">
      <template #header>
        <n-space align="center" :size="12">
          <span style="font-size: 16px; font-weight: 600">Grafana监控</span>
          <n-tag :type="stateTagType" size="small">{{ stateLabel }}</n-tag>
        </n-space>
      </template>
      <template #header-extra>
        <n-space :size="8">
          <n-switch
            :value="enabled"
            :loading="toggleLoading"
            @update:value="handleToggle"
          >
            <template #checked>已启用</template>
            <template #unchecked>已停用</template>
          </n-switch>
          <n-button size="small" @click="fetchConfig" :loading="loading">刷新</n-button>
        </n-space>
      </template>

      <n-alert
        v-if="missingDeps.length > 0"
        type="warning"
        :bordered="false"
        style="margin-bottom: 12px"
      >
        <template #header>缺少依赖组件</template>
        以下依赖未安装：{{ missingDeps.map(d => d.package).join(', ') }}
        <n-button
          type="primary"
          size="small"
          style="margin-left: 12px"
          @click="handleInstallDeps"
          :loading="installing"
        >一键安装</n-button>
      </n-alert>

      <n-descriptions v-if="enabled" label-placement="left" :column="2" bordered>
        <n-descriptions-item label="Grafana地址">{{ grafanaConfig.url }}</n-descriptions-item>
        <n-descriptions-item label="数据源">{{ grafanaConfig.datasource }}</n-descriptions-item>
      </n-descriptions>

      <n-empty v-else description="Grafana集成未启用，请开启开关启用服务" />
    </n-card>

    <n-card v-if="enabled" title="Grafana配置" :bordered="false">
      <template #header-extra>
        <n-button type="primary" size="small" @click="handleSaveConfig" :loading="savingConfig">保存配置</n-button>
      </template>
      <n-form :model="configForm" label-placement="left" label-width="120">
        <n-grid :cols="2" :x-gap="16">
          <n-form-item-gi label="Grafana地址">
            <n-input v-model:value="configForm.url" placeholder="http://localhost:3000" />
          </n-form-item-gi>
          <n-form-item-gi label="API Key">
            <n-input v-model:value="configForm.api_key" type="password" show-password-on="click" placeholder="Grafana API Key" />
          </n-form-item-gi>
          <n-form-item-gi label="数据源名称">
            <n-input v-model:value="configForm.datasource" placeholder="InfluxDB" />
          </n-form-item-gi>
        </n-grid>
      </n-form>
    </n-card>

    <n-card v-if="enabled" title="仪表板" :bordered="false">
      <template #header-extra>
        <n-button @click="fetchDashboards" size="small">刷新</n-button>
      </template>
      <n-data-table :columns="dashboardColumns" :data="dashboards" :bordered="false" size="small" />
      <n-empty v-if="!dashboards.length" description="暂无仪表板" />
    </n-card>

    <n-card v-if="enabled && embedUrl" title="Grafana面板" :bordered="false">
      <iframe :src="embedUrl" style="width: 100%; height: 600px; border: none; border-radius: 8px;" />
    </n-card>

    <n-modal v-model:show="showInstallProgress" title="安装依赖" preset="card" style="width: 480px" :closable="false">
      <n-spin :description="installProgress">
        <n-space vertical>
          <p>正在安装缺失的依赖组件，请稍候...</p>
          <p v-if="installResult">{{ installResult }}</p>
        </n-space>
      </n-spin>
      <template #action>
        <n-button @click="showInstallProgress = false" :disabled="installing">关闭</n-button>
      </template>
    </n-modal>
  </n-space>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, h } from 'vue'
import { NButton, useMessage } from 'naive-ui'
import { serviceApi, grafanaApi } from '@/api'
import type { ServiceDependency } from '@/api'

const message = useMessage()
const loading = ref(false)
const toggleLoading = ref(false)
const savingConfig = ref(false)
const installing = ref(false)
const showInstallProgress = ref(false)
const installProgress = ref('')
const installResult = ref('')

const enabled = ref(false)
const state = ref<string>('disabled')
const dependencies = ref<ServiceDependency[]>([])
const grafanaConfig = reactive<any>({})
const dashboards = ref<any[]>([])
const embedUrl = ref('')

const configForm = reactive({
  url: 'http://localhost:3000',
  api_key: '',
  datasource: 'InfluxDB',
})

const missingDeps = computed(() => dependencies.value.filter(d => !d.installed))

const stateTagType = computed(() => {
  switch (state.value) {
    case 'running': return 'success'
    case 'enabled': return 'info'
    case 'error': return 'error'
    default: return 'default'
  }
})

const stateLabel = computed(() => {
  switch (state.value) {
    case 'running': return '运行中'
    case 'enabled': return '已启用'
    case 'error': return '异常'
    case 'disabled': return '未启用'
    default: return state.value
  }
})

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
  loading.value = true
  try {
    const data = await serviceApi.status('grafana')
    enabled.value = data.state !== 'disabled'
    state.value = data.state
    dependencies.value = data.dependencies || []
    Object.assign(grafanaConfig, data.current_config || {})
    configForm.url = data.current_config?.url || 'http://localhost:3000'
    configForm.api_key = data.current_config?.api_key || ''
    configForm.datasource = data.current_config?.datasource || 'InfluxDB'
    if (enabled.value) {
      await fetchDashboards()
    }
  } catch (e: any) {
    message.error(e?.message || '获取Grafana配置失败')
  } finally {
    loading.value = false
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

async function handleToggle(val: boolean) {
  toggleLoading.value = true
  try {
    if (val) {
      await serviceApi.enable('grafana')
      message.success('Grafana监控已启用')
    } else {
      await serviceApi.disable('grafana')
      message.success('Grafana监控已停用')
    }
    await fetchConfig()
  } catch (e: any) {
    const detail = e?.response?.data?.detail
    if (typeof detail === 'object' && detail?.missing_dependencies) {
      message.warning(detail.message || '缺少依赖，请先安装')
    } else {
      message.error(typeof detail === 'string' ? detail : (e?.message || '操作失败'))
    }
  } finally {
    toggleLoading.value = false
  }
}

async function handleInstallDeps() {
  installing.value = true
  showInstallProgress.value = true
  installProgress.value = '正在安装依赖...'
  installResult.value = ''
  try {
    await serviceApi.installDeps('grafana')
    installResult.value = '依赖安装成功！'
    message.success('依赖安装成功')
    await fetchConfig()
  } catch (e: any) {
    installResult.value = `安装失败: ${e?.response?.data?.detail || e?.message}`
    message.error('依赖安装失败')
  } finally {
    installing.value = false
    installProgress.value = ''
  }
}

async function handleSaveConfig() {
  savingConfig.value = true
  try {
    await serviceApi.updateConfig('grafana', { ...configForm })
    message.success('配置已保存')
    await fetchConfig()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || '保存配置失败')
  } finally {
    savingConfig.value = false
  }
}

onMounted(fetchConfig)
</script>
