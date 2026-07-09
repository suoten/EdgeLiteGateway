<template>
  <n-popover trigger="click" placement="bottom-end" :width="400">
    <template #trigger>
      <n-badge :value="badgeCount" :max="99" :show="badgeCount > 0">
        <!-- [AUDIT-FIX] 严重-8: 告警铃铛图标按钮缺少 aria-label，屏幕阅读器无法识别 -->
        <n-button quaternary circle :class="{ 'alarm-bell-ring': hasNewFiringAlarm }" :aria-label="t('notifications.title')">
          <template #icon><n-icon :component="NotificationsOutline" /></template>
        </n-button>
      </n-badge>
    </template>
    <div class="notification-panel">
      <div class="notification-header">
        <n-space justify="space-between" align="center">
          <n-text strong>{{ t('notifications.title') }}</n-text>
          <n-space>
            <n-button v-if="unreadCount > 0" text size="small" @click.stop="markAllRead()">
              {{ t('notifications.markAllRead') }}
            </n-button>
            <n-button text size="small" @click.stop="openAlarmPage">
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
import { NIcon } from 'naive-ui'
import {
  NotificationsOutline,
  AlertCircleOutline,
  CheckmarkCircleSharp,
  WarningOutline,
  InformationCircleSharp,
} from '@vicons/ionicons5'
import { t } from '@/i18n'
import * as ws from '@/api/websocket'
import { message } from '@/utils/discreteApi'

const props = defineProps<{
  alarmCount: number
}>()

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
const notifications = ref<Notification[]>([])
const maxNotifications = 50

const unreadCount = computed(() => notifications.value.filter(n => !n.read).length)
const badgeCount = computed(() => unreadCount.value + props.alarmCount)
// FIXED-P1: 铃铛闪烁动画 - 存在未读报警时触发
const hasNewFiringAlarm = computed(() => notifications.value.some(n => !n.read && n.type === 'alarm'))

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
  // FIXED-BugR10: 去重保护，防止 WS 重连后轮询降级重复推送同一 firing 告警
  // seenAlarmIds.clear()（Bug20 修复）会导致下次轮询重新推送所有 firing 告警，
  // 此处对同一 alarmId 的未读 'alarm' 类型通知去重
  if (notification.type === 'alarm' && notification.data?.alarmId) {
    const exists = notifications.value.some(n =>
      n.type === 'alarm' && n.data?.alarmId === notification.data!.alarmId
    )
    if (exists) return
  }
  // 告警恢复/确认时移除对应的 firing 通知，允许后续重新触发时创建新通知
  if (notification.type === 'success' && notification.data?.alarmId) {
    notifications.value = notifications.value.filter(n =>
      !(n.type === 'alarm' && n.data?.alarmId === notification.data!.alarmId)
    )
  }
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

// FIXED-P2: markAllRead 不需要 MouseEvent 参数，模板中已 @click.stop 阻止冒泡
function markAllRead() {
  notifications.value.forEach(n => { n.read = true })
}

function handleNotificationClick(notification: Notification) {
  notification.read = true
  // FIXED-P2: 点击通知定位到具体报警，携带 alarmId 查询参数
  if (notification.data?.alarmId) {
    router.push({ name: 'Alarms', query: { alarmId: notification.data.alarmId } })
  }
}

function openAlarmPage(e: MouseEvent) {
  e.stopPropagation()
  router.push({ name: 'Alarms' })
}

