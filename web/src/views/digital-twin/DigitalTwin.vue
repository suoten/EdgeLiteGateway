<template>
  <div class="twin-page">
    <div ref="containerRef" class="twin-canvas" @click="onCanvasClick"></div>

    <div class="overlay-top">
      <div class="scene-selector">
        <div
          v-for="s in sceneOptions" :key="s.value"
          :class="['scene-btn', { active: selectedScene === s.value }]"
          @click="selectedScene = s.value"
        >{{ s.icon }} {{ s.label }}</div>
      </div>
      <div class="top-actions">
        <div :class="['action-btn', { active: autoRotate }]" @click="toggleAutoRotate" title="自动旋转">🔄</div>
        <div :class="['action-btn', { active: showLabels }]" @click="showLabels = !showLabels" title="标签">🏷️</div>
        <div :class="['action-btn', { active: showConnections }]" @click="showConnections = !showConnections; rebuildScene()" title="连线">🔗</div>
        <div class="action-btn" @click="takeScreenshot" title="截图">📷</div>
        <div class="action-btn" @click="rebuildScene" title="刷新">🔁</div>
      </div>
    </div>

    <div class="overlay-stats">
      <div class="stat-chip online">● {{ onlineCount }} 在线</div>
      <div class="stat-chip offline">● {{ offlineCount }} 离线</div>
      <div v-if="alarmCount > 0" class="stat-chip alarm">⚠ {{ alarmCount }} 告警</div>
      <div class="stat-chip total">共 {{ deviceList.length }} 台</div>
    </div>

    <div class="minimap-container" v-if="minimapUrl">
      <div class="minimap-title">导航</div>
      <canvas ref="minimapRef" class="minimap-canvas" @click="onMinimapClick"></canvas>
    </div>

    <Transition name="slide-right">
      <div v-if="selectedDevice" class="device-panel">
        <div class="panel-header">
          <div class="panel-title-row">
            <span class="panel-name">{{ selectedDevice.name }}</span>
            <n-tag :type="selectedDevice.status === 'online' ? 'success' : selectedDevice.status === 'offline' ? 'error' : 'default'" size="small" round>
              {{ deviceStatusLabel[selectedDevice.status] || selectedDevice.status }}
            </n-tag>
          </div>
          <div class="panel-id">{{ selectedDevice.device_id }} · {{ protocolLabel[selectedDevice.protocol] || selectedDevice.protocol }}</div>
        </div>
        <n-divider style="margin: 8px 0" />
        <div class="panel-points">
          <div v-for="pt in (selectedDevice.points || []).slice(0, 8)" :key="pt.name" class="point-row">
            <span class="point-name">{{ pt.name }}</span>
            <span :class="['point-val', { 'val-alarm': isPointAlarming(selectedDevice.device_id, pt.name) }]">{{ formatPtVal(selectedDevice.device_id, pt) }}</span>
          </div>
          <div v-if="!(selectedDevice.points || []).length" class="no-points">暂无测点数据</div>
        </div>
        <n-button block type="primary" ghost size="small" style="margin-top: 10px" @click="goToDeviceDetail">
          查看设备详情 →
        </n-button>
        <n-button text size="tiny" style="position: absolute; top: 8px; right: 8px" @click="selectedDevice = null">✕</n-button>
      </div>
    </Transition>

    <div :class="['device-list-toggle', { expanded: listExpanded }]" @click="listExpanded = !listExpanded">
      <span>{{ listExpanded ? '收起' : '📋 设备列表' }}</span>
    </div>
    <Transition name="slide-left">
      <div v-if="listExpanded" class="device-list-panel">
        <n-input v-model:value="deviceSearch" placeholder="搜索设备" size="small" clearable style="margin-bottom: 8px" />
        <div class="device-scroll">
          <div
            v-for="d in filteredDevices" :key="d.device_id"
            :class="['dl-item', { selected: selectedDevice?.device_id === d.device_id, alarming: alarmingDevices.has(d.device_id) }]"
            @click="flyToDevice(d)"
          >
            <div class="dl-dot" :style="{ background: d.status === 'online' ? '#18a058' : d.status === 'offline' ? '#d03050' : '#666' }"></div>
            <div class="dl-info">
              <div class="dl-name">{{ d.name }}</div>
              <div class="dl-meta">{{ protocolLabel[d.protocol] || d.protocol }}</div>
            </div>
            <div v-if="alarmingDevices.has(d.device_id)" class="dl-alarm-badge">⚠</div>
          </div>
        </div>
      </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRouter } from 'vue-router'
import { NTag, NButton, NDivider, NInput } from 'naive-ui'
import { deviceApi, alarmApi } from '@/api'
import { deviceStatusLabel, protocolLabel } from '@/utils/enumLabels'

