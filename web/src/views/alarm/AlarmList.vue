<template>
  <n-space vertical :size="16">
    <n-grid :cols="6" :x-gap="12" :y-gap="12" responsive="screen" item-responsive>
      <n-gi span="6 m:3 l:1">
        <n-card size="small" :bordered="true" class="ai-stat-card ai-stat-card-purple" style="height:100px">
          <n-statistic :label="t('alarmList.aiAlarmTotal')">
            <template #prefix><n-icon :component="SparklesOutline" :size="18" /></template>
            <n-number-animation :from="0" :to="aiAlarmCount" :duration="500" />
          </n-statistic>
        </n-card>
      </n-gi>
      <n-gi span="6 m:3 l:1">
        <n-card size="small" :bordered="true" class="ai-stat-card ai-stat-card-indigo" style="height:100px">
          <n-statistic :label="t('alarmList.aiAlarmRatio')">
            <template #prefix><n-icon :component="AnalyticsOutline" :size="18" /></template>
            <template #default>
              <span class="ai-ratio-num">{{ aiAlarmRatio }}%</span>
            </template>
          </n-statistic>
        </n-card>
      </n-gi>
      <n-gi span="6 m:3 l:1">
        <n-card size="small" :bordered="true" class="ai-stat-card ai-stat-card-cyan" style="height:100px">
          <n-statistic :label="t('alarmList.todayAiInferences')">
            <template #prefix><n-icon :component="SparklesOutline" :size="18" /></template>
            <n-number-animation :from="0" :to="aiStats.total_calls ?? 0" :duration="500" />
          </n-statistic>
        </n-card>
      </n-gi>
      <n-gi span="6 m:3 l:1">
        <n-card size="small" :bordered="true" class="ai-stat-card ai-stat-card-pink" style="height:100px">
          <n-statistic :label="t('alarmList.avgInferenceLatency')">
            <template #prefix><n-icon :component="TimerOutline" :size="18" /></template>
            <n-number-animation :from="0" :to="aiStats.avg_latency_ms ?? 0" :duration="500" />
            <template #suffix>ms</template>
          </n-statistic>
        </n-card>
      </n-gi>
      <n-gi span="6 m:3 l:1">
        <n-card size="small" :bordered="true" style="height:100px">
          <n-statistic :label="t('alarmList.mttr')">
            <template #default>
              <n-text :type="alarmStatistics.mttr_minutes <= 30 ? 'success' : alarmStatistics.mttr_minutes <= 120 ? 'warning' : 'error'">
                {{ alarmStatistics.mttr_minutes.toFixed(1) }} min
              </n-text>
            </template>
          </n-statistic>
        </n-card>
      </n-gi>
      <n-gi span="6 m:3 l:1">
        <n-card size="small" :bordered="true" style="height:100px">
          <n-statistic :label="t('alarmList.mtbf')">
            <template #default>
              <n-text :type="alarmStatistics.mtbf_hours >= 24 ? 'success' : alarmStatistics.mtbf_hours >= 4 ? 'warning' : 'error'">
                {{ alarmStatistics.mtbf_hours.toFixed(1) }} h
              </n-text>
            </template>
          </n-statistic>
        </n-card>
      </n-gi>
    </n-grid>

    <n-card :title="t('alarmList.trendTitle')" size="small" :bordered="true">
      <template #header-extra>
        <n-button-group size="small">
          <n-button :type="trendDays === 7 ? 'primary' : 'default'" @click="trendDays = 7; fetchAlarmTrend(); fetchTopAlarms()">{{ t('alarmList.trend7d') }}</n-button>
          <n-button :type="trendDays === 30 ? 'primary' : 'default'" @click="trendDays = 30; fetchAlarmTrend(); fetchTopAlarms()">{{ t('alarmList.trend30d') }}</n-button>
        </n-button-group>
      </template>
      <v-chart v-if="trendData.length" :option="trendChartOption" style="height: 280px" autoresize />
      <n-empty v-if="!trendData.length && !loading" :description="t('common.noData')" style="padding: 40px 0" />
    </n-card>

    <!-- 修复7: Top10 报警统计看板 -->
    <n-grid :cols="2" :x-gap="12" :y-gap="12" responsive="screen" item-responsive>
      <n-gi span="2 m:1">
        <n-card :title="t('alarmList.topDevicesTitle')" size="small" :bordered="true">
          <template #header-extra>
            <n-button size="small" quaternary :loading="topLoading" @click="fetchTopAlarms">{{ t('alarmList.topRefresh') }}</n-button>
          </template>
          <v-chart v-if="topDevices.length" :option="topDevicesChartOption" style="height: 300px" autoresize />
          <n-empty v-else :description="t('alarmList.topNoData')" style="padding: 40px 0" />
        </n-card>
      </n-gi>
      <n-gi span="2 m:1">
        <n-card :title="t('alarmList.topRulesTitle')" size="small" :bordered="true">
          <template #header-extra>
            <n-button size="small" quaternary :loading="topLoading" @click="fetchTopAlarms">{{ t('alarmList.topRefresh') }}</n-button>
          </template>
          <v-chart v-if="topRules.length" :option="topRulesChartOption" style="height: 300px" autoresize />
          <n-empty v-else :description="t('alarmList.topNoData')" style="padding: 40px 0" />
        </n-card>
      </n-gi>
    </n-grid>

    <n-space justify="space-between">
      <n-space>
        <n-input v-model:value="searchText" :placeholder="t('alarmList.searchPlaceholder')" clearable style="width: 200px" @update:value="onSearchInput" @keyup.enter="onSearchEnter" />
        <n-select v-model:value="filterStatus" :options="statusOptions" :placeholder="t('alarmList.statusFilter')" clearable style="width: 120px" @update:value="() => { pagination.page = 1; fetchAlarms() }" />
        <n-select v-model:value="filterSeverity" :options="severityOptions" :placeholder="t('alarmList.levelFilter')" clearable style="width: 120px" @update:value="() => { pagination.page = 1; fetchAlarms() }" />
        <!-- UX-FIX-04: 增加设备筛选（与审计日志页对齐） -->
        <n-select
          v-model:value="filterDeviceId"
          :options="deviceFilterOptions"
          :placeholder="t('alarmList.deviceFilter')"
          clearable
          filterable
          style="width: 180px"
          @update:value="() => { pagination.page = 1; fetchAlarms() }"
        />
        <!-- UX-FIX-04: 增加时间范围筛选 -->
        <n-date-picker
          v-model:value="filterTimeRange"
          type="datetimerange"
          clearable
          :placeholder="[t('alarmList.startTime'), t('alarmList.endTime')]"
          style="width: 360px"
          @update:value="() => { pagination.page = 1; fetchAlarms() }"
        />
        <n-button-group>
          <n-button :type="filterType === 'all' ? 'primary' : 'default'" size="small" @click="filterType = 'all'; pagination.page = 1; fetchAlarms()">{{ t('alarmList.filterAll') }}</n-button>
          <n-button :type="filterType === 'ai' ? 'primary' : 'default'" size="small" @click="filterType = 'ai'; pagination.page = 1; fetchAlarms()">{{ t('alarmList.filterAi') }}</n-button>
          <n-button :type="filterType === 'threshold' ? 'primary' : 'default'" size="small" @click="filterType = 'threshold'; pagination.page = 1; fetchAlarms()">{{ t('alarmList.filterThreshold') }}</n-button>
          <n-button :type="filterType === 'script' ? 'primary' : 'default'" size="small" @click="filterType = 'script'; pagination.page = 1; fetchAlarms()">{{ t('alarmList.filterScript') }}</n-button>
        </n-button-group>
        <!-- 修复21: 告警升级——未确认超时筛选 -->
        <n-button-group size="small">
          <n-button :type="escalationFilter === 'all' ? 'primary' : 'default'" @click="setEscalationFilter('all')">{{ t('alarmList.escalationAll') }}</n-button>
          <n-button :type="escalationFilter === 'overtime' ? 'warning' : 'default'" @click="setEscalationFilter('overtime')">{{ t('alarmList.escalationOvertime') }}</n-button>
          <n-button :type="escalationFilter === 'critical' ? 'error' : 'default'" @click="setEscalationFilter('critical')">{{ t('alarmList.escalationCritical') }}</n-button>
        </n-button-group>
        <!-- 修复5: 列自定义 -->
        <n-popover trigger="click" placement="bottom-end" :width="200">
          <template #trigger>
            <n-button size="small" quaternary>{{ t('alarmList.columnSettings') }}</n-button>
          </template>
          <div style="padding: 4px 0">
            <div style="font-size: 12px; font-weight: 600; margin-bottom: 8px; color: var(--n-text-color, #333)">{{ t('alarmList.columnSettings') }}</div>
            <n-checkbox-group :value="visibleColumnKeys" @update:value="onColumnVisibleChange">
              <n-space vertical :size="4">
                <n-checkbox v-for="opt in columnSettingOptions" :key="opt.value" :value="opt.value" :label="opt.label" />
              </n-space>
            </n-checkbox-group>
          </div>
        </n-popover>
        <!-- 修复6: 视图切换——平铺/按设备分组 -->
        <n-button-group size="small">
          <n-button :type="viewMode === 'flat' ? 'primary' : 'default'" @click="viewMode = 'flat'">{{ t('alarmList.viewFlat') }}</n-button>
          <n-button :type="viewMode === 'grouped' ? 'primary' : 'default'" @click="viewMode = 'grouped'">{{ t('alarmList.viewGrouped') }}</n-button>
        </n-button-group>
      </n-space>
      <n-space>
        <!-- UX-FIX-05: 一键全部确认（仅对当前筛选的 firing 报警） -->
        <n-popconfirm @positive-click="handleAckAllFiring">
          <template #trigger>
            <n-button type="error" size="small" :disabled="!auth.isOperator || firingAlarms.length === 0" :loading="batchAcking">{{ t('alarmList.ackAllFiring', { count: firingAlarms.length }) }}</n-button>
          </template>
          {{ t('alarmList.ackAllFiringConfirm', { count: firingAlarms.length }) }}
        </n-popconfirm>
        <n-button type="warning" size="small" :disabled="!auth.isOperator || !checkedKeys.length" :loading="batchAcking" @click="handleBatchAckSelected">{{ t('alarmList.batchAckSelected', { count: checkedKeys.length }) }}</n-button>
        <n-popconfirm @positive-click="handleBatchAckAndSilenceSelected">
          <template #trigger>
            <n-button type="warning" size="small" :disabled="!auth.isOperator || !checkedKeys.length" :loading="batchAcking">{{ t('alarmList.batchAckAndSilence', { count: checkedKeys.length }) }}</n-button>
          </template>
          {{ t('alarmList.batchAckAndSilenceConfirm', { count: checkedKeys.length }) }}
        </n-popconfirm>
        <n-button type="info" :disabled="!auth.isOperator" @click="showSilenceModal = true">{{ t('alarmSilence.setSilence') }}</n-button>
        <n-button size="small" @click="showSilenceListModal = true; fetchSilenceList()">{{ t('alarmSilence.activeList') }}</n-button>
        <!-- FIXED-General: 补充告警导出功能，支持将当前筛选条件下的告警导出为 CSV -->
        <n-button size="small" :loading="exporting" :disabled="!alarms.length" @click="handleExport">{{ t('alarmList.export') }}</n-button>
      </n-space>
    </n-space>

    <!-- 修复25: 骨架屏加载 -->
    <template v-if="loading && !alarms.length">
      <n-card v-for="i in 5" :key="i" size="small" style="margin-bottom: 8px">
        <n-skeleton text :repeat="2" />
      </n-card>
    </template>
    <n-data-table
      v-else-if="viewMode === 'flat'"
      :columns="columns" :data="alarms" :loading="loading"
      :pagination="pagination" :row-key="(r: Alarm) => r.alarm_id"
      :row-class-name="rowClassName"
      v-model:checked-row-keys="checkedKeys"
      remote
      virtual-scroll
      :max-height="600"
      :scroll-x="1720"
      @update:sorter="handleSorterChange"
    >
      <template #empty>
        <n-empty v-if="!loading" :description="t('alarmList.emptyDesc')" style="padding: 40px 0" />
      </template>
    </n-data-table>

    <!-- 修复6: 按设备分组视图 -->
    <n-card v-else size="small" :bordered="true">
      <n-spin :show="loading">
        <n-empty v-if="!groupedAlarms.length" :description="t('alarmList.emptyDesc')" style="padding: 40px 0" />
        <n-collapse v-else :default-expanded-names="groupedAlarms.slice(0, 3).map(g => g.deviceId)">
          <n-collapse-item v-for="g in groupedAlarms" :key="g.deviceId" :name="g.deviceId">
            <template #header>
              <n-space align="center" :size="8">
                <n-tag size="small" :type="g.firingCount > 0 ? 'error' : 'default'">{{ g.deviceName }}</n-tag>
                <n-text depth="3" style="font-size: 12px">{{ t('alarmList.groupTotal', { count: g.alarms.length }) }}</n-text>
                <n-tag v-if="g.firingCount > 0" size="small" type="error">{{ t('alarm.firing') }}: {{ g.firingCount }}</n-tag>
                <n-tag v-if="g.acknowledgedCount > 0" size="small" type="warning">{{ t('alarm.acknowledged') }}: {{ g.acknowledgedCount }}</n-tag>
                <n-tag v-if="g.recoveredCount > 0" size="small" type="success">{{ t('alarm.recovered') }}: {{ g.recoveredCount }}</n-tag>
              </n-space>
            </template>
            <n-data-table
              :columns="columns"
              :data="g.alarms"
              :loading="loading"
              :row-key="(r: Alarm) => r.alarm_id"
              :row-class-name="rowClassName"
              size="small"
              :pagination="{ pageSize: 10, pageSizes: [10, 20, 50, 100], showSizePicker: true }"
              :scroll-x="1720"
            />
          </n-collapse-item>
        </n-collapse>
      </n-spin>
    </n-card>

    <n-modal v-model:show="showAiDetail" preset="card" style="width: 520px; max-width: 95vw" :title="t('alarmList.aiDetailTitle')" :close-on-esc="true" :auto-focus="true" :close-on-esc-aria-label="t('common.closeDialog')">
      <template v-if="selectedAiAlarm">
        <n-descriptions label-placement="left" :column="1" bordered>
          <n-descriptions-item :label="t('alarmList.aiModelName')">{{ (selectedAiAlarm as any).ai_model_name || '-' }}</n-descriptions-item>
          <n-descriptions-item :label="t('alarmList.aiModelVersion')">{{ (selectedAiAlarm as any).ai_model_version || '-' }}</n-descriptions-item>
          <n-descriptions-item :label="t('alarmList.aiInputSummary')">{{ (selectedAiAlarm as any).ai_input_summary || '-' }}</n-descriptions-item>
          <n-descriptions-item :label="t('alarmList.aiAnomalyScore')">
            <n-space align="center" :size="12">
              <span style="font-size:28px;font-weight:700;color:#8b5cf6">{{ aiAnomalyScoreDisplay }}</span>
              <n-progress type="line" :percentage="aiAnomalyScorePercent" :color="aiAnomalyScorePercent > 80 ? '#f56c6c' : '#8b5cf6'" :rail-color="'#e5e7eb'" style="width:160px" :indicator-placement="'inside'" />
            </n-space>
          </n-descriptions-item>
          <n-descriptions-item :label="t('alarmList.aiLatency')">
            <span style="font-weight:600">{{ (selectedAiAlarm as any).ai_latency_ms ?? '-' }}</span>
            <span v-if="(selectedAiAlarm as any).ai_latency_ms != null">ms</span>
          </n-descriptions-item>
        </n-descriptions>
      </template>
    </n-modal>

    <n-modal v-model:show="showDetailModal" preset="card" style="width: 600px; max-width: 95vw" :title="t('alarmList.detailTitle')" :close-on-esc="true" :auto-focus="true" :close-on-esc-aria-label="t('common.closeDialog')">
      <template v-if="selectedAlarm">
        <n-descriptions label-placement="left" :column="2" bordered>
          <n-descriptions-item :label="t('alarmList.alarmId')" :span="2">{{ selectedAlarm.alarm_id }}</n-descriptions-item>
          <n-descriptions-item :label="t('alarmList.ruleId')">{{ selectedAlarm.rule_id }}</n-descriptions-item>
          <n-descriptions-item :label="t('alarmList.deviceId')">{{ selectedAlarm.device_id || '-' }}</n-descriptions-item>
          <n-descriptions-item :label="t('alarmList.deviceName')">{{ selectedAlarm.device_id ? ((deviceNameMap as any)[selectedAlarm.device_id] || '-') : '-' }}</n-descriptions-item>
          <n-descriptions-item :label="t('alarmList.level')">
            <n-tag :type="(severityColor as any)[selectedAlarm.severity] || 'default'" size="small">{{ (severityLabel.value as any)[selectedAlarm.severity] || selectedAlarm.severity }}</n-tag>
          </n-descriptions-item>
          <n-descriptions-item :label="t('alarmList.status')">
            <n-tag :type="statusColor[selectedAlarm.status] || 'default'" size="small">{{ statusLabel[selectedAlarm.status] || selectedAlarm.status }}</n-tag>
          </n-descriptions-item>
          <n-descriptions-item :label="t('alarmList.message')" :span="2">{{ selectedAlarm.message || '-' }}</n-descriptions-item>
          <n-descriptions-item :label="t('alarmList.triggerCount')">{{ selectedAlarm.trigger_count }}</n-descriptions-item>
          <n-descriptions-item :label="t('alarmList.alarmType')">{{ (selectedAlarm as any).rule_type === 'ai_inference' ? t('alarm.aiAlarm') : (selectedAlarm as any).rule_type === 'script' ? t('alarm.scriptAlarm') : t('alarm.thresholdAlarm') }}</n-descriptions-item>
          <n-descriptions-item :label="t('alarmList.triggerValue')" :span="2">
            <n-scrollbar x-scrollable style="max-width: 460px">
              <pre style="margin:0;white-space:pre-wrap;font-size:12px">{{ selectedAlarm.trigger_value ? JSON.stringify(selectedAlarm.trigger_value, null, 2) : '-' }}</pre>
            </n-scrollbar>
          </n-descriptions-item>
          <n-descriptions-item :label="t('alarmList.triggerTime')">{{ selectedAlarm.fired_at || '-' }}</n-descriptions-item>
          <n-descriptions-item :label="t('alarmList.ackBy')">{{ selectedAlarm.acknowledged_by || '-' }}</n-descriptions-item>
          <n-descriptions-item :label="t('alarmList.recoverTime')" :span="2">{{ selectedAlarm.recovered_at || '-' }}</n-descriptions-item>
        </n-descriptions>
        <!-- 修复9: 历史触发记录 -->
        <div style="margin-top: 16px">
          <n-text strong style="font-size: 13px">{{ t('alarmList.historyTitle') }}（{{ t('alarmList.lastDays', { days: 7 }) }}）</n-text>
          <n-spin :show="alarmHistoryLoading" size="small">
            <n-data-table
              v-if="alarmHistory.length"
              :columns="historyColumns"
              :data="alarmHistory"
              :bordered="false"
              size="small"
              :pagination="{ pageSize: 5, pageSizes: [10, 20, 50, 100], showSizePicker: true }"
              :max-height="240"
              style="margin-top: 8px"
            />
            <n-empty v-else-if="!alarmHistoryLoading" :description="t('alarmList.noHistory')" style="margin-top: 12px" />
          </n-spin>
        </div>
      </template>
    </n-modal>

    <!-- 设置静默期弹窗 -->
    <n-modal v-model:show="showSilenceModal" preset="card" style="width: 520px; max-width: 95vw" :title="t('alarmSilence.setSilence')" :close-on-esc="true" :auto-focus="true" :close-on-esc-aria-label="t('common.closeDialog')">
      <n-form :model="silenceForm" label-placement="left" label-width="100" :rules="silenceRules" ref="silenceFormRef">
        <n-form-item :label="t('alarmSilence.device')">
          <n-select v-model:value="silenceForm.device_id" :options="silenceDeviceOptions" clearable :placeholder="t('alarmSilence.devicePlaceholder')" />
        </n-form-item>
        <n-form-item :label="t('alarmSilence.rule')">
          <n-select v-model:value="silenceForm.rule_id" filterable remote clearable :options="silenceRuleOptions" :placeholder="t('alarmSilence.rulePlaceholder')" @search="searchSilenceRules" />
        </n-form-item>
        <n-form-item :label="t('alarmSilence.startTime')" path="start_time">
          <n-date-picker v-model:value="silenceForm.start_time" type="datetime" style="width: 100%" />
        </n-form-item>
        <n-form-item :label="t('alarmSilence.endTime')" path="end_time">
          <n-date-picker v-model:value="silenceForm.end_time" type="datetime" style="width: 100%" />
        </n-form-item>
        <n-form-item :label="t('alarmSilence.reason')" path="reason">
          <n-input v-model:value="silenceForm.reason" type="textarea" :rows="2" :placeholder="t('alarmSilence.reasonPlaceholder')" />
        </n-form-item>
      </n-form>
      <template #action>
        <n-button @click="showSilenceModal = false">{{ t('common.cancel') }}</n-button>
        <n-button type="primary" :loading="silenceSaving" @click="handleCreateSilence">{{ t('common.create') }}</n-button>
      </template>
    </n-modal>

    <!-- 静默期列表 -->
    <n-modal v-model:show="showSilenceListModal" preset="card" style="width: 700px; max-width: 95vw" :title="t('alarmSilence.activeList')" :close-on-esc="true" :auto-focus="true" :close-on-esc-aria-label="t('common.closeDialog')">
      <n-data-table :columns="silenceColumns" :data="silenceList" :loading="silenceListLoading" :row-key="(r: any) => r.id" :scroll-x="900">
        <template #empty>
          <n-empty :description="t('common.noData')" size="small" />
        </template>
      </n-data-table>
    </n-modal>

    <!-- 告警抑制弹窗 -->
    <n-modal v-model:show="showSuppressModal" preset="card" style="width: 480px; max-width: 95vw" :title="t('alarmSuppress.title')" :close-on-esc="true" :auto-focus="true" :close-on-esc-aria-label="t('common.closeDialog')">
      <n-form ref="suppressFormRef" :model="suppressForm" :rules="suppressRules" label-placement="left" label-width="100">
        <n-form-item :label="t('alarmSuppress.duration')">
          <n-radio-group v-model:value="suppressForm.duration_seconds">
            <n-space>
              <n-radio :value="3600">{{ t('alarmSuppress.hour1') }}</n-radio>
              <n-radio :value="14400">{{ t('alarmSuppress.hour4') }}</n-radio>
              <n-radio :value="86400">{{ t('alarmSuppress.hour24') }}</n-radio>
            </n-space>
          </n-radio-group>
        </n-form-item>
        <n-form-item :label="t('alarmSuppress.reason')" path="reason">
          <n-input v-model:value="suppressForm.reason" type="textarea" :rows="2" :maxlength="500" show-count :placeholder="t('alarmSuppress.reasonPlaceholder')" />
        </n-form-item>
      </n-form>
      <template #action>
        <n-button @click="showSuppressModal = false">{{ t('common.cancel') }}</n-button>
        <n-button type="warning" :loading="suppressing" @click="doSuppress">{{ t('alarmSuppress.suppress') }}</n-button>
      </template>
    </n-modal>

    <!-- 修复22: 确认告警备注弹窗 -->
    <n-modal v-model:show="showAckRemarkModal" preset="dialog" :title="t('alarmList.ackWithRemark')" style="width: 460px; max-width: 95vw" :close-on-esc="true" :auto-focus="true" :close-on-esc-aria-label="t('common.closeDialog')">
      <n-space vertical :size="12">
        <n-text depth="3">{{ t('alarmList.ackRemarkHint') }}</n-text>
        <n-input
          v-model:value="ackRemark"
          type="textarea"
          :rows="3"
          :placeholder="t('alarmList.ackRemarkPlaceholder')"
          :maxlength="200"
          show-count
        />
      </n-space>
      <template #action>
        <n-button @click="showAckRemarkModal = false">{{ t('common.cancel') }}</n-button>
        <n-button type="primary" :loading="ackingIds.has(ackRemarkAlarmId)" @click="confirmAckWithRemark">{{ t('alarmList.ack') }}</n-button>
      </template>
    </n-modal>
  </n-space>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, onUnmounted, onActivated, onDeactivated, h } from 'vue'
