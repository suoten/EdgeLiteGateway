<template>
  <n-spin :show="pageLoading" :description="t('scada.loading')">
  <div class="scada-page">
    <div class="scada-header">
      <div class="header-left">
        <span class="header-title">{{ t('scada.title') }}</span>
        <n-tag v-if="previewMode" type="warning" size="small" round>{{ t('scada.previewMode') }}</n-tag>
      </div>
      <div class="header-actions">
        <n-button size="small" quaternary style="color: #fff" @click="showHelp = true">{{ t('scada.instructions') }}</n-button>
        <n-button-group size="small">
          <n-button quaternary style="color: #fff" @click="undo" :disabled="historyIndex <= 0">
            <template #icon><n-icon :component="ArrowUndoOutline" /></template>
            {{ t('scada.undo') }}
          </n-button>
          <!-- 修复3: 撤销/重做步数指示器 -->
          <n-tooltip>
            <template #trigger>
              <span class="history-step-indicator">{{ historyInfo.index + 1 }}/{{ historyInfo.total }}</span>
            </template>
            {{ t('scada.historyStepTooltip', { cur: historyInfo.index + 1, total: historyInfo.total }) }}
          </n-tooltip>
            <n-button quaternary style="color: #fff" @click="redo" :disabled="historyIndex >= historyStack.length - 1">
            <template #icon><n-icon :component="ArrowRedoOutline" /></template>
            {{ t('scada.redo') }}
          </n-button>
        </n-button-group>
        <n-button size="small" :type="previewMode ? 'warning' : 'default'" style="color: #fff" @click="previewMode = !previewMode">
          <template #icon><n-icon :component="previewMode ? PlayOutline : PlayCircleSharp" /></template>
          {{ previewMode ? t('scada.exitPreview') : t('scada.preview') }}
        </n-button>
        <!-- 修复12: 冻结按钮（仅预览模式可用） -->
        <n-button v-if="previewMode" size="small" :type="frozen ? 'error' : 'default'" style="color: #fff" @click="frozen = !frozen">
          {{ frozen ? t('scada.unfreeze') : t('scada.freeze') }}
        </n-button>
        <!-- 修复15: 声音告警开关（仅预览模式可用） -->
        <n-button v-if="previewMode" size="small" :type="soundAlarmEnabled ? 'warning' : 'default'" style="color: #fff" @click="soundAlarmEnabled = !soundAlarmEnabled">
          {{ soundAlarmEnabled ? t('scada.soundOn') : t('scada.soundOff') }}
        </n-button>
        <!-- 修复14: 操作记录 -->
        <n-button size="small" quaternary style="color: #fff" @click="showOpRecords = true">
          {{ t('scada.operationRecords') }}
        </n-button>
        <n-button size="small" type="primary" :disabled="!auth.isOperator" :loading="saving" @click="saveProject">
          <template #icon><n-icon :component="SaveOutline" /></template>
          {{ t('scada.save') }}
        </n-button>
        <n-button size="small" style="color: #fff" @click="loadProject">
          <template #icon><n-icon :component="Folder" /></template>
          {{ t('scada.load') }}
        </n-button>
        <n-button size="small" quaternary style="color: #fff" @click="exportAsImage">
          <template #icon><n-icon :component="Image" /></template>
          {{ t('scada.exportImage') }}
        </n-button>
        <!-- 修复9: 场景导入导出 -->
        <n-button size="small" quaternary style="color: #fff" @click="exportScene">
          {{ t('scada.exportScene') }}
        </n-button>
        <n-button size="small" quaternary style="color: #fff" @click="triggerSceneImport">
          {{ t('scada.importScene') }}
        </n-button>
        <input type="file" ref="sceneImportInputRef" style="display: none" accept=".json" @change="onSceneImportFileChange" />
      </div>
    </div>

    <!-- 修复5: 多画面管理标签页 -->
    <div v-if="!previewMode" class="scene-tabs-bar">
      <n-tabs
        :value="currentSceneId"
        type="card"
        size="small"
        :addable="!previewMode"
        :closable="scenes.length > 1"
        @update:value="onSceneChange"
        @add="addScene"
        @close="deleteSceneById"
        style="flex: 1"
      >
        <n-tab-pane v-for="scene in scenes" :key="scene.id" :name="scene.id" :closable="scenes.length > 1">
          <template #tab>
            <span class="scene-tab-label">
              <n-icon :component="isFavorite(scene.id) ? Star : StarOutline" :size="12" :color="isFavorite(scene.id) ? '#f0a020' : '#909399'" class="scene-star-icon" @click.stop="toggleFavorite(scene.id, scene.name)" />
              <span>{{ scene.name }}</span>
            </span>
          </template>
        </n-tab-pane>
      </n-tabs>
      <!-- 修复4: 快捷访问下拉——收藏的画面快速跳转 -->
      <n-dropdown :options="quickAccessOptions" trigger="click" @select="onQuickAccessSelect">
        <n-button size="tiny" quaternary style="color: #e0f0ff" :disabled="!quickAccessOptions.length">
          <template #icon><n-icon :component="Star" /></template>
          {{ t('scada.quickAccess') }}
        </n-button>
      </n-dropdown>
      <n-dropdown :options="sceneMenuOptions" trigger="click" @select="onSceneMenuSelect">
        <n-button size="tiny" quaternary style="color: #e0f0ff" :title="t('scada.sceneActions')">⋯</n-button>
      </n-dropdown>
    </div>

    <div class="scada-body">
      <div v-if="!previewMode" class="toolbox">
        <!-- 修复8: 工具箱标签页：组件 / 图层 -->
        <n-tabs type="line" size="small" style="margin-bottom: 8px">
          <n-tab-pane :name="'components'" :tab="t('scada.toolboxTabComponents')">
        <div class="toolbox-title">{{ t('scada.components') }}</div>
        <div class="toolbox-items">
          <div v-for="comp in componentTypes" :key="comp.type" class="toolbox-item" @click="addWidgetManual(comp.type)">
            <div class="tb-icon" :style="{ background: comp.color }"><component :is="(toolboxIcons as Record<string, any>)[comp.type]" /></div>
            <div class="tb-label">{{ comp.label }}</div>
          </div>
        </div>
        <!-- UX-08: 组件模板库——保存当前选中组件为模板，便于复用 -->
        <div class="toolbox-title" style="margin-top: 16px; display: flex; justify-content: space-between; align-items: center;">
          <span>{{ t('scada.templates') }}</span>
          <n-button size="tiny" quaternary type="primary" :disabled="!selectedWidgetId" @click="saveAsTemplate" :title="t('scada.saveAsTemplate')">
            <template #icon><n-icon :component="AddOutline" /></template>
          </n-button>
        </div>
        <div class="toolbox-items">
          <div v-for="tpl in userTemplates" :key="tpl.name" class="toolbox-item" @click="addFromTemplate(tpl)" :title="t('scada.applyTemplate')">
            <div class="tb-icon" :style="{ background: tpl.color || '#667eea' }"><component :is="(toolboxIcons as Record<string, any>)[tpl.type] || (toolboxIcons as Record<string, any>).gauge" /></div>
            <div class="tb-label">
              {{ tpl.name }}
              <n-button size="tiny" text type="error" @click.stop="deleteTemplate(tpl.name)" style="margin-left: 2px">×</n-button>
            </div>
          </div>
          <div v-if="!userTemplates.length" class="dt-empty" style="padding: 8px 0">{{ t('scada.noTemplates') }}</div>
        </div>
        <div class="toolbox-title" style="margin-top: 16px">{{ t('scada.devices') }}</div>
        <n-input v-model:value="deviceSearch" :placeholder="t('scada.searchDevices')" size="tiny" clearable style="margin-bottom: 6px" />
        <div class="device-tree">
          <div v-for="d in filteredDevices" :key="d.device_id" class="dt-device" @click="onSelectDevice(d)">
            <div :class="['dt-device-header', { active: expandedDevice === d.device_id }]">
              <span class="dt-dot" :style="{ background: d.status === 'online' ? '#18a058' : d.status === 'offline' ? '#d03050' : '#666' }"></span>
              <span class="dt-name">{{ d.name }}</span>
            </div>
            <div v-if="expandedDevice === d.device_id" class="dt-points">
              <n-input v-model:value="pointSearch" :placeholder="t('scada.searchDevices')" size="tiny" clearable style="margin: 4px 0" />
              <n-virtual-list :items="filteredPoints" :item-size="28" style="max-height:230px">
                <template #default="{ item: pt }">
                  <div :key="pt.name" class="dt-point" draggable="true" @dragstart="onPointDragStart($event, pt)" @click.stop="addWidgetFromPoint(pt)">
                    <span>{{ pt.name }}</span>
                    <n-button size="tiny" type="primary" quaternary>+</n-button>
                  </div>
                </template>
              </n-virtual-list>
              <div v-if="!filteredPoints.length" class="dt-empty">{{ t('scada.noPoints') }}</div>
            </div>
          </div>
        </div>
          </n-tab-pane>
          <!-- 修复8: 图层管理面板 -->
          <n-tab-pane :name="'layers'" :tab="t('scada.toolboxTabLayers')">
            <div class="toolbox-title">{{ t('scada.layers') }}</div>
            <div v-if="!layerList.length" class="dt-empty" style="padding: 12px 0">{{ t('scada.noLayers') }}</div>
            <div v-else class="layer-list">
              <div
                v-for="layer in layerList"
                :key="layer.id"
                :class="['layer-item', { active: selectedWidgetId === layer.id }]"
                @click="selectLayer(layer.id)"
              >
                <span class="layer-name">{{ layer.name }}</span>
                <span class="layer-type-tag">{{ layer.type }}</span>
                <div class="layer-actions">
                  <n-button size="tiny" text :title="t('scada.layerToggleVisible')" @click.stop="toggleLayerVisible(layer.id)">
                    <n-icon :component="layer.visible ? EyeOutline : EyeOffOutline" :size="14" />
                  </n-button>
                  <n-button size="tiny" text :title="t('scada.layerToggleLock')" @click.stop="toggleLayerLock(layer.id)">
                    <n-icon :component="layer.locked ? LockClosedOutline : LockOpenOutline" :size="14" />
                  </n-button>
                  <n-button size="tiny" text :title="t('scada.layerMoveUp')" @click.stop="moveLayerUp(layer.id)">
                    <n-icon :component="ArrowUpOutline" :size="14" />
                  </n-button>
                  <n-button size="tiny" text :title="t('scada.layerMoveDown')" @click.stop="moveLayerDown(layer.id)">
                    <n-icon :component="ArrowDownOutline" :size="14" />
                  </n-button>
                </div>
              </div>
            </div>
          </n-tab-pane>
        </n-tabs>
      </div>

      <div class="canvas-area" ref="canvasAreaRef" @wheel="onCanvasWheel">
        <div v-if="!previewMode" class="canvas-toolbar">
          <n-button-group size="tiny">
            <n-button style="color: #fff" @click="zoom = Math.min(zoom + 0.1, 2)">{{ t('scada.zoomIn') }}</n-button>
            <n-button style="color: #fff; cursor: default; background: transparent; border-color: #1a2a3a;">{{ Math.round(zoom * 100) }}%</n-button>
            <n-button style="color: #fff" @click="zoom = Math.max(zoom - 0.1, 0.3)">{{ t('scada.zoomOut') }}</n-button>
          </n-button-group>
          <n-button size="tiny" quaternary style="color: #fff" @click="lockAllWidgets">{{ t('scada.lockAll') }}</n-button>
          <n-button size="tiny" quaternary style="color: #fff" @click="unlockAllWidgets">{{ t('scada.unlockAll') }}</n-button>
          <!-- 修复8: 组合/取消组合按钮 -->
          <n-button size="tiny" quaternary style="color: #fff" :disabled="selectedWidgetIds.length < 2" @click="groupSelectedWidgets">{{ t('scada.group') }}</n-button>
          <n-button size="tiny" quaternary style="color: #fff" @click="ungroupSelectedWidgets">{{ t('scada.ungroup') }}</n-button>
        </div>
        <!-- 修复5: 预览模式工具栏——刷新频率指示与选择 -->
        <div v-else class="canvas-toolbar preview-toolbar">
          <n-text style="color: #e0f0ff; font-size: 12px">{{ t('scada.refreshInterval') }}:</n-text>
          <n-select
            :value="refreshIntervalMs"
            :options="refreshIntervalOptions"
            size="tiny"
            style="width: 90px"
            @update:value="(v: number) => refreshIntervalMs = v"
          />
          <n-text v-if="lastRefreshTime" style="color: #909399; font-size: 12px">{{ t('scada.lastRefresh') }}: {{ lastRefreshTime }}</n-text>
        </div>
        <!-- 修复2: 画面切换 fade 过渡动画 -->
        <transition name="scada-fade" mode="out-in">
        <div
          class="scada-canvas"
          ref="canvasRef"
          :key="currentSceneId"
          :class="{ 'preview-mode': previewMode, 'drag-over': isDragOver }"
          :style="{
            transform: `scale(${zoom})`,
            transformOrigin: 'top left',
            backgroundImage: `radial-gradient(circle, ${canvasConfig.gridColor} 1px, transparent 1px)`,
            backgroundSize: `${canvasConfig.gridSpacing}px ${canvasConfig.gridSpacing}px`,
            backgroundColor: canvasConfig.backgroundColor,
            minWidth: canvasConfig.width + 'px',
            minHeight: canvasConfig.height + 'px',
          }"
          @click="onCanvasClick"
          @dragover.prevent="onCanvasDragOver"
          @dragleave="isDragOver = false"
          @drop="onCanvasDrop"
          @scroll.passive="updateViewportRect"
        >
            <div v-if="widgets.length === 0" class="empty-hint">
              <n-icon :component="StatsChartOutline" :size="48" class="empty-icon-svg" />
              <div class="empty-text">{{ t('scada.selectHint') }}</div>
            </div>
          <!-- 修复6: 拖拽对齐参考线 -->
          <div
            v-for="(line, idx) in alignLines" :key="'align-' + idx"
            class="align-line"
            :class="line.type === 'x' ? 'align-line-x' : 'align-line-y'"
            :style="line.type === 'x' ? { left: line.pos + 'px' } : { top: line.pos + 'px' }"
          ></div>
          <div
            v-for="widget in widgets" :key="widget.id"
            :class="['scada-widget', `widget-${widget.type}`, { selected: selectedWidgetId === widget.id && !previewMode, 'multi-selected': selectedWidgetIds.includes(widget.id) && selectedWidgetId !== widget.id && !previewMode, locked: widget.locked && !previewMode, grouped: widget.groupId && !previewMode }]"
            :style="widgetStyle(widget)"
            @pointerdown="startDrag($event, widget)"
            @click.stop="selectWidget(widget)"
            @contextmenu.prevent="onWidgetContextMenu($event, widget)"
          >
            <div v-if="!previewMode && widget.locked" class="widget-lock-badge">
              <n-icon :component="LockClosedOutline" :size="12" />
            </div>
            <div v-if="widget.type === 'gauge'" class="w-gauge">
              <svg viewBox="0 0 120 120" class="gauge-svg">
                <path d="M 15 95 A 50 50 0 1 1 105 95" fill="none" stroke="#1a2a3a" stroke-width="10" stroke-linecap="round" />
                <path d="M 15 95 A 50 50 0 1 1 105 95" fill="none" :stroke="gaugeColor(widget)" stroke-width="10" stroke-linecap="round"
                  :stroke-dasharray="gaugeDash(widget)" />
                <text x="60" y="62" text-anchor="middle" fill="#e0f0ff" font-size="22" font-weight="bold">{{ formatValue(widget) }}</text>
                <text x="60" y="80" text-anchor="middle" fill="#607d8b" font-size="10">{{ widget.unit || '' }}</text>
                <text x="60" y="110" text-anchor="middle" fill="#4fc3f7" font-size="9">{{ widget.label }}</text>
              </svg>
            </div>
            <div v-else-if="widget.type === 'indicator'" class="w-indicator">
              <div :class="['ind-light', { on: getWidgetValue(widget) }]"
                :style="{ background: getWidgetValue(widget) ? '#18a058' : '#333', boxShadow: getWidgetValue(widget) ? '0 0 20px #18a05880, 0 0 40px #18a05840' : 'none' }">
              </div>
              <div class="ind-label">{{ widget.label }}</div>
              <div class="ind-status">{{ getWidgetValue(widget) ? 'ON' : 'OFF' }}</div>
            </div>
            <div v-else-if="widget.type === 'chart'" class="w-chart">
              <div class="chart-title">{{ widget.label }}</div>
              <div class="chart-body"><v-chart :option="getChartOption(widget.id)" autoresize style="height: 100%" /></div>
            </div>
            <div v-else-if="widget.type === 'switch'" class="w-switch">
              <div class="sw-label">{{ widget.label }}</div>
              <n-switch v-model:value="widget.value" @update:value="v => onSwitchChange(widget, v)" :disabled="!previewMode || !auth.isOperator" />
              <div :class="['sw-status', { on: widget.value }]">{{ widget.value ? t('scada.on') : t('scada.off') }}</div>
            </div>
            <div v-else-if="widget.type === 'tank'" class="w-tank">
              <div class="tank-body">
                <div class="tank-fill" :style="{ height: tankPercent(widget) + '%', background: tankColor(widget) }"></div>
              </div>
              <div class="tank-val">{{ formatValue(widget) }}</div>
              <div class="tank-label">{{ widget.label }}</div>
            </div>
            <div v-else-if="widget.type === 'label'" class="w-label">
              {{ widget.label }}
            </div>
            <!-- 修复7: 画面跳转组件——预览模式下点击跳转目标画面 -->
            <div v-else-if="widget.type === 'link'" class="w-link" :class="{ 'link-clickable': previewMode }" @click.stop="previewMode && onSceneLinkClick(widget)">
              <n-icon :component="NavigateOutline" :size="20" color="#9c27b0" />
              <div class="link-label">{{ widget.label }}</div>
              <div class="link-target">{{ getSceneNameById(widget.targetSceneId) }}</div>
            </div>
            <div v-if="!previewMode" class="widget-actions">
              <n-button text class="wa-btn" @click.stop="editWidget(widget)">
                <n-icon :component="AddOutline" :size="14" />
              </n-button>
              <n-button text class="wa-btn wa-del" @click.stop="removeWidget(widget.id)">
                <n-icon :component="CloseOutline" :size="14" />
              </n-button>
            </div>
            <div v-if="!previewMode && selectedWidgetId === widget.id" class="resize-handle" @pointerdown.stop="startResize($event, widget)"></div>
          </div>
        </div>
        </transition>
        <!-- 修复4: 缩略图小地图导航 -->
        <div v-if="!previewMode" class="minimap-container">
          <div v-if="showMinimap" class="minimap-panel" @click="onMinimapClick">
            <svg :width="minimapWidth" :height="minimapHeight" class="minimap-svg">
              <rect v-for="w in minimapWidgets" :key="w.id" :x="w.x" :y="w.y" :width="w.w" :height="w.h" fill="#4fc3f7" fill-opacity="0.3" stroke="#4fc3f7" stroke-width="0.5" />
              <rect :x="viewportRect.x" :y="viewportRect.y" :width="Math.max(1, viewportRect.w)" :height="Math.max(1, viewportRect.h)" fill="none" stroke="#f0c040" stroke-width="1" />
            </svg>
          </div>
          <div class="minimap-toggle" :title="t('scada.minimapToggle')" @click="toggleMinimap">
            <n-icon :component="showMinimap ? CloseOutline : MapOutline" :size="14" />
          </div>
        </div>
      </div>

      <div v-if="!previewMode" class="props-panel">
        <!-- 修复4: 画布属性配置（未选中组件时显示） -->
        <template v-if="!selectedWidgetId">
          <div class="props-title">{{ t('scada.canvasProperties') }}</div>
          <div class="props-body">
            <div class="prop-row"><span class="prop-label">{{ t('scada.backgroundColor') }}</span><n-color-picker v-model:value="canvasConfig.backgroundColor" size="small" :show-alpha="true" /></div>
            <div class="prop-row"><span class="prop-label">{{ t('scada.gridColor') }}</span><n-color-picker v-model:value="canvasConfig.gridColor" size="small" :show-alpha="true" /></div>
            <div class="prop-row"><span class="prop-label">{{ t('scada.gridSpacing') }}</span><n-input-number v-model:value="canvasConfig.gridSpacing" size="tiny" :min="5" :max="100" :step="5" /></div>
            <div class="prop-row"><span class="prop-label">{{ t('scada.canvasWidth') }}</span><n-input-number v-model:value="canvasConfig.width" size="tiny" :min="400" :step="100" /></div>
            <div class="prop-row"><span class="prop-label">{{ t('scada.canvasHeight') }}</span><n-input-number v-model:value="canvasConfig.height" size="tiny" :min="300" :step="100" /></div>
          </div>
        </template>
        <template v-else>
        <div class="props-title">{{ t('scada.properties') }}</div>
        <div class="props-body">
          <div class="prop-row"><span class="prop-label">{{ t('scada.type') }}</span><span class="prop-val">{{ componentTypes.find(c => c.type === editForm.type)?.label }}</span></div>
          <div class="prop-row"><span class="prop-label">{{ t('scada.label') }}</span><n-input v-model:value="editForm.label" size="tiny" @update:value="applyProp" /></div>
            <div class="prop-row"><span class="prop-label">{{ t('scada.device') }}</span><n-select v-model:value="editForm.deviceId" :options="deviceOptions" size="tiny" :placeholder="t('scada.selectDevice')" clearable @update:value="onEditDeviceChange" /></div>
            <div class="prop-row" v-if="editForm.type === 'chart'"><span class="prop-label">{{ t('scada.point') }}</span><n-select v-model:value="editForm.pointNames" :options="editPointOptions" size="tiny" :placeholder="t('scada.selectPoint')" clearable filterable multiple :max-tag-count="2" @update:value="applyProp" /></div>
            <div class="prop-row" v-else><span class="prop-label">{{ t('scada.point') }}</span><n-select v-model:value="editForm.pointName" :options="editPointOptions" size="tiny" :placeholder="t('scada.selectPoint')" clearable filterable @update:value="applyProp" /></div>
          <template v-if="editForm.type === 'gauge' || editForm.type === 'tank'">
            <div class="prop-row"><span class="prop-label">{{ t('scada.minValue') }}</span><n-input-number v-model:value="editForm.min" size="tiny" @update:value="applyProp" /></div>
              <div class="prop-row"><span class="prop-label">{{ t('scada.maxValue') }}</span><n-input-number v-model:value="editForm.max" size="tiny" @update:value="applyProp" /></div>
            <div class="prop-row"><span class="prop-label">{{ t('scada.unit') }}</span><n-input v-model:value="editForm.unit" size="tiny" placeholder="℃ MPa %" @update:value="applyProp" /></div>
          </template>
          <!-- 修复7: 画面跳转组件目标画面选择 -->
          <div v-if="editForm.type === 'link'" class="prop-row">
            <span class="prop-label">{{ t('scada.targetScene') }}</span>
            <n-select v-model:value="editForm.targetSceneId" :options="sceneOptions" size="tiny" :placeholder="t('scada.selectScene')" @update:value="applyProp" />
          </div>
          <div class="prop-row"><span class="prop-label">{{ t('scada.width') }}</span><n-input-number v-model:value="editForm.w" size="tiny" :min="60" :step="10" @update:value="applyProp" /></div>
            <div class="prop-row"><span class="prop-label">{{ t('scada.height') }}</span><n-input-number v-model:value="editForm.h" size="tiny" :min="60" :step="10" @update:value="applyProp" /></div>
          <div class="prop-row"><span class="prop-label">{{ t('scada.lock') }}</span><n-switch v-model:value="editForm.locked" size="small" @update:value="applyProp" /></div>
          <!-- 修复7: 旋转角度 -->
          <div class="prop-row">
            <span class="prop-label">{{ t('scada.rotation') }}</span>
            <n-input-number v-model:value="editForm.rotation" size="tiny" :min="0" :max="360" :step="15" @update:value="applyProp" />
          </div>
          <div class="prop-row">
            <span class="prop-label">{{ t('scada.rotationQuick') }}</span>
            <n-space :size="4">
              <n-button size="tiny" @click="setRotation(0)">0°</n-button>
              <n-button size="tiny" @click="setRotation(90)">90°</n-button>
              <n-button size="tiny" @click="setRotation(180)">180°</n-button>
              <n-button size="tiny" @click="setRotation(270)">270°</n-button>
            </n-space>
          </div>
          <!-- 修复10: 组件样式编辑 -->
          <div class="prop-row">
            <span class="prop-label">{{ t('scada.styleColor') }}</span>
            <n-color-picker :value="(editForm.style && editForm.style.color) || ''" size="small" :show-alpha="true" @update:value="(v: string) => updateStyleField('color', v)" />
          </div>
          <div class="prop-row">
            <span class="prop-label">{{ t('scada.styleFontSize') }}</span>
            <n-input-number :value="(editForm.style && editForm.style.fontSize) || 0" size="small" :min="0" :max="72" @update:value="(v: number | null) => updateStyleField('fontSize', v ?? 0)" />
          </div>
          <div class="prop-row">
            <span class="prop-label">{{ t('scada.styleBorder') }}</span>
            <n-select
              :value="(editForm.style && editForm.style.border) || ''"
              :options="[
                { label: t('scada.borderNone'), value: '' },
                { label: t('scada.borderSolid'), value: 'solid' },
                { label: t('scada.borderDashed'), value: 'dashed' },
                { label: t('scada.borderDotted'), value: 'dotted' },
              ]"
              size="tiny"
              @update:value="(v: string) => updateStyleField('border', v)"
            />
          </div>
        </div>
        </template>
      </div>
    </div>

    <!-- 修复14: 操作记录抽屉 -->
    <n-modal v-model:show="showOpRecords" preset="card" :title="t('scada.operationRecords')" style="width: 720px; max-width: 95vw" :close-on-esc="true" :auto-focus="true" :close-on-esc-aria-label="t('common.closeDialog')">
      <n-data-table :columns="opRecordColumns" :data="opRecords" :loading="opRecordsLoading" size="small" :pagination="{ pageSize: 20, pageSizes: [10, 20, 50, 100], showSizePicker: true }" />
    </n-modal>

    <n-modal v-model:show="showHelp" preset="card" :title="t('scada.helpTitle')" style="width: 520px; max-width: 95vw" :close-on-esc="true" :auto-focus="true" :close-on-esc-aria-label="t('common.closeDialog')">  <!-- FIXED: 原问题-中文硬编码 -->
      <n-space vertical>
        <n-alert type="info" :title="t('scada.whatIsScada')">{{ t('scada.whatIsScadaDesc') }}</n-alert>  <!-- FIXED: 原问题-中文硬编码 -->
        <div style="font-size: 14px; line-height: 1.8">
          <strong>{{ t('scada.stepsTitle') }}</strong>  <!-- FIXED: 原问题-中文硬编码 -->
          <ol style="padding-left: 20px; margin: 8px 0">
            <li>{{ t('scada.step1') }}</li>  <!-- FIXED: 原问题-中文硬编码 -->
            <li>{{ t('scada.step2') }}</li>  <!-- FIXED: 原问题-中文硬编码 -->
            <li>{{ t('scada.step3') }}</li>  <!-- FIXED: 原问题-中文硬编码 -->
            <li>{{ t('scada.step4') }}</li>  <!-- FIXED: 原问题-中文硬编码 -->
            <li>{{ t('scada.step5') }}</li>  <!-- FIXED: 原问题-中文硬编码 -->
            <li>{{ t('scada.step6') }}</li>  <!-- FIXED: 原问题-中文硬编码 -->
          </ol>
          <strong>{{ t('scada.shortcutsTitle') }}</strong>  <!-- FIXED: 原问题-中文硬编码 -->
          <table style="width: 100%; margin-top: 4px; font-size: 13px">
            <tr><td style="color: #4fc3f7; width: 140px">Ctrl + Z</td><td>{{ t('scada.undo') }}</td></tr>  <!-- FIXED: 原问题-中文硬编码 -->
            <tr><td style="color: #4fc3f7">Ctrl + Y</td><td>{{ t('scada.redo') }}</td></tr>  <!-- FIXED: 原问题-中文硬编码 -->
            <tr><td style="color: #4fc3f7">Ctrl + C</td><td>{{ t('scada.copyComponent') }}</td></tr>  <!-- FIXED: 原问题-中文硬编码 -->
            <tr><td style="color: #4fc3f7">Ctrl + V</td><td>{{ t('scada.pasteComponent') }}</td></tr>  <!-- FIXED: 原问题-中文硬编码 -->
            <tr><td style="color: #4fc3f7">Ctrl + D</td><td>{{ t('scada.duplicateComponent') }}</td></tr>  <!-- FIXED: 原问题-中文硬编码 -->
            <tr><td style="color: #4fc3f7">Ctrl + S</td><td>{{ t('scada.saveProject') }}</td></tr>  <!-- FIXED: 原问题-中文硬编码 -->
            <tr><td style="color: #4fc3f7">Delete</td><td>{{ t('scada.deleteSelected') }}</td></tr>  <!-- FIXED: 原问题-中文硬编码 -->
          </table>
        </div>
      </n-space>
    </n-modal>
    <!-- 修复5: 画面重命名弹窗 -->
    <n-modal v-model:show="showRenameScene" preset="dialog" :title="t('scada.sceneRenameTitle')" style="width: 400px; max-width: 95vw" :close-on-esc="true" :auto-focus="true" :close-on-esc-aria-label="t('common.closeDialog')">
      <n-input v-model:value="renameSceneName" :placeholder="t('scada.sceneNamePlaceholder')" @keyup.enter="confirmRenameScene" />
      <template #action>
        <n-button @click="showRenameScene = false">{{ t('scada.cancel') }}</n-button>
        <n-button type="primary" @click="confirmRenameScene">{{ t('scada.confirm') }}</n-button>
      </template>
    </n-modal>
    <input type="file" ref="fileInputRef" style="display: none" accept=".json" @change="onFileLoad" />
    <!-- 修复6: 组件右键上下文菜单 -->
    <n-dropdown
      placement="bottom-start"
      trigger="manual"
      :show="contextMenuShow"
      :options="contextMenuOptions"
      :x="contextMenuX"
      :y="contextMenuY"
      @select="onContextMenuSelect"
      @clickoutside="contextMenuShow = false"
    />
  </div>
  </n-spin>
