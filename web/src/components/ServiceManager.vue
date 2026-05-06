<template>
  <n-space vertical :size="16">
    <n-card :bordered="false">
      <template #header>
        <n-space align="center" :size="12">
          <span style="font-size: 16px; font-weight: 600">{{ displayName }}</span>
          <n-tag :type="stateTagType" size="small" round>{{ stateLabel }}</n-tag>
        </n-space>
      </template>
      <template #header-extra>
        <n-space :size="8">
          <n-switch
            :value="enabled"
            :loading="toggleLoading"
            @update:value="handleToggle"
          >
            <template #checked>已启用</template>
            <template #unchecked>已停用</template>
          </n-switch>
          <n-button
            v-if="enabled && !running"
            type="primary"
            size="small"
            @click="handleStart"
            :loading="starting"
          >启动服务</n-button>
          <n-button
            v-if="running"
            type="warning"
            size="small"
            @click="handleStop"
            :loading="stopping"
          >停止服务</n-button>
          <n-button size="small" quaternary @click="fetchStatus" :loading="loading">
            <template #icon><n-icon><refresh-outline /></n-icon></template>
          </n-button>
        </n-space>
      </template>

      <n-alert
        v-if="missingDeps.length > 0 && enabled"
        type="warning"
        :bordered="false"
        style="margin-bottom: 16px"
      >
        <template #header>缺少依赖组件，服务无法启动</template>
        以下依赖未安装：<n-tag v-for="d in missingDeps" :key="d.package" size="small" type="warning" style="margin: 0 4px">{{ d.package }}</n-tag>
        <div style="margin-top: 8px">
          <n-button
            type="primary"
            size="small"
            @click="handleInstallDeps"
            :loading="installing"
          >一键安装依赖</n-button>
          <n-text depth="3" style="margin-left: 8px; font-size: 12px">安装完成后服务将自动启动</n-text>
        </div>
      </n-alert>

      <n-alert
        v-if="errorMessage"
        type="error"
        :bordered="false"
        style="margin-bottom: 16px"
      >{{ errorMessage }}</n-alert>

      <template v-if="!enabled">
        <n-alert type="info" :bordered="false" style="margin-bottom: 16px">
          <template #header>服务未启用</template>
          {{ description }}
          <div style="margin-top: 8px">开启右上角的开关即可启用此服务，启用后可随时停用，无需重启网关。</div>
        </n-alert>

        <n-card embedded size="small" title="什么时候需要启用此服务？" style="margin-bottom: 12px">
          <n-ul>
            <n-li v-for="(uc, i) in useCases" :key="i">
              <n-text>{{ uc }}</n-text>
            </n-li>
          </n-ul>
        </n-card>

        <n-card v-if="relatedFeatures.length > 0" embedded size="small" title="关联功能">
          <n-space vertical :size="4">
            <n-space v-for="(rf, i) in relatedFeatures" :key="i" align="center" :size="8">
              <n-text depth="3">●</n-text>
              <n-text>{{ rf.name }}</n-text>
              <n-text depth="3" style="font-size: 12px">— {{ rf.hint }}</n-text>
              <n-button text type="primary" size="tiny" @click="$router.push({ name: rf.route })">前往 →</n-button>
            </n-space>
          </n-space>
        </n-card>
      </template>

      <template v-else-if="enabled && !running && missingDeps.length === 0">
        <n-alert type="success" :bordered="false" style="margin-bottom: 16px">
          <template #header>服务已启用，准备就绪</template>
          点击上方"启动服务"按钮即可运行。服务启动后将按照配置参数工作。
        </n-alert>
      </template>

      <template v-if="running">
        <n-descriptions label-placement="left" :column="2" bordered>
          <n-descriptions-item v-for="item in runningFields" :key="item.label" :label="item.label">
            {{ item.value ?? '-' }}
          </n-descriptions-item>
        </n-descriptions>
      </template>

      <slot name="extra"></slot>
    </n-card>

    <n-collapse v-if="enabled">
      <n-collapse-item title="使用场景" name="usecases">
        <template #header-extra>
          <n-tag size="small" round>{{ useCases.length }}</n-tag>
        </template>
        <n-ul>
          <n-li v-for="(uc, i) in useCases" :key="i">{{ uc }}</n-li>
        </n-ul>
      </n-collapse-item>

      <n-collapse-item title="关联功能" name="related">
        <template #header-extra>
          <n-tag size="small" round>{{ relatedFeatures.length }}</n-tag>
        </template>
        <n-space vertical :size="8">
          <n-card v-for="(rf, i) in relatedFeatures" :key="i" embedded size="small" hoverable style="cursor: pointer" @click="$router.push({ name: rf.route })">
            <n-space align="center" justify="space-between">
              <n-space align="center" :size="8">
                <n-text strong>{{ rf.name }}</n-text>
                <n-text depth="3" style="font-size: 13px">{{ rf.hint }}</n-text>
              </n-space>
              <n-text type="primary" style="font-size: 13px">前往查看 →</n-text>
            </n-space>
          </n-card>
        </n-space>
      </n-collapse-item>

      <n-collapse-item title="启用指南" name="guide">
        <template #header-extra>
          <n-tag size="small" round>{{ setupGuide.length }}步</n-tag>
        </template>
        <n-steps vertical :current="currentStep" size="small">
          <n-step v-for="(step, i) in setupGuide" :key="i" :title="`步骤 ${i + 1}`" :description="step" />
        </n-steps>
      </n-collapse-item>
    </n-collapse>

    <n-card v-if="enabled && Object.keys(configSchema).length > 0" title="服务配置" :bordered="false">
      <template #header-extra>
        <n-space :size="8">
          <n-text depth="3" style="font-size: 12px">修改配置后服务将自动重启</n-text>
          <n-button type="primary" size="small" @click="handleSaveConfig" :loading="savingConfig">保存配置</n-button>
        </n-space>
      </template>
      <n-form :model="configForm" label-placement="left" label-width="140">
        <n-grid :cols="2" :x-gap="16">
          <n-form-item-gi
            v-for="(schema, key) in configSchema"
            :key="key"
            :label="schema.label || key"
          >
            <template #label>
              <n-tooltip v-if="schema.description" trigger="hover">
                <template #trigger>
                  <n-text>{{ schema.label || key }} <n-text depth="3" style="font-size: 11px">ⓘ</n-text></n-text>
                </template>
                {{ schema.description }}
              </n-tooltip>
              <n-text v-else>{{ schema.label || key }}</n-text>
            </template>
            <n-input-number
              v-if="schema.type === 'integer'"
              v-model:value="configForm[key]"
              :placeholder="String(schema.default ?? '')"
              style="width: 100%"
            />
            <n-input
              v-else
              v-model:value="configForm[key]"
              :placeholder="String(schema.default ?? '')"
            />
          </n-form-item-gi>
        </n-grid>
      </n-form>
    </n-card>

    <n-modal v-model:show="showInstallProgress" title="安装依赖" preset="card" style="width: 480px" :closable="!installing" :mask-closable="false">
      <n-spin :description="installProgress">
        <n-space vertical>
          <n-alert v-if="installing" type="info" :bordered="false">
            正在安装缺失的依赖组件，请勿关闭此窗口...
          </n-alert>
          <n-alert v-if="installResult && !installing" :type="installSuccess ? 'success' : 'error'" :bordered="false">
            {{ installResult }}
          </n-alert>
          <n-text v-if="installSuccess && !installing" depth="3" style="font-size: 13px">
            依赖安装成功！服务将自动启动。
          </n-text>
        </n-space>
      </n-spin>
      <template #action>
        <n-button @click="showInstallProgress = false" :disabled="installing">{{ installing ? '安装中...' : '关闭' }}</n-button>
      </template>
    </n-modal>
  </n-space>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import { useMessage, useDialog } from 'naive-ui'