const router = useRouter()
const containerRef = ref<HTMLElement | null>(null)
const minimapRef = ref<HTMLCanvasElement | null>(null)
const selectedScene = ref('factory')
const autoRotate = ref(true)
const showLabels = ref(true)
const showConnections = ref(true)
const deviceList = ref<any[]>([])
const selectedDevice = ref<any>(null)
const pointValues = ref<Record<string, Record<string, any>>>({})
const deviceSearch = ref('')
const listExpanded = ref(false)
const alarmingDevices = ref<Set<string>>(new Set())
const alarmCount = ref(0)
const minimapUrl = ref('')

const sceneOptions = [
  { label: '工厂车间', value: 'factory', icon: '🏭' },
  { label: '智慧园区', value: 'park', icon: '🏢' },
  { label: '能源站', value: 'energy', icon: '⚡' },
]

const onlineCount = computed(() => deviceList.value.filter(d => d.status === 'online').length)
const offlineCount = computed(() => deviceList.value.filter(d => d.status === 'offline').length)
const filteredDevices = computed(() => {
  if (!deviceSearch.value) return deviceList.value
  const q = deviceSearch.value.toLowerCase()
  return deviceList.value.filter(d => d.name.toLowerCase().includes(q) || d.device_id.toLowerCase().includes(q))
})

function formatPtVal(deviceId: string, pt: any) {
  const v = pointValues.value[deviceId]?.[pt.name]?.value ?? pointValues.value[deviceId]?.[pt.name]
  return v != null ? `${Number(v).toFixed(2)} ${pt.unit || ''}` : '-'
}

function isPointAlarming(deviceId: string, pointName: string): boolean {
  return alarmingDevices.value.has(deviceId)
}

const protocolColors: Record<string, number> = {
  modbus_tcp: 0x4fc3f7, modbus_rtu: 0x4fc3f7, opcua: 0x81c784,
  mqtt: 0xffb74d, http: 0xba68c8, simulator: 0x90a4ae,
  video: 0xef5350, s7: 0x64b5f6, fins: 0x4db6ac,
  s7comm: 0x64b5f6, mc: 0xff8a65, bacnet: 0xaed581,
}

let renderer: any = null, animationId: number | null = null, controls: any = null
let scene: any = null, camera: any = null, resizeHandler: (() => void) | null = null
let deviceMeshes: Map<string, any> = new Map(), labelSprites: Map<string, any> = new Map()
let connectionLines: any[] = [], flowParticles: any[] = []
let raycaster: any = null, mouse: any = null, refreshTimer: any = null, alarmTimer: any = null
let particleSystem: any = null, THREERef: any = null

function getDeviceColor(device: any): number {
  if (alarmingDevices.value.has(device.device_id)) return 0xff1744
  if (device.status === 'offline') return 0xe53935
  if (device.status === 'unknown') return 0x757575
  return protocolColors[device.protocol] || 0x00d4aa
}

