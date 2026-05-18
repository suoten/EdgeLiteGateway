<template>
  <n-space vertical :size="16">
    <n-space justify="space-between">
      <n-space>
        <n-input v-model:value="searchText" :placeholder="t('deviceList.searchPlaceholder')" clearable style="width: 200px" @update:value="() => { pagination.page = 1; fetchDevices() }" />
        <n-select v-model:value="filterStatus" :options="statusOptions" :placeholder="t('deviceList.statusFilter')" clearable style="width: 120px" @update:value="() => { pagination.page = 1; fetchDevices() }" />
        <n-select v-model:value="filterProtocol" :options="protocolOptions" :placeholder="t('deviceList.protocolFilter')" clearable style="width: 140px" @update:value="() => { pagination.page = 1; fetchDevices() }" />
      </n-space>
      <n-space>
        <n-button v-if="checkedKeys.length" type="error" @click="handleBatchDelete">{{ t('deviceList.batchDelete') }} ({{ checkedKeys.length }})</n-button>
        <n-button type="primary" @click="showCreateModal = true">{{ t('deviceList.createDevice') }}</n-button>
        <n-button @click="showSimModal = true">{{ t('deviceList.createSimulator') }}</n-button>
        <n-input v-model:value="discoverHost" :placeholder="t('deviceList.ipPlaceholder')" size="small" style="width: 170px" />
        <n-input-number v-model:value="discoverPort" :min="1" :max="65535" size="small" style="width: 90px" />
        <n-select v-model:value="discoverProtocol" :options="discoverProtocolOptions" size="small" style="width: 130px" />
        <n-button @click="handleDiscover" :loading="discovering">{{ t('deviceList.deviceDiscover') }}</n-button>
      </n-space>
    </n-space>

    <n-data-table
      :columns="columns" :data="devices" :loading="loading"
      :pagination="pagination" :row-key="(r: Device) => r.device_id"
      v-model:checked-row-keys="checkedKeys"
    />
    <n-empty v-if="!loading && devices.length === 0" :description="t('deviceList.emptyDesc')" style="padding: 40px 0" />

    <n-modal v-model:show="showCreateModal" :title="t('deviceList.createDevice')" preset="card" style="width: 720px">
      <n-form :model="createForm" label-placement="left" label-width="90" :rules="createRules" ref="createFormRef">
        <n-grid :cols="2" :x-gap="16">
          <n-gi>
            <n-form-item :label="t('deviceList.deviceId')" path="device_id"><n-input v-model:value="createForm.device_id" :placeholder="t('deviceList.deviceIdPlaceholder')" /></n-form-item>
          </n-gi>
          <n-gi>
            <n-form-item :label="t('deviceList.name')" path="name"><n-input v-model:value="createForm.name" :placeholder="t('deviceList.namePlaceholder')" /></n-form-item>
          </n-gi>
          <n-gi>
            <n-form-item :label="t('deviceList.protocol')" path="protocol">
              <n-select v-model:value="createForm.protocol" :options="protocolOptions.filter(o => o.value !== 'simulator')" @update:value="onProtocolChange" />
            </n-form-item>
          </n-gi>
          <n-gi>
            <n-form-item :label="t('deviceList.collectInterval')" path="collect_interval">
              <n-space align="center">
                <n-input-number v-model:value="createForm.collect_interval" :min="1" :max="3600" />
                <n-text>{{ t('deviceList.seconds') }}</n-text>
                <n-tooltip trigger="hover">
                  <template #trigger><n-text depth="3" style="cursor: help">ⓘ</n-text></template>
                  {{ t('deviceList.collectIntervalHint') }}
                </n-tooltip>
              </n-space>
            </n-form-item>
          </n-gi>
        </n-grid>

        <n-alert v-if="currentProtocolDesc" type="info" :bordered="false" style="margin-bottom: 12px">{{ currentProtocolDesc }}</n-alert>

        <n-divider>{{ t('deviceList.connectionConfig') }}</n-divider>
        <n-grid :cols="2" :x-gap="16">
          <template v-for="field in currentProtocolFields" :key="field.name">
            <n-gi>
              <n-form-item :label="field.label || field.name" :path="'config.' + field.name">
                <n-tooltip v-if="field.tooltip" trigger="hover" style="margin-right: 4px">
                  <template #trigger><n-icon size="16" style="vertical-align: middle; cursor: help">ⓘ</n-icon></template>
                  {{ field.tooltip }}
                </n-tooltip>
                <n-select
                  v-if="field.options"
                  v-model:value="createForm.config[field.name]"
                  :options="field.options.map((o: any) => ({ label: String(o), value: o }))"
                  :placeholder="field.description || ''"
                />
                <n-input-number
                  v-else-if="field.type === 'integer' || field.type === 'number'"
                  v-model:value="createForm.config[field.name]"
                  :placeholder="field.description || ''"
                  :min="field.name === 'port' ? 1 : undefined"
                  :max="field.name === 'port' ? 65535 : undefined"
                />
                <n-input
                  v-else
                  v-model:value="createForm.config[field.name]"
                  :type="field.secret ? 'password' : 'text'"
                  :placeholder="field.description || field.default?.toString() || ''"
                />
              </n-form-item>
            </n-gi>
          </template>
          <n-gi v-if="currentProtocolFields.length === 0">
            <n-text depth="3">{{ t('deviceList.noConfigNeeded') }}</n-text>
          </n-gi>
        </n-grid>

        <n-divider>{{ t('deviceList.pointDefinition') }}</n-divider>
        <n-space vertical>
          <n-space v-for="(pt, i) in createForm.points" :key="i" align="center">
            <n-input v-model:value="pt.name" :placeholder="t('deviceList.name')" style="width: 100px" />
            <n-select v-model:value="pt.data_type" :options="dataTypeOptions" style="width: 100px" />
            <n-input v-model:value="pt.address" placeholder="Address" style="width: 80px" />
            <n-input v-model:value="pt.unit" placeholder="Unit" style="width: 60px" />
            <n-select v-model:value="pt.access_mode" :options="accessModeOptions" style="width: 80px" />
            <n-button text type="error" @click="createForm.points.splice(i, 1)">{{ t('common.delete') }}</n-button>
          </n-space>
          <n-button dashed @click="createForm.points.push({ name: '', data_type: 'float32', unit: '', address: '0', access_mode: 'r' })">{{ t('deviceList.addPoint') }}</n-button>
        </n-space>
      </n-form>
      <template #action>
        <n-button @click="showCreateModal = false">{{ t('common.cancel') }}</n-button>
        <n-button type="primary" :loading="creating" @click="handleCreate">{{ t('deviceList.create') }}</n-button>
      </template>
    </n-modal>

    <n-modal v-model:show="showSimModal" :title="t('deviceList.simulatorTitle')" preset="card" style="width: 600px">
      <n-form :model="simForm" :rules="simFormRules" ref="simFormRef" label-placement="left" label-width="90">
        <n-form-item :label="t('deviceList.deviceId')" path="device_id"><n-input v-model:value="simForm.device_id" placeholder="sim-device-01" /></n-form-item>
        <n-form-item :label="t('deviceList.name')" path="name"><n-input v-model:value="simForm.name" placeholder="sim-device" /></n-form-item>
        <n-form-item :label="t('deviceList.collectInterval')"><n-input-number v-model:value="simForm.collect_interval" :min="1" /> {{ t('deviceList.seconds') }}</n-form-item>
        <n-divider>{{ t('deviceList.pointDefinition') }}</n-divider>
        <n-space vertical>
          <n-space v-for="(pt, i) in simForm.points" :key="i" align="center">
            <n-input v-model:value="pt.name" :placeholder="t('deviceList.name')" style="width: 100px" />
            <n-select v-model:value="pt.data_type" :options="dataTypeOptions" style="width: 100px" />
            <n-input v-model:value="pt.unit" placeholder="Unit" style="width: 60px" />
            <n-input-number v-model:value="pt.min" placeholder="Min" style="width: 80px" />
            <n-input-number v-model:value="pt.max" placeholder="Max" style="width: 80px" />
            <n-select v-model:value="pt.mode" :options="simModeOptions" style="width: 110px" />
            <n-button text type="error" @click="simForm.points.splice(i, 1)">{{ t('common.delete') }}</n-button>
          </n-space>
          <n-button dashed @click="simForm.points.push({ name: '', data_type: 'float32', unit: '', address: '0', access_mode: 'r', min: 0, max: 100, mode: 'sine' })">{{ t('deviceList.addPoint') }}</n-button>
        </n-space>
      </n-form>
      <template #action>
        <n-button @click="showSimModal = false">{{ t('common.cancel') }}</n-button>
        <n-button type="primary" :loading="creating" @click="handleCreateSim">{{ t('deviceList.create') }}</n-button>
      </template>
    </n-modal>

    <n-modal v-model:show="showDiscoverModal" :title="t('deviceList.discoverTitle')" preset="card" style="width: 600px">
      <n-empty v-if="discoverResults.length === 0" :description="t('deviceList.noDeviceFound')" />
      <n-data-table v-else :columns="discoverColumns" :data="discoverResults" :max-height="400" :row-key="(r: any) => r.name" v-model:checked-row-keys="selectedDiscoverKeys" />
      <template #action>
        <n-button @click="showDiscoverModal = false">{{ t('deviceList.close') }}</n-button>
        <n-button type="primary" :disabled="selectedDiscoverKeys.length === 0" :loading="addingDevices" @click="handleAddDiscovered">{{ t('deviceList.addSelected') }} ({{ selectedDiscoverKeys.length }})</n-button>
      </template>
    </n-modal>
  </n-space>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, onUnmounted, h } from 'vue'
