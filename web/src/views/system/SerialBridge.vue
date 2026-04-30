<template>
  <n-space vertical :size="16">
    <n-card title="串口桥接" :bordered="false">
      <template #header-extra>
        <n-space>
          <n-tag :type="status.running ? 'success' : 'default'" size="small">
            {{ status.running ? '运行中' : '已停止' }}
          </n-tag>
          <n-button v-if="!status.running" type="primary" @click="handleStart" :loading="starting">启动</n-button>
          <n-button v-else type="warning" @click="handleStop" :loading="stopping">停止</n-button>
          <n-button @click="fetchStatus" :loading="loading">刷新</n-button>
        </n-space>
      </template>

      <n-descriptions label-placement="left" :column="2" bordered v-if="status.running">
        <n-descriptions-item label="监听端口">{{ status.port || '-' }}</n-descriptions-item>
        <n-descriptions-item label="串口设备">{{ status.serial_port || '-' }}</n-descriptions-item>
        <n-descriptions-item label="波特率">{{ status.baudrate || '-' }}</n-descriptions-item>
        <n-descriptions-item label="已转发">{{ status.forwarded ?? 0 }} 条</n-descriptions-item>
        <n-descriptions-item label="已接收">{{ status.received ?? 0 }} 条</n-descriptions-item>
        <n-descriptions-item label="运行时间">{{ status.uptime || '-' }}</n-descriptions-item>
      </n-descriptions>

      <n-empty v-else description="串口桥接服务未启动" />
    </n-card>
  </n-space>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { useMessage } from 'naive-ui'
import { serialBridgeApi } from '@/api'

const message = useMessage()
const loading = ref(false)
const starting = ref(false)
const stopping = ref(false)
const status = reactive<any>({})

async function fetchStatus() {
  loading.value = true
  try {
    const data = await serialBridgeApi.status()
    if (data) Object.assign(status, data)
  } catch (e: any) {
    message.error(e?.message || '获取串口桥接状态失败')
  } finally {
    loading.value = false
  }
}

async function handleStart() {
  starting.value = true
  try {
    await serialBridgeApi.start()
    message.success('串口桥接已启动')
    await fetchStatus()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || '启动失败')
  } finally {
    starting.value = false
  }
}

async function handleStop() {
  stopping.value = true
  try {
    await serialBridgeApi.stop()
    message.success('串口桥接已停止')
    await fetchStatus()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || '停止失败')
  } finally {
    stopping.value = false
  }
}

onMounted(fetchStatus)
</script>
