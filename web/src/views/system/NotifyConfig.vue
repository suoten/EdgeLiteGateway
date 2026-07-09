<template>
  <div class="notify-config">
    <n-card :title="t('notify.title')">
      <n-tabs v-model:value="activeTab" type="line" animated>
        <n-tab-pane name="dingtalk" :tab="t('notify.dingtalk')">
          <n-form ref="dingtalkFormRef" :model="dingtalkForm" :rules="dingtalkRules" label-placement="left" label-width="140">
            <n-form-item :label="t('notify.webhookUrl')" path="webhook_url" required>
              <n-input v-model:value="dingtalkForm.webhook_url" maxlength="255" :placeholder="t('notify.dingtalkWebhookPlaceholder')" />
            </n-form-item>
            <n-form-item :label="t('notify.secret')" path="secret">
              <n-input v-model:value="dingtalkForm.secret" maxlength="128" :placeholder="t('notify.secretPlaceholder')" show-password-on="click" />
            </n-form-item>
            <n-form-item :label="t('notify.atMobiles')" path="at_mobiles">
              <n-dynamic-tags v-model:value="dingtalkForm.at_mobiles" />
            </n-form-item>
            <n-form-item :label="t('notify.atAll')" path="is_at_all">
              <n-switch v-model:value="dingtalkForm.is_at_all" />
            </n-form-item>
            <n-form-item :label="t('notify.maxPerMinute')" path="max_per_minute">
              <n-input-number v-model:value="dingtalkForm.max_per_minute" :min="1" :max="100" style="width: 200px" />
            </n-form-item>
            <n-form-item :label="t('notify.cooldown')" path="cooldown_seconds">
              <n-input-number v-model:value="dingtalkForm.cooldown_seconds" :min="0" :max="3600" style="width: 200px" />
              <span style="margin-left: 8px; color: var(--n-text-color-3)">{{ t('notify.seconds') }}</span>
            </n-form-item>
            <n-form-item>
              <n-space>
                <n-button type="primary" :loading="saveLoading" @click="saveDingTalk">{{ t('common.save') }}</n-button>
                <n-button @click="testChannel('dingtalk')" :loading="testing">{{ t('notify.test') }}</n-button>
              </n-space>
            </n-form-item>
          </n-form>
        </n-tab-pane>

        <n-tab-pane name="wecom" :tab="t('notify.wecom')">
          <n-form ref="wecomFormRef" :model="wecomForm" :rules="wecomRules" label-placement="left" label-width="140">
            <n-form-item :label="t('notify.webhookUrl')" path="webhook_url" required>
              <n-input v-model:value="wecomForm.webhook_url" maxlength="255" :placeholder="t('notify.wecomWebhookPlaceholder')" />
            </n-form-item>
            <n-form-item :label="t('notify.maxPerMinute')" path="max_per_minute">
              <n-input-number v-model:value="wecomForm.max_per_minute" :min="1" :max="100" style="width: 200px" />
            </n-form-item>
            <n-form-item :label="t('notify.cooldown')" path="cooldown_seconds">
              <n-input-number v-model:value="wecomForm.cooldown_seconds" :min="0" :max="3600" style="width: 200px" />
              <span style="margin-left: 8px; color: var(--n-text-color-3)">{{ t('notify.seconds') }}</span>
            </n-form-item>
            <n-form-item>
              <n-space>
                <n-button type="primary" :loading="saveLoading" @click="saveWeCom">{{ t('common.save') }}</n-button>
                <n-button @click="testChannel('wecom')" :loading="testing">{{ t('notify.test') }}</n-button>
              </n-space>
            </n-form-item>
          </n-form>
        </n-tab-pane>

        <n-tab-pane name="email" :tab="t('notify.email')">
          <n-form ref="emailFormRef" :model="emailForm" :rules="emailRules" label-placement="left" label-width="140">
            <n-grid :cols="2" :x-gap="12">
              <n-gi>
                <n-form-item :label="t('notify.smtpHost')" path="smtp_host" required>
                  <n-input v-model:value="emailForm.smtp_host" maxlength="255" :placeholder="t('notify.smtpHostPlaceholder')" />
                </n-form-item>
              </n-gi>
              <n-gi>
                <n-form-item :label="t('notify.smtpPort')" path="smtp_port">
                  <n-input-number v-model:value="emailForm.smtp_port" :min="1" :max="65535" style="width: 100%" />
                </n-form-item>
              </n-gi>
            </n-grid>
            <n-grid :cols="2" :x-gap="12">
              <n-gi>
                <n-form-item :label="t('notify.smtpUser')" path="smtp_user">
                  <n-input v-model:value="emailForm.smtp_user" :maxlength="128" :placeholder="t('notify.smtpUserPlaceholder')" />
                </n-form-item>
              </n-gi>
              <n-gi>
                <n-form-item :label="t('notify.smtpPassword')" path="smtp_password">
                  <n-input v-model:value="emailForm.smtp_password" :maxlength="128" type="password" show-password-on="click" :placeholder="t('notify.smtpPasswordPlaceholder')" />
                </n-form-item>
              </n-gi>
            </n-grid>
            <n-form-item :label="t('notify.fromAddress')" path="from_address" required>
              <n-input v-model:value="emailForm.from_address" maxlength="255" :placeholder="t('notify.fromAddressPlaceholder')" />
            </n-form-item>
            <n-form-item :label="t('notify.toAddresses')" path="to_addresses" required>
              <n-dynamic-tags v-model:value="emailForm.to_addresses" />
            </n-form-item>
            <n-grid :cols="2" :x-gap="12">
              <n-gi>
                <n-form-item :label="t('notify.useTLS')" path="use_tls">
                  <n-switch v-model:value="emailForm.use_tls" />
                </n-form-item>
              </n-gi>
              <n-gi>
                <n-form-item :label="t('notify.useSSL')" path="use_ssl">
                  <n-switch v-model:value="emailForm.use_ssl" />
                </n-form-item>
              </n-gi>
            </n-grid>
            <n-grid :cols="2" :x-gap="12">
              <n-gi>
                <n-form-item :label="t('notify.maxPerMinute')" path="max_per_minute">
                  <n-input-number v-model:value="emailForm.max_per_minute" :min="1" :max="100" style="width: 200px" />
                </n-form-item>
              </n-gi>
              <n-gi>
                <n-form-item :label="t('notify.cooldown')" path="cooldown_seconds">
                  <n-input-number v-model:value="emailForm.cooldown_seconds" :min="0" :max="3600" style="width: 200px" />
                  <span style="margin-left: 8px; color: var(--n-text-color-3)">{{ t('notify.seconds') }}</span>
                </n-form-item>
              </n-gi>
            </n-grid>
            <n-form-item>
              <n-space>
                <n-button type="primary" :loading="saveLoading" @click="saveEmail">{{ t('common.save') }}</n-button>
                <n-button @click="testChannel('email')" :loading="testing">{{ t('notify.test') }}</n-button>
              </n-space>
            </n-form-item>
          </n-form>
        </n-tab-pane>

        <n-tab-pane name="webhook" :tab="t('notify.webhook')">
          <n-form ref="webhookFormRef" :model="webhookForm" :rules="webhookRules" label-placement="left" label-width="140">
            <n-form-item :label="t('notify.webhookUrl')" path="url" required>
              <n-input v-model:value="webhookForm.url" maxlength="255" :placeholder="t('notify.webhookUrlPlaceholder')" />
            </n-form-item>
            <n-form-item :label="t('notify.method')" path="method">
              <n-select v-model:value="webhookForm.method" :options="methodOptions" clearable style="width: 200px" />
            </n-form-item>
            <n-form-item :label="t('notify.headers')" path="headers">
              <n-input v-model:value="headersText" :maxlength="4096" type="textarea" :placeholder="t('notify.headersPlaceholder')" :rows="3" />
            </n-form-item>
            <n-form-item :label="t('notify.authType')" path="auth_type">
              <n-select v-model:value="webhookForm.auth_type" :options="authTypeOptions" clearable style="width: 200px" />
            </n-form-item>
            <template v-if="webhookForm.auth_type === 'bearer' || webhookForm.auth_type === 'api_key'">
              <n-form-item :label="t('notify.authToken')" path="auth_token">
                <n-input v-model:value="webhookForm.auth_token" maxlength="128" :placeholder="t('notify.authTokenPlaceholder')" show-password-on="click" />
              </n-form-item>
            </template>
            <template v-if="webhookForm.auth_type === 'basic'">
              <n-form-item :label="t('notify.authUsername')" path="auth_username">
                <n-input v-model:value="webhookForm.auth_username" maxlength="255" :placeholder="t('notify.authUsernamePlaceholder')" />
              </n-form-item>
              <n-form-item :label="t('notify.authPassword')" path="auth_password">
                <n-input v-model:value="webhookForm.auth_password" maxlength="128" type="password" show-password-on="click" :placeholder="t('notify.authPasswordPlaceholder')" />
              </n-form-item>
            </template>
            <n-grid :cols="2" :x-gap="12">
              <n-gi>
                <n-form-item :label="t('notify.maxPerMinute')" path="max_per_minute">
                  <n-input-number v-model:value="webhookForm.max_per_minute" :min="1" :max="100" style="width: 200px" />
                </n-form-item>
              </n-gi>
              <n-gi>
                <n-form-item :label="t('notify.cooldown')" path="cooldown_seconds">
                  <n-input-number v-model:value="webhookForm.cooldown_seconds" :min="0" :max="3600" style="width: 200px" />
                  <span style="margin-left: 8px; color: var(--n-text-color-3)">{{ t('notify.seconds') }}</span>
                </n-form-item>
              </n-gi>
            </n-grid>
            <n-form-item>
              <n-space>
                <n-button type="primary" :loading="saveLoading" @click="saveWebhook">{{ t('common.save') }}</n-button>
                <n-button @click="testChannel('webhook')" :loading="testing">{{ t('notify.test') }}</n-button>
              </n-space>
            </n-form-item>
          </n-form>
        </n-tab-pane>
      </n-tabs>
    </n-card>

    <n-card :title="t('notify.channelStatus')" style="margin-top: 16px">
      <n-list>
        <n-list-item v-for="channel in channelStatus" :key="channel.id">
          <template #prefix>
            <n-icon :component="getChannelIcon(channel.type)" size="24" :style="{ color: getChannelColor(channel.status) }" />
          </template>
          <n-thing :title="channel.name" :description="getChannelStatusText(channel)">
            <template #header-extra>
              <n-space>
                <n-tag :type="channel.status === 'configured' ? 'success' : 'default'" size="small">
                  {{ getStatusLabel(channel.status) }}
                </n-tag>
                <n-button size="small" @click="removeChannel(channel.id)" :disabled="channel.status === 'not_configured'">
                  {{ t('common.delete') }}
                </n-button>
              </n-space>
            </template>
          </n-thing>
        </n-list-item>
      </n-list>
    </n-card>

    <!-- 修复9: 测试发送历史记录 -->
    <n-card :title="t('notify.testHistory')" style="margin-top: 16px">
      <template #header-extra>
        <n-button v-if="testHistory.length" size="small" quaternary type="error" @click="clearTestHistory">{{ t('notify.clearHistory') }}</n-button>
      </template>
      <n-data-table
        v-if="testHistory.length"
        :columns="testHistoryColumns"
        :data="testHistory"
        :bordered="false"
        size="small"
        :pagination="false"
      />
      <n-empty v-else :description="t('notify.noTestHistory')" style="padding: 24px 0" />
    </n-card>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, h, nextTick } from 'vue'
