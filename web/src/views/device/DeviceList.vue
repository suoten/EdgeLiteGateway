<template>
  <n-space vertical :size="16">
    <n-space justify="space-between">
      <n-space>
        <n-input v-model:value="searchText" placeholder="搜索设备名称/ID" clearable style="width: 200px" />
        <n-select v-model:value="filterStatus" :options="statusOptions" placeholder="状态筛选" clearable style="width: 120px" @update:value="fetchDevices" />
        <n-select v-model:value="filterProtocol" :options="protocolOptions" placeholder="协议筛选" clearable style="width: 140px" @update:value="fetchDevices" />
      </n-space>
      <n-space>
        <n-button type="primary" @click="showCreateModal = true">创建设备</n-button>
        <n-button @click="showSimModal = true">创建模拟器</n-button>
        <n-button @click="handleDiscover" :loading="discovering">设备发现</n-button>
      </n-space>
    </n-space>

    <n-data-table
      :columns="columns" :data="filteredDevices" :loading="loading"
      :pagination="pagination" :row-key="(r: Device) => r.device_id"
      v-model:checked-row-keys="checkedKeys"
    />

    <!-- 创建设备弹窗 -->
    <n-modal v-model:show="showCreateModal" title="创建设备" preset="card" style="width: 720px">
      <n-form :model="createForm" label-placement="left" label-width="90" :rules="createRules" ref="createFormRef">
        <n-grid :cols="2" :x-gap="16">
          <n-gi>
            <n-form-item label="设备ID" path="device_id"><n-input v-model:value="createForm.device_id" placeholder="唯一标识，如 modbus-plc-01" /></n-form-item>
          </n-gi>
          <n-gi>
            <n-form-item label="名称" path="name"><n-input v-model:value="createForm.name" placeholder="设备显示名称" /></n-form-item>
          </n-gi>
          <n-gi>
            <n-form-item label="协议" path="protocol">
              <n-select v-model:value="createForm.protocol" :options="protocolOptions.filter(o => o.value !== 'simulator')" @update:value="onProtocolChange" />
            </n-form-item>
          </n-gi>
          <n-gi>
            <n-form-item label="采集间隔" path="collect_interval"><n-input-number v-model:value="createForm.collect_interval" :min="1" :max="3600" /> 秒</n-form-item>
          </n-gi>
        </n-grid>

        <!-- 协议专业配置 -->
        <n-divider>连接配置</n-divider>
        <n-grid :cols="2" :x-gap="16">
          <n-gi v-if="createForm.protocol === 'modbus_tcp'">
            <n-form-item label="主机"><n-input v-model:value="createForm.config.host" placeholder="192.168.1.100" /></n-form-item>
          </n-gi>
          <n-gi v-if="createForm.protocol === 'modbus_tcp'">
            <n-form-item label="端口"><n-input-number v-model:value="createForm.config.port" :min="1" :max="65535" /></n-form-item>
          </n-gi>
          <n-gi v-if="createForm.protocol === 'modbus_tcp'">
            <n-form-item label="Unit ID"><n-input-number v-model:value="createForm.config.unit_id" :min="0" :max="255" /></n-form-item>
          </n-gi>
          <n-gi v-if="createForm.protocol === 'modbus_tcp'">
            <n-form-item label="超时(ms)"><n-input-number v-model:value="createForm.config.timeout" :min="100" :max="30000" /></n-form-item>
          </n-gi>

          <n-gi v-if="createForm.protocol === 'opcua'">
            <n-form-item label="端点URL"><n-input v-model:value="createForm.config.endpoint" placeholder="opc.tcp://192.168.1.100:4840" /></n-form-item>
          </n-gi>
          <n-gi v-if="createForm.protocol === 'opcua'">
            <n-form-item label="安全模式">
              <n-select v-model:value="createForm.config.security_mode" :options="opcuaSecurityOptions" />
            </n-form-item>
          </n-gi>
          <n-gi v-if="createForm.protocol === 'opcua'">
            <n-form-item label="用户名"><n-input v-model:value="createForm.config.username" placeholder="匿名留空" /></n-form-item>
          </n-gi>
          <n-gi v-if="createForm.protocol === 'opcua'">
            <n-form-item label="密码"><n-input v-model:value="createForm.config.password" type="password" /></n-form-item>
          </n-gi>

          <n-gi v-if="createForm.protocol === 'mqtt'">
            <n-form-item label="Broker"><n-input v-model:value="createForm.config.broker" placeholder="localhost" /></n-form-item>
          </n-gi>
          <n-gi v-if="createForm.protocol === 'mqtt'">
            <n-form-item label="端口"><n-input-number v-model:value="createForm.config.port" :min="1" :max="65535" /></n-form-item>
          </n-gi>
          <n-gi v-if="createForm.protocol === 'mqtt'">
            <n-form-item label="订阅主题"><n-input v-model:value="createForm.config.topic" placeholder="device/data/+" /></n-form-item>
          </n-gi>
          <n-gi v-if="createForm.protocol === 'mqtt'">
            <n-form-item label="QoS">
              <n-select v-model:value="createForm.config.qos" :options="mqttQosOptions" />
            </n-form-item>
          </n-gi>

          <n-gi v-if="createForm.protocol === 'http'">
            <n-form-item label="Webhook路径"><n-input v-model:value="createForm.config.path" placeholder="/push" /></n-form-item>
          </n-gi>
          <n-gi v-if="createForm.protocol === 'http'">
            <n-form-item label="数据格式">
              <n-select v-model:value="createForm.config.format" :options="httpFormatOptions" />
            </n-form-item>
          </n-gi>
        </n-grid>

        <!-- 测点定义 -->
        <n-divider>测点定义</n-divider>
        <n-space vertical>
          <n-space v-for="(pt, i) in createForm.points" :key="i" align="center">
            <n-input v-model:value="pt.name" placeholder="名称" style="width: 100px" />
            <n-select v-model:value="pt.data_type" :options="dataTypeOptions" placeholder="类型" style="width: 100px" />
            <n-input v-model:value="pt.address" placeholder="地址" style="width: 80px" />
            <n-input v-model:value="pt.unit" placeholder="单位" style="width: 60px" />
            <n-select v-model:value="pt.access_mode" :options="accessModeOptions" style="width: 80px" />
            <n-button text type="error" @click="createForm.points.splice(i, 1)">删除</n-button>
          </n-space>
          <n-button dashed @click="createForm.points.push({ name: '', data_type: 'float32', unit: '', address: '0', access_mode: 'r' })">添加测点</n-button>
        </n-space>
      </n-form>
      <template #action>
        <n-button @click="showCreateModal = false">取消</n-button>
        <n-button type="primary" :loading="creating" @click="handleCreate">创建</n-button>
      </template>
    </n-modal>

    <!-- 创建模拟器弹窗 -->
    <n-modal v-model:show="showSimModal" title="创建模拟设备" preset="card" style="width: 600px">
      <n-form :model="simForm" label-placement="left" label-width="90">
        <n-form-item label="设备ID"><n-input v-model:value="simForm.device_id" placeholder="sim-device-01" /></n-form-item>
        <n-form-item label="名称"><n-input v-model:value="simForm.name" placeholder="模拟设备" /></n-form-item>
        <n-form-item label="采集间隔"><n-input-number v-model:value="simForm.collect_interval" :min="1" /> 秒</n-form-item>
        <n-divider>测点定义</n-divider>
        <n-space vertical>
          <n-space v-for="(pt, i) in simForm.points" :key="i" align="center">
            <n-input v-model:value="pt.name" placeholder="名称" style="width: 100px" />
            <n-select v-model:value="pt.data_type" :options="dataTypeOptions" style="width: 100px" />
            <n-input v-model:value="pt.unit" placeholder="单位" style="width: 60px" />
            <n-input-number v-model:value="pt.min" placeholder="最小" style="width: 80px" />
            <n-input-number v-model:value="pt.max" placeholder="最大" style="width: 80px" />
            <n-select v-model:value="pt.mode" :options="simModeOptions" style="width: 110px" />
            <n-button text type="error" @click="simForm.points.splice(i, 1)">删除</n-button>
          </n-space>
          <n-button dashed @click="simForm.points.push({ name: '', data_type: 'float32', unit: '', address: '0', access_mode: 'r', min: 0, max: 100, mode: 'sine' })">添加测点</n-button>
        </n-space>
      </n-form>
      <template #action>
        <n-button @click="showSimModal = false">取消</n-button>
        <n-button type="primary" :loading="creating" @click="handleCreateSim">创建</n-button>
      </template>
    </n-modal>
  </n-space>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, h } from 'vue'
