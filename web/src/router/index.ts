import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { t } from '@/i18n'
import { message as discreteMessage } from '@/utils/discreteApi'
import PlaceholderView from '@/views/PlaceholderView.vue'

function _showPermissionDenied(msg: string) {
  discreteMessage.warning(msg)
}

export function setMessageInstance(_instance: { warning: (msg: string) => void }) {
  // kept for backward compat, no-op
}

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'Login',
      component: () => import('@/views/Login.vue'),
      meta: { requiresAuth: false },
    },
    {
      path: '/setup',
      name: 'Setup',
      component: PlaceholderView,
      meta: { requiresAuth: true, requiredRole: 'admin' },
    },
    {
      path: '/',
      component: () => import('@/layouts/MainLayout.vue'),
      meta: { requiresAuth: true },
      children: [
        { path: '', name: 'Dashboard', component: () => import('@/views/Dashboard.vue'), meta: { title: t('router.dashboard') } },
        { path: 'devices', name: 'Devices', component: () => import('@/views/device/DeviceList.vue'), meta: { title: t('router.devices') } },
        { path: 'devices/templates', name: 'DeviceTemplates', component: PlaceholderView, meta: { title: t('router.deviceTemplates'), requiredRole: ['admin', 'operator'] } },
        { path: 'devices/:id', name: 'DeviceDetail', component: () => import('@/views/device/DeviceDetail.vue'), meta: { title: t('router.deviceDetail') } },
        { path: 'devices/shadow', name: 'DeviceShadow', component: PlaceholderView, meta: { title: t('router.deviceShadow'), requiredRole: ['admin', 'operator'] } },
        { path: 'rules', name: 'Rules', component: () => import('@/views/rule/RuleList.vue'), meta: { title: t('router.rules') } },
        { path: 'alarms', name: 'Alarms', component: () => import('@/views/alarm/AlarmList.vue'), meta: { title: t('router.alarms') } },
        { path: 'data', name: 'DataQuery', component: () => import('@/views/data/DataQuery.vue'), meta: { title: t('router.dataQuery') } },
        { path: 'report', name: 'Report', component: PlaceholderView, meta: { title: t('router.report') } },
        { path: 'data/quality', name: 'DataQuality', component: PlaceholderView, meta: { title: t('router.dataQuality') } },
        { path: 'data/quality-monitor', name: 'DataQualityMonitor', component: PlaceholderView, meta: { title: t('router.qualityMonitor') } },
        { path: 'system', name: 'System', component: () => import('@/views/system/SystemStatus.vue'), meta: { title: t('router.system') } },
        { path: 'system/services', name: 'ServiceOverview', component: () => import('@/views/system/ServiceOverview.vue'), meta: { title: t('router.services'), requiredRole: 'admin' } },
        { path: 'system/drivers', name: 'DriverConfig', component: () => import('@/views/system/DriverConfig.vue'), meta: { title: t('router.drivers'), requiredRole: 'admin' } },
        { path: 'system/platforms', name: 'PlatformConfig', component: () => import('@/views/system/PlatformConfig.vue'), meta: { title: t('router.platforms'), requiredRole: 'admin' } },
        { path: 'system/platforms/dashboard', name: 'PlatformDashboard', component: PlaceholderView, meta: { title: t('router.platformDashboard'), requiredRole: 'admin' } },
        { path: 'system/platforms/tb-monitor', name: 'TbMonitor', component: PlaceholderView, meta: { title: t('router.tbMonitor'), requiredRole: 'admin' } },
        { path: 'system/platforms/custom-mqtt/:name', name: 'CustomMqttConfig', component: PlaceholderView, meta: { title: t('router.customMqttConfig'), requiredRole: 'admin' } },
        { path: 'system/expressions', name: 'ExpressionConfig', component: () => import('@/views/system/ExpressionConfig.vue'), meta: { title: t('router.expressions'), requiredRole: 'admin' } },
        { path: 'system/preprocess', name: 'PreprocessConfig', component: () => import('@/views/system/PreprocessConfig.vue'), meta: { title: t('router.preprocess'), requiredRole: 'admin' } },
        { path: 'system/audit', name: 'AuditLog', component: () => import('@/views/system/AuditLog.vue'), meta: { title: t('router.audit'), requiredRole: 'admin' } },
        { path: 'system/serial-bridge', name: 'SerialBridge', component: () => import('@/views/system/SerialBridge.vue'), meta: { title: t('router.serialBridge'), requiredRole: 'admin' } },
        { path: 'system/bridge', name: 'BridgeConfig', component: PlaceholderView, meta: { title: t('router.bridgeConfig'), requiredRole: 'admin' } },
        { path: 'system/pipeline', name: 'PipelineEditor', component: PlaceholderView, meta: { title: t('router.pipelineConfig'), requiredRole: 'admin' } },
        { path: 'system/mqtt-server', name: 'MqttServer', component: () => import('@/views/system/MqttServer.vue'), meta: { title: t('router.mqttServer'), requiredRole: 'admin' } },
        { path: 'system/modbus-slave', name: 'ModbusSlave', component: () => import('@/views/system/ModbusSlave.vue'), meta: { title: t('router.modbusSlave'), requiredRole: 'admin' } },
        { path: 'system/app-update', name: 'AppUpdate', component: PlaceholderView, meta: { title: t('router.appUpdate'), requiredRole: 'admin' } },
        { path: 'system/grafana', name: 'GrafanaDashboard', component: () => import('@/views/system/GrafanaDashboard.vue'), meta: { title: t('router.grafana'), requiredRole: 'admin' } },
        { path: 'system/mcp', name: 'McpServer', component: () => import('@/views/system/McpServer.vue'), meta: { title: t('router.mcp'), requiredRole: 'admin' } },
        { path: 'system/ai-model', name: 'AiModel', component: () => import('@/views/system/AiModel.vue'), meta: { title: t('router.aiModel'), requiredRole: 'admin' } },
        { path: 'system/ai-monitor', name: 'AiMonitor', component: PlaceholderView, meta: { title: t('router.aiMonitor'), requiredRole: 'admin' } },
        { path: 'system/ai-ab-test', name: 'AiAbTest', component: PlaceholderView, meta: { title: t('router.aiAbTest'), requiredRole: 'admin' } },
        { path: 'system/linkage', name: 'DeviceLinkage', component: PlaceholderView, meta: { title: t('router.linkage'), requiredRole: 'admin' } },
        { path: 'system/profiler', name: 'ProfilerView', component: PlaceholderView, meta: { title: t('router.profiler'), requiredRole: 'admin' } },
        { path: 'system/log-aggregator', name: 'LogAggregator', component: PlaceholderView, meta: { title: t('router.logAggregator'), requiredRole: 'admin' } },
        { path: 'system/firmware-signature', name: 'FirmwareSignature', component: PlaceholderView, meta: { title: t('router.firmwareSignature'), requiredRole: 'admin' } },
        { path: 'system/notify', name: 'NotifyConfig', component: () => import('@/views/system/NotifyConfig.vue'), meta: { title: t('router.notify'), requiredRole: 'admin' } },
        { path: 'system/integration', name: 'Integration', component: () => import('@/views/system/Integration.vue'), meta: { title: t('router.integration'), requiredRole: 'admin' } },
        { path: 'system/debug', name: 'ProtocolDebug', component: PlaceholderView, meta: { title: t('router.debug'), requiredRole: 'admin' } },
        { path: 'system/metrics', name: 'Metrics', component: PlaceholderView, meta: { title: t('metrics.title'), requiredRole: 'admin' } },
        { path: 'system/config-version', name: 'ConfigVersion', component: PlaceholderView, meta: { title: t('router.configVersion'), requiredRole: 'admin' } },
        { path: 'system/self-test', name: 'SelfTest', component: PlaceholderView, meta: { title: t('router.selfTest'), requiredRole: 'admin' } },
        { path: 'system/data-export', name: 'DataExport', component: PlaceholderView, meta: { title: t('router.dataExport'), requiredRole: 'admin' } },
        { path: 'system/data-import', name: 'DataImport', component: PlaceholderView, meta: { title: t('router.dataImport'), requiredRole: 'admin' } },
        { path: 'system/resource-sharing', name: 'ResourceSharing', component: PlaceholderView, meta: { title: t('router.resourceSharing'), requiredRole: 'admin' } },
        { path: 'data/downsample', name: 'DataDownsample', component: PlaceholderView, meta: { title: t('router.dataDownsample'), requiredRole: 'admin' } },
        { path: 'system/db-monitor', name: 'DbMonitor', component: PlaceholderView, meta: { title: t('router.dbMonitor'), requiredRole: 'admin' } },
        { path: 'alarms/trend', name: 'AlarmTrend', component: PlaceholderView, meta: { title: t('router.alarmTrend'), requiredRole: ['admin', 'operator'] } },
        { path: 'alarms/correlation', name: 'AlarmCorrelation', component: PlaceholderView, meta: { title: t('router.alarmCorrelation') || 'Alarm Correlation', requiredRole: ['admin', 'operator'] } },
        { path: 'system/backup-schedule', name: 'BackupSchedule', component: PlaceholderView, meta: { title: t('router.backupSchedule'), requiredRole: 'admin' } },
        { path: 'system/config', name: 'SystemConfig', component: PlaceholderView, meta: { title: t('router.systemConfig'), requiredRole: 'admin' } },
        { path: 'observability', name: 'Observability', redirect: '/observability/overview' },
        { path: 'observability/overview', name: 'ObservabilityOverview', component: PlaceholderView, meta: { title: t('router.observabilityOverview'), requiredRole: 'admin' } },
        { path: 'observability/rules', name: 'ObservabilityRulesPage', component: PlaceholderView, meta: { title: t('router.observabilityRules'), requiredRole: 'admin' } },
        { path: 'observability/events', name: 'ObservabilityEventsPage', component: PlaceholderView, meta: { title: t('router.observabilityEvents'), requiredRole: 'admin' } },
        { path: 'observability/traces', name: 'ObservabilityTraces', component: PlaceholderView, meta: { title: t('router.observabilityTraces'), requiredRole: 'admin' } },
        { path: 'observability/metrics', name: 'ObservabilityMetrics', component: PlaceholderView, meta: { title: t('router.observabilityMetrics'), requiredRole: 'admin' } },
        { path: 'system/scripts', name: 'ScriptEngine', component: PlaceholderView, meta: { title: t('router.scripts'), requiredRole: 'admin' } },
        { path: 'system/simulation', name: 'Simulation', component: PlaceholderView, meta: { title: t('router.simulation'), requiredRole: 'admin' } },
        { path: 'system/anomaly-learner', name: 'AnomalyLearner', component: PlaceholderView, meta: { title: t('router.anomalyLearner'), requiredRole: 'admin' } },
        { path: 'system/trend-learner', name: 'TrendLearner', component: PlaceholderView, meta: { title: t('router.trendLearner'), requiredRole: 'admin' } },
        { path: 'system/threshold-learner', name: 'ThresholdLearner', component: PlaceholderView, meta: { title: t('router.thresholdLearner'), requiredRole: 'admin' } },
        { path: 'system/ai-center', name: 'AiCenter', component: PlaceholderView, meta: { title: t('router.aiCenter'), requiredRole: 'admin' } },
        { path: 'system/ai-test', name: 'AiTest', component: PlaceholderView, meta: { title: t('router.aiTest'), requiredRole: 'admin' } },
        { path: 'system/calibration', name: 'CalibrationData', component: PlaceholderView, meta: { title: t('router.calibration'), requiredRole: 'admin' } },
        { path: 'system/physics-calibrator', name: 'PhysicsCalibrator', component: PlaceholderView, meta: { title: t('router.physCalib'), requiredRole: 'admin' } },
        { path: 'system/physics-param-db', name: 'PhysicsParamDb', component: PlaceholderView, meta: { title: t('router.paramDb'), requiredRole: 'admin' } },
        { path: 'system/precision-test', name: 'PrecisionTest', component: PlaceholderView, meta: { title: t('router.precTest'), requiredRole: 'admin' } },
        { path: 'system/evolution-verify', name: 'EvolutionVerify', component: PlaceholderView, meta: { title: t('router.evoVerify'), requiredRole: 'admin' } },
        { path: 'system/boundary-test', name: 'AiBoundaryTest', component: PlaceholderView, meta: { title: t('router.bndTest'), requiredRole: 'admin' } },
        { path: 'system/stress-test', name: 'AiStressTest', component: PlaceholderView, meta: { title: t('router.stressTest'), requiredRole: 'admin' } },
        { path: 'system/ai-report', name: 'AiReportCenter', component: PlaceholderView, meta: { title: t('router.aiRpt'), requiredRole: 'admin' } },
        { path: 'modbus-ops', name: 'ModbusOps', component: PlaceholderView, meta: { title: t('router.modbusOps'), requiredRole: ['admin', 'operator'] } },
        { path: 'users', name: 'Users', component: () => import('@/views/system/UserManage.vue'), meta: { title: t('router.users'), requiredRole: 'admin' } },
        { path: 'digital-twin', name: 'DigitalTwin', component: () => import('@/views/digital-twin/DigitalTwin.vue'), meta: { title: t('router.digitalTwin') } },
        { path: 'scada', name: 'ScadaEditor', component: () => import('@/views/scada/ScadaEditor.vue'), meta: { title: t('router.scada'), requiredRole: ['admin', 'operator'] } },
      ],
    },
    { path: '/dashboard/large-screen', name: 'LargeScreen', component: PlaceholderView, meta: { requiresAuth: true } },
    { path: '/:pathMatch(.*)*', name: 'NotFound', component: () => import('@/views/NotFound.vue'), meta: { requiresAuth: false } },
  ],
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()

  // 未认证用户：跳转登录页
  if (!auth.isAuthenticated) {
    if (to.meta.requiresAuth !== false && to.name !== 'Login') {
      return { name: 'Login', query: { redirect: to.fullPath } }
    }
    return
  }

  // 强制改密检查：mustChangePassword 时只允许访问登录页
  if (auth.mustChangePassword && to.name !== 'Login') {
    return { name: 'Login' }
  }

  // 已认证但缺少角色信息：尝试获取
  if (!auth.role) {
    try {
      await auth.fetchUserInfo()
    } catch (e: any) {
      // 只在 401（认证失败）时登出，其他错误（503/网络不可达）允许停留在当前页
      if (e?.response?.status === 401 && !auth.role) {
        auth.logout()
        return { name: 'Login', query: { redirect: to.fullPath } }
      }
      // 非 401 错误：如果仍无 role，允许用户继续（可能后端暂时不可达）
      // 不强制 logout，避免后端短暂抖动导致用户被踢
    }
  }

  // 需要认证但未认证（防御性检查）
  if (to.meta.requiresAuth !== false && !auth.isAuthenticated) {
    return { name: 'Login', query: { redirect: to.fullPath } }
  }

  // FIXED-P2: 支持requiredRole为数组，精确控制operator/viewer可访问的路由
  // 之前：仅检查requiredRole==='admin'，operator和viewer无法区分
  // 之后：requiredRole支持string|string[]，admin始终放行，其他角色需在数组中
  if (to.meta.requiredRole) {
    const required = to.meta.requiredRole as string | string[]
    if (auth.role === 'admin') { /* admin拥有所有权限 */ }
    else {
      const allowedRoles = Array.isArray(required) ? required : [required]
      if (!allowedRoles.includes(auth.role || '')) {
        _showPermissionDenied(t('common.permissionDenied'))
        return { name: 'Dashboard' }
      }
    }
  }
})

// [AUDIT-FIX] 致命级-路由错误处理：动态 import 失败（chunk 加载失败）时自动整页刷新
// 场景：发版后旧 hash chunk 失效、CDN 异常、断网恢复时首次切换路由
// 不刷新会停留在白屏，用户无法恢复
router.onError((error, to) => {
  // 仅处理 chunk 动态加载失败
  const msg = (error?.message || '') + ' ' + (error?.name || '')
  const isChunkError = /Failed to fetch dynamically imported module|Importing a module script failed|Loading chunk \S+ failed|Loading CSS chunk \S+ failed|chunk \S+ not found/i.test(msg)
  if (!isChunkError) {
    console.error('[Router Error]', error)
    return
  }
  console.error('[Router Chunk Load Error]', error)
  // 已知目标路由：尝试整页跳转到目标 fullPath，强制重新加载最新资源
  if (to?.fullPath) {
    // 使用 location.href 触发整页加载，避免 SPA 路由再次复用失败的 chunk
    window.location.href = to.fullPath
  } else {
    window.location.reload()
  }
})

export default router
