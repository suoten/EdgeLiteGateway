<template>
  <n-spin :show="pageLoading" :description="t('expressionConfig.loading')">  // FIXED: 原问题-中文硬编码，改用i18n
  <div class="expression-page">
    <n-card :title="t('expressionConfig.title')">  // FIXED: 原问题-中文硬编码，改用i18n
      <n-grid :cols="2" :x-gap="16">
        <n-gi>
          <n-card :title="t('expressionConfig.editor')" size="small">  // FIXED: 原问题-中文硬编码，改用i18n
            <n-form label-placement="left" label-width="80">
              <n-form-item :label="t('expressionConfig.expression')">  // FIXED: 原问题-中文硬编码，改用i18n
                <n-input v-model:value="expression" type="textarea" :rows="3" :placeholder="t('expressionConfig.expressionPlaceholder')" />  // FIXED: 原问题-中文硬编码，改用i18n
              </n-form-item>
              <n-form-item :label="t('expressionConfig.variables')">  // FIXED: 原问题-中文硬编码，改用i18n
                <n-dynamic-input v-model:value="variables" :on-create="() => ({ key: '', value: '0' })">
                  <template #default="{ value: item }">
                    <n-space align="center">
                      <n-input v-model:value="item.key" :placeholder="t('expressionConfig.variableName')" size="small" style="width: 150px" />  // FIXED: 原问题-中文硬编码，改用i18n
                      <span>=</span>
                      <n-input v-model:value="item.value" :placeholder="t('expressionConfig.variableValue')" size="small" style="width: 100px" />  // FIXED: 原问题-中文硬编码，改用i18n
                    </n-space>
                  </template>
                </n-dynamic-input>
              </n-form-item>
            </n-form>
            <n-space style="margin-top: 12px">
              <n-button type="primary" :loading="evaluating" @click="evaluate">{{ t('expressionConfig.calculate') }}</n-button>  // FIXED: 原问题-中文硬编码，改用i18n
              <n-button @click="validate">{{ t('expressionConfig.validateSyntax') }}</n-button>  // FIXED: 原问题-中文硬编码，改用i18n
            </n-space>
            <n-card v-if="result !== null" :title="t('expressionConfig.result')" size="small" style="margin-top: 12px">  // FIXED: 原问题-中文硬编码，改用i18n
              <n-tag :type="resultValid ? 'success' : 'error'" size="large">{{ result }}</n-tag>
            </n-card>
          </n-card>
        </n-gi>
        <n-gi>
          <n-card :title="t('expressionConfig.availableFunctions')" size="small">  // FIXED: 原问题-中文硬编码，改用i18n
            <n-data-table :columns="funcColumns" :data="functions" size="small" :max-height="300" />
          </n-card>
          <n-card :title="t('expressionConfig.availableOperators')" size="small" style="margin-top: 12px">  // FIXED: 原问题-中文硬编码，改用i18n
            <n-space>
              <n-tag v-for="op in operators" :key="op.symbol" size="small">{{ op.symbol }} {{ op.description }}</n-tag>
            </n-space>
          </n-card>
          <n-card :title="t('expressionConfig.batchTest')" size="small" style="margin-top: 12px">  // FIXED: 原问题-中文硬编码，改用i18n
            <n-input v-model:value="batchExpr" type="textarea" :rows="3" placeholder='{"fahrenheit": "${sensor.temp} * 1.8 + 32", "status": "${sensor.temp} > 100"}' />
            <n-button size="small" style="margin-top: 8px" :loading="calculating" @click="evaluateBatch">{{ t('expressionConfig.batchCalculate') }}</n-button>  // FIXED: 原问题-中文硬编码，改用i18n
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
// FIXED: 原问题-中文硬编码，改用i18n
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
  { title: t('expressionConfig.colFunction'), key: 'name', width: 80 },  // FIXED: 原问题-中文硬编码，改用i18n
  { title: t('expressionConfig.colDescription'), key: 'description' },  // FIXED: 原问题-中文硬编码，改用i18n
  { title: t('expressionConfig.colExample'), key: 'example' },  // FIXED: 原问题-中文硬编码，改用i18n
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
    result.value = e.response?.data?.detail || t('expressionConfig.calculateFailed')  // FIXED: 原问题-中文硬编码，改用i18n
    resultValid.value = false
  } finally { evaluating.value = false }
}

async function validate() {
  try {
    const data = await expressionApi.validate(expression.value, buildVarMap())
    if (data) {
      result.value = data.valid ? t('expressionConfig.syntaxValid') : t('expressionConfig.syntaxError', { error: data.error })  // FIXED: 原问题-中文硬编码，改用i18n
      resultValid.value = data.valid
    }
  } catch (e: any) {
    result.value = e.response?.data?.detail || t('expressionConfig.validateFailed')  // FIXED: 原问题-中文硬编码，改用i18n
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
    batchResult.value = t('expressionConfig.error') + (e.response?.data?.detail || e.message)  // FIXED: 原问题-中文硬编码，改用i18n
  } finally { calculating.value = false }
}

async function loadFunctions() {
  try {
    const data = await expressionApi.functions()
    functions.value = data?.functions || []
    operators.value = data?.operators || []
  } catch (e: any) { message.error(e?.response?.data?.detail || e?.message || t('expressionConfig.loadFunctionsFailed')) }  // FIXED: 原问题-中文硬编码，改用i18n
  finally { pageLoading.value = false }
}

onMounted(() => { loadFunctions() })
</script>

<style scoped>
.expression-page { padding: 16px; }
</style>
