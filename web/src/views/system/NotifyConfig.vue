<template>
  <div class="notify-config">
    <n-card :title="t('notify.title')">
      <n-tabs v-model:value="activeTab" type="line" animated>
        <n-tab-pane name="dingtalk" :tab="t('notify.dingtalk')">
          <n-form ref="dingtalkFormRef" :model="dingtalkForm" label-placement="left" label-width="140">
            <n-form-item :label="t('notify.webhookUrl')" path="webhook_url" required>
              <n-input v-model:value="dingtalkForm.webhook_url" :placeholder="t('notify.dingtalkWebhookPlaceholder')" />
            </n-form-item>
            <n-form-item :label="t('notify.secret')" path="secret">
              <n-input v-model:value="dingtalkForm.secret" :placeholder="t('notify.secretPlaceholder')" show-password-on="click" />
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
          <n-form ref="wecomFormRef" :model="wecomForm" label-placement="left" label-width="140">
            <n-form-item :label="t('notify.webhookUrl')" path="webhook_url" required>
              <n-input v-model:value="wecomForm.webhook_url" :placeholder="t('notify.wecomWebhookPlaceholder')" />
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
          <n-form ref="emailFormRef" :model="emailForm" label-placement="left" label-width="140">
            <n-grid :cols="2" :x-gap="12">
              <n-gi>
                <n-form-item :label="t('notify.smtpHost')" path="smtp_host" required>
                  <n-input v-model:value="emailForm.smtp_host" :placeholder="t('notify.smtpHostPlaceholder')" />
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
                  <n-input v-model:value="emailForm.smtp_user" :placeholder="t('notify.smtpUserPlaceholder')" />
                </n-form-item>
              </n-gi>
              <n-gi>
                <n-form-item :label="t('notify.smtpPassword')" path="smtp_password">
                  <n-input v-model:value="emailForm.smtp_password" type="password" show-password-on="click" :placeholder="t('notify.smtpPasswordPlaceholder')" />
                </n-form-item>
              </n-gi>
            </n-grid>
            <n-form-item :label="t('notify.fromAddress')" path="from_address">
              <n-input v-model:value="emailForm.from_address" :placeholder="t('notify.fromAddressPlaceholder')" />
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
          <n-form ref="webhookFormRef" :model="webhookForm" label-placement="left" label-width="140">
            <n-form-item :label="t('notify.webhookUrl')" path="url" required>
              <n-input v-model:value="webhookForm.url" :placeholder="t('notify.webhookUrlPlaceholder')" />
            </n-form-item>
            <n-form-item :label="t('notify.method')" path="method">
              <n-select v-model:value="webhookForm.method" :options="methodOptions" style="width: 200px" />
            </n-form-item>
            <n-form-item :label="t('notify.headers')" path="headers">
              <n-input v-model:value="headersText" type="textarea" :placeholder="t('notify.headersPlaceholder')" :rows="3" />
            </n-form-item>
            <n-form-item :label="t('notify.authType')" path="auth_type">
              <n-select v-model:value="webhookForm.auth_type" :options="authTypeOptions" style="width: 200px" />
            </n-form-item>
            <template v-if="webhookForm.auth_type === 'bearer' || webhookForm.auth_type === 'api_key'">
              <n-form-item :label="t('notify.authToken')" path="auth_token">
                <n-input v-model:value="webhookForm.auth_token" :placeholder="t('notify.authTokenPlaceholder')" show-password-on="click" />
              </n-form-item>
            </template>
            <template v-if="webhookForm.auth_type === 'basic'">
              <n-form-item :label="t('notify.authUsername')" path="auth_username">
                <n-input v-model:value="webhookForm.auth_username" :placeholder="t('notify.authUsernamePlaceholder')" />
              </n-form-item>
              <n-form-item :label="t('notify.authPassword')" path="auth_password">
                <n-input v-model:value="webhookForm.auth_password" type="password" show-password-on="click" :placeholder="t('notify.authPasswordPlaceholder')" />
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
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useMessage } from 'naive-ui'
import { NIcon } from 'naive-ui'
import {
  ChatboxEllipsesOutline,
  MailOutline,
  GlobeOutline,
  LinkOutline,
} from '@vicons/ionicons5'
import { t } from '@/i18n'
import { notifyApi, type NotifyChannelStatus } from '@/api'

const msg = useMessage()

const activeTab = ref('dingtalk')
const saveLoading = ref(false)
const testing = ref(false)
const channelStatus = ref<NotifyChannelStatus[]>([])

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

const headersText = computed({
  get: () => {
    try {
      return JSON.stringify(webhookForm.value.headers, null, 2)
    } catch {
      return ''
    }
  },
  set: (val: string) => {
    try {
      webhookForm.value.headers = JSON.parse(val)
    } catch {
      webhookForm.value.headers = {}
    }
  }
})

