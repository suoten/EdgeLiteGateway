/**
 * usePageVisibility - tracks whether the page is currently visible to the user.
 *
 * Uses the Page Visibility API to detect tab switching / minimization.
 * Components use this to pause polling, reduce WebSocket traffic, etc.
 */
import { ref, onMounted, onUnmounted, type Ref } from 'vue'

export function usePageVisibility(): { isVisible: Ref<boolean> } {
  const isVisible = ref(!document.hidden)

  const handler = () => {
    isVisible.value = !document.hidden
  }

  onMounted(() => {
    document.addEventListener('visibilitychange', handler)
  })

  onUnmounted(() => {
    document.removeEventListener('visibilitychange', handler)
  })

  return { isVisible }
}
