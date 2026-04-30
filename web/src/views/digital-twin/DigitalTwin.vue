<template>
  <div class="digital-twin-page">
    <n-card title="3D 数字孪生">
      <template #header-extra>
        <n-space>
          <n-select v-model:value="selectedScene" :options="sceneOptions" style="width: 200px" placeholder="选择场景" />
          <n-button type="primary" @click="rebuildScene">刷新场景</n-button>
          <n-button @click="toggleAutoRotate">{{ autoRotate ? '停止旋转' : '自动旋转' }}</n-button>
          <n-switch v-model:value="showLabels">
            <template #checked>标签</template>
            <template #unchecked>标签</template>
          </n-switch>
        </n-space>
      </template>

      <div class="twin-wrapper">
        <div ref="containerRef" class="three-container" @click="onCanvasClick"></div>

        <div v-if="selectedDevice" class="device-detail-panel">
          <div class="panel-header">
            <span class="panel-title">{{ selectedDevice.name }}</span>
            <n-button text size="small" @click="selectedDevice = null">✕</n-button>
          </div>
          <div class="panel-body">
            <div class="panel-row">
              <span class="panel-label">设备ID</span>
              <span class="panel-value">{{ selectedDevice.device_id }}</span>
            </div>
            <div class="panel-row">
              <span class="panel-label">协议</span>
              <span class="panel-value">{{ protocolLabel[selectedDevice.protocol] || selectedDevice.protocol }}</span>
            </div>
            <div class="panel-row">
              <span class="panel-label">状态</span>
              <n-tag :type="selectedDevice.status === 'online' ? 'success' : selectedDevice.status === 'offline' ? 'error' : 'default'" size="small">
                {{ deviceStatusLabel[selectedDevice.status] || selectedDevice.status }}
              </n-tag>
            </div>
            <n-divider style="margin: 8px 0" />
            <div class="panel-row" v-for="pt in (selectedDevice.points || []).slice(0, 6)" :key="pt.name">
              <span class="panel-label">{{ pt.name }}</span>
              <span class="panel-value">{{ pointValues[selectedDevice.device_id]?.[pt.name]?.value ?? '-' }} {{ pt.unit || '' }}</span>
            </div>
            <n-button text type="primary" size="small" style="margin-top: 8px" @click="goToDeviceDetail">查看详情 →</n-button>
          </div>
        </div>
      </div>

      <n-grid :cols="4" :x-gap="12" style="margin-top: 16px">
        <n-gi v-for="device in deviceList" :key="device.device_id">
          <n-card
            size="small"
            :class="['device-card', { 'device-card--selected': selectedDevice?.device_id === device.device_id }]"
            @click="selectDeviceFromCard(device)"
            hoverable
          >
            <div style="display: flex; align-items: center; justify-content: space-between">
              <span style="font-weight: 600; font-size: 13px">{{ device.name }}</span>
              <n-tag :type="device.status === 'online' ? 'success' : device.status === 'offline' ? 'error' : 'default'" size="small">
                {{ deviceStatusLabel[device.status] || device.status }}
              </n-tag>
            </div>
            <div style="font-size: 11px; color: var(--n-text-color-3); margin-top: 2px">{{ protocolLabel[device.protocol] || device.protocol }}</div>
            <div v-for="point in (device.points || []).slice(0, 2)" :key="point.name" style="margin-top: 4px; font-size: 12px">
              {{ point.name }}: <strong>{{ pointValues[device.device_id]?.[point.name]?.value ?? '-' }}</strong> {{ point.unit || '' }}
            </div>
          </n-card>
        </n-gi>
      </n-grid>
    </n-card>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch } from 'vue'
import { useRouter } from 'vue-router'
import { NCard, NButton, NSpace, NSelect, NGrid, NGi, NTag, NSwitch, NDivider } from 'naive-ui'
import { deviceApi } from '@/api'
import { deviceStatusLabel, deviceStatusColor } from '@/utils/enumLabels'

const router = useRouter()
const containerRef = ref<HTMLElement | null>(null)
const selectedScene = ref('factory')
const autoRotate = ref(true)
const showLabels = ref(true)
const deviceList = ref<any[]>([])
const selectedDevice = ref<any>(null)
const pointValues = ref<Record<string, Record<string, any>>>({})

const protocolLabel: Record<string, string> = {
  modbus_tcp: 'Modbus TCP', opcua: 'OPC-UA', mqtt: 'MQTT', http: 'HTTP',
  simulator: '模拟器', video: '视频', s7: 'S7', mc: 'MC', fins: 'FINS',
  allen_bradley: 'AB', opc_da: 'OPC DA', fanuc: 'FANUC', mtconnect: 'MTConnect',
  toledo: 'Toledo', bacnet: 'BACnet', serial_port: '串口', database_source: '数据库',
  barcode_scanner: '扫码枪', modbus_rtu: 'Modbus RTU',
}