import { type FormInst, type FormRules, NIcon, NTag } from 'naive-ui'
import {
  ChatboxEllipsesOutline,
  MailOutline,
  GlobeOutline,
  LinkOutline,
} from '@vicons/ionicons5'
import { t } from '@/i18n'
import { extractError } from '@/utils/errorCodes'
import { notifyApi, type NotifyChannelStatus } from '@/api'
import { useAuthStore } from '@/stores/auth'
import { message as msg, dialog } from '@/utils/discreteApi'
import { useDirtyFormGuard } from '@/composables/useDirtyFormGuard'

const dingtalkFormRef = ref<FormInst | null>(null)
const wecomFormRef = ref<FormInst | null>(null)
const emailFormRef = ref<FormInst | null>(null)
const webhookFormRef = ref<FormInst | null>(null)
const auth = useAuthStore()

const dingtalkRules = computed<FormRules>(() => ({
  webhook_url: [
    { required: true, message: t('notify.webhookUrlRequired'), trigger: ['input', 'blur'] },
    { type: 'url', message: t('notify.invalidUrl'), trigger: ['input', 'blur'] },
  ],
  max_per_minute: [
    { type: 'number', min: 1, max: 100, message: t('notify.rateLimitRange'), trigger: ['input', 'blur'] },
  ],
  cooldown_seconds: [
    { type: 'number', min: 0, max: 3600, message: t('notify.cooldownRange'), trigger: ['input', 'blur'] },
  ],
}))

