<template>
  <n-space vertical :size="16">
    <n-card title="内置MQTT Server" :bordered="false">
      <template #header-extra>
        <n-space>
          <n-tag :type="status.running ? 'success' : 'default'" size="small">
            {{ status.running ? '运行中' : '已停止' }}
          </n-tag>
          <n-button v-if="!status.running" type="primary" @click="handleStart" :loading="starting">启动</n-button>
          <n-button v-else type="warning" @click="handleStop" :loading="stopping">停止</n-button>
          <n-button @click="fetchStatus">刷新</n-button>
        </n-space>
      </template>

      <n-descriptions label-placement="left" :column="2" bordered v-if="status.running">
        <n-descriptions-item label="监听地址">{{ status.host || '0.0.0.0' }}</n-descriptions-item>
        <n-descriptions-item label="TCP端口">{{ status.port || 1883 }}</n-descriptions-item>
        <n-descriptions-item label="WebSocket端口">{{ status.ws_port || 8083 }}</n-descriptions-item>
        <n-descriptions-item label="连接数">{{ status.connections || 0 }}</n-descriptions-item>
      </n-descriptions>

      <n-empty v-else description="MQTT Server未启动" />
    </n-card>
  </n-space>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { useMessage } from 'naive-ui'
import { mqttServerApi } from '@/api'

const message = useMessage()
const starting = ref(false)
const stopping = ref(false)
const status = reactive<any>({})

async function fetchStatus() {
  try {
    const data = await mqttServerApi.status()
    if (data) Object.assign(status, data)
  } catch (e: any) {
    message.error(e?.message || '获取状态失败')
  }
}

async function handleStart() {
  starting.value = true
  try {
    await mqttServerApi.start()
    message.success('MQTT Server已启动')
    await fetchStatus()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || '启动失败')
  } finally { starting.value = false }
}

async function handleStop() {
  stopping.value = true
  try {
    await mqttServerApi.stop()
    message.success('MQTT Server已停止')
    await fetchStatus()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || '停止失败')
  } finally { stopping.value = false }
}

onMounted(fetchStatus)
</script>
