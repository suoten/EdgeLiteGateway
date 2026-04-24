<template>
  <n-space vertical :size="20">
    <!-- 顶部统计卡片 -->
    <n-grid :cols="4" :x-gap="16" :y-gap="16" responsive="screen">
      <n-gi>
        <n-card class="stat-card stat-card-primary" :bordered="false">
          <n-statistic label="设备总数" :value="status?.device_total ?? 0">
            <template #prefix><n-icon :component="HardwareChip" /></template>
          </n-statistic>
          <div class="stat-footer">
            <n-text depth="3">在线 {{ status?.device_online ?? 0 }} 台</n-text>
          </div>
        </n-card>
      </n-gi>
      <n-gi>
        <n-card class="stat-card stat-card-success" :bordered="false">
          <n-statistic label="规则总数" :value="status?.rule_total ?? 0">
            <template #prefix><n-icon :component="SettingsOutline" /></template>
          </n-statistic>
          <div class="stat-footer">
            <n-text depth="3">已启用 {{ status?.rule_total ?? 0 }} 条</n-text>
          </div>
        </n-card>
      </n-gi>
      <n-gi>
        <n-card class="stat-card stat-card-warning" :bordered="false">
          <n-statistic label="活跃告警" :value="status?.alarm_firing ?? 0">
            <template #prefix><n-icon :component="AlertCircleOutline" /></template>
          </n-statistic>
          <div class="stat-footer">
            <n-text v-if="status?.alarm_firing" type="warning">需要处理</n-text>
            <n-text v-else depth="3">系统正常</n-text>
          </div>
        </n-card>
      </n-gi>
      <n-gi>
        <n-card class="stat-card stat-card-info" :bordered="false">
          <n-statistic label="采集任务" :value="status?.collect_task_count ?? 0">
            <template #prefix><n-icon :component="PulseOutline" /></template>
          </n-statistic>
          <div class="stat-footer">
            <n-text depth="3">运行中</n-text>
          </div>
        </n-card>
      </n-gi>
    </n-grid>

    <!-- 系统资源监控 -->
    <n-grid :cols="3" :x-gap="16" :y-gap="16">
      <n-gi>
        <n-card title="CPU 使用率" :bordered="false" class="resource-card">
          <n-progress type="circle" :percentage="status?.cpu_percent ?? 0" :stroke-width="12" :color="cpuColor" />
          <div class="resource-info">
            <n-text depth="3">当前负载</n-text>
          </div>
        </n-card>
      </n-gi>
      <n-gi>
        <n-card title="内存使用" :bordered="false" class="resource-card">
          <n-progress type="circle" :percentage="status?.memory_percent ?? 0" :stroke-width="12" :color="memColor" />
          <div class="resource-info">
            <n-text depth="3">{{ formatBytes(status?.memory_used) }} / {{ formatBytes(status?.memory_total) }}</n-text>
          </div>
        </n-card>
      </n-gi>
      <n-gi>
        <n-card title="磁盘使用" :bordered="false" class="resource-card">
          <n-progress type="circle" :percentage="status?.disk_percent ?? 0" :stroke-width="12" :color="diskColor" />
          <div class="resource-info">
            <n-text depth="3">{{ formatBytes(status?.disk_used) }} / {{ formatBytes(status?.disk_total) }}</n-text>
          </div>
        </n-card>
      </n-gi>
    </n-grid>

    <!-- 系统信息 -->
    <n-card title="系统信息" :bordered="false">
      <n-descriptions label-placement="left" :column="3" bordered>
        <n-descriptions-item label="运行时长">
          <n-time :time="Date.now() - (status?.uptime ?? 0) * 1000" type="relative" />
        </n-descriptions-item>
        <n-descriptions-item label="版本">{{ status?.version ?? '-' }}</n-descriptions-item>
        <n-descriptions-item label="协议支持">
          <n-space>
            <n-tag size="small" type="info">Modbus</n-tag>
            <n-tag size="small" type="info">MQTT</n-tag>
            <n-tag size="small" type="info">OPC-UA</n-tag>
            <n-tag size="small" type="info">HTTP</n-tag>
          </n-space>
        </n-descriptions-item>
      </n-descriptions>
    </n-card>
  </n-space>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { HardwareChip, SettingsOutline, AlertCircleOutline, PulseOutline } from '@vicons/ionicons5'
import { systemApi, type SystemStatus } from '@/api'

const status = ref<SystemStatus | null>(null)
let timer: number | null = null

const cpuColor = computed(() => {
  const p = status.value?.cpu_percent ?? 0
  return p > 80 ? '#f56c6c' : p > 60 ? '#e6a23c' : '#67c23a'
})
const memColor = computed(() => {
  const p = status.value?.memory_percent ?? 0
  return p > 90 ? '#f56c6c' : p > 70 ? '#e6a23c' : '#67c23a'
})
const diskColor = computed(() => {
  const p = status.value?.disk_percent ?? 0
  return p > 90 ? '#f56c6c' : p > 80 ? '#e6a23c' : '#67c23a'
})

async function fetchStatus() {
  try { status.value = await systemApi.getStatus() } catch {}
}

function formatBytes(bytes?: number) {
  if (!bytes) return '-'
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB'
  if (bytes < 1024 * 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + ' MB'
  return (bytes / 1024 / 1024 / 1024).toFixed(1) + ' GB'
}

onMounted(() => { fetchStatus(); timer = window.setInterval(fetchStatus, 5000) })
onUnmounted(() => { if (timer) clearInterval(timer) })
</script>

<style scoped>
.stat-card {
  border-radius: 12px;
  transition: all 0.3s ease;
}
.stat-card:hover {
  transform: translateY(-4px);
  box-shadow: 0 12px 24px rgba(0,0,0,0.1);
}
.stat-card-primary { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: #fff; }
.stat-card-success { background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%); color: #fff; }
.stat-card-warning { background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%); color: #fff; }
.stat-card-info { background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%); color: #fff; }
.stat-footer { margin-top: 8px; font-size: 13px; }
.resource-card { text-align: center; }
.resource-info { margin-top: 12px; }
</style>