const sceneOptions = [
  { label: '工厂车间', value: 'factory' },
  { label: '智慧园区', value: 'park' },
  { label: '能源站', value: 'energy' },
]

const protocolColors: Record<string, number> = {
  modbus_tcp: 0x4fc3f7, modbus_rtu: 0x4fc3f7, opcua: 0x81c784,
  mqtt: 0xffb74d, http: 0xba68c8, simulator: 0x90a4ae,
  video: 0xef5350, s7: 0x64b5f6, fins: 0x4db6ac,
}

let renderer: any = null
let animationId: number | null = null
let controls: any = null
let scene: any = null
let camera: any = null
let resizeHandler: (() => void) | null = null
let deviceMeshes: Map<string, any> = new Map()
let labelSprites: Map<string, any> = new Map()
let raycaster: any = null
let mouse: any = null
let refreshTimer: any = null

function getDeviceColor(device: any): number {
  if (device.status === 'offline') return 0xe53935
  if (device.status === 'unknown') return 0x757575
  return protocolColors[device.protocol] || 0x00d4aa
}

function getDeviceGeometry(protocol: string, THREE: any) {
  switch (protocol) {
    case 'modbus_tcp': case 'modbus_rtu': return new THREE.CylinderGeometry(0.4, 0.4, 0.8, 8)
    case 'opcua': return new THREE.BoxGeometry(0.8, 0.8, 0.8)
    case 'mqtt': return new THREE.SphereGeometry(0.45, 16, 16)
    case 'video': return new THREE.ConeGeometry(0.4, 0.8, 6)
    case 'simulator': return new THREE.OctahedronGeometry(0.45)
    default: return new THREE.BoxGeometry(0.7, 0.5, 0.7)
  }
}

function createLabelSprite(text: string, THREE: any) {
  const canvas = document.createElement('canvas')
  canvas.width = 256
  canvas.height = 64
  const ctx = canvas.getContext('2d')!
  ctx.fillStyle = 'rgba(0,0,0,0.6)'
  ctx.roundRect(0, 0, 256, 64, 8)
  ctx.fill()
  ctx.fillStyle = '#ffffff'
  ctx.font = 'bold 22px sans-serif'
  ctx.textAlign = 'center'
  ctx.textBaseline = 'middle'
  ctx.fillText(text, 128, 32)
  const texture = new THREE.CanvasTexture(canvas)
  texture.needsUpdate = true
  const material = new THREE.SpriteMaterial({ map: texture, transparent: true, depthTest: false })
  const sprite = new THREE.Sprite(material)
  sprite.scale.set(2, 0.5, 1)
  return sprite
}

function getDevicePositions(count: number, sceneType: string) {
  const positions: { x: number; y: number; z: number }[] = []
  if (sceneType === 'factory') {
    const cols = Math.ceil(Math.sqrt(count))
    const spacing = 2.5
    for (let i = 0; i < count; i++) {
      const row = Math.floor(i / cols)
      const col = i % cols
      positions.push({ x: (col - cols / 2 + 0.5) * spacing, y: 0.5, z: (row - Math.ceil(count / cols) / 2 + 0.5) * spacing })
    }
  } else if (sceneType === 'park') {
    const radius = Math.max(count * 0.6, 3)
    for (let i = 0; i < count; i++) {
      const angle = (i / count) * Math.PI * 2
      positions.push({ x: Math.cos(angle) * radius, y: 0.5, z: Math.sin(angle) * radius })
    }
  } else {
    const cols = Math.ceil(count / 2)
    const spacing = 2.5
    for (let i = 0; i < count; i++) {
      const row = Math.floor(i / cols)
      const col = i % cols
      positions.push({ x: (col - cols / 2 + 0.5) * spacing, y: row * 2 + 0.5, z: 0 })
    }
  }
  return positions
}

