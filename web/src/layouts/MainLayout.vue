<template>
  <n-layout has-sider style="height: 100vh">
    <!-- Mobile drawer sidebar -->
    <n-drawer v-if="isMobile" :show="sidebarVisible" :width="240" placement="left" @update:show="sidebarVisible = $event">
      <n-drawer-content :native-scrollbar="false" body-content-style="padding: 0;">
        <div style="display: flex; justify-content: flex-end; padding: 8px 12px 0;">
          <n-button quaternary circle size="small" :aria-label="t('common.ariaCloseMenu')" @click="sidebarVisible = false">
            <template #icon><n-icon :component="CloseOutline" /></template>
          </n-button>
        </div>
        <div class="logo">
          <div class="logo-icon">
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128" width="28" height="28">
              <defs>
                <linearGradient id="crystalGrad1m" x1="0%" y1="0%" x2="100%" y2="100%">
                  <stop offset="0%" style="stop-color:#0f172a;stop-opacity:1" />
                  <stop offset="50%" style="stop-color:#1e293b;stop-opacity:1" />
                  <stop offset="100%" style="stop-color:#0f172a;stop-opacity:1" />
                </linearGradient>
                <linearGradient id="crystalGrad2m" x1="0%" y1="0%" x2="100%" y2="100%">
                  <stop offset="0%" style="stop-color:#38bdf8;stop-opacity:1" />
                  <stop offset="50%" style="stop-color:#818cf8;stop-opacity:1" />
                  <stop offset="100%" style="stop-color:#a78bfa;stop-opacity:1" />
                </linearGradient>
                <linearGradient id="crystalGrad3m" x1="0%" y1="100%" x2="100%" y2="0%">
                  <stop offset="0%" style="stop-color:#22d3ee;stop-opacity:1" />
                  <stop offset="100%" style="stop-color:#f472b6;stop-opacity:1" />
                </linearGradient>
                <filter id="crystalGlowm">
                  <feGaussianBlur stdDeviation="4" result="coloredBlur"/>
                  <feMerge>
                    <feMergeNode in="coloredBlur"/>
                    <feMergeNode in="SourceGraphic"/>
                  </feMerge>
                </filter>
              </defs>
              <polygon points="64,8 108,36 108,84 64,112 20,84 20,36" fill="url(#crystalGrad1m)" opacity="0.95"/>
              <polygon points="64,24 88,38 88,78 64,92 40,78 40,38" fill="url(#crystalGrad2m)" filter="url(#crystalGlowm)"/>
              <polygon points="64,36 76,46 76,72 64,82 52,72 52,46" fill="url(#crystalGrad2m)"/>
              <polygon points="64,44 72,49 72,69 64,74 56,69 56,49" fill="white" opacity="0.9"/>
            </svg>
          </div>
          <div class="logo-text">
            <span class="logo-title">EdgeLite</span>
            <span class="logo-subtitle">Gateway</span>
          </div>
        </div>
        <n-menu
          :options="menuOptions"
          :value="currentRoute"
          :indent="20"
          @update:value="handleMenuClick"
        />
        <div class="sider-footer">
          <n-text depth="3" style="font-size: 12px">v{{ version }} Community</n-text>
        </div>
      </n-drawer-content>
    </n-drawer>
    <!-- Desktop sidebar -->
    <n-layout-sider
      v-if="!isMobile"
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
        <n-button v-if="isMobile" quaternary circle :aria-label="t('common.ariaOpenMenu')" @click="sidebarVisible = true" style="margin-right: 8px">
          <template #icon><n-icon :component="MenuOutline" /></template>
        </n-button>
        <n-breadcrumb>
          <template #separator><n-icon :component="ChevronForwardOutline" /></template>
          <n-breadcrumb-item v-for="(item, idx) in breadcrumbItems" :key="item.path" @click="idx < breadcrumbItems.length - 1 && router.push(item.path)">
            {{ item.title }}
          </n-breadcrumb-item>
        </n-breadcrumb>
        <n-space align="center" :size="16">
          <n-tooltip v-if="!isOnline">
            <template #trigger>
              <n-badge value="!" type="warning" :offset="[-4, 4]">
                <n-button quaternary circle :aria-label="t('common.ariaNetworkOffline')" @click="checkOnline">
                  <template #icon><n-icon :component="CloudOfflineSharp" /></template>
                </n-button>
              </n-badge>
            </template>
            {{ t('network.offline') }}
          </n-tooltip>
          <n-button quaternary circle :aria-label="t('common.ariaSearch')" @click="showCommandPalette = true">
            <template #icon><n-icon :component="SearchOutline" /></template>
          </n-button>
          <n-tooltip>
            <template #trigger>
              <n-button quaternary circle :aria-label="t('common.ariaKeyboardShortcuts')" @click="showShortcutHelp = true">
                <template #icon><n-icon :component="KeyOutline" /></template>
              </n-button>
            </template>
            {{ t('common.keyboardShortcuts') }} (?)
          </n-tooltip>
          <n-button quaternary circle :aria-label="t('common.ariaToggleLanguage')" @click="toggleLocale">
            <template #icon><n-icon :component="LanguageOutline" /></template>
          </n-button>
          <n-button quaternary circle :aria-label="t('common.ariaToggleTheme')" @click="toggleTheme">
            <template #icon><n-icon :component="isDark ? SunnyOutline : MoonOutline" /></template>
          </n-button>
          <NotificationCenter :alarm-count="alarmCount" />
          <n-dropdown :options="userOptions" @select="handleUserSelect">
            <n-button quaternary>
              <template #icon><n-icon :component="UserAvatar" /></template>
              {{ auth.username }}
              <n-tag :bordered="false" :type="roleType" size="small" style="margin-left: 8px">{{ roleLabel }}</n-tag>
            </n-button>
          </n-dropdown>
        </n-space>
      </n-layout-header>
      <n-layout-content content-style="padding: 20px" class="main-content" id="main-content">
        <ErrorBoundary>
          <!-- [AUDIT-FIX] 严重级-列表页未缓存导致筛选/分页状态丢失，每次返回都重新加载
               include 仅缓存关键列表页（与 route name 一致），避免详情页等动态路由被错误缓存 -->
          <router-view v-slot="{ Component, route }">
            <keep-alive :include="cachedRouteNames" :max="10">
              <component :is="Component" :key="route.fullPath" />
            </keep-alive>
          </router-view>
        </ErrorBoundary>
      </n-layout-content>
    </n-layout>
    <CommandPalette
      v-model:show="showCommandPalette"
      :menu-items="commandMenuItems"
      :devices="commandDevices"
      :rules="commandRules"
    />
    <n-modal v-model:show="showChangePwd" :mask-closable="!auth.mustChangePassword" :close-on-esc="!auth.mustChangePassword" :title="t('login.changePassword')" preset="card" style="width: 440px; max-width: 90vw" :auto-focus="true" :close-on-esc-aria-label="t('common.closeDialog')">
      <n-alert v-if="auth.mustChangePassword" type="info" style="margin-bottom: 16px">
        {{ t('login.changePasswordSecurityHint') }}  <!-- FIXED: 原问题-中文硬编码，改用i18n -->
      </n-alert>
      <n-form :model="pwdForm" :rules="pwdRules" ref="pwdFormRef" label-placement="left" label-width="90">
        <n-form-item :label="t('login.oldPassword')" path="oldPassword"><n-input v-model:value="pwdForm.oldPassword" type="password" show-password-on="click" autocomplete="current-password" :maxlength="72" :placeholder="t('login.oldPasswordPlaceholder')" /></n-form-item>  <!-- FIXED: 原问题-中文硬编码，改用i18n -->
        <n-form-item :label="t('login.newPassword')" path="newPassword"><n-input v-model:value="pwdForm.newPassword" type="password" show-password-on="click" autocomplete="new-password" :maxlength="72" :placeholder="t('login.newPasswordPlaceholder')" /></n-form-item>  <!-- FIXED: 原问题-中文硬编码，改用i18n -->
        <n-form-item :label="t('login.confirmPassword')" path="confirmPassword"><n-input v-model:value="pwdForm.confirmPassword" type="password" show-password-on="click" autocomplete="new-password" :maxlength="72" :placeholder="t('login.confirmPasswordPlaceholder')" /></n-form-item>  <!-- FIXED: 原问题-中文硬编码，改用i18n -->
      </n-form>
      <template #action>
        <n-space justify="end">
          <n-button v-if="!auth.mustChangePassword" @click="showChangePwd = false">{{ t('common.cancel') }}</n-button>  <!-- FIXED: 原问题-中文硬编码，改用i18n -->
          <n-button type="primary" :loading="changingPwd" @click="handleChangePassword">{{ t('login.saveChanges') }}</n-button>  <!-- FIXED: 原问题-中文硬编码，改用i18n -->
        </n-space>
      </template>
    </n-modal>
    <n-modal v-model:show="showShortcutHelp" :mask-closable="true" :close-on-esc="true" :auto-focus="true" :title="t('common.keyboardShortcuts')" preset="card" style="width: 480px; max-width: 90vw" :close-on-esc-aria-label="t('common.closeDialog')">
      <n-data-table
        :columns="shortcutColumns"
        :data="shortcutList"
        :bordered="false"
        :single-line="false"
        size="small"
      />
    </n-modal>
  </n-layout>
