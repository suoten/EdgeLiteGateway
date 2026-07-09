<template>
  <n-space vertical :size="16">
    <!-- 修复8: 密码过期策略配置——前端 localStorage 持久化，仅 admin 可修改 -->
    <n-card v-if="auth.isAdmin" size="small" :bordered="true">
      <template #header>
        <n-space align="center" :size="8">
          <span>{{ t('userManage.passwordPolicyTitle') }}</span>
          <n-tag size="small" :type="passwordExpireDays > 0 ? 'warning' : 'default'">
            {{ passwordExpireDays > 0 ? t('userManage.passwordExpireIn', { days: passwordExpireDays }) : t('userManage.passwordNeverExpire') }}
          </n-tag>
        </n-space>
      </template>
      <n-space align="center" :size="12">
        <n-text depth="3" style="font-size: 13px">{{ t('userManage.passwordExpireDaysLabel') }}</n-text>
        <n-input-number v-model:value="passwordExpireDays" :min="0" :max="365" :step="30" size="small" style="width: 140px" />
        <n-button size="small" type="primary" @click="savePasswordPolicy">{{ t('common.save') }}</n-button>
        <n-text depth="3" style="font-size: 12px">{{ t('userManage.passwordPolicyHint') }}</n-text>
      </n-space>
    </n-card>

    <!-- 修复13/14: 标签页——用户列表 / 权限矩阵 / 在线用户 -->
    <n-tabs v-model:value="activeTab" type="line" animated>
      <n-tab-pane name="users" :tab="t('userManage.tabUsers')">
        <n-space vertical :size="12">
          <n-space justify="space-between">
            <!-- [AUDIT-FIX] G-05: 搜索改为后端过滤，输入时触发 fetchUsers -->
            <n-input v-model:value="searchText" :maxlength="64" :placeholder="t('userManage.searchUsername')" clearable style="width: 200px" @update:value="onSearch" />
            <n-button type="primary" @click="showCreateModal = true">{{ t('userManage.createUser') }}</n-button>
          </n-space>

          <!-- [AUDIT-FIX] G-05: data 改为 users（后端已过滤），移除 filteredUsers -->
          <n-data-table :columns="columns" :data="users" :loading="loading" :row-key="(r: User) => r.user_id" :pagination="pagination" remote :scroll-x="1150">
            <template #empty>
              <n-empty :description="t('userManage.noUserData')" style="margin: 24px 0" />
            </template>
          </n-data-table>
        </n-space>
      </n-tab-pane>

      <!-- 修复13: 权限矩阵可视化（只读） -->
      <n-tab-pane name="permissions" :tab="t('userManage.tabPermissionMatrix')">
        <n-space vertical :size="12">
          <n-text depth="3" style="font-size: 12px">{{ t('userManage.permissionMatrixHint') }}</n-text>
          <n-data-table :columns="permissionMatrixColumns" :data="permissionMatrixData" :bordered="true" size="small" />
        </n-space>
      </n-tab-pane>

      <!-- 修复14: 在线用户（基于最近 30 分钟登录活动） -->
      <n-tab-pane name="online" :tab="t('userManage.tabOnlineUsers')">
        <n-space vertical :size="12">
          <n-space justify="space-between" align="center">
            <n-text depth="3" style="font-size: 12px">{{ t('userManage.onlineUsersHint') }}</n-text>
            <n-button size="small" @click="fetchOnlineUsers" :loading="onlineLoading">{{ t('common.refresh') }}</n-button>
          </n-space>
          <n-data-table :columns="onlineColumns" :data="onlineUsers" :loading="onlineLoading" :bordered="false" size="small">
            <template #empty>
              <n-empty :description="t('userManage.noOnlineUsers')" style="margin: 24px 0" />
            </template>
          </n-data-table>
        </n-space>
      </n-tab-pane>
    </n-tabs>

    <n-modal v-model:show="showCreateModal" :title="t('userManage.createUser')" preset="card" :style="modalStyle" :close-on-esc="true" :auto-focus="true" :close-on-esc-aria-label="t('common.closeDialog')"> <!-- FIXED: 原问题-中文硬编码 -->
      <n-form :model="createForm" label-placement="left" label-width="80" :rules="createRules" ref="createFormRef">
        <n-form-item :label="t('userManage.username')" path="username"><n-input v-model:value="createForm.username" :maxlength="32" :placeholder="t('userManage.enterUsername')" /></n-form-item> <!-- FIXED: 原问题-中文硬编码 -->
        <n-form-item :label="t('userManage.password')" path="password"><n-input v-model:value="createForm.password" :maxlength="72" type="password" show-password-on="click" :placeholder="t('userManage.passwordPlaceholder')" /></n-form-item> <!-- FIXED: 原问题-中文硬编码 -->
        <n-form-item :label="t('userManage.role')" path="role"> <!-- FIXED: 原问题-中文硬编码 -->
          <n-select v-model:value="createForm.role" :options="roleOptions" :placeholder="t('userManage.selectRole')" /> <!-- FIXED: 原问题-中文硬编码 -->
        </n-form-item>
      </n-form>
      <template #action>
        <n-button @click="showCreateModal = false">{{ t('common.cancel') }}</n-button> <!-- FIXED: 原问题-中文硬编码 -->
        <n-button type="primary" :loading="creating" @click="handleCreate">{{ t('common.create') }}</n-button> <!-- FIXED: 原问题-中文硬编码 -->
      </template>
    </n-modal>

    <n-modal v-model:show="showEditModal" :title="t('userManage.editUser')" preset="card" :style="modalStyle" :close-on-esc="true" :auto-focus="true" :close-on-esc-aria-label="t('common.closeDialog')"> <!-- FIXED: 原问题-中文硬编码 -->
      <n-form :model="editForm" label-placement="left" label-width="80" :rules="editRules" ref="editFormRef">
        <n-form-item :label="t('userManage.username')" path="username"><n-input v-model:value="editForm.username" disabled /></n-form-item> <!-- FIXED: 原问题-中文硬编码 -->
        <n-form-item :label="t('userManage.role')" path="role"> <!-- FIXED: 原问题-中文硬编码 -->
          <n-select v-model:value="editForm.role" :options="roleOptions" />
        </n-form-item>
        <!-- FIXED-Severe(S-2): 补充 path="password" 和 path="enabled"，否则 editRules 中的密码强度校验永不触发 -->
        <n-form-item :label="t('userManage.newPassword')" path="password"><n-input v-model:value="editForm.password" :maxlength="72" type="password" show-password-on="click" :placeholder="t('userManage.leaveEmptyNoChange')" /></n-form-item> <!-- FIXED: 原问题-中文硬编码 -->
        <n-form-item :label="t('userManage.enabledStatus')" path="enabled"> <!-- FIXED: 原问题-中文硬编码 -->
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
import { ref, reactive, computed, onMounted, onUnmounted, h, watch } from 'vue'
import { NButton, NTag, NSpace, NSwitch, NText } from 'naive-ui'
import { userApi, auditApi, type User, type UserRole } from '@/api'
import { t } from '@/i18n'
import { extractError } from '@/utils/errorCodes'
import { message, dialog } from '@/utils/discreteApi'
import { formatDateTime } from '@/utils/datetime'
import { useAuthStore } from '@/stores/auth'
import { useBreakpoints } from '@/composables/useBreakpoints'

