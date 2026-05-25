<template>
  <div class="driver-config-page">
    <n-card :title="t('driver.title')">
      <template #header-extra>
        <n-button type="primary" @click="loadDrivers">{{ t('driver.refresh') }}</n-button>
      </template>
      <n-data-table :columns="columns" :data="drivers" :loading="loading" />
    </n-card>

    <n-modal v-model:show="showSchemaModal" preset="card" :title="currentSchemaTitle" style="width: 700px">
      <n-alert v-if="currentSchema?.description" type="info" :show-icon="false" style="margin-bottom: 16px">
        {{ translateFieldText(currentDriverName, currentSchema.description) }}
      </n-alert>
      <n-empty v-if="!currentSchema?.fields?.length" :description="t('driver.noSchema')" />
      <n-descriptions v-else bordered :column="1" label-placement="left">
        <n-descriptions-item v-for="field in currentSchema.fields" :key="field.name" :label="translateFieldText(currentDriverName, field.label) || field.name">
          <n-space vertical :size="4">
            <n-space align="center" :size="8">
              <n-tag size="small" :type="field.required ? 'error' : 'default'">{{ field.required ? t('driver.required') : t('driver.optional') }}</n-tag>
              <n-tag size="small" type="info">{{ typeMap[field.type] || field.type }}</n-tag>
              <n-tag v-if="field.secret" size="small" type="warning">{{ t('driver.sensitive') }}</n-tag>
            </n-space>
            <n-text v-if="field.description" depth="3" style="font-size: 13px">
              {{ translateFieldText(currentDriverName, field.description) }}
            </n-text>
            <n-text v-if="field.default !== undefined && field.default !== ''" depth="3" style="font-size: 13px">
              {{ t('driver.defaultValue') }} <n-text code style="font-size: 12px">{{ field.default }}</n-text>
            </n-text>
            <n-space v-if="field.options" :size="8" style="flex-wrap: wrap">
              <n-tag v-for="opt in field.options" :key="opt" size="small" type="success">{{ opt }}</n-tag>
            </n-space>
          </n-space>
        </n-descriptions-item>
      </n-descriptions>
    </n-modal>

    <n-modal v-model:show="showDiscoverModal" preset="card" :title="t('driver.deviceDiscover')" style="width: 500px">
      <n-spin :show="discovering">
        <n-empty v-if="discoveredDevices.length === 0 && !discovering" :description="t('driver.discoverHint')" />
        <n-list v-else>
          <n-list-item v-for="dev in discoveredDevices" :key="dev.device_id">
            <n-thing :title="dev.name">
              <template #description>
                <n-space>
                  <n-tag size="small">{{ dev.protocol }}</n-tag>
                  <span style="font-size: 12px; color: #666">{{ dev.ip }}</span>
                </n-space>
              </template>
            </n-thing>
          </n-list-item>
        </n-list>
      </n-spin>
      <template #footer>
        <n-button type="primary" :loading="discovering" @click="doDiscover">{{ t('driver.startDiscover') }}</n-button>
      </template>
    </n-modal>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, h } from 'vue'
import { NCard, NButton, NDataTable, NTag, NSpace, NModal, NDescriptions, NDescriptionsItem, NList, NListItem, NThing, NEmpty, NSpin, NAlert, NText, useMessage } from 'naive-ui'
import { t } from '@/i18n'
import { extractError } from '@/utils/errorCodes'
import { driverApi } from '@/api'