async function loadScene() {
  if (!containerRef.value) return
  try {
    const THREE = await import('three')
    const { OrbitControls } = await import('three/examples/jsm/controls/OrbitControls.js')

    cleanupScene()

    const width = containerRef.value.clientWidth
    const height = 500

    const newScene = new THREE.Scene()
    newScene.background = new THREE.Color(0x0d1117)
    newScene.fog = new THREE.Fog(0x0d1117, 15, 40)
    scene = newScene

    camera = new THREE.PerspectiveCamera(60, width / height, 0.1, 1000)
    camera.position.set(8, 8, 8)

    renderer = new THREE.WebGLRenderer({ antialias: true })
    renderer.setSize(width, height)
    renderer.setPixelRatio(window.devicePixelRatio)
    renderer.shadowMap.enabled = true
    containerRef.value.innerHTML = ''
    containerRef.value.appendChild(renderer.domElement)

    const orbitControls = new OrbitControls(camera, renderer.domElement)
    orbitControls.enableDamping = true
    orbitControls.autoRotate = autoRotate.value
    orbitControls.autoRotateSpeed = 0.5
    orbitControls.maxPolarAngle = Math.PI / 2.1
    controls = orbitControls

    const ambientLight = new THREE.AmbientLight(0x404060, 3)
    scene.add(ambientLight)
    const directionalLight = new THREE.DirectionalLight(0xffffff, 1.5)
    directionalLight.position.set(10, 15, 10)
    directionalLight.castShadow = true
    scene.add(directionalLight)
    const pointLight = new THREE.PointLight(0x4fc3f7, 1, 30)
    pointLight.position.set(0, 8, 0)
    scene.add(pointLight)

    const gridHelper = new THREE.GridHelper(30, 30, 0x1a3a4a, 0x0d1f2d)
    scene.add(gridHelper)

    const groundGeo = new THREE.PlaneGeometry(30, 30)
    const groundMat = new THREE.MeshPhongMaterial({ color: 0x0a1628, transparent: true, opacity: 0.8 })
    const ground = new THREE.Mesh(groundGeo, groundMat)
    ground.rotation.x = -Math.PI / 2
    ground.position.y = -0.01
    ground.receiveShadow = true
    scene.add(ground)

    raycaster = new THREE.Raycaster()
    mouse = new THREE.Vector2()

    addDevicesToScene(THREE)

    const onResize = () => {
      if (!containerRef.value || !camera || !renderer) return
      const w = containerRef.value.clientWidth
      camera.aspect = w / height
      camera.updateProjectionMatrix()
      renderer.setSize(w, height)
    }
    resizeHandler = onResize
    window.addEventListener('resize', onResize)

    const animate = () => {
      animationId = requestAnimationFrame(animate)
      controls.update()
      deviceMeshes.forEach((mesh) => {
        if (mesh.userData.floating) {
          mesh.position.y = mesh.userData.baseY + Math.sin(Date.now() * 0.002 + mesh.userData.floatOffset) * 0.05
        }
      })
      renderer.render(scene, camera)
    }
    animate()
  } catch (e) {
    console.error('3D场景加载失败:', e)
  }
}

function addDevicesToScene(THREE: any) {
  if (!scene) return
  deviceMeshes.forEach((mesh) => {
    scene.remove(mesh)
    if (mesh.geometry) mesh.geometry.dispose()
    if (mesh.material) mesh.material.dispose()
  })
  labelSprites.forEach((sprite) => {
    scene.remove(sprite)
    if (sprite.material) { sprite.material.map?.dispose(); sprite.material.dispose() }
  })
  deviceMeshes.clear()
  labelSprites.clear()

  const devices = deviceList.value
  if (!devices.length) return

  const positions = getDevicePositions(devices.length, selectedScene.value)

  devices.forEach((device, i) => {
    const pos = positions[i] || { x: 0, y: 0.5, z: 0 }
    const geometry = getDeviceGeometry(device.protocol, THREE)
    const color = getDeviceColor(device)
    const material = new THREE.MeshPhongMaterial({
      color,
      transparent: true,
      opacity: device.status === 'offline' ? 0.5 : 0.9,
      emissive: device.status === 'online' ? color : 0x000000,
      emissiveIntensity: device.status === 'online' ? 0.15 : 0,
    })
    const mesh = new THREE.Mesh(geometry, material)
    mesh.position.set(pos.x, pos.y, pos.z)
    mesh.castShadow = true
    mesh.userData = {
      deviceId: device.device_id,
      deviceName: device.name,
      baseY: pos.y,
      floating: device.status === 'online',
      floatOffset: i * 0.5,
    }
    scene.add(mesh)
    deviceMeshes.set(device.device_id, mesh)

    if (showLabels.value) {
      const label = createLabelSprite(device.name, THREE)
      label.position.set(pos.x, pos.y + 1.0, pos.z)
      scene.add(label)
      labelSprites.set(device.device_id, label)
    }

    if (device.status === 'online') {
      const ringGeo = new THREE.RingGeometry(0.6, 0.7, 32)
      const ringMat = new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.3, side: THREE.DoubleSide })
      const ring = new THREE.Mesh(ringGeo, ringMat)
      ring.rotation.x = -Math.PI / 2
      ring.position.set(pos.x, 0.02, pos.z)
      scene.add(ring)
    }
  })

  const maxDist = Math.sqrt(devices.length) * 2.5 + 3
  if (camera) {
    camera.position.set(maxDist, maxDist * 0.8, maxDist)
    camera.lookAt(0, 0, 0)
  }
}

