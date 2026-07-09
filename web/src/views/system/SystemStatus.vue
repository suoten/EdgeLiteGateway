<template>
  <n-spin :show="pageLoading" :description="t('system.loadingStatus')">
  <n-space vertical :size="16">
    <n-tabs type="line" animated>
      <n-tab-pane name="status" :tab="t('system.systemStatus')">
    <n-grid :cols="2" :x-gap="12">
      <n-gi>
        <n-card :title="t('system.systemStatus')" size="small">
          <template #header-extra>
            <n-space>
              <n-switch v-model:value="autoRefresh" size="small">
                <template #checked>{{ t('system.auto') }}</template>
                <template #unchecked>{{ t('system.manual') }}</template>
              </n-switch>
              <n-button text @click="fetchStatus">{{ t('system.refresh') }}</n-button>
            </n-space>
          </template>
          <n-descriptions label-placement="left" :column="1" bordered>
            <n-descriptions-item :label="t('system.cpuUsage')">
              <n-progress type="line" :percentage="status?.cpu_percent ?? 0" :indicator-placement="'inside'" :color="cpuColor" />
            </n-descriptions-item>
            <n-descriptions-item :label="t('system.memoryUsage')">
              <n-progress type="line" :percentage="status?.memory_percent ?? 0" :indicator-placement="'inside'" :color="memColor" />
            </n-descriptions-item>
            <n-descriptions-item :label="t('system.diskUsage')">
              <n-progress type="line" :percentage="status?.disk_percent ?? 0" :indicator-placement="'inside'" :color="diskColor" />
            </n-descriptions-item>
            <n-descriptions-item :label="t('system.uptime')">{{ formatUptime(status?.uptime ?? 0) }}</n-descriptions-item>
            <n-descriptions-item :label="t('system.version')">{{ status?.version ?? '-' }}</n-descriptions-item>
          </n-descriptions>
        </n-card>
      </n-gi>
      <n-gi>
        <n-card :title="t('system.businessStats')" size="small">
          <n-descriptions label-placement="left" :column="1" bordered>
            <n-descriptions-item :label="t('system.deviceTotal')">
              <n-text>{{ status?.device_total ?? '-' }}</n-text>
              <n-text depth="3" style="margin-left: 8px; font-size: 12px">({{ t('system.onlineCount', { count: status?.device_online ?? 0 }) }})</n-text>
            </n-descriptions-item>
            <n-descriptions-item :label="t('system.ruleTotal')">{{ status?.rule_total ?? '-' }}</n-descriptions-item>
            <n-descriptions-item :label="t('system.activeAlarm')">
              <n-text :type="(status?.alarm_firing ?? 0) > 0 ? 'error' : undefined">{{ status?.alarm_firing ?? '-' }}</n-text>
            </n-descriptions-item>
            <n-descriptions-item :label="t('system.collectTask')">{{ status?.collect_task_count ?? '-' }}</n-descriptions-item>
          </n-descriptions>
        </n-card>
      </n-gi>
    </n-grid>

    <n-card :title="t('system.cacheStatus')" size="small">
      <template #header-extra>
        <n-text depth="3" style="font-size: 12px">{{ t('system.watermarkStatus') }}: {{ cacheMetrics.watermark ?? '-' }}</n-text>
      </template>
      <n-descriptions label-placement="left" :column="1" bordered>
        <n-descriptions-item :label="t('system.cacheQueueSize')">{{ cacheMetrics.queueSize ?? '-' }}</n-descriptions-item>
        <n-descriptions-item :label="t('system.cacheSynced')">{{ cacheMetrics.synced ?? '-' }}</n-descriptions-item>
        <n-descriptions-item :label="t('system.cachePending')">
          <n-text :type="(cacheMetrics.pending ?? 0) > 0 ? 'warning' : undefined">{{ cacheMetrics.pending ?? '-' }}</n-text>
        </n-descriptions-item>
      </n-descriptions>
    </n-card>

    <n-grid :cols="3" :x-gap="12" style="margin-bottom:0">
      <n-gi>
        <n-card size="small">
          <template #header>{{ t('system.resourceCpu') }}</template>
          <template #header-extra><n-text :type="(status?.cpu_percent ?? 0) > 80 ? 'error' : (status?.cpu_percent ?? 0) > 60 ? 'warning' : 'success'">{{ (status?.cpu_percent ?? 0).toFixed(1) }}%</n-text></template>
          <n-progress type="circle" :percentage="status?.cpu_percent ?? 0" :color="cpuColor" :stroke-width="8" />
          <n-descriptions label-placement="left" :column="1" size="small" style="margin-top:8px" v-if="resourceDetail.cpu">
            <n-descriptions-item :label="t('system.resourceCores')">{{ resourceDetail.cpu.count ?? '-' }}</n-descriptions-item>
            <n-descriptions-item :label="t('system.resourceFreq')">{{ resourceDetail.cpu.freq_mhz ? `${resourceDetail.cpu.freq_mhz} MHz` : '-' }}</n-descriptions-item>
          </n-descriptions>
        </n-card>
      </n-gi>
      <n-gi>
        <n-card size="small">
          <template #header>{{ t('system.resourceMemory') }}</template>
          <template #header-extra><n-text :type="(status?.memory_percent ?? 0) > 90 ? 'error' : (status?.memory_percent ?? 0) > 70 ? 'warning' : 'success'">{{ (status?.memory_percent ?? 0).toFixed(1) }}%</n-text></template>
          <n-progress type="circle" :percentage="status?.memory_percent ?? 0" :color="memColor" :stroke-width="8" />
          <n-descriptions label-placement="left" :column="1" size="small" style="margin-top:8px" v-if="resourceDetail.memory">
            <n-descriptions-item :label="t('system.resourceTotal')">{{ formatBytes(resourceDetail.memory.total_bytes) }}</n-descriptions-item>
            <n-descriptions-item :label="t('system.resourceUsed')">{{ formatBytes(resourceDetail.memory.used_bytes) }}</n-descriptions-item>
          </n-descriptions>
        </n-card>
      </n-gi>
      <n-gi>
        <n-card size="small">
          <template #header>{{ t('system.resourceDisk') }}</template>
          <template #header-extra><n-text :type="(status?.disk_percent ?? 0) > 90 ? 'error' : (status?.disk_percent ?? 0) > 80 ? 'warning' : 'success'">{{ (status?.disk_percent ?? 0).toFixed(1) }}%</n-text></template>
          <n-progress type="circle" :percentage="status?.disk_percent ?? 0" :color="diskColor" :stroke-width="8" />
          <n-descriptions label-placement="left" :column="1" size="small" style="margin-top:8px" v-if="resourceDetail.disk">
            <n-descriptions-item :label="t('system.resourceTotal')">{{ formatBytes(resourceDetail.disk.total_bytes) }}</n-descriptions-item>
            <n-descriptions-item :label="t('system.resourceUsed')">{{ formatBytes(resourceDetail.disk.used_bytes) }}</n-descriptions-item>
          </n-descriptions>
        </n-card>
      </n-gi>
    </n-grid>

    <n-card :title="t('system.performanceChart')" size="small">
      <template #header-extra>
        <n-text depth="3" style="font-size: 12px">{{ t('system.lastNPoints', { n: perfHistory.length }) }}</n-text>
      </template>
      <n-grid :cols="2" :x-gap="12">
        <n-gi>
          <div style="font-size:13px;font-weight:600;margin-bottom:6px">{{ t('system.cpuUsage') }} (%)</div>
          <svg viewBox="0 0 300 80" preserveAspectRatio="none" style="width:100%;height:80px">
            <polyline :points="cpuPolyline" fill="none" stroke="#67c23a" stroke-width="2" />
          </svg>
        </n-gi>
        <n-gi>
          <div style="font-size:13px;font-weight:600;margin-bottom:6px">{{ t('system.memoryUsage') }} (%)</div>
          <svg viewBox="0 0 300 80" preserveAspectRatio="none" style="width:100%;height:80px">
            <polyline :points="memPolyline" fill="none" stroke="#409eff" stroke-width="2" />
          </svg>
        </n-gi>
      </n-grid>
      <n-grid :cols="2" :x-gap="12" style="margin-top:12px">
        <n-gi>
          <div style="font-size:13px;font-weight:600;margin-bottom:6px">{{ t('system.diskUsage') }} (%)</div>
          <svg viewBox="0 0 300 80" preserveAspectRatio="none" style="width:100%;height:80px">
            <polyline :points="diskPolyline" fill="none" stroke="#e6a23c" stroke-width="2" />
          </svg>
        </n-gi>
        <n-gi>
          <div style="font-size:13px;font-weight:600;margin-bottom:6px">{{ t('system.networkIO') }} (MB)</div>
          <svg viewBox="0 0 300 80" preserveAspectRatio="none" style="width:100%;height:80px">
            <polyline :points="netSentPolyline" fill="none" stroke="#67c23a" stroke-width="1.5" stroke-dasharray="4,2" />
            <polyline :points="netRecvPolyline" fill="none" stroke="#409eff" stroke-width="1.5" stroke-dasharray="4,2" />
          </svg>
          <n-space style="margin-top:4px;font-size:11px">
            <n-text depth="3">--- {{ t('system.netSent') }}</n-text>
            <n-text depth="3">--- {{ t('system.netRecv') }}</n-text>
          </n-space>
        </n-gi>
      </n-grid>
    </n-card>

    <n-card :title="t('system.lockStatus')" size="small">
      <template #header-extra>
        <n-button text size="small" @click="fetchLockStatus">{{ t('common.refresh') }}</n-button>
      </template>
      <n-descriptions label-placement="left" :column="1" bordered size="small">
        <n-descriptions-item :label="t('system.fineGrainedLocks')">
          <n-tag :type="lockStatus.use_fine_grained_locks ? 'success' : 'warning'" size="small">
            {{ lockStatus.use_fine_grained_locks ? t('system.enabled') : t('system.disabled') }}
          </n-tag>
        </n-descriptions-item>
        <n-descriptions-item :label="t('system.globalLock')">
          <n-tag :type="lockStatus.global_lock?.locked ? 'error' : 'success'" size="small">
            {{ lockStatus.global_lock?.locked ? t('system.locked') : t('system.unlocked') }}
          </n-tag>
        </n-descriptions-item>
      </n-descriptions>
      <n-divider />
      <n-grid v-if="lockStatus.table_locks && Object.keys(lockStatus.table_locks).length > 0" :cols="4" :x-gap="12" :y-gap="8">
        <n-gi v-for="(info, table) in lockStatus.table_locks" :key="table">
          <n-tag :bordered="false" size="small">
            {{ t('dbTable.' + table, table) }}:
            <n-tag :type="info.locked ? 'error' : 'success'" size="tiny" style="margin-left: 4px">
              {{ info.locked ? t('system.locked') : t('system.unlocked') }}
            </n-tag>
          </n-tag>
        </n-gi>
      </n-grid>
      <n-empty v-else :description="t('system.noFineGrainedLocks')" />
    </n-card>

    <n-card :title="t('system.dataBackup')" size="small">
      <template #header-extra>
        <n-space vertical align="end" :size="4">
          <n-text depth="3" style="font-size: 11px">{{ t('system.backupScope') }}</n-text>
          <n-button type="primary" size="small" :disabled="!auth.isAdmin" :loading="backupLoading" @click="handleBackup">{{ t('system.createBackup') }}</n-button>
        </n-space>
      </template>
      <n-data-table :columns="backupColumns" :data="backups" :loading="backupsLoading" :bordered="false" size="small" />
    </n-card>
      </n-tab-pane>
      <n-tab-pane name="cascade" :tab="t('cascade.title')">
        <n-space vertical :size="16">
          <n-card :title="t('cascade.topology')" size="small">
            <template #header-extra>
              <n-button text size="small" @click="fetchTopology">{{ t('common.refresh') }}</n-button>
            </template>
            <n-descriptions label-placement="left" :column="2" bordered>
              <n-descriptions-item :label="t('cascade.role')">
                <n-tag :type="topologyRoleColor" size="small">{{ topology.status ? t('cascade.' + topology.status) : t('cascade.standalone') }}</n-tag>  <!-- FIXED-P3: 替换硬编码'standalone'为i18n引用 -->
              </n-descriptions-item>
              <n-descriptions-item :label="t('cascade.nodeId')">{{ topology.local_id || '-' }}</n-descriptions-item>
              <n-descriptions-item :label="t('cascade.parent')">{{ topology.parent_id || '-' }}</n-descriptions-item>
              <n-descriptions-item :label="t('cascade.child')">{{ (topology.children || []).join(', ') || '-' }}</n-descriptions-item>
            </n-descriptions>
          </n-card>
          <n-card :title="t('cascade.topology')" size="small">
            <svg viewBox="0 0 500 320" style="width: 100%; max-width: 500px; margin: 0 auto; display: block;">
              <line v-for="(line, idx) in topoLines" :key="'line-' + idx"
                :x1="line.x1" :y1="line.y1" :x2="line.x2" :y2="line.y2"
                :stroke="line.connected ? '#18a058' : '#d0d0d0'" stroke-width="2"
                :stroke-dasharray="line.connected ? '' : '6,4'" />
              <g v-for="(node, idx) in topoNodes" :key="'node-' + idx" :transform="`translate(${node.x},${node.y})`">
                <circle :r="node.isSelf ? 30 : 22" :fill="node.isSelf ? '#18a058' : node.connected ? '#409eff' : '#c0c0c0'" :stroke="node.isSelf ? '#0e7a43' : '#fff'" stroke-width="2" />
                <text text-anchor="middle" :y="node.isSelf ? 5 : 4" fill="#fff" :font-size="node.isSelf ? 12 : 10" font-weight="600">{{ node.label }}</text>
                <text text-anchor="middle" :y="node.isSelf ? 48 : 38" fill="#666" font-size="10">{{ node.role }}</text>
              </g>
            </svg>
          </n-card>
          <n-card :title="t('cascade.neighbors')" size="small">
            <n-data-table v-if="(topology.peers || []).length > 0" :columns="neighborColumns" :data="topology.peers || []" :bordered="false" size="small" />
            <n-empty v-else :description="t('cascade.noNeighbors')" />
          </n-card>
        </n-space>
      </n-tab-pane>
      <n-tab-pane name="retention" :tab="t('retention.title')">
        <n-space vertical :size="16">
          <n-alert v-if="retentionNotImplemented" type="info" :bordered="false">
            {{ t('retention.notImplemented') }}
          </n-alert>
          <template v-if="!retentionNotImplemented">
          <n-card :title="t('retention.currentPolicy')" size="small">
            <n-descriptions label-placement="left" :column="1" bordered>
              <n-descriptions-item :label="t('retention.bucket')">{{ retentionPolicy.bucket || '-' }}</n-descriptions-item>
              <n-descriptions-item :label="t('retention.retentionPeriod')">{{ retentionPolicy.retention_period || '-' }}</n-descriptions-item>
            </n-descriptions>
          </n-card>
          <n-card :title="t('retention.title')" size="small">
            <n-space vertical :size="12">
              <n-form-item :label="t('retention.retentionPeriod')">
                <n-input v-model:value="retentionInput" :placeholder="t('retention.retentionPeriodPlaceholder')" style="max-width: 300px" />
              </n-form-item>
              <n-button type="primary" :disabled="!auth.isAdmin" :loading="retentionSaving" @click="handleUpdateRetention">{{ t('common.save') }}</n-button>
            </n-space>
          </n-card>
          </template>
        </n-space>
      </n-tab-pane>
      <n-tab-pane name="cert" :tab="t('certRotation.title')">
        <n-space vertical :size="16">
          <n-alert v-if="certNotImplemented" type="info" :bordered="false">
            {{ t('certRotation.notImplemented') }}
          </n-alert>
          <template v-if="!certNotImplemented">
          <n-card :title="t('certRotation.certExpiry')" size="small">
            <template v-if="certInfo">
              <n-descriptions label-placement="left" :column="2" bordered>
                <n-descriptions-item :label="t('certRotation.certSubject')">{{ certInfo.subject || '-' }}</n-descriptions-item>
                <n-descriptions-item :label="t('certRotation.certIssuer')">{{ certInfo.issuer || '-' }}</n-descriptions-item>
                <n-descriptions-item :label="t('certRotation.certNotBefore')">{{ certInfo.not_before || '-' }}</n-descriptions-item>
                <n-descriptions-item :label="t('certRotation.certNotAfter')">{{ certInfo.not_after || '-' }}</n-descriptions-item>
                <n-descriptions-item :label="t('certRotation.daysRemaining')">
                  <n-tag :type="certDaysRemaining <= 0 ? 'error' : certDaysRemaining <= 30 ? 'warning' : 'success'" size="small">
                    {{ certDaysRemaining <= 0 ? t('certRotation.expired') : certDaysRemaining <= 30 ? t('certRotation.expiringSoon') : t('certRotation.valid') }}
                    ({{ certDaysRemaining }})
                  </n-tag>
                </n-descriptions-item>
                <n-descriptions-item :label="t('certRotation.certFingerprint')">{{ certInfo.fingerprint || '-' }}</n-descriptions-item>
              </n-descriptions>
            </template>
            <n-empty v-else :description="t('certRotation.noCertInfo')" />
          </n-card>
          <n-card size="small">
            <n-button type="warning" :disabled="!auth.isAdmin" :loading="certRotating" @click="handleCertRotate">{{ t('certRotation.rotateNow') }}</n-button>
          </n-card>
          </template>
        </n-space>
      </n-tab-pane>
      <n-tab-pane name="migration" :tab="t('system.dbMigration')">
        <n-space vertical :size="16">
          <n-card :title="t('system.dbMigration')" size="small">
            <template #header-extra>
              <n-space>
                <n-tag :type="migrationStatusColor" size="small">{{ t('system.migrationStatus_' + migrationStatus) }}</n-tag>
                <n-button v-if="isAdmin" type="primary" size="small" :loading="migrationRetrying" @click="handleRetryMigration">{{ t('system.retryMigration') }}</n-button>
                <n-button text size="small" @click="fetchMigrationStatus">{{ t('common.refresh') }}</n-button>
              </n-space>
            </template>
            <n-alert v-if="migrationStatus === 'failed'" type="error" :title="t('system.migrationFailedTitle')" style="margin-bottom: 16px">
              <template #icon><n-icon :component="AlertCircleOutline" /></template>
              {{ t('system.migrationFailedMsg') }}
            </n-alert>
            <n-alert v-if="migrationStatus === 'success'" type="success" style="margin-bottom: 16px">
              <template #icon><n-icon :component="CheckmarkCircleOutline" /></template>
              {{ t('system.migrationSuccessMsg') }}
            </n-alert>
            <n-descriptions label-placement="left" :column="1" bordered>
              <n-descriptions-item :label="t('system.currentStatus')">
                <n-tag :type="migrationStatusColor" size="small">{{ t('system.migrationStatus_' + migrationStatus) }}</n-tag>
              </n-descriptions-item>
              <n-descriptions-item :label="t('system.lastUpdated')">{{ migrationLastUpdated || '-' }}</n-descriptions-item>
              <n-descriptions-item v-if="migrationStatus === 'failed' && migrationError" :label="t('system.errorDetails')">
                <n-alert type="error" :bordered="false" style="max-height: 200px; overflow-y: auto">
                  <pre style="white-space: pre-wrap; word-break: break-all; margin: 0; font-size: 12px">{{ migrationError }}</pre>
                </n-alert>
              </n-descriptions-item>
            </n-descriptions>
          </n-card>

          <n-card v-if="migrationHistory.length > 0" :title="t('system.migrationHistory')" size="small">
            <n-data-table :columns="migrationHistoryColumns" :data="migrationHistory" :bordered="false" size="small" />
          </n-card>
        </n-space>
      </n-tab-pane>
    </n-tabs>
  </n-space>
  </n-spin>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, onUnmounted, h, watch } from 'vue'