</template>

<script setup lang="ts">
import { ref, shallowRef, triggerRef, computed, onMounted, onUnmounted, watch, onBeforeUnmount, markRaw, h, reactive } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import {
  NButton, NButtonGroup, NSpace, NInput, NSelect, NTag, NSwitch, NModal,
  NInputNumber, NAlert, NSpin, NIcon, NVirtualList, NTabs, NTabPane, NDropdown, NTooltip,
  NColorPicker,
} from 'naive-ui'
import {
  OptionsSharp, BulbOutline, StatsChartOutline, PowerOutline,
  CubeSharp, Text, SaveOutline, Folder,
  Image, ArrowUndoOutline, ArrowRedoOutline, PlayCircleSharp, RefreshOutline, PlayOutline,
  TimerOutline, AddOutline, CloseOutline, LockClosedOutline, NavigateOutline, MapOutline,
  EyeOutline, EyeOffOutline, LockOpenOutline, ArrowUpOutline, ArrowDownOutline,
  StarOutline, Star,
} from '@vicons/ionicons5'
import { use } from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import VChart from 'vue-echarts'
import { deviceApi, scadaApi } from '@/api'
import { protocolLabel } from '@/utils/enumLabels'
import { t } from '@/i18n'  // FIXED: 原问题-#注释导致编译失败，改为//注释
import { extractError } from '@/utils/errorCodes'
import { message, dialog } from '@/utils/discreteApi'
import { useAuthStore } from '@/stores/auth'
import { usePageVisibility } from '@/composables/usePageVisibility'
// [AUDIT-FIX] 严重-1: 暗色模式适配
import { useChartTheme } from '@/composables/useChartTheme'