function buildDeviceMesh(device: any, THREE: any) {
  const group = new THREE.Group()
  const color = getDeviceColor(device)
  const isOnline = device.status === 'online'
  const isAlarming = alarmingDevices.value.has(device.device_id)
  const baseMat = new THREE.MeshStandardMaterial({ color: 0x1a2a3a, metalness: 0.6, roughness: 0.4 })
  const bodyMat = new THREE.MeshStandardMaterial({
    color, metalness: 0.3, roughness: 0.5,
    emissive: isAlarming ? 0xff1744 : (isOnline ? color : 0x000000),
    emissiveIntensity: isAlarming ? 0.6 : (isOnline ? 0.2 : 0),
    transparent: !isOnline && !isAlarming, opacity: isOnline || isAlarming ? 1.0 : 0.6,
  })

  const base = new THREE.Mesh(new THREE.BoxGeometry(1.0, 0.15, 1.0), baseMat)
  base.position.y = 0.075
  group.add(base)

  const body = new THREE.Mesh(new THREE.BoxGeometry(0.7, 0.7, 0.7), bodyMat)
  body.position.y = 0.5
  body.castShadow = true
  group.add(body)

  if (device.protocol === 'modbus_tcp' || device.protocol === 'modbus_rtu') {
    const screen = new THREE.Mesh(
      new THREE.PlaneGeometry(0.5, 0.3),
      new THREE.MeshStandardMaterial({ color: 0x0a1628, emissive: isOnline ? 0x4fc3f7 : 0x000000, emissiveIntensity: 0.5 })
    )
    screen.position.set(0, 0.55, 0.351)
    group.add(screen)
  } else if (device.protocol === 'opcua') {
    const antenna = new THREE.Mesh(new THREE.CylinderGeometry(0.02, 0.02, 0.4, 8), new THREE.MeshStandardMaterial({ color: 0xcccccc, metalness: 0.8 }))
    antenna.position.set(0.2, 1.0, 0)
    group.add(antenna)
    const tip = new THREE.Mesh(new THREE.SphereGeometry(0.05, 8, 8), new THREE.MeshStandardMaterial({ color: isOnline ? 0x00ff88 : 0xff4444, emissive: isOnline ? 0x00ff88 : 0xff4444, emissiveIntensity: 0.8 }))
    tip.position.set(0.2, 1.2, 0)
    group.add(tip)
  } else if (device.protocol === 'video') {
    const lens = new THREE.Mesh(new THREE.CylinderGeometry(0.15, 0.2, 0.15, 16), new THREE.MeshStandardMaterial({ color: 0x111111, metalness: 0.9, roughness: 0.1 }))
    lens.rotation.x = Math.PI / 2
    lens.position.set(0, 0.55, 0.4)
    group.add(lens)
  } else if (device.protocol === 'mqtt') {
    const dish = new THREE.Mesh(new THREE.SphereGeometry(0.25, 16, 8, 0, Math.PI * 2, 0, Math.PI / 3), new THREE.MeshStandardMaterial({ color: 0xdddddd, metalness: 0.5, side: THREE.DoubleSide }))
    dish.position.y = 1.0
    dish.rotation.x = Math.PI
    group.add(dish)
  } else {
    const led = new THREE.Mesh(new THREE.SphereGeometry(0.06, 8, 8), new THREE.MeshStandardMaterial({ color: isAlarming ? 0xff1744 : (isOnline ? 0x00ff88 : 0xff4444), emissive: isAlarming ? 0xff1744 : (isOnline ? 0x00ff88 : 0xff4444), emissiveIntensity: 1.0 }))
    led.position.set(0.3, 0.85, 0.3)
    group.add(led)
  }

  if (isOnline || isAlarming) {
    const ringGeo = new THREE.RingGeometry(0.55, 0.65, 32)
    const ringColor = isAlarming ? 0xff1744 : color
    const ringMat = new THREE.MeshBasicMaterial({ color: ringColor, transparent: true, opacity: isAlarming ? 0.5 : 0.25, side: THREE.DoubleSide })
    const ring = new THREE.Mesh(ringGeo, ringMat)
    ring.rotation.x = -Math.PI / 2
    ring.position.y = 0.01
    ring.name = '__ring__'
    group.add(ring)
  }

  return group
}

function createLabel(text: string, THREE: any) {
  const canvas = document.createElement('canvas')
  const dpr = Math.min(window.devicePixelRatio, 2)
  canvas.width = 256 * dpr
  canvas.height = 64 * dpr
  const ctx = canvas.getContext('2d')!
  ctx.scale(dpr, dpr)
  ctx.fillStyle = 'rgba(10,20,40,0.85)'
  ctx.beginPath()
  ctx.roundRect(0, 0, 256, 64, 10)
  ctx.fill()
  ctx.strokeStyle = 'rgba(79,195,247,0.4)'
  ctx.lineWidth = 1
  ctx.beginPath()
  ctx.roundRect(0, 0, 256, 64, 10)
  ctx.stroke()
  ctx.fillStyle = '#e0f0ff'
  ctx.font = 'bold 20px "Segoe UI", sans-serif'
  ctx.textAlign = 'center'
  ctx.textBaseline = 'middle'
  ctx.fillText(text, 128, 32)
  const texture = new THREE.CanvasTexture(canvas)
  texture.needsUpdate = true
  const material = new THREE.SpriteMaterial({ map: texture, transparent: true, depthTest: false })
  const sprite = new THREE.Sprite(material)
  sprite.scale.set(2.2, 0.55, 1)
  return sprite
}

function getPositions(count: number, sceneType: string) {
  const positions: { x: number; y: number; z: number }[] = []
  if (sceneType === 'factory') {
    const cols = Math.ceil(Math.sqrt(count))
    const spacing = 3.0
    for (let i = 0; i < count; i++) {
      const row = Math.floor(i / cols)
      const col = i % cols
      positions.push({ x: (col - cols / 2 + 0.5) * spacing, y: 0, z: (row - Math.ceil(count / cols) / 2 + 0.5) * spacing })
    }
  } else if (sceneType === 'park') {
    const radius = Math.max(count * 0.7, 4)
    for (let i = 0; i < count; i++) {
      const angle = (i / count) * Math.PI * 2
      const r = radius + (i % 2) * 2
      positions.push({ x: Math.cos(angle) * r, y: 0, z: Math.sin(angle) * r })
    }
  } else {
    const cols = Math.ceil(count / 2)
    const spacing = 3.0
    for (let i = 0; i < count; i++) {
      const row = Math.floor(i / cols)
      const col = i % cols
      positions.push({ x: (col - cols / 2 + 0.5) * spacing, y: row * 1.5, z: 0 })
    }
  }
  return positions
}

