<template>
  <div class="cloud-sync-status">
    <n-space align="center" :size="8">
      <n-icon :component="syncIcon" :color="syncColor" />
      <n-text depth="3" style="font-size: 13px">{{ statusText }}</n-text>
    </n-space>
  </div>
</template>

<script setup lang="ts">
import { ref, shallowRef, onMounted } from 'vue'
import { NSpace, NIcon, NText } from 'naive-ui'
import { CloudDoneOutline, CloudOfflineOutline } from '@vicons/ionicons5'
import { t } from '@/i18n'

// [FIX] 使用 shallowRef 而非 ref 存储组件引用，避免 Vue 将组件对象变为响应式对象导致性能开销
const syncIcon = shallowRef(CloudDoneOutline)
const syncColor = ref('#52c41a')
const statusText = ref(t('cloudSync.synced'))

onMounted(() => {
  // Placeholder: show synced status by default
  // Real implementation would check platform sync status via API
})
</script>

<style scoped>
.cloud-sync-status {
  display: inline-flex;
  align-items: center;
}
</style>