import { useRouter } from 'vue-router'
import { NButton, NTag, NSpace, NTooltip, NPopconfirm, useMessage, useDialog } from 'naive-ui'
// FIXED: 原问题-添加i18n支持，使用项目统一的i18n导入
import { t } from '@/i18n'
import { deviceApi, driverApi, type Device } from '@/api'
import { deviceStatusLabel, deviceStatusColor, protocolLabel } from '@/utils/enumLabels'
import { PROTOCOL_CONFIGS, getProtocolConfig } from '@/constants/protocolConfig'
import * as ws from '@/api/websocket'

const router = useRouter()
const message = useMessage()
const dialog = useDialog()

const devices = ref<Device[]>([])
const loading = ref(false)
const searchText = ref('')
const filterStatus = ref<string | null>(null)
const filterProtocol = ref<string | null>(null)

async function fetchDevices() {
  loading.value = true
  try {
    const data = await deviceApi.list({
      page: pagination.page, size: pagination.pageSize,
      status: filterStatus.value ?? undefined, protocol: filterProtocol.value ?? undefined,
      search: searchText.value || undefined,
    })
    devices.value = data?.data ?? []
    pagination.itemCount = data?.total ?? 0
  } catch (e: any) {
    devices.value = []
    // FIXED: 原问题-硬编码中文消息，改为i18n
    message.error(e?.response?.data?.detail || e?.message || t('deviceList.fetchFailed'))
  } finally {
    loading.value = false
  }
}

