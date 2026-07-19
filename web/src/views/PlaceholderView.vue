<template>
  <div class="placeholder-view">
    <n-result status="info" :title="title" :description="description">
      <template #footer>
        <n-button @click="goBack">{{ t('common.back') }}</n-button>
      </template>
    </n-result>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { t } from '@/i18n'
import { NResult, NButton } from 'naive-ui'

const route = useRoute()
const router = useRouter()

const title = computed(() => (route.meta?.title as string) || t('common.comingSoon'))
const description = computed(() => t('common.featureNotReady'))

function goBack() {
  router.push('/')
}
</script>

<style scoped>
.placeholder-view {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 60vh;
}
</style>
