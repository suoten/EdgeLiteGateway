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
          <n-input v-model:value="form.username" :maxlength="32" aria-label="username" autocomplete="username" :placeholder="t('login.username')" @keyup.enter="handleLogin">
            <template #prefix><n-icon :component="PersonOutline" /></template>
          </n-input>
        </n-form-item>
        <n-form-item path="password">
          <!-- FIXED-UECapsLock: 密码框无大写锁定检测，用户输错密码后才发现。
               现通过 keyup/keydown 监听 CapsLock 状态，实时提示。 -->
          <n-input
            v-model:value="form.password"
            :maxlength="72"
            type="password"
            show-password-on="click"
            autocomplete="current-password"
            :placeholder="t('login.password')"
            @keyup.enter="handleLogin"
            @keyup="onCapsLockCheck"
            @keydown="onCapsLockCheck"
          >
            <template #prefix><n-icon :component="LockClosedOutline" /></template>
          </n-input>
        </n-form-item>
        <n-alert v-if="capsLockOn" type="warning" :show-icon="true" style="margin: -8px 0 8px; font-size: 13px">
          {{ t('login.capsLockOn') }}
        </n-alert>
        <n-button type="primary" block :loading="loading" @click="handleLogin" style="margin-top: 8px">
          {{ t('login.submit') }}
        </n-button>
      </n-form>

      <n-text depth="3" style="display:block;text-align:center;margin-top:8px;font-size:13px" v-if="!showChangePassword">
        {{ t('login.firstLoginHint') }}
      </n-text>
      <n-form v-else ref="changePwdFormRef" :model="changePwdForm" :rules="changePwdRules" size="large">
        <n-alert type="warning" style="margin-bottom:16px">
          {{ t('login.mustChangePassword') }}
        </n-alert>
        <!-- FIXED: 原问题-表单label/placeholder中文硬编码，现使用t() -->
        <n-form-item path="old_password" :label="t('login.oldPassword')">
          <n-input v-model:value="changePwdForm.old_password" :maxlength="72" type="password" show-password-on="click" autocomplete="current-password" :placeholder="t('login.oldPassword')" />
        </n-form-item>
        <n-form-item path="new_password" :label="t('login.newPassword')">
          <n-input v-model:value="changePwdForm.new_password" :maxlength="72" type="password" show-password-on="click" autocomplete="new-password" :placeholder="t('login.passwordPolicy')" />
        </n-form-item>
        <!-- FIXED-UEStrength: 修改密码有4条规则(长度/字母数字/特殊字符)，原实现仅在 blur 时逐条报错，
             用户需多次提交尝试。现提供实时强度指示器与规则清单，输入时即时反馈。 -->
        <div v-if="changePwdForm.new_password" class="pwd-strength">
          <div class="pwd-strength-bar">
            <div
              v-for="i in 4"
              :key="i"
              class="pwd-strength-seg"
              :class="{ active: i <= pwdStrength.score, [`s${pwdStrength.score}`]: i <= pwdStrength.score }"
            />
          </div>
          <n-text depth="3" style="font-size:12px; margin-left:8px">{{ pwdStrength.label }}</n-text>
          <div class="pwd-rules">
            <div v-for="r in pwdRules" :key="r.key" class="pwd-rule" :class="{ met: r.met }">
              <n-icon :component="r.met ? CheckmarkCircle : CloseCircle" :color="r.met ? '#52c41a' : '#c0c0c0'" size="14" />
              <span>{{ r.label }}</span>
            </div>
          </div>
        </div>
        <n-form-item path="confirm_password" :label="t('login.confirmPassword')">
          <n-input v-model:value="changePwdForm.confirm_password" :maxlength="72" type="password" show-password-on="click" autocomplete="new-password" :placeholder="t('login.confirmPassword')" />
        </n-form-item>
        <n-button type="primary" block :loading="changingPwd" @click="handleChangePassword" style="margin-top: 8px">
          {{ t('login.changePassword') }}
        </n-button>
      </n-form>
      <div class="login-footer">
        <n-text depth="3">{{ appVersion }} Community Edition</n-text>  <!-- FIXED-P3: 版本号硬编码v1.0.0，改用__APP_VERSION__动态获取 -->
      </div>
    </n-card>

  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed } from 'vue'
