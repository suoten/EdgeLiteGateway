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
          <n-statistic :label="t('ai.modelCount')" :value="stats.model_count ?? 0">
            <template #prefix><span class="ai-icon">🧠</span></template>
          </n-statistic>
        </n-card>
      </n-gi>
      <n-gi>
        <n-card class="stat-card stat-card-ai-success" :bordered="false" size="small">
          <n-statistic :label="t('ai.totalCalls')" :value="stats.total_calls ?? 0" />
        </n-card>
      </n-gi>
      <n-gi>
        <n-card class="stat-card stat-card-ai-info" :bordered="false" size="small">
          <n-statistic :label="t('ai.avgLatency')" :value="stats.avg_latency_ms ?? '-'" />
        </n-card>
      </n-gi>
      <n-gi>
        <n-card class="stat-card stat-card-ai-warning" :bordered="false" size="small">
          <n-statistic :label="t('ai.totalErrors')" :value="stats.total_errors ?? 0" />
        </n-card>
      </n-gi>
    </n-grid>
  </n-card>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { aiApi } from '@/api'
import { t } from '@/i18n'

const stats = ref<Record<string, any>>({})
const aiEnabled = ref(false)
let timer: number | null = null

async function fetchStats() {
  try {
    const data = await aiApi.getStats()
    stats.value = data || {}
    aiEnabled.value = true
  } catch (e) {  // FIXED-P2: 原catch {}静默吞错，AI服务故障时用户无感知
    console.warn('[AiStatsWidget] Failed to fetch AI stats:', e)
    stats.value = {}
    aiEnabled.value = false
  }
}

onMounted(() => {
  fetchStats()
  timer = window.setInterval(fetchStats, 15000)
})
onUnmounted(() => {
  if (timer) clearInterval(timer)
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
.ai-icon {
  font-size: 18px;
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
</style>