import { usePageVisibility } from '@/composables/usePageVisibility'
import { NButton, NTag, NIcon } from 'naive-ui'
import { t } from '@/i18n'
import { extractError } from '@/utils/errorCodes'
import { systemApi, type SystemStatus, type PerformanceData } from '@/api'
import http from '@/api/http'
import { useAuthStore } from '@/stores/auth'
import { AlertCircleOutline, CheckmarkCircleOutline } from '@vicons/ionicons5'
import { message, dialog } from '@/utils/discreteApi'
import { formatDateTime } from '@/utils/datetime'

const auth = useAuthStore()
const isAdmin = computed(() => auth.role === 'admin')

const MAX_PERF_POINTS = 60

const status = ref<SystemStatus | null>(null)
const cacheMetrics = reactive({ queueSize: 0, synced: 0, pending: 0, watermark: 'normal' })
const backups = ref<any[]>([])
const backupLoading = ref(false)
const backupsLoading = ref(false)
const autoRefresh = ref(true)
const pageLoading = ref(true)
const topology = ref<any>({ status: 'standalone', local_id: '', parent_id: null, children: [], peers: [] })
const resourceDetail = reactive<{ cpu: Record<string, any>; memory: Record<string, any>; disk: Record<string, any> }>({ cpu: {}, memory: {}, disk: {} })

