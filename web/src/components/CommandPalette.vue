<template>
  <n-modal v-model:show="visible" preset="card" :title="t('common.search')" style="width: 600px;" :bordered="false">
    <n-input v-model:value="searchQuery" :placeholder="t('commandPalette.searchPlaceholder')" clearable>
      <template #prefix>
        <n-icon :component="SearchOutline" />
      </template>
    </n-input>
    <n-divider style="margin: 12px 0" />
    <n-empty v-if="!filteredItems.length" :description="t('common.noData')" size="small" />
    <n-list v-else hoverable clickable>
      <n-list-item v-for="item in filteredItems" :key="item.key" @click="handleSelect(item)">
        <n-thing :title="item.label" />
      </n-list-item>
    </n-list>
  </n-modal>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import { NModal, NInput, NIcon, NDivider, NEmpty, NList, NListItem, NThing } from 'naive-ui'
import { SearchOutline } from '@vicons/ionicons5'
import { t } from '@/i18n'

interface MenuItem {
  key: string
  label: string
  path: string
}

const props = defineProps<{
  show: boolean
  menuItems?: MenuItem[]
  devices?: Array<{ device_id: string; name: string }>
  rules?: Array<{ rule_id: string; name: string }>
}>()

const emit = defineEmits<{
  'update:show': [value: boolean]
}>()

const router = useRouter()
const searchQuery = ref('')

const visible = computed({
  get: () => props.show,
  set: (v: boolean) => emit('update:show', v),
})

const filteredItems = computed(() => {
  const items = props.menuItems ?? []
  if (!searchQuery.value) return items
  const q = searchQuery.value.toLowerCase()
  return items.filter(i => i.label?.toLowerCase().includes(q))
})

function handleSelect(item: MenuItem) {
  router.push(item.path)
  visible.value = false
}
</script>
