<template>
  <n-card :bordered="false" class="ai-widget-card">
    <template #header>
      <n-space align="center" :size="8">
        <span class="ai-badge">AI</span>
        <span style="font-weight: 600">{{ t('ai.engineStatus') }}</span>
        <n-tag :type="aiEnabled ? 'success' : 'default'" size="small" :bordered="false">
          {{ aiEnabled ? t('ai.statusActive') : t('ai.statusUnavailable') }}
        </n-tag>
      </n-space>
    </template>
    <template #header-extra>
      <n-button text size="small" @click="$router.push('/system/ai-model')">{{ t('ai.manageModels') }} →</n-button>
    </template>
    <n-grid :cols="4" :x-gap="16" :y-gap="16">
      <n-gi>
        <n-card class="stat-card stat-card-ai-primary" :bordered="false" size="small">
          <n-statistic :label="t('ai.modelCount')" :value="summary.model_count ?? 0">
            <template #prefix><n-icon :component="SparklesOutline" :size="18" /></template>
          </n-statistic>
        </n-card>
      </n-gi>
      <n-gi>
        <n-card class="stat-card stat-card-ai-success" :bordered="false" size="small">
          <n-statistic :label="t('ai.totalCalls')" :value="summary.total_calls ?? 0" />
        </n-card>
      </n-gi>
      <n-gi>
        <n-card class="stat-card stat-card-ai-info" :bordered="false" size="small">
          <n-statistic :label="t('ai.avgLatency')" :value="summary.avg_latency_ms ?? '-'" />
        </n-card>
      </n-gi>
      <n-gi>
        <n-card class="stat-card stat-card-ai-warning" :bordered="false" size="small">
          <n-statistic :label="t('ai.totalErrors')" :value="summary.total_errors ?? 0" />
        </n-card>
      </n-gi>
    </n-grid>

    <n-grid v-if="aiEnabled" :cols="4" :x-gap="16" :y-gap="16" style="margin-top: 16px">
      <n-gi :span="2">
        <n-card size="small" :bordered="true" class="sub-card">
          <template #header>
            <n-space align="center" :size="6">
              <n-icon :component="TrendingUpOutline" :size="16" color="#8b5cf6" />
              <span style="font-size: 13px; font-weight: 600">{{ t('ai.inferenceLatencyTrend') }}</span>
            </n-space>
          </template>
          <div v-if="latencyStats" class="latency-stats">
            <n-space :size="16" justify="center">
              <div class="latency-item">
                <span class="latency-label">{{ t('ai.latencyMin') }}</span>
                <span class="latency-value latency-min">{{ latencyStats.min }}ms</span>
              </div>
              <div class="latency-item">
                <span class="latency-label">{{ t('ai.latencyAvg') }}</span>
                <span class="latency-value latency-avg">{{ latencyStats.avg }}ms</span>
              </div>
              <div class="latency-item">
                <span class="latency-label">{{ t('ai.latencyMax') }}</span>
                <span class="latency-value latency-max">{{ latencyStats.max }}ms</span>
              </div>
            </n-space>
          </div>
          <n-empty v-else :description="t('ai.noRecentInferences')" size="small" />
        </n-card>
      </n-gi>
      <n-gi :span="1">
        <n-card size="small" :bordered="true" class="sub-card">
          <template #header>
            <n-space align="center" :size="6">
              <n-icon :component="HardwareChipOutline" :size="16" color="#8b5cf6" />
              <span style="font-size: 13px; font-weight: 600">{{ t('ai.activeTasks') }}</span>
            </n-space>
          </template>
          <div class="center-stat">
            <span class="center-stat-num">{{ summary.active_schedule_count ?? 0 }}</span>
          </div>
        </n-card>
      </n-gi>
      <n-gi :span="1">
        <n-card size="small" :bordered="true" class="sub-card">
          <template #header>
            <n-space align="center" :size="6">
              <n-icon :component="WarningOutline" :size="16" :color="hasAnomaly ? '#f56c6c' : '#67c23a'" />
              <span style="font-size: 13px; font-weight: 600">{{ t('ai.anomalyStatus') }}</span>
            </n-space>
          </template>
          <div class="center-stat">
            <n-tag :type="hasAnomaly ? 'error' : 'success'" size="small" round>
              {{ hasAnomaly ? t('ai.anomalyDetected') : t('ai.anomalyNone') }}
            </n-tag>
          </div>
        </n-card>
      </n-gi>
    </n-grid>

    <n-grid v-if="aiEnabled && recentInferences.length > 0" :cols="1" style="margin-top: 16px">
      <n-gi>
        <n-card size="small" :bordered="true" class="sub-card">
          <template #header>
            <n-space align="center" :size="6">
              <n-icon :component="AnalyticsOutline" :size="16" color="#8b5cf6" />
              <span style="font-size: 13px; font-weight: 600">{{ t('ai.recentInferences') }}</span>
            </n-space>
          </template>
          <n-data-table
            :columns="recentColumns"
            :data="recentInferences"
            :bordered="false"
            size="small"
            :row-key="(row: any) => row.timestamp + row.model_id"
            max-height="160"
          />
        </n-card>
      </n-gi>
    </n-grid>
  </n-card>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { NIcon } from 'naive-ui'