import { RefreshOutline } from '@vicons/ionicons5'
import { serviceApi } from '@/api'
import type { ServiceDependency, ServiceRelatedFeature } from '@/api'

const props = defineProps<{
  serviceName: string
  displayName: string
  runningFields: { label: string; value: any }[]
}>()

const emit = defineEmits<{
  (e: 'status-loaded', data: any): void
}>()

const message = useMessage()
const dialog = useDialog()
const loading = ref(false)
const starting = ref(false)
const stopping = ref(false)
const toggleLoading = ref(false)
const savingConfig = ref(false)
const installing = ref(false)
const showInstallProgress = ref(false)
const installProgress = ref('')
const installResult = ref('')
const installSuccess = ref(false)

const enabled = ref(false)
const running = ref(false)
const state = ref<string>('disabled')
const errorMessage = ref('')
const description = ref('')
const dependencies = ref<ServiceDependency[]>([])
const useCases = ref<string[]>([])
const relatedFeatures = ref<ServiceRelatedFeature[]>([])
const setupGuide = ref<string[]>([])
const configSchema = ref<Record<string, any>>({})
const currentConfig = ref<Record<string, any>>({})
const configForm = reactive<Record<string, any>>({})

const missingDeps = computed(() => dependencies.value.filter(d => !d.installed))

