import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

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
        { path: 'system/drivers', name: 'DriverConfig', component: () => import('@/views/system/DriverConfig.vue') },
        { path: 'system/platforms', name: 'PlatformConfig', component: () => import('@/views/system/PlatformConfig.vue') },
        { path: 'system/expressions', name: 'ExpressionConfig', component: () => import('@/views/system/ExpressionConfig.vue') },
        { path: 'users', name: 'Users', component: () => import('@/views/system/UserManage.vue'), meta: { requiredRole: 'admin' } },
        { path: 'audit', name: 'AuditLog', component: () => import('@/views/audit/AuditLog.vue'), meta: { requiredRole: 'admin' } },
        { path: 'digital-twin', name: 'DigitalTwin', component: () => import('@/views/digital-twin/DigitalTwin.vue') },
        { path: 'scada', name: 'ScadaEditor', component: () => import('@/views/scada/ScadaEditor.vue') },
      ],
    },
  ],
})

router.beforeEach((to) => {
  const auth = useAuthStore()
  if (to.meta.requiresAuth !== false && !auth.isAuthenticated) {
    return { name: 'Login' }
  }
  if (to.meta.requiredRole && auth.role !== 'admin') {
    return { name: 'Dashboard' }
  }
})

export default router