function addConnectionLines(THREE: any) {
  connectionLines.forEach(l => scene.remove(l))
  flowParticles.forEach(p => scene.remove(p))
  connectionLines = []
  flowParticles = []

  if (!showConnections.value || deviceList.value.length < 2) return

  const protocolGroups: Record<string, string[]> = {}
  deviceList.value.forEach(d => {
    const key = d.protocol
    if (!protocolGroups[key]) protocolGroups[key] = []
    protocolGroups[key].push(d.device_id)
  })

  Object.entries(protocolGroups).forEach(([protocol, ids]) => {
    if (ids.length < 2) return
    const color = protocolColors[protocol] || 0x4fc3f7
    for (let i = 0; i < ids.length - 1; i++) {
      const meshA = deviceMeshes.get(ids[i])
      const meshB = deviceMeshes.get(ids[i + 1])
      if (!meshA || !meshB) continue

      const points = [meshA.position.clone(), meshB.position.clone()]
      points[0].y = 0.8
      points[1].y = 0.8
      const geometry = new THREE.BufferGeometry().setFromPoints(points)
      const material = new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.2 })
      const line = new THREE.Line(geometry, material)
      scene.add(line)
      connectionLines.push(line)

      const particleGeo = new THREE.SphereGeometry(0.04, 6, 6)
      const particleMat = new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.7 })
      const particle = new THREE.Mesh(particleGeo, particleMat)
      particle.userData = { from: points[0].clone(), to: points[1].clone(), progress: Math.random(), speed: 0.3 + Math.random() * 0.3 }
      scene.add(particle)
      flowParticles.push(particle)
    }
  })
}

function addEnvironment(THREE: any) {
  if (!scene) return
  const oldEnv = scene.getObjectByName('__env__')
  if (oldEnv) scene.remove(oldEnv)
  const envGroup = new THREE.Group()
  envGroup.name = '__env__'
  const wallMat = new THREE.MeshStandardMaterial({ color: 0x1a2a3a, metalness: 0.3, roughness: 0.7, transparent: true, opacity: 0.3 })
  if (selectedScene.value === 'factory') {
    const wall = new THREE.Mesh(new THREE.BoxGeometry(30, 4, 0.2), wallMat)
    wall.position.set(0, 2, -15)
    envGroup.add(wall)
    const wall2 = wall.clone()
    wall2.position.set(0, 2, 15)
    envGroup.add(wall2)
    const wall3 = new THREE.Mesh(new THREE.BoxGeometry(0.2, 4, 30), wallMat)
    wall3.position.set(-15, 2, 0)
    envGroup.add(wall3)
  } else if (selectedScene.value === 'park') {
    for (let i = 0; i < 6; i++) {
      const angle = (i / 6) * Math.PI * 2
      const r = Math.max(deviceList.value.length * 0.7, 4) + 5
      const h = 3 + Math.random() * 4
      const building = new THREE.Mesh(new THREE.BoxGeometry(2 + Math.random() * 2, h, 2 + Math.random() * 2), new THREE.MeshStandardMaterial({ color: 0x2a3a4a, metalness: 0.5, roughness: 0.5 }))
      building.position.set(Math.cos(angle) * r, h / 2, Math.sin(angle) * r)
      building.castShadow = true
      envGroup.add(building)
    }
  }
  scene.add(envGroup)
}

function addParticles(THREE: any) {
  if (!scene || particleSystem) return
  const count = 200
  const positions = new Float32Array(count * 3)
  for (let i = 0; i < count; i++) {
    positions[i * 3] = (Math.random() - 0.5) * 30
    positions[i * 3 + 1] = Math.random() * 8
    positions[i * 3 + 2] = (Math.random() - 0.5) * 30
  }
  const geo = new THREE.BufferGeometry()
  geo.setAttribute('position', new THREE.BufferAttribute(positions, 3))
  const mat = new THREE.PointsMaterial({ color: 0x4fc3f7, size: 0.05, transparent: true, opacity: 0.4 })
  particleSystem = new THREE.Points(geo, mat)
  scene.add(particleSystem)
}

