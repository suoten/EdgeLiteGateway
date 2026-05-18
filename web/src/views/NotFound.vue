<template>
  <div style="display: flex; justify-content: center; align-items: center; height: 100vh;">
    <n-result status="404" title="404" :description="t('common.pageNotFound')">  <!-- FIXED: 原问题-中文硬编码，改用i18n -->
      <template #footer>
        <n-button @click="$router.push('/')">{{ t('common.backToHome') }}</n-button>  <!-- FIXED: 原问题-中文硬编码，改用i18n -->
      </template>
    </n-result>
  </div>
</template>

<script setup lang="ts">
import { NResult, NButton } from 'naive-ui'
import { t } from '@/i18n'  // FIXED: 原问题-中文硬编码，改用i18n
</script>