function onCanvasClick(event: MouseEvent) {
  if (!containerRef.value || !raycaster || !camera) return
  const rect = containerRef.value.getBoundingClientRect()
  mouse.x = ((event.clientX - rect.left) / rect.width) * 2 - 1
  mouse.y = -((event.clientY - rect.top) / rect.height) * 2 + 1
  raycaster.setFromCamera(mouse, camera)
  const meshes = Array.from(deviceMeshes.values())
  const intersects = raycaster.intersectObjects(meshes)
  if (intersects.length > 0) {
    const deviceId = intersects[0].object.userData.deviceId
    const device = deviceList.value.find(d => d.device_id === deviceId)
    if (device) {
      selectedDevice.value = device
      fetchDevicePoints(device.device_id)
      highlightDevice(deviceId)
    }
  }
}

function highlightDevice(deviceId: string) {
  deviceMeshes.forEach((mesh, id) => {
    if (id === deviceId) {
      mesh.scale.set(1.3, 1.3, 1.3)
      mesh.material.emissiveIntensity = 0.5
    } else {
      mesh.scale.set(1, 1, 1)
      const device = deviceList.value.find(d => d.device_id === id)
      mesh.material.emissiveIntensity = device?.status === 'online' ? 0.15 : 0
    }
  })
}

function selectDeviceFromCard(device: any) {
  selectedDevice.value = device
  fetchDevicePoints(device.device_id)
  highlightDevice(device.device_id)
  if (camera && controls) {
    const mesh = deviceMeshes.get(device.device_id)
    if (mesh) {
      const pos = mesh.position
      camera.position.set(pos.x + 4, pos.y + 4, pos.z + 4)
      controls.target.set(pos.x, pos.y, pos.z)
      controls.update()
    }
  }
}

function goToDeviceDetail() {
  if (selectedDevice.value) {
    router.push(`/devices/${selectedDevice.value.device_id}`)
  }
}

function toggleAutoRotate() {
  autoRotate.value = !autoRotate.value
  if (controls) controls.autoRotate = autoRotate.value
}

function rebuildScene() {
  fetchDevices().then(() => loadScene())
}

watch(showLabels, () => {
  loadScene()
})

watch(selectedScene, () => {
  loadScene()
})

async function fetchDevices() {
  try {
    const data = await deviceApi.list({ page: 1, size: 100 })
    deviceList.value = data?.data ?? []
  } catch { /* ignore */ }
}

async function fetchDevicePoints(deviceId: string) {
  try {
    const data = await deviceApi.getPoints(deviceId)
    pointValues.value = { ...pointValues.value, [deviceId]: data || {} }
  } catch { /* ignore */ }
}

async function refreshAllPoints() {
  for (const device of deviceList.value) {
    if (device.status === 'online') {
      await fetchDevicePoints(device.device_id)
    }
  }
}

function cleanupScene() {
  if (animationId) cancelAnimationFrame(animationId)
  if (scene) {
    scene.traverse((obj: any) => {
      if (obj.geometry) obj.geometry.dispose()
      if (obj.material) {
        if (obj.material.map) obj.material.map.dispose()
        if (Array.isArray(obj.material)) obj.material.forEach((m: any) => m.dispose())
        else obj.material.dispose()
      }
    })
  }
  if (renderer) {
    renderer.dispose()
    renderer.forceContextLoss()
  }
  deviceMeshes.clear()
  labelSprites.clear()
}

onMounted(async () => {
  await fetchDevices()
  loadScene()
  refreshTimer = setInterval(refreshAllPoints, 10000)
})

onUnmounted(() => {
  if (resizeHandler) {
    window.removeEventListener('resize', resizeHandler)
    resizeHandler = null
  }
  cleanupScene()
  if (controls) controls.dispose()
  if (refreshTimer) clearInterval(refreshTimer)
})
</script>

<style scoped>
.digital-twin-page { padding: 16px; }

.twin-wrapper { position: relative; }

.three-container {
  width: 100%;
  height: 500px;
  border-radius: 8px;
  overflow: hidden;
  cursor: pointer;
}

.device-detail-panel {
  position: absolute;
  top: 12px;
  right: 12px;
  width: 260px;
  background: rgba(13, 17, 23, 0.92);
  border: 1px solid rgba(79, 195, 247, 0.3);
  border-radius: 8px;
  padding: 12px;
  color: #e0e0e0;
  backdrop-filter: blur(8px);
  z-index: 10;
}

.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.panel-title {
  font-weight: 600;
  font-size: 15px;
  color: #4fc3f7;
}

.panel-body { font-size: 13px; }

.panel-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 3px 0;
}

.panel-label { color: #90a4ae; }

.panel-value { color: #e0e0e0; font-weight: 500; }

.device-card {
  cursor: pointer;
  transition: border-color 0.2s;
}

.device-card--selected {
  border-color: var(--n-primary-color) !important;
}
</style>