function onAlarmWsMessage(data: any) {
  try {
    // FIXED-P0: 原问题-严格过滤data.action存在且data.type==='alarm'，轮询降级数据被丢弃
    // 兼容WS推送格式和HTTP轮询降级格式（已由websocket.ts normalizePollData转换）
    if (!data || data.type !== 'alarm') {
      return
    }
    // 兼容单条推送和批量推送（轮询降级时data.data为数组）
    // FIXED-P0: 原问题-单条推送时错误地将整个消息信封 data 包裹进数组，
    // 导致 alarm.rule_name/device_name/alarm_id 全部为 undefined，通知内容丢失
    const alarmList = Array.isArray(data.data) ? data.data : [data.data]
    for (const alarm of alarmList) {
      const action = alarm.action || data.action || 'firing'
      const notification: Omit<Notification, 'id' | 'read'> = {
        type: action === 'firing' || action === 'trigger' ? 'alarm' : 'success',
        title: getAlarmTitle(action),
        message: `${alarm.rule_name || alarm.rule_id || t('notifications.unknownRule')}: ${alarm.device_name || alarm.device_id || t('notifications.unknownDevice')}`,
        timestamp: alarm.triggered_at || alarm.timestamp || new Date().toISOString(),
        data: { alarmId: alarm.alarm_id || alarm.id },
      }
      addNotification(notification)
      // FIXED-P0: 原问题-工业网关系统完全缺失声音报警功能；添加声光提示
      if (action === 'firing' || action === 'trigger') {
        playAlarmSound(alarm.severity)
        // FIXED-P0: 调用浏览器 Notification API 进行系统级桌面通知
        ensurePermission().then(granted => {
          if (!granted) return
          try {
            const n = new Notification(t('notifications.alarmFiring') + ': ' + (alarm.rule_name || ''), {
              body: alarm.message || '',
              tag: alarm.alarm_id || alarm.id,
              requireInteraction: true,
            })
            n.onclick = () => {
              window.focus()
              router.push({ name: 'Alarms' })
              n.close()
            }
          } catch { /* ignore notification errors */ }
        })
      }
    }
  } catch (e) {
    console.warn('Failed to process alarm notification:', e)
  }
}

// FIXED-P0: 浏览器 Notification API 权限管理
let notifPermission: NotificationPermission = 'default'
async function ensurePermission(): Promise<boolean> {
  if (notifPermission === 'granted') return true
  if (notifPermission === 'default') {
    try {
      notifPermission = await Notification.requestPermission()
    } catch {
      notifPermission = 'denied'
    }
  }
  return notifPermission === 'granted'
}

// FIXED-P0: 报警声音提示 - 使用Web Audio API生成报警音，无需音频文件
let audioCtx: AudioContext | null = null
// FIXED-P1: 浏览器自动播放策略限制，需用户首次交互后才能解锁 AudioContext
let audioUnlocked = false
// FIX-LISTENER: 将 unlock 提升到顶层作用域，便于 onUnmounted 中移除监听器，避免组件卸载后泄漏
function unlockAudio() {
  if (audioUnlocked) return
  try {
    audioCtx = new (window.AudioContext || (window as any).webkitAudioContext)()
    audioCtx.resume().then(() => { audioUnlocked = true })
  } catch { /* ignore */ }
  window.removeEventListener('click', unlockAudio)
  window.removeEventListener('keydown', unlockAudio)
}

function playAlarmSound(severity?: string) {
  // FIXED-P1: AudioContext 未解锁时（自动播放策略），降级为标签页标题闪烁提醒
  if (!audioCtx || audioCtx.state !== 'running') {
    flashTabTitle()
    return
  }
  try {
    const now = audioCtx.currentTime
    const oscillator = audioCtx.createOscillator()
    const gainNode = audioCtx.createGain()
    oscillator.connect(gainNode)
    gainNode.connect(audioCtx.destination)
    oscillator.type = 'sine'

    // 报警声音分级：critical 急促高频三连音，warning 低频单音，info 短促提示音
    const sev = (severity || '').toLowerCase()
    if (sev === 'critical' || sev === 'error') {
      // 1000Hz 三连音
      oscillator.frequency.setValueAtTime(1000, now)
      oscillator.frequency.setValueAtTime(1000, now + 0.12)
      oscillator.frequency.setValueAtTime(1000, now + 0.24)
      gainNode.gain.setValueAtTime(0.3, now)
      gainNode.gain.setValueAtTime(0.3, now + 0.10)
      gainNode.gain.exponentialRampToValueAtTime(0.01, now + 0.11)
      gainNode.gain.setValueAtTime(0.3, now + 0.12)
      gainNode.gain.setValueAtTime(0.3, now + 0.22)
      gainNode.gain.exponentialRampToValueAtTime(0.01, now + 0.23)
      gainNode.gain.setValueAtTime(0.3, now + 0.24)
      gainNode.gain.exponentialRampToValueAtTime(0.01, now + 0.36)
      oscillator.start(now)
      oscillator.stop(now + 0.36)
    } else if (sev === 'warning' || sev === 'warn') {
      // 660Hz 低频单音
      oscillator.frequency.setValueAtTime(660, now)
      gainNode.gain.setValueAtTime(0.3, now)
      gainNode.gain.exponentialRampToValueAtTime(0.01, now + 0.4)
      oscillator.start(now)
      oscillator.stop(now + 0.4)
    } else {
      // 440Hz 短促提示音（info 或默认）
      oscillator.frequency.setValueAtTime(440, now)
      gainNode.gain.setValueAtTime(0.2, now)
      gainNode.gain.exponentialRampToValueAtTime(0.01, now + 0.2)
      oscillator.start(now)
      oscillator.stop(now + 0.2)
    }
  } catch (e) {
    console.warn('Failed to play alarm sound:', e)
  }
}

