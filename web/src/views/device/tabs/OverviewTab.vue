<template>
  <n-descriptions v-if="device" :column="2" bordered size="small">
    <n-descriptions-item :label="t('deviceDetail.deviceName')">{{ device.name }}</n-descriptions-item>
    <n-descriptions-item :label="t('deviceDetail.deviceId')">{{ device.device_id }}</n-descriptions-item>
    <n-descriptions-item :label="t('deviceDetail.protocol')">{{ device.protocol }}</n-descriptions-item>
    <n-descriptions-item :label="t('common.status')">{{ device.status }}</n-descriptions-item>
    <n-descriptions-item :label="t('deviceDetail.host')">{{ device.config?.host || '-' }}</n-descriptions-item>
    <n-descriptions-item :label="t('deviceDetail.port')">{{ device.config?.port || '-' }}</n-descriptions-item>
    <n-descriptions-item :label="t('common.createdAt')">{{ device.created_at || '-' }}</n-descriptions-item>
    <n-descriptions-item :label="t('common.updatedAt')">{{ device.updated_at || '-' }}</n-descriptions-item>
  </n-descriptions>
  <n-empty v-else :description="t('common.noData')" />
</template>

<script setup lang="ts">
import { useDeviceDetailConsumer } from '../composables/useDeviceDetail'
import { t } from '@/i18n'
import { NDescriptions, NDescriptionsItem, NEmpty } from 'naive-ui'

const { device } = useDeviceDetailConsumer()
</script>
