<template>
  <div class="login-container">
    <div class="login-bg">
      <div class="bg-shape bg-shape-1"></div>
      <div class="bg-shape bg-shape-2"></div>
      <div class="bg-shape bg-shape-3"></div>
    </div>
    <n-card class="login-card" :bordered="false">
      <div class="login-header">
        <div class="login-logo">
          <svg viewBox="0 0 128 128" width="48" height="48">
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
        <h1 class="login-title">EdgeLiteGateway</h1>
        <p class="login-subtitle">轻量级边缘计算物联网网关</p>
      </div>
      <n-form ref="formRef" :model="form" :rules="rules" size="large">
        <n-form-item path="username">
          <n-input v-model:value="form.username" placeholder="用户名" @keyup.enter="handleLogin">
            <template #prefix><n-icon :component="PersonOutline" /></template>
          </n-input>
        </n-form-item>
        <n-form-item path="password">
          <n-input v-model:value="form.password" type="password" show-password-on="click" placeholder="密码" @keyup.enter="handleLogin">
            <template #prefix><n-icon :component="LockClosedOutline" /></template>
          </n-input>
        </n-form-item>
        <n-button type="primary" block :loading="loading" @click="handleLogin" style="margin-top: 8px">
          登 录
        </n-button>
      </n-form>
      <div class="login-footer">
        <n-text depth="3">v1.0.0 Community Edition</n-text>
      </div>
    </n-card>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive } from 'vue'
import { useRouter } from 'vue-router'
import { useMessage } from 'naive-ui'
import { PersonOutline, LockClosedOutline } from '@vicons/ionicons5'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const message = useMessage()
const auth = useAuthStore()
const loading = ref(false)

const form = reactive({ username: 'admin', password: 'admin123' })
const rules = {
  username: { required: true, message: '请输入用户名', trigger: 'blur' },
  password: { required: true, message: '请输入密码', trigger: 'blur' },
}

async function handleLogin() {
  loading.value = true
  try {
    await auth.login(form.username, form.password)
    message.success('登录成功')
    router.push('/')
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || '登录失败')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-container {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  position: relative;
  overflow: hidden;
}
.login-bg {
  position: absolute;
  inset: 0;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  z-index: 0;
}
.bg-shape {
  position: absolute;
  border-radius: 50%;
  background: rgba(255,255,255,0.1);
  animation: float 20s infinite ease-in-out;
}
.bg-shape-1 { width: 600px; height: 600px; top: -200px; left: -200px; }
.bg-shape-2 { width: 400px; height: 400px; bottom: -100px; right: -100px; animation-delay: -5s; }
.bg-shape-3 { width: 200px; height: 200px; top: 50%; left: 60%; animation-delay: -10s; }
@keyframes float {
  0%, 100% { transform: translate(0, 0) rotate(0deg); }
  50% { transform: translate(30px, 30px) rotate(180deg); }
}
.login-card {
  width: 400px;
  z-index: 1;
  border-radius: 16px;
  box-shadow: 0 20px 60px rgba(0,0,0,0.3);
}
.login-header {
  text-align: center;
  margin-bottom: 32px;
}
.login-logo {
  margin-bottom: 16px;
}
.login-title {
  font-size: 24px;
  font-weight: 600;
  color: #333;
  margin: 0 0 8px 0;
}
.login-subtitle {
  font-size: 14px;
  color: #999;
  margin: 0;
}
.login-footer {
  text-align: center;
  margin-top: 24px;
  padding-top: 16px;
  border-top: 1px solid #eee;
}
</style>