const currentStep = computed(() => {
  if (running.value) return setupGuide.value.length + 1
  if (enabled.value && missingDeps.value.length === 0) return setupGuide.value.length
  if (enabled.value) return 2
  return 1
})

const stateTagType = computed(() => {
  switch (state.value) {
    case 'running': return 'success'
    case 'enabled': return 'info'
    case 'error': return 'error'
    case 'installing': return 'warning'
    default: return 'default'
  }
})

const stateLabel = computed(() => {
  switch (state.value) {
    case 'running': return '运行中'
    case 'enabled': return '已启用'
    case 'error': return '异常'
    case 'installing': return '安装中'
    case 'disabled': return '未启用'
    default: return state.value
  }
})

async function fetchStatus() {
  loading.value = true
  try {
    const data = await serviceApi.status(props.serviceName)
    enabled.value = data.state !== 'disabled'
    running.value = data.state === 'running'
    state.value = data.state
    errorMessage.value = data.error_message || ''
    description.value = data.description || ''
    dependencies.value = data.dependencies || []
    useCases.value = data.use_cases || []
    relatedFeatures.value = data.related_features || []
    setupGuide.value = data.setup_guide || []
    configSchema.value = data.config_schema || {}
    currentConfig.value = data.current_config || {}

    Object.keys(configForm).forEach(k => delete configForm[k])
    for (const [key, schema] of Object.entries(configSchema.value)) {
      configForm[key] = currentConfig.value[key] ?? (schema as any).default ?? ''
    }

    emit('status-loaded', data)
  } catch (e: any) {
    message.error(e?.message || '获取状态失败')
  } finally {
    loading.value = false
  }
}

async function handleToggle(val: boolean) {
  toggleLoading.value = true
  try {
    if (val) {
      await serviceApi.enable(props.serviceName)
      message.success(`${props.displayName}已启用`)
    } else {
      await serviceApi.disable(props.serviceName)
      message.success(`${props.displayName}已停用`)
    }
    await fetchStatus()
  } catch (e: any) {
    const detail = e?.response?.data?.detail
    if (typeof detail === 'object' && detail?.missing_dependencies) {
      errorMessage.value = detail.message || '缺少依赖'
      dependencies.value = detail.missing_dependencies.map((p: string) => ({
        package: p, installed: false, version: ''
      }))
      enabled.value = true
      state.value = 'error'
      message.warning(detail.message || '缺少依赖，请先安装')
    } else {
      message.error(typeof detail === 'string' ? detail : (e?.message || '操作失败'))
    }
  } finally {
    toggleLoading.value = false
  }
}

async function handleStart() {
  starting.value = true
  try {
    await serviceApi.start(props.serviceName)
    message.success(`${props.displayName}已启动`)
    await fetchStatus()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || '启动失败')
  } finally {
    starting.value = false
  }
}

async function handleStop() {
  dialog.warning({
    title: '确认停止',
    content: `确定要停止「${props.displayName}」吗？正在使用该服务的设备可能会断开连接。`,
    positiveText: '确认停止',
    negativeText: '取消',
    onPositiveClick: async () => {
      stopping.value = true
      try {
        await serviceApi.stop(props.serviceName)
        message.success(`${props.displayName}已停止`)
        await fetchStatus()
      } catch (e: any) {
        message.error(e?.response?.data?.detail || '停止失败')
      } finally {
        stopping.value = false
      }
    },
  })
}

async function handleInstallDeps() {
  installing.value = true
  showInstallProgress.value = true
  installProgress.value = '正在安装依赖...'
  installResult.value = ''
  installSuccess.value = false
  try {
    await serviceApi.installDeps(props.serviceName)
    installResult.value = '依赖安装成功！'
    installSuccess.value = true
    message.success('依赖安装成功，正在启动服务...')
    await serviceApi.start(props.serviceName)
    await fetchStatus()
  } catch (e: any) {
    installResult.value = `安装失败: ${e?.response?.data?.detail || e?.message}`
    installSuccess.value = false
    message.error('依赖安装失败')
  } finally {
    installing.value = false
    installProgress.value = ''
  }
}

async function handleSaveConfig() {
  savingConfig.value = true
  try {
    await serviceApi.updateConfig(props.serviceName, { ...configForm })
    message.success('配置已保存，服务将自动重启')
    await fetchStatus()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || '保存配置失败')
  } finally {
    savingConfig.value = false
  }
}

onMounted(fetchStatus)
</script>
