<template>
  <n-space vertical :size="16">
    <n-card :bordered="false">
      <template #header>
        <n-space align="center" :size="12">
          <span style="font-size: 16px; font-weight: 600">{{ t('grafana.title') }}</span>
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
            <template #checked>{{ t('grafana.enabled') }}</template>
            <template #unchecked>{{ t('grafana.disabled') }}</template>
          </n-switch>
          <n-button size="small" @click="fetchConfig" :loading="loading">{{ t('grafana.refresh') }}</n-button>
        </n-space>
      </template>

      <n-alert
        v-if="missingDeps.length > 0"
        type="warning"
        :bordered="false"
        style="margin-bottom: 12px"
      >
        <template #header>{{ t('grafana.missingDeps') }}</template>
        {{ t('grafana.depsNotInstalled') }}{{ missingDeps.map(d => d.package).join(', ') }}
        <n-button
          type="primary"
          size="small"
          style="margin-left: 12px"
          @click="handleInstallDeps"
          :loading="installing"
        >{{ t('grafana.oneClickInstall') }}</n-button>
      </n-alert>

      <n-descriptions v-if="enabled" label-placement="left" :column="2" bordered>
        <n-descriptions-item :label="t('grafana.address')">{{ grafanaConfig.url }}</n-descriptions-item>
        <n-descriptions-item :label="t('grafana.datasource')">{{ grafanaConfig.datasource }}</n-descriptions-item>
      </n-descriptions>

      <n-empty v-else :description="t('grafana.notEnabled')" />
    </n-card>

    <n-card v-if="enabled" :title="t('grafana.config')" :bordered="false">
      <template #header-extra>
        <n-button type="primary" size="small" @click="handleSaveConfig" :loading="savingConfig">{{ t('grafana.saveConfig') }}</n-button>
      </template>
      <n-form ref="configFormRef" :model="configForm" :rules="configRules" label-placement="left" label-width="120">
        <n-grid :cols="2" :x-gap="16">
          <n-form-item-gi :label="t('grafana.address')" path="url">
            <n-input v-model:value="configForm.url" placeholder="http://localhost:3001" />
          </n-form-item-gi>
          <n-form-item-gi :label="t('grafana.apiKey')" path="api_key">
            <n-input v-model:value="configForm.api_key" type="password" show-password-on="click" :placeholder="t('grafana.apiKeyPlaceholder')" />
          </n-form-item-gi>
          <n-form-item-gi :label="t('grafana.datasourceName')" path="datasource">
            <n-input v-model:value="configForm.datasource" placeholder="InfluxDB" />
          </n-form-item-gi>
        </n-grid>
      </n-form>
    </n-card>

    <n-card v-if="enabled" :title="t('grafana.dashboards')" :bordered="false">
      <template #header-extra>
        <n-button @click="fetchDashboards" size="small">{{ t('grafana.refresh') }}</n-button>
      </template>
      <n-data-table :columns="dashboardColumns" :data="dashboards" :bordered="false" size="small" />
      <n-empty v-if="!dashboards.length" :description="t('grafana.noDashboards')" />
    </n-card>

    <n-card v-if="enabled && embedUrl" :title="t('grafana.panel')" :bordered="false">
      <iframe :src="embedUrl" sandbox="allow-scripts allow-same-origin allow-popups" style="width: 100%; height: 600px; border: none; border-radius: 8px;" />
    </n-card>

    <n-modal v-model:show="showInstallProgress" :title="t('grafana.installDeps')" preset="card" style="width: 480px" :closable="false">
      <n-spin :description="installProgress">
        <n-space vertical>
          <p>{{ t('grafana.installing') }}</p>
          <p v-if="installResult">{{ installResult }}</p>
        </n-space>
      </n-spin>
      <template #action>
        <n-button @click="showInstallProgress = false" :disabled="installing">{{ t('common.close') }}</n-button>
      </template>
    </n-modal>
  </n-space>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, h } from 'vue'
import { NButton, useMessage, useDialog } from 'naive-ui'
import { t } from '@/i18n'  // FIXED: 原问题-#注释导致编译失败，改为//注释
import { serviceApi, grafanaApi } from '@/api'
import type { ServiceDependency } from '@/api'

// FIXED: 原问题-GrafanaDashboard.vue全部中文硬编码，改为i18n
const message = useMessage()
const dialog = useDialog()
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
  url: 'http://localhost:3001',
  api_key: '',
  datasource: 'InfluxDB',
})

