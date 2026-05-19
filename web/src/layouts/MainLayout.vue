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
          <n-button quaternary circle @click="toggleLocale">
            <template #icon><n-icon :component="LanguageOutline" /></template>
          </n-button>
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
    <n-modal v-model:show="showChangePwd" :mask-closable="true" :close-on-esc="true" :title="'🔒 ' + t('login.changePassword')" preset="card" style="width: 440px">  <!-- FIXED: 原问题-中文硬编码，改用i18n -->
      <n-alert v-if="auth.mustChangePassword" type="info" style="margin-bottom: 16px">
        {{ t('login.changePasswordSecurityHint') }}  <!-- FIXED: 原问题-中文硬编码，改用i18n -->
      </n-alert>
      <n-form :model="pwdForm" :rules="pwdRules" ref="pwdFormRef" label-placement="left" label-width="90">
        <n-form-item :label="t('login.oldPassword')" path="oldPassword"><n-input v-model:value="pwdForm.oldPassword" type="password" show-password-on="click" :placeholder="t('login.oldPasswordPlaceholder')" /></n-form-item>  <!-- FIXED: 原问题-中文硬编码，改用i18n -->
        <n-form-item :label="t('login.newPassword')" path="newPassword"><n-input v-model:value="pwdForm.newPassword" type="password" show-password-on="click" :placeholder="t('login.newPasswordPlaceholder')" /></n-form-item>  <!-- FIXED: 原问题-中文硬编码，改用i18n -->
        <n-form-item :label="t('login.confirmPassword')" path="confirmPassword"><n-input v-model:value="pwdForm.confirmPassword" type="password" show-password-on="click" :placeholder="t('login.confirmPasswordPlaceholder')" /></n-form-item>  <!-- FIXED: 原问题-中文硬编码，改用i18n -->
      </n-form>
      <template #action>
        <n-space justify="end">
          <n-button @click="showChangePwd = false">{{ t('common.cancel') }}</n-button>  <!-- FIXED: 原问题-中文硬编码，改用i18n -->
          <n-button type="primary" :loading="changingPwd" @click="handleChangePassword">{{ t('login.saveChanges') }}</n-button>  <!-- FIXED: 原问题-中文硬编码，改用i18n -->
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
  ExtensionPuzzleOutline, LanguageOutline,
} from '@vicons/ionicons5'
import { useAuthStore } from '@/stores/auth'
import { alarmApi, authApi } from '@/api'
import { t, setLocale, getLocale } from '@/i18n'
import * as ws from '@/api/websocket'

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
  oldPassword: { required: true, message: t('login.oldPasswordRequired'), trigger: 'blur' },  // FIXED: 原问题-中文硬编码，改用i18n
  newPassword: {
    required: true,
    trigger: 'blur',
    validator: (_rule: any, value: string) => {
      if (!value) return new Error(t('login.newPasswordRequired'))  // FIXED: 原问题-中文硬编码，改用i18n
      if (value.length < 8) return new Error(t('login.passwordPolicy'))  // FIXED: 原问题-中文硬编码，改用i18n
      if (!/[a-zA-Z]/.test(value) || !/[0-9]/.test(value)) return new Error(t('login.passwordLetterAndDigit'))  // FIXED: 原问题-中文硬编码，改用i18n
      return true
    },
  },
  confirmPassword: {
    required: true,
    trigger: 'blur',
    validator: (_rule: any, value: string) => {
      if (value !== pwdForm.value.newPassword) return new Error(t('login.passwordMismatch'))  // FIXED: 原问题-中文硬编码，改用i18n
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
    message.success(t('login.passwordChanged'))  // FIXED: 原问题-中文硬编码，改用i18n
    auth.mustChangePassword = false
    sessionStorage.setItem('edgelite_mustChangePassword', 'false')
    showChangePwd.value = false
    // FIXED: 原问题-修改密码成功后未重新登录获取新token，若后端使旧token失效则后续请求401
    const newPassword = pwdForm.value.newPassword
    pwdForm.value = { oldPassword: '', newPassword: '', confirmPassword: '' }
    try {
      await auth.login(auth.username, newPassword)
    } catch {
      // FIXED: 原问题-重新登录失败时静默登出无提示，用户不知道为何被踢出
      message.error(t('login.passwordChangeFailed'))
      await auth.logout()
      router.push('/login')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || t('login.passwordChangeFailed'))  // FIXED: 原问题-中文硬编码，改用i18n
  } finally {
    changingPwd.value = false
  }
}

const toggleTheme = inject<() => void>('toggleTheme', () => {})
const isDark = inject<Ref<boolean>>('isDark', ref(false))
const version = __APP_VERSION__ || '1.0.0'