function addDevicesToScene(THREE: any) {
  if (!scene) return
  deviceMeshes.forEach((mesh) => {
    scene.remove(mesh)
    mesh.traverse((obj: any) => { if (obj.geometry) obj.geometry.dispose(); if (obj.material) obj.material.dispose() })
  })
  labelSprites.forEach((sprite) => {
    scene.remove(sprite)
    if (sprite.material) { sprite.material.map?.dispose(); sprite.material.dispose() }
  })
  deviceMeshes.clear()
  labelSprites.clear()

  const devices = deviceList.value
  if (!devices.length) return
  const positions = getPositions(devices.length, selectedScene.value)

  devices.forEach((device, i) => {
    const pos = positions[i] || { x: 0, y: 0, z: 0 }
    const group = buildDeviceMesh(device, THREE)
    group.position.set(pos.x, pos.y, pos.z)
    group.userData = { deviceId: device.device_id, deviceName: device.name, floatOffset: i * 0.7, isAlarming: alarmingDevices.value.has(device.device_id) }
    scene.add(group)
    deviceMeshes.set(device.device_id, group)

    if (showLabels.value) {
      const label = createLabel(device.name, THREE)
      label.position.set(pos.x, 1.6, pos.z)
      scene.add(label)
      labelSprites.set(device.device_id, label)
    }
  })

  addConnectionLines(THREE)

  const maxDist = Math.sqrt(devices.length) * 3 + 5
  if (camera) {
    camera.position.set(maxDist, maxDist * 0.7, maxDist)
    camera.lookAt(0, 0, 0)
  }
}

function updateMinimap() {
  if (!minimapRef.value || !camera || deviceList.value.length === 0) return
  const canvas = minimapRef.value
  const ctx = canvas.getContext('2d')!
  const w = canvas.width, h = canvas.height
  ctx.fillStyle = '#0a0f1a'
  ctx.fillRect(0, 0, w, h)

  const positions = getPositions(deviceList.value.length, selectedScene.value)
  let minX = Infinity, maxX = -Infinity, minZ = Infinity, maxZ = -Infinity
  positions.forEach(p => { minX = Math.min(minX, p.x); maxX = Math.max(maxX, p.x); minZ = Math.min(minZ, p.z); maxZ = Math.max(maxZ, p.z) })
  const rangeX = maxX - minX + 4 || 10, rangeZ = maxZ - minZ + 4 || 10
  const scale = Math.min((w - 16) / rangeX, (h - 16) / rangeZ)
  const cx = w / 2, cy = h / 2

  deviceList.value.forEach((d, i) => {
    const p = positions[i]
    if (!p) return
    const sx = cx + (p.x - (minX + maxX) / 2) * scale
    const sy = cy + (p.z - (minZ + maxZ) / 2) * scale
    ctx.beginPath()
    ctx.arc(sx, sy, 3, 0, Math.PI * 2)
    if (alarmingDevices.value.has(d.device_id)) { ctx.fillStyle = '#ff1744' }
    else if (d.status === 'online') { ctx.fillStyle = '#18a058' }
    else { ctx.fillStyle = '#d03050' }
    ctx.fill()
  })

  if (selectedDevice.value) {
    const idx = deviceList.value.findIndex(d => d.device_id === selectedDevice.value.device_id)
    if (idx >= 0 && positions[idx]) {
      const p = positions[idx]
      const sx = cx + (p.x - (minX + maxX) / 2) * scale
      const sy = cy + (p.z - (minZ + maxZ) / 2) * scale
      ctx.strokeStyle = '#4fc3f7'
      ctx.lineWidth = 2
      ctx.beginPath()
      ctx.arc(sx, sy, 6, 0, Math.PI * 2)
      ctx.stroke()
    }
  }
}

function onMinimapClick(e: MouseEvent) {
  if (!minimapRef.value || deviceList.value.length === 0) return
  const rect = minimapRef.value.getBoundingClientRect()
  const x = (e.clientX - rect.left) / rect.width
  const y = (e.clientY - rect.top) / rect.height
  const positions = getPositions(deviceList.value.length, selectedScene.value)
  let minX = Infinity, maxX = -Infinity, minZ = Infinity, maxZ = -Infinity
  positions.forEach(p => { minX = Math.min(minX, p.x); maxX = Math.max(maxX, p.x); minZ = Math.min(minZ, p.z); maxZ = Math.max(maxZ, p.z) })
  const targetX = minX + x * (maxX - minX)
  const targetZ = minZ + y * (maxZ - minZ)
  if (camera && controls) {
    camera.position.set(targetX + 5, 6, targetZ + 5)
    controls.target.set(targetX, 0.5, targetZ)
    controls.update()
  }
}

function takeScreenshot() {
  if (!renderer) return
  renderer.render(scene, camera)
  const dataUrl = renderer.domElement.toDataURL('image/png')
  const a = document.createElement('a')
  a.href = dataUrl
  a.download = `edgelite-3d-${Date.now()}.png`
  a.click()
}

