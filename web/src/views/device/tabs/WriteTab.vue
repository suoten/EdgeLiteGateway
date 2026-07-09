<template>
  <n-space vertical>
    <n-form inline v-if="points.length">
      <n-form-item :label="t('deviceDetail.selectPoint')">
        <n-select v-model:value="selectedPoint" :options="pointOptions" style="width: 200px" />
      </n-form-item>
      <n-form-item :label="t('common.value')">
        <n-input v-model:value="writeValue" placeholder="0" style="width: 150px" />
      </n-form-item>
      <n-button type="primary" :loading="writing" :disabled="!selectedPoint" @click="handleWrite">
        {{ t('deviceDetail.write') }}
      </n-button>
    </n-form>
    <n-empty v-else :description="t('deviceDetail.noWritablePoints')" size="small" />
  </n-space>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { useDeviceDetailConsumer } from '../composables/useDeviceDetail'
import { deviceApi } from '@/api'
import { t } from '@/i18n'
import { message } from '@/utils/discreteApi'
import { NSpace, NForm, NFormItem, NSelect, NInput, NButton, NEmpty } from 'naive-ui'

const { device } = useDeviceDetailConsumer()
const selectedPoint = ref<string | null>(null)
const writeValue = ref('')
const writing = ref(false)

const points = computed(() => (device.value?.points ?? []).filter(p => p.access_mode !== 'r'))

const pointOptions = computed(() => points.value.map(p => ({ label: p.name, value: p.name })))

async function handleWrite() {
  if (!device.value?.device_id || !selectedPoint.value) return
  writing.value = true
  try {
    await deviceApi.writePoint(device.value.device_id, selectedPoint.value, parseFloat(writeValue.value))
    message.success(t('deviceDetail.writeSuccess'))
  } catch (e: any) {
    message.error(e?.response?.data?.detail || t('deviceDetail.writeFailed'))
  } finally {
    writing.value = false
  }
}
</script>