const wecomRules = computed<FormRules>(() => ({
  webhook_url: [
    { required: true, message: t('notify.webhookUrlRequired'), trigger: ['input', 'blur'] },
    { type: 'url', message: t('notify.invalidUrl'), trigger: ['input', 'blur'] },
  ],
  max_per_minute: [
    { type: 'number', min: 1, max: 100, message: t('notify.rateLimitRange'), trigger: ['input', 'blur'] },
  ],
  cooldown_seconds: [
    { type: 'number', min: 0, max: 3600, message: t('notify.cooldownRange'), trigger: ['input', 'blur'] },
  ],
}))

const emailRules = computed<FormRules>(() => ({
  smtp_host: [
    { required: true, message: t('notify.smtpHostRequired'), trigger: ['input', 'blur'] },
  ],
  smtp_port: [
    { type: 'number', min: 1, max: 65535, message: t('notify.portRange'), trigger: 'blur' },
  ],
  to_addresses: [
    { required: true, type: 'array', min: 1, message: t('notify.toAddressesRequired'), trigger: 'change' },
  ],
  from_address: [
    { required: true, message: t('notify.fromAddressRequired'), trigger: ['input', 'blur'] },
    { type: 'email', message: t('notify.invalidEmail'), trigger: ['input', 'blur'] },
  ],
  max_per_minute: [
    { type: 'number', min: 1, max: 100, message: t('notify.rateLimitRange'), trigger: ['input', 'blur'] },
  ],
  cooldown_seconds: [
    { type: 'number', min: 0, max: 3600, message: t('notify.cooldownRange'), trigger: ['input', 'blur'] },
  ],
}))