import { useRouter } from 'vue-router'
import { NButton, NTag, NSpace, NTooltip, NPopconfirm, NIcon, NDropdown } from 'naive-ui'
import { SparklesOutline, AnalyticsOutline, TimerOutline, EllipsisVerticalOutline } from '@vicons/ionicons5'
import { alarmApi, aiApi, deviceApi, alarmSilenceApi, ruleApi, type Alarm, type Device, type AlarmSilence } from '@/api'
import { severityLabel, alarmStatusLabel, alarmStatusColor } from '@/utils/enumLabels'
import * as ws from '@/api/websocket'
import { t } from '@/i18n'
import { extractError } from '@/utils/errorCodes'
import * as echarts from 'echarts/core'
import { LineChart, BarChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import VChart from 'vue-echarts'
import { message, dialog } from '@/utils/discreteApi'
import { formatDateTime } from '@/utils/datetime'
// FIXED-P0: 引入 auth store 用于前端权限检查
import { useAuthStore } from '@/stores/auth'
// [AUDIT-FIX] 严重-1: 暗色模式适配
import { useChartTheme } from '@/composables/useChartTheme'

echarts.use([LineChart, BarChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer])

const router = useRouter()
const auth = useAuthStore()
// [AUDIT-FIX] 严重-1: 暗色模式适配
const { chartValueAxis, chartCategoryAxis, chartTooltipAxis, chartLegend } = useChartTheme()

const alarms = ref<Alarm[]>([])
const loading = ref(false)
const batchAcking = ref(false)
const exporting = ref(false)
const searchText = ref('')
const filterStatus = ref<string | null>(null)
const filterSeverity = ref<string | null>(null)
const filterType = ref<'all' | 'ai' | 'threshold' | 'script'>('all')
// UX-FIX-04: 设备筛选 + 时间范围筛选
const filterDeviceId = ref<string | null>(null)
const filterTimeRange = ref<[number, number] | null>(null)
const checkedKeys = ref<string[]>([])
const showAiDetail = ref(false)
const selectedAiAlarm = ref<Alarm | null>(null)
const aiStats = ref<Record<string, any>>({})
// FIXED-P2: 服务端排序状态，配合 fetchAlarms 传 sort_by/sort_order
const sortState = ref<{ columnKey: string | number | null; order: 'ascend' | 'descend' | false } | null>(null)

const trendDays = ref(7)
const trendData = ref<any[]>([])

// 修复7: Top10 报警统计看板
const topDevices = ref<any[]>([])
const topRules = ref<any[]>([])
const topLoading = ref(false)

const trendChartOption = computed(() => {
  const dates = trendData.value.map((d: any) => d.date)
  const counts = trendData.value.map((d: any) => d.count)
  return {
    // [AUDIT-FIX] 严重-1: 暗色模式适配
    tooltip: chartTooltipAxis(),
    legend: chartLegend({ data: [t('alarmList.trendCount')] }),
    grid: { left: 50, right: 20, top: 30, bottom: 30 },
    xAxis: chartCategoryAxis({ data: dates }),
    yAxis: chartValueAxis({ minInterval: 1 }),
    series: [{ name: t('alarmList.trendCount'), type: 'line', data: counts, smooth: true, areaStyle: { opacity: 0.15 }, itemStyle: { color: '#e040fb' } }],
  }
})

async function fetchAlarmTrend() {
  try {
    const data = await alarmApi.statistics({ days: trendDays.value })
    trendData.value = Array.isArray(data?.trend) ? data.trend : Array.isArray(data?.daily) ? data.daily : []
  } catch { trendData.value = [] }
}

// 修复7: 获取 Top10 报警设备/规则排名
async function fetchTopAlarms() {
  topLoading.value = true
  try {
    const data = await alarmApi.statistics({ days: trendDays.value })
    topDevices.value = Array.isArray(data?.top_devices) ? data.top_devices : []
    topRules.value = Array.isArray(data?.top_rules) ? data.top_rules : []
  } catch {
    topDevices.value = []
    topRules.value = []
  } finally {
    topLoading.value = false
  }
}

// Top10 报警设备横向柱状图
const topDevicesChartOption = computed(() => {
  const data = [...topDevices.value].reverse()
  const names = data.map((d: any) => deviceNameMap.value[d.device_id] || d.device_id || '-')
  const counts = data.map((d: any) => d.count)
  return {
    // [AUDIT-FIX] 严重-1: 暗色模式适配
    tooltip: chartTooltipAxis({ axisPointer: { type: 'shadow' } }),
    grid: { left: 120, right: 40, top: 10, bottom: 20 },
    xAxis: chartValueAxis({ minInterval: 1 }),
    yAxis: chartCategoryAxis({ data: names, axisLabel: { width: 110, overflow: 'truncate' } }),
    series: [{
      type: 'bar',
      data: counts,
      itemStyle: { color: '#e040fb', borderRadius: [0, 4, 4, 0] },
      label: { show: true, position: 'right', formatter: '{c}' },
    }],
  }
})

// Top10 报警规则横向柱状图
const topRulesChartOption = computed(() => {
  const data = [...topRules.value].reverse()
  const names = data.map((d: any) => d.rule_id || '-')
  const counts = data.map((d: any) => d.count)
  return {
    // [AUDIT-FIX] 严重-1: 暗色模式适配
    tooltip: chartTooltipAxis({ axisPointer: { type: 'shadow' } }),
    grid: { left: 120, right: 40, top: 10, bottom: 20 },
    xAxis: chartValueAxis({ minInterval: 1 }),
    yAxis: chartCategoryAxis({ data: names, axisLabel: { width: 110, overflow: 'truncate' } }),
    series: [{
      type: 'bar',
      data: counts,
      itemStyle: { color: '#6366f1', borderRadius: [0, 4, 4, 0] },
      label: { show: true, position: 'right', formatter: '{c}' },
    }],
  }
})

// ─── 告警静默期 ───
const showSilenceModal = ref(false)
const showSilenceListModal = ref(false)
const silenceSaving = ref(false)
const silenceList = ref<AlarmSilence[]>([])
const silenceListLoading = ref(false)
// 静默状态剩余时间倒计时：每分钟更新一次 now 触发重渲染
const silenceNow = ref(Date.now())
let _silenceTimer: ReturnType<typeof setInterval> | null = null
const silenceForm = reactive({
  device_id: null as string | null,
  rule_id: null as string | null,
  start_time: null as number | null,
  end_time: null as number | null,
  reason: '',
})

const silenceFormRef = ref<any>(null)
// [AUDIT-FIX] i18n 响应式：silenceRules 改为 computed 实现语言切换响应式
const silenceRules = computed(() => ({
  start_time: { required: true, type: 'number', message: t('alarmSilence.startTimeRequired'), trigger: ['change', 'blur'] },
  end_time: { required: true, type: 'number', message: t('alarmSilence.endTimeRequired'), trigger: ['change', 'blur'] },
}))

const silenceDeviceOptions = computed(() =>
  Object.entries(deviceNameMap.value).map(([id, name]) => ({ label: name || id, value: id }))
)

const silenceRuleOptions = ref<{ label: string; value: string }[]>([])

// FIX: 改为远程搜索，避免一次性拉取 999 条规则
let searchSeq = 0
async function searchSilenceRules(query: string) {
  if (!query) {
    silenceRuleOptions.value = []
    return
  }
  const seq = ++searchSeq
  try {
    const data = await ruleApi.list({ page: 1, size: 50, search: query })
    if (seq !== searchSeq) return
    silenceRuleOptions.value = (data?.data ?? []).map((r: any) => ({ label: r.name || r.rule_id, value: r.rule_id }))
  } catch { /* ignore */ }
}

const silenceColumns = computed(() => [
  { title: t('alarmSilence.device'), key: 'device_id', width: 140, render: (r: any) => r.device_id ? (deviceNameMap.value[r.device_id] || r.device_id) : '-' },
  { title: t('alarmSilence.rule'), key: 'rule_id', width: 140, render: (r: any) => r.rule_id || '-' },
  { title: t('alarmSilence.startTime'), key: 'start_time', width: 170 },
  { title: t('alarmSilence.endTime'), key: 'end_time', width: 170 },
  { title: t('alarmSilence.reason'), key: 'reason', ellipsis: { tooltip: true } },
  {
    title: t('alarmSilence.status'), key: 'status', width: 120,
    // 通过 end_time 与 silenceNow 对比，显示剩余时间或已过期
    render: (r: any) => {
      const endTime = r.end_time ? new Date(r.end_time).getTime() : 0
      const isActive = endTime > silenceNow.value
      if (!isActive) {
        return h(NTag, { type: 'default', size: 'small' }, { default: () => t('alarmSilence.expired') })
      }
      const diffMs = endTime - silenceNow.value
      const diffMin = Math.floor(diffMs / 60000)
      const hours = Math.floor(diffMin / 60)
      const mins = diffMin % 60
      const remainText = hours > 0
        ? `${t('alarmSilence.remaining')} ${hours}h ${mins}min`
        : `${t('alarmSilence.remaining')} ${mins}min`
      // 剩余时间 < 10 分钟用 warning 黄色
      const tagType = diffMin < 10 ? 'warning' : 'success'
      return h(NTag, { type: tagType, size: 'small' }, { default: () => remainText })
    },
  },
  {
    title: t('common.actions'), key: 'actions', width: 100,
    render: (r: any) => {
      const isActive = r.end_time ? new Date(r.end_time).getTime() > silenceNow.value : false
      return isActive
        ? h(NPopconfirm as any, { onPositiveClick: () => handleCancelSilence(r.id) }, {
          trigger: () => h(NButton, { text: true, type: 'warning' }, { default: () => t('alarmSilence.cancel') }),
          default: () => t('alarmSilence.cancelConfirm'),
        })
        : null
    },
  },
])

async function fetchSilenceList() {
  silenceListLoading.value = true
  try {
    const data = await alarmSilenceApi.list({ page: 1, size: 100, status: 'active' })
    silenceList.value = Array.isArray(data?.data) ? data.data : []
  } catch { silenceList.value = [] }
  finally { silenceListLoading.value = false }
}

async function handleCreateSilence() {
  // FIXED-P0: 前端权限检查，viewer 无操作权限
  if (!auth.isOperator) { message.warning(t('common.permissionDenied')); return }
  if (!silenceForm.start_time || !silenceForm.end_time || !silenceForm.reason) {
    message.warning(t('alarmSilence.requiredFields'))
    return
  }
  silenceSaving.value = true
  try {
    await alarmSilenceApi.create({
      device_id: silenceForm.device_id || '',
      rule_id: silenceForm.rule_id || '',
      start_time: new Date(silenceForm.start_time).toISOString(),
      end_time: new Date(silenceForm.end_time).toISOString(),
      reason: silenceForm.reason,
    })
    message.success(t('alarmSilence.createSuccess'))
    showSilenceModal.value = false
    silenceForm.device_id = null
    silenceForm.rule_id = null
    silenceForm.start_time = null
    silenceForm.end_time = null
    silenceForm.reason = ''
    fetchSilenceList()
  } catch (e: any) {
    message.error(extractError(e, t('alarmSilence.createFailed')))
  } finally {
    silenceSaving.value = false
  }
}

async function handleCancelSilence(silenceId: string) {
  if (!auth.isOperator) { message.warning(t('common.permissionDenied')); return }
  try {
    await alarmSilenceApi.cancel(silenceId)
    message.success(t('alarmSilence.cancelSuccess'))
    fetchSilenceList()
  } catch (e: any) {
    message.error(extractError(e, t('alarmSilence.cancelFailed')))
  }
}

// FIXED: AI 告警统计来自后端全局统计接口，而非当前页数据（原 computed 基于 alarms.value 仅含当前页 ~20 条，分页后失真）
const alarmStatistics = reactive({ mttr_minutes: 0, mtbf_hours: 0, ai_count: 0, ai_ratio: 0 })

async function fetchAlarmStatistics() {
  try {
    const data = await alarmApi.statistics()
    // FIXED: 后端返回 { summary: { mttr_seconds, mtbf_seconds, ... }, trend: [...] }
    // 原代码访问 data.mttr_minutes/data.mttr_min 永远为 0，导致 MTTR/MTBF 卡片始终显示 0
    const summary = (data as any)?.summary ?? data
    const mttrSeconds = summary?.mttr_seconds ?? 0
    const mtbfSeconds = summary?.mtbf_seconds ?? 0
    // 后端单位为秒，前端展示为分钟/小时
    alarmStatistics.mttr_minutes = mttrSeconds ? +(mttrSeconds / 60).toFixed(1) : 0
    alarmStatistics.mtbf_hours = mtbfSeconds ? +(mtbfSeconds / 3600).toFixed(2) : 0
    // FIXED: AI 告警总数/占比来自全局统计，避免分页后仅基于当前页数据失真
    alarmStatistics.ai_count = summary?.ai_count ?? summary?.ai_alarm_count ?? (data as any)?.ai_count ?? 0
    const ratio = summary?.ai_ratio ?? summary?.ai_alarm_ratio ?? (data as any)?.ai_ratio ?? 0
    // 后端可能返回 0-1 小数或 0-100 百分比，统一归一化为 0-100
    alarmStatistics.ai_ratio = ratio > 1 ? Math.round(ratio) : Math.round(ratio * 100)
  } catch { /* ignore */ }
}

// Device name mapping
const deviceNameMap = ref<Record<string, string>>({})

// Alarm detail modal
const showDetailModal = ref(false)
const selectedAlarm = ref<Alarm | null>(null)
// 修复9: 告警历史触发记录
const alarmHistory = ref<Alarm[]>([])
const alarmHistoryLoading = ref(false)

// FIXED-P1: doAck 和 doRecover 原共享 recoverLoading 标志，导致并发操作互相阻塞
// 改为独立 Set，按 alarmId 跟踪各自的进行中状态
const ackingIds = ref<Set<string>>(new Set())
const recoveringIds = ref<Set<string>>(new Set())
const firingAlarms = computed(() => alarms.value.filter(a => a.status === 'firing'))

// FIXED: AI 告警统计来自后端全局统计接口（alarmStatistics），而非当前页 alarms.value
const aiAlarmCount = computed(() => alarmStatistics.ai_count)
const aiAlarmRatio = computed(() => alarmStatistics.ai_ratio)

const aiAnomalyScoreDisplay = computed(() => {
  const score = (selectedAiAlarm.value as any)?.ai_anomaly_score
  return score != null ? Number(score).toFixed(4) : '-'
})
const aiAnomalyScorePercent = computed(() => {
  const score = (selectedAiAlarm.value as any)?.ai_anomaly_score
  return score != null ? Math.round(Number(score) * 100) : 0
})

const pagination = reactive({
  page: 1,
  pageSize: 50,
  itemCount: 0,
  pageSizes: [50, 100, 200, 500],
  showSizePicker: true,
  onChange: (p: number) => { pagination.page = p; fetchAlarms() },
  onUpdatePageSize: (s: number) => { pagination.pageSize = s; pagination.page = 1; fetchAlarms() },
})

// [AUDIT-FIX] i18n 响应式：statusOptions 改为 computed 实现语言切换响应式
const statusOptions = computed(() => [
  { label: t('alarm.firing'), value: 'firing' },
  { label: t('alarm.acknowledged'), value: 'acknowledged' },
  { label: t('alarm.recovered'), value: 'recovered' },
])

// [AUDIT-FIX] i18n 响应式：severityOptions 改为 computed 实现语言切换响应式
const severityOptions = computed(() => [
  { label: t('alarm.critical'), value: 'critical' },
  { label: t('alarm.major'), value: 'major' },
  { label: t('alarm.warning'), value: 'warning' },
  { label: t('alarm.minor'), value: 'minor' },
  { label: t('alarm.info'), value: 'info' },
])

const severityColor: Record<string, any> = { critical: 'error', major: 'warning', warning: 'warning', minor: 'info', info: 'info' }
const statusColor: Record<string, any> = { firing: 'error', acknowledged: 'warning', recovered: 'success' }
// [AUDIT-FIX] i18n 响应式：statusLabel 改为 computed 实现语言切换响应式
const statusLabel = computed<Record<string, string>>(() => ({ firing: t('alarm.firing'), acknowledged: t('alarm.acknowledged'), recovered: t('alarm.recovered') }))

// 修复9: 历史触发记录表格列
const historyColumns = computed(() => [
  { title: t('alarmList.triggerTime'), key: 'fired_at', width: 180 },
  { title: t('alarmList.level'), key: 'severity', width: 90, render: (r: Alarm) => h(NTag, { type: severityColor[r.severity] || 'default', size: 'small' }, { default: () => severityLabel.value[r.severity] || r.severity }) },
  { title: t('alarmList.status'), key: 'status', width: 90, render: (r: Alarm) => h(NTag, { type: statusColor[r.status] || 'default', size: 'small' }, { default: () => statusLabel.value[r.status] || r.status }) },
  { title: t('alarmList.message'), key: 'message', ellipsis: { tooltip: true } },
  { title: t('alarmList.triggerCount'), key: 'trigger_count', width: 80 },
])

const filteredAlarms = computed(() => {
  // FIXED-P2: filterType 已改为服务端过滤（fetchAlarms 传 rule_type），此处保留兼容
  return alarms.value
})

// 修复6: 视图模式——平铺/按设备分组
const viewMode = ref<'flat' | 'grouped'>('flat')
interface AlarmGroup {
  deviceId: string
  deviceName: string
  alarms: Alarm[]
  firingCount: number
  acknowledgedCount: number
  recoveredCount: number
}
const groupedAlarms = computed<AlarmGroup[]>(() => {
  const map = new Map<string, Alarm[]>()
  for (const a of alarms.value) {
    const key = a.device_id || t('alarmList.noDevice')
    if (!map.has(key)) map.set(key, [])
    map.get(key)!.push(a)
  }
  const groups: AlarmGroup[] = []
  for (const [deviceId, list] of map.entries()) {
    groups.push({
      deviceId,
      deviceName: deviceId === t('alarmList.noDevice') ? deviceId : (deviceNameMap.value[deviceId] || deviceId),
      alarms: list,
      firingCount: list.filter(a => a.status === 'firing').length,
      acknowledgedCount: list.filter(a => a.status === 'acknowledged').length,
      recoveredCount: list.filter(a => a.status === 'recovered').length,
    })
  }
  // 按 firing 数量降序，firing 多的设备优先展示
  groups.sort((a, b) => b.firingCount - a.firingCount || b.alarms.length - a.alarms.length)
  return groups
})

// FIXED-P2: 未确认（firing）报警行级高亮，AI 报警行保留紫色背景
function rowClassName(row: Alarm) {
  const classes: string[] = []
  if ((row as any).rule_type === 'ai_inference') classes.push('ai-alarm-row')
  if (row.status === 'firing') classes.push('alarm-firing-row')
  return classes.join(' ')
}

// [AUDIT-FIX] i18n 响应式：allColumns 改为 computed 实现语言切换响应式
const allColumns = computed(() => [
  { title: t('alarmList.alarmId'), key: 'alarm_id', width: 140 },
  { title: t('alarmList.ruleId'), key: 'rule_id', width: 140 },
  {
    title: t('alarmList.deviceId'), key: 'device_id', width: 160,
    render: (r: Alarm) => r.device_id
      ? h('a', { style: 'cursor:pointer;color:#2080f0', onClick: () => router.push(`/devices/${r.device_id}`) }, r.device_id)
      : '-',
  },
  {
    title: t('alarmList.deviceName'), key: 'device_name', width: 140,
    render: (r: Alarm) => r.device_id ? (deviceNameMap.value[r.device_id] || '-') : '-',
  },
  {
    title: t('alarmList.alarmType'), key: 'rule_type', width: 100,
    render: (r: Alarm) => {
      const isAi = (r as any).rule_type === 'ai_inference'
      const isScript = (r as any).rule_type === 'script'
      return h(NTag, {
        size: 'small',
        color: isAi ? { color: '#ede9fe', borderColor: '#8b5cf6', textColor: '#7c3aed' } : isScript ? { color: '#fef3c7', borderColor: '#f59e0b', textColor: '#b45309' } : undefined,
        type: (isAi || isScript) ? undefined : 'info',
        style: isAi ? 'background:linear-gradient(135deg,#ede9fe,#ddd6fe);border:none;' : isScript ? 'background:linear-gradient(135deg,#fef3c7,#fde68a);border:none;' : undefined,
      }, {
        default: () => isAi ? h(NSpace, { size: 4, align: 'center' }, {
          default: () => [h(NIcon, { component: SparklesOutline, size: 12 }), h('span', null, ' ' + t('alarm.aiAlarm'))]
        }) : isScript ? t('alarm.scriptAlarm') : t('alarm.thresholdAlarm'),
      })
    },
  },
  {
    title: t('alarmList.level'), key: 'severity', width: 100,
    render: (r: Alarm) => {
      const isAi = (r as any).rule_type === 'ai_inference'
      const isCritical = r.severity === 'critical'
      const children: any[] = [severityLabel.value[r.severity] || r.severity]  // FIXED-P3: computed→.value
      if (isAi && isCritical) {
        children.unshift(h('span', { class: 'pulse-dot', style: 'display:inline-block;width:8px;height:8px;border-radius:50%;background:#f56c6c;margin-right:6px;animation:pulse-anim 1.5s ease-in-out infinite;' }))
      }
      return h(NSpace, { size: 4, align: 'center' }, {
        default: () => [
          h(NTag, { type: severityColor[r.severity] || 'default', size: 'small' }, { default: () => children }),
        ],
      })
    },
  },
  {
    title: t('alarmList.status'), key: 'status', width: 90,
    render: (r: Alarm) => h(NTag, { type: statusColor[r.status] || 'default', size: 'small' }, { default: () => statusLabel.value[r.status] || r.status }),
  },
  { title: t('alarmList.triggerCount'), key: 'trigger_count', width: 80 },
  {
    title: t('alarmList.triggerValue'), key: 'trigger_value', width: 160,
    render: (r: Alarm) => r.trigger_value ? h(NTooltip, {}, { trigger: () => h('span', { style: 'max-width:140px;display:inline-block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap' }, JSON.stringify(r.trigger_value)), default: () => JSON.stringify(r.trigger_value) }) : '-',
  },
  { title: t('alarmList.triggerTime'), key: 'fired_at', width: 180, sorter: true, defaultSortOrder: 'descend', render: (r: Alarm) => r.fired_at ? formatDateTime(r.fired_at) : '-' },
  { title: t('alarmList.ackBy'), key: 'acknowledged_by', width: 100, render: (r: Alarm) => r.acknowledged_by || '-' },
  { title: t('alarmList.recoverTime'), key: 'recovered_at', width: 180, render: (r: Alarm) => r.recovered_at ? formatDateTime(r.recovered_at) : '-' },
  {
    title: t('common.actions'), key: 'actions', width: 100, fixed: 'right',
    render: (r: Alarm) => {
      const isAi = (r as any).rule_type === 'ai_inference'
      const options: any[] = []
      if (r.status === 'firing') {
        options.push({ label: t('alarmList.ack'), key: 'ack' })
        options.push({ label: t('alarmList.recover'), key: 'recover' })
        options.push({
          label: t('alarmSuppress.suppress'),
          key: 'suppress',
          children: [
            { label: t('alarmList.quickSilence', { duration: '1h' }), key: 'silence-1h' },
            { label: t('alarmList.quickSilence', { duration: '4h' }), key: 'silence-4h' },
            { label: t('alarmList.quickSilence', { duration: '24h' }), key: 'silence-24h' },
            { label: t('alarmSuppress.suppress') + '...', key: 'silence-custom' },
          ],
        })
        options.push({
          label: t('alarmList.ackAndSilence'),
          key: 'ack-and-silence',
          children: [
            { label: t('alarmList.ackAndSilenceHours', { duration: '1h' }), key: 'ack-silence-1h' },
            { label: t('alarmList.ackAndSilenceHours', { duration: '4h' }), key: 'ack-silence-4h' },
            { label: t('alarmList.ackAndSilenceHours', { duration: '24h' }), key: 'ack-silence-24h' },
          ],
        })
      }
      options.push({ label: t('alarmList.detail'), key: 'detail' })
      if (r.device_id) options.push({ label: t('alarmList.gotoDevice'), key: 'goto-device' })
      if (isAi) options.push({ label: t('alarmList.aiDetail'), key: 'ai-detail' })
      // 修复7: 启停规则——仅 operator 可操作，且需有 rule_id
      if (r.rule_id && auth.isOperator) {
        options.push({ label: t('alarmList.disableRule'), key: 'disable-rule' })
      }
      return h(NDropdown, { options, onSelect: (key: string) => handleAlarmAction(key, r) }, {
        trigger: () => h(NButton, { quaternary: true, size: 'small', ariaLabel: t('common.actions') }, { icon: () => h(NIcon, { component: EllipsisVerticalOutline }) })
      })
    },
  },
])

// 修复5: 列自定义——用户可勾选可见列，配置持久化到 localStorage
// [AUDIT-FIX] i18n 响应式：columnSettingOptions 改为 computed，依赖 allColumns computed 实现语言切换响应式
const ALARM_COLUMNS_STORAGE_KEY = 'alarm_columns_config'
const columnSettingOptions = computed(() =>
  allColumns.value
    .filter(c => c.key && c.title)
    .map(c => ({ label: c.title as string, value: c.key as string }))
)

function loadVisibleColumnKeys(): string[] {
  try {
    const raw = localStorage.getItem(ALARM_COLUMNS_STORAGE_KEY)
    if (raw) {
      const parsed = JSON.parse(raw)
      if (Array.isArray(parsed) && parsed.length) return parsed
    }
  } catch { /* ignore */ }
  return allColumns.value.filter(c => c.key).map(c => c.key as string)
}

const visibleColumnKeys = ref<string[]>(loadVisibleColumnKeys())

function onColumnVisibleChange(keys: (string | number)[]) {
  visibleColumnKeys.value = keys as string[]
  try { localStorage.setItem(ALARM_COLUMNS_STORAGE_KEY, JSON.stringify(keys)) } catch { /* ignore quota */ }
}

const columns = computed(() => {
  const visible = new Set(visibleColumnKeys.value)
  return allColumns.value.filter(c => !c.key || visible.has(c.key as string))
})

// 告警抑制功能
const showSuppressModal = ref(false)
const suppressing = ref(false)
const suppressFormRef = ref<any>(null)
const suppressForm = reactive({
  duration_seconds: 3600,
  reason: '',
})
const selectedAlarmForSuppress = ref<Alarm | null>(null)
const suppressRules = computed(() => ({
  reason: [
    { required: true, message: t('common.required'), trigger: ['blur', 'input'] },
    { max: 500, message: t('alarmSuppress.reasonLength', { max: 500 }), type: 'string', trigger: ['blur', 'input'] },
  ],
}))

// 修复21: 告警升级——未确认超时筛选
const escalationFilter = ref<'all' | 'overtime' | 'critical'>('all')
const ESCALATION_OVERTIME_MINUTES = 30  // 超过 30 分钟未确认视为升级
function setEscalationFilter(mode: 'all' | 'overtime' | 'critical') {
  escalationFilter.value = mode
  pagination.page = 1
  fetchAlarms()
}

// 修复22: 确认告警备注
const showAckRemarkModal = ref(false)
const ackRemark = ref('')
const ackRemarkAlarmId = ref('')

function openSuppressModal(alarm: Alarm) {
  selectedAlarmForSuppress.value = alarm
  suppressForm.duration_seconds = 3600
  suppressForm.reason = ''
  showSuppressModal.value = true
}

async function doSuppress() {
  // FIXED-P0: 前端权限检查，viewer 无操作权限
  if (!auth.isOperator) { message.warning(t('common.permissionDenied')); return }
  if (!selectedAlarmForSuppress.value) return
  try {
    await suppressFormRef.value?.validate()
  } catch {
    return
  }
  suppressing.value = true
  try {
    await alarmApi.suppress(
      selectedAlarmForSuppress.value.alarm_id,
      suppressForm.duration_seconds,
      suppressForm.reason
    )
    message.success(t('alarmSuppress.suppressSuccess'))
    showSuppressModal.value = false
    // FIXED-General: 静默成功后刷新列表，与 quickSuppress 行为保持一致
    fetchAlarms()
  } catch (e: any) {
    message.error(extractError(e, t('alarmSuppress.suppressFailed')))
  } finally {
    suppressing.value = false
  }
}

function openAiDetail(r: Alarm) {
  selectedAiAlarm.value = r
  showAiDetail.value = true
}

function handleAlarmAction(key: string, r: Alarm) {
  switch (key) {
    case 'ack': openAckRemarkModal(r.alarm_id); break
    case 'recover': doRecover(r.alarm_id); break
    case 'silence-1h': quickSuppress(r, 3600); break
    case 'silence-4h': quickSuppress(r, 14400); break
    case 'silence-24h': quickSuppress(r, 86400); break
    case 'silence-custom': openSuppressModal(r); break
    case 'ack-silence-1h': handleAckAndSilence(r.alarm_id, 1); break
    case 'ack-silence-4h': handleAckAndSilence(r.alarm_id, 4); break
    case 'ack-silence-24h': handleAckAndSilence(r.alarm_id, 24); break
    case 'detail': openAlarmDetail(r); break
    case 'goto-device': router.push(`/devices/${r.device_id}`); break
    case 'ai-detail': openAiDetail(r); break
    case 'disable-rule': handleDisableRule(r); break
  }
}

// 修复7: 禁用告警关联的规则——带权限检查与二次确认
async function handleDisableRule(alarm: Alarm) {
  if (!auth.isOperator) { message.warning(t('common.permissionDenied')); return }
  if (!alarm.rule_id) { message.warning(t('alarmList.noRuleId')); return }
  dialog.warning({
    title: t('common.confirm'),
    content: t('alarmList.disableRuleConfirm', { ruleId: alarm.rule_id }),
    positiveText: t('common.confirm'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      try {
        await ruleApi.disable(alarm.rule_id)
        message.success(t('alarmList.disableRuleSuccess'))
        fetchAlarms()
      } catch (e: any) {
        message.error(extractError(e, t('alarmList.disableRuleFailed')))
      }
    },
  })
}

// 组合操作：先确认告警，再静默 N 小时
async function handleAckAndSilence(alarmId: string, hours: number) {
  if (!auth.isOperator) { message.warning(t('common.permissionDenied')); return }
  if (ackingIds.value.has(alarmId)) return
  ackingIds.value.add(alarmId)
  const snapshot = alarms.value.map(a => ({ ...a }))
  alarms.value = alarms.value.map(a => a.alarm_id === alarmId ? {
    ...a,
    status: 'acknowledged',
    acknowledged_by: auth.username,
    acknowledged_at: new Date().toISOString(),
  } : a)
  try {
    await alarmApi.ack(alarmId)
    try {
      await alarmApi.suppress(alarmId, hours * 3600, t('alarmList.ackAndSilence'))
      message.success(t('alarmList.ackAndSilenceSuccess'))
    } catch (e: any) {
      // 确认已成功，仅静默失败
      message.warning(extractError(e, t('alarmList.ackAndSilenceFailed')))
    }
    fetchAlarms()
  } catch (e: any) {
    alarms.value = snapshot
    message.error(extractError(e, t('alarmList.ackAndSilenceFailed')))
  } finally {
    ackingIds.value.delete(alarmId)
  }
}

async function quickSuppress(r: Alarm, durationSeconds: number) {
  // FIXED-P0: 前端权限检查，viewer 无操作权限
  if (!auth.isOperator) { message.warning(t('common.permissionDenied')); return }
  try {
    await alarmApi.suppress(r.alarm_id, durationSeconds, t('alarmSuppress.reasonPlaceholder'))
    message.success(t('alarmSuppress.suppressSuccess'))
    fetchAlarms()
  } catch (e: any) {
    message.error(extractError(e, t('alarmSuppress.suppressFailed')))
  }
}

// FIXED-P2: 排序变化时更新 sortState 并重新拉取数据（服务端排序）
function handleSorterChange(sorter: any) {
  sortState.value = sorter && sorter.order ? { columnKey: sorter.columnKey, order: sorter.order } : null
  pagination.page = 1
  fetchAlarms()
}

async function fetchAlarms() {
  loading.value = true
  try {
    // UX-FIX-04: 传递 device_id 与时间范围到后端
    const params: any = {
      page: pagination.page,
      size: pagination.pageSize,
      status: filterStatus.value ?? undefined,
      severity: filterSeverity.value ?? undefined,
      search: searchText.value || undefined,
      // FIXED-P2: 客户端排序仅限当前页，改为服务端排序
      sort_by: (sortState.value as any)?.columnKey,
      sort_order: (sortState.value as any)?.order === 'ascend' ? 'asc' : (sortState.value as any)?.order === 'descend' ? 'desc' : undefined,
      // FIXED-P2: filterType 原仅客户端过滤，分页 itemCount 不准确，改为服务端过滤
      rule_type: filterType.value === 'ai' ? 'ai_inference' : filterType.value === 'threshold' ? 'threshold' : filterType.value === 'script' ? 'script' : undefined,
      device_id: filterDeviceId.value || undefined,
    }
    // 修复21: 告警升级筛选
    if (escalationFilter.value === 'critical') {
      params.status = 'firing'
      params.severity = 'critical'
    } else if (escalationFilter.value === 'overtime') {
      params.status = 'firing'
      params.unack_overtime_minutes = ESCALATION_OVERTIME_MINUTES
    }
    if (filterTimeRange.value && filterTimeRange.value.length === 2) {
      params.start_time = new Date(filterTimeRange.value[0]).toISOString()
      params.end_time = new Date(filterTimeRange.value[1]).toISOString()
    }
    const data = await alarmApi.list(params)
    let list = Array.isArray(data?.data) ? data.data : []
    // 修复21: 客户端兜底过滤未确认超时（后端不支持该参数时）
    if (escalationFilter.value === 'overtime') {
      const now = Date.now()
      list = list.filter((a: any) => {
        const firedAt = a.fired_at ? new Date(a.fired_at).getTime() : 0
        return (now - firedAt) >= ESCALATION_OVERTIME_MINUTES * 60 * 1000
      })
    }
    alarms.value = list
    pagination.itemCount = data?.total ?? 0
    // FIXED: 刷新全局告警统计（含 AI 告警总数/占比/MTTR/MTBF），非阻塞避免影响列表加载
    fetchAlarmStatistics()
  } catch (e: any) {
    // FIXED-P1: 失败时不清空已有列表，保留用户已加载的数据
    message.error(extractError(e, t('alarmList.fetchFailed')))
  } finally {
    loading.value = false
  }
}

async function fetchAiStats() {
  try {
    const data = await aiApi.getStats()
    aiStats.value = data || {}
  } catch {
    aiStats.value = {}
  }
}

async function doAck(alarmId: string, remark?: string) {
  // FIXED-P0: 前端权限检查，viewer 无操作权限
  if (!auth.isOperator) { message.warning(t('common.permissionDenied')); return }
  // FIXED-P1: 独立 loading 标志，避免与 doRecover 互相阻塞
  if (ackingIds.value.has(alarmId)) return
  ackingIds.value.add(alarmId)
  // FIXED-P1: 乐观更新 - 先快照，再本地更新，失败回滚
  const snapshot = alarms.value.map(a => ({ ...a }))
  alarms.value = alarms.value.map(a => a.alarm_id === alarmId ? {
    ...a,
    status: 'acknowledged',
    acknowledged_by: auth.username,
    acknowledged_at: new Date().toISOString(),
  } : a)
  try {
    await alarmApi.ack(alarmId, remark)
    message.success(t('alarmList.ackSuccess'))
  } catch (e: any) {
    alarms.value = snapshot
    message.error(extractError(e, t('alarmList.ackFailed')))
  } finally {
    ackingIds.value.delete(alarmId)
  }
}

// 修复22: 打开确认备注弹窗
function openAckRemarkModal(alarmId: string) {
  if (!auth.isOperator) { message.warning(t('common.permissionDenied')); return }
  ackRemarkAlarmId.value = alarmId
  ackRemark.value = ''
  showAckRemarkModal.value = true
}

// 修复22: 确认带备注
async function confirmAckWithRemark() {
  const alarmId = ackRemarkAlarmId.value
  if (!alarmId) return
  const remark = ackRemark.value.trim()
  showAckRemarkModal.value = false
  await doAck(alarmId, remark || undefined)
}

async function doRecover(alarmId: string) {
  // FIXED-P0: 前端权限检查，viewer 无操作权限
  if (!auth.isOperator) { message.warning(t('common.permissionDenied')); return }
  // FIXED-P1: 独立 loading 标志，避免与 doAck 互相阻塞
  if (recoveringIds.value.has(alarmId)) return
  recoveringIds.value.add(alarmId)
  // FIXED-P1: 乐观更新 - 先快照，再本地更新，失败回滚
  const snapshot = alarms.value.map(a => ({ ...a }))
  alarms.value = alarms.value.map(a => a.alarm_id === alarmId ? {
    ...a,
    status: 'recovered',
    recovered_at: new Date().toISOString(),
  } : a)
  try {
    await alarmApi.recover(alarmId)
    message.success(t('alarmList.recoverSuccess'))
  } catch (e: any) {
    alarms.value = snapshot
    message.error(extractError(e, t('alarmList.recoverFailed')))
  } finally {
    recoveringIds.value.delete(alarmId)
  }
}

async function fetchDeviceNames() {
  // FIXED-General: 原拉取 9999 条完整设备对象（含 points/config）仅为构建 id→name 映射
  // 改为分批加载最多 200 条，并在告警列表返回 device_name 时直接复用，避免二次查询
  // FIX-PERF2: 限制分页大小为 200，避免大数据量全量加载导致性能问题
  try {
    const data = await deviceApi.list({ page: 1, size: 200 })
    const map: Record<string, string> = {}
    for (const d of (data?.data ?? [])) {
      map[d.device_id] = d.name
    }
    deviceNameMap.value = map
  } catch { /* ignore */ }
}

function openAlarmDetail(r: Alarm) {
  selectedAlarm.value = r
  showDetailModal.value = true
  // 修复9: 加载该规则的历史触发记录
  loadAlarmHistory(r)
}

// 修复9: 加载指定告警规则的历史触发记录
async function loadAlarmHistory(alarm: Alarm) {
  if (!alarm.rule_id) { alarmHistory.value = []; return }
  alarmHistoryLoading.value = true
  try {
    const data = await alarmApi.getHistory(alarm.rule_id, 7)
    alarmHistory.value = Array.isArray(data) ? data : []
  } catch {
    alarmHistory.value = []
  } finally {
    alarmHistoryLoading.value = false
  }
}

// FIXED-Severe: 导出所有符合筛选条件的告警（分页循环拉取），而非仅当前页
async function handleExport() {
  if (exporting.value) return
  exporting.value = true
  try {
    // 拉取所有符合筛选条件的告警（分页循环）
    const allAlarms: any[] = []
    let page = 1
    const size = 200
    let total = 0
    do {
      const params: any = {
        page,
        size,
        status: filterStatus.value ?? undefined,
        severity: filterSeverity.value ?? undefined,
        search: searchText.value || undefined,
        sort_by: (sortState.value as any)?.columnKey,
        sort_order: (sortState.value as any)?.order === 'ascend' ? 'asc' : (sortState.value as any)?.order === 'descend' ? 'desc' : undefined,
        rule_type: filterType.value === 'ai' ? 'ai_inference' : filterType.value === 'threshold' ? 'threshold' : filterType.value === 'script' ? 'script' : undefined,
        device_id: filterDeviceId.value || undefined,
      }
      if (filterTimeRange.value && filterTimeRange.value.length === 2) {
        params.start_time = new Date(filterTimeRange.value[0]).toISOString()
        params.end_time = new Date(filterTimeRange.value[1]).toISOString()
      }
      const data = await alarmApi.list(params)
      allAlarms.push(...(Array.isArray(data?.data) ? data.data : []))
      total = data?.total ?? 0
      page++
    } while (allAlarms.length < total && page <= 50)  // 最多 50 页 = 10000 条
    // [AUDIT-FIX] 严重级-达到导出上限时提示用户缩小筛选范围，避免数据被静默截断
    if (allAlarms.length < total) {
      message.warning(t('alarmList.exportTruncated'))
    }
    if (allAlarms.length === 0) {
      message.warning(t('alarmList.noDataToExport'))
      return
    }
    const rows = allAlarms
    const headers = [
      'alarm_id', 'rule_id', 'device_name', 'severity', 'status',
      'fired_at', 'recovered_at', 'acknowledged_at', 'message',
    ]
    const escapeCsv = (v: any) => {
      const s = v == null ? '' : String(v)
      if (s.includes(',') || s.includes('"') || s.includes('\n')) {
        return `"${s.replace(/"/g, '""')}"`
      }
      return s
    }
    const csvLines = [headers.join(',')]
    for (const r of rows) {
      csvLines.push([
        r.alarm_id,
        r.rule_id || '',
        r.device_id ? (deviceNameMap.value[r.device_id] || r.device_id) : '',
        r.severity || '',
        r.status || '',
        r.fired_at ? formatDateTime(r.fired_at) : '',
        r.recovered_at ? formatDateTime(r.recovered_at) : '',
        r.acknowledged_at ? formatDateTime(r.acknowledged_at) : '',
        r.message || '',
      ].map(escapeCsv).join(','))
    }
    const csv = '\uFEFF' + csvLines.join('\n')
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `alarms_${new Date().toISOString().slice(0, 10)}.csv`
    a.click()
    // [AUDIT-FIX] 严重级-Firefox 下载为异步触发，立即 revoke 可能导致下载失败，延迟 1 秒
    setTimeout(() => URL.revokeObjectURL(url), 1000)
    message.success(t('alarmList.exportSuccess'))
  } catch (e: any) {
    message.error(extractError(e, t('alarmList.exportFailed')))
  } finally {
    exporting.value = false
  }
}

async function handleBatchAckSelected() {
  // FIXED-P0: 前端权限检查，viewer 无操作权限
  if (!auth.isOperator) { message.warning(t('common.permissionDenied')); return }
  if (!checkedKeys.value.length) return
  dialog.warning({
    title: t('common.confirm'),
    content: t('alarmList.batchAckSelected', { count: checkedKeys.value.length }),
    positiveText: t('common.confirm'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      batchAcking.value = true
      try {
        // FIXED: 改用后端批量确认接口，N 条报警一次 HTTP 请求
        const result = await alarmApi.batchAck(checkedKeys.value)
        const succeeded = result.success_count
        const failed = result.failed_count
        if (failed > 0) {
          message.warning(t('alarmList.batchAckResult', { success: succeeded, failed }))
        } else {
          message.success(t('alarmList.batchAckResultAll', { success: succeeded }))
        }
        checkedKeys.value = []
        fetchAlarms()
      } catch (e: any) {
        message.error(extractError(e, t('alarmList.ackFailed')))
      } finally {
        batchAcking.value = false
      }
    },
  })
}

// 组合批量操作：先批量确认，再逐条静默（默认 1 小时）
async function handleBatchAckAndSilenceSelected() {
  if (!auth.isOperator) { message.warning(t('common.permissionDenied')); return }
  if (!checkedKeys.value.length) return
  dialog.warning({
    title: t('common.confirm'),
    content: t('alarmList.batchAckAndSilence', { count: checkedKeys.value.length }),
    positiveText: t('common.confirm'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      batchAcking.value = true
      const ids = [...checkedKeys.value]
      try {
        const result = await alarmApi.batchAck(ids)
        const succeeded = result.success_count
        const failed = result.failed_count
        // FIXED: 对确认成功的告警并发静默 1 小时（限制并发数避免压垮后端，原顺序 for 循环对 N 条告警需 N 次 RTT）
        const CONCURRENCY = 5
        let silenceFailed = 0
        for (let i = 0; i < ids.length; i += CONCURRENCY) {
          const batch = ids.slice(i, i + CONCURRENCY)
          const settled = await Promise.allSettled(
            batch.map(id => alarmApi.suppress(id, 3600, t('alarmList.ackAndSilence')))
          )
          for (const r of settled) {
            if (r.status !== 'fulfilled') silenceFailed++
          }
        }
        const totalFailed = failed + silenceFailed
        if (totalFailed > 0) {
          message.warning(t('alarmList.batchAckAndSilenceResult', { success: succeeded - silenceFailed, failed: totalFailed }))
        } else {
          message.success(t('alarmList.batchAckAndSilenceResultAll', { success: succeeded }))
        }
        checkedKeys.value = []
        fetchAlarms()
      } catch (e: any) {
        message.error(extractError(e, t('alarmList.ackAndSilenceFailed')))
      } finally {
        batchAcking.value = false
      }
    },
  })
}

// UX-FIX-05: 一键全部确认当前筛选条件下的 firing 报警
// 注意：此处仅确认当前页可见的 firing 报警，避免一次性确认过多导致后端压力
async function handleAckAllFiring() {
  if (!auth.isOperator) { message.warning(t('common.permissionDenied')); return }
  const firingIds = alarms.value.filter(a => a.status === 'firing').map(a => a.alarm_id)
  if (!firingIds.length) return
  batchAcking.value = true
  try {
    // FIXED: 改用后端批量确认接口，N 条报警一次 HTTP 请求
    const result = await alarmApi.batchAck(firingIds)
    const succeeded = result.success_count
    const failed = result.failed_count
    if (failed > 0) {
      message.warning(t('alarmList.batchAckResult', { success: succeeded, failed }))
    } else {
      message.success(t('alarmList.batchAckResultAll', { success: succeeded }))
    }
    fetchAlarms()
  } catch (e: any) {
    message.error(extractError(e, t('alarmList.ackFailed')))
  } finally {
    batchAcking.value = false
  }
}

// UX-FIX-05: 设备筛选下拉选项（复用已加载的 deviceNameMap）
const deviceFilterOptions = computed(() =>
  Object.entries(deviceNameMap.value).map(([id, name]) => ({ label: name || id, value: id }))
)

// UX-FIX-05: 快捷键——A 确认当前页所有 firing，Space 确认选中
function onAlarmKeydown(e: KeyboardEvent) {
  // 忽略输入框中的按键
  const target = e.target as HTMLElement
  if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable)) return
  if (!auth.isOperator) return
  if (e.key === 'a' || e.key === 'A') {
    if (firingAlarms.value.length > 0 && !batchAcking.value) {
      e.preventDefault()
      handleAckAllFiring()
    }
  } else if (e.key === ' ' || e.code === 'Space') {
    // FIXED: Space 确认选中报警，原注释承诺但未实现。阻止默认滚动行为。
    if (checkedKeys.value.length > 0 && !batchAcking.value) {
      e.preventDefault()
      handleBatchAckSelected()
    }
  }
}

