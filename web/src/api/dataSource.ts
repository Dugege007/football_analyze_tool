/**
 * 数据源切换：现网（/api → 8787）/ 副本（/api-replica → 8788，v2d3 只读实例，写接口 403）。
 * 标识一律以后端响应里的 meta={db, promoted, readonly} 为准（§12.1 / §13「0.3.18」）：
 *   选现网但 meta.db≠live、选副本但 meta.db≠v2d3 → 报错，不静默混用。
 */
import { useCallback, useEffect, useState } from 'react'

export type DbSource = 'live' | 'replica'

export interface DbMeta {
  db?: string | null
  promoted?: boolean | null
  readonly?: boolean | null
}

export const DB_SOURCE_LABEL: Record<DbSource, string> = {
  live: '现网',
  replica: '副本（未晋升）',
}

/** 每个数据源期望的 meta.db */
export const EXPECTED_DB: Record<DbSource, string> = { live: 'live', replica: 'v2d3' }

export const REPLICA_BANNER = '副本数据（未晋升），仅供核对，不代表现网'
export const REPLICA_READONLY_TIP = '副本只读'

const LS_KEY = 'dataSource.v1'
const EVT = 'data-source-change'

export function apiBase(src: DbSource): string {
  return src === 'replica' ? '/api-replica' : '/api'
}

export function readDbSource(): DbSource {
  try {
    return localStorage.getItem(LS_KEY) === 'replica' ? 'replica' : 'live'
  } catch {
    return 'live'
  }
}

/** 页面共用的数据源选择（存本地；同一页面内多个组件同步） */
export function useDbSource(): [DbSource, (s: DbSource) => void] {
  const [src, setSrc] = useState<DbSource>(readDbSource)
  useEffect(() => {
    const on = () => setSrc(readDbSource())
    window.addEventListener(EVT, on)
    window.addEventListener('storage', on)
    return () => {
      window.removeEventListener(EVT, on)
      window.removeEventListener('storage', on)
    }
  }, [])
  const set = useCallback((s: DbSource) => {
    try {
      localStorage.setItem(LS_KEY, s)
    } catch {
      /* 存不了就只在本页生效 */
    }
    setSrc(s)
    window.dispatchEvent(new Event(EVT))
  }, [])
  return [src, set]
}

/** meta 与所选数据源不符时返回中文错误；相符返回 null */
export function metaMismatch(src: DbSource, meta: DbMeta | null | undefined): string | null {
  const want = EXPECTED_DB[src]
  if (!meta || !meta.db) return `所选数据源为「${DB_SOURCE_LABEL[src]}」，但响应里没有 meta.db，无法确认数据来源，已停止显示`
  if (meta.db !== want)
    return `所选数据源为「${DB_SOURCE_LABEL[src]}」（应为 ${want}），但后端返回 meta.db=${meta.db}，已停止显示，避免混用`
  return null
}

/** 横条只按 meta 判：db=v2d3 且 promoted=false */
export function isUnpromotedReplica(meta: DbMeta | null | undefined): boolean {
  return meta?.db === 'v2d3' && meta.promoted === false
}

export async function fetchHealthMeta(src: DbSource): Promise<DbMeta | null> {
  const res = await fetch(`${apiBase(src)}/health`)
  if (!res.ok) throw new Error(`${DB_SOURCE_LABEL[src]}实例 /health 返回 HTTP ${res.status}`)
  const body = (await res.json()) as { meta?: DbMeta }
  return body.meta ?? null
}
