<template>
  <n-space vertical :size="16">
    <n-space justify="space-between">
      <n-space>
        <n-select v-model:value="filterStatus" :options="statusOptions" placeholder="状态筛选" clearable style="width: 120px" @update:value="fetchDevices" />
        <n-select v-model:value="filterProtocol" :options="protocolOptions" placeholder="协议筛选" clearable style="width: 140px" @update:value="fetchDevices" />
      </n-space>
      <n-space>
        <n-button type="primary" @click="showCreateModal = true">创建设备</n-button>
        <n-button @click="showSimModal = true">创建模拟器</n-button>
      </n-space>
    </n-space>

    <n-data-table :columns="columns" :data="devices" :loading="loading" :pagination="pagination" :row-key="(r: Device) => r.device_id" />

    <!-- 创建设备弹窗 -->
    <n-modal v-model:show="showCreateModal" title="创建设备" preset="card" style="width: 600px">
      <n-form :model="createForm" label-placement="left" label-width="80">
        <n-form-item label="设备ID"><n-input v-model:value="createForm.device_id" /></n-form-item>
        <n-form-item label="名称"><n-input v-model:value="createForm.name" /></n-form-item>
        <n-form-item label="协议">
          <n-select v-model:value="createForm.protocol" :options="protocolOptions.filter(o => o.value !== 'simulator')" />
        </n-form-item>
        <n-form-item label="采集间隔"><n-input-number v-model:value="createForm.collect_interval" :min="1" /></n-form-item>
      </n-form>
      <template #action>
        <n-button @click="showCreateModal = false">取消</n-button>
        <n-button type="primary" :loading="creating" @click="handleCreate">创建</n-button>
      </template>
    </n-modal>

    <!-- 创建模拟器弹窗 -->
    <n-modal v-model:show="showSimModal" title="创建模拟设备" preset="card" style="width: 600px">
      <n-form :model="simForm" label-placement="left" label-width="80">
        <n-form-item label="设备ID"><n-input v-model:value="simForm.device_id" /></n-form-item>
        <n-form-item label="名称"><n-input v-model:value="simForm.name" /></n-form-item>
        <n-form-item label="采集间隔"><n-input-number v-model:value="simForm.collect_interval" :min="1" /></n-form-item>
      </n-form>
      <template #action>
        <n-button @click="showSimModal = false">取消</n-button>
        <n-button type="primary" :loading="creating" @click="handleCreateSim">创建</n-button>
      </template>
    </n-modal>
  </n-space>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, h } from 'vue'
import { useRouter } from 'vue-router'
import { NButton, NTag, NSpace, useMessage, useDialog } from 'naive-ui'
import { deviceApi, type Device } from '@/api'

const router = useRouter()
const message = useMessage()
const dialog = useDialog()

const devices = ref<Device[]>([])
const loading = ref(false)
const filterStatus = ref<string | null>(null)
const filterProtocol = ref<string | null>(null)
const showCreateModal = ref(false)
const showSimModal = ref(false)
const creating = ref(false)

const pagination = reactive({ page: 1, pageSize: 20, itemCount: 0, onChange: (p: number) => { pagination.page = p; fetchDevices() } })

const statusOptions = [
  { label: '在线', value: 'online' },
  { label: '离线', value: 'offline' },
  { label: '未知', value: 'unknown' },
]

const protocolOptions = [
  { label: 'Modbus TCP', value: 'modbus_tcp' },
  { label: '模拟器', value: 'simulator' },
  { label: 'MQTT', value: 'mqtt' },
  { label: 'HTTP', value: 'http' },
  { label: '视频', value: 'video' },
]

const statusColor: Record<string, any> = { online: 'success', offline: 'default', unknown: 'warning' }

const columns = [
  { title: '设备ID', key: 'device_id', width: 180 },
  { title: '名称', key: 'name', width: 150 },
  { title: '协议', key: 'protocol', width: 120 },
  {
    title: '状态', key: 'status', width: 80,
    render: (row: Device) => h(NTag, { type: statusColor[row.status] || 'default', size: 'small' }, { default: () => row.status }),
  },
  { title: '测点数', key: 'points', width: 80, render: (row: Device) => row.points?.length ?? 0 },
  { title: '采集间隔', key: 'collect_interval', width: 90, render: (row: Device) => `${row.collect_interval}s` },
  { title: '创建时间', key: 'created_at', width: 180 },
  {
    title: '操作', key: 'actions', width: 180,
    render: (row: Device) =>
      h(NSpace, null, {
        default: () => [
          h(NButton, { text: true, type: 'primary', onClick: () => router.push(`/devices/${row.device_id}`) }, { default: () => '详情' }),
          h(NButton, { text: true, type: 'error', onClick: () => handleDelete(row) }, { default: () => '删除' }),
        ],
      }),
  },
]

const createForm = reactive({ device_id: '', name: '', protocol: 'modbus_tcp', collect_interval: 5, points: [{ name: 'value', data_type: 'float32', unit: '', address: '0', access_mode: 'r' }] })
const simForm = reactive({ device_id: '', name: '', collect_interval: 5, points: [{ name: 'temperature', data_type: 'float32', unit: '°C', address: '0', access_mode: 'r', min: 15, max: 35, mode: 'sine' }] })

async function fetchDevices() {
  loading.value = true
  try {
    const data = await deviceApi.list({ page: pagination.page, size: pagination.pageSize, status: filterStatus.value ?? undefined, protocol: filterProtocol.value ?? undefined })
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