function formatBytes(bytes: number): string {
  if (!bytes || bytes <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  let v = bytes
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(1)} ${units[i]}`
}

async function fetchResources() {
  try {
    const data = await systemApi.getResources()
    if (data) {
      resourceDetail.cpu = data.cpu || {}
      resourceDetail.memory = data.memory || {}
      resourceDetail.disk = data.disk || {}
    }
  } catch { /* ignore */ }
}

const topologyRoleColor = computed(() => {
  const r = topology.value.status || 'standalone'
  if (r === 'parent') return 'success'
  if (r === 'child') return 'warning'
  if (r === 'peer') return 'info'
  return 'default'
})

interface TopoNode { x: number; y: number; label: string; role: string; isSelf: boolean; connected: boolean }
interface TopoLine { x1: number; y1: number; x2: number; y2: number; connected: boolean }

const topoNodes = computed<TopoNode[]>(() => {
  const nodes: TopoNode[] = []
  const cx = 250, cy = 160
  nodes.push({ x: cx, y: cy, label: topology.value.local_id || 'Self', role: t('cascade.' + (topology.value.status || 'standalone')), isSelf: true, connected: true })
  const peers = topology.value.peers || []
  const count = peers.length
  if (count === 0) return nodes
  const radius = 110
  peers.forEach((p: any, i: number) => {
    const angle = (2 * Math.PI * i) / count - Math.PI / 2
    nodes.push({
      x: cx + radius * Math.cos(angle),
      y: cy + radius * Math.sin(angle),
      label: p.neighbor_id || p.host || `N${i + 1}`,
      role: t('cascade.' + (p.role || 'peer')),
      isSelf: false,
      connected: p.connected !== false,
    })
  })
  return nodes
})

const topoLines = computed<TopoLine[]>(() => {
  const lines: TopoLine[] = []
  const self = topoNodes.value.find(n => n.isSelf)
  if (!self) return lines
  topoNodes.value.forEach(n => {
    if (n.isSelf) return
    lines.push({ x1: self.x, y1: self.y, x2: n.x, y2: n.y, connected: n.connected })
  })
  return lines
})
const perfHistory = ref<PerformanceData[]>([])
let timer: number | null = null
let consecutiveErrors = 0

function toPolyline(data: number[], maxVal: number = 100, width: number = 300, height: number = 80): string {
  if (data.length < 2) return ''
  const step = width / (data.length - 1)
  return data.map((v, i) => {
    const x = i * step
    const y = height - (Math.min(v, maxVal) / maxVal) * height
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
}

const cpuPolyline = computed(() => toPolyline(perfHistory.value.map(p => p.cpu_percent)))
const memPolyline = computed(() => toPolyline(perfHistory.value.map(p => p.memory_percent)))
const diskPolyline = computed(() => toPolyline(perfHistory.value.map(p => p.disk_percent)))
const netMax = computed(() => Math.max(1, ...perfHistory.value.map(p => Math.max(p.net_sent_mb, p.net_recv_mb))))
const netSentPolyline = computed(() => toPolyline(perfHistory.value.map(p => p.net_sent_mb), netMax.value))
const netRecvPolyline = computed(() => toPolyline(perfHistory.value.map(p => p.net_recv_mb), netMax.value))

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

const backupColumns = [
  { title: t('system.backupId'), key: 'backup_id' },
  { title: t('system.file'), key: 'file' },
  { title: t('system.size'), key: 'size', render: (r: any) => r.size ? `${(r.size / 1024).toFixed(1)} KB` : '-' },
  { title: t('system.time'), key: 'created_at' },
  {
    title: t('system.actions'), key: 'actions', width: 120,
    render: (r: any) => h(NButton, { text: true, type: 'primary', size: 'small', disabled: !auth.isAdmin, onClick: () => handleRestore(r) }, { default: () => t('system.restore') }),
  },
]

function formatUptime(seconds: number) {
  const d = Math.floor(seconds / 86400)
  const hr = Math.floor((seconds % 86400) / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = seconds % 60
  if (d > 0) return `${d}${t('system.days')} ${hr}${t('system.hours')} ${m}${t('system.minutes')}`
  return `${hr}${t('system.hours')} ${m}${t('system.minutes')} ${s}${t('system.seconds')}`
}

async function fetchStatus() {
  try {
    status.value = await systemApi.getStatus()
    consecutiveErrors = 0
  } catch (e: any) {
    consecutiveErrors++
    // FIXED: 原问题-定时器持续失败时每5秒弹错误消息刷屏，连续3次以上不再弹
    if (consecutiveErrors <= 3) {
      message.error(extractError(e, t('system.fetchStatusFailed')))
    }
  } finally {
    pageLoading.value = false
  }
}

async function fetchBackups() {
  backupsLoading.value = true
  try { backups.value = await systemApi.listBackups() } catch (e: any) { message.error(extractError(e, t('system.fetchBackupFailed'))) } finally { backupsLoading.value = false }
}

async function fetchCacheMetrics() {
  try {
    // 适配4: 移除 baseURL:'/api' 覆盖，使用默认 baseURL /api/v1，请求路径变为 /api/v1/metrics.json
    const { data } = await http.get('/metrics.json')
    const m = data?.metrics || data || {}
    cacheMetrics.queueSize = m.edgelite_cache_size ?? 0
    cacheMetrics.pending = m.edgelite_cache_pending ?? 0
    cacheMetrics.synced = m.edgelite_cache_synced ?? (cacheMetrics.queueSize - cacheMetrics.pending)
    cacheMetrics.watermark = m.edgelite_cache_watermark ?? 'normal'
  } catch { /* ignore */ }
}

async function fetchPerformance() {
  try {
    const perf = await systemApi.getPerformance()
    if (perf) {
      perfHistory.value.push(perf)
      if (perfHistory.value.length > MAX_PERF_POINTS) {
        perfHistory.value.splice(0, perfHistory.value.length - MAX_PERF_POINTS)
      }
    }
  } catch { /* ignore */ }
}

async function handleBackup() {
  if (!auth.isAdmin) { message.warning(t('common.permissionDenied')); return }
  backupLoading.value = true
  try {
    await systemApi.createBackup()
    message.success(t('system.backupSuccess'))  // FIXED: 原问题-中文硬编码，改为i18n
    fetchBackups()
  } catch (e: any) {
    message.error(extractError(e, t('system.backupFailed')))  // FIXED: 原问题-中文硬编码，改为i18n
  } finally {
    backupLoading.value = false
  }
}

function handleRestore(r: any) {
  if (!auth.isAdmin) { message.warning(t('common.permissionDenied')); return }
  dialog.warning({
    title: t('system.restoreConfirmTitle'),
    content: t('system.restoreConfirm', { id: r.backup_id }),  // FIXED: 原问题-中文硬编码，改为i18n
    positiveText: t('system.restore'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      try {
        await systemApi.restore(r.backup_id)
        message.success(t('system.restoreSuccess'))  // FIXED: 原问题-中文硬编码，改为i18n
        // FIXED: 原问题-恢复成功后未刷新状态，添加刷新调用
        fetchStatus()
        fetchBackups()
      } catch (e: any) {
        message.error(extractError(e, t('system.restoreFailed')))  // FIXED: 原问题-中文硬编码，改为i18n
      }
    },
  })
}

// [AUDIT-FIX] 一般级-页面隐藏时暂停轮询，恢复时立即刷新数据
// [AUDIT-FIX] 严重-S1: watch 可见分支先清除已有定时器，避免重复创建定时器泄漏
const { isVisible } = usePageVisibility()
watch(isVisible, (visible) => {
  if (visible) {
    if (timer) { clearInterval(timer); timer = null }
    if (autoRefresh.value) {
      fetchStatus(); fetchCacheMetrics(); fetchPerformance(); fetchResources()
      timer = window.setInterval(() => { if (autoRefresh.value) { fetchStatus(); fetchCacheMetrics(); fetchPerformance(); fetchResources() } }, 5000)
    }
  } else {
    if (timer) { clearInterval(timer); timer = null }
  }
})

onMounted(() => {
  fetchStatus(); fetchBackups(); fetchTopology(); fetchCacheMetrics(); fetchPerformance(); fetchRetentionPolicy(); fetchCertInfo(); fetchResources(); fetchMigrationStatus(); fetchMigrationHistory(); fetchLockStatus()
  timer = window.setInterval(() => { if (autoRefresh.value) { fetchStatus(); fetchCacheMetrics(); fetchPerformance(); fetchResources() } }, 5000)
})
onUnmounted(() => { if (timer) clearInterval(timer) })

const neighborColumns = [
  { title: t('cascade.nodeId'), key: 'neighbor_id' },
  { title: t('cascade.host'), key: 'host' },
  { title: t('cascade.port'), key: 'port' },
  { title: t('cascade.role'), key: 'role', render: (r: any) => h(NTag, { size: 'small' }, { default: () => r.role || 'peer' }) },
]

async function fetchTopology() {
  try {
    topology.value = await systemApi.getCascadeTopology()
  } catch { /* ignore */ }
}

const retentionPolicy = reactive({ bucket: '', retention_period: '' })
const retentionNotImplemented = ref(false)
const retentionInput = ref('')
const retentionSaving = ref(false)

async function fetchRetentionPolicy() {
  try {
    const data = await systemApi.getRetentionPolicy()
    retentionNotImplemented.value = false
    retentionPolicy.bucket = data?.bucket || ''
    retentionPolicy.retention_period = data?.retention_period || ''
    retentionInput.value = data?.retention_period || ''
  } catch (e: any) {
    if (e?.response?.status === 501) { retentionNotImplemented.value = true; return }
  }
}

async function handleUpdateRetention() {
  if (!auth.isAdmin) { message.warning(t('common.permissionDenied')); return }
  retentionSaving.value = true
  try {
    await systemApi.updateRetentionPolicy(retentionInput.value)
    message.success(t('retention.updateSuccess'))
    await fetchRetentionPolicy()
  } catch (e: any) {
    message.error(extractError(e, t('retention.updateFailed')))
  } finally {
    retentionSaving.value = false
  }
}

const certInfo = ref<any>(null)
const certNotImplemented = ref(false)
const certRotating = ref(false)

const certDaysRemaining = computed(() => {
  if (!certInfo.value?.not_after) return 0
  const expiry = new Date(certInfo.value.not_after)
  const now = new Date()
  return Math.ceil((expiry.getTime() - now.getTime()) / (1000 * 60 * 60 * 24))
})

async function fetchCertInfo() {
  try {
    certInfo.value = await systemApi.getCertInfo()
    certNotImplemented.value = false
  } catch (e: any) {
    if (e?.response?.status === 501) { certNotImplemented.value = true; return }
  }
}

function handleCertRotate() {
  if (!auth.isAdmin) { message.warning(t('common.permissionDenied')); return }
  dialog.warning({
    title: t('certRotation.rotateNow'),
    content: t('certRotation.rotateConfirm'),
    positiveText: t('certRotation.rotateNow'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      certRotating.value = true
      try {
        await systemApi.rotateCert()
        message.success(t('certRotation.rotateSuccess'))
        await fetchCertInfo()
      } catch (e: any) {
        message.error(extractError(e, t('certRotation.rotateFailed')))
      } finally {
        certRotating.value = false
      }
    },
  })
}

// ─── Database Migration Status ───
const migrationStatus = ref<string>('unknown')
const migrationLastUpdated = ref<string | null>(null)
const migrationError = ref<string | null>(null)
const migrationHistory = ref<any[]>([])
const migrationRetrying = ref(false)

// ─── Database Lock Status ───
const lockStatus = ref<{
  use_fine_grained_locks?: boolean
  global_lock?: { locked: boolean }
  table_locks?: Record<string, { locked: boolean }>
}>({})

async function fetchLockStatus() {
  try {
    const { data } = await http.get('/system/locks/status')
    if (data?.data) {
      lockStatus.value = data.data
    }
  } catch (e) {
    console.error('Failed to fetch lock status:', e)
  }
}

const migrationStatusColor = computed(() => {
  const status = migrationStatus.value
  if (status === 'success') return 'success'
  if (status === 'failed') return 'error'
  if (status === 'in_progress') return 'warning'
  return 'default'
})

const migrationHistoryColumns = [
  { title: t('system.migrationTime'), key: 'timestamp', width: 180, render: (r: any) => formatDateTime(r.timestamp) },
  { title: t('system.migrationStatus_'), key: 'status', width: 120, render: (r: any) => h(NTag, { type: r.status === 'success' ? 'success' : r.status === 'failed' ? 'error' : 'default', size: 'small' }, { default: () => t('system.migrationStatus_' + r.status) }) },
  { title: t('system.migrationMessage'), key: 'message' },
]

async function fetchMigrationStatus() {
  try {
    const { data } = await http.get('/system/migration/status')
    if (data?.data) {
      migrationStatus.value = data.data.current_status || 'unknown'
      migrationLastUpdated.value = data.data.last_updated
      if (data.data.last_failure) {
        migrationError.value = data.data.last_failure.error || null
      } else {
        migrationError.value = null
      }
    }
  } catch (e) {
    console.error('Failed to fetch migration status:', e)
  }
}

async function fetchMigrationHistory() {
  try {
    const { data } = await http.get('/system/migration/history')
    if (data?.data?.history) {
      migrationHistory.value = data.data.history
    }
  } catch (e) {
    console.error('Failed to fetch migration history:', e)
  }
}

async function handleRetryMigration() {
  dialog.warning({
    title: t('system.retryMigrationConfirm'),
    content: t('system.retryMigrationConfirmMsg'),
    positiveText: t('common.confirm'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      migrationRetrying.value = true
      try {
        await http.post('/system/migration/retry')
        message.success(t('system.retryMigrationSuccess'))
        await fetchMigrationStatus()
        await fetchMigrationHistory()
      } catch (e: any) {
        message.error(extractError(e, t('system.retryMigrationFailed')))
      } finally {
        migrationRetrying.value = false
      }
    },
  })
}
</script>