const configRules = {
  url: [{ required: true, message: () => t('grafana.addressRequired'), trigger: 'blur' }],
  datasource: [{ required: true, message: () => t('grafana.datasourceRequired'), trigger: 'blur' }],
}

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
    case 'running': return t('grafana.stateRunning')
    case 'enabled': return t('grafana.stateEnabled')
    case 'error': return t('grafana.stateError')
    case 'disabled': return t('grafana.stateDisabled')
    default: return state.value
  }
})

const dashboardColumns = computed(() => [
  { title: t('grafana.colTitle'), key: 'title', width: 200 },
  { title: t('grafana.colType'), key: 'type', width: 100 },
  { title: t('grafana.colUri'), key: 'uri', width: 200 },
  {
    title: t('grafana.colActions'), key: 'actions', width: 100,
    render: (row: any) =>
      h(NButton, { text: true, type: 'primary', onClick: () => handleOpen(row.uid) }, { default: () => t('grafana.open') }),
  },
])

async function fetchConfig() {
  loading.value = true
  try {
    const data = await serviceApi.status('grafana')
    enabled.value = data?.state === 'running'
    state.value = data.state
    dependencies.value = data.dependencies || []
    Object.assign(grafanaConfig, data.current_config || {})
    configForm.url = data.current_config?.url || 'http://localhost:3001'
    configForm.api_key = data.current_config?.api_key || ''
    configForm.datasource = data.current_config?.datasource || 'InfluxDB'
    if (enabled.value) {
      await fetchDashboards()
    }
  } catch (e: any) {
    if (e?.response?.status !== 404) message.error(e?.response?.data?.detail || e?.message || t('grafana.fetchConfigFailed'))
  } finally {
    loading.value = false
  }
}

async function fetchDashboards() {
  try {
    const data = await grafanaApi.dashboards()
    dashboards.value = data?.dashboards || []
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || t('grafana.fetchDashboardsFailed'))
  }
}

async function handleOpen(uid?: string) {
  try {
    const data = await grafanaApi.embedUrl(uid)
    embedUrl.value = data?.url ?? ''
  } catch (e: any) {
    message.error(e?.response?.data?.detail || t('grafana.fetchEmbedFailed'))
  }
}

async function handleToggle(val: boolean) {
  if (!val) {
    dialog.warning({
      title: t('grafana.disableConfirmTitle'),
      content: t('grafana.disableConfirmContent'),
      positiveText: t('grafana.disableConfirmBtn'),
      negativeText: t('common.cancel'),
      onPositiveClick: () => doToggle(false),
    })
    return
  }
  await doToggle(true)
}

async function doToggle(val: boolean) {
  toggleLoading.value = true
  try {
    if (val) {
      await serviceApi.enable('grafana')
      message.success(t('grafana.enabledSuccess'))
    } else {
      await serviceApi.disable('grafana')
      message.success(t('grafana.disabledSuccess'))
    }
    await fetchConfig()
  } catch (e: any) {
    const detail = e?.response?.data?.detail
    if (typeof detail === 'object' && detail?.missing_dependencies) {
      message.warning(detail.message || t('grafana.missingDepsWarning'))
    } else {
      message.error(typeof detail === 'string' ? detail : (e?.message || t('grafana.operationFailed')))
    }
  } finally {
    toggleLoading.value = false
  }
}

async function handleInstallDeps() {
  installing.value = true
  showInstallProgress.value = true
  installProgress.value = t('grafana.installing')
  installResult.value = ''
  try {
    await serviceApi.installDeps('grafana')
    installResult.value = t('grafana.installSuccess')
    message.success(t('grafana.installSuccess'))
    await fetchConfig()
  } catch (e: any) {
    installResult.value = `${t('grafana.installFailed')}: ${e?.response?.data?.detail || e?.message}`
    message.error(t('grafana.installFailed'))
  } finally {
    installing.value = false
    installProgress.value = ''
  }
}

async function handleSaveConfig() {
  savingConfig.value = true
  try {
    await serviceApi.updateConfig('grafana', { ...configForm })
    message.success(t('grafana.configSaved'))
    await fetchConfig()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || t('grafana.saveConfigFailed'))
  } finally {
    savingConfig.value = false
  }
}

onMounted(fetchConfig)
</script>
