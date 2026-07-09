<template>
  <n-spin :show="pageLoading" :description="t('expressionConfig.loading')">
  <div class="expression-page">
    <n-card :title="t('expressionConfig.title')">
      <n-grid :cols="2" :x-gap="16">
        <n-gi>
          <n-card :title="t('expressionConfig.editor')" size="small">
            <n-form ref="exprFormRef" :model="exprForm" :rules="exprFormRules" label-placement="left" label-width="80">
              <n-form-item :label="t('expressionConfig.expression')" path="expression">
                <n-input v-model:value="expression" type="textarea" :rows="3" maxlength="2000" :placeholder="t('expressionConfig.expressionPlaceholder')" />
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
            <n-data-table :columns="funcColumns" :data="functions" :loading="pageLoading" size="small" :max-height="300" />
          </n-card>
          <n-card :title="t('expressionConfig.availableOperators')" size="small" style="margin-top: 12px">
            <n-space>
              <n-tag v-for="op in operators" :key="op.symbol" size="small">{{ op.symbol }} {{ op.description }}</n-tag>
            </n-space>
          </n-card>
          <n-card :title="t('expressionConfig.batchTest')" size="small" style="margin-top: 12px">
            <n-input v-model:value="batchExpr" type="textarea" :rows="3" maxlength="2000" placeholder='{"fahrenheit": "${sensor.temp} * 1.8 + 32", "status": "${sensor.temp} > 100"}' />
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
import { ref, computed, onMounted } from 'vue'
import { NCard, NButton, NInput, NForm, NFormItem, NSpace, NTag, NDataTable, NDynamicInput, NGrid, NGi, NCode, NSpin } from 'naive-ui'
import { expressionApi } from '@/api'
import { t } from '@/i18n'
import { extractError } from '@/utils/errorCodes'
import { message } from '@/utils/discreteApi'
import { useDirtyFormGuard } from '@/composables/useDirtyFormGuard'

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
const exprFormRef = ref<any>(null)

// [AUDIT-FIX] 严重级-表单未保存离开确认（表达式编辑器无持久化保存，保护用户输入不丢失）
useDirtyFormGuard({
  watchSource: () => ({ expr: expression.value, vars: variables.value, batch: batchExpr.value }),
})

const exprForm = computed(() => ({
  expression: expression.value,
}))

const exprFormRules = computed(() => ({
  expression: [
    { required: true, message: t('expressionConfig.expressionRequired'), trigger: ['input', 'blur'] },
    { max: 2000, message: t('expressionConfig.expressionMaxLength'), trigger: ['input', 'blur'] },
  ],
}))

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
  try { await exprFormRef.value?.validate() } catch { return }
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
      result.value = data.valid ? t('expressionConfig.syntaxValid') : t('expressionConfig.syntaxError', { error: data.error ?? '' })
      resultValid.value = data.valid
    }
  } catch (e: any) {
    result.value = e.response?.data?.detail || t('expressionConfig.validateFailed')
    resultValid.value = false
  }
}

async function evaluateBatch() {
  if (!batchExpr.value.trim()) {
    message.warning(t('expressionConfig.expressionRequired'))
    return
  }
  if (batchExpr.value.length > 2000) {
    message.warning(t('expressionConfig.expressionMaxLength'))
    return
  }
  calculating.value = true
  try {
    const exprs = JSON.parse(batchExpr.value)
    const data = await expressionApi.evaluateBatch(exprs, buildVarMap())
    batchResult.value = JSON.stringify(data?.results, null, 2)
  } catch (e: any) {
    batchResult.value = t('expressionConfig.error') + extractError(e, '')
  } finally { calculating.value = false }
}

async function loadFunctions() {
  try {
    const data = await expressionApi.functions()
    const funcDescI18n: Record<string, string> = {
      'Absolute value': 'exprFunc.abs',
      'Round to N decimal places': 'exprFunc.round',
      'Minimum value': 'exprFunc.min',
      'Maximum value': 'exprFunc.max',
      'Power operation': 'exprFunc.pow',
      'Square root': 'exprFunc.sqrt',
      'Convert to integer': 'exprFunc.int',
      'Convert to float': 'exprFunc.float',
      'Ceiling (round up)': 'exprFunc.ceil',
      'Floor (round down)': 'exprFunc.floor',
      'Natural logarithm': 'exprFunc.log',
      'Base-10 logarithm': 'exprFunc.log10',
      'Sine': 'exprFunc.sin',
      'Cosine': 'exprFunc.cos',
      'Tangent': 'exprFunc.tan',
      'Convert degrees to radians': 'exprFunc.radians',
      'Convert radians to degrees': 'exprFunc.degrees',
      'Sum of iterable': 'exprFunc.sum',
      'Count of items': 'exprFunc.count',
      'Standard deviation': 'exprFunc.stdev',
      'Mean (average)': 'exprFunc.mean',
      'Median': 'exprFunc.median',
      'e raised to power x': 'exprFunc.exp',
      'Hyperbolic sine': 'exprFunc.sinh',
      'Hyperbolic cosine': 'exprFunc.cosh',
      'Hyperbolic tangent': 'exprFunc.tanh',
    }
    const opDescI18n: Record<string, string> = {
      'Addition': 'exprOp.add',
      'Subtraction': 'exprOp.sub',
      'Multiplication': 'exprOp.mul',
      'Division': 'exprOp.div',
      'Modulo': 'exprOp.mod',
      'Exponentiation': 'exprOp.pow',
      'Floor division': 'exprOp.floordiv',
      'Equal': 'exprOp.eq',
      'Not equal': 'exprOp.ne',
      'Less than': 'exprOp.lt',
      'Less or equal': 'exprOp.le',
      'Greater than': 'exprOp.gt',
      'Greater or equal': 'exprOp.ge',
      'Logical AND': 'exprOp.and',
      'Logical OR': 'exprOp.or',
      'Logical NOT': 'exprOp.not',
      'Bitwise AND': 'exprOp.bitand',
      'Bitwise OR': 'exprOp.bitor',
      'Bitwise XOR': 'exprOp.bitxor',
      'Left shift': 'exprOp.lshift',
      'Right shift': 'exprOp.rshift',
    }
    functions.value = (data?.functions || []).map((f: any) => ({
      ...f,
      description: funcDescI18n[f.description] ? t(funcDescI18n[f.description]) : f.description,
    }))
    operators.value = (data?.operators || []).map((o: any) => ({
      ...o,
      description: opDescI18n[o.description] ? t(opDescI18n[o.description]) : o.description,
    }))
  } catch (e: any) { message.error(extractError(e, t('expressionConfig.loadFunctionsFailed'))) }
  finally { pageLoading.value = false }
}

onMounted(() => { loadFunctions() })
</script>

<style scoped>
.expression-page { padding: 16px; }
</style>