async function loadScene() {
  if (!containerRef.value) return
  try {
    const THREE = await import('three')
    const { OrbitControls } = await import('three/examples/jsm/controls/OrbitControls.js')
    THREERef = THREE
    cleanupScene()

    const width = containerRef.value.clientWidth
    const height = containerRef.value.clientHeight

    scene = new THREE.Scene()
    scene.background = new THREE.Color(0x0a0f1a)
    scene.fog = new THREE.FogExp2(0x0a0f1a, 0.02)

    camera = new THREE.PerspectiveCamera(55, width / height, 0.1, 500)
    camera.position.set(10, 8, 10)

    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false, preserveDrawingBuffer: true })
    renderer.setSize(width, height)
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
    renderer.shadowMap.enabled = true
    renderer.shadowMap.type = THREE.PCFSoftShadowMap
    renderer.toneMapping = THREE.ACESFilmicToneMapping
    renderer.toneMappingExposure = 1.2
    containerRef.value.innerHTML = ''
    containerRef.value.appendChild(renderer.domElement)

    controls = new OrbitControls(camera, renderer.domElement)
    controls.enableDamping = true
    controls.dampingFactor = 0.08
    controls.autoRotate = autoRotate.value
    controls.autoRotateSpeed = 0.3
    controls.maxPolarAngle = Math.PI / 2.1
    controls.minDistance = 3
    controls.maxDistance = 50

    const ambient = new THREE.AmbientLight(0x1a2a4a, 2)
    scene.add(ambient)
    const dirLight = new THREE.DirectionalLight(0xffffff, 2)
    dirLight.position.set(10, 20, 10)
    dirLight.castShadow = true
    dirLight.shadow.mapSize.set(2048, 2048)
    dirLight.shadow.camera.far = 50
    dirLight.shadow.camera.left = -20
    dirLight.shadow.camera.right = 20
    dirLight.shadow.camera.top = 20
    dirLight.shadow.camera.bottom = -20
    scene.add(dirLight)
    const hemiLight = new THREE.HemisphereLight(0x4fc3f7, 0x0a1628, 0.5)
    scene.add(hemiLight)

    const grid = new THREE.GridHelper(40, 40, 0x1a3a5a, 0x0d1f2d)
    scene.add(grid)
    const ground = new THREE.Mesh(new THREE.PlaneGeometry(40, 40), new THREE.MeshStandardMaterial({ color: 0x0a1628, roughness: 0.9, metalness: 0.1 }))
    ground.rotation.x = -Math.PI / 2
    ground.position.y = -0.01
    ground.receiveShadow = true
    scene.add(ground)

    raycaster = new THREE.Raycaster()
    mouse = new THREE.Vector2()

    addEnvironment(THREE)
    addParticles(THREE)
    addDevicesToScene(THREE)

    const onResize = () => {
      if (!containerRef.value || !camera || !renderer) return
      const w = containerRef.value.clientWidth
      const h = containerRef.value.clientHeight
      camera.aspect = w / h
      camera.updateProjectionMatrix()
      renderer.setSize(w, h)
    }
    resizeHandler = onResize
    window.addEventListener('resize', onResize)

    const clock = new THREE.Clock()
    const animate = () => {
      animationId = requestAnimationFrame(animate)
      const t = clock.getElapsedTime()
      controls.update()

      deviceMeshes.forEach((mesh) => {
        if (mesh.userData.deviceId) {
          const offset = mesh.userData.floatOffset || 0
          mesh.position.y = Math.sin(t * 0.8 + offset) * 0.03
          if (mesh.userData.isAlarming) {
            const ring = mesh.getObjectByName('__ring__')
            if (ring) ring.material.opacity = 0.2 + Math.sin(t * 4) * 0.3
          }
        }
      })

      flowParticles.forEach(p => {
        p.userData.progress += p.userData.speed * 0.01
        if (p.userData.progress > 1) p.userData.progress = 0
        const from = p.userData.from, to = p.userData.to
        p.position.lerpVectors(from, to, p.userData.progress)
      })

      if (particleSystem) {
        const pos = particleSystem.geometry.attributes.position
        for (let i = 0; i < pos.count; i++) {
          pos.array[i * 3 + 1] += 0.002
          if (pos.array[i * 3 + 1] > 8) pos.array[i * 3 + 1] = 0
        }
        pos.needsUpdate = true
      }

      renderer.render(scene, camera)
      updateMinimap()
    }
    animate()
  } catch (e) {
    console.error('3D场景加载失败:', e)
  }
}

function onCanvasClick(event: MouseEvent) {
  if (!containerRef.value || !raycaster || !camera) return
  const rect = containerRef.value.getBoundingClientRect()
  mouse.x = ((event.clientX - rect.left) / rect.width) * 2 - 1
  mouse.y = -((event.clientY - rect.top) / rect.height) * 2 + 1
  raycaster.setFromCamera(mouse, camera)
  const allMeshes: any[] = []
  deviceMeshes.forEach((group) => {
    group.traverse((obj: any) => { if (obj.isMesh) allMeshes.push(obj) })
  })
  const intersects = raycaster.intersectObjects(allMeshes)
  if (intersects.length > 0) {
    let obj = intersects[0].object
    while (obj && !obj.userData?.deviceId) obj = obj.parent
    if (obj?.userData?.deviceId) {
      const device = deviceList.value.find(d => d.device_id === obj.userData.deviceId)
      if (device) {
        selectedDevice.value = device
        fetchDevicePoints(device.device_id)
        flyToPosition(obj.position)
        return
      }
    }
  }
  selectedDevice.value = null
}