</template>

<script setup lang="ts">
import { ref, computed, h, inject, onMounted, onUnmounted, watch, type Ref } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { NIcon } from 'naive-ui'
import {
  Speedometer, HardwareChipOutline, SettingsOutline, AlertCircleOutline,
  DesktopOutline, PeopleOutline, ChevronForwardOutline, NotificationsOutline, PersonOutline as UserAvatar,
  LogOutOutline, StatsChartOutline, ServerOutline, CubeSharp, BuildOutline, FileTrayOutline,
  PulseSharp, CloudOutline, CloudOfflineSharp, CalculatorSharp, MoonOutline, SunnyOutline,
  RocketOutline, BarChartOutline, RadioOutline, PowerOutline, SwapHorizontalOutline,
  ExtensionPuzzleOutline, LanguageOutline, ShieldOutline, ServerOutline as DatabaseOutline, WifiSharp,
  NavigateOutline, SparklesOutline, EyeOffOutline, TerminalOutline, LinkOutline,
  NotificationsOffOutline, BugOutline, OptionsOutline, GridOutline, LayersOutline, LockClosedOutline, KeyOutline,
  PulseOutline, ShieldCheckmarkOutline, SpeedometerOutline, DocumentTextOutline, GitBranchOutline,
  EyeOutline, TrendingUpOutline, GitNetworkOutline, AnalyticsOutline,
  LeafOutline, CloudDownloadOutline, SettingsOutline as GearOutline,
  SearchOutline, MenuOutline, CloseOutline,
} from '@vicons/ionicons5'
import { useAuthStore, _setItem } from '@/stores/auth'
import NotificationCenter from '@/components/NotificationCenter.vue'
import CommandPalette from '@/components/CommandPalette.vue'
import ErrorBoundary from '@/components/ErrorBoundary.vue'
import { alarmApi, authApi, deviceApi, ruleApi } from '@/api'
import { t, setLocale, getLocale, useCurrentLocale } from '@/i18n'
import { extractError } from '@/utils/errorCodes'
import * as ws from '@/api/websocket'
import { message, dialog } from '@/utils/discreteApi'
import { useBreakpoints } from '@/composables/useBreakpoints'
import { usePageVisibility } from '@/composables/usePageVisibility'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()

