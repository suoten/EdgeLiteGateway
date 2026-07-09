/**
 * useChartTheme - provides ECharts theme configuration that adapts to dark/light mode.
 *
 * Returns functions that accept override params and return merged config objects,
 * plus reactive color values for direct use.
 *
 * Also exports the EChartsOption type for use in components.
 */
import { computed, type ComputedRef } from 'vue'
import { useOsTheme } from 'naive-ui'

// Re-export ECharts option type so components can import from here
export type EChartsOption = Record<string, any>

// Color palette for chart series
const LIGHT_AXIS_COLOR = '#999'
const DARK_AXIS_COLOR = '#888'
const LIGHT_SPLIT_LINE_COLOR = '#eee'
const DARK_SPLIT_LINE_COLOR = '#333'

export function useChartTheme(): {
  isDark: ComputedRef<boolean>
  themeVars: ComputedRef<Record<string, any>>
  chartAxisColor: ComputedRef<string>
  chartSplitLineColor: ComputedRef<string>
  chartTextColor: ComputedRef<string>
  chartLegendColor: ComputedRef<string>
  chartTooltipAxis: (overrides?: Record<string, any>) => Record<string, any>
  chartCategoryAxis: (overrides?: Record<string, any>) => Record<string, any>
  chartValueAxis: (overrides?: Record<string, any>) => Record<string, any>
  chartTooltip: (overrides?: Record<string, any>) => Record<string, any>
  chartLegend: (overrides?: Record<string, any>) => Record<string, any>
  chartGrid: (overrides?: Record<string, any>) => Record<string, any>
} {
  const osTheme = useOsTheme()
  const isDark = computed(() => osTheme.value === 'dark')

  const chartAxisColor = computed(() => isDark.value ? DARK_AXIS_COLOR : LIGHT_AXIS_COLOR)
  const chartSplitLineColor = computed(() => isDark.value ? DARK_SPLIT_LINE_COLOR : LIGHT_SPLIT_LINE_COLOR)
  const chartTextColor = computed(() => isDark.value ? 'rgba(255,255,255,0.82)' : 'rgba(0,0,0,0.75)')
  const chartLegendColor = computed(() => chartTextColor.value)

  const themeVars = computed(() => ({
    axisColor: chartAxisColor.value,
    splitLineColor: chartSplitLineColor.value,
    textColor: chartTextColor.value,
    legendColor: chartLegendColor.value,
    tooltipBg: isDark.value ? 'rgba(50,50,50,0.95)' : 'rgba(255,255,255,0.95)',
    tooltipBorder: isDark.value ? '#555' : '#ddd',
    tooltipText: isDark.value ? '#eee' : '#333',
    isDark: isDark.value,
  }))

  function chartTooltipAxis(overrides?: Record<string, any>): Record<string, any> {
    return {
      type: 'axis',
      axisPointer: {
        type: 'cross',
        label: { backgroundColor: isDark.value ? '#333' : '#6a7985' },
      },
      backgroundColor: isDark.value ? 'rgba(50,50,50,0.95)' : 'rgba(255,255,255,0.95)',
      borderColor: isDark.value ? '#555' : '#ddd',
      textStyle: { color: isDark.value ? '#eee' : '#333' },
      ...overrides,
    }
  }

  function chartCategoryAxis(overrides?: Record<string, any>): Record<string, any> {
    return {
      type: 'category',
      axisLine: { lineStyle: { color: chartAxisColor.value } },
      axisLabel: { color: chartTextColor.value },
      axisTick: { lineStyle: { color: chartAxisColor.value } },
      splitLine: { show: false },
      ...overrides,
    }
  }

  function chartValueAxis(overrides?: Record<string, any>): Record<string, any> {
    return {
      type: 'value',
      axisLine: { lineStyle: { color: chartAxisColor.value } },
      axisLabel: { color: chartTextColor.value },
      axisTick: { lineStyle: { color: chartAxisColor.value } },
      splitLine: { lineStyle: { color: chartSplitLineColor.value } },
      ...overrides,
    }
  }

  function chartTooltip(overrides?: Record<string, any>): Record<string, any> {
    return {
      backgroundColor: isDark.value ? 'rgba(50,50,50,0.95)' : 'rgba(255,255,255,0.95)',
      borderColor: isDark.value ? '#555' : '#ddd',
      textStyle: { color: isDark.value ? '#eee' : '#333' },
      ...overrides,
    }
  }

  function chartLegend(overrides?: Record<string, any>): Record<string, any> {
    return {
      textStyle: { color: chartTextColor.value },
      pageTextStyle: { color: chartTextColor.value },
      ...overrides,
    }
  }

  function chartGrid(overrides?: Record<string, any>): Record<string, any> {
    return {
      left: '3%',
      right: '4%',
      bottom: '3%',
      top: '12%',
      containLabel: true,
      ...overrides,
    }
  }

  return {
    isDark,
    themeVars,
    chartAxisColor,
    chartSplitLineColor,
    chartTextColor,
    chartLegendColor,
    chartTooltipAxis,
    chartCategoryAxis,
    chartValueAxis,
    chartTooltip,
    chartLegend,
    chartGrid,
  }
}