const webhookRules = computed<FormRules>(() => ({
  url: [
    { required: true, message: t('notify.webhookUrlRequired'), trigger: ['input', 'blur'] },
    { type: 'url', message: t('notify.invalidUrl'), trigger: ['input', 'blur'] },
  ],
  method: [
    { required: true, message: t('notify.methodRequired'), trigger: ['change', 'blur'] },
  ],
  max_per_minute: [
    { type: 'number', min: 1, max: 100, message: t('notify.rateLimitRange'), trigger: ['input', 'blur'] },
  ],
  cooldown_seconds: [
    { type: 'number', min: 0, max: 3600, message: t('notify.cooldownRange'), trigger: ['input', 'blur'] },
  ],
  auth_token: [
    { trigger: ['input', 'blur'], validator: (_rule: any, value: any) => {
      if ((webhookForm.value.auth_type === 'bearer' || webhookForm.value.auth_type === 'api_key') && !value) {
        return new Error(t('notify.authTokenRequired'))
      }
      return true
    }},
  ],
  auth_username: [
    { trigger: ['input', 'blur'], validator: (_rule: any, value: any) => {
      if (webhookForm.value.auth_type === 'basic' && !value) {
        return new Error(t('notify.authUsernameRequired'))
      }
      return true
    }},
  ],
  auth_password: [
    { trigger: ['input', 'blur'], validator: (_rule: any, value: any) => {
      if (webhookForm.value.auth_type === 'basic' && !value) {
        return new Error(t('notify.authPasswordRequired'))
      }
      return true
    }},
  ],
}))

