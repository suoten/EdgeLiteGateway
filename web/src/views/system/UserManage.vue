<template>
  <n-space vertical :size="16">
    <n-space justify="space-between">
      <n-input v-model:value="searchText" placeholder="搜索用户名" clearable style="width: 200px" @update:value="filterUsers" />
      <n-button type="primary" @click="showCreateModal = true">创建用户</n-button>
    </n-space>

    <n-data-table :columns="columns" :data="filteredUsers" :loading="loading" :row-key="(r: User) => r.user_id" />

    <n-modal v-model:show="showCreateModal" title="创建用户" preset="card" style="width: 500px">
      <n-form :model="createForm" label-placement="left" label-width="80" :rules="createRules" ref="createFormRef">
        <n-form-item label="用户名" path="username"><n-input v-model:value="createForm.username" placeholder="请输入用户名" /></n-form-item>
        <n-form-item label="密码" path="password"><n-input v-model:value="createForm.password" type="password" show-password-on="click" placeholder="请输入密码（至少6位）" /></n-form-item>
        <n-form-item label="角色" path="role">
          <n-select v-model:value="createForm.role" :options="roleOptions" placeholder="选择角色" />
        </n-form-item>
      </n-form>
      <template #action>
        <n-button @click="showCreateModal = false">取消</n-button>
        <n-button type="primary" :loading="creating" @click="handleCreate">创建</n-button>
      </template>
    </n-modal>

    <n-modal v-model:show="showEditModal" title="编辑用户" preset="card" style="width: 500px">
      <n-form :model="editForm" label-placement="left" label-width="80" :rules="editRules" ref="editFormRef">
        <n-form-item label="用户名" path="username"><n-input v-model:value="editForm.username" disabled /></n-form-item>
        <n-form-item label="角色" path="role">
          <n-select v-model:value="editForm.role" :options="roleOptions" />
        </n-form-item>
        <n-form-item label="新密码"><n-input v-model:value="editForm.password" type="password" show-password-on="click" placeholder="留空则不修改" /></n-form-item>
      </n-form>
      <template #action>
        <n-button @click="showEditModal = false">取消</n-button>
        <n-button type="primary" :loading="editing" @click="handleEdit">保存</n-button>
      </template>
    </n-modal>
  </n-space>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, h } from 'vue'
import { NButton, NTag, NSpace, useMessage, useDialog } from 'naive-ui'
import { userApi, type User } from '@/api'

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
  role: { required: true, message: '请选择角色', trigger: 'change' },
}

const filteredUsers = computed(() => {
  if (!searchText.value) return users.value
  const s = searchText.value.toLowerCase()
  return users.value.filter(u => u.username.toLowerCase().includes(s))
})

const roleOptions = [
  { label: '管理员 (admin)', value: 'admin' },
  { label: '操作员 (operator)', value: 'operator' },
  { label: '观察者 (viewer)', value: 'viewer' },
]

const roleColor: Record<string, any> = { admin: 'error', operator: 'warning', viewer: 'info' }
const roleLabel: Record<string, string> = { admin: '管理员', operator: '操作员', viewer: '观察者' }

const createRules = {
  username: { required: true, message: '请输入用户名', trigger: 'blur' },
  password: { required: true, min: 6, message: '密码至少6位', trigger: 'blur' },
  role: { required: true, message: '请选择角色', trigger: 'change' },
}

const columns = [
  { title: '用户ID', key: 'user_id', width: 140 },
  { title: '用户名', key: 'username', width: 150 },
  {
    title: '角色', key: 'role', width: 120,
    render: (row: User) => h(NTag, { type: roleColor[row.role] || 'default', size: 'small' }, { default: () => roleLabel[row.role] || row.role }),
  },
  { title: '创建时间', key: 'created_at', width: 180 },
  { title: '更新时间', key: 'updated_at', width: 180 },
  {
    title: '操作', key: 'actions', width: 150,
    render: (row: User) =>
      h(NSpace, null, {
        default: () => [
          h(NButton, { text: true, type: 'primary', onClick: () => openEdit(row) }, { default: () => '编辑' }),
          row.username !== 'admin'
            ? h(NButton, { text: true, type: 'error', onClick: () => handleDelete(row) }, { default: () => '删除' })
            : null,
        ],
      }),
  },
]

const createForm = reactive({ username: '', password: '', role: 'viewer' })
const editForm = reactive({ user_id: '', username: '', role: '', password: '' })

function filterUsers() {}

async function fetchUsers() {
  loading.value = true
  try {
    users.value = await userApi.list()
  } catch (e: any) {
    message.error(e?.message || '获取用户列表失败')
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
    message.success('用户创建成功')
    showCreateModal.value = false
    createForm.username = ''
    createForm.password = ''
    createForm.role = 'viewer'
    fetchUsers()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || '创建失败')
  } finally {
    creating.value = false
  }
}

function openEdit(row: User) {
  editForm.user_id = row.user_id
  editForm.username = row.username
  editForm.role = row.role
  editForm.password = ''
  showEditModal.value = true
}

async function handleEdit() {
  editing.value = true
  try {
    const data: any = { role: editForm.role }
    if (editForm.password) data.password = editForm.password
    await userApi.update(editForm.user_id, data)
    message.success('用户更新成功')
    showEditModal.value = false
    fetchUsers()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || '更新失败')
  } finally {
    editing.value = false
  }
}

function handleDelete(row: User) {
  dialog.warning({
    title: '确认删除',
    content: `确定删除用户 "${row.username}"？此操作不可撤销。`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await userApi.delete(row.user_id)
        message.success('删除成功')
        fetchUsers()
      } catch (e: any) {
        message.error(e?.response?.data?.detail || '删除失败')
      }
    },
  })
}

onMounted(fetchUsers)
</script>
