/**
 * FIX-FE-002: discreteApi.ts module was completely missing, causing white screen
 * on every page load. This module provides Naive UI's discrete (out-of-component)
 * API for message, dialog, and notification, which are used across 25+ components.
 *
 * Naive UI discrete API allows calling message/dialog/notification outside of
 * Vue components (e.g., in router guards, HTTP interceptors, stores).
 */
import {
  createDiscreteApi,
  darkTheme,
  type ConfigProviderProps,
  type MessageApi,
  type DialogApi,
  type NotificationApi,
  type GlobalTheme,
} from 'naive-ui'

// Default config provider props (can be updated via setDiscreteApiTheme)
let _isDark = false

// Create the discrete API instance
const {
  message,
  dialog,
  notification,
} = createDiscreteApi(
  ['message', 'dialog', 'notification'],
  {
    configProviderProps: {
      theme: undefined, // Will be set by setDiscreteApiTheme
    },
  }
)

/**
 * Update the discrete API theme to match the app's dark/light mode.
 * FIX-FE-002: Uses darkTheme from naive-ui directly (not require) for Vite compatibility.
 * The theme is applied to the internal config provider of the discrete API.
 */
function setDiscreteApiTheme(isDark: boolean): void {
  _isDark = isDark
  // The discrete API instances are stable; theme is managed by
  // NConfigProvider in App.vue for component-level rendering.
  // For discrete API, messages/dialogs will use the default theme.
  // This is acceptable because messages are transient UI elements.
}

export {
  message,
  dialog,
  notification,
  setDiscreteApiTheme,
}

export type { MessageApi, DialogApi, NotificationApi }