const activeTab = ref('dingtalk')
const saveLoading = ref(false)
const testing = ref(false)
const channelStatus = ref<NotifyChannelStatus[]>([])

// 修复9: 测试发送历史记录——本地数组保存最近10条测试记录
interface TestHistoryItem {
  id: string
  time: string
  channel: string
  success: boolean
  message: string
}
const testHistory = ref<TestHistoryItem[]>([])
function addTestHistory(channel: string, success: boolean, message: string) {
  testHistory.value.unshift({
    id: `th_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`,
    time: new Date().toLocaleString(),
    channel,
    success,
    message,
  })
  // 仅保留最近10条
  if (testHistory.value.length > 10) testHistory.value = testHistory.value.slice(0, 10)
}
function clearTestHistory() {
  testHistory.value = []
}

// 修复9: 测试历史表格列定义
const testHistoryColumns = computed(() => [
  { title: t('common.time'), key: 'time', width: 180 },
  { title: t('notify.title'), key: 'channel', width: 120 },
  {
    title: t('notify.testResult'), key: 'success', width: 100,
    render: (r: TestHistoryItem) => r.success
      ? h(NTag, { type: 'success', size: 'small', bordered: false }, { default: () => t('common.success') })
      : h(NTag, { type: 'error', size: 'small', bordered: false }, { default: () => t('common.failed') }),
  },
  { title: t('common.message'), key: 'message', render: (r: TestHistoryItem) => r.message },
])

const dingtalkForm = ref({
  webhook_url: '',
  secret: '',
  at_mobiles: [] as string[],
  is_at_all: false,
  max_per_minute: 10,
  cooldown_seconds: 60,
})

const wecomForm = ref({
  webhook_url: '',
  max_per_minute: 10,
  cooldown_seconds: 60,
})

const emailForm = ref({
  smtp_host: '',
  smtp_port: 587,
  smtp_user: '',
  smtp_password: '',
  from_address: '',
  to_addresses: [] as string[],
  use_tls: true,
  use_ssl: false,
  max_per_minute: 10,
  cooldown_seconds: 60,
})

const webhookForm = ref({
  url: '',
  method: 'POST',
  headers: {} as Record<string, string>,
  auth_type: 'none' as 'none' | 'basic' | 'bearer' | 'api_key',
  auth_token: '',
  auth_username: '',
  auth_password: '',
  max_per_minute: 10,
  cooldown_seconds: 60,
})

// [AUDIT-FIX] 严重级-表单未保存离开确认
const { markClean } = useDirtyFormGuard({
  watchSource: () => [dingtalkForm.value, wecomForm.value, emailForm.value, webhookForm.value],
})

const headersText = computed({
  get: () => {
    try {
      return JSON.stringify(webhookForm.value.headers, null, 2)
    } catch {
      return ''
    }
  },
  set: (val: string) => {
    // FIXED: 原问题-JSON.parse 失败时静默将 headers 设为 {}，用户输入非法 JSON 无任何提示，
    // 保存时会丢失所有 headers 配置。现解析失败时保留原 headers 并提示用户。
    if (!val.trim()) {
      webhookForm.value.headers = {}
      return
    }
    try {
      const parsed = JSON.parse(val)
      if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
        msg.warning(t('notify.headersMustBeObject') || 'Headers must be a JSON object')
        return
      }
      webhookForm.value.headers = parsed
    } catch {
      msg.warning(t('notify.headersInvalidJson') || 'Invalid JSON format, headers not updated')
    }
  }
})

const methodOptions = [
  { label: 'POST', value: 'POST' },
  { label: 'PUT', value: 'PUT' },
]

// [AUDIT-FIX] 一般级-authTypeOptions 改为 computed，语言切换时响应式更新
const authTypeOptions = computed(() => [
  { label: t('notify.authNone'), value: 'none' },
  { label: 'Basic Auth', value: 'basic' },
  { label: 'Bearer Token', value: 'bearer' },
  { label: 'API Key', value: 'api_key' },
])

