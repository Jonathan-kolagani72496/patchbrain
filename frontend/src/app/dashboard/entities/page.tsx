'use client'

import { useCallback, useEffect, useMemo, useState } from 'react'
import { useRouter } from 'next/navigation'
import { API_URL, getAuthHeader, supabase } from '@/lib/supabase'

/* ─── Types ──────────────────────────────────────────────────────────────── */
type EntityRow = {
  entity_type: string
  canonical_value: string
  value: string
  source_field: string | null
  source_row: number | null
  confidence_score: number | null
  extraction_method: string | null
  original_filename: string | null
  file_version_id: string | null
}

type EntitySummary = {
  trucks: number
  drivers: number
  clients: number
  leases: number
  ticket_ids: number
  invoice_ids: number
  po_numbers: number
  job_ids: number
}

type GroupedEntity = {
  canonical_value: string
  entity_type: string
  mentions: EntityRow[]
}

const TYPE_CONFIG: Record<string, { label: string; singular: string; color: string; bg: string; border: string }> = {
  truck:      { label: 'Trucks',      singular: 'Truck',      color: '#fdba74', bg: 'rgba(249,115,22,0.10)', border: 'rgba(249,115,22,0.22)' },
  driver:     { label: 'Drivers',     singular: 'Driver',     color: '#c4b5fd', bg: 'rgba(139,92,246,0.10)', border: 'rgba(139,92,246,0.22)' },
  client:     { label: 'Clients',     singular: 'Client',     color: '#86efac', bg: 'rgba(34,197,94,0.10)',  border: 'rgba(34,197,94,0.22)'  },
  lease:      { label: 'Leases',      singular: 'Lease',      color: '#93c5fd', bg: 'rgba(59,130,246,0.10)', border: 'rgba(59,130,246,0.22)' },
  ticket_id:  { label: 'Ticket IDs',  singular: 'Ticket',     color: '#5eead4', bg: 'rgba(20,184,166,0.10)', border: 'rgba(20,184,166,0.22)' },
  invoice_id: { label: 'Invoice IDs', singular: 'Invoice',    color: '#fde047', bg: 'rgba(234,179,8,0.10)',  border: 'rgba(234,179,8,0.22)'  },
  po_number:  { label: 'PO Numbers',  singular: 'PO',         color: '#fca5a5', bg: 'rgba(239,68,68,0.10)',  border: 'rgba(239,68,68,0.22)'  },
  job_id:     { label: 'Job IDs',     singular: 'Job',        color: '#94a3b8', bg: 'rgba(100,116,139,0.12)',border: 'rgba(100,116,139,0.25)' },
}

const TYPE_KEYS = ['truck', 'driver', 'client', 'lease', 'ticket_id', 'invoice_id', 'po_number', 'job_id']

const SUMMARY_KEY_MAP: Record<string, keyof EntitySummary> = {
  truck:      'trucks',
  driver:     'drivers',
  client:     'clients',
  lease:      'leases',
  ticket_id:  'ticket_ids',
  invoice_id: 'invoice_ids',
  po_number:  'po_numbers',
  job_id:     'job_ids',
}

/* ─── Helpers ────────────────────────────────────────────────────────────── */
function groupByCanonical(rows: EntityRow[]): GroupedEntity[] {
  const map = new Map<string, GroupedEntity>()
  for (const row of rows) {
    const key = `${row.entity_type}::${row.canonical_value}`
    if (!map.has(key)) {
      map.set(key, { canonical_value: row.canonical_value, entity_type: row.entity_type, mentions: [] })
    }
    map.get(key)!.mentions.push(row)
  }
  return Array.from(map.values())
}

function confColor(score: number | null): string {
  if (score === null) return '#64748b'
  if (score >= 0.9) return '#22c55e'
  if (score >= 0.7) return '#f97316'
  return '#ef4444'
}

