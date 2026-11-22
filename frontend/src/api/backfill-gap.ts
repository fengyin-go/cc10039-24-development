import { request } from './client'

export type GapRow = {
  id: number
  湖表名: string
  字段名: string
  元数据版本ID: number
  字段版本: string
  业务日期: string
  允许列值: string[]
  缺口状态: string
  回填任务ID: number | null
  已写入列值: string | null
  复核说明: string | null
}

export type FillRow = {
  id: number
  湖表名: string
  字段名: string
  元数据版本ID: number
  字段版本: string
  业务日期: string
  回填任务ID: number
  写入列值: string
  是否采用: boolean
  记录说明: string | null
}

export type GapPlan = {
  湖表名: string
  字段名: string
  元数据版本ID: number
  字段版本: string
  查询起始: string | null
  查询结束: string | null
  允许列值: string[]
  缺口总数: number
  待补缺口数: number
  已补齐缺口数: number
  待补日期: string[]
  可执行范围: { start: string; end: string }[]
}

export type OverlapDay = {
  业务日期: string
  版本缺口: {
    缺口ID: number
    元数据版本ID: number
    字段版本: string
    允许列值: string[]
    缺口状态: string
    回填任务ID: number | null
    已写入列值: string | null
    复核说明: string | null
  }[]
  已写入字段数据: string[]
  待复核值: string[]
}

type Envelope<T> = { ok: boolean; message: string } & T

async function postJson<T>(path: string, body: Record<string, unknown>): Promise<Envelope<T>> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  if (!response.ok) throw new Error('接口请求失败')
  return (await response.json()) as Envelope<T>
}

export async function queryBackfillGaps(payload: {
  湖表名?: string
  字段名?: string
  元数据版本ID?: number
  字段版本?: string
  起始日期?: string
  结束日期?: string
  缺口状态?: string
}): Promise<Envelope<{ items: GapRow[]; total: number; pending_total: number; review_total: number }>> {
  return postJson('/api/backfill/gaps/query', payload)
}

export async function planGapBackfill(payload: {
  湖表名: string
  元数据版本ID: number
  起始日期?: string
  结束日期?: string
}): Promise<Envelope<{ plan: GapPlan }>> {
  return postJson('/api/backfill/gaps/plan', payload)
}

export async function startGapBackfill(payload: {
  回填任务ID: number
  元数据版本ID: number
  起始日期: string
  结束日期: string
}): Promise<Envelope<{ entry: Record<string, unknown> | null }>> {
  return postJson('/api/backfill/gaps/start', payload)
}

export async function fillBackfillGap(payload: {
  回填任务ID: number
  元数据版本ID: number
  业务日期: string
  写入列值: string
}): Promise<Envelope<{ gap: GapRow | null }>> {
  return postJson('/api/backfill/gaps/fill', payload)
}

export async function reviewOverlapGaps(payload: {
  湖表名: string
  字段名: string
  起始日期?: string
  结束日期?: string
}): Promise<
  Envelope<{
    report: {
      湖表名: string
      字段名: string
      重叠日期数: number
      重叠日期明细: OverlapDay[]
      补数记录数: number
      待复核值总数: number
    }
  }>
> {
  return postJson('/api/backfill/gaps/overlap-review', payload)
}

export async function listBackfillFills(params: {
  湖表名?: string
  字段名?: string
  元数据版本ID?: number
}): Promise<Envelope<{ items: FillRow[]; total: number }>> {
  const query = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') query.set(key, String(value))
  })
  const response = await request(`/api/backfill/fills?${query.toString()}`)
  if (!response.ok) throw new Error('补数记录读取失败')
  return (await response.json()) as Envelope<{ items: FillRow[]; total: number }>
}

export async function listFieldVersions(
  湖表名: string,
  字段名: string,
): Promise<Envelope<{ items: Record<string, unknown>[]; total: number }>> {
  const query = new URLSearchParams({ 湖表名, 字段名 })
  const response = await request(`/api/backfill/field-versions?${query.toString()}`)
  if (!response.ok) throw new Error('字段版本读取失败')
  return (await response.json()) as Envelope<{ items: Record<string, unknown>[]; total: number }>
}

export async function createFieldChange(payload: {
  湖表名: string
  变更字段: string
  变更类型: string
  允许列值: string[]
  回填起始日期: string
  回填结束日期: string
  变更前快照?: string
}): Promise<Envelope<{ entry: Record<string, unknown> | null }>> {
  return postJson('/api/backfill/field-changes', payload)
}