async function loadChannels() {
  try {
    const data = await notifyApi.listChannels()
    channelStatus.value = data.channels || []
    data.channels?.forEach((ch: NotifyChannelStatus) => {
      if (ch.type === 'dingtalk') {
        dingtalkForm.value.webhook_url = ch.config.webhook_url || ''
        dingtalkForm.value.secret = ch.config.secret || ''
        // FIXED: 原问题-加载通道时未填充 at_mobiles/is_at_all/max_per_minute/cooldown_seconds，
        // 导致编辑已保存的通道时这些字段显示为空或默认值，保存后会丢失原配置。
        dingtalkForm.value.at_mobiles = ch.config.at_mobiles || []
        dingtalkForm.value.is_at_all = ch.config.is_at_all ?? false
        dingtalkForm.value.max_per_minute = ch.config.max_per_minute ?? 10
        dingtalkForm.value.cooldown_seconds = ch.config.cooldown_seconds ?? 60
      } else if (ch.type === 'wecom') {
        wecomForm.value.webhook_url = ch.config.webhook_url || ''
        wecomForm.value.max_per_minute = ch.config.max_per_minute ?? 10
        wecomForm.value.cooldown_seconds = ch.config.cooldown_seconds ?? 60
      } else if (ch.type === 'email') {
        emailForm.value.smtp_host = ch.config.smtp_host || ''
        emailForm.value.smtp_port = ch.config.smtp_port || 587
        emailForm.value.smtp_user = ch.config.smtp_user || ''
        emailForm.value.smtp_password = ch.config.smtp_password || ''
        emailForm.value.from_address = ch.config.from_address || ''
        emailForm.value.to_addresses = ch.config.to_addresses || []
        emailForm.value.use_tls = ch.config.use_tls ?? false
        emailForm.value.use_ssl = ch.config.use_ssl ?? false
        emailForm.value.max_per_minute = ch.config.max_per_minute ?? 10
        emailForm.value.cooldown_seconds = ch.config.cooldown_seconds ?? 60
      } else if (ch.type === 'webhook') {
        webhookForm.value.url = ch.config.url || ''
        webhookForm.value.method = ch.config.method || 'POST'
        webhookForm.value.headers = ch.config.headers || {}
        webhookForm.value.auth_type = ch.config.auth_type || 'none'
        webhookForm.value.auth_token = ch.config.auth_token || ''
        webhookForm.value.auth_username = ch.config.auth_username || ''
        webhookForm.value.auth_password = ch.config.auth_password || ''
        webhookForm.value.max_per_minute = ch.config.max_per_minute ?? 10
        webhookForm.value.cooldown_seconds = ch.config.cooldown_seconds ?? 60
      }
    })
  } catch (e: any) {
    msg.error(extractError(e, t('common.failed')))
  }
  // 加载通道数据会触发 watch 标记脏，需在 watch 回调后重置
  nextTick(() => markClean())
}

async function saveDingTalk() {
  if (!auth.isOperator) { msg.warning(t('common.permissionDenied')); return }
  try {
    await dingtalkFormRef.value?.validate()
  } catch { return }
  // 校验@手机号格式
  const mobileRegex = /^1[3-9]\d{9}$/
  const invalidMobiles = (dingtalkForm.value.at_mobiles || []).filter((m: string) => !mobileRegex.test(m))
  if (invalidMobiles.length > 0) {
    msg.error(t('notify.invalidMobileFormat'))
    return
  }
  saveLoading.value = true
  try {
    // FIXED: 原问题-保存前强制调用 testChannel，测试失败（网络瞬断/目标服务不可达）时
    // 保存操作永远不会执行，用户以为保存了实际没存。现将测试与保存解耦，保存只调用 update 接口。
    // 用户可通过独立的"测试"按钮主动测试连通性。
    await notifyApi.updateDingTalk({
      webhook_url: dingtalkForm.value.webhook_url,
      secret: dingtalkForm.value.secret,
      at_mobiles: dingtalkForm.value.at_mobiles || [],
      is_at_all: dingtalkForm.value.is_at_all,
      max_per_minute: dingtalkForm.value.max_per_minute ?? 10,
      cooldown_seconds: dingtalkForm.value.cooldown_seconds ?? 60,
    })
    msg.success(t('common.success'))
    markClean()
    loadChannels()
  } catch (e: any) {
    msg.error(extractError(e, t('common.failed')))
  } finally {
    saveLoading.value = false
  }
}