import { useRouter } from 'vue-router'
import { PersonOutline, LockClosedOutline, CheckmarkCircle, CloseCircle } from '@vicons/ionicons5'
import { useAuthStore, _setItem } from '@/stores/auth'
import { authApi } from '@/api'
import { t } from '@/i18n'
import { extractError } from '@/utils/errorCodes'
import { message } from '@/utils/discreteApi'

declare const __APP_VERSION__: string
const appVersion = `v${__APP_VERSION__}`

const router = useRouter()
const auth = useAuthStore()
const loading = ref(false)
const showChangePassword = ref(false)
const changingPwd = ref(false)

// FIXED-UECapsLock: 大写锁定状态检测
const capsLockOn = ref(false)
function onCapsLockCheck(e: KeyboardEvent) {
  // getModifierState 是标准 API，能准确反映 CapsLock 物理按键状态
  capsLockOn.value = e.getModifierState && e.getModifierState('CapsLock')
}

const form = reactive({ username: '', password: '' })
const rules = computed(() => ({
  username: { required: true, message: t('login.usernameRequired'), trigger: ['input', 'blur'] },
  // [AUDIT-FIX] 严重级-原代码使用 t('login.password')(请输入密码，实为 placeholder 文案) 当验证消息，
  // 虽然文案恰好合适但语义错误，现改用专用验证消息键 t('login.passwordRequired')
  password: { required: true, message: t('login.passwordRequired'), trigger: ['input', 'blur'] },
}))
const formRef = ref<any>(null)