const collapsed = ref(false)
const alarmCount = ref(0)
const isOnline = ref(navigator.onLine)
let alarmTimer: number | null = null

const { isMobile } = useBreakpoints()
const sidebarVisible = ref(false)
const showCommandPalette = ref(false)
const showShortcutHelp = ref(false)
const commandDevices = ref<Array<{device_id: string; name: string}>>([])
const commandRules = ref<Array<{rule_id: string; name: string}>>([])

// 键盘快捷键帮助面板触发：? 键（Shift+/）
function onGlobalKeydown(e: KeyboardEvent) {
  // 忽略输入框/文本域中的按键，避免影响表单输入
  const target = e.target as HTMLElement
  if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable)) return
  // ? 键（Shift+/）触发快捷键帮助
  if (e.key === '?' || (e.shiftKey && e.key === '/')) {
    e.preventDefault()
    showShortcutHelp.value = true
  }
}

// 快捷键列表（用于帮助面板展示）
const shortcutList = computed(() => {
  void localeRef.value
  return [
    { key: '?', action: t('common.keyboardShortcuts') },
    { key: 'Ctrl/Cmd + K', action: t('common.shortcutGlobalSearch') },
    { key: 'T', action: t('common.shortcutToggleTheme') },
    { key: 'L', action: t('common.shortcutToggleLanguage') },
    { key: 'Esc', action: t('common.shortcutCloseDialog') },
    { key: 'Enter', action: t('common.shortcutConfirm') },
    { key: 'Tab', action: t('common.shortcutTabNext') },
    { key: 'Shift + Tab', action: t('common.shortcutTabPrev') },
  ]
})

// 快捷键表格列定义
const shortcutColumns = computed(() => {
  void localeRef.value
  return [
    { title: t('common.shortcutKey'), key: 'key', width: 160, render: (row: any) => h('kbd', { style: 'display:inline-block;padding:2px 8px;background:var(--n-color-hover,#f5f5f5);border:1px solid var(--n-border-color,#ddd);border-radius:4px;font-family:monospace;font-size:12px' }, row.key) },
    { title: t('common.shortcutAction'), key: 'action' },
  ]
})

const commandMenuItems = computed(() => {
  void localeRef.value
  const items = [
    { key: 'Dashboard', label: t('nav.dashboard'), path: '/' },
    { key: 'Devices', label: t('nav.devices'), path: '/devices' },
    { key: 'Rules', label: t('nav.rules'), path: '/rules' },
    { key: 'Alarms', label: t('nav.alarms'), path: '/alarms' },
    { key: 'DataQuery', label: t('nav.dataQuery'), path: '/data', adminOnly: true },
    { key: 'System', label: t('nav.system'), path: '/system', adminOnly: true },
    { key: 'Users', label: t('nav.users'), path: '/users', adminOnly: true },
    { key: 'DriverConfig', label: t('nav.driverConfig'), path: '/system/drivers', adminOnly: true },
    { key: 'PlatformConfig', label: t('nav.platformConfig'), path: '/system/platforms', adminOnly: true },
    { key: 'AiModel', label: t('nav.aiModel'), path: '/system/ai-model', adminOnly: true },
    { key: 'MqttServer', label: t('nav.mqttServer'), path: '/system/mqtt-server', adminOnly: true },
    { key: 'ModbusSlave', label: t('nav.modbusSlave'), path: '/system/modbus-slave', adminOnly: true },
    { key: 'DigitalTwin', label: t('nav.digitalTwinMenu'), path: '/digital-twin' },
    { key: 'ScadaEditor', label: t('nav.scadaEditorMenu'), path: '/scada' },
  ]
  // FIXED-M1: CommandPalette 未按角色过滤，非管理员可见 adminOnly 页面
  if (auth.role === 'admin') return items
  return items.filter(item => !(item as any).adminOnly)
})

function checkOnline() {
  isOnline.value = navigator.onLine
  if (isOnline.value) {
    message.success(t('network.reconnected'))
  } else {
    message.warning(t('network.offline'))
  }
}

// Listen for online/offline events
function onOnline() { isOnline.value = true; message.success(t('network.reconnected')) }
function onOffline() { isOnline.value = false; message.warning(t('network.offline')) }