// FIXED: 原问题-ScadaEditor.vue全部中文硬编码，改为i18n

use([LineChart, GridComponent, TooltipComponent, CanvasRenderer])

const auth = useAuthStore()
// [AUDIT-FIX] 严重-1: 暗色模式适配
const { chartAxisColor, chartSplitLineColor, chartTooltipAxis, chartCategoryAxis, chartValueAxis } = useChartTheme()

const _h = h  // FIX: 生产构建中 h() 在模板里调用报 "h is not defined"，改为模板中使用预创建VNode
const _markRaw = markRaw
const toolboxIcons = _markRaw({
  gauge: _h(NIcon, { component: OptionsSharp, size: 18 }),
  indicator: _h(NIcon, { component: BulbOutline, size: 18 }),
  chart: _h(NIcon, { component: StatsChartOutline, size: 18 }),
  switch: _h(NIcon, { component: PowerOutline, size: 18 }),
  tank: _h(NIcon, { component: CubeSharp, size: 18 }),
  label: _h(NIcon, { component: Text, size: 18 }),
  link: _h(NIcon, { component: NavigateOutline, size: 18 }),
})
const toolboxColors: Record<string, string> = {
  gauge: '#18a058', indicator: '#f0c040', chart: '#667eea',
  switch: '#e8804c', tank: '#4fc3f7', label: '#90a4ae', link: '#9c27b0',
}
const canvasRef = ref<HTMLElement | null>(null)
const canvasAreaRef = ref<HTMLElement | null>(null)
const fileInputRef = ref<HTMLInputElement | null>(null)
const previewMode = ref(false)
const showHelp = ref(false)
// 修复12: 冻结按钮
const frozen = ref(false)
// 修复15: 声音告警开关
const soundAlarmEnabled = ref(false)
const lastAlarmSoundAt = ref(0)
// 修复14: 操作记录
const showOpRecords = ref(false)
const opRecords = ref<any[]>([])
const opRecordsLoading = ref(false)
const opRecordColumns = computed(() => [
  { title: t('common.time'), key: 'time', width: 180 },
  { title: t('scada.opRecordUser'), key: 'user', width: 100 },
  { title: t('scada.opRecordAction'), key: 'action' },
])
function logOpRecord(action: string) {
  opRecords.value.unshift({
    time: new Date().toLocaleString(),
    user: auth.username || '-',
    action,
  })
  if (opRecords.value.length > 200) opRecords.value.pop()
}
// 修复13: 告警阈值高亮
const alarmThresholds = ref<Record<string, { min?: number; max?: number }>>({})
function isWidgetAlarming(widget: any): boolean {
  if (widget.value === undefined || widget.value === null) return false
  const threshold = alarmThresholds.value[widget.id]
  if (!threshold) return false
  const v = Number(widget.value)
  if (isNaN(v)) return false
  if (threshold.min !== undefined && v < threshold.min) return true
  if (threshold.max !== undefined && v > threshold.max) return true
  return false
}
// [AUDIT-FIX] 复用单例 AudioContext，避免每次播放新建实例导致浏览器实例上限耗尽泄漏
let _audioCtx: AudioContext | null = null
function getAudioCtx(): AudioContext | null {
  if (!_audioCtx) {
    const AudioCtx = (window as any).AudioContext || (window as any).webkitAudioContext
    if (!AudioCtx) return null
    _audioCtx = new AudioCtx()
  }
  return _audioCtx
}
function playAlarmSound() {
  if (!soundAlarmEnabled.value) return
  const now = Date.now()
  // 节流：5秒内不重复播放
  if (now - lastAlarmSoundAt.value < 5000) return
  lastAlarmSoundAt.value = now
  try {
    const ctx = getAudioCtx()
    if (!ctx) return
    const osc = ctx.createOscillator()
    const gain = ctx.createGain()
    osc.connect(gain)
    gain.connect(ctx.destination)
    osc.type = 'sine'
    osc.frequency.value = 880
    gain.gain.setValueAtTime(0.3, ctx.currentTime)
    gain.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.5)
    osc.start()
    osc.stop(ctx.currentTime + 0.5)
  } catch { /* ignore audio errors */ }
}
const devices = ref<any[]>([])
const deviceSearch = ref('')
const expandedDevice = ref<string | null>(null)
const currentDevicePoints = ref<any[]>([])
const pointSearch = ref('')  // FIXED: 测点级搜索，OPC UA 等设备数百测点需搜索定位
const pointValues = shallowRef<Record<string, Record<string, any>>>({})
const widgets = ref<any[]>([])
const selectedWidgetId = ref<number | null>(null)
// 修复8: 多选与分组——Ctrl+click 多选，Ctrl+G 组合，Ctrl+Shift+G 取消组合
const selectedWidgetIds = ref<number[]>([])
let groupCounter = 0
// 修复5: 多画面管理
interface ScadaScene { id: string; name: string; widgets: any[]; canvasConfig?: CanvasConfig }
// 修复4: 画布属性配置
interface CanvasConfig {
  backgroundColor: string
  gridColor: string
  gridSpacing: number
  width: number
  height: number
}
const defaultCanvasConfig = (): CanvasConfig => ({
  backgroundColor: '#0a0f1a',
  gridColor: '#1a2a3a',
  gridSpacing: 20,
  width: 2000,
  height: 1200,
})
const canvasConfig = reactive<CanvasConfig>(defaultCanvasConfig())
const scenes = ref<ScadaScene[]>([])
const currentSceneId = ref<string>('')
// 修复6: 拖拽对齐参考线
const alignLines = ref<Array<{ type: 'x' | 'y'; pos: number }>>([])
// 画面重命名弹窗
const showRenameScene = ref(false)
const renameSceneName = ref('')
const chartData = shallowRef<Map<number, Map<string, { time: number; value: number }[]>>>(new Map())
const zoom = ref(1)
const isDragOver = ref(false)
const editForm = ref<any>({})
const editPointOptions = ref<{ label: string; value: string }[]>([])
const pageLoading = ref(true)
const saving = ref(false)
const dirty = ref(false)

// 修复4: 缩略图小地图导航
const showMinimap = ref(false)
const minimapWidth = 200
const minimapHeight = 150
const viewportRect = ref({ x: 0, y: 0, w: 0, h: 0 })

const canvasBounds = computed(() => {
  if (!widgets.value.length) return { minX: 0, minY: 0, maxX: 1000, maxY: 600 }
  let minX = Infinity, minY = Infinity, maxX = 0, maxY = 0
  for (const w of widgets.value) {
    minX = Math.min(minX, w.x)
    minY = Math.min(minY, w.y)
    maxX = Math.max(maxX, w.x + w.w)
    maxY = Math.max(maxY, w.y + w.h)
  }
  return { minX: Math.min(minX, 0), minY: Math.min(minY, 0), maxX: maxX + 50, maxY: maxY + 50 }
})

const minimapScale = computed(() => {
  const b = canvasBounds.value
  const sx = minimapWidth / Math.max(1, b.maxX - b.minX)
  const sy = minimapHeight / Math.max(1, b.maxY - b.minY)
  return Math.min(sx, sy)
})

const minimapWidgets = computed(() => {
  const b = canvasBounds.value
  const s = minimapScale.value
  return widgets.value.map(w => ({
    id: w.id,
    x: (w.x - b.minX) * s,
    y: (w.y - b.minY) * s,
    w: Math.max(1, w.w * s),
    h: Math.max(1, w.h * s),
  }))
})

function updateViewportRect() {
  const el = canvasRef.value
  if (!el) return
  const b = canvasBounds.value
  const s = minimapScale.value
  viewportRect.value = {
    x: (el.scrollLeft - b.minX) * s,
    y: (el.scrollTop - b.minY) * s,
    w: el.clientWidth * s,
    h: el.clientHeight * s,
  }
}

function toggleMinimap() {
  showMinimap.value = !showMinimap.value
  if (showMinimap.value) updateViewportRect()
}

function onMinimapClick(e: MouseEvent) {
  const el = canvasRef.value
  if (!el) return
  const target = e.currentTarget as HTMLElement
  const rect = target.getBoundingClientRect()
  const clickX = e.clientX - rect.left
  const clickY = e.clientY - rect.top
  const b = canvasBounds.value
  const s = minimapScale.value
  const canvasX = clickX / s + b.minX
  const canvasY = clickY / s + b.minY
  el.scrollLeft = canvasX - el.clientWidth / 2
  el.scrollTop = canvasY - el.clientHeight / 2
  updateViewportRect()
}

let widgetIdCounter = 0
let dragging: any = null
let resizing: any = null
let refreshTimer: any = null
// 页面可见性检测：页面隐藏时暂停轮询，恢复可见时立即刷新并恢复调度
const { isVisible } = usePageVisibility()
let historyStack: string[] = []
let historyIndex = -1
const historyInfo = ref({ index: 0, total: 0 })  // 修复3: 响应式步数显示
let clipboard: any = null
// 拖拽/缩放过程中的全局监听器引用，用于组件卸载时清理
// FIXED-Touch: 使用 PointerEvent 替代 MouseEvent，统一处理鼠标、触摸、手写笔输入
let _activeMouseMove: ((ev: PointerEvent) => void) | null = null
let _activeMouseUp: (() => void) | null = null

function _clearDragListeners() {
  if (_activeMouseMove) { document.removeEventListener('pointermove', _activeMouseMove); _activeMouseMove = null }
  if (_activeMouseUp) { document.removeEventListener('pointerup', _activeMouseUp); _activeMouseUp = null }
}

function pushHistory() {
  const snapshot = JSON.stringify(widgets.value)
  historyStack = historyStack.slice(0, historyIndex + 1)
  historyStack.push(snapshot)
  if (historyStack.length > 50) historyStack.shift()
  historyIndex = historyStack.length - 1
  historyInfo.value = { index: historyIndex, total: historyStack.length }
}

function undo() {
  if (historyIndex <= 0) return
  historyIndex--
  widgets.value = JSON.parse(historyStack[historyIndex])
  selectedWidgetId.value = null
  selectedWidgetIds.value = []
  historyInfo.value = { index: historyIndex, total: historyStack.length }
}

function redo() {
  if (historyIndex >= historyStack.length - 1) return
  historyIndex++
  widgets.value = JSON.parse(historyStack[historyIndex])
  selectedWidgetId.value = null
  selectedWidgetIds.value = []
  historyInfo.value = { index: historyIndex, total: historyStack.length }
}

function copyWidget() {
  if (!selectedWidgetId.value) return
  const w = widgets.value.find(w => w.id === selectedWidgetId.value)
  if (w) { clipboard = { ...w }; message.success(t('scada.copied')) }  // FIXED: 原问题-中文硬编码
}