const showCreateModal = ref(false)
const showSimModal = ref(false)
const creating = ref(false)
const discovering = ref(false)
const checkedKeys = ref<string[]>([])
const discoverHost = ref('192.168.1.*')
const discoverPort = ref(502)
const discoverProtocol = ref('modbus_tcp')
const discoverResults = ref<any[]>([])
const showDiscoverModal = ref(false)
const selectedDiscoverKeys = ref<string[]>([])
const addingDevices = ref(false)
const createFormRef = ref<any>(null)

// FIXED: 添加WebSocket device频道监听，设备列表自动刷新
let _wsDeviceTimer: ReturnType<typeof setTimeout> | null = null
function onDeviceWsMessage(data: any) {
  try {
    if (data?.device_id) {
      if (_wsDeviceTimer) clearTimeout(_wsDeviceTimer)
      _wsDeviceTimer = setTimeout(() => {
        fetchDevices()
        _wsDeviceTimer = null
      }, 500)
    }
  } catch { /* ignore */ }
}

const discoverProtocolOptions = computed(() =>
  protocolOptions.value.filter(o => o.value !== 'simulator' && o.value !== 'video')
)

const discoverColumns = [
  { type: 'selection' as const },
  { title: t('deviceList.name'), key: 'name', width: 180 },
  { title: t('deviceList.protocol'), key: 'protocol', width: 120 },
  { title: t('deviceList.host'), key: 'host', width: 140 },
  { title: t('deviceList.port'), key: 'port', width: 80 },
  { title: t('deviceList.slaveId'), key: 'slave_id', width: 80 },
]

