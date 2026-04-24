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
        { path: 'system', name: 'System', component: () => import('@/views/system/SystemStatus.vue') },
        { path: 'users', name: 'Users', component: () => import('@/views/system/UserManage.vue') },
      ],
    },
  ],
})

router.beforeEach((to) => {
  const auth = useAuthStore()
  if (to.meta.requiresAuth !== false && !auth.isAuthenticated) {
    return { name: 'Login' }
  }
})

export default router