const methodOptions = [
  { label: 'POST', value: 'POST' },
  { label: 'PUT', value: 'PUT' },
]

const authTypeOptions = [
  { label: t('notify.authNone'), value: 'none' },
  { label: 'Basic Auth', value: 'basic' },
  { label: 'Bearer Token', value: 'bearer' },
  { label: 'API Key', value: 'api_key' },
]

async function loadChannels() {
  try {
    const data = await notifyApi.listChannels()
    channelStatus.value = data.channels || []
    data.channels?.forEach((ch: NotifyChannelStatus) => {
      if (ch.type === 'dingtalk') {
        dingtalkForm.value.webhook_url = ch.config.webhook_url || ''
        dingtalkForm.value.secret = ch.config.secret || ''
      } else if (ch.type === 'wecom') {
        wecomForm.value.webhook_url = ch.config.webhook_url || ''
      } else if (ch.type === 'email') {
        emailForm.value.smtp_host = ch.config.smtp_host || ''
        emailForm.value.smtp_port = ch.config.smtp_port || 587
        emailForm.value.from_address = ch.config.from_address || ''
        emailForm.value.to_addresses = ch.config.to_addresses || []
      } else if (ch.type === 'webhook') {
        webhookForm.value.url = ch.config.url || ''
        webhookForm.value.method = ch.config.method || 'POST'
      }
    })
  } catch (e: any) {
    msg.error(e?.message || t('common.failed'))
  }
}

async function saveDingTalk() {
  saveLoading.value = true
  try {
    await notifyApi.updateDingTalk({
      webhook_url: dingtalkForm.value.webhook_url,
      secret: dingtalkForm.value.secret,
      at_mobiles: dingtalkForm.value.at_mobiles,
      is_at_all: dingtalkForm.value.is_at_all,
      max_per_minute: dingtalkForm.value.max_per_minute,
      cooldown_seconds: dingtalkForm.value.cooldown_seconds,
    })
    msg.success(t('common.success'))
    loadChannels()
  } catch (e: any) {
    msg.error(e?.message || t('common.failed'))
  } finally {
    saveLoading.value = false
  }
}

async function saveWeCom() {
  saveLoading.value = true
  try {
    await notifyApi.updateWeCom({
      webhook_url: wecomForm.value.webhook_url,
      max_per_minute: wecomForm.value.max_per_minute,
      cooldown_seconds: wecomForm.value.cooldown_seconds,
    })
    msg.success(t('common.success'))
    loadChannels()
  } catch (e: any) {
    msg.error(e?.message || t('common.failed'))
  } finally {
    saveLoading.value = false
  }
}

async function saveEmail() {
  saveLoading.value = true
  try {
    await notifyApi.updateEmail({
      smtp_host: emailForm.value.smtp_host,
      smtp_port: emailForm.value.smtp_port,
      smtp_user: emailForm.value.smtp_user,
      smtp_password: emailForm.value.smtp_password,
      from_address: emailForm.value.from_address,
      to_addresses: emailForm.value.to_addresses,
      use_tls: emailForm.value.use_tls,
      use_ssl: emailForm.value.use_ssl,
      max_per_minute: emailForm.value.max_per_minute,
      cooldown_seconds: emailForm.value.cooldown_seconds,
    })
    msg.success(t('common.success'))
    loadChannels()
  } catch (e: any) {
    msg.error(e?.message || t('common.failed'))
  } finally {
    saveLoading.value = false
  }
}

async function saveWebhook() {
  saveLoading.value = true
  try {
    await notifyApi.updateWebhook({
      url: webhookForm.value.url,
      method: webhookForm.value.method,
      headers: webhookForm.value.headers,
      auth_type: webhookForm.value.auth_type,
      auth_token: webhookForm.value.auth_token,
      auth_username: webhookForm.value.auth_username,
      auth_password: webhookForm.value.auth_password,
      max_per_minute: webhookForm.value.max_per_minute,
      cooldown_seconds: webhookForm.value.cooldown_seconds,
    })
    msg.success(t('common.success'))
    loadChannels()
  } catch (e: any) {
    msg.error(e?.message || t('common.failed'))
  } finally {
    saveLoading.value = false
  }
}

async function testChannel(channelId: string) {
  testing.value = true
  try {
    const result = await notifyApi.testChannel(channelId)
    if (result.success) {
      msg.success(t('notify.testSuccess'))
    } else {
      msg.error(result.message || t('notify.testFailed'))
    }
  } catch (e: any) {
    msg.error(e?.message || t('notify.testFailed'))
  } finally {
    testing.value = false
  }
}

async function removeChannel(channelId: string) {
  try {
    await notifyApi.deleteChannel(channelId)
    msg.success(t('common.success'))
    loadChannels()
  } catch (e: any) {
    msg.error(e?.message || t('common.failed'))
  }
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
