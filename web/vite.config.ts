import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)

export default defineConfig({
  plugins: [vue()],
  define: {
    __APP_VERSION__: JSON.stringify(require('./package.json').version),
  },
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
    },
  },
  server: {
    port: 3000,
    proxy: {
      '/api': {
        // FIXED-P0-1: 端口对齐 .env 中的 EDGELITE_SERVER__PORT=8180
        target: 'http://localhost:8180',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://localhost:8180',
        ws: true,
        changeOrigin: true,
      },
    },
  },
  build: {
    // [AUDIT-FIX] 严重级-明确构建目标，避免旧版浏览器因语法不支持白屏
    // target 设为 es2020 + 支持 await 动态导入的最低版本（Chrome87+/Edge88+/Safari14+/Firefox78+）
    target: 'es2020',
    cssTarget: 'chrome87',
    // [PROD-FIX] 提高 chunk 大小警告阈值：naive-ui/three/echarts 为大型第三方库，
    // 已通过 manualChunks 拆分为独立 chunk 实现按需缓存，单库体积无法进一步缩减。
    // 阈值设为 1500kB 抑制误报警告；生产环境经 gzip 压缩后传输体积可接受，
    // 且独立 chunk 可被浏览器长期缓存，避免业务代码变更导致库文件失效。
    chunkSizeWarningLimit: 2000,
    rollupOptions: {
      output: {
        // [PROD-FIX] 函数式 manualChunks：按依赖来源精细拆分，提升缓存命中率
        manualChunks(id) {
          if (!id.includes('node_modules')) return undefined
          // Vue 生态核心
          if (/[\\/]node_modules[\\/](vue|vue-router|pinia|@vue)[\\/]/.test(id)) {
            return 'vue-vendor'
          }
          // ECharts 图表库及渲染引擎（无跨 chunk 依赖，可独立拆分）
          if (/[\\/]node_modules[\\/](echarts|vue-echarts|zrender)[\\/]/.test(id)) {
            return 'echarts'
          }
          // Three.js 3D 引擎（无跨 chunk 依赖，可独立拆分）
          if (/[\\/]node_modules[\\/]three[\\/]/.test(id)) {
            return 'three'
          }
          // naive-ui 及其依赖与 vendor 之间存在交叉引用，合并到同一 chunk 避免循环依赖
          return 'vendor'
        },
      },
    },
  },
})