onMounted(() => {
  fetchAlarms()
  fetchAiStats()
  fetchAlarmStatistics()
  fetchAlarmTrend()
  fetchTopAlarms()  // 修复7: 加载 Top10 报警统计
  fetchDeviceNames()
  fetchSilenceList()
  // FIX: 静默规则选项改为远程搜索，不再 onMounted 全量加载
  // 修复1: 请求桌面通知权限，critical 报警时弹出系统通知
  if ('Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission()
  }
  ws.connect('alarm', onAlarmPush)
  // FIXED-P0: 保存status handler引用以便onUnmounted中注销，原匿名函数无法被移除导致泄漏
  alarmWsStatusHandler = (status: string) => {
    if (status === 'disconnected' || status === 'error') {
      message.warning(t('alarmList.wsDisconnected'))
    } else if (status === 'connected') {
      // FIXED-Bug19: WS 重连后触发补偿查询，拉取断线期间所有状态变更的报警（含 ack/recover）
      // 之前：只拉 status='firing' 报警，不更新已存在报警的 ack/recover 状态，导致"幽灵 firing 报警"
      if (lastSeenAlarmTs) {
        // FIXED: WS 重连补偿查询添加 size 限制，避免断线时间过长导致无界拉取
        alarmApi.list({ since: lastSeenAlarmTs, size: 200 }).then((data: any) => {
          const missed = Array.isArray(data?.data) ? data.data : []
          if (missed.length) {
            const existingMap = new Map(alarms.value.map(a => [a.alarm_id, a]))
            const toInsert: Alarm[] = []
            for (const a of missed) {
              if (!a.alarm_id) continue
              const existing = existingMap.get(a.alarm_id)
              if (existing) {
                // 同步状态变更：更新已存在报警的 ack/recover 状态
                Object.assign(existing, {
                  status: a.status || existing.status,
                  acknowledged_at: a.acknowledged_at ?? existing.acknowledged_at,
                  acknowledged_by: a.acknowledged_by ?? existing.acknowledged_by,
                  recovered_at: a.recovered_at ?? existing.recovered_at,
                })
              } else {
                toInsert.push(a as Alarm)
              }
            }
            if (toInsert.length) {
              alarms.value = [...toInsert, ...alarms.value]
            }
            // 更新最后收到报警的时间
            lastSeenAlarmTs = missed[0].fired_at || missed[0].updated_at || lastSeenAlarmTs
          }
        }).catch(() => { /* ignore compensation errors */ })
      }
      // FIXED-Bug19: 重连后触发全量同步，确保前端状态与后端一致
      fetchAlarms()
    }
  }
  ws.onStatus('alarm', alarmWsStatusHandler)
  // UX-FIX-05: 注册快捷键监听
  window.addEventListener('keydown', onAlarmKeydown)
  // 静默列表剩余时间倒计时：每分钟更新一次
  _silenceTimer = setInterval(() => { silenceNow.value = Date.now() }, 60000)
})
onUnmounted(() => {
  ws.disconnect('alarm', onAlarmPush)
  // FIXED-P0: 注销status handler防止泄漏
  if (alarmWsStatusHandler) { ws.offStatus('alarm', alarmWsStatusHandler); alarmWsStatusHandler = null }
  if (alarmDebounceTimer) { clearTimeout(alarmDebounceTimer); alarmDebounceTimer = null }
  // FIXED: 清理搜索防抖定时器
  if (_searchTimer) { clearTimeout(_searchTimer); _searchTimer = null }
  // UX-FIX-05: 注销快捷键监听
  window.removeEventListener('keydown', onAlarmKeydown)
  // 清理静默列表倒计时定时器
  if (_silenceTimer) { clearInterval(_silenceTimer); _silenceTimer = null }
})

