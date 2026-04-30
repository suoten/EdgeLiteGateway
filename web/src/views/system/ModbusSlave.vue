<template>
  <n-space vertical :size="16">
    <n-card title="内置Modbus Slave" :bordered="false">
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
        <n-descriptions-item label="端口">{{ status.port || 502 }}</n-descriptions-item>
        <n-descriptions-item label="保持寄存器">{{ status.holding_size || 100 }}</n-descriptions-item>
        <n-descriptions-item label="输入寄存器">{{ status.input_size || 100 }}</n-descriptions-item>
      </n-descriptions>

      <n-empty v-else description="Modbus Slave未启动" />
    </n-card>
  </n-space>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { useMessage } from 'naive-ui'
import { modbusSlaveApi } from '@/api'

const message = useMessage()
const starting = ref(false)
const stopping = ref(false)
const status = reactive<any>({})

async function fetchStatus() {
  try {
    const data = await modbusSlaveApi.status()
    if (data) Object.assign(status, data)
  } catch (e: any) {
    message.error(e?.message || '获取状态失败')
  }
}

async function handleStart() {
  starting.value = true
  try {
    await modbusSlaveApi.start()
    message.success('Modbus Slave已启动')
    await fetchStatus()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || '启动失败')
  } finally { starting.value = false }
}

async function handleStop() {
  stopping.value = true
  try {
    await modbusSlaveApi.stop()
    message.success('Modbus Slave已停止')
    await fetchStatus()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || '停止失败')
  } finally { stopping.value = false }
}

onMounted(fetchStatus)
</script>
