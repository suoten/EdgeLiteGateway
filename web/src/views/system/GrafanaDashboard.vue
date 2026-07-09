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
      <n-data-table :columns="dashboardColumns" :data="dashboards" :bordered="false" size="small">
        <template #empty>
          <n-empty :description="t('grafana.noDashboards')" />
        </template>
      </n-data-table>
    </n-card>

    <n-card v-if="enabled && embedUrl" :title="t('grafana.panel')" :bordered="false">
      <!-- FIXED-Severe: 移除 allow-same-origin，避免 iframe 内脚本访问父页面 Cookie/sessionStorage -->
      <!-- allow-scripts + allow-same-origin 同时设置会使 sandbox 保护形同虚设 -->
      <iframe :src="embedUrl" sandbox="allow-scripts allow-popups" style="width: 100%; height: 600px; border: none; border-radius: 8px;" />
    </n-card>

    <n-modal v-model:show="showInstallProgress" :title="t('grafana.installDeps')" preset="card" style="width: 480px; max-width: 95vw" :closable="false" :close-on-esc="true" :auto-focus="true" :close-on-esc-aria-label="t('common.closeDialog')">
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
import { NButton } from 'naive-ui'
import { t } from '@/i18n'
import { extractError } from '@/utils/errorCodes'
import { serviceApi, grafanaApi } from '@/api'
import type { ServiceDependency } from '@/api'
import { message, dialog } from '@/utils/discreteApi'
// [AUDIT-FIX] 严重级-Grafana 服务的启停/依赖安装/配置保存属敏感写操作，需函数级权限校验
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

// FIXED: 原问题-GrafanaDashboard.vue全部中文硬编码，改为i18n
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
const configFormRef = ref<any>(null)

const configForm = reactive({
  url: 'http://localhost:3001',
  api_key: '',
  datasource: 'InfluxDB',
})

const configRules = {
  url: [{ required: true, message: () => t('grafana.addressRequired'), trigger: ['input', 'blur'] }],
  api_key: [{ required: true, message: () => t('grafana.apiKeyRequired'), trigger: ['input', 'blur'] }],
  datasource: [{ required: true, message: () => t('grafana.datasourceRequired'), trigger: ['input', 'blur'] }],
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
    // FIX: 原问题-前端调用 serviceApi.status('grafana') 返回的 current_config 永远为空（后端 grafana.py 从 config.grafana 读取但从未正确赋值），
    // 导致"保存配置"按钮配置无法加载。
    // 改为直接调用 grafanaApi.config()，后端 grafana.py 会返回 config.grafana 的实际值。
    const data = await grafanaApi.config()
    enabled.value = data?.enabled ?? false
    state.value = data?.state ?? 'disabled'
    dependencies.value = data?.dependencies ?? []
    // current_config 字段来自 grafanaApi.config() 的返回，后端会正确填充 config.grafana 的 url/datasource 等值
    Object.assign(grafanaConfig, data ?? {})
    configForm.url = data?.url || 'http://localhost:3001'
    configForm.api_key = data?.api_key && data?.api_key !== '' ? '***configured***' : ''
    configForm.datasource = data?.datasource || 'InfluxDB'
    if (enabled.value) {
      // FIXED-P2: 前端先验证api_key是否已配置，避免发无效请求到后端
      if (!data?.api_key || data.api_key === '') {
        message.info(t('grafana.apiKeyMissing'))
        dashboards.value = []
      } else {
        await fetchDashboards()
      }
    }
  } catch (e: any) {
    if (e?.response?.status !== 404) message.error(extractError(e, t('grafana.fetchConfigFailed')))
  } finally {
    loading.value = false
  }
}

async function fetchDashboards() {
  // FIXED-P2: 前端先验证Grafana是否启用且api_key已配置，避免发无效请求
  if (!enabled.value) {
    dashboards.value = []
    return
  }
  if (!grafanaConfig.api_key || grafanaConfig.api_key === '') {
    message.info(t('grafana.apiKeyMissing'))
    dashboards.value = []
    return
  }
  try {
    const data = await grafanaApi.dashboards()
    dashboards.value = data?.dashboards || []
  } catch (e: any) {
    dashboards.value = []
    if (e?.response?.status === 503) {
      message.info(t('grafana.notEnabled'))
    } else {
      message.error(extractError(e, t('grafana.fetchDashboardsFailed')))
    }
  }
}

async function handleOpen(uid?: string) {
  if (!auth.isOperator) { message.warning(t('common.permissionDenied')); return }
  try {
    const data = await grafanaApi.embedUrl(uid)
    embedUrl.value = data?.url ?? ''
  } catch (e: any) {
    message.error(extractError(e, t('grafana.fetchEmbedFailed')))
  }
}

async function handleToggle(val: boolean) {
  if (!auth.isOperator) { message.warning(t('common.permissionDenied')); return }
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
      message.warning(extractError(e, t('grafana.missingDepsWarning')))
    } else {
      message.error(extractError(e, t('grafana.operationFailed')))
    }
  } finally {
    toggleLoading.value = false
  }
}

async function handleInstallDeps() {
  if (!auth.isOperator) { message.warning(t('common.permissionDenied')); return }
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
    installResult.value = `${t('grafana.installFailed')}: ${extractError(e, '')}`
    message.error(t('grafana.installFailed'))
  } finally {
    installing.value = false
    installProgress.value = ''
  }
}

async function handleSaveConfig() {
  if (!auth.isOperator) { message.warning(t('common.permissionDenied')); return }
  try {
    await configFormRef.value?.validate()
  } catch {
    message.error(t('common.checkFormData'))
    return
  }
  savingConfig.value = true
  try {
    const configPayload: Record<string, any> = {
      url: configForm.url,
      datasource: configForm.datasource,
    }
    // Only send api_key if user entered a new value (not the masked placeholder)
    if (configForm.api_key && configForm.api_key !== '***configured***') {
      configPayload.api_key = configForm.api_key
    }
    await serviceApi.updateConfig('grafana', configPayload)
    message.success(t('grafana.configSaved'))
    await fetchConfig()
  } catch (e: any) {
    message.error(extractError(e, t('grafana.saveConfigFailed')))
  } finally {
    savingConfig.value = false
  }
}

onMounted(fetchConfig)
</script>
