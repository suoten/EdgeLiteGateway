<template>
  <n-space vertical :size="16">
    <n-space justify="space-between">
      <n-text>用户管理</n-text>
      <n-button type="primary" @click="showCreateModal = true">创建用户</n-button>
    </n-space>

    <n-data-table :columns="columns" :data="users" :loading="loading" :row-key="(r: User) => r.user_id" />

    <n-modal v-model:show="showCreateModal" title="创建用户" preset="card" style="width: 500px">
      <n-form :model="createForm" label-placement="left" label-width="80">
        <n-form-item label="用户名"><n-input v-model:value="createForm.username" /></n-form-item>
        <n-form-item label="密码"><n-input v-model:value="createForm.password" type="password" show-password-on="click" /></n-form-item>
        <n-form-item label="角色">
          <n-select v-model:value="createForm.role" :options="roleOptions" />
        </n-form-item>
      </n-form>
      <template #action>
        <n-button @click="showCreateModal = false">取消</n-button>
        <n-button type="primary" :loading="creating" @click="handleCreate">创建</n-button>
      </template>
    </n-modal>

    <n-modal v-model:show="showEditModal" title="编辑用户" preset="card" style="width: 500px">
      <n-form :model="editForm" label-placement="left" label-width="80">
        <n-form-item label="用户名"><n-input v-model:value="editForm.username" disabled /></n-form-item>
        <n-form-item label="角色">
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
import { ref, reactive, onMounted, h } from 'vue'
import { NButton, NTag, NSpace, useMessage, useDialog } from 'naive-ui'
import http from '@/api/http'
import type { ApiResponse } from '@/api/http'

interface User {
  user_id: string
  username: string
  role: string
  created_at: string
  updated_at: string
}

const message = useMessage()
const dialog = useDialog()
const users = ref<User[]>([])
const loading = ref(false)
const showCreateModal = ref(false)
const showEditModal = ref(false)
const creating = ref(false)
const editing = ref(false)

const roleOptions = [
  { label: '管理员', value: 'admin' },
  { label: '操作员', value: 'operator' },
  { label: '观察者', value: 'viewer' },
]

const roleColor: Record<string, any> = { admin: 'error', operator: 'warning', viewer: 'info' }

const columns = [
  { title: '用户ID', key: 'user_id', width: 140 },
  { title: '用户名', key: 'username', width: 150 },
  {
    title: '角色', key: 'role', width: 100,
    render: (row: User) => h(NTag, { type: roleColor[row.role] || 'default', size: 'small' }, { default: () => row.role }),
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

async function fetchUsers() {
  loading.value = true
  try {
    const resp = await http.get<ApiResponse<User[]>>('/users')
    users.value = resp.data.data
  } catch (e: any) {
    message.error(e?.message || '获取用户列表失败')
  } finally {
    loading.value = false
  }
}

async function handleCreate() {
  creating.value = true
  try {
    await http.post('/users', createForm)
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
    await http.put(`/users/${editForm.user_id}`, data)
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
    content: `确定删除用户 "${row.username}"？`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await http.delete(`/users/${row.user_id}`)
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