// [AUDIT-FIX] 严重级-keep-alive 缓存页面生命周期处理：deactivated 时断开 WS 和清除定时器，activated 时恢复
// onActivated 在首次挂载时也会触发（onMounted 之后），ws.connect 内部有防重复守卫，已连接时不会重复创建
onActivated(() => {
  // 重新连接 WS alarm 频道
  ws.connect('alarm', onAlarmPush)
  // 刷新告警列表
  fetchAlarms()
  // 重启 _silenceTimer
  if (_silenceTimer) { clearInterval(_silenceTimer); _silenceTimer = null }
  _silenceTimer = setInterval(() => { silenceNow.value = Date.now() }, 60000)
})
onDeactivated(() => {
  // 断开 WS
  ws.disconnect('alarm', onAlarmPush)
  // 清除 _silenceTimer
  if (_silenceTimer) { clearInterval(_silenceTimer); _silenceTimer = null }
})

// FIXED: 搜索输入防抖（300ms），避免每次按键触发 API 请求造成后端压力与列表闪烁
let _searchTimer: ReturnType<typeof setTimeout> | null = null
function _triggerSearch() {
  pagination.page = 1
  fetchAlarms()
}
function onSearchInput() {
  if (_searchTimer) clearTimeout(_searchTimer)
  _searchTimer = setTimeout(() => {
    _searchTimer = null
    _triggerSearch()
  }, 300)
}
function onSearchEnter() {
  if (_searchTimer) { clearTimeout(_searchTimer); _searchTimer = null }
  _triggerSearch()
}

