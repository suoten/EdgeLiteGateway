/**
 * useBreakpoints - reactive breakpoint detection for responsive layouts.
 *
 * Provides reactive flags for common screen sizes.
 * Supports the required resolutions: 1920, 1366, 1280.
 */
import { ref, onMounted, onUnmounted, computed, type Ref, type ComputedRef } from 'vue'

// Breakpoint thresholds (px)
const BP_SM = 640   // mobile
const BP_MD = 768   // small tablet
const BP_LG = 1024  // tablet / small desktop
const BP_XL = 1280  // desktop (1280px target)
const BP_2XL = 1536 // large desktop (1366px target)
const BP_3XL = 1920 // full HD (1920px target)

export function useBreakpoints(): {
  width: Ref<number>
  isMobile: ComputedRef<boolean>
  isTablet: ComputedRef<boolean>
  isDesktop: ComputedRef<boolean>
  isLargeDesktop: ComputedRef<boolean>
  isExtraLargeDesktop: ComputedRef<boolean>
  isCompact: ComputedRef<boolean>
} {
  const width = ref(typeof window !== 'undefined' ? window.innerWidth : 1920)

  const update = () => {
    width.value = window.innerWidth
  }

  onMounted(() => {
    window.addEventListener('resize', update, { passive: true })
  })

  onUnmounted(() => {
    window.removeEventListener('resize', update)
  })

  const isMobile = computed(() => width.value < BP_MD)
  const isTablet = computed(() => width.value >= BP_MD && width.value < BP_LG)
  const isDesktop = computed(() => width.value >= BP_LG)
  const isLargeDesktop = computed(() => width.value >= BP_2XL)
  const isExtraLargeDesktop = computed(() => width.value >= BP_3XL)
  // Compact: screens below 1280px that need collapsed sidebar
  const isCompact = computed(() => width.value < BP_XL)

  return {
    width,
    isMobile,
    isTablet,
    isDesktop,
    isLargeDesktop,
    isExtraLargeDesktop,
    isCompact,
  }
}
