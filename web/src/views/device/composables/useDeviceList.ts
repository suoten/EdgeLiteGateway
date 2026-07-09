/**
 * useDeviceList - composable that manages device list state and operations.
 *
 * Provides reactive state for devices, filters, modals, forms, batch operations,
 * discovery, import/export, and configuration comparison.
 */
import { ref, reactive, computed, onMounted, onUnmounted, watch, type Ref, type ComputedRef } from 'vue'
import http from '@/api/http'
import { deviceApi, type Device, type DeviceCreateParams, type PointDef } from '@/api'
import { dialog, message } from '@/utils/discreteApi'
import { t } from '@/i18n'
import { extractError } from '@/utils/errorCodes'
import { connect, disconnect, onStatus, offStatus } from '@/api/websocket'
import { PROTOCOL_CONFIGS } from '@/constants/protocolConfig'

// Type definitions
interface PaginationState { page: number; pageSize: number; itemCount: number; pageSizes?: number[]; onChange?: (page: number) => void; onUpdatePageSize?: (size: number) => void; showSizePicker?: boolean }

export function useDeviceList() {
  // ─── Core state ───
  const devices = ref<Device[]>([])
  const loading = ref(false)
  const searchText = ref('')
  const filterStatus = ref<string | null>(null)
  const filterProtocol = ref<string | null>(null)
  const collectFilter = ref<string>('all')
  const checkedKeys = ref<string[]>([])
  const pagination = reactive<PaginationState>({ page: 1, pageSize: 20, itemCount: 0, pageSizes: [10, 20, 50, 100], showSizePicker: true })

  // ─── WebSocket status ───
  const wsConnected = ref(false)
  const wsReconnecting = ref(false)
  let _wsStatusHandler: ((status: string, reason?: string) => void) | null = null

  // ─── Modal visibility ───
  const showCreateModal = ref(false)
  const showEditModal = ref(false)
  const showSimModal = ref(false)
  const showDiscoverModal = ref(false)
  const showImportModal = ref(false)
  const showDeployModal = ref(false)
  const showShareModal = ref(false)
  const showTransferModal = ref(false)

  // ─── Forms ───
  const createForm = reactive<DeviceCreateParams & { points: PointDef[]; tags: string[] }>({
    device_id: '', name: '', protocol: '', config: {}, collect_interval: 5, points: [], tags: [] as string[],
  })
  const editForm = reactive<Device & { points: PointDef[]; tags: string[] }>({
    device_id: '', name: '', protocol: '', status: 'offline', config: {}, collect_interval: 5,
    points: [], created_by: null, created_at: '', updated_at: '', version: 0, tags: [] as string[],
  })
  const simForm = reactive({
    device_id: '', name: '', protocol: 'simulator', config: { interval: 1 }, collect_interval: 1,
    points: [] as PointDef[],
  })
  const shareForm = reactive({ device_ids: [] as string[], target_user_id: '', shared_with_user_id: '', permission_level: 'view' as string })
  const transferForm = reactive({ device_ids: [] as string[], target_user_id: '', device_name: '', new_owner_id: '' })

  // ─── Loading flags ───
  const creating = ref(false)
  const editSaving = ref(false)
  const discovering = ref(false)
  const addingDevices = ref(false)
  const importing = ref(false)
  const importProgress = ref(0)
  const deploying = ref(false)
  const batchCollectLoading = ref(false)
  const batchDeleteLoading = ref(false)
  const sharing = ref(false)
  const transferring = ref(false)
  const testingConnection = ref(false)
  const connectionTestResult = ref<{ ok: boolean; success?: boolean; supported?: boolean; message: string } | null>(null)

  // ─── Discovery ───
  const discoverHost = ref('192.168.1.1')
  const discoverPort = ref(502)
  const discoverProtocol = ref('modbus-tcp')
  const discoverResults = ref<any[]>([])
  const selectedDiscoverKeys = ref<string[]>([])
  const importPreview = ref<any[]>([])
  const importErrors = ref<any[]>([])
  const importAtomicMode = ref(true)

  // ─── Deploy ───
  const deployTemplateId = ref<string | null>('')
  const deployTemplateOptions = ref<{ label: string; value: string }[]>([])

  // ─── Selects ───
  const protocolOptions = computed(() => {
    const configs = PROTOCOL_CONFIGS.value
    return Object.keys(configs).map(k => ({ label: configs[k].label, value: k }))
  })
  const statusOptions = computed(() => [
    { label: t('deviceList.statusOnline'), value: 'online' },
    { label: t('deviceList.statusOffline'), value: 'offline' },
    { label: t('deviceList.statusError'), value: 'error' },
  ])
  const discoverProtocolOptions = computed(() =>
    Object.keys(PROTOCOL_CONFIGS.value).filter(k => PROTOCOL_CONFIGS.value[k].capabilities?.discover).map(k => ({
      label: PROTOCOL_CONFIGS.value[k].label, value: k,
    }))
  )
  const dataTypeOptions = [
    { label: 'BOOL', value: 'bool' },
    { label: 'INT16', value: 'int16' },
    { label: 'INT32', value: 'int32' },
    { label: 'UINT16', value: 'uint16' },
    { label: 'UINT32', value: 'uint32' },
    { label: 'FLOAT32', value: 'float32' },
    { label: 'FLOAT64', value: 'float64' },
    { label: 'STRING', value: 'string' },
  ]
  const simModeOptions = [
    { label: t('deviceList.simSine'), value: 'sine' },
    { label: t('deviceList.simRandom'), value: 'random' },
    { label: t('deviceList.simStatic'), value: 'static' },
    { label: t('deviceList.simRamp'), value: 'ramp' },
  ]
  const userOptions = ref<{ label: string; value: string }[]>([])

  // ─── Form refs ───
  const createFormRef = ref<any>(null)
  const editFormRef = ref<any>(null)
  const simFormRef = ref<any>(null)
  const protocolFormRef = ref<any>(null)
  const protocolEditRef = ref<any>(null)
  const shareFormRef = ref<any>(null)
  const transferFormRef = ref<any>(null)

  // ─── Driver schemas ───
  const driverSchemas = ref<Record<string, any>>({})

  // ─── Tags ───
  const selectedTags = ref<string[]>([])
  const tagOptions = computed(() => {
    const tags = new Set<string>()
    devices.value.forEach(d => {
      const dtags = (d as any).tags
      if (Array.isArray(dtags)) dtags.forEach((t: string) => tags.add(t))
    })
    return Array.from(tags).map(t => ({ label: t, value: t }))
  })
  const filteredDevicesByTag = computed(() => {
    if (!selectedTags.value.length) return devices.value
    return devices.value.filter(d => {
      const dtags = (d as any).tags || []
      return selectedTags.value.some(t => dtags.includes(t))
    })
  })
  function getDeviceTags(device: Device): string[] {
    return (device as any).tags || []
  }

  // ─── Compare ───
  const showCompareModal = ref(false)
  const compareDeviceAId = ref<string>('')
  const compareDeviceBId = ref<string>('')
  const compareLoading = ref(false)
  const compareDeviceA = ref<Device | null>(null)
  const compareDeviceB = ref<Device | null>(null)
  const compareDeviceOptions = computed(() => devices.value.map(d => ({ label: d.name, value: d.device_id })))
  const compareRows = ref<any[]>([])
  const compareDiffCount = ref(0)
  function openCompareModal() { showCompareModal.value = true; compareRows.value = []; compareDiffCount.value = 0 }
  async function handleCompare() {
    if (!compareDeviceAId.value || !compareDeviceBId.value) return
    compareLoading.value = true
    try {
      const [a, b] = await Promise.all([
        deviceApi.get(compareDeviceAId.value),
        deviceApi.get(compareDeviceBId.value),
      ])
      compareDeviceA.value = a
      compareDeviceB.value = b
      const rows: any[] = []
      let diffCount = 0
      const allKeys = new Set([...Object.keys(a || {}), ...Object.keys(b || {})])
      allKeys.forEach(k => {
        const va = (a as any)?.[k]
        const vb = (b as any)?.[k]
        const isDiff = JSON.stringify(va) !== JSON.stringify(vb)
        if (isDiff) diffCount++
        rows.push({ key: k, valueA: va, valueB: vb, diff: isDiff })
      })
      compareRows.value = rows
      compareDiffCount.value = diffCount
    } catch (e) {
      message.error(extractError(e))
    } finally {
      compareLoading.value = false
    }
  }

  // ─── Active filter count ───
  const activeFilterCount = computed(() => {
    let n = 0
    if (searchText.value) n++
    if (filterStatus.value) n++
    if (filterProtocol.value) n++
    if (collectFilter.value !== 'all') n++
    if (selectedTags.value.length) n++
    return n
  })

  function resetFilters() {
    searchText.value = ''
    filterStatus.value = null
    filterProtocol.value = null
    collectFilter.value = 'all'
    selectedTags.value = []
    pagination.page = 1
    fetchDevices()
  }

  // ─── Discover columns ───
  const discoverColumns = computed(() => [
    { type: 'selection' as const },
    { title: t('deviceList.deviceId'), key: 'device_id' },
    { title: t('deviceList.protocol'), key: 'protocol' },
    { title: t('deviceList.address'), key: 'address' },
  ])

  // ─── Import preview columns ───
  const importPreviewColumns = computed(() => [
    { title: t('deviceList.deviceId'), key: 'device_id' },
    { title: t('deviceList.name'), key: 'name' },
    { title: t('deviceList.protocol'), key: 'protocol' },
    { title: t('common.status'), key: 'status', render: (row: any) => row.error ? t('common.error') : t('common.ok') },
  ])

  // ─── Validation rules ───
  const createRules = computed(() => ({
    device_id: { required: true, message: t('deviceList.deviceIdRequired'), trigger: 'blur' },
    name: { required: true, message: t('deviceList.nameRequired'), trigger: 'blur' },
    protocol: { required: true, message: t('deviceList.protocolRequired'), trigger: 'change' },
  }))
  const editRules = computed(() => ({
    name: { required: true, message: t('deviceList.nameRequired'), trigger: 'blur' },
  }))
  const simFormRules = computed(() => ({
    device_id: { required: true, message: t('deviceList.deviceIdRequired'), trigger: 'blur' },
    name: { required: true, message: t('deviceList.nameRequired'), trigger: 'blur' },
  }))
  const shareRules = computed(() => ({
    target_user_id: { required: true, message: t('deviceList.selectUser'), trigger: 'change' },
  }))
  const transferRules = computed(() => ({
    target_user_id: { required: true, message: t('deviceList.selectUser'), trigger: 'change' },
  }))

  // ─── Table columns ───
  const columns = computed(() => [
    { type: 'selection' as const },
    { title: t('deviceList.deviceId'), key: 'device_id', width: 150, sorter: true },
    { title: t('deviceList.name'), key: 'name', width: 150 },
    { title: t('deviceList.protocol'), key: 'protocol', width: 120 },
    { title: t('deviceList.status'), key: 'status', width: 100 },
    { title: t('deviceList.collectInterval'), key: 'collect_interval', width: 100 },
    { title: t('common.actions'), key: 'actions', width: 200, fixed: 'right' as const },
  ])

  // ─── Fetch devices ───
  async function fetchDevices() {
    loading.value = true
    try {
      const params: any = { page: pagination.page, size: pagination.pageSize }
      if (searchText.value) params.search = searchText.value
      if (filterStatus.value) params.status = filterStatus.value
      if (filterProtocol.value) params.protocol = filterProtocol.value
      if (collectFilter.value !== 'all') params.collecting = collectFilter.value === 'collecting'
      const resp = await deviceApi.list(params)
      devices.value = resp.data || []
      pagination.itemCount = resp.total || 0
    } catch (e) {
      message.error(extractError(e))
    } finally {
      loading.value = false
    }
  }

  // ─── Protocol change handler ───
  function onProtocolChange(protocol: string) {
    const cfg = PROTOCOL_CONFIGS.value[protocol]
    if (cfg) {
      const defaults: Record<string, any> = {}
      cfg.configFields.forEach(f => {
        if (f.default !== undefined) defaults[f.key] = f.default
      })
      createForm.config = { ...defaults, ...createForm.config }
    }
  }

  // ─── Create ───
  async function handleCreate(protocolConfig?: any) {
    try {
      await createFormRef.value?.validate()
    } catch { return }
    creating.value = true
    try {
      if (protocolConfig && typeof protocolConfig === 'object') {
        createForm.config = { ...createForm.config, ...protocolConfig }
      }
      await deviceApi.create({ ...createForm })
      message.success(t('common.createSuccess'))
      showCreateModal.value = false
      clearCreateDraft()
      resetCreateForm()
      await fetchDevices()
    } catch (e) {
      message.error(extractError(e))
    } finally {
      creating.value = false
    }
  }

  // ─── Edit ───
  function handleEdit(row: Device) {
    Object.assign(editForm, JSON.parse(JSON.stringify(row)))
    showEditModal.value = true
  }
  async function handleEditSubmit(protocolConfig?: any, protocolPoints?: any) {
    try {
      await editFormRef.value?.validate()
    } catch { return }
    editSaving.value = true
    try {
      if (protocolConfig && typeof protocolConfig === 'object') {
        editForm.config = { ...editForm.config, ...protocolConfig }
      }
      if (protocolPoints && Array.isArray(protocolPoints)) {
        editForm.points = protocolPoints
      }
      await deviceApi.update(editForm.device_id, { ...editForm })
      message.success(t('common.saveSuccess'))
      showEditModal.value = false
      await fetchDevices()
    } catch (e) {
      message.error(extractError(e))
    } finally {
      editSaving.value = false
    }
  }

  // ─── Batch operations ───
  async function handleBatchDelete() {
    if (!checkedKeys.value.length) return
    dialog.warning({
      title: t('common.confirm'),
      content: t('deviceList.batchDeleteConfirm', { count: checkedKeys.value.length }),
      positiveText: t('common.delete'),
      negativeText: t('common.cancel'),
      onPositiveClick: async () => {
        batchDeleteLoading.value = true
        try {
          await Promise.all(checkedKeys.value.map(id => deviceApi.delete(id)))
          message.success(t('common.deleteSuccess'))
          checkedKeys.value = []
          await fetchDevices()
        } catch (e) {
          message.error(extractError(e))
        } finally {
          batchDeleteLoading.value = false
        }
      },
    })
  }

  async function handleBatchStartCollect() {
    if (!checkedKeys.value.length) return
    batchCollectLoading.value = true
    try {
      await deviceApi.batchStartCollect(checkedKeys.value)
      message.success(t('deviceList.batchStartSuccess'))
      await fetchDevices()
    } catch (e) {
      message.error(extractError(e))
    } finally {
      batchCollectLoading.value = false
    }
  }

  async function handleBatchStopCollect() {
    if (!checkedKeys.value.length) return
    batchCollectLoading.value = true
    try {
      await deviceApi.batchStopCollect(checkedKeys.value)
      message.success(t('deviceList.batchStopSuccess'))
      await fetchDevices()
    } catch (e) {
      message.error(extractError(e))
    } finally {
      batchCollectLoading.value = false
    }
  }

  async function handleBatchDeploy() {
    if (!checkedKeys.value.length || !deployTemplateId.value) return
    deploying.value = true
    try {
      await deviceApi.batchDeploy(deployTemplateId.value, checkedKeys.value)
      message.success(t('deviceList.batchDeploySuccess'))
      showDeployModal.value = false
      await fetchDevices()
    } catch (e) {
      message.error(extractError(e))
    } finally {
      deploying.value = false
    }
  }

  // ─── Discovery ───
  async function handleDiscover() {
    discovering.value = true
    discoverResults.value = []
    try {
      const data = await deviceApi.discover({ protocol: discoverProtocol.value, host: discoverHost.value, port: discoverPort.value })
      discoverResults.value = data || []
    } catch (e) {
      message.error(extractError(e))
    } finally {
      discovering.value = false
    }
  }

  async function handleAddDiscovered() {
    if (!selectedDiscoverKeys.value.length) return
    addingDevices.value = true
    try {
      const selected = discoverResults.value.filter(r => selectedDiscoverKeys.value.includes(r.device_id || r.address))
      await Promise.all(selected.map(s => deviceApi.create(s)))
      message.success(t('deviceList.addDiscoveredSuccess'))
      showDiscoverModal.value = false
      selectedDiscoverKeys.value = []
      await fetchDevices()
    } catch (e) {
      message.error(extractError(e))
    } finally {
      addingDevices.value = false
    }
  }

  // ─── Connection test ───
  async function handleTestConnection(deviceId?: string) {
    const protocol = createForm.protocol
    const config = createForm.config || {}
    if (!protocol) return
    testingConnection.value = true
    connectionTestResult.value = null
    try {
      const result = await deviceApi.testConnection(protocol, config)
      connectionTestResult.value = { ok: result?.success ?? true, success: result?.success, supported: result?.supported, message: result?.message || t('deviceList.connectionOk') }
    } catch (e) {
      connectionTestResult.value = { ok: false, message: extractError(e) }
    } finally {
      testingConnection.value = false
    }
  }

  // ─── Import/Export ───
  async function handleExport() {
    try {
      const resp = await http.get('/devices/export', { responseType: 'blob' })
      const blob = new Blob([resp.data as any], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = 'devices_export.json'
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      message.error(extractError(e))
    }
  }

  async function handleImportFileChange(file: File) {
    importing.value = true
    importProgress.value = 0
    importPreview.value = []
    importErrors.value = []
    try {
      const text = await file.text()
      const data = JSON.parse(text)
      if (!Array.isArray(data)) throw new Error('Invalid import format')
      importPreview.value = data.map((d: any) => ({ ...d, status: 'pending' }))
      importProgress.value = 50
    } catch (e: any) {
      importErrors.value.push({ error: e.message || String(e) })
      message.error(t('deviceList.importParseError'))
    } finally {
      importing.value = false
      importProgress.value = 100
    }
  }

  async function handleImportConfirm() {
    if (!importPreview.value.length) return
    importing.value = true
    importProgress.value = 0
    const total = importPreview.value.length
    try {
      const result = await deviceApi.batchImport(importPreview.value, false, importAtomicMode.value)
      const success = result?.success || 0
      const failed = result?.failed || 0
      importProgress.value = 100
      importing.value = false
      if (failed === 0) {
        message.success(t('deviceList.importSuccess', { count: success }))
        showImportModal.value = false
        await fetchDevices()
      } else {
        message.warning(t('deviceList.importPartial', { success, failed }))
        if (success > 0) await fetchDevices()
      }
    } catch (e) {
      importing.value = false
      message.error(extractError(e))
    }
  }

  async function downloadImportTemplate() {
    const template = [{ device_id: 'demo-001', name: 'Demo Device', protocol: 'modbus-tcp', config: { host: '192.168.1.1', port: 502, slave_id: 1 }, collect_interval: 5, points: [{ name: 'temperature', data_type: 'float32', unit: 'C', address: '40001', access_mode: 'rw' }] }]
    const blob = new Blob([JSON.stringify(template, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'device_import_template.json'
    a.click()
    URL.revokeObjectURL(url)
  }

  // ─── Simulator creation ───
  async function handleCreateSim() {
    try {
      await simFormRef.value?.validate()
    } catch { return }
    creating.value = true
    try {
      await deviceApi.create({ ...simForm } as DeviceCreateParams)
      message.success(t('common.createSuccess'))
      showSimModal.value = false
      simForm.device_id = ''
      simForm.name = ''
      simForm.points = []
      await fetchDevices()
    } catch (e) {
      message.error(extractError(e))
    } finally {
      creating.value = false
    }
  }

  // ─── Clone device ───
  function handleCloneDevice(row: Device) {
    createForm.device_id = row.device_id + '_clone'
    createForm.name = row.name + '_copy'
    createForm.protocol = row.protocol
    createForm.config = { ...row.config }
    createForm.collect_interval = row.collect_interval
    createForm.points = (row.points || []).map(p => ({ ...p }))
    showCreateModal.value = true
  }

  // ─── Share / Transfer ───
  function openShare() { shareForm.device_ids = [...checkedKeys.value]; showShareModal.value = true }
  async function handleShare() {
    try { await shareFormRef.value?.validate() } catch { return }
    sharing.value = true
    try {
      await http.post('/resource-shares', { resource_type: 'device', resource_ids: shareForm.device_ids, target_user_id: shareForm.target_user_id })
      message.success(t('deviceList.shareSuccess'))
      showShareModal.value = false
      checkedKeys.value = []
    } catch (e) { message.error(extractError(e)) }
    finally { sharing.value = false }
  }

  function openTransfer() { transferForm.device_ids = [...checkedKeys.value]; showTransferModal.value = true }
  async function handleTransfer() {
    try { await transferFormRef.value?.validate() } catch { return }
    transferring.value = true
    try {
      await http.post('/resource-shares/transfer', { resource_type: 'device', resource_ids: transferForm.device_ids, target_user_id: transferForm.target_user_id })
      message.success(t('deviceList.transferSuccess'))
      showTransferModal.value = false
      checkedKeys.value = []
      await fetchDevices()
    } catch (e) { message.error(extractError(e)) }
    finally { transferring.value = false }
  }

  // ─── Draft management ───
  const DRAFT_KEY = 'edgelite_device_create_draft'
  function scheduleDraftSave() {
    const draft = { ...createForm, savedAt: Date.now() }
    try { sessionStorage.setItem(DRAFT_KEY, JSON.stringify(draft)) } catch { /* ignore */ }
  }
  function loadCreateDraft(): any | null {
    try {
      const raw = sessionStorage.getItem(DRAFT_KEY)
      return raw ? JSON.parse(raw) : null
    } catch { return null }
  }
  function clearCreateDraft() {
    try { sessionStorage.removeItem(DRAFT_KEY) } catch { /* ignore */ }
  }
  function resetCreateForm() {
    createForm.device_id = ''
    createForm.name = ''
    createForm.protocol = ''
    createForm.config = {}
    createForm.collect_interval = 5
    createForm.points = []
  }

  // Auto-save draft when creating
  watch(showCreateModal, (v) => { if (v) scheduleDraftSave() })
  watch(createForm, () => { if (showCreateModal.value) scheduleDraftSave() }, { deep: true })

  // ─── WebSocket ───
  function onWsMessage(data: any) {
    if (data?.type === 'device_status' && data?.device_id) {
      const idx = devices.value.findIndex(d => d.device_id === data.device_id)
      if (idx >= 0) {
        devices.value[idx].status = data.status
      }
    }
  }

  _wsStatusHandler = (status, reason) => {
    wsConnected.value = status === 'connected'
    wsReconnecting.value = status === 'reconnecting'
  }

  onMounted(() => {
    fetchDevices()
    connect('device', onWsMessage)
    if (_wsStatusHandler) onStatus('device', _wsStatusHandler)
  })

  onUnmounted(() => {
    disconnect('device', onWsMessage)
    if (_wsStatusHandler) offStatus('device', _wsStatusHandler)
  })

  return {
    // Core state
    devices, loading, searchText, filterStatus, filterProtocol, collectFilter,
    wsConnected, wsReconnecting, checkedKeys, pagination, columns, activeFilterCount,
    // Modals
    showCreateModal, showEditModal, showSimModal, showDiscoverModal, showImportModal,
    showDeployModal, showShareModal, showTransferModal,
    // Forms
    createForm, editForm, simForm, shareForm, transferForm,
    // Loading flags
    creating, editSaving, discovering, addingDevices, importing, importProgress,
    deploying, batchCollectLoading, batchDeleteLoading, sharing, transferring,
    // Connection
    testingConnection, connectionTestResult,
    // Discovery
    discoverHost, discoverPort, discoverProtocol, discoverResults, selectedDiscoverKeys,
    importPreview, importErrors, importAtomicMode, importPreviewColumns,
    // Selects
    deployTemplateId, deployTemplateOptions, protocolOptions, statusOptions,
    discoverProtocolOptions, dataTypeOptions, simModeOptions, userOptions,
    // Refs
    createFormRef, editFormRef, simFormRef, protocolFormRef, protocolEditRef,
    shareFormRef, transferFormRef,
    // Schemas
    driverSchemas,
    // Rules
    createRules, editRules, simFormRules, shareRules, transferRules, discoverColumns,
    // Methods
    fetchDevices, onProtocolChange, handleCreate, handleEditSubmit, handleEdit,
    handleShare, openShare, handleTransfer, openTransfer,
    handleBatchDelete, handleBatchStartCollect, handleBatchStopCollect, handleBatchDeploy,
    handleDiscover, handleAddDiscovered, handleExport, handleImportFileChange, handleImportConfirm, handleCreateSim,
    downloadImportTemplate, resetFilters, handleTestConnection,
    // Draft
    loadCreateDraft, clearCreateDraft, scheduleDraftSave, resetCreateForm,
    // Tags
    selectedTags, tagOptions, filteredDevicesByTag, getDeviceTags,
    // Clone
    handleCloneDevice,
    // Compare
    showCompareModal, compareDeviceAId, compareDeviceBId, compareLoading,
    compareDeviceA, compareDeviceB, compareDeviceOptions, compareRows, compareDiffCount,
    openCompareModal, handleCompare,
  }
}