// 修复6: 移动端弹窗全屏适配
const { isMobile } = useBreakpoints()
const modalStyle = computed(() => isMobile.value
  ? { width: '100vw', maxWidth: '100vw', height: '100vh', maxHeight: '100vh', margin: 0, borderRadius: 0 }
  : { width: '500px', maxWidth: '95vw' }
)

// FIXED-Severe(S-5): 用户管理写操作需要 admin 权限
const auth = useAuthStore()

// 修复8: 密码过期策略——前端 localStorage 持久化
const PASSWORD_EXPIRE_KEY = 'password_expire_days'
const passwordExpireDays = ref<number>(loadPasswordExpireDays())

function loadPasswordExpireDays(): number {
  try {
    const raw = localStorage.getItem(PASSWORD_EXPIRE_KEY)
    if (raw != null) {
      const n = parseInt(raw, 10)
      if (!isNaN(n) && n >= 0 && n <= 365) return n
    }
  } catch { /* ignore */ }
  return 90  // 默认 90 天过期
}

function savePasswordPolicy() {
  try {
    localStorage.setItem(PASSWORD_EXPIRE_KEY, String(passwordExpireDays.value))
    message.success(t('common.saveSuccess'))
  } catch (e: any) {
    message.error(t('common.saveFailed'))
  }
}