import { useRouter } from 'vue-router'
import { NButton, NTag, NSpace, NTooltip, useMessage, useDialog } from 'naive-ui'
import { deviceApi, type Device } from '@/api'

const router = useRouter()
const message = useMessage()
const dialog = useDialog()

const devices = ref<Device[]>([])
const loading = ref(false)
const searchText = ref('')
const filterStatus = ref<string | null>(null)
const filterProtocol = ref<string | null>(null)

const filteredDevices = computed(() => {
  if (!searchText.value) return devices.value
  const q = searchText.value.toLowerCase()
  return devices.value.filter((d: Device) =>
    d.name?.toLowerCase().includes(q) || d.device_id?.toLowerCase().includes(q)
  )
})
const showCreateModal = ref(false)
const showSimModal = ref(false)
const creating = ref(false)
const discovering = ref(false)
const checkedKeys = ref<string[]>([])

const pagination = reactive({ page: 1, pageSize: 20, itemCount: 0, onChange: (p: number) => { pagination.page = p; fetchDevices() } })

const statusOptions = [
  { label: '在线', value: 'online' },
  { label: '离线', value: 'offline' },
  { label: '未知', value: 'unknown' },
]

const protocolOptions = [
  { label: 'Modbus TCP', value: 'modbus_tcp' },
  { label: 'OPC-UA', value: 'opcua' },
  { label: 'MQTT', value: 'mqtt' },
  { label: 'HTTP', value: 'http' },
  { label: '模拟器', value: 'simulator' },
  { label: '视频', value: 'video' },
]