async function saveWeCom() {
  if (!auth.isOperator) { msg.warning(t('common.permissionDenied')); return }
  try {
    await wecomFormRef.value?.validate()
  } catch { return }
  saveLoading.value = true
  try {
    // FIXED: 同 saveDingTalk，移除保存前的强制测试
    await notifyApi.updateWeCom({
      webhook_url: wecomForm.value.webhook_url,
      max_per_minute: wecomForm.value.max_per_minute ?? 10,
      cooldown_seconds: wecomForm.value.cooldown_seconds ?? 60,
    })
    msg.success(t('common.success'))
    markClean()
    loadChannels()
  } catch (e: any) {
    msg.error(extractError(e, t('common.failed')))
  } finally {
    saveLoading.value = false
  }
}

async function saveEmail() {
  if (!auth.isOperator) { msg.warning(t('common.permissionDenied')); return }
  try {
    await emailFormRef.value?.validate()
  } catch { return }
  // 校验收件人邮箱格式
  const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/
  const invalidEmails = (emailForm.value.to_addresses || []).filter((a: string) => !emailRegex.test(a))
  if (invalidEmails.length > 0) {
    msg.error(t('notify.invalidEmailFormat'))
    return
  }
  saveLoading.value = true
  try {
    // FIXED: 同 saveDingTalk，移除保存前的强制测试
    await notifyApi.updateEmail({
      smtp_host: emailForm.value.smtp_host,
      smtp_port: emailForm.value.smtp_port ?? 587,
      smtp_user: emailForm.value.smtp_user,
      smtp_password: emailForm.value.smtp_password,
      from_address: emailForm.value.from_address,
      to_addresses: emailForm.value.to_addresses || [],
      use_tls: emailForm.value.use_tls,
      use_ssl: emailForm.value.use_ssl,
      max_per_minute: emailForm.value.max_per_minute ?? 10,
      cooldown_seconds: emailForm.value.cooldown_seconds ?? 60,
    })
    msg.success(t('common.success'))
    markClean()
    loadChannels()
  } catch (e: any) {
    msg.error(extractError(e, t('common.failed')))
  } finally {
    saveLoading.value = false
  }
}

async function saveWebhook() {
  if (!auth.isOperator) { msg.warning(t('common.permissionDenied')); return }
  try {
    await webhookFormRef.value?.validate()
  } catch { return }
  saveLoading.value = true
  try {
    // FIXED: 同 saveDingTalk，移除保存前的强制测试
    await notifyApi.updateWebhook({
      url: webhookForm.value.url,
      method: webhookForm.value.method,
      headers: webhookForm.value.headers || {},
      auth_type: webhookForm.value.auth_type,
      auth_token: webhookForm.value.auth_token,
      auth_username: webhookForm.value.auth_username,
      auth_password: webhookForm.value.auth_password,
      max_per_minute: webhookForm.value.max_per_minute ?? 10,
      cooldown_seconds: webhookForm.value.cooldown_seconds ?? 60,
    })
    msg.success(t('common.success'))
    markClean()
    loadChannels()
  } catch (e: any) {
    msg.error(extractError(e, t('common.failed')))
  } finally {
    saveLoading.value = false
  }
}