function pasteWidget() {
  if (!clipboard) return
  widgetIdCounter++
  const newWidget = { ...clipboard, id: widgetIdCounter, x: clipboard.x + 20, y: clipboard.y + 20 }
  widgets.value.push(newWidget)
  selectedWidgetId.value = widgetIdCounter
  pushHistory()
  message.success(t('scada.pasted'))  // FIXED: 原问题-中文硬编码
}

function duplicateWidget() {
  const w = widgets.value.find(w => w.id === selectedWidgetId.value)
  if (!w) return
  widgetIdCounter++
  const newWidget = { ...w, id: widgetIdCounter, x: w.x + 20, y: w.y + 20 }
  widgets.value.push(newWidget)
  selectedWidgetId.value = widgetIdCounter
  pushHistory()
}

function deleteSelected() {
  if (previewMode.value) return
  // 修复8: 支持多选删除
  const idsToDelete = selectedWidgetIds.value.length > 1 ? [...selectedWidgetIds.value] : (selectedWidgetId.value ? [selectedWidgetId.value] : [])
  if (!idsToDelete.length) return
  // [AUDIT-FIX] 严重级-批量删除 widget 属不可撤销操作，添加二次确认
  dialog.warning({
    title: t('common.confirmDelete'),
    content: t('common.confirmDeleteDesc'),
    positiveText: t('common.confirm'),
    negativeText: t('common.cancel'),
    onPositiveClick: () => {
      const idSet = new Set(idsToDelete)
      widgets.value = widgets.value.filter(w => !idSet.has(w.id))
      for (const id of idsToDelete) chartData.value.delete(id)
      selectedWidgetId.value = null
      selectedWidgetIds.value = []
      pushHistory()
    },
  })
}

// 修复6: 右键上下文菜单
const contextMenuShow = ref(false)
const contextMenuX = ref(0)
const contextMenuY = ref(0)
const contextMenuWidgetId = ref<number | null>(null)

const contextMenuOptions = computed(() => {
  if (contextMenuWidgetId.value == null) return []
  const w = widgets.value.find(x => x.id === contextMenuWidgetId.value)
  if (!w) return []
  return [
    { label: t('scada.copyComponent'), key: 'copy' },
    { label: t('scada.duplicateComponent'), key: 'duplicate' },
    { type: 'divider' as const, key: 'd1' },
    { label: w.locked ? (t('scada.unlock')) : (t('scada.lock')), key: 'lock' },
    { label: t('scada.bringToFront'), key: 'front' },
    { label: t('scada.sendToBack'), key: 'back' },
    { type: 'divider' as const, key: 'd2' },
    { label: t('scada.properties'), key: 'properties' },
    { label: t('scada.deleteSelected'), key: 'delete' },
  ]
})

function onWidgetContextMenu(e: MouseEvent, widget: any) {
  if (previewMode.value) return
  e.preventDefault()
  contextMenuWidgetId.value = widget.id
  selectedWidgetId.value = widget.id
  editForm.value = { ...widget }
  if (widget.deviceId) onEditDeviceChange(widget.deviceId)
  contextMenuX.value = e.clientX
  contextMenuY.value = e.clientY
  contextMenuShow.value = true
}

function bringToFront(id: number) {
  const idx = widgets.value.findIndex(w => w.id === id)
  if (idx < 0 || idx === widgets.value.length - 1) return
  const [w] = widgets.value.splice(idx, 1)
  widgets.value.push(w)
  pushHistory()
}

function sendToBack(id: number) {
  const idx = widgets.value.findIndex(w => w.id === id)
  if (idx <= 0) return
  const [w] = widgets.value.splice(idx, 1)
  widgets.value.unshift(w)
  pushHistory()
}

function toggleWidgetLock(id: number) {
  const w = widgets.value.find(x => x.id === id)
  if (!w) return
  w.locked = !w.locked
  if (selectedWidgetId.value === id) editForm.value = { ...w }
  pushHistory()
}

function onContextMenuSelect(key: string) {
  const id = contextMenuWidgetId.value
  contextMenuShow.value = false
  if (id == null) return
  if (key === 'copy') copyWidget()
  else if (key === 'duplicate') { selectedWidgetId.value = id; duplicateWidget() }
  else if (key === 'lock') toggleWidgetLock(id)
  else if (key === 'front') bringToFront(id)
  else if (key === 'back') sendToBack(id)
  else if (key === 'properties') { const w = widgets.value.find(x => x.id === id); if (w) editWidget(w) }
  else if (key === 'delete') { selectedWidgetId.value = id; removeWidget(id) }
}

function exportAsImage() {
  if (!canvasRef.value) return
  const canvas = document.createElement('canvas')
  const rect = canvasRef.value.getBoundingClientRect()
  canvas.width = rect.width * 2
  canvas.height = rect.height * 2
  const ctx = canvas.getContext('2d')!
  ctx.scale(2, 2)
  ctx.fillStyle = '#0a0f1a'
  ctx.fillRect(0, 0, rect.width, rect.height)
  ctx.fillStyle = '#1a2a3a'
  for (let x = 0; x < rect.width; x += 20) {
    for (let y = 0; y < rect.height; y += 20) {
      ctx.beginPath()
      ctx.arc(x, y, 0.8, 0, Math.PI * 2)
      ctx.fill()
    }
  }
  widgets.value.forEach(w => {
    ctx.fillStyle = '#0d1520'
    ctx.strokeStyle = '#1a2a3a'
    ctx.lineWidth = 1
    ctx.beginPath()
    ctx.roundRect(w.x, w.y, w.w, w.h, 8)
    ctx.fill()
    ctx.stroke()
    ctx.fillStyle = '#e0f0ff'
    ctx.font = '12px sans-serif'
    ctx.textAlign = 'center'
    ctx.fillText(w.label, w.x + w.w / 2, w.y + w.h / 2)
    if (w.type === 'gauge' || w.type === 'tank') {
      const val = formatValue(w)
      ctx.font = 'bold 18px sans-serif'
      ctx.fillText(val, w.x + w.w / 2, w.y + w.h / 2 + 18)
    }
  })
  const url = canvas.toDataURL('image/png')
  const a = document.createElement('a')
  a.href = url
  a.download = `scada-${Date.now()}.png`
  a.click()
}

function onKeyDown(e: KeyboardEvent) {
  if (previewMode.value) return
  if (e.key === 'Delete' || e.key === 'Backspace') { if (selectedWidgetId.value) { e.preventDefault(); deleteSelected() } }
  else if (e.ctrlKey && e.key === 'z') { e.preventDefault(); undo() }
  else if (e.ctrlKey && e.key === 'y') { e.preventDefault(); redo() }
  else if (e.ctrlKey && e.key === 'c') { copyWidget() }
  else if (e.ctrlKey && e.key === 'v') { pasteWidget() }
  else if (e.ctrlKey && e.key === 'd') { e.preventDefault(); duplicateWidget() }
  else if (e.ctrlKey && e.key === 's') { e.preventDefault(); saveProject() }
  // 修复8: Ctrl+G 组合，Ctrl+Shift+G 取消组合
  else if (e.ctrlKey && (e.key === 'g' || e.key === 'G')) {
    e.preventDefault()
    if (e.shiftKey) ungroupSelectedWidgets()
    else groupSelectedWidgets()
  }
}

const componentTypes = [
  { type: 'gauge', label: t('scada.gauge'), color: toolboxColors.gauge },
  { type: 'indicator', label: t('scada.indicator'), color: toolboxColors.indicator },
  { type: 'chart', label: t('scada.chart'), color: toolboxColors.chart },
  { type: 'switch', label: t('scada.switchCtrl'), color: toolboxColors.switch },
  { type: 'tank', label: t('scada.tank'), color: toolboxColors.tank },
  { type: 'label', label: t('scada.textLabel'), color: toolboxColors.label },
  // 修复7: 画面跳转组件——预览模式下点击跳转到目标画面
  { type: 'link', label: t('scada.sceneLink'), color: toolboxColors.link },
]

// UX-08: 用户组件模板库——持久化到 localStorage，跨会话复用
interface WidgetTemplate {
  name: string
  type: string
  color?: string
  w: number
  h: number
  min: number
  max: number
  unit: string
}
const TEMPLATE_STORAGE_KEY = 'scada_widget_templates'
const userTemplates = ref<WidgetTemplate[]>([])

function loadTemplates() {
  try {
    const raw = localStorage.getItem(TEMPLATE_STORAGE_KEY)
    if (raw) userTemplates.value = JSON.parse(raw)
  } catch { userTemplates.value = [] }
}
function persistTemplates() {
  try { localStorage.setItem(TEMPLATE_STORAGE_KEY, JSON.stringify(userTemplates.value)) } catch { /* ignore quota */ }
}
function saveAsTemplate() {
  const w = widgets.value.find(x => x.id === selectedWidgetId.value)
  if (!w) return
  const comp = componentTypes.find(c => c.type === w.type)
  const name = `${comp?.label || w.type}_${userTemplates.value.length + 1}`
  userTemplates.value.push({ name, type: w.type, color: comp?.color, w: w.w, h: w.h, min: w.min, max: w.max, unit: w.unit || '' })
  persistTemplates()
  message.success(t('scada.templateSaved', { name }))
}
function addFromTemplate(tpl: WidgetTemplate) {
  widgetIdCounter++
  widgets.value.push({
    id: widgetIdCounter, type: tpl.type, label: tpl.name, deviceId: null, deviceName: '',
    pointName: null, unit: tpl.unit || '', min: tpl.min, max: tpl.max,
    x: 20 + (widgets.value.length % 5) * 170, y: 20 + Math.floor(widgets.value.length / 5) * 150,
    w: tpl.w, h: tpl.h,
    value: tpl.type === 'switch' ? false : tpl.type === 'indicator' ? false : 0,
    locked: false,
    rotation: 0,  // 修复7: 旋转角度
    style: { color: '', fontSize: 0, border: '' },  // 修复10: 组件样式
  })
  selectedWidgetId.value = widgetIdCounter
  pushHistory()
}
function deleteTemplate(name: string) {
  // [AUDIT-FIX] 严重级-删除模板需二次确认
  dialog.warning({
    title: t('scada.deleteTemplateConfirm'),
    content: t('scada.deleteTemplateContent', { name }),
    positiveText: t('common.confirm'),
    negativeText: t('common.cancel'),
    onPositiveClick: () => {
      userTemplates.value = userTemplates.value.filter(t => t.name !== name)
      persistTemplates()
      message.success(t('common.deleteSuccess'))
    },
  })
}
loadTemplates()

const filteredDevices = computed(() => {
  if (!deviceSearch.value) return devices.value
  const q = deviceSearch.value.toLowerCase()
  return devices.value.filter(d => d.name.toLowerCase().includes(q) || d.device_id.toLowerCase().includes(q))
})

// FIXED: 测点级搜索过滤，按 name 模糊匹配
const filteredPoints = computed(() => {
  if (!pointSearch.value) return currentDevicePoints.value
  const q = pointSearch.value.toLowerCase()
  return currentDevicePoints.value.filter(pt => (pt.name || '').toLowerCase().includes(q))
})

const deviceOptions = computed(() =>
  devices.value.map(d => ({ label: `${d.name} (${protocolLabel.value[d.protocol] || d.protocol})`, value: d.device_id }))
)

function widgetStyle(widget: any) {
  const rotation = widget.rotation ?? 0
  const style: Record<string, string> = {
    left: widget.x + 'px',
    top: widget.y + 'px',
    width: widget.w + 'px',
    height: widget.h + 'px',
  }
  if (rotation) style.transform = `rotate(${rotation}deg)`
  // 修复10: 应用组件样式
  const ws = widget.style
  if (ws) {
    if (ws.color) style.color = ws.color
    if (ws.fontSize) style.fontSize = ws.fontSize + 'px'
    if (ws.border) {
      const borderMap: Record<string, string> = {
        solid: '1px solid #4fc3f7',
        dashed: '1px dashed #4fc3f7',
        dotted: '1px dotted #4fc3f7',
      }
      if (borderMap[ws.border]) style.border = borderMap[ws.border]
    }
  }
  // 修复13: 告警阈值高亮（仅预览模式）
  if (previewMode.value && isWidgetAlarming(widget)) {
    style.boxShadow = '0 0 8px 2px #d03050'
    style.border = '2px solid #d03050'
  }
  return style
}

function gaugeColor(widget: any) {
  const pct = gaugePercent(widget)
  if (pct > 80) return '#d03050'
  if (pct > 60) return '#f0c040'
  return '#18a058'
}

function gaugePercent(widget: any) {
  const val = Number(getWidgetValue(widget)) || 0
  const min = widget.min ?? 0
  const max = widget.max ?? 100
  return Math.min(100, Math.max(0, ((val - min) / (max - min)) * 100))
}

function gaugeDash(widget: any) {
  const pct = gaugePercent(widget)
  const arc = 235.5
  return `${arc * pct / 100} ${arc}`
}

function tankPercent(widget: any) {
  return gaugePercent(widget)
}

function tankColor(widget: any) {
  const pct = tankPercent(widget)
  if (pct > 80) return '#d03050'
  if (pct > 60) return '#f0c040'
  return '#4fc3f7'
}

const chartOptionCache = computed(() => {
  const cache = new Map<number, any>()
  const seriesColors = ['#667eea', '#18a058', '#f0c040', '#e8804c', '#4fc3f7', '#d03050']
  for (const w of widgets.value) {
    if (w.type !== 'chart') continue
    const seriesMap = chartData.value.get(w.id)
    const pointNames: string[] = w.pointNames || (w.pointName ? [w.pointName] : [])
    // 以第一个测点的时间轴作为 x 轴（同设备同周期采样，时间近似对齐）
    const firstArr = pointNames.length ? (seriesMap?.get(pointNames[0]) || []) : []
    const times = firstArr.map(d => {
      const dt = new Date(d.time)
      return `${String(dt.getHours()).padStart(2, '0')}:${String(dt.getMinutes()).padStart(2, '0')}:${String(dt.getSeconds()).padStart(2, '0')}`
    })
    const series = pointNames.map((pn, idx) => {
      const arr = seriesMap?.get(pn) || []
      const color = seriesColors[idx % seriesColors.length]
      return {
        type: 'line', name: pn, data: arr.map(d => d.value), smooth: true, symbol: 'none',
        itemStyle: { color },
        areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: color + '4D' }, { offset: 1, color: color + '05' }] } },
      }
    })
    cache.set(w.id, {
      // [AUDIT-FIX] 严重-1: 暗色模式适配
      grid: { left: 35, right: 8, top: 8, bottom: 22 },
      tooltip: chartTooltipAxis(),
      xAxis: chartCategoryAxis({ data: times, axisLabel: { fontSize: 9, color: chartAxisColor.value }, show: firstArr.length > 0 }),
      yAxis: chartValueAxis({ axisLabel: { fontSize: 9, color: chartAxisColor.value }, splitLine: { lineStyle: { color: chartSplitLineColor.value } }, show: firstArr.length > 0 }),
      series,
    })
  }
  return cache
})

