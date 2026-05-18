<template>
  <n-space vertical :size="16">
    <n-space justify="space-between">
      <n-input v-model:value="searchText" :placeholder="t('userManage.searchUsername')" clearable style="width: 200px" /> <!-- FIXED: 原问题-空函数filterUsers无实际作用，v-model已自动触发computed重算 -->
      <n-button type="primary" @click="showCreateModal = true">{{ t('userManage.createUser') }}</n-button> <!-- FIXED: 原问题-中文硬编码 -->
    </n-space>

    <n-data-table :columns="columns" :data="filteredUsers" :loading="loading" :row-key="(r: User) => r.user_id" />

    <n-empty v-if="!loading && filteredUsers.length === 0" :description="t('userManage.noUserData')" style="margin-top: 24px" /> <!-- FIXED: 原问题-中文硬编码 -->

    <n-modal v-model:show="showCreateModal" :title="t('userManage.createUser')" preset="card" style="width: 500px"> <!-- FIXED: 原问题-中文硬编码 -->
      <n-form :model="createForm" label-placement="left" label-width="80" :rules="createRules" ref="createFormRef">
        <n-form-item :label="t('userManage.username')" path="username"><n-input v-model:value="createForm.username" :placeholder="t('userManage.enterUsername')" /></n-form-item> <!-- FIXED: 原问题-中文硬编码 -->
        <n-form-item :label="t('userManage.password')" path="password"><n-input v-model:value="createForm.password" type="password" show-password-on="click" :placeholder="t('userManage.passwordPlaceholder')" /></n-form-item> <!-- FIXED: 原问题-中文硬编码 -->
        <n-form-item :label="t('userManage.role')" path="role"> <!-- FIXED: 原问题-中文硬编码 -->
          <n-select v-model:value="createForm.role" :options="roleOptions" :placeholder="t('userManage.selectRole')" /> <!-- FIXED: 原问题-中文硬编码 -->
        </n-form-item>
      </n-form>
      <template #action>
        <n-button @click="showCreateModal = false">{{ t('common.cancel') }}</n-button> <!-- FIXED: 原问题-中文硬编码 -->
        <n-button type="primary" :loading="creating" @click="handleCreate">{{ t('common.create') }}</n-button> <!-- FIXED: 原问题-中文硬编码 -->
      </template>
    </n-modal>

    <n-modal v-model:show="showEditModal" :title="t('userManage.editUser')" preset="card" style="width: 500px"> <!-- FIXED: 原问题-中文硬编码 -->
      <n-form :model="editForm" label-placement="left" label-width="80" :rules="editRules" ref="editFormRef">
        <n-form-item :label="t('userManage.username')" path="username"><n-input v-model:value="editForm.username" disabled /></n-form-item> <!-- FIXED: 原问题-中文硬编码 -->
        <n-form-item :label="t('userManage.role')" path="role"> <!-- FIXED: 原问题-中文硬编码 -->
          <n-select v-model:value="editForm.role" :options="roleOptions" />
        </n-form-item>
        <n-form-item :label="t('userManage.newPassword')"><n-input v-model:value="editForm.password" type="password" show-password-on="click" :placeholder="t('userManage.leaveEmptyNoChange')" /></n-form-item> <!-- FIXED: 原问题-中文硬编码 -->
        <n-form-item :label="t('userManage.enabledStatus')"> <!-- FIXED: 原问题-中文硬编码 -->
          <n-switch v-model:value="editForm.enabled" />
        </n-form-item>
      </n-form>
      <template #action>
        <n-button @click="showEditModal = false">{{ t('common.cancel') }}</n-button> <!-- FIXED: 原问题-中文硬编码 -->
        <n-button type="primary" :loading="editing" @click="handleEdit">{{ t('common.save') }}</n-button> <!-- FIXED: 原问题-中文硬编码 -->
      </template>
    </n-modal>
  </n-space>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, h } from 'vue'
import { NButton, NTag, NSpace, NSwitch, useMessage, useDialog } from 'naive-ui'
import { userApi, type User } from '@/api'
import { t } from '@/i18n'  // FIXED: 原问题-中文硬编码

const message = useMessage()
const dialog = useDialog()
const users = ref<User[]>([])
const loading = ref(false)
const showCreateModal = ref(false)
const showEditModal = ref(false)
const creating = ref(false)
const editing = ref(false)
const searchText = ref('')
const createFormRef = ref<any>(null)
const editFormRef = ref<any>(null)

const editRules = {
  role: { required: true, message: t('userManage.selectRole'), trigger: 'change' },  // FIXED: 原问题-中文硬编码
  password: {
    trigger: 'blur',
    validator: (_rule: any, value: string) => {
      if (!value) return true
      if (value.length < 8) return new Error(t('userManage.passwordMinLength'))  // FIXED: 原问题-中文硬编码
      if (!/[a-zA-Z]/.test(value) || !/[0-9]/.test(value)) return new Error(t('userManage.passwordLetterAndDigit'))  // FIXED: 原问题-中文硬编码
      return true
    },
  },
}

const filteredUsers = computed(() => {
  if (!searchText.value) return users.value
  const s = searchText.value.toLowerCase()
  return users.value.filter(u => u.username.toLowerCase().includes(s))
})

const roleOptions = [
  { label: t('userManage.admin') + ' (admin)', value: 'admin' },  // FIXED: 原问题-中文硬编码
  { label: t('userManage.operator') + ' (operator)', value: 'operator' },  // FIXED: 原问题-中文硬编码
  { label: t('userManage.viewer') + ' (viewer)', value: 'viewer' },  // FIXED: 原问题-中文硬编码
]

