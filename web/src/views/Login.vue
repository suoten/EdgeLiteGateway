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
        <p class="login-subtitle">{{ t('login.title') }}</p>
      </div>
      <n-form v-if="!showChangePassword" ref="formRef" :model="form" :rules="rules" size="large">
        <n-form-item path="username">
          <n-input v-model:value="form.username" :placeholder="t('login.username')" @keyup.enter="handleLogin">
            <template #prefix><n-icon :component="PersonOutline" /></template>
          </n-input>
        </n-form-item>
        <n-form-item path="password">
          <n-input v-model:value="form.password" type="password" show-password-on="click" :placeholder="t('login.password')" @keyup.enter="handleLogin">
            <template #prefix><n-icon :component="LockClosedOutline" /></template>
          </n-input>
        </n-form-item>
        <n-button type="primary" block :loading="loading" @click="handleLogin" style="margin-top: 8px">
          {{ t('login.submit') }}
        </n-button>
      </n-form>
      <n-form v-else ref="changePwdFormRef" :model="changePwdForm" :rules="changePwdRules" size="large">
        <n-alert type="warning" style="margin-bottom:16px">
          {{ t('login.mustChangePassword') }}
        </n-alert>
        <!-- FIXED: 原问题-表单label/placeholder中文硬编码，现使用t() -->
        <n-form-item path="old_password" :label="t('login.oldPassword')">
          <n-input v-model:value="changePwdForm.old_password" type="password" show-password-on="click" :placeholder="t('login.oldPassword')" />
        </n-form-item>
        <n-form-item path="new_password" :label="t('login.newPassword')">
          <n-input v-model:value="changePwdForm.new_password" type="password" show-password-on="click" :placeholder="t('login.passwordPolicy')" />
        </n-form-item>
        <n-form-item path="confirm_password" :label="t('login.confirmPassword')">
          <n-input v-model:value="changePwdForm.confirm_password" type="password" show-password-on="click" :placeholder="t('login.confirmPassword')" />
        </n-form-item>
        <n-button type="primary" block :loading="changingPwd" @click="handleChangePassword" style="margin-top: 8px">
          {{ t('login.changePassword') }}
        </n-button>
      </n-form>
      <n-text depth="3" style="display:block;text-align:center;margin-top:16px;font-size:13px">
        {{ t('login.firstLoginHint') }}
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
import { authApi } from '@/api'
import { t } from '@/i18n'
import { getErrorMessage } from '@/utils/errorCodes'

const router = useRouter()
const message = useMessage()
const auth = useAuthStore()
const loading = ref(false)
const showChangePassword = ref(false)
const changingPwd = ref(false)

const form = reactive({ username: '', password: '' })
const rules = {
  username: { required: true, message: t('login.usernameRequired'), trigger: 'blur' },
  password: { required: true, message: t('login.password'), trigger: 'blur' },
}
const formRef = ref<any>(null)

const changePwdForm = reactive({ old_password: '', new_password: '', confirm_password: '' })
const changePwdRules = {
  old_password: { required: true, message: t('login.oldPassword'), trigger: 'blur' },
  new_password: [
    { required: true, message: t('login.newPassword'), trigger: 'blur' },
    { min: 8, message: t('login.passwordMinLength'), trigger: 'blur' },
    { validator: (_rule: Any, value: string) => /[a-zA-Z]/.test(value) && /\d/.test(value), message: t('login.passwordLetterAndDigit'), trigger: 'blur' },  // FIXED: 原问题-硬编码英文，改用i18n
  ],
  confirm_password: [
    { required: true, message: t('login.confirmPassword'), trigger: 'blur' },
    { validator: (_rule: any, value: string) => value === changePwdForm.new_password, message: t('login.passwordMismatch'), trigger: 'blur' },  // FIXED: 原问题-硬编码英文，改用i18n
  ],
}
const changePwdFormRef = ref<any>(null)

async function handleLogin() {
  try {
    await formRef.value?.validate()
  } catch { return }
  loading.value = true
  try {
    await auth.login(form.username, form.password)
    if (auth.mustChangePassword) {
      showChangePassword.value = true
      changePwdForm.old_password = form.password
      // FIXED: 原问题-硬编码中文消息，改为i18n
      message.warning(t('loginPage.mustChangePassword'))
    } else {
      // FIXED: 原问题-硬编码中文消息，改为i18n
      message.success(t('loginPage.loginSuccess'))
      const redirect = (router.currentRoute.value.query.redirect as string) || '/'
      router.push(redirect)
    }
  } catch (e: any) {
    // FIXED: 原问题-错误分类依赖中文字符串匹配(detail.includes('频繁'))，
    // 现改为基于HTTP status code判断，并使用错误码映射i18n
    const status = e?.response?.status
    const detail = e?.response?.data?.detail || e?.message || ''
    if (status === 429) {
      message.warning(getErrorMessage(detail))
    } else if (status === 401) {
      message.error(getErrorMessage(detail))
    } else if (status === 403) {
      message.error(getErrorMessage(detail))
    } else if (!status) {
      // FIXED: 原问题-非Axios错误(如fetchUserInfo抛Error)走入此分支显示"网络错误"，
      // 现区分isBusinessError和纯网络错误
      if (e?.isBusinessError) {
        message.error(e?.message || t('loginPage.loginFailed'))
      } else {
        message.error(t('loginPage.networkError'))
      }
    } else {
      message.error(getErrorMessage(detail))
    }
  } finally {
    loading.value = false
  }
}

async function handleChangePassword() {
  try {
    await changePwdFormRef.value?.validate()
  } catch { return }
  changingPwd.value = true
  try {
    await authApi.changePassword(
      changePwdForm.old_password,
      changePwdForm.new_password,
    )
    auth.mustChangePassword = false
    sessionStorage.setItem('edgelite_mustChangePassword', 'false')
    message.success(t('login.passwordChanged'))
    // FIXED: 原问题-修改密码后未重新获取token，若后端使旧token失效则后续请求401
    // 现在修改密码成功后重新登录获取新token
    try {
      await auth.login(auth.username, changePwdForm.new_password)
    } catch (reLoginError: any) {
      // FIXED: 原问题-重新登录失败时误用passwordChanged(成功语义)作为错误消息
      message.error(t('login.passwordChangeFailed'))
      router.push('/login')
      return
    }
    router.push('/')
  } catch (e: any) {
    const detail = e?.response?.data?.detail || e?.message || ''
    message.error(getErrorMessage(detail) || t('login.passwordChangeFailed'))
  } finally {
    changingPwd.value = false
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