let alarmDebounceTimer: ReturnType<typeof setTimeout> | null = null
let alarmWsStatusHandler: ((status: string) => void) | null = null  // FIXED-P0: status handler引用
// FIXED-P2: 断线期间报警离线缓存补偿 - 记录最后收到报警的时间，WS 重连后据此拉取缺失报警
let lastSeenAlarmTs: string | null = null

// 修复1: critical 报警桌面通知 - 用户切换页面或最小化浏览器时不漏报
function showDesktopNotification(alarm: Alarm) {
  if (!('Notification' in window)) return
  const title = `${t('alarm.critical')} · ${alarm.rule_id || t('alarmList.alarmId')}`
  const body = alarm.message || (alarm.device_id ? `device: ${alarm.device_id}` : '')
  if (Notification.permission === 'granted') {
    try {
      const n = new Notification(title, {
        body,
        tag: alarm.alarm_id,
        requireInteraction: true,
      })
      n.onclick = () => {
        window.focus()
        router.push({ name: 'Alarms', query: { alarmId: alarm.alarm_id } })
        n.close()
      }
    } catch { /* ignore notification errors */ }
  }
  // 权限未授予时降级为声音提醒（NotificationCenter 已实现声音报警，此处不重复）
}

function onAlarmPush(data: any) {
  if (!data) return
  // FIXED-P0: 原问题-推送数据字段与 Alarm 接口不匹配（id/triggered_at/acknowledged 等不存在于 Alarm）
  // 且仅处理 trigger 动作，忽略 acknowledged/recovered。改为正确映射字段并处理所有动作
  if (data.type === 'alarm' && data.data) {
    const action = data.action
    const payload = Array.isArray(data.data) ? data.data : [data.data]
    // FIXED-BugR9: 兼容 firing 动作（与 NotificationCenter 一致），避免告警延迟 500ms 才显示
    if (action === 'trigger' || action === 'firing') {
      // FIXED-P0: 正确映射 Alarm 接口字段
      const mapped: Alarm[] = payload.filter((a: any) => a.alarm_id || a.id).map((a: any) => ({
        alarm_id: a.alarm_id || a.id,
        rule_id: a.rule_id || '',
        device_id: a.device_id ?? null,
        severity: a.severity || 'warning',
        status: a.status || (data.action === 'recover' ? 'recovered' : data.action === 'acknowledged' ? 'acknowledged' : 'firing'),
        message: a.message || '',
        trigger_value: a.trigger_value ?? {},
        trigger_count: a.trigger_count ?? 1,
        rule_type: a.rule_type || 'threshold',
        fired_at: a.fired_at || a.timestamp || new Date().toISOString(),
        acknowledged_at: a.acknowledged_at ?? null,
        acknowledged_by: a.acknowledged_by ?? null,
        recovered_at: a.recovered_at ?? null,
        version: a.version ?? 0,
      }))
      if (mapped.length) {
        // FIXED-P2: 更新最后收到报警的时间，用于断线补偿
        lastSeenAlarmTs = mapped[0].fired_at
        // 去重：避免重复插入已存在的报警
        const existingIds = new Set(alarms.value.map(a => a.alarm_id))
        const toInsert = mapped.filter(a => !existingIds.has(a.alarm_id))
        if (toInsert.length) {
          // FIXED-General: 限制内存中告警数组上限，避免 WS 持续推送导致内存增长
          const MAX_ALARMS_IN_MEMORY = 500
          alarms.value = [...toInsert, ...alarms.value].slice(0, MAX_ALARMS_IN_MEMORY)
        }
        // 修复1: critical 报警桌面通知，用户切换页面或最小化浏览器时不会错过
        for (const a of toInsert) {
          if (a.severity === 'critical') {
            showDesktopNotification(a)
          }
        }
      }
    } else if (action === 'acknowledged' || action === 'recovered') {
      // FIXED-P0: 处理 ack/recover 动作，增量更新已有报警状态
      const ids = new Set(payload.map((a: any) => a.alarm_id || a.id))
      alarms.value = alarms.value.map(a => {
        if (!ids.has(a.alarm_id)) return a
        const update = payload.find((x: any) => (x.alarm_id || x.id) === a.alarm_id)
        return {
          ...a,
          status: action === 'acknowledged' ? 'acknowledged' : 'recovered',
          acknowledged_at: update?.acknowledged_at ?? a.acknowledged_at,
          acknowledged_by: update?.acknowledged_by ?? a.acknowledged_by,
          recovered_at: update?.recovered_at ?? a.recovered_at,
        }
      })
    }
    // FIXED-P1: 原问题-防抖代码在 if 块外部，对所有 WS 消息（含心跳 pong）执行，
    // 导致每隔约 25 秒（心跳周期）自动触发一次全量拉取，造成不必要的网络流量和状态闪烁
    // 修复：将防抖同步移入 if 块内，仅在实际处理了报警消息后才触发后台同步
    if (alarmDebounceTimer) clearTimeout(alarmDebounceTimer)
    alarmDebounceTimer = setTimeout(() => {
      fetchAlarms()
      fetchAiStats()
      alarmDebounceTimer = null
    }, 500)
  }
}
</script>

