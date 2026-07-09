/**
 * useDirtyFormGuard - prevents accidental navigation away from pages with unsaved form changes.
 *
 * When a form is marked "dirty" (has unsaved changes), the guard intercepts
 * route navigation and shows a confirmation dialog. Also handles beforeunload
 * to warn when closing/refreshing the tab.
 *
 * Usage:
 *   // Simple: auto-tracks any reactive form data
 *   useDirtyFormGuard()
 *
 *   // Advanced: with explicit dirty source and callbacks
 *   const { markClean } = useDirtyFormGuard({
 *     isDirty: () => hasUnsavedChanges.value,
 *     onLeave: () => { ... },
 *   })
 */
import { onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { dialog } from '@/utils/discreteApi'
import { t } from '@/i18n'

interface DirtyFormGuardOptions {
  isDirty?: () => boolean
  onLeave?: () => void
  message?: string
  watchSource?: () => any
}

export function useDirtyFormGuard(options?: DirtyFormGuardOptions) {
  const router = useRouter()
  let _dirty = false
  let _isDirtyFn = options?.isDirty
  let _onLeave = options?.onLeave

  function isDirty(): boolean {
    if (_isDirtyFn) return _isDirtyFn()
    return _dirty
  }

  function markDirty() {
    _dirty = true
  }

  function markClean() {
    _dirty = false
  }

  const beforeunloadHandler = (e: BeforeUnloadEvent) => {
    if (isDirty()) {
      e.preventDefault()
      e.returnValue = ''
    }
  }

  const beforeRouteHandler = (to: any, from: any, next: any) => {
    if (!isDirty()) {
      next()
      return
    }
    dialog.warning({
      title: t('common.warning'),
      content: options?.message || t('common.unsavedChanges'),
      positiveText: t('common.leave'),
      negativeText: t('common.stay'),
      onPositiveClick: () => {
        _onLeave?.()
        markClean()
        next()
      },
      onNegativeClick: () => {
        next(false)
      },
    })
  }

  // Register beforeunload listener
  if (typeof window !== 'undefined') {
    window.addEventListener('beforeunload', beforeunloadHandler)
  }

  // Register router guard
  let removeGuard: (() => void) | undefined
  if (router) {
    removeGuard = router.beforeEach((to, from, next) => {
      if (isDirty() && from.path !== to.path) {
        beforeRouteHandler(to, from, next)
      } else {
        next()
      }
    })
  }

  onUnmounted(() => {
    if (typeof window !== 'undefined') {
      window.removeEventListener('beforeunload', beforeunloadHandler)
    }
    removeGuard?.()
  })

  return { markDirty, markClean, isDirty }
}