// FIXED: 提供 getChartOption 函数供模板调用，从 chartOptionCache 获取图表配置
function getChartOption(widgetId: number) {
  return chartOptionCache.value.get(widgetId) || {}
}

function onEditDeviceChange(deviceId: string) {
  if (editForm.value.type === 'chart') {
    editForm.value.pointNames = []
  } else {
    editForm.value.pointName = null
  }
  if (!deviceId) { editPointOptions.value = []; return }
  const device = devices.value.find(d => d.device_id === deviceId)
  // FIXED: 原问题-device?.points?.map(...)后链式调用不安全，改为(device?.points ?? []).map(...)
  editPointOptions.value = (device?.points ?? []).map((p: any) => ({ label: `${p.name} (${p.unit || '-'})`, value: p.name }))
  applyProp()
}

function applyProp() {
  const idx = widgets.value.findIndex(w => w.id === editForm.value.id)
  if (idx >= 0) {
    const device = devices.value.find(d => d.device_id === editForm.value.deviceId)
    widgets.value[idx] = { ...widgets.value[idx], ...editForm.value, deviceName: device?.name || '' }
  }
}

// 修复10: 更新组件样式字段
function updateStyleField(field: 'color' | 'fontSize' | 'border', value: any) {
  if (!selectedWidgetId.value) return
  const idx = widgets.value.findIndex(w => w.id === selectedWidgetId.value)
  if (idx < 0) return
  if (!widgets.value[idx].style) widgets.value[idx].style = { color: '', fontSize: 0, border: '' }
  widgets.value[idx].style[field] = value
  editForm.value = { ...widgets.value[idx] }
  pushHistory()
}

async function fetchDevices() {
  try {
    // FIXED: 原 size=200 硬编码，超过 200 台设备无法显示。后端 PaginationParams 上限 5000，放宽至 200。
    // FIX-PERF3: 限制分页大小为 200，避免大数据量全量加载导致性能问题
    const data = await deviceApi.list({ page: 1, size: 200 })
    devices.value = data?.data ?? []
  } catch {
    message.warning(t('scada.fetchDevicesFailed'))  // FIXED: 原问题-中文硬编码
  }
}

async function onSelectDevice(device: any) {
  expandedDevice.value = expandedDevice.value === device.device_id ? null : device.device_id
  if (expandedDevice.value !== device.device_id) return
  try {
    const data = await deviceApi.getPoints(device.device_id)
    if (Array.isArray(data)) currentDevicePoints.value = data
    else if (data && typeof data === 'object') currentDevicePoints.value = Object.entries(data).map(([name, info]: [string, any]) => ({ name, ...(typeof info === 'object' ? info : { value: info }) }))
    else currentDevicePoints.value = device.points || []
    await fetchPointValues(device.device_id)
  } catch (e) {
    currentDevicePoints.value = device.points || []
    console.warn('Failed to fetch device points:', e)  // FIXED: 原问题-硬编码中文label
  }
}

async function fetchPointValues(deviceId: string) {
  try {
    const data = await deviceApi.getPoints(deviceId)
    if (data) {
      pointValues.value[deviceId] = data
      // FIXED-BugR7: 移除 triggerRef(pointValues)，避免分批请求过程中多次全量重渲染
      // 模板通过 getWidgetValue 读取 widget.value（由 updateWidgetValues 更新），
      // 值变化守卫 if (w.value !== numVal) w.value = numVal 真正生效，仅值变化的图元重绘
    }
  } catch (e) {
    console.warn('Failed to get device point values:', deviceId, e)
  }
}

async function refreshAllValues() {
  // 修复12: 冻结状态下跳过刷新
  if (frozen.value) return
  const deviceIds = new Set<string>()
  widgets.value.forEach(w => { if (w.deviceId) deviceIds.add(w.deviceId) })
  // FIXED-P0: 原问题-串行await导致多设备刷新耗时超过5秒，改为并行请求
  // FIXED-Bug23: 大量设备时无并发限制，浏览器同源连接上限（HTTP/1.1 约6个）导致请求排队
  // 改为分批并发控制，每批最多 6 个并行请求，避免压垮后端和浏览器连接池
  const CONCURRENCY = 6
  const idList = [...deviceIds]
  for (let i = 0; i < idList.length; i += CONCURRENCY) {
    const batch = idList.slice(i, i + CONCURRENCY)
    await Promise.allSettled(batch.map(id => fetchPointValues(id)))
  }
  updateWidgetValues()
  // 修复13/15: 检测告警阈值并播放声音
  let hasAlarm = false
  for (const w of widgets.value) {
    if (isWidgetAlarming(w)) { hasAlarm = true; break }
  }
  if (hasAlarm) playAlarmSound()
}

// FIXED-P0: 原问题-setInterval不等待async完成，多设备时刷新周期重叠导致数据错乱
// 改为递归setTimeout确保上一次刷新完成后再排下一次
// 修复5: 刷新频率可配置（1s/5s/10s/30s），默认 5s
let isRefreshing = false
const refreshIntervalMs = ref(5000)
const lastRefreshTime = ref<string>('')
const refreshIntervalOptions = computed(() => [
  { label: t('scada.refreshInterval1s'), value: 1000 },
  { label: t('scada.refreshInterval5s'), value: 5000 },
  { label: t('scada.refreshInterval10s'), value: 10000 },
  { label: t('scada.refreshInterval30s'), value: 30000 },
])
function scheduleRefresh() {
  if (!isVisible.value) return  // 页面不可见时暂停调度
  refreshTimer = setTimeout(async () => {
    refreshTimer = null
    if (isRefreshing) { scheduleRefresh(); return }
    isRefreshing = true
    try { await refreshAllValues(); lastRefreshTime.value = new Date().toLocaleTimeString() } finally { isRefreshing = false }
    scheduleRefresh()
  }, refreshIntervalMs.value)
}

function updateWidgetValues() {
  widgets.value.forEach(w => {
    if (w.type === 'chart') {
      // chart 支持多测点：遍历 pointNames，每个测点维护独立时序数据
      const pointNames: string[] = w.pointNames || (w.pointName ? [w.pointName] : [])
      if (!w.deviceId || !pointNames.length) return
      let seriesMap = chartData.value.get(w.id)
      if (!seriesMap) { seriesMap = new Map(); chartData.value.set(w.id, seriesMap) }
      let lastVal: number | null = null
      for (const pn of pointNames) {
        const val = pointValues.value[w.deviceId]?.[pn]?.value ?? pointValues.value[w.deviceId]?.[pn]
        if (val !== undefined && val !== null) {
          const numVal = Number(val)
          const arr = seriesMap.get(pn) || []
          arr.push({ time: Date.now(), value: numVal })
          if (arr.length > 60) arr.splice(0, arr.length - 60)
          seriesMap.set(pn, arr)
          lastVal = numVal
        }
      }
      if (lastVal !== null) {
        triggerRef(chartData)
        if (w.value !== lastVal) w.value = lastVal
      }
      return
    }
    if (w.deviceId && w.pointName) {
      const val = pointValues.value[w.deviceId]?.[w.pointName]?.value ?? pointValues.value[w.deviceId]?.[w.pointName]
      if (val !== undefined && val !== null) {
        const numVal = Number(val)
        // FIXED-P1: 原问题-每5秒全量变更所有widget触发重渲染；添加值变化守卫，仅在实际变化时更新
        if (w.type === 'gauge' || w.type === 'tank') {
          if (w.value !== numVal) w.value = numVal
        }
        else if (w.type === 'indicator') {
          if (w.value !== !!val) w.value = !!val
        }
      }
    }
  })
}

function addWidgetFromPoint(pt: any, dropX?: number, dropY?: number) {
  const isBool = pt.type === 'bool' || pt.name.toLowerCase().includes('switch') || pt.name.toLowerCase().includes('status')
  const type = isBool ? 'indicator' : 'gauge'
  const device = devices.value.find(d => d.device_id === expandedDevice.value)
  if (!device) return
  widgetIdCounter++
  widgets.value.push({
    id: widgetIdCounter, type, label: pt.name, deviceId: device.device_id, deviceName: device.name,
    pointName: pt.name, pointNames: undefined,
    unit: pt.unit || '', min: 0, max: 100,
    x: dropX ?? 20 + (widgets.value.length % 5) * 170, y: dropY ?? 20 + Math.floor(widgets.value.length / 5) * 150,
    w: 150, h: 150,
    value: pointValues.value[device.device_id]?.[pt.name]?.value ?? (isBool ? false : 0),
    locked: false,
    rotation: 0,  // 修复7: 旋转角度
    style: { color: '', fontSize: 0, border: '' },  // 修复10: 组件样式
  })
  pushHistory()
  message.success(t('scada.addedPoint', { name: pt.name }))  // FIXED: 原问题-中文硬编码
}

function addWidgetManual(type: string) {
  widgetIdCounter++
  const comp = componentTypes.find(c => c.type === type)
  widgets.value.push({
    id: widgetIdCounter, type, label: comp?.label || type, deviceId: null, deviceName: '',
    pointName: null, pointNames: type === 'chart' ? [] : undefined,
    unit: '', min: 0, max: 100,
    x: 20 + (widgets.value.length % 5) * 170, y: 20 + Math.floor(widgets.value.length / 5) * 150,
    w: type === 'chart' ? 300 : type === 'label' ? 120 : type === 'link' ? 140 : 150,
    h: type === 'chart' ? 180 : type === 'label' ? 40 : type === 'link' ? 60 : 150,
    value: type === 'switch' ? false : type === 'indicator' ? false : 0,
    locked: false,
    // 修复7: 画面跳转组件目标画面
    targetSceneId: type === 'link' ? (scenes.value[0]?.id || '') : undefined,
    rotation: 0,  // 修复7: 旋转角度
    style: { color: '', fontSize: 0, border: '' },  // 修复10: 组件样式
  })
  selectedWidgetId.value = widgetIdCounter
  pushHistory()
  logOpRecord(t('scada.opAddWidget', { type: comp?.label || type }))
}

function removeWidget(id: number) {
  const widget = widgets.value.find(w => w.id === id)
  dialog.warning({  // FIXED: 原问题-中文硬编码
    title: t('scada.confirmDelete'),
    content: t('scada.confirmDeleteContent', { name: widget?.label || String(id) }),
    positiveText: t('scada.confirmDelete'),
    negativeText: t('scada.cancel'),
    onPositiveClick: () => {
      widgets.value = widgets.value.filter(w => w.id !== id)
      chartData.value.delete(id)
      if (selectedWidgetId.value === id) selectedWidgetId.value = null
      pushHistory()
    },
  })
}

function selectWidget(widget: any) {
  if (previewMode.value) return
  // 修复8: Ctrl+click 多选切换
  if (window.event && (window.event as MouseEvent).ctrlKey) {
    const idx = selectedWidgetIds.value.indexOf(widget.id)
    if (idx >= 0) selectedWidgetIds.value.splice(idx, 1)
    else selectedWidgetIds.value.push(widget.id)
    // 多选时以最后选中的为主选中（驱动属性面板）
    selectedWidgetId.value = selectedWidgetIds.value.length ? selectedWidgetIds.value[selectedWidgetIds.value.length - 1] : null
    if (selectedWidgetId.value) {
      const w = widgets.value.find(x => x.id === selectedWidgetId.value)
      if (w) { editForm.value = { ...w }; if (w.deviceId) onEditDeviceChange(w.deviceId) }
    }
    return
  }
  selectedWidgetId.value = widget.id
  selectedWidgetIds.value = [widget.id]
  editForm.value = { ...widget }
  if (widget.deviceId) onEditDeviceChange(widget.deviceId)
}

// 修复8: 组合选中组件——分配相同 groupId
function groupSelectedWidgets() {
  if (selectedWidgetIds.value.length < 2) { message.warning(t('scada.groupSelected')); return }
  groupCounter++
  const gid = 'group_' + groupCounter
  widgets.value.forEach(w => {
    if (selectedWidgetIds.value.includes(w.id)) w.groupId = gid
  })
  pushHistory()
  message.success(t('scada.group'))
}

// 修复8: 取消组合——清除选中组件的 groupId
function ungroupSelectedWidgets() {
  const ids = new Set(selectedWidgetIds.value)
  let found = false
  widgets.value.forEach(w => {
    if (ids.has(w.id) && w.groupId) { w.groupId = undefined; found = true }
  })
  if (found) { pushHistory(); message.success(t('scada.ungroup')) }
}

function onCanvasClick() { selectedWidgetId.value = null; selectedWidgetIds.value = [] }

function onCanvasWheel(e: WheelEvent) {
  if (!e.ctrlKey) return
  e.preventDefault()
  const delta = e.deltaY > 0 ? -0.1 : 0.1
  zoom.value = Math.min(2, Math.max(0.3, +(zoom.value + delta).toFixed(2)))
}

function lockAllWidgets() {
  widgets.value.forEach(w => { w.locked = true })
  if (selectedWidgetId.value) {
    const w = widgets.value.find(x => x.id === selectedWidgetId.value)
    if (w) editForm.value = { ...w }
  }
  pushHistory()
}

function unlockAllWidgets() {
  widgets.value.forEach(w => { w.locked = false })
  if (selectedWidgetId.value) {
    const w = widgets.value.find(x => x.id === selectedWidgetId.value)
    if (w) editForm.value = { ...w }
  }
  pushHistory()
}

// 迁移旧 chart widget：pointName → pointNames（兼容旧数据）
function migrateChartWidgets(ws: any[]): any[] {
  ws.forEach((w: any) => {
    if (w.type === 'chart' && w.pointName && !w.pointNames) {
      w.pointNames = [w.pointName]
    }
  })
  return ws
}

