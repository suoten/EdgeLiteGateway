<template>
  <n-space vertical :size="16">
    <n-card :title="`设备: ${device?.name ?? ''}`">
      <n-descriptions label-placement="left" :column="2" bordered>
        <n-descriptions-item label="设备ID">{{ device?.device_id }}</n-descriptions-item>
        <n-descriptions-item label="协议">{{ device?.protocol }}</n-descriptions-item>
        <n-descriptions-item label="状态">
          <n-tag :type="statusColor[device?.status ?? ''] || 'default'" size="small">{{ device?.status }}</n-tag>
        </n-descriptions-item>
        <n-descriptions-item label="采集间隔">{{ device?.collect_interval }}s</n-descriptions-item>
        <n-descriptions-item label="创建时间">{{ device?.created_at }}</n-descriptions-item>
        <n-descriptions-item label="更新时间">{{ device?.updated_at }}</n-descriptions-item>
      </n-descriptions>
    </n-card>

    <n-card title="测点定义">
      <n-data-table :columns="pointColumns" :data="device?.points ?? []" :bordered="false" size="small" />
    </n-card>

    <n-card title="实时数据">
      <n-space vertical>
        <n-button @click="fetchPoints" :loading="pointsLoading">刷新</n-button>
        <n-descriptions v-if="pointValues" label-placement="left" :column="2" bordered>
          <n-descriptions-item v-for="(val, key) in pointValues" :key="key" :label="String(key)">
            {{ val?.value ?? val }} <n-text v-if="val?.quality" depth="3" style="font-size: 12px">({{ val.quality }})</n-text>
          </n-descriptions-item>
        </n-descriptions>
        <n-text v-else depth="3">点击刷新获取实时数据</n-text>
      </n-space>
    </n-card>
  </n-space>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { useMessage } from 'naive-ui'
import { deviceApi, type Device } from '@/api'

const route = useRoute()
const message = useMessage()
const device = ref<Device | null>(null)
const pointValues = ref<Record<string, any> | null>(null)
const pointsLoading = ref(false)

const statusColor: Record<string, any> = { online: 'success', offline: 'default', unknown: 'warning' }

const pointColumns = [
  { title: '名称', key: 'name' },
  { title: '数据类型', key: 'data_type' },
  { title: '单位', key: 'unit' },
  { title: '地址', key: 'address' },
  { title: '访问模式', key: 'access_mode' },
  { title: '最小值', key: 'min', render: (r: any) => r.min ?? '-' },
  { title: '最大值', key: 'max', render: (r: any) => r.max ?? '-' },
]

async function fetchDevice() {
  try {
    device.value = await deviceApi.get(route.params.id as string)
  } catch (e: any) {
    message.error('获取设备详情失败')
  }
}

async function fetchPoints() {
  pointsLoading.value = true
  try {
    pointValues.value = await deviceApi.getPoints(route.params.id as string)
  } catch (e: any) {
    message.error('获取实时数据失败')
  } finally {
    pointsLoading.value = false
  }
}

onMounted(fetchDevice)
</script>
