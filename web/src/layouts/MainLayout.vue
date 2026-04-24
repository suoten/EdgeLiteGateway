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
          <svg viewBox="0 0 24 24" width="28" height="28" fill="currentColor">
            <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93zm6.9-2.54c-.26-.81-1-1.39-1.9-1.39h-1v-3c0-.55-.45-1-1-1H8v-2h2c.55 0 1-.45 1-1V7h2c1.1 0 2-.9 2-2v-.41c2.93 1.19 5 4.07 5 7.41 0 2.08-.8 3.97-2.1 5.39z"/>
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
        <n-text v-if="!collapsed" depth="3" style="font-size: 12px">v1.0.0 Community</n-text>
      </div>
    </n-layout-sider>
    <n-layout>
      <n-layout-header bordered class="header">
        <n-breadcrumb>
          <n-breadcrumb-item>
            <template #separator><n-icon :component="ChevronForwardOutline" /></template>
            {{ currentTitle }}
          </n-breadcrumb-item>
        </n-breadcrumb>
        <n-space align="center" :size="16">
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
  </n-layout>
</template>

<script setup lang="ts">
import { ref, computed, h } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { NIcon, useDialog } from 'naive-ui'
import {
  SpeedometerOutline, HardwareChip, SettingsOutline, AlertCircleOutline,
  DesktopOutline, PeopleOutline, ChevronForwardOutline, NotificationsOutline, PersonOutline as UserAvatar,
  LogOutOutline,
} from '@vicons/ionicons5'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()
const dialog = useDialog()
const collapsed = ref(false)
const alarmCount = ref(0)

const currentRoute = computed(() => route.name as string)
const currentTitle = computed(() => {
  const titles: Record<string, string> = {
    Dashboard: '仪表盘', Devices: '设备管理', DeviceDetail: '设备详情',
    Rules: '规则管理', Alarms: '告警中心', System: '系统管理', Users: '用户管理',
  }
  return titles[route.name as string] || 'EdgeLiteGateway'
})

const roleLabel = computed(() => ({ admin: '管理员', operator: '操作员', viewer: '观察者' }[auth.role] || auth.role))
const roleType = computed(() => ({ admin: 'error', operator: 'warning', viewer: 'info' }[auth.role] || 'default') as any)

const renderIcon = (icon: any) => () => h(NIcon, { component: icon, size: 18 })

const menuOptions = [
  { label: '仪表盘', key: 'Dashboard', icon: renderIcon(SpeedometerOutline) },
  { label: '设备管理', key: 'Devices', icon: renderIcon(HardwareChip) },
  { label: '规则管理', key: 'Rules', icon: renderIcon(SettingsOutline) },
  { label: '告警中心', key: 'Alarms', icon: renderIcon(AlertCircleOutline) },
  { label: '系统管理', key: 'System', icon: renderIcon(DesktopOutline) },
  { label: '用户管理', key: 'Users', icon: renderIcon(PeopleOutline) },
]

const userOptions = [
  { label: '退出登录', key: 'logout', icon: renderIcon(LogOutOutline) },
]

function handleMenuClick(key: string) { router.push({ name: key }) }

function handleUserSelect(key: string) {
  if (key === 'logout') {
    dialog.warning({
      title: '确认退出',
      content: '确定要退出登录吗？',
      positiveText: '退出',
      negativeText: '取消',
      onPositiveClick: () => { auth.logout(); router.push('/login') },
    })
  }
}
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
.logo-icon { color: #667eea; display: flex; align-items: center; }
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