const users = ref<User[]>([])
const loading = ref(false)
const showCreateModal = ref(false)
const showEditModal = ref(false)
const creating = ref(false)
const editing = ref(false)
const searchText = ref('')
const createFormRef = ref<any>(null)
const editFormRef = ref<any>(null)

// [AUDIT-FIX] G-05: 移除客户端过滤 filteredUsers，改用后端搜索 + 防抖
let searchTimer: ReturnType<typeof setTimeout> | null = null
function onSearch() {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    pagination.page = 1
    fetchUsers()
  }, 300)
}

// [AUDIT-FIX] G-06: editRules 改为 computed 实现语言切换响应式
const editRules = computed(() => ({
  role: { required: true, message: t('userManage.selectRole'), trigger: ['change', 'blur'] },  // FIXED: 原问题-中文硬编码
  password: {
    trigger: ['input', 'blur'],
    validator: (_rule: any, value: string) => {
      if (!value) return true
      if (value.length < 8) return new Error(t('userManage.passwordMinLength'))
      if (value.length > 72) return new Error(t('userManage.passwordMaxLength'))
      if (!/[a-zA-Z]/.test(value) || !/[0-9]/.test(value)) return new Error(t('userManage.passwordLetterAndDigit'))
      if (!/[!@#$%^&*()_+\-=\[\]{}|;':",.\/<>?`~]/.test(value)) return new Error(t('login.passwordNeedSpecial'))
      return true
    },
  },
}))

// [AUDIT-FIX] G-06: roleOptions 改为 computed 实现语言切换响应式
// 修复9: 移除硬编码的英文后缀，label 直接使用 i18n 文本，避免中英混杂
const roleOptions = computed(() => [
  { label: t('userManage.admin'), value: 'admin' },
  { label: t('userManage.operator'), value: 'operator' },
  { label: t('userManage.viewer'), value: 'viewer' },
])

const roleColor: Record<string, any> = { admin: 'error', operator: 'warning', viewer: 'info' }
// [AUDIT-FIX] G-06: roleLabel 改为 computed 实现语言切换响应式
const roleLabel = computed<Record<string, string>>(() => ({ admin: t('userManage.admin'), operator: t('userManage.operator'), viewer: t('userManage.viewer') }))

// [AUDIT-FIX] G-06: createRules 改为 computed 实现语言切换响应式
// 使用 userApi.list 客户端校验用户名唯一性（社区版用户数 <100，可一次拉取全量过滤）
// FIX-DEBOUNCE: 用户名唯一性校验添加 300ms 防抖，避免每次按键触发 API 请求
let _usernameCheckTimer: ReturnType<typeof setTimeout> | null = null
function checkUsernameUnique(value: string): Promise<boolean> {
  return new Promise<boolean>((resolve) => {
    if (_usernameCheckTimer) clearTimeout(_usernameCheckTimer)
    _usernameCheckTimer = setTimeout(async () => {
      _usernameCheckTimer = null
      try {
        const data = await userApi.list({ page: 1, size: 999 })
        const exists = (data?.data ?? []).some((u: any) => u.username === value)
        resolve(!exists)
      } catch {
        resolve(true)  // 接口失败时不阻塞提交
      }
    }, 300)
  })
}
const createRules = computed(() => ({
  username: [
    { required: true, message: t('userManage.usernamePattern'), trigger: ['input', 'blur'] },
    { min: 3, max: 32, message: t('userManage.usernameLength'), trigger: ['input', 'blur'] },
    { pattern: /^[a-zA-Z0-9_]+$/, message: t('userManage.usernamePattern'), trigger: ['input', 'blur'] },
    {
      validator: async (_rule: any, value: string) => {
        if (!value || value.length < 3) return true
        const ok = await checkUsernameUnique(value)
        if (!ok) return new Error(t('userManage.usernameExists'))
        return true
      },
      trigger: ['input', 'blur'],
    },
  ],
  password: {
    required: true,
    trigger: ['input', 'blur'],
    validator: (_rule: any, value: string) => {
      if (!value) return new Error(t('userManage.enterPassword'))
      if (value.length < 8) return new Error(t('userManage.passwordMinLength'))
      if (value.length > 72) return new Error(t('userManage.passwordMaxLength'))
      if (!/[a-zA-Z]/.test(value) || !/[0-9]/.test(value)) return new Error(t('userManage.passwordLetterAndDigit'))
      if (!/[!@#$%^&*()_+\-=\[\]{}|;':",.\/<>?`~]/.test(value)) return new Error(t('login.passwordNeedSpecial'))
      return true
    },
  },
  role: { required: true, message: t('userManage.selectRole'), trigger: ['change', 'blur'] },
}))

// [AUDIT-FIX] G-06: columns 改为 computed 实现语言切换响应式
// [AUDIT-FIX] G-06: user_id 列改为序号列，避免暴露内部主键
// 修复14: 添加"最后登录"列
const columns = computed(() => [
  { title: '#', key: 'index', width: 60, render: (_row: User, index: number) => index + 1 + (pagination.page - 1) * pagination.pageSize },
  { title: t('userManage.username'), key: 'username', width: 150 },
  { title: t('userManage.role'), key: 'role', width: 120,
    render: (row: User) => h(NTag, { type: roleColor[row.role] || 'default', size: 'small' }, { default: () => roleLabel.value[row.role] || row.role }),
  },
  {
    title: t('userManage.status'), key: 'enabled', width: 80,
    render: (row: User) => h(NTag, { type: row.enabled !== false ? 'success' : 'default', size: 'small' }, { default: () => row.enabled !== false ? t('userManage.enabled') : t('userManage.disabled') }),
  },
  // 修复14: 最后登录列——从 audit 日志聚合
  {
    title: t('userManage.lastLogin'), key: 'last_login', width: 180,
    render: (row: User) => {
      const ts = lastLoginMap.value[row.user_id]
      if (!ts) return h(NText, { depth: 3, style: 'font-size: 12px' }, { default: () => t('userManage.neverLogin') })
      return formatDateTime(ts)
    },
  },
  { title: t('userManage.createTime'), key: 'created_at', width: 180, render: (row: User) => row.created_at ? formatDateTime(row.created_at) : '-' },
  { title: t('userManage.updateTime'), key: 'updated_at', width: 180, render: (row: User) => row.updated_at ? formatDateTime(row.updated_at) : '-' },
  {
    title: t('userManage.actions'), key: 'actions', width: 150,
    // [FIX] 操作列按钮仅 admin 可见
    render: (row: User) => {
      if (!auth.isAdmin) return null
      return h(NSpace, null, {
        default: () => [
          h(NButton, { text: true, type: 'primary', onClick: () => openEdit(row) }, { default: () => t('common.edit') }),
          // [AUDIT-FIX] G-06: 仅禁止删除自己（FIX: auth.user 不存在，改用 username 比较）
          row.username !== auth.username
            ? h(NButton, { text: true, type: 'error', onClick: () => handleDelete(row) }, { default: () => t('common.delete') })
            : null,
        ],
      })
    },
  },
])

const createForm = reactive({ username: '', password: '', role: 'viewer' as UserRole })
const editForm = reactive({ user_id: '', username: '', role: '', password: '', enabled: true })

const pagination = reactive({
  page: 1,
  pageSize: 20,
  itemCount: 0,
  pageSizes: [10, 20, 50, 100],
  // [AUDIT-FIX] G-06: 添加 showSizePicker 使 pageSizes 配置生效
  showSizePicker: true,
  onChange: (p: number) => { pagination.page = p; fetchUsers() },
  onUpdatePageSize: (s: number) => { pagination.pageSize = s; pagination.page = 1; fetchUsers() },
})

async function fetchUsers() {
  loading.value = true
  try {
    // [AUDIT-FIX] G-05: 将 searchText 传给后端实现跨页搜索
    const params: any = { page: pagination.page, size: pagination.pageSize }
    if (searchText.value) params.search = searchText.value
    const data = await userApi.list(params)
    users.value = data?.data ?? []
    pagination.itemCount = data?.total ?? 0
  } catch (e: any) {
    message.error(extractError(e, t('userManage.fetchListFailed')))  // FIXED: 原问题-中文硬编码
  } finally {
    loading.value = false
  }
}

async function handleCreate() {
  // FIXED-Severe(S-5): 创建用户需要 admin 权限
  if (!auth.isAdmin) { message.warning(t('common.permissionDenied')); return }
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
    // [AUDIT-FIX] G-06: 创建成功后重置表单校验状态
    createFormRef.value?.restoreValidation()
    fetchUsers()
  } catch (e: any) {
    message.error(extractError(e, t('userManage.createFailed')))  // FIXED: 原问题-中文硬编码
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
  // FIXED-Severe(S-5): 编辑用户需要 admin 权限
  if (!auth.isAdmin) { message.warning(t('common.permissionDenied')); return }
  try {
    await editFormRef.value?.validate()
  } catch { return }
  // FIXED-Severe: 防止禁用或降级最后一个启用的管理员，避免系统锁定
  if (editForm.role !== 'admin' || editForm.enabled === false) {
    const activeAdmins = users.value.filter(
      (u: User) => u.role === 'admin' && u.enabled !== false && u.user_id !== editForm.user_id,
    )
    if (activeAdmins.length === 0) {
      message.error(t('userManage.cannotDisableLastAdmin'))
      return
    }
  }
  editing.value = true
  try {
    const data: any = { role: editForm.role, enabled: editForm.enabled }
    if (editForm.password) data.password = editForm.password
    await userApi.update(editForm.user_id, data)
    message.success(t('userManage.updateSuccess'))  // FIXED: 原问题-中文硬编码
    showEditModal.value = false
    fetchUsers()
  } catch (e: any) {
    message.error(extractError(e, t('userManage.updateFailed')))  // FIXED: 原问题-中文硬编码
  } finally {
    editing.value = false
  }
}

function handleDelete(row: User) {
  // FIXED-Severe(S-5): 删除用户需要 admin 权限
  if (!auth.isAdmin) { message.warning(t('common.permissionDenied')); return }
  // 防御性检查：避免删除最后一个启用的 admin（后端有最终保护，此处仅作前端兜底）
  if (row.role === 'admin' && row.enabled !== false) {
    const activeAdmins = users.value.filter(u => u.role === 'admin' && u.enabled !== false)
    if (activeAdmins.length <= 1) {
      message.error(t('userManage.cannotDeleteLastAdmin'))
      return
    }
  }
  dialog.warning({
    title: t('userManage.confirmDelete'),  // FIXED: 原问题-中文硬编码
    content: t('userManage.confirmDeleteUser', { username: row.username }),  // FIXED: 原问题-中文硬编码
    positiveText: t('common.delete'),  // FIXED: 原问题-中文硬编码
    negativeText: t('common.cancel'),  // FIXED: 原问题-中文硬编码
    onPositiveClick: async () => {
      try {
        await userApi.delete(row.user_id)
        message.success(t('userManage.deleteSuccess'))  // FIXED: 原问题-中文硬编码
        // [AUDIT-FIX] 严重级-删除最后一页最后一项后回退到上一页
        if (pagination.itemCount > 0) {
          const newTotal = pagination.itemCount - 1
          const maxPage = Math.max(1, Math.ceil(newTotal / pagination.pageSize))
          if (pagination.page > maxPage) pagination.page = maxPage
        }
        fetchUsers()
      } catch (e: any) {
        message.error(extractError(e, t('userManage.deleteFailed')))  // FIXED: 原问题-中文硬编码
      }
    },
  })
}

// 修复13/14: 标签页、权限矩阵、登录历史、在线用户（声明提前，确保 vue-tsc 能识别模板绑定）
const activeTab = ref('users')

// 修复14: 最后登录时间映射——从 audit 日志聚合（action=login）
const lastLoginMap = ref<Record<string, string>>({})

async function fetchLastLoginMap() {
  try {
    // 拉取最近 500 条登录日志，聚合每个用户的最后登录时间
    const data = await auditApi.list({ page: 1, size: 500, action: 'login' })
    const logs = data?.logs ?? []
    const map: Record<string, string> = {}
    for (const log of logs) {
      // audit 日志字段：user_id, timestamp / created_at / time
      const uid = log.user_id || log.username
      const ts = log.timestamp || log.created_at || log.time
      if (!uid || !ts) continue
      // 取最新的登录时间
      if (!map[uid] || new Date(ts).getTime() > new Date(map[uid]).getTime()) {
        map[uid] = ts
      }
    }
    lastLoginMap.value = map
  } catch {
    // 静默失败，不影响主流程
  }
}

// 修复13: 权限矩阵——只读视图，展示各角色对各资源的访问权限
// 权限定义：基于项目已知的前端权限校验（auth.isAdmin / auth.isOperator）
interface PermCell { allowed: boolean; label: string }
interface PermRow {
  resource: string
  admin: PermCell
  operator: PermCell
  viewer: PermCell
}

// 权限矩阵数据：admin 全部允许，operator 允许运维操作，viewer 只读
const permissionMatrixData = computed<PermRow[]>(() => [
  { resource: t('userManage.tabUsers'), admin: { allowed: true, label: '✓' }, operator: { allowed: false, label: '✗' }, viewer: { allowed: false, label: '✗' } },
  { resource: t('userManage.createUser'), admin: { allowed: true, label: '✓' }, operator: { allowed: false, label: '✗' }, viewer: { allowed: false, label: '✗' } },
  { resource: t('userManage.editUser'), admin: { allowed: true, label: '✓' }, operator: { allowed: false, label: '✗' }, viewer: { allowed: false, label: '✗' } },
  { resource: t('common.delete'), admin: { allowed: true, label: '✓' }, operator: { allowed: false, label: '✗' }, viewer: { allowed: false, label: '✗' } },
  { resource: t('router.devices'), admin: { allowed: true, label: '✓' }, operator: { allowed: true, label: '✓' }, viewer: { allowed: true, label: '✓' } },
  { resource: t('deviceDetail.dataWrite'), admin: { allowed: true, label: '✓' }, operator: { allowed: true, label: '✓' }, viewer: { allowed: false, label: '✗' } },
  { resource: t('router.alarms'), admin: { allowed: true, label: '✓' }, operator: { allowed: true, label: '✓' }, viewer: { allowed: true, label: '✓' } },
  { resource: t('router.system'), admin: { allowed: true, label: '✓' }, operator: { allowed: false, label: '✗' }, viewer: { allowed: false, label: '✗' } },
  { resource: t('router.audit'), admin: { allowed: true, label: '✓' }, operator: { allowed: false, label: '✗' }, viewer: { allowed: false, label: '✗' } },
])

function renderPermCell(cell: PermCell) {
  return h(NTag, {
    size: 'small',
    type: cell.allowed ? 'success' : 'default',
    bordered: false,
  }, { default: () => cell.label })
}

const permissionMatrixColumns = computed(() => [
  { title: t('userManage.permissionResource'), key: 'resource', width: 200 },
  { title: t('userManage.permissionAdmin'), key: 'admin', width: 120, align: 'center' as const, render: (r: PermRow) => renderPermCell(r.admin) },
  { title: t('userManage.permissionOperator'), key: 'operator', width: 120, align: 'center' as const, render: (r: PermRow) => renderPermCell(r.operator) },
  { title: t('userManage.permissionViewer'), key: 'viewer', width: 120, align: 'center' as const, render: (r: PermRow) => renderPermCell(r.viewer) },
])

// 修复14: 在线用户——基于最近 30 分钟的登录活动
interface OnlineUserRow {
  user_id: string
  username: string
  session_start: string
  client_ip: string
  user_agent: string
}

const onlineUsers = ref<OnlineUserRow[]>([])
const onlineLoading = ref(false)

const onlineColumns = computed(() => [
  { title: t('userManage.username'), key: 'username', width: 150 },
  { title: t('userManage.onlineSessionStart'), key: 'session_start', width: 180, render: (r: OnlineUserRow) => r.session_start ? formatDateTime(r.session_start) : '-' },
  { title: t('userManage.onlineClientIp'), key: 'client_ip', width: 140, render: (r: OnlineUserRow) => r.client_ip || '-' },
  {
    title: t('userManage.onlineUserAgent'), key: 'user_agent', width: 300,
    render: (r: OnlineUserRow) => {
      const ua = r.user_agent || '-'
      // 截断过长的 UA
      const display = ua.length > 60 ? ua.substring(0, 60) + '...' : ua
      return h(NText, { depth: 3, style: 'font-size: 12px', title: ua }, { default: () => display })
    },
  },
])

async function fetchOnlineUsers() {
  onlineLoading.value = true
  try {
    // 拉取最近 30 分钟的登录日志
    const since = new Date(Date.now() - 30 * 60 * 1000).toISOString()
    const data = await auditApi.list({ page: 1, size: 200, action: 'login', start_time: since })
    const logs = data?.logs ?? []
    // 按用户去重，保留最新一条
    const userMap = new Map<string, OnlineUserRow>()
    for (const log of logs) {
      const key = log.user_id || log.username || ''
      if (!key) continue
      const ts = log.timestamp || log.created_at || log.time || ''
      const existing = userMap.get(key)
      if (!existing || (ts && new Date(ts).getTime() > new Date(existing.session_start).getTime())) {
        userMap.set(key, {
          user_id: log.user_id || '',
          username: log.username || log.user_id || '',
          session_start: ts,
          client_ip: log.client_ip || log.ip || log.details?.client_ip || '',
          user_agent: log.user_agent || log.details?.user_agent || '',
        })
      }
    }
    onlineUsers.value = Array.from(userMap.values())
  } catch (e: any) {
    message.error(extractError(e, t('userManage.loadLoginHistoryFailed')))
    onlineUsers.value = []
  } finally {
    onlineLoading.value = false
  }
}

// 切换到在线用户标签时自动加载
watch(activeTab, (val) => {
  if (val === 'online' && onlineUsers.value.length === 0) {
    fetchOnlineUsers()
  }
})

// 用户列表加载后，同时加载最后登录时间
watch(users, () => {
  if (users.value.length && Object.keys(lastLoginMap.value).length === 0) {
    fetchLastLoginMap()
  }
})

// 生命周期钩子放在最后，确保所有模板绑定的变量已声明
onMounted(fetchUsers)

// [FIX] searchTimer 内存泄漏：组件卸载时清理防抖定时器
onUnmounted(() => {
  if (searchTimer) clearTimeout(searchTimer)
})
</script>
