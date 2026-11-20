<template>
  <section class="page" data-module="dedup">
    <header class="page-head">
      <div>
        <h2>采集去重管理</h2>
        <p class="page-desc">维护去重策略，围绕策略编号、输入来源、去重键、排序字段做登记、筛选与状态流转。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记去重策略</button>
        <button class="btn" type="button" @click="exportRows">导出采集去重清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
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
          <td :colspan="columns.length + 1" class="empty-state">暂无采集去重数据，可先登记去重策略</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集去重记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <Teleport to="body">
      <div v-if="showCreateModal" class="modal-overlay" @click.self="showCreateModal = false">
        <div class="modal-card">
          <header class="modal-head">
            <h3>登记去重策略</h3>
            <button class="btn ghost" type="button" @click="showCreateModal = false">&times;</button>
          </header>
          <form class="modal-body" @submit.prevent="submitCreate">
            <label v-for="field in columns.slice(0, 6)" :key="field" class="modal-field">
              <span>{{ field }}</span>
              <input v-model="createFields[field]" :placeholder="`请输入${field}`" :required="columns.slice(0, 3).includes(field)" />
            </label>
            <footer class="modal-foot">
              <button class="btn" type="button" @click="showCreateModal = false">取消</button>
              <button class="btn primary" type="submit">提交登记</button>
            </footer>
          </form>
        </div>
      </div>
    </Teleport>
  </section>

  <section class="page" data-module="dedup-merge">
    <header class="page-head">
      <div>
        <h2>去重归并与明细追溯</h2>
        <p class="page-desc">
          按策略的去重键和有效时间范围批量归并输入事件，整组的保留记录与被归并事件逐条写入去重明细数据集；
          归并使用的窗口与排序规则以批次版本快照为准，已确认版本冻结，迟到事件归入新版本且归属可解释。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="submitLineage">同步血缘关系</button>
        <button class="btn" type="button" @click="loadStats">版本对账</button>
      </div>
    </header>

    <div class="merge-grid">
      <form class="merge-card" @submit.prevent="submitEvent">
        <h3>登记输入事件</h3>
        <label class="merge-field">
          <span>策略编号</span>
          <select v-model="mergeStrategy" required>
            <option value="" disabled>请选择策略</option>
            <option v-for="no in strategyOptions" :key="no" :value="no">{{ no }}</option>
          </select>
        </label>
        <label class="merge-field">
          <span>事件编号（留空自动生成）</span>
          <input v-model="eventForm.事件编号" placeholder="如 EVT-1001" />
        </label>
        <label class="merge-field">
          <span>去重键值</span>
          <input v-model="eventForm.去重键值" placeholder="如 device-001" required />
        </label>
        <label class="merge-field">
          <span>事件时间</span>
          <input v-model="eventForm.事件时间" placeholder="2026-11-01T10:00:00" required />
        </label>
        <label class="merge-field">
          <span>负载 JSON（含排序字段，可空）</span>
          <input v-model="eventForm.负载" placeholder='{"ts": "2026-11-01T10:00:00"}' />
        </label>
        <button class="btn primary" type="submit">登记事件</button>
      </form>

      <form class="merge-card" @submit.prevent="submitMerge">
        <h3>执行批量归并</h3>
        <label class="merge-field">
          <span>范围开始</span>
          <input v-model="mergeForm.范围开始" placeholder="2026-11-01T00:00:00" required />
        </label>
        <label class="merge-field">
          <span>范围结束</span>
          <input v-model="mergeForm.范围结束" placeholder="2026-11-02T00:00:00" required />
        </label>
        <button class="btn primary" type="submit">按有效时间范围归并</button>
        <p class="merge-hint">归并对所选策略下范围内全部待归并事件生效，取舍逐条写入明细数据集。</p>
      </form>

      <form class="merge-card" @submit.prevent="explainEvent">
        <h3>事件归属解释</h3>
        <label class="merge-field">
          <span>事件编号</span>
          <input v-model="explainNo" placeholder="如 EVT-0001" required />
        </label>
        <button class="btn" type="submit">解释归并去向</button>
        <p class="merge-hint">迟到事件会说明归到了哪一版、未改写哪个已确认版本。</p>
      </form>
    </div>

    <p v-if="mergeMessage" class="merge-message">{{ mergeMessage }}</p>

    <h3 class="block-title">归并批次</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in batchColumns" :key="column">{{ column }}</th>
          <th>动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in batches" :key="String(row.id)">
          <td v-for="column in batchColumns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button v-if="row.status !== '已确认'" class="link" type="button" @click="confirmBatch(row)">确认批次</button>
            <span v-else>已冻结</span>
          </td>
        </tr>
        <tr v-if="!batches.length">
          <td :colspan="batchColumns.length + 1" class="empty-state">暂无归并批次，可先登记事件并执行归并</td>
        </tr>
      </tbody>
    </table>

    <h3 class="block-title">输入事件</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in eventColumns" :key="column">{{ column }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in mergeEvents" :key="String(row.id)">
          <td v-for="column in eventColumns" :key="column">
            {{ column === '迟到标记' ? (row[column] ? '迟到' : '正常') : (row[column] ?? '—') }}
          </td>
        </tr>
        <tr v-if="!mergeEvents.length">
          <td :colspan="eventColumns.length" class="empty-state">暂无输入事件</td>
        </tr>
      </tbody>
    </table>

    <h3 class="block-title">去重明细数据集</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in detailColumns" :key="column">{{ column }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in details" :key="String(row.id)">
          <td v-for="column in detailColumns" :key="column">
            {{ column === '迟到标记' ? (row[column] ? '迟到' : '—') : (row[column] ?? '—') }}
          </td>
        </tr>
        <tr v-if="!details.length">
          <td :colspan="detailColumns.length" class="empty-state">暂无去重明细，归并后此处逐条可查取舍结果</td>
        </tr>
      </tbody>
    </table>

    <div v-if="explainResult" class="result-card">
      <h3>事件 {{ explainResult.事件编号 }} 的归并去向</h3>
      <p>{{ explainResult.说明 }}</p>
      <table class="data-table">
        <thead>
          <tr><th>批次编号</th><th>策略版本</th><th>角色</th><th>归并目标</th><th>窗口</th><th>批次状态</th></tr>
        </thead>
        <tbody>
          <tr v-for="(track, i) in explainResult.版本轨迹" :key="i">
            <td>{{ track.批次编号 }}</td>
            <td>v{{ track.策略版本 }}</td>
            <td>{{ track.角色 }}</td>
            <td>{{ track.归并目标 }}</td>
            <td>{{ track.窗口开始 }} ~ {{ track.窗口结束 }}</td>
            <td>{{ track.批次状态 }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="statsResult" class="result-card">
      <h3>版本对账：{{ statsResult.策略编号 }}（当前版本 v{{ statsResult.当前策略版本 }}）</h3>
      <table class="data-table">
        <thead>
          <tr><th>策略版本</th><th>是否已确认</th><th>保留记录（去重键 → 事件）</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in statsResult.版本清单" :key="item.策略版本">
            <td>v{{ item.策略版本 }}</td>
            <td>{{ item.已确认 ? '已确认（报告引用口径）' : '未确认' }}</td>
            <td>{{ formatKeeps(item.保留记录) }}</td>
          </tr>
        </tbody>
      </table>
      <div v-if="statsResult.口径差异 && statsResult.口径差异.length">
        <h4>口径差异（原报告引用 vs 本次统计）</h4>
        <table class="data-table">
          <thead>
            <tr><th>去重键值</th><th>已确认版本</th><th>已确认保留记录</th><th>当前版本</th><th>当前保留记录</th><th>原因</th></tr>
          </thead>
          <tbody>
            <tr v-for="diff in statsResult.口径差异" :key="diff.去重键值">
              <td>{{ diff.去重键值 }}</td>
              <td>v{{ diff.已确认版本 }}</td>
              <td>{{ diff.已确认保留记录 }}</td>
              <td>v{{ diff.当前版本 }}</td>
              <td>{{ diff.当前保留记录 }}</td>
              <td>{{ diff.原因 }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else class="merge-hint">各版本口径一致，没有迟到事件引起的保留记录变化。</p>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/dedup'
const columns = ["策略编号", "输入来源", "去重键", "排序字段", "保留策略", "时间窗口", "重复阈值", "策略状态"]
const actions = ["提交配置", "启用策略", "停用策略"]
const statuses = ["草稿", "已配置", "生效中", "已停用"]
const stats = [{"label": "策略总数", "value": 0}, {"label": "生效中策略", "value": 0}, {"label": "待配置策略", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreateModal = ref(false)
const createFields = ref<Record<string, string>>({})

function updateStats() {
  const data = rows.value
  stats.length > 0 && (stats[0].value = data.filter((r: Row) => r.status === statuses[1] || r.pending).length)
  stats.length > 1 && (stats[1].value = data.filter((r: Row) => r.status && !r.pending && !r.abnormal).length)
  stats.length > 2 && (stats[2].value = data.filter((r: Row) => r.abnormal).length)
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = ''
  createFields.value = {}
  showCreateModal.value = true
}

async function submitCreate() {
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST',
      body: JSON.stringify({ values: createFields.value }),
    })
    if (!response.ok) {
      const data = await response.json()
      throw new Error(data.message || '登记失败')
    }
    showCreateModal.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    if (!response.ok) {
      throw new Error('采集去重动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集去重操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('去重策略列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    updateStats()
    if (!mergeStrategy.value && rows.value.length) {
      mergeStrategy.value = String(rows.value[0]['策略编号'] ?? '')
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集去重列表读取失败'
  }
}

// —— 去重归并与明细追溯 ——
const MERGE_ENDPOINT = '/api/dedup/merge'
const batchColumns = ['批次编号', '策略版本', '范围开始', '范围结束', '时间窗口', '排序字段', '保留策略', '事件总数', '保留条数', '归并条数', '迟到条数', 'status', '确认时间']
const eventColumns = ['事件编号', '去重键值', '事件时间', '到达时间', '归并状态', '归并批次', '迟到标记']
const detailColumns = ['明细编号', '批次编号', '策略版本', '去重键值', '事件编号', '角色', '归并目标', '窗口开始', '窗口结束', '排序字段', '迟到标记']

const mergeStrategy = ref('')
const eventForm = ref({ 事件编号: '', 去重键值: '', 事件时间: '', 负载: '' })
const mergeForm = ref({ 范围开始: '', 范围结束: '' })
const explainNo = ref('')
const explainResult = ref<Record<string, any> | null>(null)
const statsResult = ref<Record<string, any> | null>(null)
const mergeEvents = ref<Row[]>([])
const batches = ref<Row[]>([])
const details = ref<Row[]>([])
const mergeMessage = ref('')

const strategyOptions = computed(() =>
  rows.value.map((row: Row) => String(row['策略编号'] ?? '')).filter(Boolean),
)

watch(mergeStrategy, () => {
  void loadMergeData()
})

function formatKeeps(keeps: Record<string, string>) {
  return Object.entries(keeps ?? {}).map(([key, val]) => `${key} → ${val}`).join('；') || '—'
}

async function mergePost(path: string, values: Record<string, unknown>) {
  const response = await request(`${MERGE_ENDPOINT}${path}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  const data = await response.json()
  mergeMessage.value = data.message || ''
  return data
}

async function loadMergeData() {
  const query = mergeStrategy.value ? `?strategy_no=${encodeURIComponent(mergeStrategy.value)}&size=100` : '?size=100'
  try {
    const [eventsRes, batchesRes, detailsRes] = await Promise.all([
      request(`${MERGE_ENDPOINT}/events${query}`),
      request(`${MERGE_ENDPOINT}/batches${query}`),
      request(`${MERGE_ENDPOINT}/details${query}`),
    ])
    mergeEvents.value = (await eventsRes.json()).items ?? []
    batches.value = (await batchesRes.json()).items ?? []
    details.value = (await detailsRes.json()).items ?? []
  } catch {
    mergeMessage.value = '归并数据读取失败'
  }
}

async function submitEvent() {
  mergeMessage.value = ''
  let payload: Record<string, unknown> = {}
  if (eventForm.value.负载.trim()) {
    try {
      payload = JSON.parse(eventForm.value.负载)
    } catch {
      mergeMessage.value = '负载 JSON 无法解析，请检查格式'
      return
    }
  }
  const data = await mergePost('/events', {
    策略编号: mergeStrategy.value,
    事件列表: [{
      事件编号: eventForm.value.事件编号 || undefined,
      去重键值: eventForm.value.去重键值,
      事件时间: eventForm.value.事件时间,
      负载: payload,
    }],
  })
  if (data.ok) {
    eventForm.value = { 事件编号: '', 去重键值: '', 事件时间: '', 负载: '' }
    await loadMergeData()
  }
}

async function submitMerge() {
  mergeMessage.value = ''
  const data = await mergePost('/run', {
    策略编号: mergeStrategy.value,
    范围开始: mergeForm.value.范围开始,
    范围结束: mergeForm.value.范围结束,
  })
  if (data.ok) {
    await loadMergeData()
  }
}

async function confirmBatch(row: Row) {
  mergeMessage.value = ''
  const response = await request(`${MERGE_ENDPOINT}/batches/${row.id}/confirm`, { method: 'POST' })
  const data = await response.json()
  mergeMessage.value = data.message || ''
  await loadMergeData()
}

async function submitLineage() {
  mergeMessage.value = ''
  await mergePost('/lineage', { 策略编号: mergeStrategy.value })
}

async function explainEvent() {
  mergeMessage.value = ''
  explainResult.value = null
  try {
    const response = await request(`${MERGE_ENDPOINT}/explain/${encodeURIComponent(explainNo.value)}`)
    if (!response.ok) {
      const data = await response.json()
      throw new Error(data.detail || '事件不存在')
    }
    explainResult.value = await response.json()
  } catch (error) {
    mergeMessage.value = error instanceof Error ? error.message : '解释查询失败'
  }
}

async function loadStats() {
  mergeMessage.value = ''
  statsResult.value = null
  if (!mergeStrategy.value) {
    mergeMessage.value = '请先选择策略编号'
    return
  }
  try {
    const response = await request(`${MERGE_ENDPOINT}/stats?strategy_no=${encodeURIComponent(mergeStrategy.value)}`)
    if (!response.ok) {
      throw new Error('版本对账读取失败')
    }
    statsResult.value = await response.json()
  } catch (error) {
    mergeMessage.value = error instanceof Error ? error.message : '版本对账读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.merge-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 12px;
  margin-bottom: 12px;
}
.merge-card {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.merge-card h3 {
  margin: 0;
  font-size: 14px;
}
.merge-field span {
  display: block;
  font-size: 12px;
  color: var(--muted);
  margin-bottom: 2px;
}
.merge-field input,
.merge-field select {
  width: 100%;
  padding: 6px 8px;
  border: 1px solid var(--border);
  border-radius: 6px;
  font-size: 13px;
}
.merge-hint {
  margin: 0;
  font-size: 12px;
  color: var(--muted);
}
.merge-message {
  font-size: 13px;
  color: var(--brand);
}
.block-title {
  font-size: 14px;
  margin: 16px 0 8px;
}
.result-card {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 14px;
  margin-top: 16px;
}
.result-card h3 {
  margin: 0 0 8px;
  font-size: 14px;
}
.result-card h4 {
  margin: 12px 0 8px;
  font-size: 13px;
}
</style>