import { SparklesOutline, TrendingUpOutline, HardwareChipOutline, WarningOutline, AnalyticsOutline } from '@vicons/ionicons5'
import { aiApi } from '@/api'
import { t } from '@/i18n'
import { formatDateTime } from '@/utils/datetime'
import { usePageVisibility } from '@/composables/usePageVisibility'

const summary = ref<Record<string, any>>({})
const aiEnabled = ref(false)
let timer: number | null = null
// 页面可见性检测：页面隐藏时暂停轮询，恢复可见时立即刷新并恢复轮询
const { isVisible } = usePageVisibility()

const recentInferences = computed(() => {
  const recent = summary.value.recent_inferences || []
  return recent.slice(-5).reverse()
})

const recentColumns = computed(() => [
  { title: t('ai.modelName'), key: 'model_name', width: 120, ellipsis: { tooltip: true } },
  { title: t('ai.latencyMs'), key: 'latency_ms', width: 80 },
  { title: t('ai.status'), key: 'status', width: 80,
    render: (row: any) => row.status === 'success' ? t('ai.statusActive') : t('ai.statusError'),
  },
  { title: t('ai.lastInference'), key: 'timestamp', width: 160, ellipsis: { tooltip: true }, render: (row: any) => formatDateTime(row.timestamp) },
])

const latencyStats = computed(() => {
  const trend = summary.value.latency_trend || []
  if (trend.length === 0) return null
  const values = trend.map((t: any) => t.v as number)
  const min = Math.min(...values)
  const max = Math.max(...values)
  const avg = Math.round(values.reduce((a: number, b: number) => a + b, 0) / values.length)
  return { min, max, avg }
})

const hasAnomaly = computed(() => {
  const recent = summary.value.recent_inferences || []
  return recent.some((r: any) => r.status === 'error')
})

async function fetchStats() {
  try {
    const data = await aiApi.getSummary()
    summary.value = data || {}
    aiEnabled.value = true
  } catch {
    try {
      const data = await aiApi.getStats()
      summary.value = data || {}
      aiEnabled.value = true
    } catch (e) {
      console.warn('[AiStatsWidget] Failed to fetch AI stats:', e)
      summary.value = {}
      aiEnabled.value = false
    }
  }
}

onMounted(() => {
  fetchStats()
  timer = window.setInterval(fetchStats, 15000)
})
onUnmounted(() => {
  if (timer) clearInterval(timer)
})

// 页面可见性变化：隐藏时暂停轮询，恢复可见时立即刷新并恢复轮询
watch(isVisible, (visible) => {
  if (visible) {
    fetchStats()
    if (!timer) {
      timer = window.setInterval(fetchStats, 15000)
    }
  } else {
    if (timer) {
      clearInterval(timer)
      timer = null
    }
  }
})
</script>

<style scoped>
.ai-widget-card {
  border: 1px solid rgba(139, 92, 246, 0.2);
  background: linear-gradient(135deg, rgba(139, 92, 246, 0.03) 0%, rgba(102, 126, 234, 0.03) 100%);
}
.ai-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border-radius: 6px;
  background: linear-gradient(135deg, #8b5cf6 0%, #667eea 100%);
  color: #fff;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.5px;
}
.stat-card {
  border-radius: 8px;
  transition: all 0.3s ease;
  color: #fff !important;
}
.stat-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 8px 16px rgba(0,0,0,0.1);
}
.stat-card-ai-primary { background: linear-gradient(135deg, #8b5cf6 0%, #667eea 100%); }
.stat-card-ai-success { background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%); }
.stat-card-ai-info { background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%); }
.stat-card-ai-warning { background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%); }
.stat-card :deep(.n-statistic .n-statistic-value__content),
.stat-card :deep(.n-statistic .n-statistic-value),
.stat-card :deep(.n-statistic .n-statistic-value__integer),
.stat-card :deep(.n-statistic .n-statistic-value__fraction),
.stat-card :deep(.n-statistic__label),
.stat-card :deep(.n-icon) {
  color: #fff !important;
}
.sub-card {
  border-radius: 8px;
}
.latency-stats {
  padding: 8px 0;
}
.latency-item {
  text-align: center;
}
.latency-label {
  display: block;
  font-size: 11px;
  color: #909399;
  margin-bottom: 4px;
}
.latency-value {
  font-size: 16px;
  font-weight: 700;
}
.latency-min { color: #67c23a; }
.latency-avg { color: #8b5cf6; }
.latency-max { color: #f56c6c; }
.center-stat {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 8px 0;
}
.center-stat-num {
  font-size: 24px;
  font-weight: 700;
  color: #8b5cf6;
}
</style>