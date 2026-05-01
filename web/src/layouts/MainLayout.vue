<template>
  <n-layout has-sider style="height: 100vh">
    <n-layout-sider
      bordered
      collapse-mode="width"
      :collapsed-width="64"
      :width="240"
      :collapsed="collapsed"
      show-trigger
      @collapse="collapsed = true"
      @expand="collapsed = false"
    >
      <div class="logo">
        <div class="logo-icon">
          <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128" width="28" height="28">
            <defs>
              <linearGradient id="crystalGrad1" x1="0%" y1="0%" x2="100%" y2="100%">
                <stop offset="0%" style="stop-color:#0f172a;stop-opacity:1" />
                <stop offset="50%" style="stop-color:#1e293b;stop-opacity:1" />
                <stop offset="100%" style="stop-color:#0f172a;stop-opacity:1" />
              </linearGradient>
              <linearGradient id="crystalGrad2" x1="0%" y1="0%" x2="100%" y2="100%">
                <stop offset="0%" style="stop-color:#38bdf8;stop-opacity:1" />
                <stop offset="50%" style="stop-color:#818cf8;stop-opacity:1" />
                <stop offset="100%" style="stop-color:#a78bfa;stop-opacity:1" />
              </linearGradient>
              <linearGradient id="crystalGrad3" x1="0%" y1="100%" x2="100%" y2="0%">
                <stop offset="0%" style="stop-color:#22d3ee;stop-opacity:1" />
                <stop offset="100%" style="stop-color:#f472b6;stop-opacity:1" />
              </linearGradient>
              <filter id="crystalGlow">
                <feGaussianBlur stdDeviation="4" result="coloredBlur"/>
                <feMerge>
                  <feMergeNode in="coloredBlur"/>
                  <feMergeNode in="SourceGraphic"/>
                </feMerge>
              </filter>
              <filter id="crystalShine">
                <feGaussianBlur stdDeviation="1" result="blur"/>
                <feOffset dx="0" dy="-1" result="offsetBlur"/>
                <feComposite in="SourceGraphic" in2="offsetBlur" operator="arithmetic" k2="0" k3="0" k4="1"/>
              </filter>
            </defs>
            <polygon points="64,8 108,36 108,84 64,112 20,84 20,36" fill="url(#crystalGrad1)" opacity="0.95"/>
            <polygon points="64,14 102,38 102,82 64,106 26,82 26,38" fill="none" stroke="url(#crystalGrad2)" stroke-width="1" opacity="0.6"/>
            <polygon points="64,6 112,34 112,86 64,114 16,86 16,34" fill="none" stroke="url(#crystalGrad2)" stroke-width="0.5" opacity="0.3"/>
            <polygon points="64,24 88,38 88,78 64,92 40,78 40,38" fill="url(#crystalGrad2)" filter="url(#crystalGlow)"/>
            <polygon points="64,24 64,92 40,78 40,38" fill="url(#crystalGrad2)" opacity="0.7"/>
            <polygon points="64,24 88,38 88,78 64,92" fill="url(#crystalGrad3)" opacity="0.5"/>
            <polygon points="64,30 82,42 82,74 64,86 46,74 46,42" fill="url(#crystalGrad1)" opacity="0.8"/>
            <polygon points="64,36 76,46 76,72 64,82 52,72 52,46" fill="url(#crystalGrad2)"/>
            <polygon points="64,36 64,82 52,72 52,46" fill="url(#crystalGrad2)" opacity="0.8"/>
            <polygon points="64,36 76,46 76,72 64,82" fill="url(#crystalGrad3)" opacity="0.6"/>
            <polygon points="64,44 72,49 72,69 64,74 56,69 56,49" fill="white" opacity="0.9"/>
            <polygon points="64,24 64,36 46,42 40,38" fill="white" opacity="0.15"/>
            <polygon points="64,24 88,38 82,42 64,36" fill="white" opacity="0.25"/>
            <line x1="40" y1="38" x2="46" y2="42" stroke="url(#crystalGrad3)" stroke-width="0.8" opacity="0.6"/>
            <line x1="88" y1="38" x2="82" y2="42" stroke="url(#crystalGrad3)" stroke-width="0.8" opacity="0.6"/>
            <line x1="40" y1="78" x2="46" y2="74" stroke="url(#crystalGrad3)" stroke-width="0.8" opacity="0.6"/>
            <line x1="88" y1="78" x2="82" y2="74" stroke="url(#crystalGrad3)" stroke-width="0.8" opacity="0.6"/>
          </svg>   
        </div>
        <transition name="fade">
          <div v-if="!collapsed" class="logo-text">
            <span class="logo-title">EdgeLite</span>
            <span class="logo-subtitle">Gateway</span>
          </div>
        </transition>
      </div>
      <n-menu
        :collapsed="collapsed"
        :collapsed-width="64"
        :collapsed-icon-size="20"
        :options="menuOptions"
        :value="currentRoute"
        :indent="20"
        @update:value="handleMenuClick"
      />
      <div class="sider-footer">
        <n-text v-if="!collapsed" depth="3" style="font-size: 12px">v{{ version }} Community</n-text>
      </div>
    </n-layout-sider>
    <n-layout>
      <n-layout-header bordered class="header">
        <n-breadcrumb>
          <template #separator><n-icon :component="ChevronForwardOutline" /></template>
          <n-breadcrumb-item v-for="item in breadcrumbItems" :key="item.path">
            {{ item.title }}
          </n-breadcrumb-item>
        </n-breadcrumb>
        <n-space align="center" :size="16">
          <n-button quaternary circle @click="toggleTheme">
            <template #icon><n-icon :component="isDark ? SunnyOutline : MoonOutline" /></template>
          </n-button>
          <n-badge :value="alarmCount" :max="99" :show="alarmCount > 0">
            <n-button quaternary circle @click="router.push({ name: 'Alarms' })">
              <template #icon><n-icon :component="NotificationsOutline" /></template>
            </n-button>
          </n-badge>
          <n-dropdown :options="userOptions" @select="handleUserSelect">
            <n-button quaternary>
              <template #icon><n-icon :component="UserAvatar" /></template>
              {{ auth.username }}
              <n-tag :bordered="false" :type="roleType" size="small" style="margin-left: 8px">{{ roleLabel }}</n-tag>
            </n-button>
          </n-dropdown>
        </n-space>
      </n-layout-header>
      <n-layout-content content-style="padding: 20px" class="main-content">
        <router-view />
      </n-layout-content>
    </n-layout>
    <n-modal v-model:show="showChangePwd" :mask-closable="true" :close-on-esc="true" title="🔒 修改密码" preset="card" style="width: 440px">
      <n-alert v-if="auth.mustChangePassword" type="info" style="margin-bottom: 16px">
        为保障您的账户安全，建议修改默认密码后再使用系统
      </n-alert>
      <n-form :model="pwdForm" :rules="pwdRules" ref="pwdFormRef" label-placement="left" label-width="90">
        <n-form-item label="当前密码" path="oldPassword"><n-input v-model:value="pwdForm.oldPassword" type="password" show-password-on="click" placeholder="请输入当前使用的密码" /></n-form-item>
        <n-form-item label="新密码" path="newPassword"><n-input v-model:value="pwdForm.newPassword" type="password" show-password-on="click" placeholder="至少6位，建议包含字母和数字" /></n-form-item>
        <n-form-item label="确认新密码" path="confirmPassword"><n-input v-model:value="pwdForm.confirmPassword" type="password" show-password-on="click" placeholder="请再次输入新密码" /></n-form-item>
      </n-form>
      <template #action>
        <n-space justify="end">
          <n-button @click="showChangePwd = false">稍后再说</n-button>
          <n-button type="primary" :loading="changingPwd" @click="handleChangePassword">保存修改</n-button>
        </n-space>
      </template>
    </n-modal>
  </n-layout>