function toggleLocale() {
  const next = getLocale() === 'zh-CN' ? 'en-US' : 'zh-CN'
  setLocale(next)
  window.location.reload()
}

const currentRoute = computed(() => {
  const name = route.name as string
  if (name === 'DeviceDetail') return 'Devices'
  return name
})
const currentTitle = computed(() => {  // FIXED: 原问题-中文硬编码，改用i18n
  const titleKeys: Record<string, string> = {
    Dashboard: 'nav.dashboard', Devices: 'nav.devices', DeviceDetail: 'nav.deviceDetail',
    Rules: 'nav.rules', Alarms: 'nav.alarms', DataQuery: 'nav.dataQuery',
    DigitalTwin: 'nav.digitalTwin', ScadaEditor: 'nav.scadaEditor',
    DriverConfig: 'nav.driverConfig', PlatformConfig: 'nav.platformConfig', ExpressionConfig: 'nav.expressionConfig',
    PreprocessConfig: 'nav.preprocessConfig',
    System: 'nav.system', Users: 'nav.users',
    AuditLog: 'nav.auditLog',
    OtaUpdate: 'nav.otaUpdate', GrafanaDashboard: 'nav.grafanaDashboard', McpServer: 'nav.mcpServer',
    MqttServer: 'nav.mqttServer', ModbusSlave: 'nav.modbusSlave', SerialBridge: 'nav.serialBridge',
    ServiceOverview: 'nav.serviceOverview',
  }
  const key = titleKeys[route.name as string]
  return key ? t(key) : 'EdgeLiteGateway'
})

const breadcrumbItems = computed(() => {  // FIXED: 原问题-中文硬编码，改用i18n
  const titleKeys: Record<string, string> = {
    Dashboard: 'nav.dashboard', Devices: 'nav.devices', DeviceDetail: 'nav.deviceDetail',
    Rules: 'nav.rules', Alarms: 'nav.alarms', DataQuery: 'nav.dataQuery',
    DigitalTwin: 'nav.digitalTwin', ScadaEditor: 'nav.scadaEditor',
    DriverConfig: 'nav.driverConfig', PlatformConfig: 'nav.platformConfig', ExpressionConfig: 'nav.expressionConfig',
    PreprocessConfig: 'nav.preprocessConfig',
    System: 'nav.system', Users: 'nav.users', AuditLog: 'nav.auditLog',
    OtaUpdate: 'nav.otaUpdate', GrafanaDashboard: 'nav.grafanaDashboard', McpServer: 'nav.mcpServer',
    MqttServer: 'nav.mqttServer', ModbusSlave: 'nav.modbusSlave', SerialBridge: 'nav.serialBridge',
    ServiceOverview: 'nav.serviceOverview',
  }
  return route.matched
    .filter(r => r.name)
    .map(r => {
      const key = titleKeys[r.name as string]
      return { path: r.path, title: key ? t(key) : (r.name as string) }
    })
})

const roleLabel = computed(() => ({ admin: t('role.admin'), operator: t('role.operator'), viewer: t('role.viewer') }[auth.role] || auth.role))  // FIXED: 原问题-中文硬编码，改用i18n
const roleType = computed(() => ({ admin: 'error', operator: 'warning', viewer: 'info' }[auth.role] || 'default') as any)

const renderIcon = (icon: any) => () => h(NIcon, { component: icon, size: 18 })

