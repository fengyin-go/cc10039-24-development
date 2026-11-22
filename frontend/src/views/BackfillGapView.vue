<template>
  <section class="page" data-module="backfill_gap">
    <header class="page-head">
      <div>
        <h2>字段补数缺口</h2>
        <p class="page-desc">
          按湖表、字段版本和日期范围检索待补缺口；每条缺口可追溯元数据版本与回填任务，
          同列多版变更的重叠日期可核对写入字段数据与待复核值。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create-change" type="button" @click="showCreateChange = true">
          登记字段变更
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">查询缺口总数</span>
        <strong class="stat-value">{{ stats.total }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">待补缺口</span>
        <strong class="stat-value">{{ stats.pending }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">待复核值</span>
        <strong class="stat-value">{{ stats.review }}</strong>
      </article>
    </div>

    <nav class="tab-bar">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        type="button"
        class="tab-item"
        :class="{ active: activeTab === tab.key }"
        data-testid="tab-gap"
        @click="activeTab = tab.key"
      >
        {{ tab.label }}
      </button>
    </nav>

    <div v-if="activeTab === 'gap'" class="tab-panel">
      <form class="filter-bar" @submit.prevent="reloadGaps">
        <label class="filter-item">
          <span>湖表名</span>
          <input v-model="query.湖表名" data-testid="filter-table" placeholder="按湖表筛选" />
        </label>
        <label class="filter-item">
          <span>字段名</span>
          <input v-model="query.字段名" data-testid="filter-column" placeholder="按列筛选" />
        </label>
        <label class="filter-item">
          <span>元数据版本ID</span>
          <input v-model.number="query.元数据版本ID" data-testid="filter-version-id" placeholder="如 4" />
        </label>
        <label class="filter-item">
          <span>字段版本</span>
          <input v-model="query.字段版本" data-testid="filter-version" placeholder="如 v1" />
        </label>
        <label class="filter-item">
          <span>起始日期</span>
          <input v-model="query.起始日期" type="date" data-testid="filter-start" />
        </label>
        <label class="filter-item">
          <span>结束日期</span>
          <input v-model="query.结束日期" type="date" data-testid="filter-end" />
        </label>
        <label class="filter-item">
          <span>缺口状态</span>
          <select v-model="query.缺口状态" data-testid="filter-status">
            <option value="">全部</option>
            <option value="待补">待补</option>
            <option value="已补齐">已补齐</option>
            <option value="待复核">待复核</option>
          </select>
        </label>
        <button class="btn" data-testid="query-gaps" type="submit">查询缺口</button>
        <button class="btn ghost" type="button" @click="resetGapQuery">重置条件</button>
      </form>

      <form class="filter-bar" @submit.prevent="loadPlan">
        <label class="filter-item">
          <span>核算用元数据版本ID</span>
          <input v-model.number="planVersionId" data-testid="plan-version-id" placeholder="版本ID" required />
        </label>
        <label class="filter-item">
          <span>核算起始日期</span>
          <input v-model="planStart" type="date" data-testid="plan-start" />
        </label>
        <label class="filter-item">
          <span>核算结束日期</span>
          <input v-model="planEnd" type="date" data-testid="plan-end" />
        </label>
        <button class="btn primary" data-testid="plan-gap" type="submit">核算回填输入</button>
      </form>

      <div v-if="plan" class="analysis-card" data-testid="plan-result">
        <strong>{{ plan.字段版本 }}（版本 #{{ plan.元数据版本ID }}）回填核算结果</strong>
        <p class="page-desc">
          待补 {{ plan.待补缺口数 }} 天 / 共 {{ plan.缺口总数 }} 天，已补齐 {{ plan.已补齐缺口数 }} 天；
          允许写入列值：<b>{{ plan.允许列值.join('、') || '—' }}</b>
        </p>
        <p class="page-desc">
          本次可继续执行范围：
          <b v-if="plan.可执行范围.length">
            {{ plan.可执行范围.map((r) => `${r.start} ~ ${r.end}`).join('，') }}
          </b>
          <b v-else>无待补范围</b>
        </p>
        <form class="filter-bar" @submit.prevent="startFromPlan">
          <label class="filter-item">
            <span>回填任务ID（待调度）</span>
            <input v-model.number="startJobId" data-testid="start-job-id" placeholder="任务ID" required />
          </label>
          <label class="filter-item">
            <span>启动起始日期</span>
            <input v-model="startRangeStart" type="date" data-testid="start-range-start" required />
          </label>
          <label class="filter-item">
            <span>启动结束日期</span>
            <input v-model="startRangeEnd" type="date" data-testid="start-range-end" required />
          </label>
          <button class="btn primary" data-testid="start-gap-backfill" type="submit">按选中范围启动回填</button>
        </form>
      </div>

      <table class="data-table" data-testid="gap-table">
        <thead>
          <tr>
            <th>湖表名</th>
            <th>字段名</th>
            <th>字段版本</th>
            <th>元数据版本</th>
            <th>业务日期</th>
            <th>允许列值</th>
            <th>缺口状态</th>
            <th>已写入列值</th>
            <th>回填任务</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="gap in gaps" :key="String(gap.id)" :data-testid="`gap-row-${gap.id}`" :data-status="gap.缺口状态">
            <td>{{ gap.湖表名 }}</td>
            <td>{{ gap.字段名 }}</td>
            <td>{{ gap.字段版本 }}</td>
            <td>
              <a class="link" :data-testid="`gap-version-link-${gap.id}`" @click="openVersion(gap)">
                #{{ gap.元数据版本ID }}
              </a>
            </td>
            <td>{{ gap.业务日期 }}</td>
            <td>{{ gap.允许列值.join('、') }}</td>
            <td :class="gap.缺口状态 === '待复核' ? 'error-text' : ''">{{ gap.缺口状态 }}</td>
            <td>{{ gap.已写入列值 ?? '—' }}</td>
            <td>
              <a v-if="gap.回填任务ID" class="link" :data-testid="`gap-job-link-${gap.id}`" @click="openJob(gap)">
                #{{ gap.回填任务ID }}
              </a>
              <span v-else>—</span>
            </td>
            <td class="row-actions">
              <button
                v-if="gap.缺口状态 === '待补' || gap.缺口状态 === '待复核'"
                class="link"
                type="button"
                :data-testid="`fill-gap-${gap.id}`"
                @click="openFill(gap)"
              >
                写入补数
              </button>
              <span v-else class="page-desc">已完成</span>
            </td>
          </tr>
          <tr v-if="!gaps.length">
            <td colspan="10" class="empty-state">没有匹配的字段补数缺口</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-else-if="activeTab === 'overlap'" class="tab-panel">
      <form class="filter-bar" @submit.prevent="reloadOverlap">
        <label class="filter-item">
          <span>湖表名</span>
          <input v-model="overlapQuery.湖表名" data-testid="overlap-table" placeholder="按湖表筛选" required />
        </label>
        <label class="filter-item">
          <span>字段名</span>
          <input v-model="overlapQuery.字段名" data-testid="overlap-column" placeholder="同列多版" required />
        </label>
        <label class="filter-item">
          <span>起始日期</span>
          <input v-model="overlapQuery.起始日期" type="date" data-testid="overlap-start" />
        </label>
        <label class="filter-item">
          <span>结束日期</span>
          <input v-model="overlapQuery.结束日期" type="date" data-testid="overlap-end" />
        </label>
        <button class="btn primary" data-testid="query-overlap" type="submit">检查重叠日期</button>
      </form>

      <div v-if="overlapReport" class="analysis-card">
        <strong>
          {{ overlapReport.湖表名 }}.{{ overlapReport.字段名 }}：重叠 {{ overlapReport.重叠日期数 }} 天，
          待复核值 {{ overlapReport.待复核值总数 }} 个，补数记录 {{ overlapReport.补数记录数 }} 条
        </strong>
      </div>

      <table class="data-table" data-testid="overlap-table">
        <thead>
          <tr>
            <th>业务日期</th>
            <th>字段版本</th>
            <th>允许列值</th>
            <th>缺口状态</th>
            <th>已写入字段数据</th>
            <th>回填任务</th>
            <th>复核说明</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="day in overlapDays" :key="day.业务日期">
            <tr
              v-for="vg in day.版本缺口"
              :key="String(vg.缺口ID)"
              :data-testid="`overlap-row-${day.业务日期}-${vg.字段版本}`"
            >
              <td>{{ day.业务日期 }}</td>
              <td>{{ vg.字段版本 }}</td>
              <td>{{ vg.允许列值.join('、') }}</td>
              <td :class="vg.缺口状态 === '待复核' ? 'error-text' : ''">{{ vg.缺口状态 }}</td>
              <td>{{ vg.已写入列值 ?? '—' }}</td>
              <td>{{ vg.回填任务ID ? `#${vg.回填任务ID}` : '—' }}</td>
              <td>{{ vg.复核说明 ?? '—' }}</td>
            </tr>
          </template>
          <tr v-if="!overlapDays.length">
            <td colspan="7" class="empty-state">没有同列多版的重叠日期</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-else-if="activeTab === 'fills'" class="tab-panel">
      <form class="filter-bar" @submit.prevent="reloadFills">
        <label class="filter-item">
          <span>湖表名</span>
          <input v-model="fillQuery.湖表名" data-testid="fill-table" placeholder="按湖表筛选" />
        </label>
        <label class="filter-item">
          <span>字段名</span>
          <input v-model="fillQuery.字段名" data-testid="fill-column" placeholder="按列筛选" />
        </label>
        <label class="filter-item">
          <span>元数据版本ID</span>
          <input v-model.number="fillQuery.元数据版本ID" data-testid="fill-version-id" placeholder="版本ID" />
        </label>
        <button class="btn" data-testid="query-fills" type="submit">查询补数记录</button>
      </form>
      <table class="data-table" data-testid="fill-table-view">
        <thead>
          <tr>
            <th>记录ID</th>
            <th>湖表名</th>
            <th>字段名</th>
            <th>字段版本</th>
            <th>业务日期</th>
            <th>回填任务</th>
            <th>写入列值</th>
            <th>是否采用</th>
            <th>记录说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="fill in fills" :key="String(fill.id)" :data-testid="`fill-row-${fill.id}`">
            <td>#{{ fill.id }}</td>
            <td>{{ fill.湖表名 }}</td>
            <td>{{ fill.字段名 }}</td>
            <td>{{ fill.字段版本 }}</td>
            <td>{{ fill.业务日期 }}</td>
            <td>#{{ fill.回填任务ID }}</td>
            <td>{{ fill.写入列值 }}</td>
            <td :class="fill.是否采用 ? '' : 'error-text'">{{ fill.是否采用 ? '已采用' : '待复核' }}</td>
            <td>{{ fill.记录说明 ?? '—' }}</td>
          </tr>
          <tr v-if="!fills.length">
            <td colspan="9" class="empty-state">暂无补数记录</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="fillTarget" class="modal-backdrop" data-testid="fill-modal">
      <form class="modal" @submit.prevent="submitFill">
        <h3>写入补数：{{ fillTarget.字段版本 }} / {{ fillTarget.业务日期 }}</h3>
        <p class="page-desc">
          {{ fillTarget.湖表名 }}.{{ fillTarget.字段名 }}，允许列值：{{ fillTarget.允许列值.join('、') }}
        </p>
        <label class="create-field">
          <span>回填任务ID（需为回填中）</span>
          <input v-model.number="fillForm.回填任务ID" data-testid="fill-job-id" required />
        </label>
        <label class="create-field">
          <span>写入列值</span>
          <input v-model="fillForm.写入列值" data-testid="fill-column-value" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="fillTarget = null">取消</button>
          <button class="btn primary" data-testid="submit-fill" type="submit">确认写入</button>
        </div>
      </form>
    </div>

    <div v-if="showCreateChange" class="modal-backdrop" data-testid="create-change-modal">
      <form class="modal" @submit.prevent="submitCreateChange">
        <h3>登记不兼容字段变更（生成字段版本缺口）</h3>
        <label class="create-field">
          <span>湖表名</span>
          <input v-model="changeForm.湖表名" data-testid="change-table" required />
        </label>
        <label class="create-field">
          <span>变更字段</span>
          <input v-model="changeForm.变更字段" data-testid="change-column" required />
        </label>
        <label class="create-field">
          <span>变更类型</span>
          <select v-model="changeForm.变更类型" data-testid="change-type">
            <option>修改字段类型</option>
            <option>重命名字段</option>
            <option>删除字段</option>
          </select>
        </label>
        <label class="create-field">
          <span>允许列值（逗号分隔，新版本可收窄）</span>
          <input v-model="allowedValuesText" data-testid="change-allowed" placeholder="普通,银卡,金卡" required />
        </label>
        <label class="create-field">
          <span>补数起始日期</span>
          <input v-model="changeForm.回填起始日期" type="date" data-testid="change-start" required />
        </label>
        <label class="create-field">
          <span>补数结束日期</span>
          <input v-model="changeForm.回填结束日期" type="date" data-testid="change-end" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreateChange = false">取消</button>
          <button class="btn primary" data-testid="submit-change" type="submit">登记草稿</button>
        </div>
        <p class="page-desc">草稿登记后需到「Schema演进」执行提交审批与执行变更，才会生成字段版本缺口。</p>
      </form>
    </div>

    <footer class="page-foot">
      <span>{{ message || '缺口查询结果仅展示当前筛选条件下的数据' }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import {
  createFieldChange,
  fillBackfillGap,
  listBackfillFills,
  planGapBackfill,
  queryBackfillGaps,
  reviewOverlapGaps,
  startGapBackfill,
  type FillRow,
  type GapPlan,
  type GapRow,
  type OverlapDay,
} from '@/api/backfill-gap'

const router = useRouter()
const tabs = [
  { key: 'gap', label: '缺口查询与补数' },
  { key: 'overlap', label: '重叠日期复核' },
  { key: 'fills', label: '补数记录' },
] as const

const activeTab = ref<(typeof tabs)[number]['key']>('gap')
const message = ref('')
const errorMessage = ref('')

const query = reactive({
  湖表名: '',
  字段名: '',
  元数据版本ID: undefined as number | undefined,
  字段版本: '',
  起始日期: '',
  结束日期: '',
  缺口状态: '',
})
const gaps = ref<GapRow[]>([])
const stats = reactive({ total: 0, pending: 0, review: 0 })

const planVersionId = ref<number | undefined>(undefined)
const planStart = ref('')
const planEnd = ref('')
const plan = ref<GapPlan | null>(null)
const startJobId = ref<number | undefined>(undefined)
const startRangeStart = ref('')
const startRangeEnd = ref('')

const overlapQuery = reactive({ 湖表名: '', 字段名: '', 起始日期: '', 结束日期: '' })
const overlapDays = ref<OverlapDay[]>([])
const overlapReport = ref<{ 重叠日期数: number; 待复核值总数: number; 补数记录数: number; 湖表名: string; 字段名: string } | null>(null)

const fillQuery = reactive({ 湖表名: '', 字段名: '', 元数据版本ID: undefined as number | undefined })
const fills = ref<FillRow[]>([])

const fillTarget = ref<GapRow | null>(null)
const fillForm = reactive({ 回填任务ID: undefined as number | undefined, 写入列值: '' })

const showCreateChange = ref(false)
const changeForm = reactive({
  湖表名: '',
  变更字段: '',
  变更类型: '修改字段类型',
  回填起始日期: '',
  回填结束日期: '',
})
const allowedValuesText = ref('')

const cleanedQuery = computed(() =>
  Object.fromEntries(
    Object.entries(query).filter(([, value]) => value !== '' && value !== undefined && value !== null),
  ),
)

async function reloadGaps() {
  errorMessage.value = ''
  try {
    const result = await queryBackfillGaps(cleanedQuery.value)
    gaps.value = result.items
    stats.total = result.total
    stats.pending = result.pending_total
    stats.review = result.review_total
    message.value = result.message
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '缺口查询失败'
  }
}

function resetGapQuery() {
  Object.assign(query, {
    湖表名: '',
    字段名: '',
    元数据版本ID: undefined,
    字段版本: '',
    起始日期: '',
    结束日期: '',
    缺口状态: '',
  })
  void reloadGaps()
}

async function loadPlan() {
  if (!planVersionId.value) {
    errorMessage.value = '请输入核算用元数据版本ID'
    return
  }
  errorMessage.value = ''
  try {
    const result = await planGapBackfill({
      湖表名: query.湖表名,
      元数据版本ID: planVersionId.value,
      起始日期: planStart.value || undefined,
      结束日期: planEnd.value || undefined,
    })
    if (!result.ok) throw new Error(result.message)
    plan.value = result.plan
    message.value = result.message
    if (result.plan.可执行范围.length) {
      startRangeStart.value = result.plan.可执行范围[0].start
      startRangeEnd.value = result.plan.可执行范围[result.plan.可执行范围.length - 1].end
    }
  } catch (error) {
    plan.value = null
    errorMessage.value = error instanceof Error ? error.message : '回填核算失败'
  }
}

async function startFromPlan() {
  if (!plan.value || !startJobId.value) return
  errorMessage.value = ''
  try {
    const result = await startGapBackfill({
      回填任务ID: startJobId.value,
      元数据版本ID: plan.value.元数据版本ID,
      起始日期: startRangeStart.value,
      结束日期: startRangeEnd.value,
    })
    message.value = result.message
    if (!result.ok) throw new Error(result.message)
    await reloadGaps()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '启动回填失败'
  }
}

function openFill(gap: GapRow) {
  fillTarget.value = gap
  fillForm.回填任务ID = gap.回填任务ID ?? undefined
  fillForm.写入列值 = gap.已写入列值 ?? ''
}

async function submitFill() {
  if (!fillTarget.value || !fillForm.回填任务ID) return
  errorMessage.value = ''
  try {
    const result = await fillBackfillGap({
      回填任务ID: fillForm.回填任务ID,
      元数据版本ID: fillTarget.value.元数据版本ID,
      业务日期: fillTarget.value.业务日期,
      写入列值: fillForm.写入列值,
    })
    message.value = result.message
    if (!result.ok) throw new Error(result.message)
    fillTarget.value = null
    await reloadGaps()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '补数写入失败'
  }
}

async function reloadOverlap() {
  if (!overlapQuery.湖表名 || !overlapQuery.字段名) {
    errorMessage.value = '重叠复核需要湖表名和字段名'
    return
  }
  errorMessage.value = ''
  try {
    const result = await reviewOverlapGaps({
      湖表名: overlapQuery.湖表名,
      字段名: overlapQuery.字段名,
      起始日期: overlapQuery.起始日期 || undefined,
      结束日期: overlapQuery.结束日期 || undefined,
    })
    overlapDays.value = result.report.重叠日期明细
    overlapReport.value = result.report
    message.value = result.message
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重叠复核失败'
  }
}

async function reloadFills() {
  errorMessage.value = ''
  try {
    const params = Object.fromEntries(
      Object.entries(fillQuery).filter(([, value]) => value !== '' && value !== undefined),
    )
    const result = await listBackfillFills(params)
    fills.value = result.items
    message.value = result.message
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '补数记录查询失败'
  }
}

async function submitCreateChange() {
  errorMessage.value = ''
  const allowed = allowedValuesText.value
    .split(/[,，]/)
    .map((item) => item.trim())
    .filter(Boolean)
  if (!allowed.length) {
    errorMessage.value = '请至少填写一个允许列值'
    return
  }
  try {
    const result = await createFieldChange({
      湖表名: changeForm.湖表名,
      变更字段: changeForm.变更字段,
      变更类型: changeForm.变更类型,
      允许列值: allowed,
      回填起始日期: changeForm.回填起始日期,
      回填结束日期: changeForm.回填结束日期,
    })
    if (!result.ok) throw new Error(result.message)
    message.value = result.message
    showCreateChange.value = false
    await router.push('/schema_evolution')
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '字段变更登记失败'
  }
}

function openVersion(gap: GapRow) {
  void router.push({ path: '/schema_evolution', query: { highlight: String(gap.元数据版本ID) } })
}

function openJob(gap: GapRow) {
  void router.push({ path: '/backfill_job', query: { highlight: String(gap.回填任务ID ?? '') } })
}

onMounted(() => {
  void reloadGaps()
})
</script>

<style scoped>
.tab-bar {
  display: flex;
  gap: 8px;
  margin: 8px 0 12px;
}
.tab-item {
  border: 1px solid var(--border);
  background: #fff;
  border-radius: 6px;
  padding: 6px 14px;
  cursor: pointer;
  font-size: 13px;
}
.tab-item.active {
  background: var(--brand);
  border-color: var(--brand);
  color: #fff;
}
.tab-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
</style>
