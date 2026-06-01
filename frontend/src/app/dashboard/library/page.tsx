'use client'

import { useCallback, useEffect, useMemo, useState } from 'react'
import { useRouter } from 'next/navigation'
import { API_URL, getAuthHeader, supabase } from '@/lib/supabase'

/* ─── Types ──────────────────────────────────────────────────────────────── */
type IntelRecord = {
  id: string
  file_id: string
  file_version_id: string
  company_id: string
  category: string
  document_type: string
  short_summary: string | null
  key_entities: Record<string, string[]> | null
  suggested_questions: string[] | null
  risk_flags: string[] | null
  needs_review: boolean
  date_start: string | null
  date_end: string | null
  confidence_score: number | null
  created_at: string
  // joined
  logical_name: string | null
  original_filename: string | null
  version_status: string | null
  version_created_at: string | null
}

const CATEGORY_LABELS: Record<string, string> = {
  field_data:     'Field Data',
  financials:     'Financials',
  safety:         'Safety',
  maintenance:    'Maintenance',
  compliance:     'Compliance',
  communications: 'Communications',
  unknown:        'Unknown',
}

const CATEGORY_COLORS: Record<string, { bg: string; text: string; border: string; dot: string }> = {
  field_data:     { bg: 'rgba(249,115,22,0.10)',  text: '#fdba74', border: 'rgba(249,115,22,0.22)',  dot: '#f97316' },
  financials:     { bg: 'rgba(34,197,94,0.10)',   text: '#86efac', border: 'rgba(34,197,94,0.22)',   dot: '#22c55e' },
  safety:         { bg: 'rgba(239,68,68,0.10)',   text: '#fca5a5', border: 'rgba(239,68,68,0.22)',   dot: '#ef4444' },
  maintenance:    { bg: 'rgba(139,92,246,0.10)',  text: '#c4b5fd', border: 'rgba(139,92,246,0.22)',  dot: '#8b5cf6' },
  compliance:     { bg: 'rgba(59,130,246,0.10)',  text: '#93c5fd', border: 'rgba(59,130,246,0.22)',  dot: '#3b82f6' },
  communications: { bg: 'rgba(20,184,166,0.10)',  text: '#5eead4', border: 'rgba(20,184,166,0.22)',  dot: '#14b8a6' },
  unknown:        { bg: 'rgba(100,116,139,0.12)', text: '#94a3b8', border: 'rgba(100,116,139,0.22)', dot: '#64748b' },
}

const RISK_LABELS: Record<string, { label: string; color: string }> = {
  expired_cert:      { label: 'Expired cert',      color: '#f87171' },
  spill_incident:    { label: 'Spill/Incident',     color: '#f87171' },
  missing_po:        { label: 'Missing PO',         color: '#fbbf24' },
  failed_parser:     { label: 'Parser failed',      color: '#f87171' },
  unknown_category:  { label: 'Unknown category',   color: '#94a3b8' },
  needs_review:      { label: 'Needs review',       color: '#fde047' },
}

const SUMMARY_BUCKETS = [
  { key: 'all',           label: 'All Files',      filter: (_r: IntelRecord) => true },
  { key: 'field_data',    label: 'Field Data',     filter: (r: IntelRecord) => r.category === 'field_data' },
  { key: 'financials',    label: 'Financials',     filter: (r: IntelRecord) => r.category === 'financials' },
  { key: 'safety',        label: 'Safety',         filter: (r: IntelRecord) => r.category === 'safety' },
  { key: 'maintenance',   label: 'Maintenance',    filter: (r: IntelRecord) => r.category === 'maintenance' },
  { key: 'compliance',    label: 'Compliance',     filter: (r: IntelRecord) => r.category === 'compliance' },
  { key: 'communications',label: 'Communications', filter: (r: IntelRecord) => r.category === 'communications' },
  { key: 'needs_review',  label: 'Needs Review',   filter: (r: IntelRecord) => r.needs_review === true },
  { key: 'unknown',       label: 'Unknown',        filter: (r: IntelRecord) => r.category === 'unknown' },
]

/* ─── Helpers ────────────────────────────────────────────────────────────── */
function displayName(r: IntelRecord): string {
  return r.logical_name || r.original_filename || 'Unnamed file'
}