function flyToPosition(pos: any) {
  if (!camera || !controls) return
  const start = { x: camera.position.x, y: camera.position.y, z: camera.position.z }
  const target = { x: pos.x + 5, y: pos.y + 5, z: pos.z + 5 }
  const startTime = performance.now()
  const duration = 800
  const animateFly = () => {
    const elapsed = performance.now() - startTime
    const t = Math.min(elapsed / duration, 1)
    const ease = 1 - Math.pow(1 - t, 3)
    camera.position.set(start.x + (target.x - start.x) * ease, start.y + (target.y - start.y) * ease, start.z + (target.z - start.z) * ease)
    controls.target.set(pos.x, pos.y + 0.5, pos.z)
    controls.update()
    if (t < 1) requestAnimationFrame(animateFly)
  }
  animateFly()
}

function flyToDevice(device: any) {
  selectedDevice.value = device
  fetchDevicePoints(device.device_id)
  const mesh = deviceMeshes.get(device.device_id)
  if (mesh) flyToPosition(mesh.position)
}

function toggleAutoRotate() {
  autoRotate.value = !autoRotate.value
  if (controls) controls.autoRotate = autoRotate.value
}

function rebuildScene() { fetchDevices().then(() => loadScene()) }
function goToDeviceDetail() { if (selectedDevice.value) router.push(`/devices/${selectedDevice.value.device_id}`) }

watch([showLabels, selectedScene], () => loadScene())

async function fetchDevices() {
  try {
    const data = await deviceApi.list({ page: 1, size: 200 })
    deviceList.value = data?.data ?? []
  } catch { /* ignore */ }
}