const pagination = reactive({ page: 1, pageSize: 20, itemCount: 0, onChange: (p: number) => { pagination.page = p; fetchDevices() } })

const statusOptions = [
  { label: t('dashboard.online'), value: 'online' },
  { label: t('dashboard.offline'), value: 'offline' },
  { label: t('common.unknown'), value: 'unknown' },
]

const protocolOptions = ref<{ label: string; value: string }[]>([])

async function loadProtocols() {
  try {
    const res = await driverApi.protocols()
    const protocols = res?.protocols || []
    if (Array.isArray(protocols) && protocols.length > 0) {
      protocolOptions.value = protocols.map((p: any) => ({
        label: typeof p === 'string' ? p : (p.label || p.name || p),
        value: typeof p === 'string' ? p : (p.name || p.value || p),
      }))
    }
  } catch {
    protocolOptions.value = [
      { label: 'Modbus TCP', value: 'modbus_tcp' },
      { label: 'OPC-UA', value: 'opcua' },
      { label: 'MQTT Client', value: 'mqtt' },
      { label: 'HTTP Webhook', value: 'http' },
      { label: protocolLabel['siemens_s7'] || 'Siemens S7', value: 's7' },
      { label: protocolLabel['mitsubishi_mc'] || 'Mitsubishi MC', value: 'mc' },
      { label: protocolLabel['omron_fins'] || 'Omron FINS', value: 'fins' },
      { label: 'Allen Bradley', value: 'allen_bradley' },
      { label: 'OPC DA', value: 'opc_da' },
      { label: 'FANUC CNC', value: 'fanuc' },
      { label: 'MTConnect', value: 'mtconnect' },
      { label: 'Toledo', value: 'toledo' },
      { label: protocolLabel['serial_port'] || 'Serial', value: 'serial_port' },
      { label: protocolLabel['database_source'] || 'Database', value: 'database_source' },
      { label: protocolLabel['barcode_scanner'] || 'Scanner', value: 'barcode_scanner' },
      { label: protocolLabel['simulator'] || 'Simulator', value: 'simulator' },
      { label: protocolLabel['video'] || 'Video', value: 'video' },
      { label: 'MQTT Sparkplug B', value: 'sparkplug_b' },
      { label: 'DL/T 645', value: 'dlt645' },
      { label: 'IEC 104', value: 'iec104' },
      { label: 'KUKA', value: 'kuka' },
      { label: 'ABB', value: 'abb_robot' },
      { label: 'ONVIF', value: 'onvif' },
    ]
  }
}

const dataTypeOptions = [
  { label: 'BOOL', value: 'bool' },
  { label: 'INT16', value: 'int16' },
  { label: 'UINT16', value: 'uint16' },
  { label: 'FLOAT32', value: 'float32' },
  { label: 'INT32', value: 'int32' },
  { label: 'STRING', value: 'string' },
]

const simModeOptions = [
  { label: t('common.sineWave'), value: 'sine' },
  { label: t('common.randomWalk'), value: 'random_walk' },
  { label: t('common.uniformRandom'), value: 'random' },
  { label: t('common.fixedValue'), value: 'fixed' },
]

const accessModeOptions = [
  { label: t('common.readOnly'), value: 'r' },
  { label: t('common.writeOnly'), value: 'w' },
  { label: t('common.readWrite'), value: 'rw' },
]

