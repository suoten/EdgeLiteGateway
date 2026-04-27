<template>
  <div class="expression-page">
    <n-card title="计算表达式">
      <n-grid :cols="2" :x-gap="16">
        <n-gi>
          <n-card title="表达式编辑器" size="small">
            <n-form label-placement="left" label-width="80">
              <n-form-item label="表达式">
                <n-input v-model:value="expression" type="textarea" :rows="3" placeholder="例: ${sensor1.temp} * 1.8 + 32" />
              </n-form-item>
              <n-form-item label="变量">
                <n-dynamic-input v-model:value="variables" :on-create="() => ({ key: '', value: '0' })">
                  <template #default="{ value: item }">
                    <n-space align="center">
                      <n-input v-model:value="item.key" placeholder="变量名" size="small" style="width: 150px" />
                      <span>=</span>
                      <n-input v-model:value="item.value" placeholder="值" size="small" style="width: 100px" />
                    </n-space>
                  </template>
                </n-dynamic-input>
              </n-form-item>
            </n-form>
            <n-space style="margin-top: 12px">
              <n-button type="primary" :loading="evaluating" @click="evaluate">计算</n-button>
              <n-button @click="validate">验证语法</n-button>
            </n-space>
            <n-card v-if="result !== null" title="结果" size="small" style="margin-top: 12px">
              <n-tag :type="resultValid ? 'success' : 'error'" size="large">{{ result }}</n-tag>
            </n-card>
          </n-card>
        </n-gi>
        <n-gi>
          <n-card title="可用函数" size="small">
            <n-data-table :columns="funcColumns" :data="functions" size="small" :max-height="300" />
          </n-card>
          <n-card title="可用运算符" size="small" style="margin-top: 12px">
            <n-space>
              <n-tag v-for="op in operators" :key="op.symbol" size="small">{{ op.symbol }} {{ op.description }}</n-tag>
            </n-space>
          </n-card>
          <n-card title="批量测试" size="small" style="margin-top: 12px">
            <n-input v-model:value="batchExpr" type="textarea" :rows="3" placeholder='{"fahrenheit": "${sensor.temp} * 1.8 + 32", "status": "${sensor.temp} > 100"}' />
            <n-button size="small" style="margin-top: 8px" @click="evaluateBatch">批量计算</n-button>
            <n-code v-if="batchResult" :code="batchResult" language="json" style="margin-top: 8px" />
          </n-card>
        </n-gi>
      </n-grid>
    </n-card>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { NCard, NButton, NInput, NForm, NFormItem, NSpace, NTag, NDataTable, NDynamicInput, NGrid, NGi, NCode, useMessage } from 'naive-ui'
import { expressionApi } from '../../api'

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

const funcColumns = [
  { title: '函数', key: 'name', width: 80 },
  { title: '说明', key: 'description' },
  { title: '示例', key: 'example' },
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
    result.value = e.response?.data?.detail || '计算失败'
    resultValid.value = false
  } finally { evaluating.value = false }
}

async function validate() {
  try {
    const data = await expressionApi.validate(expression.value, buildVarMap())
    if (data) {
      result.value = data.valid ? '语法正确 ✓' : `语法错误: ${data.error}`
      resultValid.value = data.valid
    }
  } catch (e: any) {
    result.value = e.response?.data?.detail || '验证失败'
    resultValid.value = false
  }
}

async function evaluateBatch() {
  try {
    const exprs = JSON.parse(batchExpr.value)
    const data = await expressionApi.evaluateBatch(exprs, buildVarMap())
    batchResult.value = JSON.stringify(data?.results, null, 2)
  } catch (e: any) {
    batchResult.value = '错误: ' + (e.response?.data?.detail || e.message)
  }
}

async function loadFunctions() {
  try {
    const data = await expressionApi.functions()
    functions.value = data?.functions || []
    operators.value = data?.operators || []
  } catch (e) { message.error('加载函数列表失败') }
}

onMounted(() => { loadFunctions() })
</script>

<style scoped>
.expression-page { padding: 16px; }
</style>