function formatDateRange(start: string | null, end: string | null): string {
  if (!start && !end) return '—'
  const fmt = (d: string) => {
    const parsed = new Date(d)
    if (isNaN(parsed.getTime())) return d
    return parsed.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
  }
  if (start && end && start !== end) return `${fmt(start)} – ${fmt(end)}`
  return fmt(start || end || '')
}

function flatEntities(key_entities: Record<string, string[]> | null): string[] {
  if (!key_entities) return []
  return Object.values(key_entities).flat().slice(0, 8)
}

function parseRiskFlags(raw: unknown): string[] {
  if (!raw) return []
  if (Array.isArray(raw)) {
    return raw.map(x => (typeof x === 'string' ? x : x?.type || x?.flag || String(x))).filter(Boolean)
  }
  if (typeof raw === 'string') {
    try { return parseRiskFlags(JSON.parse(raw)) } catch { return [] }
  }
  return []
}

/* ─── Sub-components ─────────────────────────────────────────────────────── */
function CategoryPill({ category }: { category: string }) {
  const c = CATEGORY_COLORS[category] || CATEGORY_COLORS.unknown
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 5,
      padding: '2px 8px', borderRadius: 999, fontSize: 11, fontWeight: 500,
      background: c.bg, color: c.text, border: `1px solid ${c.border}`,
    }}>
      <span style={{ width: 5, height: 5, borderRadius: '50%', background: c.dot, flexShrink: 0 }} />
      {CATEGORY_LABELS[category] || category}
    </span>
  )
}

function RiskBadge({ flag }: { flag: string }) {
  const r = RISK_LABELS[flag] || { label: flag, color: '#94a3b8' }
  return (
    <span style={{
      display: 'inline-block', padding: '2px 7px', borderRadius: 4, fontSize: 10, fontWeight: 600,
      letterSpacing: '0.04em', textTransform: 'uppercase',
      background: 'rgba(248,113,113,0.08)', color: r.color,
      border: `1px solid ${r.color}30`,
    }}>
      {r.label}
    </span>
  )
}

function SkeletonCard() {
  return (
    <div style={{
      padding: '20px 22px', borderRadius: 14, border: '1px solid var(--border)',
      background: 'var(--bg-card)', display: 'flex', flexDirection: 'column', gap: 12,
    }}>
      {[60, 40, 80, 50].map((w, i) => (
        <div key={i} style={{
          height: i === 0 ? 16 : 12, width: `${w}%`,
          borderRadius: 6, background: 'rgba(255,255,255,0.05)',
          animation: 'shimmer 1.6s ease-in-out infinite',
        }} />
      ))}
    </div>
  )
}

/* ═══════════════════════════════════════════════════════════════════════════
   LibraryPage
   ═══════════════════════════════════════════════════════════════════════════ */