</template>

<script setup lang="ts">
import { ref, computed, h, inject, onMounted, onUnmounted, watch, type Ref } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { NIcon, useDialog, useMessage } from 'naive-ui'
import {
  SpeedometerOutline, HardwareChip, SettingsOutline, AlertCircleOutline,
  DesktopOutline, PeopleOutline, ChevronForwardOutline, NotificationsOutline, PersonOutline as UserAvatar,
  LogOutOutline, StatsChartOutline, ServerOutline, CubeOutline, BuildOutline, DocumentTextOutline,
  PulseOutline, CloudOutline, CalculatorOutline, MoonOutline, SunnyOutline,
  RocketOutline, BarChartOutline, RadioOutline, PowerOutline, SwapHorizontalOutline,
  ExtensionPuzzleOutline,
} from '@vicons/ionicons5'
import { useAuthStore } from '@/stores/auth'
import { alarmApi, authApi } from '@/api'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()
const dialog = useDialog()
const message = useMessage()
const collapsed = ref(false)
const alarmCount = ref(0)
let alarmTimer: number | null = null

const showChangePwd = ref(false)
const changingPwd = ref(false)
const pwdFormRef = ref<any>(null)
const pwdForm = ref({ oldPassword: '', newPassword: '', confirmPassword: '' })
const pwdRules = {
  oldPassword: { required: true, message: '请输入当前密码', trigger: 'blur' },
  newPassword: { required: true, min: 6, message: '新密码至少需要6个字符', trigger: 'blur' },
  confirmPassword: {
    required: true,
    trigger: 'blur',
    validator: (_rule: any, value: string) => {
      if (value !== pwdForm.value.newPassword) return new Error('两次输入的密码不一致，请重新输入')
      return true
    },
  },
}

