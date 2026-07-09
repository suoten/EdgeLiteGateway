<template>
  <n-data-table
    :columns="columns"
    :data="points"
    size="small"
    :pagination="{ pageSize: 20 }"
  >
    <template #empty>
      <n-empty :description="t('common.noData')" size="small" />
    </template>
  </n-data-table>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useDeviceDetailConsumer } from '../composables/useDeviceDetail'
import { t } from '@/i18n'
import { NDataTable, NEmpty } from 'naive-ui'

const { device } = useDeviceDetailConsumer()

const points = computed(() => device.value?.points ?? [])

const columns = computed(() => [
  { title: t('deviceDetail.pointName'), key: 'name', width: 150 },
  { title: t('deviceDetail.dataType'), key: 'data_type', width: 100 },
  { title: t('deviceDetail.unit'), key: 'unit', width: 80 },
  { title: t('deviceDetail.address'), key: 'address', width: 150 },
  { title: t('deviceDetail.accessMode'), key: 'access_mode', width: 100 },
])
</script>