const showChangePwd = ref(false)
const changingPwd = ref(false)
const pwdFormRef = ref<any>(null)
const pwdForm = ref({ oldPassword: '', newPassword: '', confirmPassword: '' })
const pwdRules = computed(() => ({
  oldPassword: { required: true, message: t('login.oldPasswordRequired'), trigger: 'blur' },  // FIXED: 原问题-中文硬编码，改用i18n
  newPassword: {
    required: true,
    trigger: 'blur',
    validator: (_rule: any, value: string) => {
      if (!value) return new Error(t('login.newPasswordRequired'))  // FIXED: 原问题-中文硬编码，改用i18n
      if (value.length < 8) return new Error(t('login.passwordPolicy'))
      if (value.length > 72) return new Error(t('login.passwordMaxLength'))
      if (!/[a-zA-Z]/.test(value) || !/[0-9]/.test(value)) return new Error(t('login.passwordLetterAndDigit'))
      if (!/[!@#$%^&*()_+\-=\[\]{}|;':",.\/<>?`~]/.test(value)) return new Error(t('login.passwordNeedSpecial'))
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
}))

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
    _setItem('edgelite_mustChangePassword', 'false')
    showChangePwd.value = false
    // FIXED: 原问题-修改密码成功后未重新登录获取新token，若后端使旧token失效则后续请求401
    const newPassword = pwdForm.value.newPassword
    pwdForm.value = { oldPassword: '', newPassword: '', confirmPassword: '' }
    try {
      await auth.login(auth.username, newPassword)
    } catch (e: any) {
      // FIXED: 原问题-重新登录失败时静默登出无提示，用户不知道为何被踢出
      message.error(extractError(e, t('login.passwordChangeFailed')))
      await auth.logout()
      router.push('/login')
    }
  } catch (e: any) {
    message.error(extractError(e, t('login.passwordChangeFailed')))
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
  // FIXED-P2: 原window.location.reload()丢失所有Pinia store状态、WS连接和表单输入
  // 改为无需刷新的响应式切换，t()函数依赖currentLocale ref会自动更新
}

const currentRoute = computed(() => {
  const name = route.name as string
  if (name === 'DeviceDetail') return 'Devices'
  if (name === 'DeviceTemplates') return 'Devices'
  // [AUDIT-FIX] 严重级-子页面高亮父菜单：动态路由 / 平台子页面需映射到父菜单 key
  if (name === 'CustomMqttConfig') return 'PlatformConfig'
  if (name === 'PlatformDashboard') return 'PlatformConfig'
  if (name === 'TbMonitor') return 'PlatformConfig'
  // 可观测性子页面直接返回子菜单key，让n-menu同时高亮子项和展开父级
  return name
})
// [AUDIT-FIX] 严重级-keep-alive include 列表：仅缓存关键列表页，保留筛选/分页状态
// 名称需与组件 name 选项一致（vite-plugin-vue 默认按文件名推断，与 route name 对齐）
const cachedRouteNames = [
  'DeviceList', 'RuleList', 'AlarmList', 'DataQuery', 'DataQuality', 'AuditLog', 'UserManage',
]
// FIX 7: 提取共享的 titleKeys 映射，供 currentTitle 和 breadcrumbItems 共同使用
const titleKeys: Record<string, string> = {
  Dashboard: 'nav.dashboard', Devices: 'nav.devices', DeviceDetail: 'nav.deviceDetail', DeviceTemplates: 'nav.deviceTemplates',
  Rules: 'nav.rules', Alarms: 'nav.alarms', DataQuery: 'nav.dataQuery',
  DigitalTwin: 'nav.digitalTwin', ScadaEditor: 'nav.scadaEditor',
  DriverConfig: 'nav.driverConfig', PlatformConfig: 'nav.platformConfig', ExpressionConfig: 'nav.expressionConfig',
  PreprocessConfig: 'nav.preprocessConfig',
  System: 'nav.system', Users: 'nav.users',
  AuditLog: 'nav.auditLog',
  AppUpdate: 'nav.appUpdate', GrafanaDashboard: 'nav.grafanaDashboard', McpServer: 'nav.mcpServer',
  MqttServer: 'nav.mqttServer', ModbusSlave: 'nav.modbusSlave', SerialBridge: 'nav.serialBridge',
  ServiceOverview: 'nav.serviceOverview', AiModel: 'nav.aiModel', Integration: 'nav.platformIntegration',
  NotifyConfig: 'nav.notifyChannels',
  ProtocolDebug: 'nav.protocolDebug',
  DeviceLinkage: 'nav.linkage',
  ProfilerView: 'nav.profiler',
  LogAggregator: 'nav.logAggregator',
  FirmwareSignature: 'nav.firmwareSignature',
  ConfigVersion: 'nav.configVersion',
  SelfTest: 'nav.selfTest',
  ResourceSharing: 'nav.resourceSharing',
  DbMonitor: 'nav.dbMonitor',
  BackupSchedule: 'nav.backupSchedule',
  SystemConfig: 'nav.systemConfig',
  AlarmTrend: 'nav.alarmTrend',
  AlarmCorrelation: 'nav.alarmCorrelation',
  DataDownsample: 'nav.dataDownsample',
  ModbusOps: 'nav.modbusOps',
  ObservabilityOverview: 'observability.overview',
  ObservabilityRulesPage: 'nav.observabilityRules',
  ObservabilityEventsPage: 'nav.observabilityEvents',
  ObservabilityTraces: 'observability.traces',
  ObservabilityMetrics: 'observability.metricsMonitor',
  // [AUDIT-FIX] 严重级-孤儿路由面包屑标题映射
  DeviceShadow: 'nav.deviceShadow',
  Report: 'nav.report',
  BridgeConfig: 'nav.bridgeConfig',
  PipelineEditor: 'nav.pipelineConfig',
  AiMonitor: 'nav.aiMonitor',
  AiAbTest: 'nav.aiAbTest',
  AiCenter: 'nav.aiCenter',
  AiTest: 'nav.aiTest',
  Metrics: 'nav.metricsView',
  DataExport: 'nav.dataExport',
  DataImport: 'nav.dataImport',
  ScriptEngine: 'nav.scripts',
  Simulation: 'nav.simulation',
  AnomalyLearner: 'nav.anomalyLearner',
  TrendLearner: 'nav.trendLearner',
  ThresholdLearner: 'nav.thresholdLearner',
  CalibrationData: 'nav.calibration',
  PhysicsCalibrator: 'nav.physCalib',
  PhysicsParamDb: 'nav.paramDb',
  PrecisionTest: 'nav.precTest',
  EvolutionVerify: 'nav.evoVerify',
  AiBoundaryTest: 'nav.bndTest',
  AiStressTest: 'nav.stressTest',
  AiReportCenter: 'nav.aiRpt',
  PlatformDashboard: 'nav.platformDashboard',
  TbMonitor: 'nav.tbMonitor',
  DataQuality: 'nav.qualityMonitor',
}

const currentTitle = computed(() => {
  const key = titleKeys[route.name as string]
  return key ? t(key) : 'EdgeLiteGateway'
})

const breadcrumbItems = computed(() => {
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

const localeRef = useCurrentLocale()

const allMenuOptions = computed(() => {
  // 读取 localeRef 触发响应式依赖，语言切换时重新求值
  void localeRef.value
  return [
  { label: t('nav.dashboard'), key: 'Dashboard', icon: renderIcon(Speedometer) },
  { label: t('nav.largeScreen'), key: 'LargeScreen', icon: renderIcon(DesktopOutline) },
  { label: t('nav.devices'), key: 'Devices', icon: renderIcon(HardwareChipOutline) },
  { label: t('nav.rules'), key: 'Rules', icon: renderIcon(BuildOutline) },
  // 非管理员：简洁的告警入口
  { label: t('nav.alarms'), key: 'Alarms', icon: renderIcon(AlertCircleOutline), hideForAdmin: true },
  // 管理员：告警中心分组
  {
    label: t('nav.alarmGroup'), key: 'alarm-group', icon: renderIcon(AlertCircleOutline), roles: ['admin', 'operator'],
    children: [
      { label: t('nav.alarmList'), key: 'Alarms', icon: renderIcon(AlertCircleOutline) },
      {
        label: () => h('span', { style: 'display:inline-flex;align-items:center;gap:6px' }, [
          h('span', { style: 'display:inline-flex;align-items:center;justify-content:center;width:20px;height:20px;border-radius:4px;background:linear-gradient(135deg,#f59e0b,#ef4444);color:#fff;font-size:9px;font-weight:700' }, 'TR'),
          t('nav.alarmTrend'),
        ]),
        key: 'AlarmTrend', icon: renderIcon(TrendingUpOutline),
      },
      {
        label: () => h('span', { style: 'display:inline-flex;align-items:center;gap:6px' }, [
          h('span', { style: 'display:inline-flex;align-items:center;justify-content:center;width:20px;height:20px;border-radius:4px;background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;font-size:9px;font-weight:700' }, 'CR'),
          t('nav.alarmCorrelation'),
        ]),
        key: 'AlarmCorrelation', icon: renderIcon(LinkOutline),
      },
      { label: t('nav.observabilityRules'), key: 'ObservabilityRulesPage', icon: renderIcon(ShieldCheckmarkOutline) },
      { label: t('nav.observabilityEvents'), key: 'ObservabilityEventsPage', icon: renderIcon(AlertCircleOutline) },
    ],
  },
  // 可观测性（精简：移除告警相关项）
  {
    label: () => h('span', { style: 'display:inline-flex;align-items:center;gap:6px' }, [
      h('span', { style: 'display:inline-flex;align-items:center;justify-content:center;width:20px;height:20px;border-radius:4px;background:linear-gradient(135deg,#06b6d4,#8b5cf6);color:#fff;font-size:9px;font-weight:700' }, 'OB'),
      t('observability.title'),
    ]),
    key: 'observability-group',
    icon: renderIcon(AnalyticsOutline),
    adminOnly: true,
    children: [
      { label: t('observability.overview'), key: 'ObservabilityOverview', icon: renderIcon(EyeOutline) },
      { label: t('observability.traces'), key: 'ObservabilityTraces', icon: renderIcon(GitNetworkOutline) },
      { label: t('observability.metricsMonitor'), key: 'ObservabilityMetrics', icon: renderIcon(TrendingUpOutline) },
    ],
  },
  // 数据管理
  {
    label: t('nav.dataGroup'), key: 'data-group', icon: renderIcon(StatsChartOutline), adminOnly: true,
    children: [
      { label: t('nav.dataQuery'), key: 'DataQuery', icon: renderIcon(StatsChartOutline) },
      {
        label: () => h('span', { style: 'display:inline-flex;align-items:center;gap:6px' }, [
          h('span', { style: 'display:inline-flex;align-items:center;justify-content:center;width:20px;height:20px;border-radius:4px;background:linear-gradient(135deg,#10b981,#059669);color:#fff;font-size:9px;font-weight:700' }, 'DS'),
          t('nav.dataDownsample'),
        ]),
        key: 'DataDownsample', icon: renderIcon(LeafOutline),
      },
      // [AUDIT-FIX] 严重级-孤儿路由归入数据管理分组
      { label: t('nav.qualityMonitor'), key: 'DataQuality', icon: renderIcon(AnalyticsOutline) },
      { label: t('nav.report'), key: 'Report', icon: renderIcon(DocumentTextOutline) },
      { label: t('nav.dataExport'), key: 'DataExport', icon: renderIcon(CloudDownloadOutline) },
      { label: t('nav.dataImport'), key: 'DataImport', icon: renderIcon(CloudDownloadOutline) },
    ],
  },
  // AI 智能中心
  {
    label: () => h('span', { style: 'display:inline-flex;align-items:center;gap:6px' }, [
      h('span', { style: 'display:inline-flex;align-items:center;justify-content:center;width:20px;height:20px;border-radius:4px;background:linear-gradient(135deg,#8b5cf6,#667eea);color:#fff;font-size:9px;font-weight:700' }, 'AI'),
      t('nav.aiGroup'),
    ]),
    key: 'ai-group',
    icon: renderIcon(SparklesOutline),
    adminOnly: true,
    children: [
      { label: t('nav.aiModel'), key: 'AiModel', icon: renderIcon(HardwareChipOutline) },
      { label: t('nav.mcpServer'), key: 'McpServer', icon: renderIcon(ExtensionPuzzleOutline) },
      // [AUDIT-FIX] 严重级-孤儿路由归入 AI 中心
      { label: t('nav.aiMonitor'), key: 'AiMonitor', icon: renderIcon(EyeOutline) },
      { label: t('nav.aiCenter'), key: 'AiCenter', icon: renderIcon(SparklesOutline) },
      { label: t('nav.aiTest'), key: 'AiTest', icon: renderIcon(BugOutline) },
      { label: t('nav.aiAbTest'), key: 'AiAbTest', icon: renderIcon(OptionsOutline) },
    ],
  },
  // [AUDIT-FIX] 严重级-AI 实验室（自学习/标定/测试类孤儿路由）
  {
    label: t('nav.aiLabGroup'), key: 'ai-lab-group', icon: renderIcon(SparklesOutline), adminOnly: true,
    children: [
      { label: t('nav.simulation'), key: 'Simulation', icon: renderIcon(PulseOutline) },
      { label: t('nav.anomalyLearner'), key: 'AnomalyLearner', icon: renderIcon(AnalyticsOutline) },
      { label: t('nav.trendLearner'), key: 'TrendLearner', icon: renderIcon(TrendingUpOutline) },
      { label: t('nav.thresholdLearner'), key: 'ThresholdLearner', icon: renderIcon(BarChartOutline) },
      { label: t('nav.calibration'), key: 'CalibrationData', icon: renderIcon(CalculatorSharp) },
      { label: t('nav.physCalib'), key: 'PhysicsCalibrator', icon: renderIcon(CalculatorSharp) },
      { label: t('nav.paramDb'), key: 'PhysicsParamDb', icon: renderIcon(DatabaseOutline) },
      { label: t('nav.precTest'), key: 'PrecisionTest', icon: renderIcon(SpeedometerOutline) },
      { label: t('nav.evoVerify'), key: 'EvolutionVerify', icon: renderIcon(ShieldCheckmarkOutline) },
      { label: t('nav.bndTest'), key: 'AiBoundaryTest', icon: renderIcon(ShieldCheckmarkOutline) },
      { label: t('nav.stressTest'), key: 'AiStressTest', icon: renderIcon(SpeedometerOutline) },
      { label: t('nav.aiRpt'), key: 'AiReportCenter', icon: renderIcon(DocumentTextOutline) },
    ],
  },
  // 可视化
  {
    label: t('nav.visualGroup'), key: 'visual-group', icon: renderIcon(LayersOutline), roles: ['admin', 'operator'],
    children: [
      { label: t('nav.digitalTwinMenu'), key: 'DigitalTwin', icon: renderIcon(CubeSharp) },
      { label: t('nav.scadaEditorMenu'), key: 'ScadaEditor', icon: renderIcon(GridOutline) },
    ],
  },
  // 通信与联动（从服务管理拆分）
  {
    label: t('nav.commGroup'), key: 'comm-group', icon: renderIcon(WifiSharp), roles: ['admin', 'operator'],
    children: [
      { label: t('nav.mqttServer'), key: 'MqttServer', icon: renderIcon(WifiSharp) },
      { label: t('nav.modbusSlave'), key: 'ModbusSlave', icon: renderIcon(HardwareChipOutline) },
      { label: t('nav.modbusOps'), key: 'ModbusOps', icon: renderIcon(PulseSharp) },
      { label: t('nav.serialBridge'), key: 'SerialBridge', icon: renderIcon(LinkOutline) },
      { label: t('nav.protocolDebug'), key: 'ProtocolDebug', icon: renderIcon(PulseOutline) },
      { label: t('nav.platformIntegration'), key: 'Integration', icon: renderIcon(CloudOutline) },
      { label: t('nav.linkage'), key: 'DeviceLinkage', icon: renderIcon(GitBranchOutline) },
      // [AUDIT-FIX] 严重级-孤儿路由归入通信与联动分组
      { label: t('nav.deviceShadow'), key: 'DeviceShadow', icon: renderIcon(HardwareChipOutline) },
      { label: t('nav.bridgeConfig'), key: 'BridgeConfig', icon: renderIcon(LinkOutline) },
    ],
  },
  // 运维监控（从服务管理拆分）
  {
    label: t('nav.opsGroup'), key: 'ops-group', icon: renderIcon(SpeedometerOutline), adminOnly: true,
    children: [
      { label: t('nav.serviceOverview'), key: 'ServiceOverview', icon: renderIcon(TerminalOutline) },
      { label: t('nav.grafanaDashboard'), key: 'GrafanaDashboard', icon: renderIcon(BarChartOutline) },
      { label: t('nav.profiler'), key: 'ProfilerView', icon: renderIcon(SpeedometerOutline) },
      { label: t('nav.logAggregator'), key: 'LogAggregator', icon: renderIcon(DocumentTextOutline) },
      { label: t('nav.dbMonitor'), key: 'DbMonitor', icon: renderIcon(DatabaseOutline) },
      // [AUDIT-FIX] 严重级-孤儿路由归入运维监控分组
      { label: t('nav.metricsView'), key: 'Metrics', icon: renderIcon(AnalyticsOutline) },
    ],
  },
  // 系统配置（精简：仅保留配置类）
  {
    label: t('nav.systemGroup'), key: 'system-group', icon: renderIcon(ShieldOutline), adminOnly: true,
    children: [
      { label: t('nav.system'), key: 'System', icon: renderIcon(ServerOutline) },
      { label: t('nav.systemConfig'), key: 'SystemConfig', icon: renderIcon(GearOutline) },
      { label: t('nav.driverConfig'), key: 'DriverConfig', icon: renderIcon(GridOutline) },
      { label: t('nav.platformConfig'), key: 'PlatformConfig', icon: renderIcon(CloudOutline) },
      { label: t('nav.platformDashboard'), key: 'PlatformDashboard', icon: renderIcon(BarChartOutline) },
      { label: t('nav.tbMonitor'), key: 'TbMonitor', icon: renderIcon(EyeOutline) },
      { label: t('nav.expressionConfig'), key: 'ExpressionConfig', icon: renderIcon(CalculatorSharp) },
      { label: t('nav.preprocessConfig'), key: 'PreprocessConfig', icon: renderIcon(NavigateOutline) },
      { label: t('nav.notifyChannels'), key: 'NotifyConfig', icon: renderIcon(NotificationsOutline) },
      { label: t('nav.configVersion'), key: 'ConfigVersion', icon: renderIcon(DocumentTextOutline) },
      // [AUDIT-FIX] 严重级-孤儿路由归入系统配置分组
      { label: t('nav.pipelineConfig'), key: 'PipelineEditor', icon: renderIcon(GitBranchOutline) },
      { label: t('nav.scripts'), key: 'ScriptEngine', icon: renderIcon(TerminalOutline) },
      { label: t('nav.deviceTemplates'), key: 'DeviceTemplates', icon: renderIcon(HardwareChipOutline) },
    ],
  },
  // 安全与维护（从系统配置拆分）
  {
    label: t('nav.securityGroup'), key: 'security-group', icon: renderIcon(LockClosedOutline), adminOnly: true,
    children: [
      { label: t('nav.users'), key: 'Users', icon: renderIcon(PeopleOutline) },
      { label: t('nav.auditLog'), key: 'AuditLog', icon: renderIcon(FileTrayOutline) },
      { label: t('nav.selfTest'), key: 'SelfTest', icon: renderIcon(ShieldCheckmarkOutline) },
      { label: t('nav.firmwareSignature'), key: 'FirmwareSignature', icon: renderIcon(ShieldCheckmarkOutline) },
      { label: t('nav.backupSchedule'), key: 'BackupSchedule', icon: renderIcon(CloudDownloadOutline) },
      { label: t('nav.appUpdate'), key: 'AppUpdate', icon: renderIcon(RocketOutline) },
      { label: t('nav.resourceSharing'), key: 'ResourceSharing', icon: renderIcon(LinkOutline) },
    ],
  },
] as const
})

// FIXED-P2: 隐藏未实现的路由对应菜单项（PlaceholderView 路由标记了 meta.hidden: true）
// 同时递归过滤子菜单，如果分组下所有子项都被隐藏则隐藏整个分组
const _HIDDEN_ROUTES = new Set([
  'Setup', 'LargeScreen', 'DeviceTemplates', 'DeviceShadow', 'Report',
  'DataQuality', 'DataQualityMonitor', 'PlatformDashboard', 'TbMonitor', 'CustomMqttConfig',
  'BridgeConfig', 'PipelineEditor', 'AiMonitor', 'AiAbTest', 'DeviceLinkage',
  'ProfilerView', 'LogAggregator', 'FirmwareSignature', 'ProtocolDebug', 'Metrics',
  'ConfigVersion', 'SelfTest', 'DataExport', 'DataImport', 'ResourceSharing',
  'DataDownsample', 'DbMonitor', 'AlarmTrend', 'AlarmCorrelation', 'BackupSchedule',
  'SystemConfig', 'ObservabilityOverview', 'ObservabilityRulesPage', 'ObservabilityEventsPage',
  'ObservabilityTraces', 'ObservabilityMetrics', 'ScriptEngine', 'Simulation',
  'AnomalyLearner', 'TrendLearner', 'ThresholdLearner', 'AiCenter', 'AiTest',
  'CalibrationData', 'PhysicsCalibrator', 'PhysicsParamDb', 'PrecisionTest',
  'EvolutionVerify', 'AiBoundaryTest', 'AiStressTest', 'AiReportCenter', 'ModbusOps',
])

function _filterMenuItems(items: readonly any[]): any[] {
  return items
    .filter(item => {
      // 隐藏标记的路由
      if (_HIDDEN_ROUTES.has(item.key)) return false
      // 角色过滤
      if (auth.role !== 'admin') {
        if (item.adminOnly) return false
        const roles = item.roles
        if (roles && !roles.includes(auth.role)) return false
      } else {
        if (item.hideForAdmin) return false
      }
      return true
    })
    .map(item => {
      // 递归过滤子菜单
      if (item.children) {
        const filteredChildren = _filterMenuItems(item.children)
        return { ...item, children: filteredChildren }
      }
      return item
    })
    .filter(item => {
      // 如果分组下所有子项都被隐藏，则隐藏整个分组
      if (item.children && item.children.length === 0) return false
      return true
    })
}

const menuOptions = computed(() => {
  void localeRef.value
  const opts = allMenuOptions.value
  return _filterMenuItems(opts)
})

const userOptions = computed(() => {
  void localeRef.value
  return [
  { label: t('nav.changePassword'), key: 'changePassword', icon: renderIcon(KeyOutline) },
  { label: t('nav.logout'), key: 'logout', icon: renderIcon(LogOutOutline) },
] as const
})

function handleMenuClick(key: string) {
  router.push({ name: key })
  if (isMobile.value) sidebarVisible.value = false
}

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

// FIXED-General: WS 连接成功时暂停 30 秒轮询，断开时恢复轮询，避免冗余请求
function onAlarmWsStatus(status: 'connected' | 'disconnected' | 'error' | 'reconnecting') {
  if (status === 'connected') {
    if (alarmTimer) { clearInterval(alarmTimer); alarmTimer = null }
    fetchAlarmCount()  // 连接成功立即拉取一次
  } else if (status === 'disconnected' || status === 'error') {
    if (!alarmTimer) {
      alarmTimer = window.setInterval(fetchAlarmCount, 30000)
    }
  }
}

// FIX-PERF12: 页面隐藏时暂停轮询，恢复时立即刷新并在原已轮询的情况下重启轮询
const { isVisible } = usePageVisibility()
let alarmPollingActive = false
watch(isVisible, (visible) => {
  if (visible) {
    fetchAlarmCount()
    if (alarmPollingActive && !alarmTimer) {
      alarmTimer = window.setInterval(fetchAlarmCount, 30000)
    }
  } else {
    alarmPollingActive = !!alarmTimer
    if (alarmTimer) { clearInterval(alarmTimer); alarmTimer = null }
  }
})

onMounted(() => {
  fetchAlarmCount(); alarmTimer = window.setInterval(fetchAlarmCount, 30000)
  ws.connect('alarm', onAlarmWsMessage)
  ws.onStatus('alarm', onAlarmWsStatus)
  window.addEventListener('online', onOnline); window.addEventListener('offline', onOffline)
  window.addEventListener('keydown', onGlobalKeydown)
  deviceApi.list({ page: 1, size: 200 }).then(data => {
    commandDevices.value = (data?.data ?? []).map((d: any) => ({ device_id: d.device_id, name: d.name }))
  }).catch(() => {})
  ruleApi.list({ page: 1, size: 200 }).then(data => {
    commandRules.value = (data?.data ?? []).map((r: any) => ({ rule_id: r.rule_id, name: r.name }))
  }).catch(() => {})
})
onUnmounted(() => { if (alarmTimer) clearInterval(alarmTimer); if (_wsAlarmTimer) clearTimeout(_wsAlarmTimer); ws.offStatus('alarm', onAlarmWsStatus); ws.disconnect('alarm', onAlarmWsMessage); window.removeEventListener('online', onOnline); window.removeEventListener('offline', onOffline); window.removeEventListener('keydown', onGlobalKeydown) })
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
.n-breadcrumb-item { cursor: pointer; }
.n-breadcrumb-item:last-child { cursor: default; }
@media (max-width: 768px) {
  .header {
    padding: 0 12px;
  }
  .header .n-space {
    gap: 8px !important;
  }
}
</style>