watch(() => auth.mustChangePassword, (val) => {
  if (val) showChangePwd.value = true
}, { immediate: true })

async function handleChangePassword() {
  try { await pwdFormRef.value?.validate() } catch { return }
  changingPwd.value = true
  try {
    await authApi.changePassword(pwdForm.value.oldPassword, pwdForm.value.newPassword)
    message.success('密码修改成功')
    auth.mustChangePassword = false
    sessionStorage.setItem('edgelite_mustChangePassword', 'false')
    showChangePwd.value = false
    pwdForm.value = { oldPassword: '', newPassword: '', confirmPassword: '' }
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || '修改失败')
  } finally {
    changingPwd.value = false
  }
}

const toggleTheme = inject<() => void>('toggleTheme', () => {})
const isDark = inject<Ref<boolean>>('isDark', ref(false))
const version = __APP_VERSION__ || '1.0.0'

const currentRoute = computed(() => {
  const name = route.name as string
  if (name === 'DeviceDetail') return 'Devices'
  return name
})
const currentTitle = computed(() => {
  const titles: Record<string, string> = {
    Dashboard: '仪表盘', Devices: '设备管理', DeviceDetail: '设备详情',
    Rules: '规则管理', Alarms: '告警中心', DataQuery: '数据查询',
    DigitalTwin: '3D数字孪生', ScadaEditor: 'Web组态',
    DriverConfig: '驱动配置', PlatformConfig: '平台对接', ExpressionConfig: '计算表达式',
    PreprocessConfig: '数据预处理',
    System: '系统管理', Users: '用户管理',
    OtaUpdate: 'OTA升级', GrafanaDashboard: 'Grafana监控', McpServer: 'MCP Server',
    MqttServer: 'MQTT Server', ModbusSlave: 'Modbus Slave', SerialBridge: '串口透传',
  }
  return titles[route.name as string] || 'EdgeLiteGateway'
})

const breadcrumbItems = computed(() => {
  const titleMap: Record<string, string> = {
    Dashboard: '仪表盘', Devices: '设备管理', DeviceDetail: '设备详情',
    Rules: '规则管理', Alarms: '告警中心', DataQuery: '数据查询',
    DigitalTwin: '3D数字孪生', ScadaEditor: 'Web组态',
    DriverConfig: '驱动配置', PlatformConfig: '平台对接', ExpressionConfig: '计算表达式',
    PreprocessConfig: '数据预处理',
    System: '系统管理', Users: '用户管理',
  }
  return route.matched
    .filter(r => r.name)
    .map(r => ({ path: r.path, title: titleMap[r.name as string] || (r.name as string) }))
})

const roleLabel = computed(() => ({ admin: '管理员', operator: '操作员', viewer: '观察者' }[auth.role] || auth.role))
const roleType = computed(() => ({ admin: 'error', operator: 'warning', viewer: 'info' }[auth.role] || 'default') as any)

const renderIcon = (icon: any) => () => h(NIcon, { component: icon, size: 18 })

