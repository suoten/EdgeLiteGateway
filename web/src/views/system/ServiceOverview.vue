<template>
  <n-space vertical :size="16">
    <n-card :bordered="false">
      <template #header>
        <n-space align="center" :size="12">
          <span style="font-size: 18px; font-weight: 600">{{ t('serviceOverview.title') }}</span>
          <n-tag round>{{ runningCount }}/{{ services.length }} {{ t('serviceOverview.running') }}</n-tag>
        </n-space>
      </template>
      <template #header-extra>
        <n-button @click="fetchServices" :loading="loading" quaternary size="small">
          <template #icon><n-icon><refresh-outline /></n-icon></template>
          {{ t('serviceOverview.refresh') }}
        </n-button>
      </template>

      <n-alert type="info" :bordered="false" style="margin-bottom: 16px">
        {{ t('serviceOverview.desc') }}
      </n-alert>
    </n-card>

    <n-card :title="t('serviceOverview.builtInService')" :bordered="false">
      <template #header-extra>
        <n-text depth="3" style="font-size: 13px">{{ t('serviceOverview.builtInDesc') }}</n-text>
      </template>
      <n-grid :cols="3" :x-gap="16" :y-gap="16" responsive="screen">
        <n-gi v-for="svc in builtinServices" :key="svc.name">
          <n-card embedded hoverable size="small" :style="cardStyle(svc.state)">
            <template #header>
              <n-space align="center" :size="8">
                <n-icon size="20"><component :is="iconComponent(svc.icon)" /></n-icon>
                <n-text strong>{{ svc.display_name }}</n-text>
              </n-space>
            </template>
            <template #header-extra>
              <n-tag :type="stateTagType(svc.state)" size="small" round>{{ stateLabel(svc.state) }}</n-tag>
            </template>

            <n-text depth="3" style="font-size: 13px">{{ svc.description }}</n-text>

            <n-divider style="margin: 12px 0 8px" />

            <n-space vertical :size="6">
              <n-text depth="3" style="font-size: 12px; font-weight: 600">{{ t('serviceOverview.useCases') }}</n-text>
              <n-text v-for="(uc, i) in (svc.use_cases ?? []).slice(0, 2)" :key="i" depth="3" style="font-size: 12px; display: block; padding-left: 8px">
                · {{ uc }}
              </n-text>
              <n-text v-if="(svc.use_cases ?? []).length > 2" depth="3" style="font-size: 12px; padding-left: 8px">
                {{ t('serviceOverview.moreScenes', { count: (svc.use_cases ?? []).length }) }}
              </n-text>
            </n-space>

            <n-divider style="margin: 8px 0" />

            <n-space justify="space-between" align="center">
              <n-switch
                :value="svc.state !== 'disabled'"
                :loading="toggleLoadingMap[svc.name]"
                size="small"
                @update:value="(v: boolean) => handleToggle(svc.name, v)"
              >
                <template #checked>{{ t('serviceOverview.enable') }}</template>
                <template #unchecked>{{ t('serviceOverview.disable') }}</template>
              </n-switch>
              <n-button text type="primary" size="small" @click="$router.push(serviceRoute(svc.name))">
                {{ t('serviceOverview.viewDetail') }}
              </n-button>
            </n-space>

            <n-alert
              v-if="getMissingDeps(svc).length > 0 && svc.state !== 'disabled'"
              type="warning"
              :bordered="false"
              style="margin-top: 8px"
              size="small"
            >
              {{ t('serviceOverview.missingDeps') }}{{ getMissingDeps(svc).join(', ') }}
              <n-button text type="primary" size="tiny" @click.stop="handleInstallDeps(svc.name)" :loading="installingMap[svc.name]">{{ t('serviceOverview.install') }}</n-button>
            </n-alert>
          </n-card>
        </n-gi>
      </n-grid>
    </n-card>

    <n-card :title="t('serviceOverview.integrationService')" :bordered="false">
      <template #header-extra>
        <n-text depth="3" style="font-size: 13px">{{ t('serviceOverview.integrationDesc') }}</n-text>
      </template>
      <n-grid :cols="3" :x-gap="16" :y-gap="16" responsive="screen">
        <n-gi v-for="svc in integrationServices" :key="svc.name">
          <n-card embedded hoverable size="small" :style="cardStyle(svc.state)">
            <template #header>
              <n-space align="center" :size="8">
                <n-icon size="20"><component :is="iconComponent(svc.icon)" /></n-icon>
                <n-text strong>{{ svc.display_name }}</n-text>
              </n-space>
            </template>
            <template #header-extra>
              <n-tag :type="stateTagType(svc.state)" size="small" round>{{ stateLabel(svc.state) }}</n-tag>
            </template>

            <n-text depth="3" style="font-size: 13px">{{ svc.description }}</n-text>

            <n-divider style="margin: 12px 0 8px" />

            <n-space vertical :size="6">
              <n-text depth="3" style="font-size: 12px; font-weight: 600">{{ t('serviceOverview.useCases') }}</n-text>
              <n-text v-for="(uc, i) in (svc.use_cases ?? []).slice(0, 2)" :key="i" depth="3" style="font-size: 12px; display: block; padding-left: 8px">
                · {{ uc }}
              </n-text>
            </n-space>

            <n-divider style="margin: 8px 0" />

            <n-space justify="space-between" align="center">
              <n-switch
                :value="svc.state !== 'disabled'"
                :loading="toggleLoadingMap[svc.name]"
                size="small"
                @update:value="(v: boolean) => handleToggle(svc.name, v)"
              >
                <template #checked>{{ t('serviceOverview.enable') }}</template>
                <template #unchecked>{{ t('serviceOverview.disable') }}</template>
              </n-switch>
              <n-button text type="primary" size="small" @click="$router.push(serviceRoute(svc.name))">
                {{ t('serviceOverview.viewDetail') }}
              </n-button>
            </n-space>

            <n-alert
              v-if="getMissingDeps(svc).length > 0 && svc.state !== 'disabled'"
              type="warning"
              :bordered="false"
              style="margin-top: 8px"
              size="small"
            >
              {{ t('serviceOverview.missingDeps') }}{{ getMissingDeps(svc).join(', ') }}
              <n-button text type="primary" size="tiny" @click.stop="handleInstallDeps(svc.name)" :loading="installingMap[svc.name]">{{ t('serviceOverview.install') }}</n-button>
            </n-alert>
          </n-card>
        </n-gi>
      </n-grid>
    </n-card>
  </n-space>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, h } from 'vue'
