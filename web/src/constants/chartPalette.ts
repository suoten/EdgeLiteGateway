/**
 * Chart color palette constants for consistent visual styling across charts.
 *
 * CATEGORICAL_PALETTE: General-purpose categorical colors for non-ordinal data
 * SEMANTIC_COLORS: Status/severity colors (success, warning, error, etc.)
 * BRAND_COLORS: Primary brand identity colors
 * CHART_ROLE_COLORS: Role-based chart colors (primary, secondary, accent)
 * DIVERGING_PALETTE: Diverging color scale for heatmaps/ranges
 */

// Categorical palette - 12 distinct colors for data series
export const CATEGORICAL_PALETTE: string[] = [
  '#5470c6',
  '#91cc75',
  '#fac858',
  '#ee6666',
  '#73c0de',
  '#3ba272',
  '#fc8452',
  '#9a60b4',
  '#ea7ccc',
  '#2f9e44',
  '#d6336c',
  '#495057',
]

// Semantic colors for status, severity, etc.
export const SEMANTIC_COLORS = {
  success: '#18a058',
  warning: '#f0a020',
  error: '#d03050',
  info: '#2080f0',
  disabled: '#999999',
  critical: '#d03050',
  major: '#e65100',
  minor: '#f0a020',
  online: '#18a058',
  offline: '#999999',
  collecting: '#2080f0',
  stopped: '#999999',
  error_state: '#d03050',
  degraded: '#f0a020',
  unknown: '#999999',
  pending: '#2080f0',
  inactive: '#909399',
} as const

// Brand colors
export const BRAND_COLORS = {
  primary: '#2080f0',
  primaryHover: '#4098fc',
  primaryPressed: '#1060c9',
  primarySuppl: '#4098fc',
  success: '#18a058',
  warning: '#f0a020',
  error: '#d03050',
  info: '#2080f0',
  indigo: '#5470c6',
  panelBase: 'rgba(255,255,255,0.85)',
  panelBg: 'rgba(255,255,255,0.85)',
  panelBorder: '#efeff5',
} as const

// Chart role colors - for assigning distinct visual roles in multi-series charts
export const CHART_ROLE_COLORS = {
  primary: '#5470c6',
  secondary: '#91cc75',
  tertiary: '#fac858',
  quaternary: '#ee6666',
  accent1: '#73c0de',
  accent2: '#3ba272',
  accent3: '#fc8452',
  accent4: '#9a60b4',
  highlight: '#ea7ccc',
  muted: '#999999',
  anomaly: '#ee6666',
  trend: '#91cc75',
  threshold: '#fac858',
  baseline: '#73c0de',
} as const

// Diverging palette - for heatmaps and ranges (low to high)
export const DIVERGING_PALETTE: string[] = [
  '#d6336c',
  '#e64980',
  '#f06595',
  '#cc5de8',
  '#845ef7',
  '#5c7cfa',
  '#339af0',
  '#22b8cf',
  '#20c997',
  '#51cf66',
]