const menuOptions = [
  { label: '仪表盘', key: 'Dashboard', icon: renderIcon(SpeedometerOutline) },
  { label: '设备管理', key: 'Devices', icon: renderIcon(HardwareChip) },
  { label: '规则管理', key: 'Rules', icon: renderIcon(SettingsOutline) },
  { label: '告警中心', key: 'Alarms', icon: renderIcon(AlertCircleOutline) },
  { label: '数据查询', key: 'DataQuery', icon: renderIcon(StatsChartOutline) },
  {
    label: '可视化', key: 'visual-group', icon: renderIcon(CubeOutline),
    children: [
      { label: '数字孪生', key: 'DigitalTwin', icon: renderIcon(CubeOutline) },
      { label: '组态编辑', key: 'ScadaEditor', icon: renderIcon(BuildOutline) },
    ],
  },
  {
    label: '服务管理', key: 'service-group', icon: renderIcon(RadioOutline),
    children: [
      { label: '服务总览', key: 'ServiceOverview', icon: renderIcon(RadioOutline) },
      { label: 'MQTT Server', key: 'MqttServer', icon: renderIcon(RadioOutline) },
      { label: 'Modbus Slave', key: 'ModbusSlave', icon: renderIcon(PowerOutline) },
      { label: '串口透传', key: 'SerialBridge', icon: renderIcon(SwapHorizontalOutline) },
      { label: 'Grafana监控', key: 'GrafanaDashboard', icon: renderIcon(BarChartOutline) },
      { label: 'MCP Server', key: 'McpServer', icon: renderIcon(ExtensionPuzzleOutline) },
    ],
  },
  {
    label: '系统配置', key: 'system-group', icon: renderIcon(ServerOutline),
    children: [
      { label: '系统管理', key: 'System', icon: renderIcon(ServerOutline) },
      { label: '驱动配置', key: 'DriverConfig', icon: renderIcon(PulseOutline) },
      { label: '平台对接', key: 'PlatformConfig', icon: renderIcon(CloudOutline) },
      { label: '计算表达式', key: 'ExpressionConfig', icon: renderIcon(CalculatorOutline) },
      { label: '数据预处理', key: 'PreprocessConfig', icon: renderIcon(PulseOutline) },
      { label: '用户管理', key: 'Users', icon: renderIcon(PeopleOutline) },
      { label: 'OTA升级', key: 'OtaUpdate', icon: renderIcon(RocketOutline) },
    ],
  },
]

const userOptions = [
  { label: '修改密码', key: 'changePassword', icon: renderIcon(DocumentTextOutline) },
  { label: '退出登录', key: 'logout', icon: renderIcon(LogOutOutline) },
]

function handleMenuClick(key: string) { router.push({ name: key }) }

function handleUserSelect(key: string) {
  if (key === 'changePassword') {
    pwdForm.value = { oldPassword: '', newPassword: '', confirmPassword: '' }
    showChangePwd.value = true
  } else if (key === 'logout') {
    dialog.warning({
      title: '确认退出',
      content: '确定要退出登录吗？',
      positiveText: '退出',
      negativeText: '取消',
      onPositiveClick: async () => { await auth.logout(); router.push('/login') },
    })
  }
}

async function fetchAlarmCount() {
  try {
    const data = await alarmApi.list({ page: 1, size: 1, status: 'firing' })
    alarmCount.value = data.total
  } catch (e) { console.warn('获取告警计数失败:', e) }
}

onMounted(() => { fetchAlarmCount(); alarmTimer = window.setInterval(fetchAlarmCount, 30000) })
onUnmounted(() => { if (alarmTimer) clearInterval(alarmTimer) })
</script>

<style scoped>
.logo {
  height: 64px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 0 16px;
  border-bottom: 1px solid var(--n-border-color);
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  -webkit-background-clip: text;
}
.logo-icon { display: flex; align-items: center; }
.logo-text { display: flex; flex-direction: column; line-height: 1.2; }
.logo-title { font-size: 18px; font-weight: 700; color: #333; }
.logo-subtitle { font-size: 11px; color: #999; letter-spacing: 2px; }
.sider-footer {
  position: absolute; bottom: 12px; left: 0; right: 0;
  text-align: center; padding: 8px;
}
.header {
  height: 56px;
  display: flex; align-items: center; padding: 0 20px;
  justify-content: space-between;
  background: #fff;
}
.main-content {
  height: calc(100vh - 56px);
  overflow: auto;
  background: #f5f7fa;
}
.fade-enter-active, .fade-leave-active { transition: opacity 0.2s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; }
</style>
