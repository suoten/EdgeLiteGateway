/**
 * useDeviceDetail - composable that manages device detail page state.
 *
 * Provides reactive device data, tab management, health monitoring,
 * and protocol metadata for the device detail page.
 */
import { ref, computed, onMounted, watch, onScopeDispose, type Ref, type ComputedRef, provide, inject } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { deviceApi, debugApi, type Device } from '@/api'
import { connect, disconnect } from '@/api/websocket'
import { PROTOCOL_CONFIGS } from '@/constants/protocolConfig'
import { message } from '@/utils/discreteApi'
import { extractError } from '@/utils/errorCodes'
import { t } from '@/i18n'

const DEVICE_DETAIL_KEY = Symbol('deviceDetail')

export interface DeviceDetailContext {
  device: Ref<Device | null>
  notFound: Ref<boolean>
  pageLoading: Ref<boolean>
  activeTab: Ref<string>
  protocolMeta: ComputedRef<any>
  healthData: Ref<any>
  driverHealth: Ref<any>
  refreshing: Ref<boolean>
  resettingHealth: Ref<boolean>
  selfTestRunning: Ref<boolean>
  fetchDevice: () => Promise<void>
  refresh: () => Promise<void>
  startEdit: () => void
  handleResetHealthConfirm: () => void
  runSelfTest: () => Promise<void>
  exportPointsToCsv: () => void
}

export function useDeviceDetailProvider(): DeviceDetailContext {
  const route = useRoute()
  const router = useRouter()
  const deviceId = computed(() => route.params.device_id as string || route.params.id as string || '')

  const device = ref<Device | null>(null)
  const notFound = ref(false)
  const pageLoading = ref(true)
  const activeTab = ref((route.query.tab as string) || 'overview')
  const healthData = ref<any>(null)
  const driverHealth = ref<any>(null)
  const refreshing = ref(false)
  const resettingHealth = ref(false)
  const selfTestRunning = ref(false)

  const protocolMeta = computed(() => {
    if (!device.value?.protocol) return null
    return PROTOCOL_CONFIGS.value[device.value.protocol] || null
  })

  async function fetchDevice() {
    if (!deviceId.value) return
    pageLoading.value = true
    notFound.value = false
    try {
      device.value = await deviceApi.get(deviceId.value)
    } catch (e: any) {
      if (e?.response?.status === 404) {
        notFound.value = true
        device.value = null
      } else {
        message.error(extractError(e))
      }
    } finally {
      pageLoading.value = false
    }
  }

  async function fetchHealth() {
    if (!deviceId.value) return
    try {
      healthData.value = await deviceApi.getHealth(deviceId.value)
    } catch { healthData.value = null }
  }

  async function fetchDriverHealth() {
    if (!deviceId.value) return
    try {
      driverHealth.value = await deviceApi.getHealthDetail(deviceId.value)
    } catch { driverHealth.value = null }
  }

  async function refresh() {
    refreshing.value = true
    await Promise.all([fetchDevice(), fetchHealth(), fetchDriverHealth()])
    refreshing.value = false
  }

  function startEdit() {
    // Navigate to edit or emit event - handled by parent component
  }

  async function handleResetHealthConfirm() {
    if (!deviceId.value) return
    resettingHealth.value = true
    try {
      await deviceApi.resetHealth(deviceId.value)
      await fetchHealth()
    } catch { /* ignore */ } finally {
      resettingHealth.value = false
    }
  }

  async function runSelfTest() {
    if (!deviceId.value) return
    selfTestRunning.value = true
    try {
      await deviceApi.selfTest(deviceId.value)
    } catch { /* ignore */ } finally {
      selfTestRunning.value = false
    }
  }

  function exportPointsToCsv() {
    if (!device.value?.points?.length) return
    const headers = ['name', 'data_type', 'unit', 'address', 'access_mode']
    const rows = device.value.points.map(p => headers.map(h => (p as any)[h] ?? '').join(','))
    const csv = [headers.join(','), ...rows].join('\n')
    const blob = new Blob([csv], { type: 'text/csv' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `device_${deviceId.value}_points.csv`
    a.click()
    URL.revokeObjectURL(url)
  }

  // WebSocket message handler for real-time device updates
  function onWsMessage(data: any) {
    if (data?.type === 'device_status' && data?.device_id === deviceId.value && device.value) {
      device.value.status = data.status
    }
    if (data?.type === 'point_update' && data?.device_id === deviceId.value) {
      // Real-time point data is handled by child components via their own WS subscriptions
    }
  }

  watch(deviceId, () => {
    if (deviceId.value) refresh()
  })

  watch(() => route.query.tab, (newTab) => {
    if (newTab && typeof newTab === 'string') {
      activeTab.value = newTab
    }
  })

  onMounted(() => {
    refresh()
    connect('device', onWsMessage)
    connect('realtime', onWsMessage)
  })

  // Cleanup on scope dispose
  onScopeDispose(() => {
    disconnect('device', onWsMessage)
    disconnect('realtime', onWsMessage)
  })

  const ctx: DeviceDetailContext = {
    device, notFound, pageLoading, activeTab, protocolMeta, healthData, driverHealth,
    refreshing, resettingHealth, selfTestRunning,
    fetchDevice, refresh, startEdit, handleResetHealthConfirm, runSelfTest, exportPointsToCsv,
  }

  provide(DEVICE_DETAIL_KEY, ctx)

  return ctx
}

export function useDeviceDetailConsumer(): DeviceDetailContext {
  const ctx = inject<DeviceDetailContext>(DEVICE_DETAIL_KEY)
  if (!ctx) {
    throw new Error('useDeviceDetailConsumer must be used within a component that calls useDeviceDetailProvider')
  }
  return ctx
}
