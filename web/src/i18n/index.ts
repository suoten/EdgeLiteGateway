import { ref, type Ref } from 'vue'

interface LocaleMessages {
  [key: string]: string | LocaleMessages
}

const localeMessages: Record<string, LocaleMessages> = {}
const currentLocale: Ref<string> = ref('zh-CN')

export function setupLocale(locale: string, msgs: LocaleMessages) {
  localeMessages[locale] = msgs
  if (!localeMessages[currentLocale.value]) {
    currentLocale.value = locale
  }
}

export function setLocale(locale: string) {
  if (locale !== 'zh-CN' && locale !== 'en-US') return
  if (localeMessages[locale]) {
    currentLocale.value = locale
    localStorage.setItem('edgelite_locale', locale)
  }
}

export function getLocale(): string {
  return currentLocale.value
}

export function useCurrentLocale(): Ref<string> {
  return currentLocale
}

export function getAvailableLocales(): string[] {
  return Object.keys(localeMessages)
}

export function initLocale() {
  const saved = localStorage.getItem('edgelite_locale')
  if (saved && localeMessages[saved]) {
    currentLocale.value = saved
  }
}

export function t(key: string, params?: Record<string, string | number> | string): string {
  const fallbackStr = typeof params === 'string' ? params : undefined
  const actualParams = typeof params === 'object' ? params : undefined
  const parts = key.split('.')
  let result: string | LocaleMessages = localeMessages[currentLocale.value] || {}
  if (typeof result !== 'object' || result === null || Object.keys(result).length === 0) {
    result = localeMessages['zh-CN'] || {}
  }
  for (const part of parts) {
    if (typeof result === 'object' && result !== null && part in result) {
      result = result[part]
    } else {
      const fallback = _resolveFallback(parts, localeMessages['en-US'] || {})
      if (fallback === undefined) return fallbackStr !== undefined ? fallbackStr : key
      result = fallback
      break
    }
  }
  if (typeof result !== 'string') return fallbackStr !== undefined ? fallbackStr : key
  if (!actualParams) return result
  return Object.entries(actualParams).reduce(
    (s, [k, v]) => s.replace(new RegExp(`\\{${k}\\}`, 'g'), String(v)),
    result,
  )
}

function _resolveFallback(parts: string[], messages: LocaleMessages): string | undefined {
  let result: string | LocaleMessages = messages
  for (const part of parts) {
    if (typeof result === 'object' && result !== null && part in result) {
      result = result[part]
    } else {
      return undefined
    }
  }
  return typeof result === 'string' ? result : undefined
}

export { type LocaleMessages }