// ============== 修复5: 多画面管理 ==============

// 将当前 widgets 同步到当前画面（切换/保存前调用）
function syncCurrentSceneWidgets() {
  const cur = scenes.value.find(s => s.id === currentSceneId.value)
  if (cur) {
    cur.widgets = widgets.value.map(w => ({ ...w }))
    // 修复4: 同步画布属性配置到当前画面
    cur.canvasConfig = { ...canvasConfig }
  }
}

// 新建画面
function addScene() {
  syncCurrentSceneWidgets()
  const id = 'scene_' + Date.now()
  const name = t('scada.scenes') + ' ' + (scenes.value.length + 1)
  // 修复4: 新画面使用默认画布配置
  const newConfig = defaultCanvasConfig()
  scenes.value.push({ id, name, widgets: [], canvasConfig: newConfig })
  currentSceneId.value = id
  widgets.value = []
  Object.assign(canvasConfig, newConfig)
  selectedWidgetId.value = null
  pushHistory()
  message.success(t('scada.sceneCreated'))
}

// 切换画面
function onSceneChange(newId: string) {
  if (newId === currentSceneId.value) return
  syncCurrentSceneWidgets()
  currentSceneId.value = newId
  const target = scenes.value.find(s => s.id === newId)
  widgets.value = target ? target.widgets.map(w => ({ ...w })) : []
  // 修复4: 加载目标画面的画布配置
  if (target?.canvasConfig) {
    Object.assign(canvasConfig, target.canvasConfig)
  } else {
    Object.assign(canvasConfig, defaultCanvasConfig())
  }
  selectedWidgetId.value = null
  pushHistory()
}

// 删除画面
function deleteSceneById(id: string) {
  if (scenes.value.length <= 1) {
    message.warning(t('scada.sceneDeleteLastError'))
    return
  }
  const scene = scenes.value.find(s => s.id === id)
  if (!scene) return
  dialog.warning({
    title: t('scada.deleteScene'),
    content: t('scada.sceneDeleteConfirm', { name: scene.name }),
    positiveText: t('scada.confirm'),
    negativeText: t('scada.cancel'),
    onPositiveClick: () => {
      const idx = scenes.value.findIndex(s => s.id === id)
      if (idx < 0) return
      scenes.value.splice(idx, 1)
      if (currentSceneId.value === id) {
        const next = scenes.value[0]
        currentSceneId.value = next.id
        widgets.value = next.widgets.map(w => ({ ...w }))
        // 修复4: 切换到下一画面时加载其画布配置
        Object.assign(canvasConfig, next.canvasConfig || defaultCanvasConfig())
        selectedWidgetId.value = null
      }
      pushHistory()
      message.success(t('scada.sceneDeleted'))
    },
  })
}

// 复制画面
function duplicateScene() {
  syncCurrentSceneWidgets()
  const cur = scenes.value.find(s => s.id === currentSceneId.value)
  if (!cur) return
  const id = 'scene_' + Date.now()
  const name = cur.name + ' ' + t('scada.duplicateScene')
  const copiedWidgets = cur.widgets.map(w => ({ ...w, id: ++widgetIdCounter }))
  // 修复4: 复制画布配置
  const copiedConfig = { ...canvasConfig }
  scenes.value.push({ id, name, widgets: copiedWidgets, canvasConfig: copiedConfig })
  currentSceneId.value = id
  widgets.value = copiedWidgets.map(w => ({ ...w }))
  Object.assign(canvasConfig, copiedConfig)
  selectedWidgetId.value = null
  pushHistory()
  message.success(t('scada.sceneDuplicated', { name: cur.name }))
}

// 重命名画面
function openRenameScene() {
  const cur = scenes.value.find(s => s.id === currentSceneId.value)
  if (!cur) return
  renameSceneName.value = cur.name
  showRenameScene.value = true
}

function confirmRenameScene() {
  const name = renameSceneName.value.trim()
  if (!name) {
    message.warning(t('scada.sceneNameEmpty'))
    return
  }
  const cur = scenes.value.find(s => s.id === currentSceneId.value)
  if (cur) {
    cur.name = name
    pushHistory()
    message.success(t('scada.sceneRenamed'))
  }
  showRenameScene.value = false
}

// 画面操作下拉菜单
const sceneMenuOptions = computed(() => [
  { label: t('scada.renameScene'), key: 'rename' },
  { label: t('scada.duplicateScene'), key: 'duplicate' },
  { type: 'divider' as const, key: 'd1' },
  { label: t('scada.deleteScene'), key: 'delete', disabled: scenes.value.length <= 1 },
])

function onSceneMenuSelect(key: string) {
  if (key === 'rename') openRenameScene()
  else if (key === 'duplicate') duplicateScene()
  else if (key === 'delete') deleteSceneById(currentSceneId.value)
}

// 修复7: 画面跳转组件辅助函数
const sceneOptions = computed(() => scenes.value.map(s => ({ label: s.name, value: s.id })))
function getSceneNameById(id: string | undefined): string {
  if (!id) return ''
  return scenes.value.find(s => s.id === id)?.name || ''
}
function onSceneLinkClick(widget: any) {
  if (!widget.targetSceneId) { message.warning(t('scada.selectScene')); return }
  if (widget.targetSceneId === currentSceneId.value) return
  onSceneChange(widget.targetSceneId)
}

// 修复4: 画面收藏/快捷访问——收藏存入 localStorage，顶部下拉快速跳转
const FAVORITE_SCENES_KEY = 'scada_favorite_scenes'
const favoriteScenes = ref<string[]>(loadFavoriteScenes())
function loadFavoriteScenes(): string[] {
  try {
    const raw = localStorage.getItem(FAVORITE_SCENES_KEY)
    return raw ? JSON.parse(raw) : []
  } catch { return [] }
}
function persistFavoriteScenes() {
  try { localStorage.setItem(FAVORITE_SCENES_KEY, JSON.stringify(favoriteScenes.value)) } catch { /* ignore */ }
}
function isFavorite(sceneId: string): boolean {
  return favoriteScenes.value.includes(sceneId)
}
function toggleFavorite(sceneId: string, sceneName?: string) {
  const idx = favoriteScenes.value.indexOf(sceneId)
  if (idx >= 0) {
    favoriteScenes.value.splice(idx, 1)
    message.info(t('scada.unfavoriteScene') + (sceneName ? ': ' + sceneName : ''))
  } else {
    favoriteScenes.value.push(sceneId)
    message.success(t('scada.favoriteScene') + (sceneName ? ': ' + sceneName : ''))
  }
  persistFavoriteScenes()
}
const quickAccessOptions = computed(() => {
  const favs = favoriteScenes.value
    .map(id => scenes.value.find(s => s.id === id))
    .filter((s): s is ScadaScene => !!s)
  return favs.map(s => ({ label: '★ ' + s.name, key: s.id }))
})
function onQuickAccessSelect(key: string) {
  if (!previewMode.value) onSceneChange(key)
}

// 修复8: 图层管理——上移/下移调整 z-index（通过数组顺序）
function moveLayerUp(id: number) {
  const idx = widgets.value.findIndex(w => w.id === id)
  if (idx < 0 || idx === widgets.value.length - 1) return
  const arr = [...widgets.value]
  ;[arr[idx], arr[idx + 1]] = [arr[idx + 1], arr[idx]]
  widgets.value = arr
  pushHistory()
}
function moveLayerDown(id: number) {
  const idx = widgets.value.findIndex(w => w.id === id)
  if (idx <= 0) return
  const arr = [...widgets.value]
  ;[arr[idx], arr[idx - 1]] = [arr[idx - 1], arr[idx]]
  widgets.value = arr
  pushHistory()
}
function toggleLayerVisible(id: number) {
  const w = widgets.value.find(x => x.id === id)
  if (!w) return
  w.visible = w.visible === false ? true : false
  pushHistory()
}
function toggleLayerLock(id: number) {
  const w = widgets.value.find(x => x.id === id)
  if (!w) return
  w.locked = !w.locked
  pushHistory()
}
function selectLayer(id: number) {
  const w = widgets.value.find(x => x.id === id)
  if (!w) return
  selectWidget(w)
}
// 图层列表（按 z-index 倒序显示，最上层在最前）
const layerList = computed(() => {
  return [...widgets.value].reverse().map(w => ({
    id: w.id,
    name: w.label || w.type,
    type: w.type,
    visible: w.visible !== false,
    locked: !!w.locked,
  }))
})

