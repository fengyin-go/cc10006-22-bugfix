<template>
  <section class="page" data-module="search">
    <header class="page-head">
      <div>
        <h2>数据检索管理</h2>
        <p class="page-desc">维护数据检索索引的输入、执行约束与结果记录，为数据检索提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记检索索引</button>
        <button class="btn" type="button" @click="exportRows">导出数据检索清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="search">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <label class="filter-item">
        <span>索引状态</span>
        <select v-model="statusFilter">
          <option value="">全部状态</option>
          <option v-for="status in statuses" :key="status" :value="status">{{ status }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">{{ emptyMessage }}</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条数据检索记录</span>
      <span class="pager">
        <button class="btn ghost" type="button" :disabled="page <= 1" @click="goToPage(page - 1)">上一页</button>
        <span>第 {{ page }} 页 / 每页 {{ size }} 条</span>
        <button class="btn ghost" type="button" :disabled="page * size >= total" @click="goToPage(page + 1)">下一页</button>
      </span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/search'
const columns = ["索引编号", "索引名称", "覆盖表名", "索引字段", "分词策略", "排序权重", "刷新频率", "索引大小"]
const actions = ["提交构建", "启用索引", "标记失效"]
const statuses = ["草稿", "待构建", "已生效", "已失效"]
const stats = [{"label": "检索索引总数", "value": 0}, {"label": "已生效索引", "value": 0}, {"label": "待构建索引", "value": 0}]
// 编号、名称、覆盖表名、索引字段都要参与过滤；空白值表示不过滤。
const filterFields = columns.slice(0, 4)

const rows = ref<Row[]>([])
const total = ref(0)
const page = ref(1)
const size = ref(20)
const errorMessage = ref('')
const responseMessage = ref('')
const filters = ref<Record<string, string>>({})
const statusFilter = ref('')

const emptyMessage = computed(
  () => responseMessage.value || '暂无数据检索数据，可先登记检索索引',
)

function currentQuery(includePaging: boolean): string {
  const params = new URLSearchParams()
  for (const field of filterFields) {
    const value = (filters.value[field] ?? '').trim()
    if (value) {
      params.set(field, value)
    }
  }
  const status = statusFilter.value.trim()
  if (status) {
    params.set('status', status)
  }
  if (includePaging) {
    params.set('page', String(page.value))
    params.set('size', String(size.value))
  }
  return params.toString()
}

function resetFilters() {
  filters.value = {}
  statusFilter.value = ''
  page.value = 1
  void reload()
}

function search() {
  page.value = 1
  void reload()
}

function goToPage(target: number) {
  if (target < 1) {
    return
  }
  page.value = target
  void reload()
}

function exportRows() {
  // 导出沿用当前筛选条件与索引状态，不携带分页参数，导出全量匹配结果。
  const query = currentQuery(false)
  window.open(query ? `${ENDPOINT}/export?${query}` : `${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '检索索引登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('数据检索动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据检索操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  responseMessage.value = ''
  const query = currentQuery(true)
  try {
    const response = await request(query ? `${ENDPOINT}?${query}` : ENDPOINT)
    if (!response.ok) {
      throw new Error('检索索引列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    responseMessage.value = payload.message ?? ''
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据检索列表读取失败'
  }
}

onMounted(reload)
</script>
