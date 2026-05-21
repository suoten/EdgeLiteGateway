<template>
  <n-spin :show="pageLoading" :description="t('expressionConfig.loading')">
  <div class="expression-page">
    <n-card :title="t('expressionConfig.title')">
      <n-grid :cols="2" :x-gap="16">
        <n-gi>
          <n-card :title="t('expressionConfig.editor')" size="small">
            <n-form label-placement="left" label-width="80">
              <n-form-item :label="t('expressionConfig.expression')">
                <n-input v-model:value="expression" type="textarea" :rows="3" :placeholder="t('expressionConfig.expressionPlaceholder')" />
              </n-form-item>
              <n-form-item :label="t('expressionConfig.variables')">
                <n-dynamic-input v-model:value="variables" :on-create="() => ({ key: '', value: '0' })">
                  <template #default="{ value: item }">
                    <n-space align="center">
                      <n-input v-model:value="item.key" :placeholder="t('expressionConfig.variableName')" size="small" style="width: 150px" />
                      <span>=</span>
                      <n-input v-model:value="item.value" :placeholder="t('expressionConfig.variableValue')" size="small" style="width: 100px" />
                    </n-space>
                  </template>
                </n-dynamic-input>
              </n-form-item>
            </n-form>
            <n-space style="margin-top: 12px">
              <n-button type="primary" :loading="evaluating" @click="evaluate">{{ t('expressionConfig.calculate') }}</n-button>
              <n-button @click="validate">{{ t('expressionConfig.validateSyntax') }}</n-button>
            </n-space>
            <n-card v-if="result !== null" :title="t('expressionConfig.result')" size="small" style="margin-top: 12px">
              <n-tag :type="resultValid ? 'success' : 'error'" size="large">{{ result }}</n-tag>
            </n-card>
          </n-card>
        </n-gi>
        <n-gi>
          <n-card :title="t('expressionConfig.availableFunctions')" size="small">
            <n-data-table :columns="funcColumns" :data="functions" size="small" :max-height="300" />
          </n-card>
          <n-card :title="t('expressionConfig.availableOperators')" size="small" style="margin-top: 12px">
            <n-space>
              <n-tag v-for="op in operators" :key="op.symbol" size="small">{{ op.symbol }} {{ op.description }}</n-tag>
            </n-space>
          </n-card>
          <n-card :title="t('expressionConfig.batchTest')" size="small" style="margin-top: 12px">
            <n-input v-model:value="batchExpr" type="textarea" :rows="3" placeholder='{"fahrenheit": "${sensor.temp} * 1.8 + 32", "status": "${sensor.temp} > 100"}' />
            <n-button size="small" style="margin-top: 8px" :loading="calculating" @click="evaluateBatch">{{ t('expressionConfig.batchCalculate') }}</n-button>
            <n-code v-if="batchResult" :code="batchResult" language="json" style="margin-top: 8px" />
          </n-card>
        </n-gi>
      </n-grid>
    </n-card>
  </div>
  </n-spin>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { NCard, NButton, NInput, NForm, NFormItem, NSpace, NTag, NDataTable, NDynamicInput, NGrid, NGi, NCode, NSpin, useMessage } from 'naive-ui'
import { expressionApi } from '@/api'
import { t } from '@/i18n'

const message = useMessage()
const expression = ref('')
const variables = ref<{ key: string; value: string }[]>([])
const evaluating = ref(false)
const result = ref<string | null>(null)
const resultValid = ref(true)
const functions = ref<any[]>([])
const operators = ref<any[]>([])
const batchExpr = ref('')
const batchResult = ref('')
const calculating = ref(false)
const pageLoading = ref(true)

const funcColumns = [
  { title: t('expressionConfig.colFunction'), key: 'name', width: 80 },
  { title: t('expressionConfig.colDescription'), key: 'description' },
  { title: t('expressionConfig.colExample'), key: 'example' },
]

function buildVarMap() {
  const m: Record<string, any> = {}
  for (const v of variables.value) {
    if (v.key) {
      const num = Number(v.value)
      m[v.key] = isNaN(num) ? v.value : num
    }
  }
  return m
}

async function evaluate() {
  evaluating.value = true
  result.value = null
  try {
    const data = await expressionApi.evaluate(expression.value, buildVarMap())
    result.value = String(data?.result ?? 'null')
    resultValid.value = true
  } catch (e: any) {
    result.value = e.response?.data?.detail || t('expressionConfig.calculateFailed')
    resultValid.value = false
  } finally { evaluating.value = false }
}

async function validate() {
  try {
    const data = await expressionApi.validate(expression.value, buildVarMap())
    if (data) {
      result.value = data.valid ? t('expressionConfig.syntaxValid') : t('expressionConfig.syntaxError', { error: data.error })
      resultValid.value = data.valid
    }
  } catch (e: any) {
    result.value = e.response?.data?.detail || t('expressionConfig.validateFailed')
    resultValid.value = false
  }
}

async function evaluateBatch() {
  calculating.value = true
  try {
    const exprs = JSON.parse(batchExpr.value)
    const data = await expressionApi.evaluateBatch(exprs, buildVarMap())
    batchResult.value = JSON.stringify(data?.results, null, 2)
  } catch (e: any) {
    batchResult.value = t('expressionConfig.error') + (e.response?.data?.detail || e.message)
  } finally { calculating.value = false }
}

async function loadFunctions() {
  try {
    const data = await expressionApi.functions()
    functions.value = data?.functions || []
    operators.value = data?.operators || []
  } catch (e: any) { message.error(e?.response?.data?.detail || e?.message || t('expressionConfig.loadFunctionsFailed')) }
  finally { pageLoading.value = false }
}

onMounted(() => { loadFunctions() })
</script>

<style scoped>
.expression-page { padding: 16px; }
</style>