// 修复9: 场景导入导出
function exportScene() {
  syncCurrentSceneWidgets()
  const exportData = {
    version: '1.0',
    exported_at: new Date().toISOString(),
    scenes: scenes.value,
  }
  const blob = new Blob([JSON.stringify(exportData, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `scada-scene-${new Date().toISOString().slice(0, 10)}.json`
  a.click()
  URL.revokeObjectURL(url)
  message.success(t('scada.sceneExported'))
}
const sceneImportInputRef = ref<HTMLInputElement | null>(null)
function triggerSceneImport() {
  sceneImportInputRef.value?.click()
}
function onSceneImportFileChange(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  // [AUDIT-FIX] 严重-S2: 文件上传需校验大小，避免大文件导致浏览器 OOM
  const MAX_IMPORT_SIZE = 10 * 1024 * 1024  // 10MB
  if (file.size > MAX_IMPORT_SIZE) {
    message.error(t('scada.importFileTooLarge', { size: '10MB' }))
    input.value = ''
    return
  }
  const reader = new FileReader()
  reader.onload = (ev) => {
    try {
      const data = JSON.parse(ev.target?.result as string)
      if (Array.isArray(data.scenes) && data.scenes.length) {
        scenes.value = data.scenes.map((s: any) => ({
          id: s.id || 'scene_' + Date.now(),
          name: s.name || t('scada.sceneNameDefault'),
          widgets: migrateChartWidgets(s.widgets || []),
          canvasConfig: s.canvasConfig || defaultCanvasConfig(),
        }))
        widgetIdCounter = Math.max(0, ...scenes.value.flatMap(s => s.widgets.map((w: any) => w.id)))
        currentSceneId.value = scenes.value[0].id
        widgets.value = scenes.value[0].widgets.map((w: any) => ({ ...w }))
        Object.assign(canvasConfig, scenes.value[0].canvasConfig || defaultCanvasConfig())
        selectedWidgetId.value = null
        pushHistory()
        message.success(t('scada.sceneImported'))
      } else if (Array.isArray(data.widgets)) {
        // 兼容仅导出 widgets 的情况
        const ws = migrateChartWidgets(data.widgets)
        widgetIdCounter = Math.max(0, ...ws.map((w: any) => w.id))
        widgets.value = ws.map((w: any) => ({ ...w }))
        const cur = scenes.value.find(s => s.id === currentSceneId.value)
        if (cur) {
          cur.widgets = widgets.value.map(w => ({ ...w }))
        }
        selectedWidgetId.value = null
        pushHistory()
        message.success(t('scada.sceneImported'))
      } else {
        message.error(t('scada.sceneImportFailed'))
      }
    } catch {
      message.error(t('scada.sceneImportFailed'))
    }
  }
  reader.readAsText(file)
  input.value = ''
}

// 修复7: 快捷旋转
function setRotation(deg: number) {
  if (!selectedWidgetId.value) return
  const w = widgets.value.find(x => x.id === selectedWidgetId.value)
  if (!w) return
  w.rotation = deg
  editForm.value = { ...w }
  pushHistory()
}

// ============== 修复6: 拖拽对齐参考线 ==============

// 计算对齐参考线并吸附位置（5px 阈值）
function calcAlignLines(widget: any) {
  const lines: Array<{ type: 'x' | 'y'; pos: number }> = []
  const threshold = 5
  const dragged = {
    left: widget.x, cx: widget.x + widget.w / 2, right: widget.x + widget.w,
    top: widget.y, cy: widget.y + widget.h / 2, bottom: widget.y + widget.h,
  }
  for (const other of widgets.value) {
    if (other.id === widget.id) continue
    const o = {
      left: other.x, cx: other.x + other.w / 2, right: other.x + other.w,
      top: other.y, cy: other.y + other.h / 2, bottom: other.y + other.h,
    }
    // 检查 x 方向对齐（左/中/右）
    let xAligned = false
    const xChecks: Array<[string, number]> = [['left', dragged.left], ['cx', dragged.cx], ['right', dragged.right]]
    for (const [dKey, dVal] of xChecks) {
      if (xAligned) break
      for (const oVal of [o.left, o.cx, o.right]) {
        if (Math.abs(dVal - oVal) < threshold) {
          lines.push({ type: 'x', pos: oVal })
          if (dKey === 'left') widget.x = oVal
          else if (dKey === 'cx') widget.x = oVal - widget.w / 2
          else widget.x = oVal - widget.w
          xAligned = true
          break
        }
      }
    }
    // 检查 y 方向对齐（上/中/下）
    let yAligned = false
    const yChecks: Array<[string, number]> = [['top', dragged.top], ['cy', dragged.cy], ['bottom', dragged.bottom]]
    for (const [dKey, dVal] of yChecks) {
      if (yAligned) break
      for (const oVal of [o.top, o.cy, o.bottom]) {
        if (Math.abs(dVal - oVal) < threshold) {
          lines.push({ type: 'y', pos: oVal })
          if (dKey === 'top') widget.y = oVal
          else if (dKey === 'cy') widget.y = oVal - widget.h / 2
          else widget.y = oVal - widget.h
          yAligned = true
          break
        }
      }
    }
  }
  return lines
}

// HTML5 拖放：从设备树拖拽测点到画布
function onPointDragStart(e: DragEvent, pt: any) {
  const device = devices.value.find(d => d.device_id === expandedDevice.value)
  e.dataTransfer?.setData('application/json', JSON.stringify({ deviceId: device?.device_id, pointName: pt.name, point: pt }))
  if (e.dataTransfer) e.dataTransfer.effectAllowed = 'copy'
}

function onCanvasDragOver(e: DragEvent) {
  if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy'
  isDragOver.value = true
}

function onCanvasDrop(e: DragEvent) {
  e.preventDefault()
  isDragOver.value = false
  const raw = e.dataTransfer?.getData('application/json')
  if (!raw) return
  try {
    const data = JSON.parse(raw)
    if (!data.point) return
    const rect = canvasRef.value?.getBoundingClientRect()
    if (!rect) return
    // 考虑缩放比例，将屏幕坐标转换为画布坐标并对齐网格
    const x = Math.max(0, Math.round((e.clientX - rect.left) / zoom.value / 10) * 10)
    const y = Math.max(0, Math.round((e.clientY - rect.top) / zoom.value / 10) * 10)
    addWidgetFromPoint(data.point, x, y)
  } catch { /* ignore invalid drop data */ }
}

function editWidget(widget: any) {
  selectedWidgetId.value = widget.id
  editForm.value = { ...widget }
  if (widget.deviceId) onEditDeviceChange(widget.deviceId)
}

async function onSwitchChange(widget: any, value: boolean) {
  if (!auth.isOperator) { message.warning(t('common.permissionDenied')); return }
  if (widget.deviceId && widget.pointName) {
    dialog.warning({  // FIXED: 原问题-中文硬编码
      title: t('scada.confirmOperation'), content: t('scada.writeConfirmContent', { device: widget.deviceName || widget.deviceId, label: widget.label, action: value ? t('scada.actionOn') : t('scada.actionOff') }),
      positiveText: t('scada.confirm'), negativeText: t('scada.cancel'),
      onPositiveClick: async () => {
        try { await deviceApi.writePoint(widget.deviceId, widget.pointName, value); message.success(t('scada.operationSuccess', { action: value ? t('scada.actionOn') : t('scada.actionOff'), label: widget.label })) }  // FIXED: 原问题-中文硬编码
        catch (e: any) { message.error(extractError(e, t('scada.operationFailed'))); widget.value = !value }  // FIXED: 原问题-中文硬编码
      },
      onNegativeClick: () => { widget.value = !value },
    })
  }
}

function getWidgetValue(widget: any): any {
  // FIXED-BugR7: 只返回 widget.value，不再读取 pointValues，避免触发全量重渲染
  // pointValues 由 updateWidgetValues 同步到 widget.value，值变化守卫真正生效
  return widget.value
}

function formatValue(widget: any): string {
  const val = getWidgetValue(widget)
  if (typeof val === 'number') return val.toFixed(1)
  return String(val ?? '-')
}

function startDrag(e: PointerEvent, widget: any) {
  if (previewMode.value) return
  if (widget.locked) return
  const rect = canvasRef.value!.getBoundingClientRect()
  // 修复8: 分组拖拽——同 groupId 的组件一起移动
  const groupPeers = widget.groupId ? widgets.value.filter(w => w.groupId === widget.groupId && w.id !== widget.id && !w.locked) : []
  const peerOffsets = groupPeers.map(w => ({ w, offsetX: e.clientX / zoom.value - rect.left / zoom.value - w.x, offsetY: e.clientY / zoom.value - rect.top / zoom.value - w.y }))
  dragging = { widget, offsetX: e.clientX / zoom.value - rect.left / zoom.value - widget.x, offsetY: e.clientY / zoom.value - rect.top / zoom.value - widget.y, peers: peerOffsets }
  _clearDragListeners()
  _activeMouseMove = (ev: PointerEvent) => {
    if (dragging) {
      const r = canvasRef.value!.getBoundingClientRect()
      dragging.widget.x = Math.max(0, Math.round((ev.clientX / zoom.value - r.left / zoom.value - dragging.offsetX) / 10) * 10)
      dragging.widget.y = Math.max(0, Math.round((ev.clientY / zoom.value - r.top / zoom.value - dragging.offsetY) / 10) * 10)
      // 同步移动分组内其他组件
      if (dragging.peers) {
        for (const p of dragging.peers) {
          p.w.x = Math.max(0, Math.round((ev.clientX / zoom.value - r.left / zoom.value - p.offsetX) / 10) * 10)
          p.w.y = Math.max(0, Math.round((ev.clientY / zoom.value - r.top / zoom.value - p.offsetY) / 10) * 10)
        }
      }
      // 修复6: 计算对齐参考线并吸附
      alignLines.value = calcAlignLines(dragging.widget)
    }
  }
  _activeMouseUp = () => { dragging = null; alignLines.value = []; _clearDragListeners() }
  document.addEventListener('pointermove', _activeMouseMove)
  document.addEventListener('pointerup', _activeMouseUp)
}

function startResize(e: PointerEvent, widget: any) {
  if (widget.locked) return
  const startW = widget.w, startH = widget.h, startX = e.clientX, startY = e.clientY
  _clearDragListeners()
  _activeMouseMove = (ev: PointerEvent) => {
    widget.w = Math.max(60, Math.round((startW + (ev.clientX - startX) / zoom.value) / 10) * 10)
    widget.h = Math.max(40, Math.round((startH + (ev.clientY - startY) / zoom.value) / 10) * 10)
  }
  _activeMouseUp = () => { _clearDragListeners() }
  document.addEventListener('pointermove', _activeMouseMove)
  document.addEventListener('pointerup', _activeMouseUp)
}

async function saveProject() {
  if (!auth.isOperator) { message.warning(t('common.permissionDenied')); return }
  saving.value = true
  try {
    // 修复5: 保存前同步当前画面 widgets 到 scenes
    syncCurrentSceneWidgets()
    await scadaApi.saveProject({ name: 'default', widgets: widgets.value, scenes: scenes.value })
    localStorage.setItem('scada-project', JSON.stringify({ widgets: widgets.value, scenes: scenes.value }))
    dirty.value = false
    message.success(t('scada.savedToServer'))  // FIXED: 原问题-中文硬编码
  } catch {
    localStorage.setItem('scada-project', JSON.stringify({ widgets: widgets.value, scenes: scenes.value }))
    dirty.value = false
    message.warning(t('scada.saveFailedLocal'))  // FIXED: 原问题-中文硬编码
  } finally {
    saving.value = false
  }
}

function loadProject() { fileInputRef.value?.click() }

function onFileLoad(e: Event) {
  const file = (e.target as HTMLInputElement).files?.[0]
  if (!file) return
  // [AUDIT-FIX] 严重-S2: 文件上传需校验大小，避免大文件导致浏览器 OOM
  const MAX_IMPORT_SIZE = 10 * 1024 * 1024  // 10MB
  if (file.size > MAX_IMPORT_SIZE) {
    message.error(t('scada.importFileTooLarge', { size: '10MB' }))
    ;(e.target as HTMLInputElement).value = ''
    return
  }
  const reader = new FileReader()
  reader.onload = (ev) => {
    try {
      const data = JSON.parse(ev.target?.result as string)
      // 修复5: 兼容 scenes 结构加载
      if (Array.isArray(data.scenes) && data.scenes.length) {
        scenes.value = data.scenes.map((s: any) => ({
          id: s.id || 'scene_' + Date.now(),
          name: s.name || t('scada.sceneNameDefault'),
          widgets: migrateChartWidgets(s.widgets || []),
          canvasConfig: s.canvasConfig || defaultCanvasConfig(),
        }))
        widgetIdCounter = Math.max(...scenes.value.flatMap(s => s.widgets.map((w: any) => w.id)), 0)
        currentSceneId.value = scenes.value[0].id
        widgets.value = scenes.value[0].widgets.map((w: any) => ({ ...w }))
        // 修复4: 加载首画面的画布配置
        Object.assign(canvasConfig, scenes.value[0].canvasConfig || defaultCanvasConfig())
        message.success(t('scada.loadedComponents', { count: widgets.value.length }))
      } else if (Array.isArray(data.widgets) && data.widgets.length) {
        // 兼容旧数据：无 scenes 字段，将 widgets 包装为默认画面
        const ws = migrateChartWidgets(data.widgets)
        widgetIdCounter = Math.max(...ws.map((w: any) => w.id), 0)
        const defaultScene: ScadaScene = { id: 'scene_' + Date.now(), name: t('scada.sceneNameDefault'), widgets: ws }
        scenes.value = [defaultScene]
        currentSceneId.value = defaultScene.id
        widgets.value = ws.map((w: any) => ({ ...w }))
        message.success(t('scada.loadedComponents', { count: ws.length }))
      }  // FIXED: 原问题-中文硬编码
    } catch { message.error(t('scada.fileFormatError')) }  // FIXED: 原问题-中文硬编码
  }
  reader.readAsText(file)
  ;(e.target as HTMLInputElement).value = ''
}

// 修复5: 预览模式切换时同步进入/退出全屏
watch(previewMode, (val) => {
  if (val) {
    selectedWidgetId.value = null
    refreshAllValues()
    // 进入预览模式时请求全屏
    requestEditorFullscreen()
  } else {
    // 退出预览模式时退出全屏
    exitEditorFullscreen()
  }
})

// 修复3: 全屏状态变化时仅退出全屏，保持预览模式不变
// 原问题：ESC 退出全屏会直接关闭预览模式，用户需重新进入预览
// 修复后：ESC 仅退出全屏，预览模式保持，用户可通过按钮手动退出预览
function onFullscreenChange() {
  const isFullscreen = !!document.fullscreenElement
  if (!isFullscreen && previewMode.value) {
    // 保持预览模式，仅退出全屏，提示用户可通过按钮退出预览
    message.info(t('scada.previewFullscreenExited'))
  }
}

function requestEditorFullscreen() {
  const el = canvasRef.value
  if (!el) return
  try {
    if (el.requestFullscreen) {
      el.requestFullscreen()
    } else if ((el as any).webkitRequestFullscreen) {
      (el as any).webkitRequestFullscreen()
    }
  } catch (e) {
    // 全屏失败不阻塞预览模式
    console.warn('[ScadaEditor] requestFullscreen failed:', e)
  }
}

function exitEditorFullscreen() {
  try {
    if (document.fullscreenElement && document.exitFullscreen) {
      document.exitFullscreen()
    } else if ((document as any).webkitExitFullscreen) {
      (document as any).webkitExitFullscreen()
    }
  } catch (e) {
    console.warn('[ScadaEditor] exitFullscreen failed:', e)
  }
}
// 修复4: 小地图打开时更新视口矩形
watch(showMinimap, (val) => { if (val) updateViewportRect() })

// [AUDIT-FIX] 严重级-组件卸载后异步响应仍更新状态，添加 isMounted 守卫
let isMounted = true

onMounted(async () => {
  await fetchDevices()
  if (!isMounted) return
  try {
    const data = await scadaApi.getProject('default')
    if (!isMounted) return
    // 修复5: 优先加载 scenes 结构，兼容旧数据（无 scenes 时包装为默认画面）
    if (data && Array.isArray(data.scenes) && data.scenes.length) {
      scenes.value = data.scenes.map((s: any) => ({
        id: s.id || 'scene_' + Date.now(),
        name: s.name || t('scada.sceneNameDefault'),
        widgets: migrateChartWidgets(s.widgets || []),
        canvasConfig: s.canvasConfig || defaultCanvasConfig(),
      }))
      widgetIdCounter = Math.max(0, ...scenes.value.flatMap(s => s.widgets.map((w: any) => w.id)))
      currentSceneId.value = scenes.value[0].id
      widgets.value = scenes.value[0].widgets.map((w: any) => ({ ...w }))
      // 修复4: 加载首画面的画布配置
      Object.assign(canvasConfig, scenes.value[0].canvasConfig || defaultCanvasConfig())
    } else if (data?.widgets?.length) {
      const ws = migrateChartWidgets(data.widgets)
      widgetIdCounter = Math.max(0, ...(ws ?? []).map((w: any) => w.id))
      const defaultScene: ScadaScene = { id: 'scene_' + Date.now(), name: t('scada.sceneNameDefault'), widgets: ws }
      scenes.value = [defaultScene]
      currentSceneId.value = defaultScene.id
      widgets.value = ws.map((w: any) => ({ ...w }))
    } else {
      // 无数据时初始化默认空画面
      const defaultScene: ScadaScene = { id: 'scene_' + Date.now(), name: t('scada.sceneNameDefault'), widgets: [] }
      scenes.value = [defaultScene]
      currentSceneId.value = defaultScene.id
      widgets.value = []
    }
  } catch {
    if (!isMounted) return
    const saved = localStorage.getItem('scada-project')
    if (saved) {
      try {
        const data = JSON.parse(saved)
        if (Array.isArray(data.scenes) && data.scenes.length) {
          scenes.value = data.scenes.map((s: any) => ({
            id: s.id || 'scene_' + Date.now(),
            name: s.name || t('scada.sceneNameDefault'),
            widgets: migrateChartWidgets(s.widgets || []),
            canvasConfig: s.canvasConfig || defaultCanvasConfig(),
          }))
          widgetIdCounter = Math.max(0, ...scenes.value.flatMap(s => s.widgets.map((w: any) => w.id)))
          currentSceneId.value = scenes.value[0].id
          widgets.value = scenes.value[0].widgets.map((w: any) => ({ ...w }))
          // 修复4: 加载首画面的画布配置
          Object.assign(canvasConfig, scenes.value[0].canvasConfig || defaultCanvasConfig())
        } else if (data.widgets?.length) {
          const ws = migrateChartWidgets(data.widgets)
          widgetIdCounter = Math.max(0, ...(ws ?? []).map((w: any) => w.id))
          const defaultScene: ScadaScene = { id: 'scene_' + Date.now(), name: t('scada.sceneNameDefault'), widgets: ws }
          scenes.value = [defaultScene]
          currentSceneId.value = defaultScene.id
          widgets.value = ws.map((w: any) => ({ ...w }))
        } else {
          const defaultScene: ScadaScene = { id: 'scene_' + Date.now(), name: t('scada.sceneNameDefault'), widgets: [] }
          scenes.value = [defaultScene]
          currentSceneId.value = defaultScene.id
          widgets.value = []
        }
      } catch (e) {
        console.warn('Failed to parse local storage project:', e)
        const defaultScene: ScadaScene = { id: 'scene_' + Date.now(), name: t('scada.sceneNameDefault'), widgets: [] }
        scenes.value = [defaultScene]
        currentSceneId.value = defaultScene.id
        widgets.value = []
      }
    } else {
      const defaultScene: ScadaScene = { id: 'scene_' + Date.now(), name: t('scada.sceneNameDefault'), widgets: [] }
      scenes.value = [defaultScene]
      currentSceneId.value = defaultScene.id
      widgets.value = []
    }
  }
  if (!isMounted) return
  pageLoading.value = false
  pushHistory()
  scheduleRefresh()  // FIXED-P0: 使用递归setTimeout替代setInterval
  document.addEventListener('keydown', onKeyDown)
  // 修复5: 监听全屏状态变化，ESC 退出全屏时同步 previewMode
  document.addEventListener('fullscreenchange', onFullscreenChange)
  document.addEventListener('webkitfullscreenchange', onFullscreenChange)
})

onUnmounted(() => {
  isMounted = false
  isRefreshing = true
  if (refreshTimer) clearTimeout(refreshTimer)
  // FIXED-P1: 原问题-卸载时未清理拖拽/缩放监听器，拖拽中切换路由后
  // canvasRef.value 变为 null，mousemove 回调中 getBoundingClientRect() 抛错
  _clearDragListeners()
  document.removeEventListener('keydown', onKeyDown)
  // 修复5: 移除全屏状态变化监听
  document.removeEventListener('fullscreenchange', onFullscreenChange)
  document.removeEventListener('webkitfullscreenchange', onFullscreenChange)
  // 修复5: 卸载时若仍处于全屏状态则退出
  if (document.fullscreenElement) {
    exitEditorFullscreen()
  }
  // 修复5: 卸载时同步当前画面并保存 scenes 到本地
  syncCurrentSceneWidgets()
  localStorage.setItem('scada-project', JSON.stringify({ widgets: widgets.value, scenes: scenes.value }))
  // [AUDIT-FIX] 卸载时关闭 AudioContext，释放音频资源
  if (_audioCtx) { _audioCtx.close(); _audioCtx = null }
})

// 页面可见性变化：隐藏时暂停调度，恢复可见时立即刷新并恢复调度
watch(isVisible, (visible) => {
  if (visible) {
    refreshAllValues()
    if (!refreshTimer) scheduleRefresh()
  } else {
    if (refreshTimer) {
      clearTimeout(refreshTimer)
      refreshTimer = null
    }
  }
})

// 修复5: 刷新频率变化时重启调度定时器
watch(refreshIntervalMs, () => {
  if (refreshTimer) {
    clearTimeout(refreshTimer)
    refreshTimer = null
    scheduleRefresh()
  }
})

// [AUDIT-FIX] widgets 通过 push/splice 及属性原地修改变更，浅 watch 无法感知，保留 deep；
// 大规模场景下 deep watch 高频触发，添加 300ms 防抖以降低性能损耗
let markDirtyTimer: ReturnType<typeof setTimeout> | null = null
const markDirtyDebounced = () => {
  if (markDirtyTimer) clearTimeout(markDirtyTimer)
  markDirtyTimer = setTimeout(() => { dirty.value = true }, 300)
}
watch(widgets, markDirtyDebounced, { deep: true })

onBeforeRouteLeave((_to, _from, next) => {
  if (dirty.value) {
    dialog.warning({  // FIXED: 原问题-中文硬编码
      title: t('scada.unsavedChanges'),
      content: t('scada.unsavedChangesContent'),
      positiveText: t('scada.leave'),
      negativeText: t('scada.stay'),
      onPositiveClick: () => next(),
      onNegativeClick: () => next(false),
    })
  } else {
    next()
  }
})

onBeforeUnmount(() => {
  window.onbeforeunload = null
})

if (typeof window !== 'undefined') {
  window.onbeforeunload = (e) => {
    if (dirty.value) {
      e.preventDefault()
    }
  }
}
</script>

<style scoped>
.scada-page {
  display: flex;
  flex-direction: column;
  height: calc(100vh - 80px);
  background: #0a0f1a;
  border-radius: 8px;
  overflow: hidden;
}

.scada-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 16px;
  background: #0d1520;
  border-bottom: 1px solid #1a2a3a;
}

.header-left { display: flex; align-items: center; gap: 8px; }
.header-title { font-weight: 700; font-size: 15px; color: #e0f0ff; }
.header-actions { display: flex; gap: 6px; }

/* 修复5: 多画面标签栏 */
.scene-tabs-bar {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 4px 12px;
  background: #0d1520;
  border-bottom: 1px solid #1a2a3a;
}
.scene-tabs-bar :deep(.n-tabs) { --n-tab-padding: 4px 12px; }

/* 修复6: 对齐参考线 */
.align-line { position: absolute; pointer-events: none; z-index: 9999; }
.align-line-x { width: 0; height: 100%; border-left: 1px dashed #ff6b6b; top: 0; }
.align-line-y { height: 0; width: 100%; border-top: 1px dashed #ff6b6b; left: 0; }

.scada-body { display: flex; flex: 1; overflow: hidden; }

.toolbox {
  width: 200px;
  min-width: 200px;
  background: #0d1520;
  border-right: 1px solid #1a2a3a;
  padding: 12px;
  overflow-y: auto;
}

.toolbox-title { font-size: 11px; font-weight: 600; color: #e0f0ff; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; }

.toolbox-items { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }

.toolbox-item {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  padding: 8px 4px;
  border-radius: 6px;
  cursor: pointer;
  transition: background 0.15s;
  border: 1px solid transparent;
}
.toolbox-item:hover { background: rgba(79, 195, 247, 0.08); border-color: rgba(79, 195, 247, 0.15); }

.tb-icon { width: 32px; height: 32px; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-size: 16px; color: #e0f0ff; }
.tb-label { font-size: 10px; color: #e0f0ff; }

.device-tree { max-height: 300px; overflow-y: auto; }

.dt-device-header {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  border-radius: 4px;
  cursor: pointer;
  transition: background 0.15s;
  font-size: 12px;
  color: #e0f0ff;
}
.dt-device-header:hover { background: rgba(79, 195, 247, 0.08); }
.dt-device-header.active { background: rgba(79, 195, 247, 0.12); }

.dt-dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
.dt-name { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

.dt-points { padding-left: 16px; }
.dt-point {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 4px 8px;
  border-radius: 4px;
  font-size: 11px;
  color: #e0f0ff;
  cursor: pointer;
  transition: background 0.15s;
}
.dt-point:hover { background: rgba(79, 195, 247, 0.08); color: #e0f0ff; }
.dt-empty { font-size: 11px; color: #e0f0ff; padding: 8px; text-align: center; }

.canvas-area { flex: 1; display: flex; flex-direction: column; overflow: hidden; position: relative; }

.canvas-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 12px;
  background: #0d1520;
  border-bottom: 1px solid #1a2a3a;
}

.zoom-label { font-size: 11px; color: #e0f0ff; }

.scada-canvas {
  flex: 1;
  position: relative;
  background-image: radial-gradient(circle, #1a2a3a 1px, transparent 1px);
  background-size: 20px 20px;
  background-color: #0a0f1a;
  overflow: auto;
  min-height: 600px;
}

.scada-canvas.preview-mode { background-color: #080c14; }

.scada-canvas.drag-over {
  background-color: #0e1a2a;
  outline: 2px dashed #4fc3f7;
  outline-offset: -4px;
}

/* 修复2: 画面切换 fade 过渡动画 */
.scada-fade-enter-active, .scada-fade-leave-active { transition: opacity 0.25s ease; }
.scada-fade-enter-from, .scada-fade-leave-to { opacity: 0; }

/* 修复4: 场景标签星标按钮 */
.scene-tab-label { display: inline-flex; align-items: center; gap: 4px; }
.scene-star-icon { cursor: pointer; flex-shrink: 0; }
.scene-star-icon:hover { transform: scale(1.2); }

[draggable="true"] { cursor: grab; }
[draggable="true"]:active { cursor: grabbing; }

.empty-hint { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); text-align: center; color: #8faabe; }
.empty-icon-svg { font-size: 48px; margin-bottom: 12px; color: #4fc3f7; }
.empty-text { font-size: 14px; }

.scada-widget {
  position: absolute;
  background: #0d1520;
  border: 1px solid #1a2a3a;
  border-radius: 8px;
  padding: 8px;
  cursor: move;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.3);
  transition: border-color 0.2s;
  user-select: none;
  overflow: hidden;
  /* FIXED-Touch: 防止触摸滚动干扰拖拽 */
  touch-action: none;
}
.scada-widget:hover { border-color: rgba(79, 195, 247, 0.3); }
.scada-widget.selected { border-color: #4fc3f7; box-shadow: 0 0 0 1px #4fc3f7, 0 2px 16px rgba(79, 195, 247, 0.2); }
.scada-widget.multi-selected { border-color: #9c27b0; box-shadow: 0 0 0 1px #9c27b0; }
.scada-widget.grouped { border-style: dashed; border-color: #6a8caa; }
.scada-widget.locked { opacity: 0.7; cursor: default; }
.scada-widget.locked:hover { border-color: #1a2a3a; }

.widget-lock-badge {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 18px;
  height: 18px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 4px;
  background: rgba(208, 48, 80, 0.2);
  color: #d03050;
  z-index: 2;
}

.widget-actions {
  position: absolute;
  top: 2px;
  right: 2px;
  display: flex;
  gap: 2px;
}
.wa-btn { width: 20px; height: 20px; display: flex; align-items: center; justify-content: center; border-radius: 4px; cursor: pointer; font-size: 11px; color: #8faabe; transition: all 0.15s; padding: 2px; }
.wa-btn:hover { color: #e0f0ff; background: rgba(79, 195, 247, 0.15); }
.wa-del:hover { color: #d03050; background: rgba(208, 48, 80, 0.15); }

.resize-handle {
  position: absolute;
  bottom: 0;
  right: 0;
  width: 14px;
  height: 14px;
  cursor: nwse-resize;
  background: linear-gradient(135deg, transparent 50%, #4fc3f7 50%);
  border-radius: 0 0 8px 0;
  opacity: 0.5;
  /* FIXED-Touch: 防止触摸滚动干扰缩放 */
  touch-action: none;
}
.resize-handle:hover { opacity: 1; }

.w-gauge { height: 100%; display: flex; align-items: center; justify-content: center; }
.gauge-svg { width: 100%; height: 100%; max-width: 140px; }

.w-indicator { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 6px; height: 100%; }
.ind-light { width: 36px; height: 36px; border-radius: 50%; transition: all 0.3s; }
.ind-label { font-size: 12px; color: #e0f0ff; font-weight: 600; }
.ind-status { font-size: 10px; color: #8faabe; }

.w-chart { height: 100%; display: flex; flex-direction: column; }
.chart-title { font-size: 11px; font-weight: 600; color: #e0f0ff; margin-bottom: 4px; }
.chart-body { flex: 1; min-height: 60px; background: #080c14; border-radius: 4px; }

.w-switch { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px; height: 100%; }
.sw-label { font-size: 12px; color: #e0f0ff; font-weight: 600; }
.sw-status { font-size: 10px; color: #8faabe; transition: color 0.2s; }
.sw-status.on { color: #18a058; }

.w-tank { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 4px; height: 100%; }
.tank-body { width: 50px; height: 60px; border: 2px solid #1a3a5a; border-radius: 4px 4px 8px 8px; position: relative; overflow: hidden; background: #080c14; }
.tank-fill { position: absolute; bottom: 0; left: 0; right: 0; transition: height 0.5s ease, background 0.3s; border-radius: 0 0 6px 6px; }
.tank-val { font-size: 16px; font-weight: bold; color: #e0f0ff; }
.tank-label { font-size: 10px; color: #8faabe; }

.w-label { display: flex; align-items: center; justify-content: center; height: 100%; font-size: 14px; font-weight: 600; color: #e0f0ff; }

/* 修复7: 画面跳转组件 */
.w-link { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 4px; height: 100%; color: #e0f0ff; }
.w-link.link-clickable { cursor: pointer; }
.w-link.link-clickable:hover { color: #9c27b0; }
.link-label { font-size: 12px; font-weight: 600; }
.link-target { font-size: 10px; color: #8faabe; }

.props-panel {
  width: 220px;
  min-width: 220px;
  background: #0d1520;
  border-left: 1px solid #1a2a3a;
  padding: 12px;
  overflow-y: auto;
}

.props-title { font-size: 11px; font-weight: 600; color: #8faabe; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 12px; }
.props-body { display: flex; flex-direction: column; gap: 8px; }

.prop-row { display: flex; flex-direction: column; gap: 2px; }
.prop-label { font-size: 10px; color: #8faabe; }
.prop-val { font-size: 12px; color: #b8c9d1; }

/* 修复3: 撤销/重做步数指示器 */
.history-step-indicator {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 38px;
  padding: 0 6px;
  font-size: 11px;
  color: #e0f0ff;
  background: rgba(255, 255, 255, 0.06);
  cursor: default;
  user-select: none;
}

/* 修复4: 缩略图小地图 */
.minimap-container {
  position: absolute;
  bottom: 12px;
  right: 12px;
  z-index: 100;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
}
.minimap-toggle {
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #0d1520;
  border: 1px solid #1a2a3a;
  border-radius: 6px;
  cursor: pointer;
  color: #e0f0ff;
  transition: background 0.15s;
}
.minimap-toggle:hover { background: rgba(79, 195, 247, 0.12); }
.minimap-panel {
  position: absolute;
  bottom: 32px;
  right: 0;
  background: #0d1520;
  border: 1px solid #1a2a3a;
  border-radius: 6px;
  overflow: hidden;
  cursor: pointer;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
}
.minimap-svg { display: block; }

/* 修复8: 图层管理面板 */
.layer-list { display: flex; flex-direction: column; gap: 4px; max-height: 60vh; overflow-y: auto; }
.layer-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  border-radius: 4px;
  cursor: pointer;
  transition: background 0.15s;
  font-size: 11px;
  color: #e0f0ff;
  background: rgba(255, 255, 255, 0.02);
  border: 1px solid transparent;
}
.layer-item:hover { background: rgba(79, 195, 247, 0.08); }
.layer-item.active { background: rgba(79, 195, 247, 0.12); border-color: rgba(79, 195, 247, 0.3); }
.layer-name { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.layer-type-tag {
  font-size: 9px;
  padding: 1px 4px;
  border-radius: 3px;
  background: rgba(102, 126, 234, 0.15);
  color: #8faabe;
  flex-shrink: 0;
}
.layer-actions { display: flex; gap: 2px; flex-shrink: 0; }
.layer-actions .n-button { color: #8faabe; }
.layer-actions .n-button:hover { color: #e0f0ff; }
</style>