const allMenuOptions = [  // FIXED: 原问题-中文硬编码，改用i18n
  { label: t('nav.dashboard'), key: 'Dashboard', icon: renderIcon(SpeedometerOutline) },
  { label: t('nav.devices'), key: 'Devices', icon: renderIcon(HardwareChip) },
  { label: t('nav.rules'), key: 'Rules', icon: renderIcon(SettingsOutline) },
  { label: t('nav.alarms'), key: 'Alarms', icon: renderIcon(AlertCircleOutline) },
  { label: t('nav.dataQuery'), key: 'DataQuery', icon: renderIcon(StatsChartOutline) },
  {
    label: t('nav.visualGroup'), key: 'visual-group', icon: renderIcon(CubeOutline),
    children: [
      { label: t('nav.digitalTwinMenu'), key: 'DigitalTwin', icon: renderIcon(CubeOutline) },
      { label: t('nav.scadaEditorMenu'), key: 'ScadaEditor', icon: renderIcon(BuildOutline) },
    ],
  },
  {
    label: t('nav.serviceGroup'), key: 'service-group', icon: renderIcon(RadioOutline), adminOnly: true,
    children: [
      { label: t('nav.serviceOverview'), key: 'ServiceOverview', icon: renderIcon(RadioOutline) },
      { label: t('nav.mqttServer'), key: 'MqttServer', icon: renderIcon(RadioOutline) },
      { label: t('nav.modbusSlave'), key: 'ModbusSlave', icon: renderIcon(PowerOutline) },
      { label: t('nav.serialBridge'), key: 'SerialBridge', icon: renderIcon(SwapHorizontalOutline) },
      { label: t('nav.grafanaDashboard'), key: 'GrafanaDashboard', icon: renderIcon(BarChartOutline) },
      { label: t('nav.mcpServer'), key: 'McpServer', icon: renderIcon(ExtensionPuzzleOutline) },
    ],
  },
  {
    label: t('nav.systemGroup'), key: 'system-group', icon: renderIcon(ServerOutline), adminOnly: true,
    children: [
      { label: t('nav.system'), key: 'System', icon: renderIcon(ServerOutline) },
      { label: t('nav.driverConfig'), key: 'DriverConfig', icon: renderIcon(PulseOutline) },
      { label: t('nav.platformConfig'), key: 'PlatformConfig', icon: renderIcon(CloudOutline) },
      { label: t('nav.expressionConfig'), key: 'ExpressionConfig', icon: renderIcon(CalculatorOutline) },
      { label: t('nav.preprocessConfig'), key: 'PreprocessConfig', icon: renderIcon(PulseOutline) },
      { label: t('nav.auditLog'), key: 'AuditLog', icon: renderIcon(DocumentTextOutline) },
      { label: t('nav.users'), key: 'Users', icon: renderIcon(PeopleOutline) },
      { label: t('nav.otaUpdate'), key: 'OtaUpdate', icon: renderIcon(RocketOutline) },
    ],
  },
]

const menuOptions = computed(() => {
  if (auth.role === 'admin') return allMenuOptions
  return allMenuOptions.filter(item => !(item as any).adminOnly)
})

const userOptions = [  // FIXED: 原问题-中文硬编码，改用i18n
  { label: t('nav.changePassword'), key: 'changePassword', icon: renderIcon(DocumentTextOutline) },
  { label: t('nav.logout'), key: 'logout', icon: renderIcon(LogOutOutline) },
]

function handleMenuClick(key: string) { router.push({ name: key }) }

function handleUserSelect(key: string) {
  if (key === 'changePassword') {
    pwdForm.value = { oldPassword: '', newPassword: '', confirmPassword: '' }
    showChangePwd.value = true
  } else if (key === 'logout') {
    dialog.warning({  // FIXED: 原问题-中文硬编码，改用i18n
      title: t('nav.logoutConfirmTitle'),
      content: t('nav.logoutConfirmContent'),
      positiveText: t('nav.logoutConfirmBtn'),
      negativeText: t('common.cancel'),
      onPositiveClick: async () => { await auth.logout(); router.push('/login') },
    })
  }
}

async function fetchAlarmCount() {
  try {
    const data = await alarmApi.list({ page: 1, size: 1, status: 'firing' })
    // FIXED: 原问题-data.total可能为undefined，添加空值保护
    alarmCount.value = data?.total ?? 0
  } catch (e) { console.warn('Failed to fetch alarm count:', e) }  // FIXED: 原问题-硬编码中文label
}

// FIXED: 原问题-告警徽章30秒轮询延迟，不监听WS，现添加WS alarm频道监听，收到消息时立即更新告警计数（带5秒防抖）
let _wsAlarmTimer: ReturnType<typeof setTimeout> | null = null
function onAlarmWsMessage(_data: any) {
  try {
    if (_wsAlarmTimer) clearTimeout(_wsAlarmTimer)
    _wsAlarmTimer = setTimeout(() => {
      fetchAlarmCount()
      _wsAlarmTimer = null
    }, 5000)
  } catch { /* ignore */ }
}

onMounted(() => { fetchAlarmCount(); alarmTimer = window.setInterval(fetchAlarmCount, 30000); ws.connect('alarm', onAlarmWsMessage) })
onUnmounted(() => { if (alarmTimer) clearInterval(alarmTimer); if (_wsAlarmTimer) clearTimeout(_wsAlarmTimer); ws.disconnect('alarm', onAlarmWsMessage) })
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
.logo-title { font-size: 18px; font-weight: 700; }
.logo-subtitle { font-size: 11px; opacity: 0.6; letter-spacing: 2px; }
.sider-footer {
  position: absolute; bottom: 12px; left: 0; right: 0;
  text-align: center; padding: 8px;
}
.header {
  height: 56px;
  display: flex; align-items: center; padding: 0 20px;
  justify-content: space-between;
}
.main-content {
  height: calc(100vh - 56px);
  overflow: auto;
}
.fade-enter-active, .fade-leave-active { transition: opacity 0.2s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; }
</style>
