<template>
  <n-space vertical :size="16">
    <n-card :bordered="false">
      <template #header>
        <n-space align="center" :size="12">
          <span style="font-size: 18px; font-weight: 600">服务管理</span>
          <n-tag round>{{ runningCount }}/{{ services.length }} 运行中</n-tag>
        </n-space>
      </template>
      <template #header-extra>
        <n-button @click="fetchServices" :loading="loading" quaternary size="small">
          <template #icon><n-icon><refresh-outline /></n-icon></template>
          刷新
        </n-button>
      </template>

      <n-alert type="info" :bordered="false" style="margin-bottom: 16px">
        在这里可以管理网关的所有可选服务。点击开关即可启用或停用服务，无需重启网关。如果缺少依赖组件，系统会自动提示并支持一键安装。
      </n-alert>
    </n-card>

    <n-card title="内置服务" :bordered="false">
      <template #header-extra>
        <n-text depth="3" style="font-size: 13px">网关内置的通信和仿真服务</n-text>
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
              <n-text depth="3" style="font-size: 12px; font-weight: 600">适用场景：</n-text>
              <n-text v-for="(uc, i) in svc.use_cases.slice(0, 2)" :key="i" depth="3" style="font-size: 12px; display: block; padding-left: 8px">
                · {{ uc }}
              </n-text>
              <n-text v-if="svc.use_cases.length > 2" depth="3" style="font-size: 12px; padding-left: 8px">
                ...等{{ svc.use_cases.length }}个场景
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
                <template #checked>启用</template>
                <template #unchecked>停用</template>
              </n-switch>
              <n-button text type="primary" size="small" @click="$router.push(serviceRoute(svc.name))">
                详情 →
              </n-button>
            </n-space>

            <n-alert
              v-if="getMissingDeps(svc).length > 0 && svc.state !== 'disabled'"
              type="warning"
              :bordered="false"
              style="margin-top: 8px"
              size="small"
            >
              缺少依赖：{{ getMissingDeps(svc).join(', ') }}
              <n-button text type="primary" size="tiny" @click.stop="handleInstallDeps(svc.name)" :loading="installingMap[svc.name]">安装</n-button>
            </n-alert>
          </n-card>
        </n-gi>
      </n-grid>
    </n-card>

    <n-card title="集成服务" :bordered="false">
      <template #header-extra>
        <n-text depth="3" style="font-size: 13px">与外部系统的集成服务</n-text>
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
              <n-text depth="3" style="font-size: 12px; font-weight: 600">适用场景：</n-text>
              <n-text v-for="(uc, i) in svc.use_cases.slice(0, 2)" :key="i" depth="3" style="font-size: 12px; display: block; padding-left: 8px">
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
                <template #checked>启用</template>
                <template #unchecked>停用</template>
              </n-switch>
              <n-button text type="primary" size="small" @click="$router.push(serviceRoute(svc.name))">
                详情 →
              </n-button>
            </n-space>

            <n-alert
              v-if="getMissingDeps(svc).length > 0 && svc.state !== 'disabled'"
              type="warning"
              :bordered="false"
              style="margin-top: 8px"
              size="small"
            >
              缺少依赖：{{ getMissingDeps(svc).join(', ') }}
              <n-button text type="primary" size="tiny" @click.stop="handleInstallDeps(svc.name)" :loading="installingMap[svc.name]">安装</n-button>
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

function stateLabel(state: string) {
  switch (state) {
    case 'running': return '运行中'
    case 'enabled': return '已启用'
    case 'error': return '异常'
    case 'installing': return '安装中'
    case 'disabled': return '未启用'
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
      message.error(e?.message || '获取服务列表失败')
    }
  } finally {
    loading.value = false
  }
}

async function handleToggle(name: string, val: boolean) {
  if (!val) {
    dialog.warning({
      title: '确认停用服务',
      content: `停用「${name}」服务后，依赖该服务的设备可能断开连接。确定继续？`,
      positiveText: '确认停用',
      negativeText: '取消',
      onPositiveClick: async () => {
        toggleLoadingMap[name] = true
        try {
          await serviceApi.disable(name)
          message.success('服务已停用')
          await fetchServices()
        } catch (e: any) {
          message.error(e?.response?.data?.detail || e?.message || '操作失败')
        } finally {
          toggleLoadingMap[name] = false
        }
      },
    })
  } else {
    toggleLoadingMap[name] = true
    try {
      await serviceApi.enable(name)
      message.success('服务已启用')
      await fetchServices()
    } catch (e: any) {
      const detail = e?.response?.data?.detail
      if (typeof detail === 'object' && detail?.missing_dependencies) {
        message.warning(detail.message || '缺少依赖，请先安装')
      } else {
        message.error(typeof detail === 'string' ? detail : (e?.message || '操作失败'))
      }
    } finally {
      toggleLoadingMap[name] = false
    }
  }
}

async function handleInstallDeps(name: string) {
  installingMap[name] = true
  try {
    await serviceApi.installDeps(name)
    message.success('依赖安装成功，正在启动服务...')
    await serviceApi.start(name)
    await fetchServices()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || '安装失败')
  } finally {
    installingMap[name] = false
  }
}

onMounted(fetchServices)
</script>