const dataTypeOptions = [
  { label: 'BOOL', value: 'bool' },
  { label: 'INT16', value: 'int16' },
  { label: 'UINT16', value: 'uint16' },
  { label: 'FLOAT32', value: 'float32' },
  { label: 'INT32', value: 'int32' },
  { label: 'STRING', value: 'string' },
]

const accessModeOptions = [
  { label: '只读', value: 'r' },
  { label: '只写', value: 'w' },
  { label: '读写', value: 'rw' },
]

const simModeOptions = [
  { label: '正弦波', value: 'sine' },
  { label: '随机游走', value: 'random_walk' },
  { label: '均匀随机', value: 'random' },
  { label: '固定值', value: 'fixed' },
]

const opcuaSecurityOptions = [
  { label: 'None', value: 'None' },
  { label: 'Sign', value: 'Sign' },
  { label: 'SignAndEncrypt', value: 'SignAndEncrypt' },
]

const mqttQosOptions = [
  { label: 'QoS 0 (最多一次)', value: 0 },
  { label: 'QoS 1 (至少一次)', value: 1 },
  { label: 'QoS 2 (恰好一次)', value: 2 },
]

const httpFormatOptions = [
  { label: 'JSON', value: 'json' },
  { label: 'Form', value: 'form' },
]

const protocolLabel: Record<string, string> = { modbus_tcp: 'Modbus TCP', opcua: 'OPC-UA', mqtt: 'MQTT', http: 'HTTP', simulator: 'Simulator', video: 'Video' }
const statusColor: Record<string, any> = { online: 'success', offline: 'default', unknown: 'warning' }