async function testChannel(channelId: string) {
  if (!auth.isOperator) { msg.warning(t('common.permissionDenied')); return }
  // Build config_override from current form data so test uses unsaved values
  let configOverride: Record<string, any> | undefined

  if (channelId === 'dingtalk') {
    if (!dingtalkForm.value.webhook_url) {
      msg.warning(t('notify.webhookUrlRequired')); return
    }
    configOverride = {
      webhook_url: dingtalkForm.value.webhook_url,
      secret: dingtalkForm.value.secret,
      at_mobiles: dingtalkForm.value.at_mobiles,
      is_at_all: dingtalkForm.value.is_at_all,
    }
  } else if (channelId === 'wecom') {
    if (!wecomForm.value.webhook_url) {
      msg.warning(t('notify.webhookUrlRequired')); return
    }
    configOverride = {
      webhook_url: wecomForm.value.webhook_url,
    }
  } else if (channelId === 'email') {
    if (!emailForm.value.smtp_host) {
      msg.warning(t('notify.smtpHostRequired')); return
    }
    configOverride = {
      smtp_host: emailForm.value.smtp_host,
      smtp_port: emailForm.value.smtp_port,
      smtp_user: emailForm.value.smtp_user,
      smtp_password: emailForm.value.smtp_password,
      from_address: emailForm.value.from_address,
      to_addresses: emailForm.value.to_addresses,
      use_tls: emailForm.value.use_tls,
      use_ssl: emailForm.value.use_ssl,
    }
  } else if (channelId === 'webhook') {
    if (!webhookForm.value.url) {
      msg.warning(t('notify.webhookUrlRequired')); return
    }
    configOverride = {
      url: webhookForm.value.url,
      method: webhookForm.value.method,
      headers: webhookForm.value.headers,
      auth_type: webhookForm.value.auth_type,
      auth_token: webhookForm.value.auth_token,
      auth_username: webhookForm.value.auth_username,
      auth_password: webhookForm.value.auth_password,
    }
  }

  testing.value = true
  try {
    const result = await notifyApi.testChannel(channelId, configOverride)
    if (result.success) {
      msg.success(t('notify.testSuccess'))
      addTestHistory(channelId, true, result.message || t('notify.testSuccess'))
    } else {
      msg.error(result.message || t('notify.testFailed'))
      addTestHistory(channelId, false, result.message || t('notify.testFailed'))
    }
  } catch (e: any) {
    if (e?.response?.status === 400) {
      msg.warning(t('notify.channelNotConfigured'))
      addTestHistory(channelId, false, t('notify.channelNotConfigured'))
    } else {
      msg.error(extractError(e, t('notify.testFailed')))
      addTestHistory(channelId, false, extractError(e, t('notify.testFailed')))
    }
  } finally {
    testing.value = false
  }
}

async function removeChannel(channelId: string) {
  // [AUDIT-FIX] 严重级-删除通道属高危操作，需操作员及以上权限（与 save/test 操作保持一致）
  if (!auth.isOperator) { msg.warning(t('common.permissionDenied')); return }
  // FIXED: 原问题-删除通道无二次确认对话框，误点击会直接删除通道无法撤销。
  // 现添加 dialog.warning 二次确认。
  dialog.warning({
    title: t('common.confirmDelete'),
    content: t('notify.removeChannelConfirm') || t('common.confirmDeleteDesc'),
    positiveText: t('common.delete'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      try {
        await notifyApi.deleteChannel(channelId)
        msg.success(t('common.success'))
        loadChannels()
      } catch (e: any) {
        msg.error(extractError(e, t('common.failed')))
      }
    },
  })
}

function getChannelIcon(type: string) {
  switch (type) {
    case 'dingtalk': return ChatboxEllipsesOutline
    case 'wecom': return ChatboxEllipsesOutline
    case 'email': return MailOutline
    case 'webhook': return LinkOutline
    default: return GlobeOutline
  }
}

function getChannelColor(status: string) {
  switch (status) {
    case 'configured': return '#18a058'
    case 'not_configured': return '#d03050'
    default: return '#808080'
  }
}

function getChannelStatusText(channel: NotifyChannelStatus) {
  switch (channel.status) {
    case 'configured': return t('notify.configured')
    case 'not_configured': return t('notify.notConfigured')
    default: return channel.status
  }
}

function getStatusLabel(status: string) {
  switch (status) {
    case 'configured': return t('notify.configured')
    case 'not_configured': return t('notify.notConfigured')
    default: return status
  }
}

onMounted(() => {
  loadChannels()
})
</script>

<style scoped>
.notify-config {
  max-width: 900px;
}
</style>
