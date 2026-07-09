/**
 * DateTime utility functions
 * Provides consistent date/time formatting across the application.
 */

/**
 * Format a timestamp (ISO string, epoch ms, or Date) into a human-readable local datetime string.
 *
 * @param ts - ISO string, epoch milliseconds number, or Date object
 * @returns Formatted string like "2024-01-15 14:30:25" or "-" if input is falsy
 */
export function formatDateTime(ts: string | number | Date | null | undefined): string {
  if (!ts) return '-'
  try {
    const d = typeof ts === 'number' ? new Date(ts) : new Date(ts)
    if (isNaN(d.getTime())) return '-'
    const pad = (n: number) => String(n).padStart(2, '0')
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  } catch {
    return '-'
  }
}

/**
 * Format a timestamp to date only (without time).
 */
export function formatDate(ts: string | number | Date | null | undefined): string {
  if (!ts) return '-'
  try {
    const d = typeof ts === 'number' ? new Date(ts) : new Date(ts)
    if (isNaN(d.getTime())) return '-'
    const pad = (n: number) => String(n).padStart(2, '0')
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
  } catch {
    return '-'
  }
}

/**
 * Format a relative time from now (e.g., "3 minutes ago").
 */
export function formatRelativeTime(ts: string | number | Date | null | undefined): string {
  if (!ts) return '-'
  try {
    const d = typeof ts === 'number' ? new Date(ts) : new Date(ts)
    if (isNaN(d.getTime())) return '-'
    const diff = Date.now() - d.getTime()
    const seconds = Math.floor(diff / 1000)
    if (seconds < 60) return `${seconds}s ago`
    const minutes = Math.floor(seconds / 60)
    if (minutes < 60) return `${minutes}m ago`
    const hours = Math.floor(minutes / 60)
    if (hours < 24) return `${hours}h ago`
    const days = Math.floor(hours / 24)
    if (days < 30) return `${days}d ago`
    return formatDateTime(ts)
  } catch {
    return '-'
  }
}
