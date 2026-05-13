// FIXED: 原问题-TypeScript循环类型引用，改用interface避免自引用
interface LocaleMessages {
  [key: string]: string | LocaleMessages
}

const messages: LocaleMessages = {}

let currentLocale = 'zh-CN'

export function setupLocale(locale: string, msgs: LocaleMessages) {
  currentLocale = locale
  Object.assign(messages, msgs)
}

export function t(key: string, params?: Record<string, string | number>): string {
  const parts = key.split('.')
  let result: string | LocaleMessages = messages
  for (const part of parts) {
    if (typeof result === 'object' && result !== null && part in result) {
      result = result[part]
    } else {
      return key
    }
  }
  if (typeof result !== 'string') return key
  if (!params) return result
  return Object.entries(params).reduce(
    (s, [k, v]) => s.replace(new RegExp(`\\{${k}\\}`, 'g'), String(v)),
    result,
  )
}

export { type LocaleMessages }