const driverFieldI18nMap: Record<string, Record<string, string>> = {
  'Modbus TCP': {
    'Modbus TCP industrial standard protocol for reading/writing PLC/ineter coils and registers': 'driverField.modbus-tcp.desc',
    'IP Address': 'driverField.modbus-tcp.ipAddr',
    'PLC or gateway IP address': 'driverField.modbus-tcp.ipAddrDesc',
    'Port': 'driverField.modbus-tcp.port',
    'Modbus TCP port, default 502': 'driverField.modbus-tcp.portDesc',
    'Slave ID': 'driverField.modbus-tcp.slaveId',
    'Device slave address (Unit ID), usually 1': 'driverField.modbus-tcp.slaveIdDesc',
    'Timeout (s)': 'driverField.modbus-tcp.timeout',
    'Connection and read timeout': 'driverField.modbus-tcp.timeoutDesc',
  },
  'Modbus RTU': {
    'Modbus RTU serial protocol for RS-485/RS-232 devices': 'driverField.modbus-rtu.desc',
    'Serial Port': 'driverField.modbus-rtu.serialPort',
    'Serial port device path': 'driverField.modbus-rtu.serialPortDesc',
    'Baud Rate': 'driverField.modbus-rtu.baudRate',
    'Communication baud rate': 'driverField.modbus-rtu.baudRateDesc',
    'Parity': 'driverField.modbus-rtu.parity',
    'Serial parity check': 'driverField.modbus-rtu.parityDesc',
  },
  'OPC-UA': {
    'OPC UA industrial communication protocol': 'driverField.opcua.desc',
    'Server URL': 'driverField.opcua.serverUrl',
    'OPC UA server address': 'driverField.opcua.serverUrlDesc',
    'Security Mode': 'driverField.opcua.securityMode',
    'OPC UA security mode': 'driverField.opcua.securityModeDesc',
    'Username': 'driverField.opcua.username',
    'Password': 'driverField.opcua.password',
  },
  'MQTT Client': {
    'MQTT message queue telemetry transport protocol': 'driverField.mqtt-client.desc',
    'Broker': 'driverField.mqtt-client.broker',
    'MQTT broker address': 'driverField.mqtt-client.brokerDesc',
    'Topic': 'driverField.mqtt-client.topic',
    'MQTT subscribe/publish topic': 'driverField.mqtt-client.topicDesc',
    'QoS': 'driverField.mqtt-client.qos',
  },
  'HTTP Webhook': {
    'HTTP Webhook for receiving external data push': 'driverField.http-webhook.desc',
    'URL': 'driverField.http-webhook.url',
    'Webhook callback URL': 'driverField.http-webhook.urlDesc',
    'Method': 'driverField.http-webhook.method',
    'HTTP request method': 'driverField.http-webhook.methodDesc',
  },
  'Siemens S7': {
    'Siemens S7 protocol for S7-200/300/400/1200/1500 PLCs': 'driverField.siemens-s7.desc',
    'Host': 'driverField.siemens-s7.host',
    'PLC IP address': 'driverField.siemens-s7.hostDesc',
    'Rack': 'driverField.siemens-s7.rack',
    'PLC rack number': 'driverField.siemens-s7.rackDesc',
    'Slot': 'driverField.siemens-s7.slot',
    'PLC slot number': 'driverField.siemens-s7.slotDesc',
    'CPU Type': 'driverField.siemens-s7.cpuType',
    'Siemens CPU model': 'driverField.siemens-s7.cpuTypeDesc',
  },
  'Mitsubishi MC': {
    'Mitsubishi MC protocol for Q/FX series PLCs': 'driverField.mitsubishi-mc.desc',
    'Network No': 'driverField.mitsubishi-mc.networkNo',
    'Station No': 'driverField.mitsubishi-mc.stationNo',
  },
  'Omron FINS': {
    'Omron FINS protocol for CJ/CP series PLCs': 'driverField.omron-fins.desc',
  },
  'Allen-Bradley': {
    'Allen-Bradley CIP protocol for ControlLogix/CompactLogix': 'driverField.allen-bradley.desc',
  },
  'FANUC CNC': {
    'FANUC FOCAS2 protocol (native socket, no fwlipy required)': 'driverField.fanuc-cnc.desc',
    'FANUC CNC IP address': 'driverField.fanuc-cnc.ipAddr',
    'Port Number': 'driverField.fanuc-cnc.portNumber',
    'FANUC CNC port number': 'driverField.fanuc-cnc.portNumberDesc',
  },
  'MTConnect': {
    'MTConnect protocol for CNC/machine data': 'driverField.mtconnect.desc',
    'Agent URL': 'driverField.mtconnect.agentUrl',
    'MTConnect agent address': 'driverField.mtconnect.agentUrlDesc',
  },
  'Toledo': {
    'Toledo weighing instrument protocol': 'driverField.toledo.desc',
    'COM Port': 'driverField.toledo.comPort',
    'Serial port for Toledo device': 'driverField.toledo.comPortDesc',
  },
  'Serial Device': {
    'Generic serial port device protocol': 'driverField.serial-device.desc',
    'Serial port device path (e.g. /dev/ttyUSB0)': 'driverField.serial-device.serialPortPath',
  },
  'Database Source': {
    'Database source for reading data from SQL databases': 'driverField.database-source.desc',
    'Database Type': 'driverField.database-source.dbType',
    'Connection string or database type': 'driverField.database-source.dbTypeDesc',
    'Connection String': 'driverField.database-source.connStr',
    'Database connection URL': 'driverField.database-source.connStrDesc',
    'Query': 'driverField.database-source.query',
    'SQL query statement': 'driverField.database-source.queryDesc',
  },
  'Barcode Scanner': {
    'Barcode scanner input via serial port': 'driverField.barcode-scanner.desc',
  },
  'Sparkplug B': {
    'Sparkplug B industrial MQTT specification': 'driverField.sparkplug-b.desc',
    'Group ID': 'driverField.sparkplug-b.groupId',
    'Sparkplug B group ID': 'driverField.sparkplug-b.groupIdDesc',
    'Edge Node ID': 'driverField.sparkplug-b.edgeNodeId',
    'Sparkplug B edge node ID': 'driverField.sparkplug-b.edgeNodeIdDesc',
  },
  'DL/T 645': {
    'DL/T 645 Chinese smart meter protocol': 'driverField.dlt645.desc',
    'Meter Address': 'driverField.dlt645.meterAddr',
    'Smart meter communication address': 'driverField.dlt645.meterAddrDesc',
  },
  'IEC 104': {
    'IEC 60870-5-104 power system protocol': 'driverField.iec104.desc',
    'ASDU Address': 'driverField.iec104.asduAddr',
    'IEC 104 ASDU address': 'driverField.iec104.asduAddrDesc',
  },
  'KUKA EKRL': {
    'KUKA EKRL robot protocol': 'driverField.kuka-ekrl.desc',
    'Robot IP': 'driverField.kuka-ekrl.robotIp',
    'KUKA robot IP address': 'driverField.kuka-ekrl.robotIpDesc',
  },
  'ABB RWS': {
    'ABB Robot Web Services protocol': 'driverField.abb-rws.desc',
    'Robot Controller IP': 'driverField.abb-rws.controllerIp',
    'ABB robot controller IP address': 'driverField.abb-rws.controllerIpDesc',
  },
  'ONVIF': {
    'ONVIF IP camera protocol': 'driverField.onvif.desc',
    'Camera IP': 'driverField.onvif.cameraIp',
    'ONVIF camera IP address': 'driverField.onvif.cameraIpDesc',
  },
  'Video(GB28181)': {
    'GB/T 28181 video surveillance protocol': 'driverField.video-gb28181.desc',
    'SIP Server': 'driverField.video-gb28181.sipServer',
    'GB28181 SIP server address': 'driverField.video-gb28181.sipServerDesc',
    'Device ID': 'driverField.video-gb28181.deviceId',
    'GB28181 device ID (20 digits)': 'driverField.video-gb28181.deviceIdDesc',
  },
  'Simulator': {
    'Built-in simulator for testing and demo': 'driverField.simulator.desc',
  },
}