export default function LibraryPage() {
  const router = useRouter()
  const [records, setRecords] = useState<IntelRecord[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [activeBucket, setActiveBucket] = useState('all')
  const [search, setSearch] = useState('')
  const [expandedId, setExpandedId] = useState<string | null>(null)

  /* ── Auth + load ── */
  useEffect(() => {
    let alive = true
    supabase.auth.getSession().then(({ data }) => {
      if (!alive) return
      if (!data.session) { router.replace('/login'); return }
      loadIntel(alive)
    })
    return () => { alive = false }
  }, [router])

  const loadIntel = useCallback(async (alive = true) => {
    setLoading(true)
    setError('')
    try {
      const headers = await getAuthHeader()
      const res = await fetch(`${API_URL}/files/intelligence`, { headers })
      if (res.status === 401) { router.replace('/login'); return }
      const data = await res.json().catch(() => [])
      if (!res.ok) throw new Error(data?.detail || 'Failed to load intelligence data')
      if (alive) setRecords(Array.isArray(data) ? data : [])
    } catch (err: any) {
      if (alive) setError(err?.message || 'Failed to load')
    } finally {
      if (alive) setLoading(false)
    }
  }, [router])

  /* ── Filtered records ── */
  const filtered = useMemo(() => {
    const bucket = SUMMARY_BUCKETS.find(b => b.key === activeBucket)
    let result = bucket ? records.filter(bucket.filter) : records
    const q = search.trim().toLowerCase()
    if (q) {
      result = result.filter(r =>
        displayName(r).toLowerCase().includes(q) ||
        (r.short_summary || '').toLowerCase().includes(q) ||
        flatEntities(r.key_entities).some(e => e.toLowerCase().includes(q))
      )
    }
    return result
  }, [records, activeBucket, search])

  /* ── Bucket counts ── */
  const bucketCounts = useMemo(() => {
    const map: Record<string, number> = {}
    for (const b of SUMMARY_BUCKETS) {
      map[b.key] = records.filter(b.filter).length
    }
    return map
  }, [records])

  /* ── Render ── */
  return (
    <>
      <style>{`
        @keyframes shimmer { 0%,100%{opacity:.5} 50%{opacity:.9} }
        @keyframes fade-in-up { from{opacity:0;transform:translateY(8px)} to{opacity:1;transform:translateY(0)} }
        .lib-bucket-btn { cursor:pointer; border:none; transition:all .15s ease; }
        .lib-bucket-btn:hover { border-color: rgba(249,115,22,0.3) !important; }
        .lib-card { transition: border-color .15s ease; }
        .lib-card:hover { border-color: rgba(249,115,22,0.25) !important; }
        .lib-nav-link { cursor:pointer; border:none; background:transparent; transition:all .15s ease; }
        .lib-nav-link:hover { color: var(--fg) !important; }
        .lib-tag { display:inline-block; padding:2px 7px; border-radius:5px; font-size:11px;
          background:rgba(255,255,255,0.05); color:var(--fg-muted); border:1px solid var(--border); }
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
            {/* Logo */}
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

            {/* Nav tabs */}
            <nav style={{ display: 'flex', alignItems: 'center', gap: 2 }}>
              {[
                { label: 'Files', href: '/dashboard' },
                { label: 'Library', href: '/dashboard/library' },
                { label: 'Entities', href: '/dashboard/entities' },
                { label: 'Issues', href: '/dashboard/issues' },
              ].map(item => {
                const active = item.href === '/dashboard/library'
                return (
                  <button
                    key={item.href}
                    className="lib-nav-link"
                    onClick={() => router.push(item.href)}
                    style={{
                      padding: '5px 12px', borderRadius: 7, fontSize: 13, fontWeight: active ? 600 : 400,
                      color: active ? '#fdba74' : 'var(--fg-muted)',
                      background: active ? 'rgba(249,115,22,0.08)' : 'transparent',
                      border: `1px solid ${active ? 'rgba(249,115,22,0.2)' : 'transparent'}`,
                    }}
                  >
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

          {/* ── Page title + search ── */}
          <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 16, marginBottom: 24, flexWrap: 'wrap' }}>
            <div>
              <h1 style={{ margin: 0, fontSize: 20, fontWeight: 600, color: 'var(--fg)' }}>Document Library</h1>
              <p style={{ margin: '4px 0 0', fontSize: 13, color: 'var(--fg-muted)' }}>
                Auto-categorized by PatchBrain intelligence pass
              </p>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <input
                type="search"
                placeholder="Search files, summaries, entities…"
                value={search}
                onChange={e => setSearch(e.target.value)}
                style={{
                  width: 280, padding: '7px 12px', borderRadius: 8, fontSize: 13,
                  background: 'var(--bg-elevated)', color: 'var(--fg)',
                  border: '1px solid var(--border)', outline: 'none',
                }}
                onFocus={e => { e.currentTarget.style.borderColor = 'rgba(249,115,22,0.4)' }}
                onBlur={e => { e.currentTarget.style.borderColor = 'var(--border)' }}
              />
            </div>
          </div>

          {/* ── Bucket filter pills ── */}
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 24 }}>
            {SUMMARY_BUCKETS.map(b => {
              const active = activeBucket === b.key
              const count = bucketCounts[b.key] ?? 0
              const isReview = b.key === 'needs_review'
              return (
                <button
                  key={b.key}
                  className="lib-bucket-btn"
                  onClick={() => setActiveBucket(b.key)}
                  style={{
                    display: 'flex', alignItems: 'center', gap: 6,
                    padding: '5px 12px', borderRadius: 8, fontSize: 12, fontWeight: active ? 600 : 400,
                    background: active
                      ? (isReview ? 'rgba(234,179,8,0.12)' : 'rgba(249,115,22,0.10)')
                      : 'var(--bg-card)',
                    color: active
                      ? (isReview ? '#fde047' : '#fdba74')
                      : 'var(--fg-muted)',
                    border: `1px solid ${active
                      ? (isReview ? 'rgba(234,179,8,0.3)' : 'rgba(249,115,22,0.28)')
                      : 'var(--border)'}`,
                  }}
                >
                  {b.label}
                  <span style={{
                    padding: '1px 6px', borderRadius: 999, fontSize: 10, fontWeight: 600,
                    background: 'rgba(255,255,255,0.07)', color: active ? 'inherit' : 'var(--fg-subtle)',
                  }}>
                    {loading ? '—' : count}
                  </span>
                </button>
              )
            })}
          </div>

          {/* ── Error ── */}
          {error && (
            <div style={{
              padding: '12px 16px', borderRadius: 10, marginBottom: 20,
              background: 'rgba(248,113,113,0.08)', border: '1px solid rgba(248,113,113,0.2)',
              color: '#fca5a5', fontSize: 13, display: 'flex', alignItems: 'center', gap: 8,
            }}>
              <span>⚠</span>
              <span>{error}</span>
              <button onClick={() => loadIntel(true)} style={{ marginLeft: 'auto', background: 'none', border: '1px solid rgba(248,113,113,0.3)', color: '#fca5a5', borderRadius: 6, padding: '3px 10px', fontSize: 12, cursor: 'pointer' }}>
                Retry
              </button>
            </div>
          )}

          {/* ── Empty / no intel yet ── */}
          {!loading && !error && records.length === 0 && (
            <div style={{
              padding: '48px 24px', textAlign: 'center', borderRadius: 16,
              border: '1px dashed var(--border)', background: 'var(--bg-card)',
            }}>
              <p style={{ fontSize: 15, fontWeight: 500, color: 'var(--fg-muted)', margin: 0 }}>No intelligence records yet</p>
              <p style={{ fontSize: 13, color: 'var(--fg-subtle)', marginTop: 6 }}>
                Upload files and wait for PatchBrain to finish processing them.
              </p>
              <button
                onClick={() => router.push('/dashboard')}
                style={{
                  marginTop: 16, padding: '8px 18px', borderRadius: 8, fontSize: 13, fontWeight: 500,
                  background: 'rgba(249,115,22,0.1)', color: '#fdba74',
                  border: '1px solid rgba(249,115,22,0.2)', cursor: 'pointer',
                }}
              >
                Go to File Upload
              </button>
            </div>
          )}

          {/* ── No results after filter ── */}
          {!loading && !error && records.length > 0 && filtered.length === 0 && (
            <div style={{ padding: '32px 24px', textAlign: 'center', color: 'var(--fg-muted)', fontSize: 13 }}>
              No files match this filter.
              <button onClick={() => { setActiveBucket('all'); setSearch('') }}
                style={{ marginLeft: 8, color: '#fdba74', background: 'none', border: 'none', cursor: 'pointer', fontSize: 13 }}>
                Clear filters
              </button>
            </div>
          )}

          {/* ── Cards grid ── */}
          <div style={{ display: 'grid', gap: 14, gridTemplateColumns: 'repeat(auto-fill, minmax(360px, 1fr))' }}>
            {loading
              ? Array.from({ length: 6 }).map((_, i) => <SkeletonCard key={i} />)
              : filtered.map(r => {
                const flags = parseRiskFlags(r.risk_flags)
                const entities = flatEntities(r.key_entities)
                const questions = (r.suggested_questions || []).slice(0, 3)
                const isExpanded = expandedId === r.id

                return (
                  <div
                    key={r.id}
                    className="lib-card"
                    style={{
                      padding: '18px 20px', borderRadius: 14,
                      border: `1px solid ${r.needs_review ? 'rgba(234,179,8,0.18)' : 'var(--border)'}`,
                      background: 'var(--bg-card)',
                      display: 'flex', flexDirection: 'column', gap: 10,
                    }}
                  >
                    {/* Filename + badges row */}
                    <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 8 }}>
                      <p style={{ margin: 0, fontSize: 13, fontWeight: 600, color: 'var(--fg)', lineHeight: 1.35, wordBreak: 'break-word' }}>
                        {displayName(r)}
                      </p>
                      <div style={{ display: 'flex', gap: 4, flexShrink: 0 }}>
                        {r.needs_review && (
                          <span style={{
                            padding: '2px 7px', borderRadius: 4, fontSize: 10, fontWeight: 600,
                            letterSpacing: '0.06em', textTransform: 'uppercase',
                            background: 'rgba(234,179,8,0.10)', color: '#fde047',
                            border: '1px solid rgba(234,179,8,0.22)',
                          }}>
                            Review
                          </span>
                        )}
                        {r.version_status && (
                          <span style={{
                            padding: '2px 7px', borderRadius: 4, fontSize: 10, fontWeight: 500,
                            background: r.version_status === 'ready' ? 'rgba(34,197,94,0.08)' : 'rgba(100,116,139,0.12)',
                            color: r.version_status === 'ready' ? '#86efac' : '#94a3b8',
                            border: `1px solid ${r.version_status === 'ready' ? 'rgba(34,197,94,0.18)' : 'rgba(100,116,139,0.2)'}`,
                          }}>
                            {r.version_status}
                          </span>
                        )}
                      </div>
                    </div>

                    {/* Category + type */}
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                      <CategoryPill category={r.category} />
                      {r.document_type && r.document_type !== r.category && (
                        <span className="lib-tag">{r.document_type.replace(/_/g, ' ')}</span>
                      )}
                      {r.date_start || r.date_end ? (
                        <span style={{ fontSize: 11, color: 'var(--fg-subtle)', marginLeft: 'auto' }}>
                          {formatDateRange(r.date_start, r.date_end)}
                        </span>
                      ) : null}
                    </div>

                    {/* Summary */}
                    {r.short_summary && (
                      <p style={{ margin: 0, fontSize: 12, color: 'var(--fg-muted)', lineHeight: 1.6 }}>
                        {r.short_summary}
                      </p>
                    )}

                    {/* Entities */}
                    {entities.length > 0 && (
                      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                        {entities.map((e, i) => (
                          <span key={i} className="lib-tag">{e}</span>
                        ))}
                      </div>
                    )}

                    {/* Risk flags */}
                    {flags.length > 0 && (
                      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                        {flags.map((f, i) => <RiskBadge key={i} flag={f} />)}
                      </div>
                    )}

                    {/* Expand toggle */}
                    {questions.length > 0 && (
                      <>
                        <button
                          onClick={() => setExpandedId(isExpanded ? null : r.id)}
                          style={{
                            alignSelf: 'flex-start', padding: '3px 0', background: 'none', border: 'none',
                            color: '#fdba74', fontSize: 12, cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 4,
                          }}
                        >
                          <svg width="10" height="10" viewBox="0 0 10 10" fill="none" style={{ transition: 'transform .15s', transform: isExpanded ? 'rotate(90deg)' : 'rotate(0deg)' }}>
                            <path d="M3 2l4 3-4 3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
                          </svg>
                          {isExpanded ? 'Hide suggested questions' : 'Suggested questions'}
                        </button>
                        {isExpanded && (
                          <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                            {questions.map((q, i) => (
                              <button
                                key={i}
                                onClick={() => router.push(`/chat?q=${encodeURIComponent(q)}`)}
                                style={{
                                  textAlign: 'left', padding: '6px 10px', borderRadius: 7, fontSize: 12,
                                  background: 'rgba(249,115,22,0.06)', color: '#fdba74',
                                  border: '1px solid rgba(249,115,22,0.14)', cursor: 'pointer',
                                  transition: 'background .12s',
                                }}
                                onMouseEnter={e => { e.currentTarget.style.background = 'rgba(249,115,22,0.12)' }}
                                onMouseLeave={e => { e.currentTarget.style.background = 'rgba(249,115,22,0.06)' }}
                              >
                                {q}
                              </button>
                            ))}
                          </div>
                        )}
                      </>
                    )}
                  </div>
                )
              })}
          </div>
        </div>
      </main>
    </>
  )
}