const roleColor: Record<string, any> = { admin: 'error', operator: 'warning', viewer: 'info' }
const roleLabel: Record<string, string> = { admin: t('userManage.admin'), operator: t('userManage.operator'), viewer: t('userManage.viewer') }  // FIXED: 原问题-中文硬编码

const createRules = {
  username: { required: true, pattern: /^[a-zA-Z0-9_]+$/, message: t('userManage.usernamePattern'), trigger: 'blur' },  // FIXED: 原问题-中文硬编码
  password: {
    required: true,
    trigger: 'blur',
    validator: (_rule: any, value: string) => {
      if (!value) return new Error(t('userManage.enterPassword'))  // FIXED: 原问题-中文硬编码
      if (value.length < 8) return new Error(t('userManage.passwordMinLength'))  // FIXED: 原问题-中文硬编码
      if (!/[a-zA-Z]/.test(value) || !/[0-9]/.test(value)) return new Error(t('userManage.passwordLetterAndDigit'))  // FIXED: 原问题-中文硬编码
      return true
    },
  },
  role: { required: true, message: t('userManage.selectRole'), trigger: 'change' },  // FIXED: 原问题-中文硬编码
}

const columns = [
  { title: t('userManage.userId'), key: 'user_id', width: 140 },  // FIXED: 原问题-中文硬编码
  { title: t('userManage.username'), key: 'username', width: 150 },  // FIXED: 原问题-中文硬编码
  { title: t('userManage.role'), key: 'role', width: 120,  // FIXED: 原问题-中文硬编码
    render: (row: User) => h(NTag, { type: roleColor[row.role] || 'default', size: 'small' }, { default: () => roleLabel[row.role] || row.role }),
  },
  {
    title: t('userManage.status'), key: 'enabled', width: 80,  // FIXED: 原问题-中文硬编码
    render: (row: User) => h(NTag, { type: row.enabled !== false ? 'success' : 'default', size: 'small' }, { default: () => row.enabled !== false ? t('userManage.enabled') : t('userManage.disabled') }),  // FIXED: 原问题-中文硬编码
  },
  { title: t('userManage.createTime'), key: 'created_at', width: 180 },  // FIXED: 原问题-中文硬编码
  { title: t('userManage.updateTime'), key: 'updated_at', width: 180 },  // FIXED: 原问题-中文硬编码
  {
    title: t('userManage.actions'), key: 'actions', width: 150,  // FIXED: 原问题-中文硬编码
    render: (row: User) =>
      h(NSpace, null, {
        default: () => [
          h(NButton, { text: true, type: 'primary', onClick: () => openEdit(row) }, { default: () => t('common.edit') }),  // FIXED: 原问题-中文硬编码
          row.username !== 'admin'
            ? h(NButton, { text: true, type: 'error', onClick: () => handleDelete(row) }, { default: () => t('common.delete') })  // FIXED: 原问题-中文硬编码
            : null,
        ],
      }),
  },
]

const createForm = reactive({ username: '', password: '', role: 'viewer' })
const editForm = reactive({ user_id: '', username: '', role: '', password: '', enabled: true })

async function fetchUsers() {
  loading.value = true
  try {
    const data = await userApi.list({ page: 1, size: 9999 })  // FIXED: 原问题-用户管理全量加载，size:1000改为9999
    users.value = data?.data ?? []
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || t('userManage.fetchListFailed'))  // FIXED: 原问题-中文硬编码
  } finally {
    loading.value = false
  }
}

async function handleCreate() {
  try {
    await createFormRef.value?.validate()
  } catch { return }
  creating.value = true
  try {
    await userApi.create(createForm)
    message.success(t('userManage.createSuccess'))  // FIXED: 原问题-中文硬编码
    showCreateModal.value = false
    createForm.username = ''
    createForm.password = ''
    createForm.role = 'viewer'
    fetchUsers()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || t('userManage.createFailed'))  // FIXED: 原问题-中文硬编码
  } finally {
    creating.value = false
  }
}

function openEdit(row: User) {
  editForm.user_id = row.user_id
  editForm.username = row.username
  editForm.role = row.role
  editForm.password = ''
  editForm.enabled = row.enabled !== false
  showEditModal.value = true
}

async function handleEdit() {
  try {
    await editFormRef.value?.validate()
  } catch { return }
  editing.value = true
  try {
    const data: any = { role: editForm.role, enabled: editForm.enabled }
    if (editForm.password) data.password = editForm.password
    await userApi.update(editForm.user_id, data)
    message.success(t('userManage.updateSuccess'))  // FIXED: 原问题-中文硬编码
    showEditModal.value = false
    fetchUsers()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || t('userManage.updateFailed'))  // FIXED: 原问题-中文硬编码
  } finally {
    editing.value = false
  }
}

function handleDelete(row: User) {
  dialog.warning({
    title: t('userManage.confirmDelete'),  // FIXED: 原问题-中文硬编码
    content: t('userManage.confirmDeleteUser', { username: row.username }),  // FIXED: 原问题-中文硬编码
    positiveText: t('common.delete'),  // FIXED: 原问题-中文硬编码
    negativeText: t('common.cancel'),  // FIXED: 原问题-中文硬编码
    onPositiveClick: async () => {
      try {
        await userApi.delete(row.user_id)
        message.success(t('userManage.deleteSuccess'))  // FIXED: 原问题-中文硬编码
        fetchUsers()
      } catch (e: any) {
        message.error(e?.response?.data?.detail || e?.message || t('userManage.deleteFailed'))  // FIXED: 原问题-中文硬编码
      }
    },
  })
}

onMounted(fetchUsers)
</script>