function translateFieldText(driverName: string, text: string): string {
  if (!text) return text
  const driverMap = driverFieldI18nMap[driverName]
  if (driverMap && driverMap[text]) return t(driverMap[text])
  return text
}

// FIXED: 原问题-DriverConfig.vue全部中文硬编码，改为i18n
const message = useMessage()
const drivers = ref<any[]>([])
const loading = ref(false)
const showSchemaModal = ref(false)
const currentSchema = ref<any>(null)
const currentDriverName = ref('')
const showDiscoverModal = ref(false)
const discovering = ref(false)
const discoveredDevices = ref<any[]>([])

const currentSchemaTitle = computed(() => {
  const name = currentDriverName.value
  return `${name} - ${t('driver.configTemplate')}`
})

const typeMap = computed<Record<string, string>>(() => ({
  string: t('driver.typeText'),
  integer: t('driver.typeInteger'),
  number: t('driver.typeNumber'),
  boolean: t('driver.typeBoolean'),
  array: t('driver.typeArray'),
  object: t('driver.typeObject'),
}))

const columns = computed(() => [
  { title: t('driver.colName'), key: 'name', width: 160 },
  { title: t('driver.colVersion'), key: 'version', width: 80 },
  { title: t('driver.colProtocol'), key: 'protocols', render: (row: any) => h(NSpace, { size: 4 }, () => (row.protocols ?? []).map((p: string) => h(NTag, { size: 'small', type: 'info' }, () => p))) },
  { title: t('driver.colActions'), key: 'actions', width: 200, render: (row: any) => h(NSpace, {}, () => [
    h(NButton, { size: 'small', onClick: () => viewSchema(row.name) }, () => t('driver.colConfig')),
    h(NButton, { size: 'small', type: 'primary', onClick: () => startDiscover(row.name) }, () => t('driver.colDiscover')),
  ]) },
])

async function loadDrivers() {
  loading.value = true
  try {
    const data = await driverApi.list()
    drivers.value = data?.drivers || []
  } catch (e: any) { message.error(extractError(e, t('driver.loadListFailed'))) }
  finally { loading.value = false }
}

async function viewSchema(name: string) {
  currentDriverName.value = name
  try {
    const data = await driverApi.configSchema(name)
    if (data) {
      currentSchema.value = data.config_schema
      showSchemaModal.value = true
    }
  } catch (e: any) { message.error(extractError(e, t('driver.loadSchemaFailed'))) }
}

function startDiscover(name: string) {
  currentDriverName.value = name
  discoveredDevices.value = []
  showDiscoverModal.value = true
}

async function doDiscover() {
  discovering.value = true
  try {
    const data = await driverApi.discover(currentDriverName.value)
    discoveredDevices.value = data?.devices || []
  } catch (e: any) { message.error(extractError(e, t('driver.discoverFailed'))) }
  finally { discovering.value = false }
}

onMounted(() => { loadDrivers() })
</script>

<style scoped>
.driver-config-page { padding: 16px; }
</style>
