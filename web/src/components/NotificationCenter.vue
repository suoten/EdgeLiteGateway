<template>
  <n-popover trigger="click" placement="bottom-end" :width="400">
    <template #trigger>
      <n-badge :value="unreadCount" :max="99" :show="unreadCount > 0">
        <n-button quaternary circle @click="openNotifications">
          <template #icon><n-icon :component="NotificationsOutline" /></template>
        </n-button>
      </n-badge>
    </template>
    <div class="notification-panel">
      <div class="notification-header">
        <n-space justify="space-between" align="center">
          <n-text strong>{{ t('notifications.title') }}</n-text>
          <n-space>
            <n-button v-if="unreadCount > 0" text size="small" @click="markAllRead">
              {{ t('notifications.markAllRead') }}
            </n-button>
            <n-button text size="small" @click="openAlarmPage">
              {{ t('notifications.viewAll') }}
            </n-button>
          </n-space>
        </n-space>
      </div>
      <n-divider style="margin: 8px 0" />
      <div class="notification-list" v-if="notifications.length > 0">
        <div
          v-for="notification in notifications"
          :key="notification.id"
          class="notification-item"
          :class="{ unread: !notification.read, [notification.type]: true }"
          @click="handleNotificationClick(notification)"
        >
          <div class="notification-icon">
            <n-icon :component="getNotificationIcon(notification.type)" :size="20" />
          </div>
          <div class="notification-content">
            <div class="notification-title">{{ notification.title }}</div>
            <div class="notification-message">{{ notification.message }}</div>
            <div class="notification-time">{{ formatTime(notification.timestamp) }}</div>
          </div>
          <div v-if="!notification.read" class="notification-dot"></div>
        </div>
      </div>
      <n-empty v-else :description="t('notifications.noNotifications')" />
    </div>
  </n-popover>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { useRouter } from 'vue-router'
import { NIcon, useMessage } from 'naive-ui'
import {
  NotificationsOutline,
  AlertCircleOutline,
  CheckmarkCircleSharp,
  WarningOutline,
  InformationCircleSharp,
} from '@vicons/ionicons5'
import { t } from '@/i18n'
import * as ws from '@/api/websocket'

interface Notification {
  id: string
  type: 'alarm' | 'system' | 'info' | 'success'
  title: string
  message: string
  timestamp: string
  read: boolean
  data?: any
}

const router = useRouter()
const message = useMessage()

const notifications = ref<Notification[]>([])
const maxNotifications = 50

const unreadCount = computed(() => notifications.value.filter(n => !n.read).length)

function getNotificationIcon(type: string) {
  switch (type) {
    case 'alarm': return AlertCircleOutline
    case 'success': return CheckmarkCircleSharp
    case 'warning': return WarningOutline
    default: return InformationCircleSharp
  }
}

function formatTime(timestamp: string): string {
  const date = new Date(timestamp)
  const now = new Date()
  const diff = now.getTime() - date.getTime()

  if (diff < 60000) return t('notifications.justNow')
  if (diff < 3600000) return `${Math.floor(diff / 60000)}${t('notifications.minutesAgo')}`
  if (diff < 86400000) return `${Math.floor(diff / 3600000)}${t('notifications.hoursAgo')}`
  return date.toLocaleDateString()
}

function addNotification(notification: Omit<Notification, 'id' | 'read'>) {
  const newNotification: Notification = {
    ...notification,
    id: `notif_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`,
    read: false,
  }

  notifications.value.unshift(newNotification)

  // Keep only max notifications
  if (notifications.value.length > maxNotifications) {
    notifications.value = notifications.value.slice(0, maxNotifications)
  }
}

function markAllRead() {
  notifications.value.forEach(n => { n.read = true })
}

function handleNotificationClick(notification: Notification) {
  notification.read = true
  if (notification.data?.alarmId) {
    router.push({ name: 'Alarms' })
  }
}

function openNotifications() {
  // Could trigger additional fetch from API
}

function openAlarmPage() {
  router.push({ name: 'Alarms' })
}

function onAlarmWsMessage(data: any) {
  try {
    const notification: Omit<Notification, 'id' | 'read'> = {
      type: 'alarm',
      title: data.action === 'firing' ? t('notifications.alarmFiring') : t('notifications.alarmRecovered'),
      message: `${data.rule_name || data.rule_id}: ${data.device_name || data.device_id}`,
      timestamp: new Date().toISOString(),
      data: { alarmId: data.alarm_id },
    }
    addNotification(notification)
  } catch (e) {
    console.warn('Failed to process alarm notification:', e)
  }
}

onMounted(() => {
  ws.connect('alarm', onAlarmWsMessage)
})

onUnmounted(() => {
  ws.disconnect('alarm', onAlarmWsMessage)
})
</script>

<style scoped>
.notification-panel {
  max-height: 500px;
  overflow: hidden;
}

.notification-header {
  padding: 4px 0;
}

.notification-list {
  max-height: 400px;
  overflow-y: auto;
}

.notification-item {
  display: flex;
  align-items: flex-start;
  padding: 12px 8px;
  border-radius: 8px;
  cursor: pointer;
  transition: background-color 0.2s;
  position: relative;
}

.notification-item:hover {
  background-color: var(--n-color-hover);
}

.notification-item.unread {
  background-color: var(--n-color-embedded);
}

.notification-icon {
  flex-shrink: 0;
  margin-right: 12px;
  padding-top: 2px;
}

.notification-icon.alarm { color: #ee4b4b; }
.notification-icon.warning { color: #f0a020; }
.notification-icon.success { color: #18a058; }
.notification-icon.info { color: #2080f0; }

.notification-content {
  flex: 1;
  min-width: 0;
}

.notification-title {
  font-weight: 600;
  font-size: 14px;
  margin-bottom: 2px;
}

.notification-message {
  font-size: 13px;
  color: var(--n-text-color-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.notification-time {
  font-size: 12px;
  color: var(--n-text-color-3);
  margin-top: 4px;
}

.notification-dot {
  position: absolute;
  top: 14px;
  right: 8px;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background-color: #ee4b4b;
}
</style>
