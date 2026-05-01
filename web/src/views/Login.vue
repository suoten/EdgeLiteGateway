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
          <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128" width="48" height="48">
            <defs>
              <linearGradient id="crystalGrad1" x1="0%" y1="0%" x2="100%" y2="100%">
                <stop offset="0%" style="stop-color:#0f172a;stop-opacity:1" />
                <stop offset="50%" style="stop-color:#1e293b;stop-opacity:1" />
                <stop offset="100%" style="stop-color:#0f172a;stop-opacity:1" />
              </linearGradient>
              <linearGradient id="crystalGrad2" x1="0%" y1="0%" x2="100%" y2="100%">
                <stop offset="0%" style="stop-color:#38bdf8;stop-opacity:1" />
                <stop offset="50%" style="stop-color:#818cf8;stop-opacity:1" />
                <stop offset="100%" style="stop-color:#a78bfa;stop-opacity:1" />
              </linearGradient>
              <linearGradient id="crystalGrad3" x1="0%" y1="100%" x2="100%" y2="0%">
                <stop offset="0%" style="stop-color:#22d3ee;stop-opacity:1" />
                <stop offset="100%" style="stop-color:#f472b6;stop-opacity:1" />
              </linearGradient>
              <filter id="crystalGlow">
                <feGaussianBlur stdDeviation="4" result="coloredBlur"/>
                <feMerge>
                  <feMergeNode in="coloredBlur"/>
                  <feMergeNode in="SourceGraphic"/>
                </feMerge>
              </filter>
              <filter id="crystalShine">
                <feGaussianBlur stdDeviation="1" result="blur"/>
                <feOffset dx="0" dy="-1" result="offsetBlur"/>
                <feComposite in="SourceGraphic" in2="offsetBlur" operator="arithmetic" k2="0" k3="0" k4="1"/>
              </filter>
            </defs>
            <polygon points="64,8 108,36 108,84 64,112 20,84 20,36" fill="url(#crystalGrad1)" opacity="0.95"/>
            <polygon points="64,14 102,38 102,82 64,106 26,82 26,38" fill="none" stroke="url(#crystalGrad2)" stroke-width="1" opacity="0.6"/>
            <polygon points="64,6 112,34 112,86 64,114 16,86 16,34" fill="none" stroke="url(#crystalGrad2)" stroke-width="0.5" opacity="0.3"/>
            <polygon points="64,24 88,38 88,78 64,92 40,78 40,38" fill="url(#crystalGrad2)" filter="url(#crystalGlow)"/>
            <polygon points="64,24 64,92 40,78 40,38" fill="url(#crystalGrad2)" opacity="0.7"/>
            <polygon points="64,24 88,38 88,78 64,92" fill="url(#crystalGrad3)" opacity="0.5"/>
            <polygon points="64,30 82,42 82,74 64,86 46,74 46,42" fill="url(#crystalGrad1)" opacity="0.8"/>
            <polygon points="64,36 76,46 76,72 64,82 52,72 52,46" fill="url(#crystalGrad2)"/>
            <polygon points="64,36 64,82 52,72 52,46" fill="url(#crystalGrad2)" opacity="0.8"/>
            <polygon points="64,36 76,46 76,72 64,82" fill="url(#crystalGrad3)" opacity="0.6"/>
            <polygon points="64,44 72,49 72,69 64,74 56,69 56,49" fill="white" opacity="0.9"/>
            <polygon points="64,24 64,36 46,42 40,38" fill="white" opacity="0.15"/>
            <polygon points="64,24 88,38 82,42 64,36" fill="white" opacity="0.25"/>
            <line x1="40" y1="38" x2="46" y2="42" stroke="url(#crystalGrad3)" stroke-width="0.8" opacity="0.6"/>
            <line x1="88" y1="38" x2="82" y2="42" stroke="url(#crystalGrad3)" stroke-width="0.8" opacity="0.6"/>
            <line x1="40" y1="78" x2="46" y2="74" stroke="url(#crystalGrad3)" stroke-width="0.8" opacity="0.6"/>
            <line x1="88" y1="78" x2="82" y2="74" stroke="url(#crystalGrad3)" stroke-width="0.8" opacity="0.6"/>
          </svg>          
        </div>
        <h1 class="login-title">EdgeLiteGateway</h1>
        <p class="login-subtitle">轻量级边缘计算物联网网关</p>
      </div>
      <n-form ref="formRef" :model="form" :rules="rules" size="large">
        <n-form-item path="username">
          <n-input v-model:value="form.username" placeholder="请输入用户名" @keyup.enter="handleLogin">
            <template #prefix><n-icon :component="PersonOutline" /></template>
          </n-input>
        </n-form-item>
        <n-form-item path="password">
          <n-input v-model:value="form.password" type="password" show-password-on="click" placeholder="请输入密码" @keyup.enter="handleLogin">
            <template #prefix><n-icon :component="LockClosedOutline" /></template>
          </n-input>
        </n-form-item>
        <n-button type="primary" block :loading="loading" @click="handleLogin" style="margin-top: 8px">
          登 录
        </n-button>
      </n-form>
      <n-text depth="3" style="display:block;text-align:center;margin-top:16px;font-size:13px">
        默认账号: admin / admin123
      </n-text>
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

const form = reactive({ username: '', password: '' })
const rules = {
  username: { required: true, message: '请输入用户名', trigger: 'blur' },
  password: { required: true, message: '请输入密码', trigger: 'blur' },
}
const formRef = ref<any>(null)

async function handleLogin() {
  try {
    await formRef.value?.validate()
  } catch { return }
  loading.value = true
  try {
    await auth.login(form.username, form.password)
    message.success('登录成功，欢迎回来！')
    router.push('/')
  } catch (e: any) {
    const detail = e?.response?.data?.detail || e?.message || ''
    if (detail.includes('429') || detail.includes('频繁') || detail.includes('过多')) {
      message.warning('登录尝试过于频繁，请稍后再试')
    } else if (detail.includes('401') || detail.includes('用户名或密码')) {
      message.error('用户名或密码不正确，请重新输入')
    } else if (detail.includes('403') || detail.includes('禁用')) {
      message.error('该账户已被禁用，请联系管理员')
    } else if (detail.includes('网络') || detail.includes('connect')) {
      message.error('网络连接失败，请检查网络后重试')
    } else {
      message.error(detail || '登录失败，请稍后重试')
    }
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