const columns = [
  { type: 'selection' as const },
  { title: t('deviceList.deviceId'), key: 'device_id', width: 180 },
  { title: t('deviceList.name'), key: 'name', width: 150, sorter: true },
  {
    title: t('deviceList.protocol'), key: 'protocol', width: 120,
    render: (row: Device) => h(NTag, { size: 'small', bordered: false, type: 'info' }, { default: () => protocolLabel[row.protocol] || row.protocol }),
  },
  {
    title: t('deviceList.status'), key: 'status', width: 80,
    render: (row: Device) => h(NTag, { type: deviceStatusColor[row.status] || 'default', size: 'small' }, { default: () => deviceStatusLabel[row.status] || row.status }),
  },
  { title: t('deviceList.pointCount'), key: 'points', width: 80, render: (row: Device) => row.points?.length ?? 0 },
  { title: t('deviceList.collectInterval'), key: 'collect_interval', width: 90, render: (row: Device) => `${row.collect_interval}s` },
  { title: t('deviceList.createTime'), key: 'created_at', width: 180 },
  {
    title: t('deviceList.actions'), key: 'actions', width: 200,
    render: (row: Device) =>
      h(NSpace, null, {
        default: () => [
          h(NButton, { text: true, type: 'primary', onClick: () => router.push(`/devices/${row.device_id}`) }, { default: () => t('deviceList.detail') }),
          h(NButton, { text: true, type: 'info', onClick: () => handleWritePoint(row) }, { default: () => t('deviceList.push') }),
          h(NPopconfirm as any, { onPositiveClick: () => doDelete(row) }, {
            trigger: () => h(NButton, { text: true, type: 'error' }, { default: () => t('common.delete') }),
            default: () => t('deviceList.deleteConfirm', { name: row.name }),
          }),
        ],
      }),
  },
]

const createRules = {
  device_id: { required: true, message: t('deviceList.deviceIdRequired'), trigger: 'blur' },
  name: { required: true, message: t('deviceList.deviceNameRequired'), trigger: 'blur' },
  protocol: { required: true, message: t('deviceList.protocolRequired'), trigger: 'change' },
}

const PROTOCOL_KEY_MAP: Record<string, string> = {
  modbus_tcp: 'modbus-tcp', modbus_rtu: 'modbus-rtu', opcua: 'opcua', mqtt: 'mqtt',
  s7: 's7', mc: 'mc', fins: 'fins', allen_bradley: 'ab', http: 'http', simulator: 'simulator',
}

const driverSchemas = ref<Record<string, any>>({})

const currentProtocolFields = computed(() => {
  const schema = driverSchemas.value[createForm.protocol]
  if (schema?.fields?.length) return schema.fields
  const cfgKey = PROTOCOL_KEY_MAP[createForm.protocol]
  const cfg = cfgKey ? getProtocolConfig(cfgKey) : undefined
  if (cfg?.configFields?.length) {
    return cfg.configFields.map(f => ({
      name: f.key, label: f.label, description: f.placeholder || '', tooltip: f.tooltip || '',
      type: f.type === 'number' ? 'integer' : (f.type === 'select' ? 'string' : f.type || 'string'),
      default: f.default, options: f.options?.map(o => o.value), secret: f.key === 'password',
    }))
  }
  return []
})

const currentProtocolDesc = computed(() => {
  const cfgKey = PROTOCOL_KEY_MAP[createForm.protocol]
  const cfg = cfgKey ? getProtocolConfig(cfgKey) : undefined
  return cfg?.description || ''
})

async function loadDriverSchemas() {
  try {
    // FIXED: 原问题-设备发现硬编码协议列表，改为从driverApi.protocols()动态获取
    let protocols: string[] = []
    try {
      const res = await driverApi.protocols()
      protocols = res?.protocols || []
    } catch { /* fallback to empty */ }
    for (const p of protocols) {
      try {
        const data = await driverApi.configSchema(p)
        if (data?.config_schema) driverSchemas.value[p] = data.config_schema
      } catch { /* skip */ }
    }
  } catch { /* ignore */ }
}