const changePwdForm = reactive({ old_password: '', new_password: '', confirm_password: '' })
const changePwdRules = computed(() => ({
  // [AUDIT-FIX] 严重级-原代码使用 t('login.oldPassword')(原密码，label) 当验证消息，改用 t('login.oldPasswordRequired')
  old_password: { required: true, message: t('login.oldPasswordRequired'), trigger: ['input', 'blur'] },
  new_password: [
    // [AUDIT-FIX] 严重级-原代码使用 t('login.newPassword')(新密码，label) 当验证消息，改用 t('login.newPasswordRequired')
    { required: true, message: t('login.newPasswordRequired'), trigger: ['input', 'blur'] },
    { min: 8, max: 72, message: t('login.passwordMinLength'), trigger: ['input', 'blur'] },
    { validator: (_rule: any, value: string) => /[a-zA-Z]/.test(value) && /\d/.test(value), message: t('login.passwordLetterAndDigit'), trigger: ['input', 'blur'] },
    { validator: (_rule: any, value: string) => /[!@#$%^&*()_+\-=\[\]{}|;':",.\/<>?`~]/.test(value), message: t('login.passwordNeedSpecial'), trigger: ['input', 'blur'] },
  ],
  confirm_password: [
    // [AUDIT-FIX] 严重级-原代码使用 t('login.confirmPassword')(确认新密码，label) 当验证消息，改用 t('login.confirmPasswordRequired')
    { required: true, message: t('login.confirmPasswordRequired'), trigger: ['input', 'blur'] },
    { validator: (_rule: any, value: string) => value === changePwdForm.new_password, message: t('login.passwordMismatch'), trigger: ['input', 'blur'] },
  ],
}))
const changePwdFormRef = ref<any>(null)

// FIXED-UEStrength: 实时密码规则清单与强度计算
const pwdRules = computed(() => [
  { key: 'length', label: t('login.ruleLength'), met: changePwdForm.new_password.length >= 8 && changePwdForm.new_password.length <= 72 },
  { key: 'letterDigit', label: t('login.ruleLetterDigit'), met: /[a-zA-Z]/.test(changePwdForm.new_password) && /\d/.test(changePwdForm.new_password) },
  { key: 'special', label: t('login.ruleSpecial'), met: /[!@#$%^&*()_+\-=\[\]{}|;':",.\/<>?`~]/.test(changePwdForm.new_password) },
  { key: 'confirm', label: t('login.ruleConfirm'), met: !!changePwdForm.confirm_password && changePwdForm.confirm_password === changePwdForm.new_password },
])
const pwdStrength = computed(() => {
  const metCount = pwdRules.value.filter(r => r.met && r.key !== 'confirm').length
  // 4档：弱/中/较强/强
  if (metCount <= 1) return { score: 1, label: t('login.strengthWeak') }
  if (metCount === 2) return { score: 2, label: t('login.strengthMedium') }
  if (metCount === 3) return { score: 3, label: t('login.strengthGood') }
  return { score: 4, label: t('login.strengthStrong') }
})

// FIX 4: 提取 setup 重定向检查为辅助函数，避免 handleLogin 和 handleChangePassword 逻辑不一致
// FIX 2: 校验 redirect 参数，仅允许相对路径，防止开放重定向
function checkSetupRedirect() {
  const setupCompleted = localStorage.getItem('edgelite_setup_completed') === 'true'
  if (!setupCompleted) {
    router.push('/setup')
    return
  }
  const redirect = router.currentRoute.value.query.redirect
  // FIX 2: 校验 redirect 参数，仅允许以单个 / 开头的相对路径
  const safeRedirect = typeof redirect === 'string' && redirect.startsWith('/') && !redirect.startsWith('//') ? redirect : '/'
  router.push(safeRedirect)
}

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
      // FIX 4: 使用 checkSetupRedirect 辅助函数统一处理重定向逻辑
      checkSetupRedirect()
    }
  } catch (e: any) {
    // FIXED: 原问题-错误分类依赖中文字符串匹配(detail.includes('频繁'))，
    // 现改为基于HTTP status code判断，并使用错误码映射i18n
    const status = e?.response?.status
    if (status === 429) {
      message.warning(extractError(e, t('loginPage.loginFailed')))
    } else if (status === 401) {
      message.error(extractError(e, t('login.invalidCredentials')))
    } else if (status === 403) {
      message.error(extractError(e, t('loginPage.loginFailed')))
    } else if (!status) {
      if (e?.isBusinessError) {
        message.error(e?.message || t('loginPage.loginFailed'))
      } else {
        message.error(t('loginPage.networkError'))
      }
    } else {
      message.error(extractError(e, t('loginPage.loginFailed')))
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
    _setItem('edgelite_mustChangePassword', 'false')
    message.success(t('login.passwordChanged'))
    // FIXED: 原问题-修改密码后未重新获取token，若后端使旧token失效则后续请求401
    // 现在修改密码成功后重新登录获取新token
    try {
      await auth.login(auth.username, changePwdForm.new_password)
    } catch (reLoginError: any) {
      // FIXED: 原问题-重新登录失败时误用passwordChanged(成功语义)作为错误消息
      message.error(extractError(reLoginError, t('login.passwordChangeFailed')))
      router.push('/login')
      return
    }
    // FIX 4: 使用 checkSetupRedirect 辅助函数，检查 setup 向导并校验 redirect
    checkSetupRedirect()
  } catch (e: any) {
    message.error(extractError(e, t('login.passwordChangeFailed')))
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
@media (prefers-reduced-motion: reduce) {
  .bg-shape { animation: none; }
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
  /* [AUDIT-FIX] 严重-2: 暗色模式下硬编码 #333 不可见，改用 naive-ui CSS 变量 */
  color: var(--n-text-color, #333);
  margin: 0 0 8px 0;
}
.login-subtitle {
  font-size: 14px;
  /* [AUDIT-FIX] 严重-2: 暗色模式下硬编码 #999 不可见，改用 naive-ui CSS 变量 */
  color: var(--n-text-color-3, #999);
  margin: 0;
}
.login-footer {
  text-align: center;
  margin-top: 24px;
  padding-top: 16px;
  /* [AUDIT-FIX] 严重-2: 暗色模式下硬编码 #eee 边框不可见，改用 naive-ui CSS 变量 */
  border-top: 1px solid var(--n-border-color, #eee);
}
/* FIXED-UEStrength: 密码强度指示器样式 */
.pwd-strength {
  margin: -8px 0 12px;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
}
.pwd-strength-bar {
  display: flex;
  gap: 4px;
  flex: 1;
  min-width: 120px;
}
.pwd-strength-seg {
  height: 4px;
  flex: 1;
  border-radius: 2px;
  background: #e5e7eb;
  transition: background 0.2s;
}
.pwd-strength-seg.s1 { background: #ff4d4f; }
.pwd-strength-seg.s2 { background: #faad14; }
.pwd-strength-seg.s3 { background: #52c41a; }
.pwd-strength-seg.s4 { background: #52c41a; }
.pwd-rules {
  width: 100%;
  margin-top: 8px;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 4px 12px;
}
.pwd-rule {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: #909399;
  transition: color 0.2s;
}
.pwd-rule.met {
  color: #52c41a;
}
</style>