/* ─── Sub-components ─────────────────────────────────────────────────────── */
function TypePill({ type }: { type: string }) {
  const c = TYPE_CONFIG[type]
  if (!c) return <span style={{ fontSize: 11, color: 'var(--fg-subtle)' }}>{type}</span>
  return (
    <span style={{
      display: 'inline-block', padding: '2px 8px', borderRadius: 999, fontSize: 11, fontWeight: 500,
      background: c.bg, color: c.color, border: `1px solid ${c.border}`,
    }}>
      {c.singular}
    </span>
  )
}

function SkeletonRow() {
  return (
    <tr>
      {[20, 35, 12, 18, 15].map((w, i) => (
        <td key={i} style={{ padding: '12px 14px' }}>
          <div style={{ height: 12, width: `${w}%`, minWidth: 40, borderRadius: 5, background: 'rgba(255,255,255,0.05)', animation: 'shimmer 1.6s ease-in-out infinite' }} />
        </td>
      ))}
    </tr>
  )
}

/* ═══════════════════════════════════════════════════════════════════════════
   EntitiesPage
   ═══════════════════════════════════════════════════════════════════════════ */
export default function EntitiesPage() {
  const router = useRouter()
  const [rows, setRows] = useState<EntityRow[]>([])
  const [summary, setSummary] = useState<EntitySummary | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [activeType, setActiveType] = useState('truck')
  const [search, setSearch] = useState('')
  const [limit, setLimit] = useState(100)
  const [expandedKey, setExpandedKey] = useState<string | null>(null)

  /* ── Auth + load ── */
  useEffect(() => {
    let alive = true
    supabase.auth.getSession().then(({ data }) => {
      if (!alive) return
      if (!data.session) { router.replace('/login'); return }
      loadSummary(alive)
      loadEntities(activeType, search, alive)
    })
    return () => { alive = false }
  }, [router])

  const loadSummary = useCallback(async (alive = true) => {
    try {
      const headers = await getAuthHeader()
      const res = await fetch(`${API_URL}/entities/summary`, { headers })
      if (!res.ok) return
      const data = await res.json().catch(() => null)
      if (alive && data) setSummary(data)
    } catch {}
  }, [])

  const loadEntities = useCallback(async (type: string, q: string, alive = true) => {
    setLoading(true)
    setError('')
    try {
      const headers = await getAuthHeader()
      const params = new URLSearchParams({ entity_type: type, limit: String(limit) })
      if (q.trim()) params.set('search', q.trim())
      const res = await fetch(`${API_URL}/entities?${params}`, { headers })
      if (res.status === 401) { router.replace('/login'); return }
      const data = await res.json().catch(() => [])
      if (!res.ok) throw new Error(data?.detail || 'Failed to load entities')
      if (alive) setRows(Array.isArray(data) ? data : [])
    } catch (err: any) {
      if (alive) setError(err?.message || 'Failed to load')
    } finally {
      if (alive) setLoading(false)
    }
  }, [router, limit])

  /* ── Trigger load on type/search change ── */
  useEffect(() => {
    const t = window.setTimeout(() => loadEntities(activeType, search, true), search ? 300 : 0)
    return () => window.clearTimeout(t)
  }, [activeType, search, loadEntities])

  /* ── Grouped data ── */
  const grouped = useMemo(() => groupByCanonical(rows), [rows])

  /* ── Render ── */
  return (
    <>
      <style>{`
        @keyframes shimmer { 0%,100%{opacity:.5} 50%{opacity:.9} }
        @keyframes fade-in-up { from{opacity:0;transform:translateY(8px)} to{opacity:1;transform:translateY(0)} }
        .ent-row { cursor:pointer; transition:background .12s; }
        .ent-row:hover { background: rgba(249,115,22,0.03) !important; }
        .ent-nav-link { cursor:pointer; border:none; background:transparent; transition:all .15s ease; }
        .ent-nav-link:hover { color: var(--fg) !important; }
        .ent-type-btn { cursor:pointer; border:none; transition:all .15s ease; }
        .ent-type-btn:hover { border-color: rgba(255,255,255,0.15) !important; }
      `}</style>

      <main style={{ minHeight: '100dvh', background: 'var(--bg)', color: 'var(--fg)', fontFamily: 'var(--font-sans,"IBM Plex Sans",sans-serif)' }}>

        {/* ── Header ── */}
        <header style={{
          position: 'sticky', top: 0, zIndex: 30,
          borderBottom: '1px solid var(--border)',
          background: 'rgba(6,9,16,0.88)',
          backdropFilter: 'blur(16px)', WebkitBackdropFilter: 'blur(16px)',
        }}>
          <div style={{ maxWidth: 1280, margin: '0 auto', padding: '0 20px', display: 'flex', alignItems: 'center', justifyContent: 'space-between', height: 60 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <div style={{
                width: 36, height: 36, borderRadius: 10, flexShrink: 0,
                background: 'linear-gradient(135deg, #f97316 0%, #c2410c 100%)',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                boxShadow: '0 2px 12px rgba(249,115,22,0.30)',
              }}>
                <span style={{ color: '#fff', fontSize: 15, fontWeight: 800, lineHeight: 1 }}>P</span>
              </div>
              <span style={{ fontSize: 14, fontWeight: 600, color: 'var(--fg)' }}>PatchBrain</span>
            </div>

            <nav style={{ display: 'flex', alignItems: 'center', gap: 2 }}>
              {[
                { label: 'Files', href: '/dashboard' },
                { label: 'Library', href: '/dashboard/library' },
                { label: 'Entities', href: '/dashboard/entities' },
                { label: 'Issues', href: '/dashboard/issues' },
              ].map(item => {
                const active = item.href === '/dashboard/entities'
                return (
                  <button key={item.href} className="ent-nav-link" onClick={() => router.push(item.href)}
                    style={{
                      padding: '5px 12px', borderRadius: 7, fontSize: 13, fontWeight: active ? 600 : 400,
                      color: active ? '#fdba74' : 'var(--fg-muted)',
                      background: active ? 'rgba(249,115,22,0.08)' : 'transparent',
                      border: `1px solid ${active ? 'rgba(249,115,22,0.2)' : 'transparent'}`,
                    }}>
                    {item.label}
                  </button>
                )
              })}
              <div style={{ width: 1, height: 20, background: 'var(--border)', margin: '0 6px' }} />
              <button
                onClick={() => router.push('/chat')}
                style={{
                  display: 'flex', alignItems: 'center', gap: 6,
                  padding: '6px 14px', borderRadius: 8, fontSize: 13, fontWeight: 600,
                  color: '#fff', background: '#f97316', border: 'none', cursor: 'pointer',
                  boxShadow: '0 2px 10px rgba(249,115,22,0.28)', transition: 'all .15s ease',
                }}
                onMouseEnter={e => { e.currentTarget.style.background = '#fb923c'; e.currentTarget.style.transform = 'translateY(-1px)' }}
                onMouseLeave={e => { e.currentTarget.style.background = '#f97316'; e.currentTarget.style.transform = 'translateY(0)' }}
              >
                Ask PatchBrain
              </button>
            </nav>
          </div>
        </header>

        <div style={{ maxWidth: 1280, margin: '0 auto', padding: '28px 20px', animation: 'fade-in-up 0.4s ease both' }}>

          {/* ── Title ── */}
          <div style={{ marginBottom: 24 }}>
            <h1 style={{ margin: 0, fontSize: 20, fontWeight: 600, color: 'var(--fg)' }}>Entity Explorer</h1>
            <p style={{ margin: '4px 0 0', fontSize: 13, color: 'var(--fg-muted)' }}>
              Extracted from uploaded documents by PatchBrain
            </p>
          </div>

          {/* ── Summary cards ── */}
          <div style={{ display: 'grid', gap: 10, gridTemplateColumns: 'repeat(auto-fill, minmax(120px, 1fr))', marginBottom: 24 }}>
            {TYPE_KEYS.map(type => {
              const c = TYPE_CONFIG[type]
              const summaryKey = SUMMARY_KEY_MAP[type]
              const count = summary ? summary[summaryKey] : null
              const active = activeType === type
              return (
                <button
                  key={type}
                  className="ent-type-btn"
                  onClick={() => setActiveType(type)}
                  style={{
                    padding: '14px 16px', borderRadius: 12, textAlign: 'left',
                    background: active ? c.bg : 'var(--bg-card)',
                    border: `1px solid ${active ? c.border : 'var(--border)'}`,
                    cursor: 'pointer', transition: 'all .15s ease',
                  }}
                >
                  <p style={{ margin: 0, fontSize: 11, fontWeight: 500, color: active ? c.color : 'var(--fg-subtle)', textTransform: 'uppercase', letterSpacing: '0.08em' }}>
                    {c.label}
                  </p>
                  <p style={{ margin: '6px 0 0', fontSize: 22, fontWeight: 700, color: active ? c.color : 'var(--fg)', lineHeight: 1 }}>
                    {summary === null ? '—' : count ?? 0}
                  </p>
                </button>
              )
            })}
          </div>

          {/* ── Search + limit ── */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 16, flexWrap: 'wrap' }}>
            <input
              type="search"
              placeholder={`Search ${TYPE_CONFIG[activeType]?.label.toLowerCase() || 'entities'}…`}
              value={search}
              onChange={e => setSearch(e.target.value)}
              style={{
                flex: '1 1 220px', maxWidth: 320, padding: '7px 12px', borderRadius: 8, fontSize: 13,
                background: 'var(--bg-elevated)', color: 'var(--fg)',
                border: '1px solid var(--border)', outline: 'none',
              }}
              onFocus={e => { e.currentTarget.style.borderColor = 'rgba(249,115,22,0.4)' }}
              onBlur={e => { e.currentTarget.style.borderColor = 'var(--border)' }}
            />
            <select
              value={limit}
              onChange={e => setLimit(Number(e.target.value))}
              style={{
                padding: '7px 10px', borderRadius: 8, fontSize: 12,
                background: 'var(--bg-elevated)', color: 'var(--fg-muted)',
                border: '1px solid var(--border)', outline: 'none', cursor: 'pointer',
              }}
            >
              {[50, 100, 200, 500].map(n => <option key={n} value={n}>Show {n}</option>)}
            </select>
            <span style={{ fontSize: 12, color: 'var(--fg-subtle)', marginLeft: 4 }}>
              {loading ? '…' : `${grouped.length} unique · ${rows.length} mentions`}
            </span>
          </div>

          {/* ── Error ── */}
          {error && (
            <div style={{
              padding: '12px 16px', borderRadius: 10, marginBottom: 16,
              background: 'rgba(248,113,113,0.08)', border: '1px solid rgba(248,113,113,0.2)',
              color: '#fca5a5', fontSize: 13, display: 'flex', gap: 8,
            }}>
              <span>⚠</span><span>{error}</span>
            </div>
          )}

          {/* ── Table ── */}
          <div style={{ borderRadius: 14, border: '1px solid var(--border)', overflow: 'hidden', background: 'var(--bg-card)' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse' }}>
              <thead>
                <tr style={{ borderBottom: '1px solid var(--border)', background: 'rgba(255,255,255,0.02)' }}>
                  {['Value', 'Type', 'Mentions', 'Example file', 'Confidence'].map(h => (
                    <th key={h} style={{ padding: '10px 14px', textAlign: 'left', fontSize: 11, fontWeight: 600, color: 'var(--fg-subtle)', letterSpacing: '0.08em', textTransform: 'uppercase', whiteSpace: 'nowrap' }}>
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {loading
                  ? Array.from({ length: 8 }).map((_, i) => <SkeletonRow key={i} />)
                  : grouped.length === 0
                    ? (
                      <tr>
                        <td colSpan={5} style={{ padding: '32px', textAlign: 'center', color: 'var(--fg-muted)', fontSize: 13 }}>
                          {search ? 'No matches found.' : 'No entities extracted yet for this type.'}
                        </td>
                      </tr>
                    )
                    : grouped.map(g => {
                      const key = `${g.entity_type}::${g.canonical_value}`
                      const isExpanded = expandedKey === key
                      const exampleFile = g.mentions[0]?.original_filename || '—'
                      const avgConf = g.mentions.reduce((sum, m) => sum + (m.confidence_score ?? 0), 0) / g.mentions.length

                      return (
                        <>
                          <tr
                            key={key}
                            className="ent-row"
                            onClick={() => setExpandedKey(isExpanded ? null : key)}
                            style={{ borderBottom: '1px solid var(--border)' }}
                          >
                            <td style={{ padding: '12px 14px' }}>
                              <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                                <svg width="10" height="10" viewBox="0 0 10 10" fill="none" style={{ flexShrink: 0, transition: 'transform .15s', transform: isExpanded ? 'rotate(90deg)' : 'rotate(0deg)', color: 'var(--fg-subtle)' }}>
                                  <path d="M3 2l4 3-4 3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
                                </svg>
                                <span style={{ fontSize: 13, fontWeight: 500, color: 'var(--fg)' }}>{g.canonical_value}</span>
                              </div>
                            </td>
                            <td style={{ padding: '12px 14px' }}><TypePill type={g.entity_type} /></td>
                            <td style={{ padding: '12px 14px', fontSize: 13, color: 'var(--fg-muted)', fontVariantNumeric: 'tabular-nums' }}>
                              {g.mentions.length}
                            </td>
                            <td style={{ padding: '12px 14px', fontSize: 12, color: 'var(--fg-muted)', maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                              {exampleFile}
                            </td>
                            <td style={{ padding: '12px 14px' }}>
                              <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                                <div style={{
                                  width: 48, height: 4, borderRadius: 2, background: 'rgba(255,255,255,0.08)', overflow: 'hidden',
                                }}>
                                  <div style={{ height: '100%', width: `${(avgConf || 0) * 100}%`, background: confColor(avgConf), borderRadius: 2 }} />
                                </div>
                                <span style={{ fontSize: 11, color: confColor(avgConf), fontVariantNumeric: 'tabular-nums' }}>
                                  {avgConf ? `${Math.round(avgConf * 100)}%` : '—'}
                                </span>
                              </div>
                            </td>
                          </tr>

                          {/* Expanded mentions */}
                          {isExpanded && g.mentions.map((m, mi) => (
                            <tr key={`${key}-${mi}`} style={{ background: 'rgba(249,115,22,0.025)', borderBottom: '1px solid rgba(255,255,255,0.04)' }}>
                              <td colSpan={5} style={{ padding: '8px 14px 8px 34px' }}>
                                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px 20px', fontSize: 11, color: 'var(--fg-muted)' }}>
                                  {m.original_filename && (
                                    <span><span style={{ color: 'var(--fg-subtle)' }}>File: </span>{m.original_filename}</span>
                                  )}
                                  {m.value !== m.canonical_value && (
                                    <span><span style={{ color: 'var(--fg-subtle)' }}>Raw: </span>{m.value}</span>
                                  )}
                                  {m.source_field && (
                                    <span><span style={{ color: 'var(--fg-subtle)' }}>Field: </span>{m.source_field}</span>
                                  )}
                                  {m.source_row !== null && m.source_row !== undefined && (
                                    <span><span style={{ color: 'var(--fg-subtle)' }}>Row: </span>{m.source_row}</span>
                                  )}
                                  {m.extraction_method && (
                                    <span><span style={{ color: 'var(--fg-subtle)' }}>Method: </span>{m.extraction_method}</span>
                                  )}
                                  {m.confidence_score !== null && (
                                    <span style={{ color: confColor(m.confidence_score) }}>
                                      {Math.round((m.confidence_score ?? 0) * 100)}% conf
                                    </span>
                                  )}
                                </div>
                              </td>
                            </tr>
                          ))}
                        </>
                      )
                    })
                }
              </tbody>
            </table>
          </div>
        </div>
      </main>
    </>
  )
}
