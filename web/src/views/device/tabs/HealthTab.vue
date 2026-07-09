<template>
  <n-descriptions v-if="healthData" :column="2" bordered size="small">
    <n-descriptions-item :label="t('common.status')">{{ healthData.status || '-' }}</n-descriptions-item>
    <n-descriptions-item :label="t('healthDetail.lastCheck')">{{ healthData.last_check || healthData.timestamp || '-' }}</n-descriptions-item>
    <n-descriptions-item :label="t('healthDetail.successCount')">{{ healthData.success_count ?? 0 }}</n-descriptions-item>
    <n-descriptions-item :label="t('healthDetail.failCount')">{{ healthData.fail_count ?? 0 }}</n-descriptions-item>
    <n-descriptions-item :label="t('healthDetail.latency')">{{ healthData.latency_ms ?? '-' }} ms</n-descriptions-item>
    <n-descriptions-item :label="t('common.message')">{{ healthData.message || healthData.error || '-' }}</n-descriptions-item>
  </n-descriptions>
  <n-empty v-else :description="t('healthDetail.noHealthData')" />
</template>

<script setup lang="ts">
import { useDeviceDetailConsumer } from '../composables/useDeviceDetail'
import { t } from '@/i18n'
import { NDescriptions, NDescriptionsItem, NEmpty } from 'naive-ui'

const { healthData } = useDeviceDetailConsumer()
</script>