// FIXED-P1: 自动播放被阻止时的视觉降级方案 - 标签页标题闪烁
// FIXED: 将定时器引用存储为组件级变量，避免多次触发告警时创建并行 interval 互相覆盖标题，
// 并在组件卸载时清理，防止泄漏
let _flashInterval: ReturnType<typeof setInterval> | null = null
let _flashTimeout: ReturnType<typeof setTimeout> | null = null
let _flashOrigTitle: string | null = null
function flashTabTitle() {
  // 清理上一次的闪烁定时器，避免并行 interval 互相覆盖
  if (_flashInterval) { clearInterval(_flashInterval); _flashInterval = null }
  if (_flashTimeout) { clearTimeout(_flashTimeout); _flashTimeout = null }
  // 仅在首次记录原始标题，避免后续被覆盖为带 ⚠ 的标题
  if (_flashOrigTitle === null) _flashOrigTitle = document.title
  const orig = _flashOrigTitle
  let toggle = false
  _flashInterval = setInterval(() => {
    document.title = toggle ? '⚠ ' + orig : orig
    toggle = !toggle
  }, 1000)
  _flashTimeout = setTimeout(() => {
    if (_flashInterval) { clearInterval(_flashInterval); _flashInterval = null }
    document.title = orig
    _flashOrigTitle = null
    _flashTimeout = null
  }, 10000)
}

function getAlarmTitle(action: string): string {
  switch (action) {
    case 'firing': return t('notifications.alarmFiring')
    case 'recovered': return t('notifications.alarmRecovered')
    case 'acknowledged': return t('notifications.alarmAcknowledged') || t('notifications.alarmRecovered')
    default: return t('notifications.alarmFiring')
  }
}

onMounted(() => {
  ws.connect('alarm', onAlarmWsMessage)
  // FIXED-P1: 浏览器自动播放策略限制，需用户首次交互（click/keydown）后才能解锁 AudioContext
  // 在 onMounted 中绑定一次性交互解锁处理器
  window.addEventListener('click', unlockAudio, { once: true })
  window.addEventListener('keydown', unlockAudio, { once: true })
})

onUnmounted(() => {
  ws.disconnect('alarm', onAlarmWsMessage)
  // FIX-LISTENER: 移除未触发的交互解锁监听器，避免组件卸载后泄漏
  window.removeEventListener('click', unlockAudio)
  window.removeEventListener('keydown', unlockAudio)
  // FIXED-P1: 关闭 audioCtx 防止组件卸载后泄漏
  if (audioCtx) {
    audioCtx.close().catch(() => {})
    audioCtx = null
  }
  // FIXED: 清理标签页标题闪烁定时器，恢复原始标题
  if (_flashInterval) { clearInterval(_flashInterval); _flashInterval = null }
  if (_flashTimeout) { clearTimeout(_flashTimeout); _flashTimeout = null }
  if (_flashOrigTitle !== null) { document.title = _flashOrigTitle; _flashOrigTitle = null }
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

/* FIXED-P1: 报警铃铛闪烁动画 - 存在未读报警时触发 */
@keyframes bell-ring {
  0%, 100% { transform: rotate(0); }
  10%, 30%, 50% { transform: rotate(-15deg); }
  20%, 40%, 60% { transform: rotate(15deg); }
  70% { transform: rotate(0); }
}
.alarm-bell-ring {
  animation: bell-ring 1s ease-in-out infinite;
}
@media (prefers-reduced-motion: reduce) {
  .alarm-bell-ring {
    animation: none;
  }
}
</style>
