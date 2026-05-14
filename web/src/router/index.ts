import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { t } from '@/i18n'

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
      path: '/',
      component: () => import('@/layouts/MainLayout.vue'),
      meta: { requiresAuth: true },
      children: [
        { path: '', name: 'Dashboard', component: () => import('@/views/Dashboard.vue') },
        { path: 'devices', name: 'Devices', component: () => import('@/views/device/DeviceList.vue') },
        { path: 'devices/:id', name: 'DeviceDetail', component: () => import('@/views/device/DeviceDetail.vue') },
        { path: 'rules', name: 'Rules', component: () => import('@/views/rule/RuleList.vue') },
        { path: 'alarms', name: 'Alarms', component: () => import('@/views/alarm/AlarmList.vue') },
        { path: 'data', name: 'DataQuery', component: () => import('@/views/data/DataQuery.vue') },
        { path: 'system', name: 'System', component: () => import('@/views/system/SystemStatus.vue') },
        { path: 'system/services', name: 'ServiceOverview', component: () => import('@/views/system/ServiceOverview.vue'), meta: { requiredRole: 'admin' } },
        { path: 'system/drivers', name: 'DriverConfig', component: () => import('@/views/system/DriverConfig.vue') },
        { path: 'system/platforms', name: 'PlatformConfig', component: () => import('@/views/system/PlatformConfig.vue') },
        { path: 'system/expressions', name: 'ExpressionConfig', component: () => import('@/views/system/ExpressionConfig.vue') },
        // FIXED: 原问题-路由meta.title硬编码中文，改为i18n
        { path: 'system/preprocess', name: 'PreprocessConfig', component: () => import('@/views/system/PreprocessConfig.vue'), meta: { title: t('router.preprocess'), requiredRole: 'admin' } },
        { path: 'system/audit', name: 'AuditLog', component: () => import('@/views/system/AuditLog.vue'), meta: { requiredRole: 'admin' } },
        { path: 'system/serial-bridge', name: 'SerialBridge', component: () => import('@/views/system/SerialBridge.vue'), meta: { requiredRole: 'admin' } },
        { path: 'system/mqtt-server', name: 'MqttServer', component: () => import('@/views/system/MqttServer.vue'), meta: { requiredRole: 'admin' } },
        { path: 'system/modbus-slave', name: 'ModbusSlave', component: () => import('@/views/system/ModbusSlave.vue'), meta: { requiredRole: 'admin' } },
        { path: 'system/ota', name: 'OtaUpdate', component: () => import('@/views/system/OtaUpdate.vue'), meta: { requiredRole: 'admin' } },
        { path: 'system/grafana', name: 'GrafanaDashboard', component: () => import('@/views/system/GrafanaDashboard.vue') },
        { path: 'system/mcp', name: 'McpServer', component: () => import('@/views/system/McpServer.vue'), meta: { requiredRole: 'admin' } },
        { path: 'users', name: 'Users', component: () => import('@/views/system/UserManage.vue'), meta: { requiredRole: 'admin' } },
        { path: 'digital-twin', name: 'DigitalTwin', component: () => import('@/views/digital-twin/DigitalTwin.vue') },
        { path: 'scada', name: 'ScadaEditor', component: () => import('@/views/scada/ScadaEditor.vue') },
      ],
    },
    { path: '/:pathMatch(.*)*', name: 'NotFound', component: () => import('@/views/NotFound.vue'), meta: { requiresAuth: false } },
  ],
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if (auth.isAuthenticated && !auth.role) {
    await auth.fetchUserInfo()
  }
  if (to.meta.requiresAuth !== false && !auth.isAuthenticated) {
    return { name: 'Login', query: { redirect: to.fullPath } }
  }
  if (to.meta.requiredRole) {
    const required = to.meta.requiredRole as string
    if (auth.role === 'admin') { /* admin拥有所有权限 */ }
    else if (auth.role !== required) {
      return { name: 'Dashboard' }
    }
  }
})

export default router