const columns = [
  { type: 'selection' as const },
  { title: '设备ID', key: 'device_id', width: 180 },
  { title: '名称', key: 'name', width: 150 },
  {
    title: '协议', key: 'protocol', width: 120,
    render: (row: Device) => h(NTag, { size: 'small', bordered: false, type: 'info' }, { default: () => protocolLabel[row.protocol] || row.protocol }),
  },
  {
    title: '状态', key: 'status', width: 80,
    render: (row: Device) => h(NTag, { type: statusColor[row.status] || 'default', size: 'small' }, { default: () => row.status }),
  },
  { title: '测点数', key: 'points', width: 80, render: (row: Device) => row.points?.length ?? 0 },
  { title: '采集间隔', key: 'collect_interval', width: 90, render: (row: Device) => `${row.collect_interval}s` },
  { title: '创建时间', key: 'created_at', width: 180 },
  {
    title: '操作', key: 'actions', width: 200,
    render: (row: Device) =>
      h(NSpace, null, {
        default: () => [
          h(NButton, { text: true, type: 'primary', onClick: () => router.push(`/devices/${row.device_id}`) }, { default: () => '详情' }),
          h(NButton, { text: true, type: 'info', onClick: () => handleWritePoint(row) }, { default: () => '下发' }),
          h(NButton, { text: true, type: 'error', onClick: () => handleDelete(row) }, { default: () => '删除' }),
        ],
      }),
  },
]

const createRules = {
  device_id: { required: true, message: '请输入设备ID', trigger: 'blur' },
  name: { required: true, message: '请输入设备名称', trigger: 'blur' },
  protocol: { required: true, message: '请选择协议', trigger: 'change' },
}

const defaultConfig: Record<string, any> = {
  modbus_tcp: { host: '192.168.1.100', port: 502, unit_id: 1, timeout: 3000 },
  opcua: { endpoint: 'opc.tcp://localhost:4840', security_mode: 'None', username: '', password: '' },
  mqtt: { broker: 'localhost', port: 1883, topic: 'device/data/+', qos: 0, username: '', password: '' },
  http: { path: '/push', format: 'json' },
  video: { endpoint: '', api_key: '' },
}

function onProtocolChange(val: string) {
  createForm.config = { ...(defaultConfig[val] || {}) }
}

const createForm = reactive({
  device_id: '', name: '', protocol: 'modbus_tcp', collect_interval: 5,
  config: { ...defaultConfig.modbus_tcp },
  points: [{ name: 'value', data_type: 'float32', unit: '', address: '0', access_mode: 'r' }],
})

const simForm = reactive({
  device_id: '', name: '', collect_interval: 5,
  points: [{ name: 'temperature', data_type: 'float32', unit: '°C', address: '0', access_mode: 'r', min: 15, max: 35, mode: 'sine' }],
})

async function fetchDevices() {
  loading.value = true
  try {
    const data = await deviceApi.list({
      page: pagination.page, size: pagination.pageSize,
      status: filterStatus.value ?? undefined, protocol: filterProtocol.value ?? undefined,
    })
    devices.value = data.data
    pagination.itemCount = data.total
  } catch (e: any) {
    message.error(e?.message || '获取设备列表失败')
  } finally {
    loading.value = false
  }
}

async function handleCreate() {
  creating.value = true
  try {
    await deviceApi.create(createForm as any)
    message.success('设备创建成功')
    showCreateModal.value = false
    fetchDevices()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || '创建失败')
  } finally {
    creating.value = false
  }
}

async function handleCreateSim() {
  creating.value = true
  try {
    await deviceApi.createSimulator(simForm as any)
    message.success('模拟设备创建成功')
    showSimModal.value = false
    fetchDevices()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || '创建失败')
  } finally {
    creating.value = false
  }
}

async function handleDiscover() {
  discovering.value = true
  try {
    const result = await deviceApi.discover({ protocol: filterProtocol.value || 'modbus_tcp', host: '192.168.1.0', port: 502 })
    if (result && result.length > 0) {
      message.success(`发现 ${result.length} 个设备`)
    } else {
      message.info('未发现新设备')
    }
    fetchDevices()
  } catch (e: any) {
    message.error(e?.message || '设备发现失败')
  } finally {
    discovering.value = false
  }
}

function handleWritePoint(row: Device) {
  router.push({ name: 'DeviceDetail', params: { id: row.device_id }, query: { tab: 'write' } })
}

function handleDelete(row: Device) {
  dialog.warning({
    title: '确认删除',
    content: `确定删除设备 "${row.name}" (${row.device_id})？`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await deviceApi.delete(row.device_id)
        message.success('删除成功')
        fetchDevices()
      } catch (e: any) {
        message.error(e?.response?.data?.detail || '删除失败')
      }
    },
  })
}

onMounted(fetchDevices)
</script>