const defaultConfig: Record<string, any> = {
  modbus_tcp: { host: '192.168.1.100', port: 502, slave_id: 1, timeout: 3.0 },
  opcua: { endpoint: 'opc.tcp://localhost:4840', security_mode: 'None', username: '', password: '' },
  mqtt: { broker: 'localhost', port: 1883, topic: 'device/data/+', username: '', password: '' },
  http: { path: '/webhook/data', method: 'POST' },
  s7: { host: '192.168.1.1', rack: 0, slot: 1 },
  serial_port: { port: 'COM1', baudrate: 9600, bytesize: 8, parity: 'N', stopbits: 1, protocol: 'raw' },
  database_source: { db_type: 'mysql', host: 'localhost', port: 3306, database: '', username: '', password: '' },
  barcode_scanner: { port: 'COM1', baudrate: 9600, prefix: '', suffix: '\\r' },
  video: { endpoint: '', api_key: '' },
}

function buildConfigFromTemplate(protocol: string): Record<string, any> {
  const cfgKey = PROTOCOL_KEY_MAP[protocol]
  const cfg = cfgKey ? getProtocolConfig(cfgKey) : undefined
  if (cfg?.configFields?.length) {
    const config: Record<string, any> = {}
    for (const f of cfg.configFields) {
      config[f.key] = f.default !== undefined ? f.default : ''
    }
    return config
  }
  return { ...(defaultConfig[protocol] || {}) }
}

function buildPointsFromTemplate(protocol: string) {
  const cfgKey = PROTOCOL_KEY_MAP[protocol]
  const cfg = cfgKey ? getProtocolConfig(cfgKey) : undefined
  if (cfg?.pointTemplates?.length) {
    return cfg.pointTemplates.map(pt => ({
      name: pt.name, data_type: pt.data_type, unit: pt.unit,
      address: pt.address, access_mode: pt.access_mode === 'read' ? 'r' : pt.access_mode === 'write' ? 'w' : pt.access_mode,
    }))
  }
  return [{ name: 'value', data_type: 'float32', unit: '', address: '0', access_mode: 'r' }]
}

function onProtocolChange(val: string) {
  const schema = driverSchemas.value[val]
  if (schema?.fields) {
    const config: Record<string, any> = {}
    for (const field of schema.fields) {
      config[field.name] = field.default !== undefined ? field.default : ''
    }
    createForm.config = config
  } else {
    createForm.config = buildConfigFromTemplate(val)
  }
  createForm.points = buildPointsFromTemplate(val)
}

const createForm = reactive({
  device_id: '', name: '', protocol: 'modbus_tcp', collect_interval: 5,
  config: buildConfigFromTemplate('modbus_tcp'),
  points: buildPointsFromTemplate('modbus_tcp'),
})

const simForm = reactive({
  device_id: '', name: '', collect_interval: 5,
  points: [{ name: 'temperature', data_type: 'float32', unit: '°C', address: '0', access_mode: 'r', min: 15, max: 35, mode: 'sine' }],
})
const simFormRef = ref<any>(null)
const simFormRules = {
  device_id: { required: true, message: t('deviceList.deviceIdRequired'), trigger: 'blur' },
  name: { required: true, message: t('deviceList.deviceNameRequired'), trigger: 'blur' },
}

async function handleCreate() {
  try {
    await createFormRef.value?.validate()
  } catch { return }
  const hasEmptyPoint = createForm.points.some((pt: any) => !pt.name || !pt.address)
  if (hasEmptyPoint) {
    message.error(t('deviceList.pointNameAddrRequired'))
    return
  }
  creating.value = true
  try {
    await deviceApi.create(createForm as any)
    message.success(t('deviceList.createSuccess'))
    showCreateModal.value = false
    createForm.device_id = ''
    createForm.name = ''
    createForm.protocol = 'modbus_tcp'
    createForm.collect_interval = 5
    createForm.config = buildConfigFromTemplate('modbus_tcp')
    createForm.points = buildPointsFromTemplate('modbus_tcp')
    fetchDevices()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || t('deviceList.createFailed'))
  } finally {
    creating.value = false
  }
}

