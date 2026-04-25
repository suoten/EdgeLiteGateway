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
          <svg viewBox="0 0 128 128" width="28" height="28">
            <defs>
              <linearGradient id="lg1" x1="0%" y1="0%" x2="100%" y2="100%">
                <stop offset="0%" style="stop-color:#667eea;stop-opacity:1" />
                <stop offset="100%" style="stop-color:#764ba2;stop-opacity:1" />
              </linearGradient>
            </defs>
            <polygon points="64,36 90,50 90,78 64,92 38,78 38,50" fill="url(#lg1)"/>
            <path d="M 54 64 L 74 64" stroke="white" stroke-width="5" stroke-linecap="round"/>
            <path d="M 69 57 L 76 64 L 69 71" fill="none" stroke="white" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>
            <path d="M 59 57 L 52 64 L 59 71" fill="none" stroke="white" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>
            <circle cx="26" cy="44" r="6" fill="#667eea" opacity="0.7"/>
            <circle cx="26" cy="84" r="6" fill="#667eea" opacity="0.7"/>
            <circle cx="102" cy="44" r="6" fill="#a855f7" opacity="0.7"/>
            <circle cx="102" cy="84" r="6" fill="#a855f7" opacity="0.7"/>
            <line x1="30" y1="46" x2="42" y2="54" stroke="#667eea" stroke-width="2" opacity="0.5"/>
            <line x1="30" y1="82" x2="42" y2="74" stroke="#667eea" stroke-width="2" opacity="0.5"/>
            <line x1="98" y1="46" x2="86" y2="54" stroke="#a855f7" stroke-width="2" opacity="0.5"/>
            <line x1="98" y1="82" x2="86" y2="74" stroke="#a855f7" stroke-width="2" opacity="0.5"/>
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
import { ref, computed, h, onMounted, onUnmounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { NIcon, useDialog } from 'naive-ui'
import {
  SpeedometerOutline, HardwareChip, SettingsOutline, AlertCircleOutline,
  DesktopOutline, PeopleOutline, ChevronForwardOutline, NotificationsOutline, PersonOutline as UserAvatar,
  LogOutOutline, StatsChartOutline, ServerOutline,
} from '@vicons/ionicons5'
import { useAuthStore } from '@/stores/auth'
import { alarmApi } from '@/api'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()
const dialog = useDialog()
const collapsed = ref(false)
const alarmCount = ref(0)
let alarmTimer: number | null = null

const currentRoute = computed(() => route.name as string)
const currentTitle = computed(() => {
  const titles: Record<string, string> = {
    Dashboard: '仪表盘', Devices: '设备管理', DeviceDetail: '设备详情',
    Rules: '规则管理', Alarms: '告警中心', DataQuery: '数据查询',
    System: '系统管理', Users: '用户管理',
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
  { label: '数据查询', key: 'DataQuery', icon: renderIcon(StatsChartOutline) },
  { label: '系统管理', key: 'System', icon: renderIcon(ServerOutline) },
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

async function fetchAlarmCount() {
  try {
    const data = await alarmApi.list({ page: 1, size: 1, status: 'firing' })
    alarmCount.value = data.total
  } catch {}
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
