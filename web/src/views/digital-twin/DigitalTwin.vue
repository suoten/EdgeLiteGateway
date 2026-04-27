<template>
  <div class="digital-twin-page">
    <n-card title="3D 数字孪生">
      <template #header-extra>
        <n-space>
          <n-select v-model:value="selectedScene" :options="sceneOptions" style="width: 200px" placeholder="选择场景" />
          <n-button type="primary" @click="loadScene">加载场景</n-button>
          <n-button @click="toggleAutoRotate">{{ autoRotate ? '停止旋转' : '自动旋转' }}</n-button>
        </n-space>
      </template>
      <div ref="containerRef" class="three-container"></div>
      <n-grid :cols="4" :x-gap="12" style="margin-top: 16px">
        <n-gi v-for="device in deviceList" :key="device.device_id">
          <n-card size="small" :title="device.name">
            <n-tag :type="device.status === 'online' ? 'success' : 'error'" size="small">
              {{ device.status === 'online' ? '在线' : '离线' }}
            </n-tag>
            <div v-for="point in (device.points || []).slice(0, 3)" :key="point.name" style="margin-top: 4px; font-size: 12px">
              {{ point.name }}: {{ point.value ?? '-' }} {{ point.unit || '' }}
            </div>
          </n-card>
        </n-gi>
      </n-grid>
    </n-card>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { NCard, NButton, NSpace, NSelect, NGrid, NGi, NTag } from 'naive-ui'

const containerRef = ref<HTMLElement | null>(null)
const selectedScene = ref('factory')
const autoRotate = ref(true)
const deviceList = ref<any[]>([])

const sceneOptions = [
  { label: '工厂车间', value: 'factory' },
  { label: '智慧园区', value: 'park' },
  { label: '能源站', value: 'energy' },
]

let renderer: any = null
let animationId: number | null = null

async function loadScene() {
  if (!containerRef.value) return
  try {
    const THREE = await import('three')
    const { OrbitControls } = await import('three/examples/jsm/controls/OrbitControls.js')

    if (renderer) {
      renderer.dispose()
      if (animationId) cancelAnimationFrame(animationId)
    }

    const width = containerRef.value.clientWidth
    const height = 500

    const scene = new THREE.Scene()
    scene.background = new THREE.Color(0x1a1a2e)

    const camera = new THREE.PerspectiveCamera(75, width / height, 0.1, 1000)
    camera.position.set(5, 5, 5)

    renderer = new THREE.WebGLRenderer({ antialias: true })
    renderer.setSize(width, height)
    containerRef.value.innerHTML = ''
    containerRef.value.appendChild(renderer.domElement)

    const controls = new OrbitControls(camera, renderer.domElement)
    controls.enableDamping = true
    controls.autoRotate = autoRotate.value

    const ambientLight = new THREE.AmbientLight(0x404040, 2)
    scene.add(ambientLight)
    const directionalLight = new THREE.DirectionalLight(0xffffff, 1)
    directionalLight.position.set(5, 10, 7)
    scene.add(directionalLight)

    const gridHelper = new THREE.GridHelper(20, 20, 0x444444, 0x222222)
    scene.add(gridHelper)

    const geometry = new THREE.BoxGeometry(1, 1, 1)
    const material = new THREE.MeshPhongMaterial({ color: 0x00d4aa, transparent: true, opacity: 0.8 })
    const cube = new THREE.Mesh(geometry, material)
    cube.position.set(0, 0.5, 0)
    scene.add(cube)

    const animate = () => {
      animationId = requestAnimationFrame(animate)
      controls.update()
      renderer.render(scene, camera)
    }
    animate()
  } catch (e) {
    console.error('3D场景加载失败:', e)
  }
}

function toggleAutoRotate() {
  autoRotate.value = !autoRotate.value
}

onMounted(() => {
  loadScene()
})

onUnmounted(() => {
  if (renderer) renderer.dispose()
  if (animationId) cancelAnimationFrame(animationId)
})
</script>

<style scoped>
.digital-twin-page { padding: 16px; }
.three-container { width: 100%; height: 500px; border-radius: 8px; overflow: hidden; }
</style>