async function handleCreateSim() {
  try {
    await simFormRef.value?.validate()
  } catch { return }
  const hasEmptyPoint = simForm.points.some((pt: any) => !pt.name)
  if (hasEmptyPoint) {
    message.error(t('deviceList.simPointNameRequired'))
    return
  }
  creating.value = true
  try {
    await deviceApi.createSimulator(simForm as any)
    message.success(t('deviceList.simCreateSuccess'))
    showSimModal.value = false
    simForm.device_id = ''
    simForm.name = ''
    simForm.collect_interval = 5
    simForm.points = [{ name: 'temperature', data_type: 'float32', unit: '°C', address: '0', access_mode: 'r', min: 15, max: 35, mode: 'sine' }]
    fetchDevices()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || t('deviceList.createFailed'))
  } finally {
    creating.value = false
  }
}

async function handleDiscover() {
  discovering.value = true
  try {
    const result = await deviceApi.discover({ protocol: discoverProtocol.value, host: discoverHost.value, port: discoverPort.value })
    discoverResults.value = result || []
    selectedDiscoverKeys.value = []
    showDiscoverModal.value = true
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || t('deviceList.discoverFailed'))
  } finally {
    discovering.value = false
  }
}

// FIXED: 一键添加发现的设备
async function handleAddDiscovered() {
  addingDevices.value = true
  const selected = discoverResults.value.filter(r => selectedDiscoverKeys.value.includes(r.name))
  let succeeded = 0
  let failed = 0
  try {
    for (const item of selected) {
      try {
        await deviceApi.create({
          device_id: item.name,
          name: item.name,
          protocol: item.protocol || discoverProtocol.value,
          config: { host: item.host, port: item.port, slave_id: item.slave_id },
          points: [{ name: 'value', data_type: 'float32', unit: '', address: '0', access_mode: 'r' }],
        } as any)
        succeeded++
      } catch {
        failed++
      }
    }
    if (failed > 0) {
      message.warning(t('deviceList.addResult', { success: succeeded, failed }))
    } else {
      message.success(t('deviceList.addResultAll', { success: succeeded }))
    }
    showDiscoverModal.value = false
    await fetchDevices()
  } finally {
    // FIXED: 原问题-addingDevices未在finally中重置，异常时永久卡住
    addingDevices.value = false
  }
}

function handleWritePoint(row: Device) {
  router.push({ name: 'DeviceDetail', params: { id: row.device_id }, query: { tab: 'write' } })
}

async function doDelete(row: Device) {
  try {
    await deviceApi.delete(row.device_id)
    message.success(t('deviceList.deleteSuccess'))
    fetchDevices()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || t('deviceList.deleteFailed'))
  }
}

async function handleBatchDelete() {
  if (!checkedKeys.value.length) return
  dialog.warning({
    title: t('deviceList.batchDeleteTitle'),
    content: t('deviceList.batchDeleteConfirm', { count: checkedKeys.value.length }),
    positiveText: t('common.delete'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      const results = await Promise.allSettled(checkedKeys.value.map(id => deviceApi.delete(id)))
      const succeeded = results.filter(r => r.status === 'fulfilled').length
      const failed = results.filter(r => r.status === 'rejected').length
      if (failed > 0) {
        message.warning(t('device.batchDeletePartial', { succeeded, failed }))  // FIXED: 原问题-硬编码中文，改用i18n
      } else {
        message.success(t('device.batchDeleteSuccess', { succeeded }))  // FIXED: 原问题-硬编码中文，改用i18n
      }
      checkedKeys.value = []
      fetchDevices()
    },
  })
}

onMounted(() => { fetchDevices(); loadDriverSchemas(); loadProtocols(); ws.connect('device', onDeviceWsMessage) })
onUnmounted(() => {
  if (_wsDeviceTimer) clearTimeout(_wsDeviceTimer)
  ws.disconnect('device', onDeviceWsMessage)
})
</script>