async function fetchAlarms() {
  try {
    const data = await alarmApi.list({ status: 'firing', page: 1, size: 100 })
    const alarms = data?.data ?? []
    const newSet = new Set<string>()
    alarms.forEach((a: any) => { if (a.device_id) newSet.add(a.device_id) })
    alarmingDevices.value = newSet
    alarmCount.value = alarms.length
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
    if (device.status === 'online') await fetchDevicePoints(device.device_id)
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
  if (renderer) { renderer.dispose(); renderer.forceContextLoss() }
  deviceMeshes.clear()
  labelSprites.clear()
  connectionLines = []
  flowParticles = []
  particleSystem = null
}

onMounted(async () => {
  await fetchDevices()
  await fetchAlarms()
  loadScene()
  minimapUrl.value = 'active'
  refreshTimer = setInterval(refreshAllPoints, 10000)
  alarmTimer = setInterval(fetchAlarms, 15000)
  ;(window as any).__THREE__ = await import('three')
})

onUnmounted(() => {
  if (resizeHandler) { window.removeEventListener('resize', resizeHandler); resizeHandler = null }
  cleanupScene()
  if (controls) controls.dispose()
  if (refreshTimer) clearInterval(refreshTimer)
  if (alarmTimer) clearInterval(alarmTimer)
})
</script>

<style scoped>
.twin-page {
  position: relative;
  width: 100%;
  height: calc(100vh - 80px);
  overflow: hidden;
  background: #0a0f1a;
  border-radius: 8px;
}

.twin-canvas { width: 100%; height: 100%; cursor: grab; }
.twin-canvas:active { cursor: grabbing; }

.overlay-top {
  position: absolute; top: 12px; left: 16px; right: 16px;
  display: flex; justify-content: space-between; align-items: center;
  pointer-events: none; z-index: 10;
}
.overlay-top > * { pointer-events: auto; }

.scene-selector {
  display: flex; gap: 4px;
  background: rgba(10, 15, 26, 0.85); backdrop-filter: blur(12px);
  border-radius: 8px; padding: 3px; border: 1px solid rgba(79, 195, 247, 0.15);
}

.scene-btn {
  padding: 6px 14px; border-radius: 6px; font-size: 13px; cursor: pointer;
  color: #b8c9d1; transition: all 0.2s; white-space: nowrap;
}
.scene-btn:hover { color: #e0f0ff; background: rgba(79, 195, 247, 0.1); }
.scene-btn.active { color: #fff; background: rgba(79, 195, 247, 0.25); }

.top-actions {
  display: flex; gap: 4px;
  background: rgba(10, 15, 26, 0.85); backdrop-filter: blur(12px);
  border-radius: 8px; padding: 3px; border: 1px solid rgba(79, 195, 247, 0.15);
}

.action-btn {
  width: 34px; height: 34px; display: flex; align-items: center; justify-content: center;
  border-radius: 6px; cursor: pointer; font-size: 16px; transition: all 0.2s;
}
.action-btn:hover { background: rgba(79, 195, 247, 0.15); }
.action-btn.active { background: rgba(79, 195, 247, 0.25); }

.overlay-stats {
  position: absolute; bottom: 16px; left: 16px;
  display: flex; gap: 8px; z-index: 10;
}

.stat-chip {
  padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 500;
  background: rgba(10, 15, 26, 0.85); backdrop-filter: blur(12px);
  border: 1px solid rgba(79, 195, 247, 0.15);
}
.stat-chip.online { color: #18a058; }
.stat-chip.offline { color: #d03050; }
.stat-chip.alarm { color: #ff1744; border-color: rgba(255, 23, 68, 0.3); }
.stat-chip.total { color: #b8c9d1; }

.minimap-container {
  position: absolute; bottom: 16px; left: 50%; transform: translateX(-50%);
  background: rgba(10, 15, 26, 0.85); backdrop-filter: blur(12px);
  border: 1px solid rgba(79, 195, 247, 0.15); border-radius: 8px;
  padding: 4px; z-index: 10;
}
.minimap-title { font-size: 9px; color: #8faabe; text-align: center; margin-bottom: 2px; }
.minimap-canvas { width: 120px; height: 120px; border-radius: 4px; cursor: pointer; }

.device-panel {
  position: absolute; top: 60px; right: 16px; width: 280px;
  background: rgba(10, 20, 40, 0.92); backdrop-filter: blur(16px);
  border: 1px solid rgba(79, 195, 247, 0.2); border-radius: 12px;
  padding: 16px; color: #e0f0ff; z-index: 20;
}

.panel-header { margin-bottom: 4px; }
.panel-title-row { display: flex; align-items: center; gap: 8px; }
.panel-name { font-weight: 700; font-size: 16px; color: #4fc3f7; }
.panel-id { font-size: 11px; color: #8faabe; margin-top: 4px; }

.panel-points { max-height: 240px; overflow-y: auto; }
.point-row { display: flex; justify-content: space-between; padding: 4px 0; font-size: 13px; }
.point-name { color: #b8c9d1; }
.point-val { color: #e0f0ff; font-weight: 600; font-variant-numeric: tabular-nums; }
.point-val.val-alarm { color: #ff1744; }
.no-points { text-align: center; color: #8faabe; font-size: 12px; padding: 16px 0; }

.device-list-toggle {
  position: absolute; bottom: 16px; right: 16px;
  padding: 8px 16px;
  background: rgba(10, 15, 26, 0.85); backdrop-filter: blur(12px);
  border: 1px solid rgba(79, 195, 247, 0.15); border-radius: 8px;
  color: #b8c9d1; font-size: 13px; cursor: pointer; z-index: 10; transition: all 0.2s;
}
.device-list-toggle:hover { color: #e0f0ff; border-color: rgba(79, 195, 247, 0.3); }

.device-list-panel {
  position: absolute; bottom: 52px; right: 16px; width: 260px; max-height: 400px;
  background: rgba(10, 20, 40, 0.92); backdrop-filter: blur(16px);
  border: 1px solid rgba(79, 195, 247, 0.2); border-radius: 12px;
  padding: 12px; color: #e0f0ff; z-index: 20;
}

.device-scroll { max-height: 320px; overflow-y: auto; }

.dl-item {
  display: flex; align-items: center; gap: 8px; padding: 8px;
  border-radius: 6px; cursor: pointer; transition: background 0.15s;
}
.dl-item:hover { background: rgba(79, 195, 247, 0.1); }
.dl-item.selected { background: rgba(79, 195, 247, 0.2); }
.dl-item.alarming { background: rgba(255, 23, 68, 0.08); }

.dl-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.dl-info { flex: 1; min-width: 0; }
.dl-name { font-size: 13px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.dl-meta { font-size: 11px; color: #8faabe; }
.dl-alarm-badge { font-size: 12px; animation: pulse 1s infinite; }

@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }

.slide-right-enter-active, .slide-right-leave-active { transition: all 0.3s ease; }
.slide-right-enter-from { transform: translateX(20px); opacity: 0; }
.slide-right-leave-to { transform: translateX(20px); opacity: 0; }

.slide-left-enter-active, .slide-left-leave-active { transition: all 0.25s ease; }
.slide-left-enter-from { transform: translateY(10px); opacity: 0; }
.slide-left-leave-to { transform: translateY(10px); opacity: 0; }
</style>