import { useMessage, useDialog } from 'naive-ui'
import { NIcon } from 'naive-ui'
import {
  RefreshOutline,
  RadioOutline,
  PowerOutline,
  SwapHorizontalOutline,
  ExtensionPuzzleOutline,
  BarChartOutline,
} from '@vicons/ionicons5'
import { serviceApi } from '@/api'
import type { ServiceInfo } from '@/api'
import { extractError } from '@/utils/errorCodes'
// FIXED: 原问题-添加i18n支持
import { t } from '@/i18n'

const message = useMessage()
const dialog = useDialog()
const loading = ref(false)
const services = ref<ServiceInfo[]>([])
const toggleLoadingMap = reactive<Record<string, boolean>>({})
const installingMap = reactive<Record<string, boolean>>({})

const iconMap: Record<string, any> = {
  radio: RadioOutline,
  power: PowerOutline,
  swap: SwapHorizontalOutline,
  puzzle: ExtensionPuzzleOutline,
  chart: BarChartOutline,
}

const routeMap: Record<string, string> = {
  mqtt_server: 'MqttServer',
  modbus_slave: 'ModbusSlave',
  serial_bridge: 'SerialBridge',
  mcp_server: 'McpServer',
  grafana: 'GrafanaDashboard',
}

const builtinServices = computed(() => services.value.filter(s => s.category === 'builtin'))
const integrationServices = computed(() => services.value.filter(s => s.category === 'integration'))
const runningCount = computed(() => services.value.filter(s => s.state === 'running').length)

function iconComponent(icon: string) { return iconMap[icon] || RadioOutline }
function serviceRoute(name: string) { return { name: routeMap[name] || 'Dashboard' } }