<style scoped>
.ai-alarm-row {
  background: rgba(139, 92, 246, 0.06);
}
/* FIXED-P2: 未确认（firing）报警行级高亮 */
.alarm-firing-row {
  background: rgba(239, 68, 68, 0.08) !important;
  border-left: 3px solid #ef4444;
}
.ai-stat-card {
  border-radius: 8px;
  transition: all 0.3s ease;
  color: #fff !important;
}
.ai-stat-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 8px 16px rgba(0,0,0,0.1);
}
.ai-stat-card-purple { background: linear-gradient(135deg, #8b5cf6 0%, #7c3aed 100%); }
.ai-stat-card-indigo { background: linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%); }
.ai-stat-card-cyan { background: linear-gradient(135deg, #06b6d4 0%, #8b5cf6 100%); }
.ai-stat-card-pink { background: linear-gradient(135deg, #ec4899 0%, #8b5cf6 100%); }
.ai-ratio-num {
  color: #fff;
  font-size: 22px;
  font-weight: 700;
}
.ai-stat-card :deep(.n-statistic .n-statistic-value__content),
.ai-stat-card :deep(.n-statistic .n-statistic-value),
.ai-stat-card :deep(.n-statistic .n-statistic-value__integer),
.ai-stat-card :deep(.n-statistic .n-statistic-value__fraction),
.ai-stat-card :deep(.n-statistic__label),
.ai-stat-card :deep(.n-icon),
.ai-stat-card :deep(.n-statistic-value__suffix) {
  color: #fff !important;
}
@keyframes pulse-anim {
  0% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.4; transform: scale(1.4); }
  100% { opacity: 1; transform: scale(1); }
}
.pulse-dot {
  animation: pulse-anim 1.5s ease-in-out infinite;
}
@media (prefers-reduced-motion: reduce) {
  .pulse-dot {
    animation: none;
  }
}
</style>
