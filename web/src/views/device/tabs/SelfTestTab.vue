<template>
  <n-space vertical>
    <n-button type="primary" :loading="ctx.selfTestRunning.value" @click="ctx.runSelfTest">
      {{ t('selfTest.runTest') }}
    </n-button>
    <n-descriptions v-if="testResult" :column="1" bordered size="small">
      <n-descriptions-item :label="t('common.status')">{{ testResult.status || '-' }}</n-descriptions-item>
      <n-descriptions-item :label="t('common.message')">{{ testResult.message || testResult.detail || '-' }}</n-descriptions-item>
      <n-descriptions-item v-if="testResult.duration" :label="t('selfTest.duration')">{{ testResult.duration }} ms</n-descriptions-item>
    </n-descriptions>
    <n-empty v-else :description="t('selfTest.noResult')" />
  </n-space>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useDeviceDetailConsumer } from '../composables/useDeviceDetail'
import { deviceApi } from '@/api'
import { t } from '@/i18n'
import { NSpace, NButton, NDescriptions, NDescriptionsItem, NEmpty } from 'naive-ui'

const ctx = useDeviceDetailConsumer()
const testResult = ref<any>(null)
</script>