function stateTagType(state: string) {
  switch (state) {
    case 'running': return 'success'
    case 'enabled': return 'info'
    case 'error': return 'error'
    case 'installing': return 'warning'
    default: return 'default'
  }
}

// FIXED: 原问题-stateLabel中文硬编码，改为i18n
function stateLabel(state: string) {
  switch (state) {
    case 'running': return t('serviceState.running')
    case 'enabled': return t('serviceState.enabled')
    case 'error': return t('serviceState.error')
    case 'installing': return t('serviceState.installing')
    case 'disabled': return t('serviceState.disabled')
    default: return state
  }
}

function cardStyle(state: string) {
  if (state === 'running') return 'border-left: 3px solid var(--n-success-color)'
  if (state === 'error') return 'border-left: 3px solid var(--n-error-color)'
  if (state === 'enabled') return 'border-left: 3px solid var(--n-info-color)'
  return 'border-left: 3px solid var(--n-border-color)'
}

function getMissingDeps(svc: ServiceInfo): string[] {
  return (svc.dependencies || []).filter(d => !d.installed).map(d => d.package)
}

async function fetchServices() {
  loading.value = true
  try {
    const data = await serviceApi.list()
    services.value = data?.services || []
  } catch (e: any) {
    if (e?.response?.status === 404) {
      services.value = []
    } else {
      message.error(e?.message || t('serviceOverview.fetchFailed'))
    }
  } finally {
    loading.value = false
  }
}

async function handleToggle(name: string, val: boolean) {
  if (!val) {
    dialog.warning({
      title: t('serviceOverview.disableTitle'),
      content: t('serviceOverview.disableContent', { name }),
      positiveText: t('serviceOverview.confirmDisable'),
      negativeText: t('common.cancel'),
      onPositiveClick: async () => {
        toggleLoadingMap[name] = true
        try {
          await serviceApi.disable(name)
          message.success(t('serviceOverview.disableSuccess'))
          await fetchServices()
        } catch (e: any) {
          message.error(extractError(e, t('serviceOverview.operationFailed')))
        } finally {
          toggleLoadingMap[name] = false
        }
      },
    })
  } else {
    toggleLoadingMap[name] = true
    try {
      const result = await serviceApi.enable(name)
      if (result?.warning) {
        message.warning(result.warning || result.message || t('serviceOverview.enableButStartFailed'))
      } else {
        message.success(t('serviceOverview.enableSuccess'))
      }
      await fetchServices()
    } catch (e: any) {
      const detail = e?.response?.data?.detail
      if (e?.response?.status === 424 || (typeof detail === 'object' && detail?.missing_dependencies)) {
        await fetchServices()
        const svc = services.value.find(s => s.name === name)
        const missingPkgs = typeof detail === 'object' && detail?.missing_dependencies
          ? detail.missing_dependencies.join(', ')
          : (svc ? getMissingDeps(svc).join(', ') : t('serviceOverview.unknownDep'))
        dialog.warning({
          title: t('serviceOverview.depTitle'),
          content: t('serviceOverview.depContent', { name, deps: missingPkgs }),
          positiveText: t('serviceOverview.oneClickInstall'),
          negativeText: t('serviceOverview.later'),
          onPositiveClick: async () => {
            await handleInstallDeps(name)
          },
        })
      } else {
        const extracted = extractError(e, t('serviceOverview.operationFailed'))
        const hint = (typeof detail === 'object' && detail !== null) ? (detail.hint || '') : ''
        if (hint) {
          dialog.error({ title: extracted, content: hint, positiveText: t('common.confirm') })
        } else {
          message.error(extracted)
        }
      }
      await fetchServices()
    } finally {
      toggleLoadingMap[name] = false
    }
  }
}

async function handleInstallDeps(name: string) {
  installingMap[name] = true
  try {
    await serviceApi.installDeps(name)
    message.success(t('serviceOverview.installSuccess'))
    await serviceApi.enable(name)
    await fetchServices()
  } catch (e: any) {
    message.error(extractError(e, t('serviceOverview.installFailed')))
  } finally {
    installingMap[name] = false
  }
}

onMounted(fetchServices)
</script>
