'use client'

import { useCallback, useEffect, useMemo, useState } from 'react'
import { useRouter } from 'next/navigation'
import { API_URL, getAuthHeader, supabase } from '@/lib/supabase'

/* ─── Types ──────────────────────────────────────────────────────────────── */
type OperationalIssue = {
  id: string
  company_id: string
  issue_type: string
  compliance_framework: string
  status: 'candidate' | 'confirmed' | 'dismissed' | 'resolved' | 'ignored'
  severity: 'critical' | 'high' | 'medium' | 'low'
  title: string
  description: string | null
  related_entities: Record<string, any>
  related_files: any[]
  raw_data: Record<string, any>
  confidence_score: number
  rule_key: string
  created_at: string
}

type IntelRecord = {
  id: string
  file_id: string
  category: string
  document_type: string
  short_summary: string | null
  risk_flags: unknown
  needs_review: boolean
  date_start: string | null
  date_end: string | null
  created_at: string
  logical_name: string | null
  original_filename: string | null
  version_status: string | null
}

/* ─── Risk config (legacy fallback) ─────────────────────────────────────── */
const RISK_CONFIG: Record<string, {
  label: string; description: string; action: string
  severity: 'high' | 'medium' | 'low'; color: string; bg: string; border: string
}> = {
  expired_cert: {
    label: 'Expired Certification',
    description: 'A certification or qualification may be expired or past due.',
    action: 'Verify certification renewal and update records.',
    severity: 'high', color: '#f87171', bg: 'rgba(248,113,113,0.08)', border: 'rgba(248,113,113,0.22)',
  },
  spill_incident: {
    label: 'Spill / Incident',
    description: 'Document references a spill, leak, blowout, or operational incident.',
    action: 'Review incident notes and confirm follow-up actions are documented.',
    severity: 'high', color: '#f87171', bg: 'rgba(248,113,113,0.08)', border: 'rgba(248,113,113,0.22)',
  },
  missing_po: {
    label: 'Missing PO',
    description: 'Invoice references a missing or unresolved purchase order.',
    action: 'Check invoice against email or dispatch records for PO mismatch.',
    severity: 'medium', color: '#fbbf24', bg: 'rgba(251,191,36,0.08)', border: 'rgba(251,191,36,0.22)',
  },
  failed_parser: {
    label: 'Parser Failed',
    description: 'PatchBrain could not fully parse this file.',
    action: 'Re-upload the file or check for formatting issues.',
    severity: 'medium', color: '#fbbf24', bg: 'rgba(251,191,36,0.08)', border: 'rgba(251,191,36,0.22)',
  },
  unknown_category: {
    label: 'Unknown Category',
    description: 'File could not be classified into a known document type.',
    action: 'Review classification manually and re-upload if needed.',
    severity: 'low', color: '#94a3b8', bg: 'rgba(100,116,139,0.10)', border: 'rgba(100,116,139,0.22)',
  },
  needs_review: {
    label: 'Needs Review',
    description: 'File was flagged for manual review during processing.',
    action: 'Open the file and verify contents.',
    severity: 'low', color: '#fde047', bg: 'rgba(234,179,8,0.08)', border: 'rgba(234,179,8,0.22)',
  },
}

const SEVERITY_ORDER: Record<string, number> = { high: 0, medium: 1, low: 2 }
const CATEGORY_LABELS: Record<string, string> = {
  field_data: 'Field Data', financials: 'Financials', safety: 'Safety',
  maintenance: 'Maintenance', compliance: 'Compliance', communications: 'Communications', unknown: 'Unknown',
}

function displayName(r: IntelRecord): string {
  return r.logical_name || r.original_filename || 'Unnamed file'
}
function parseRiskFlags(raw: unknown): string[] {
  if (!raw) return []
  if (Array.isArray(raw)) return raw.map(x => (typeof x === 'string' ? x : (x as any)?.type || (x as any)?.flag || String(x))).filter(Boolean)
  if (typeof raw === 'string') { try { return parseRiskFlags(JSON.parse(raw)) } catch { return [] } }
  return []
}
type IssueRow = { record: IntelRecord; flags: string[]; highestSeverity: 'high' | 'medium' | 'low' }

/* ─── Sub-components ─────────────────────────────────────────────────────── */
function SeverityDot({ severity }: { severity: string }) {
  const color = severity === 'critical' || severity === 'high' ? '#f87171' : severity === 'medium' ? '#fbbf24' : '#94a3b8'
  return <span style={{ display: 'inline-block', width: 8, height: 8, borderRadius: '50%', background: color, flexShrink: 0, boxShadow: `0 0 6px ${color}60` }} />
}

function SkeletonIssue() {
  return (
    <div style={{ padding: '18px 20px', borderRadius: 12, border: '1px solid var(--border)', background: 'var(--bg-card)', display: 'flex', flexDirection: 'column', gap: 10 }}>
      {[55, 30, 80, 45].map((w, i) => (
        <div key={i} style={{ height: i === 0 ? 14 : 11, width: `${w}%`, borderRadius: 5, background: 'rgba(255,255,255,0.05)', animation: 'shimmer 1.6s ease-in-out infinite' }} />
      ))}
    </div>
  )
}

/* ═══════════════════════════════════════════════════════════════════════════
   IssuesPage
   ═══════════════════════════════════════════════════════════════════════════ */
export default function IssuesPage() {
  const router = useRouter()
  const [operationalIssues, setOperationalIssues] = useState<OperationalIssue[]>([])
  const [intelRecords, setIntelRecords] = useState<IntelRecord[]>([])
  const [useNewIssues, setUseNewIssues] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [activeFilter, setActiveFilter] = useState<string>('all')

  /* ── Auth + load ── */
  useEffect(() => {
    let alive = true
    supabase.auth.getSession().then(({ data }) => {
      if (!alive) return
      if (!data.session) { router.replace('/login'); return }
      loadData(alive)
    })
    return () => { alive = false }
  }, [router])

  const loadData = useCallback(async (alive = true) => {
    setLoading(true)
    setError('')
    try {
      const headers = await getAuthHeader()

      // Try new operational_issues endpoint first
      try {
        const issuesRes = await fetch(`${API_URL}/issues?limit=50`, { headers })
        if (issuesRes.status === 401) { router.replace('/login'); return }
        if (issuesRes.ok) {
          const data = await issuesRes.json().catch(() => null)
          if (Array.isArray(data) && data.length > 0) {
            if (alive) { setOperationalIssues(data); setUseNewIssues(true); setLoading(false) }
            return
          }
        }
      } catch {
        // fall through to legacy
      }

      // Fallback: legacy intel records from file_intelligence
      const [reviewRes, allRes] = await Promise.all([
        fetch(`${API_URL}/files/intelligence?needs_review=true`, { headers }),
        fetch(`${API_URL}/files/intelligence`, { headers }),
      ])
      if (reviewRes.status === 401) { router.replace('/login'); return }
      const reviewData: IntelRecord[] = await reviewRes.json().catch(() => [])
      const allData: IntelRecord[] = await allRes.json().catch(() => [])
      const seen = new Set<string>()
      const merged: IntelRecord[] = []
      for (const r of [...reviewData, ...allData]) {
        if (seen.has(r.id)) continue
        const flags = parseRiskFlags(r.risk_flags)
        if (r.needs_review || flags.length > 0) { seen.add(r.id); merged.push(r) }
      }
      if (alive) { setIntelRecords(merged); setUseNewIssues(false) }
    } catch (err: any) {
      if (alive) setError(err?.message || 'Failed to load issues')
    } finally {
      if (alive) setLoading(false)
    }
  }, [router])

  /* ── Status action ── */
  async function updateIssueStatus(issueId: string, newStatus: string) {
    try {
      const headers = await getAuthHeader()
      const res = await fetch(`${API_URL}/issues/${issueId}/status`, {
        method: 'POST',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: newStatus }),
      })
      if (res.ok) {
        setOperationalIssues(prev => prev.map(i => i.id === issueId ? { ...i, status: newStatus as any } : i))
      }
    } catch {}
  }

  /* ── Operational issues derived state ── */
  const opCounts = useMemo(() => ({
    total: operationalIssues.length,
    critical: operationalIssues.filter(i => i.severity === 'critical').length,
    high: operationalIssues.filter(i => i.severity === 'high').length,
    medium: operationalIssues.filter(i => i.severity === 'medium').length,
    candidate: operationalIssues.filter(i => i.status === 'candidate').length,
  }), [operationalIssues])

  const filteredOpIssues = useMemo(() => {
    if (activeFilter === 'all') return operationalIssues
    const statusValues = ['candidate', 'confirmed', 'dismissed', 'resolved', 'ignored']
    if (statusValues.includes(activeFilter)) return operationalIssues.filter(i => i.status === activeFilter)
    return operationalIssues.filter(i => i.severity === activeFilter)
  }, [operationalIssues, activeFilter])

  /* ── Legacy intel derived state ── */
  const issueRows = useMemo((): IssueRow[] => {
    return intelRecords.map(r => {
      const flags = parseRiskFlags(r.risk_flags)
      if (r.needs_review && !flags.includes('needs_review')) flags.push('needs_review')
      let highestSeverity: 'high' | 'medium' | 'low' = 'low'
      for (const f of flags) {
        const s = RISK_CONFIG[f]?.severity || 'low'
        if (SEVERITY_ORDER[s] < SEVERITY_ORDER[highestSeverity]) highestSeverity = s
      }
      return { record: r, flags, highestSeverity }
    }).sort((a, b) => SEVERITY_ORDER[a.highestSeverity] - SEVERITY_ORDER[b.highestSeverity])
  }, [intelRecords])

  const counts = useMemo(() => {
    const byFlag: Record<string, number> = {}
    let high = 0, medium = 0, low = 0, review = 0
    for (const row of issueRows) {
      if (row.record.needs_review) review++
      if (row.highestSeverity === 'high') high++
      else if (row.highestSeverity === 'medium') medium++
      else low++
      for (const f of row.flags) byFlag[f] = (byFlag[f] || 0) + 1
    }
    return { high, medium, low, review, total: issueRows.length, byFlag }
  }, [issueRows])

  const filtered = useMemo(() => {
    if (activeFilter === 'all') return issueRows
    if (activeFilter === 'needs_review') return issueRows.filter(r => r.record.needs_review)
    return issueRows.filter(r => r.flags.includes(activeFilter))
  }, [issueRows, activeFilter])

  const headerCount = useNewIssues ? opCounts.total : counts.total
  const headerHasHigh = useNewIssues ? (opCounts.critical + opCounts.high) > 0 : counts.high > 0

  function statusBadgeStyle(status: string): React.CSSProperties {
    if (status === 'candidate') return { background: 'rgba(251,191,36,0.12)', color: '#fbbf24', border: '1px solid rgba(251,191,36,0.25)' }
    if (status === 'confirmed') return { background: 'rgba(134,239,172,0.10)', color: '#86efac', border: '1px solid rgba(134,239,172,0.22)' }
    if (status === 'resolved')  return { background: 'rgba(96,165,250,0.10)',  color: '#60a5fa', border: '1px solid rgba(96,165,250,0.22)' }
    return { background: 'rgba(100,116,139,0.10)', color: '#94a3b8', border: '1px solid rgba(100,116,139,0.22)' }
  }

  function severityBorder(sev: string) {
    if (sev === 'critical' || sev === 'high') return 'rgba(248,113,113,0.22)'
    if (sev === 'medium') return 'rgba(251,191,36,0.22)'
    return 'rgba(100,116,139,0.22)'
  }

  function fmtDate(iso: string) {
    try { return new Date(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' }) } catch { return iso }
  }

  /* ── Render ── */
  return (
    <>
      <style>{`
        @keyframes shimmer { 0%,100%{opacity:.5} 50%{opacity:.9} }
        @keyframes fade-in-up { from{opacity:0;transform:translateY(8px)} to{opacity:1;transform:translateY(0)} }
        .iss-nav-link { cursor:pointer; border:none; background:transparent; transition:all .15s ease; }
        .iss-nav-link:hover { color: var(--fg) !important; }
        .iss-card { transition: border-color .15s ease; }
        .iss-card:hover { border-color: rgba(249,115,22,0.25) !important; }
        .iss-filter-btn { cursor:pointer; border:none; transition:all .15s ease; }
        .iss-filter-btn:hover { border-color: rgba(255,255,255,0.15) !important; }
        .iss-action-btn { cursor:pointer; border:none; transition:all .15s ease; font-size:11px; font-weight:600; padding:4px 10px; border-radius:6px; }
        .iss-action-btn:hover { opacity:.85; transform:translateY(-1px); }
      `}</style>

      <main style={{ minHeight: '100dvh', background: 'var(--bg)', color: 'var(--fg)', fontFamily: 'var(--font-sans,"IBM Plex Sans",sans-serif)' }}>

        {/* ── Header ── */}
        <header style={{ position: 'sticky', top: 0, zIndex: 30, borderBottom: '1px solid var(--border)', background: 'rgba(6,9,16,0.88)', backdropFilter: 'blur(16px)', WebkitBackdropFilter: 'blur(16px)' }}>
          <div style={{ maxWidth: 1280, margin: '0 auto', padding: '0 20px', display: 'flex', alignItems: 'center', justifyContent: 'space-between', height: 60 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <div style={{ width: 36, height: 36, borderRadius: 10, flexShrink: 0, background: 'linear-gradient(135deg, #f97316 0%, #c2410c 100%)', display: 'flex', alignItems: 'center', justifyContent: 'center', boxShadow: '0 2px 12px rgba(249,115,22,0.30)' }}>
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
                const active = item.href === '/dashboard/issues'
                return (
                  <button key={item.href} className="iss-nav-link" onClick={() => router.push(item.href)}
                    style={{ padding: '5px 12px', borderRadius: 7, fontSize: 13, fontWeight: active ? 600 : 400, color: active ? '#fdba74' : 'var(--fg-muted)', background: active ? 'rgba(249,115,22,0.08)' : 'transparent', border: `1px solid ${active ? 'rgba(249,115,22,0.2)' : 'transparent'}` }}>
                    {item.label}
                    {active && headerCount > 0 && (
                      <span style={{ marginLeft: 5, padding: '1px 5px', borderRadius: 999, fontSize: 10, fontWeight: 700, background: headerHasHigh ? 'rgba(248,113,113,0.2)' : 'rgba(251,191,36,0.2)', color: headerHasHigh ? '#f87171' : '#fbbf24' }}>
                        {headerCount}
                      </span>
                    )}
                  </button>
                )
              })}
              <div style={{ width: 1, height: 20, background: 'var(--border)', margin: '0 6px' }} />
              <button
                onClick={() => router.push('/chat')}
                style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '6px 14px', borderRadius: 8, fontSize: 13, fontWeight: 600, color: '#fff', background: '#f97316', border: 'none', cursor: 'pointer', boxShadow: '0 2px 10px rgba(249,115,22,0.28)', transition: 'all .15s ease' }}
                onMouseEnter={e => { e.currentTarget.style.background = '#fb923c'; e.currentTarget.style.transform = 'translateY(-1px)' }}
                onMouseLeave={e => { e.currentTarget.style.background = '#f97316'; e.currentTarget.style.transform = 'translateY(0)' }}
              >Ask PatchBrain</button>
            </nav>
          </div>
        </header>

        <div style={{ maxWidth: 1280, margin: '0 auto', padding: '28px 20px', animation: 'fade-in-up 0.4s ease both' }}>

          {/* ── Title ── */}
          <div style={{ marginBottom: 24 }}>
            <h1 style={{ margin: 0, fontSize: 20, fontWeight: 600, color: 'var(--fg)' }}>
              Risk &amp; Issues
              {!loading && headerCount > 0 && (
                <span style={{ marginLeft: 10, padding: '2px 8px', borderRadius: 6, fontSize: 13, fontWeight: 600, verticalAlign: 'middle', background: headerHasHigh ? 'rgba(248,113,113,0.10)' : 'rgba(251,191,36,0.10)', color: headerHasHigh ? '#f87171' : '#fbbf24', border: `1px solid ${headerHasHigh ? 'rgba(248,113,113,0.22)' : 'rgba(251,191,36,0.22)'}` }}>
                  {headerCount} issue{headerCount !== 1 ? 's' : ''}
                </span>
              )}
            </h1>
            <p style={{ margin: '4px 0 0', fontSize: 13, color: 'var(--fg-muted)' }}>
              {useNewIssues ? 'Operational issues detected by PatchBrain intelligence rules' : 'Files with risk flags or needing manual review'}
            </p>
          </div>

          {/* ── Error ── */}
          {error && (
            <div style={{ padding: '12px 16px', borderRadius: 10, marginBottom: 16, background: 'rgba(248,113,113,0.08)', border: '1px solid rgba(248,113,113,0.2)', color: '#fca5a5', fontSize: 13, display: 'flex', gap: 8 }}>
              <span>⚠</span><span>{error}</span>
              <button onClick={() => loadData(true)} style={{ marginLeft: 'auto', background: 'none', border: '1px solid rgba(248,113,113,0.3)', color: '#fca5a5', borderRadius: 6, padding: '3px 10px', fontSize: 12, cursor: 'pointer' }}>Retry</button>
            </div>
          )}

          {/* ══════════════════════════════════════════════════════
              OPERATIONAL ISSUES UI (new)
              ══════════════════════════════════════════════════════ */}
          {useNewIssues && (
            <>
              {/* Stats */}
              {!loading && (
                <div style={{ display: 'grid', gap: 10, gridTemplateColumns: 'repeat(auto-fill, minmax(130px, 1fr))', marginBottom: 24 }}>
                  {[
                    { label: 'Total',     value: opCounts.total,     accent: 'var(--fg)' },
                    { label: 'Critical',  value: opCounts.critical,  accent: '#f87171' },
                    { label: 'High',      value: opCounts.high,      accent: '#f87171' },
                    { label: 'Medium',    value: opCounts.medium,    accent: '#fbbf24' },
                    { label: 'Candidate', value: opCounts.candidate, accent: '#fbbf24' },
                  ].map(s => (
                    <div key={s.label} style={{ padding: '14px 16px', borderRadius: 12, border: '1px solid var(--border)', background: 'var(--bg-card)' }}>
                      <p style={{ margin: 0, fontSize: 10, fontWeight: 600, color: 'var(--fg-subtle)', textTransform: 'uppercase', letterSpacing: '0.1em' }}>{s.label}</p>
                      <p style={{ margin: '6px 0 0', fontSize: 26, fontWeight: 700, color: s.value > 0 ? s.accent : 'var(--fg-subtle)', lineHeight: 1 }}>{s.value}</p>
                    </div>
                  ))}
                </div>
              )}

              {/* Filter pills */}
              {!loading && operationalIssues.length > 0 && (
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 20 }}>
                  {[
                    { key: 'all',       label: `All (${opCounts.total})` },
                    { key: 'candidate', label: 'Candidate' },
                    { key: 'confirmed', label: 'Confirmed' },
                    { key: 'dismissed', label: 'Dismissed' },
                    { key: 'resolved',  label: 'Resolved' },
                    { key: 'ignored',   label: 'Ignored' },
                    { key: 'critical',  label: 'Critical' },
                    { key: 'high',      label: 'High' },
                    { key: 'medium',    label: 'Medium' },
                  ].map(({ key, label }) => {
                    const active = activeFilter === key
                    return (
                      <button key={key} className="iss-filter-btn" onClick={() => setActiveFilter(key)}
                        style={{ padding: '5px 12px', borderRadius: 8, fontSize: 12, fontWeight: active ? 600 : 400, background: active ? 'rgba(249,115,22,0.10)' : 'var(--bg-card)', color: active ? '#fdba74' : 'var(--fg-muted)', border: `1px solid ${active ? 'rgba(249,115,22,0.28)' : 'var(--border)'}` }}>
                        {label}
                      </button>
                    )
                  })}
                </div>
              )}

              {/* All clear */}
              {!loading && !error && operationalIssues.length === 0 && (
                <div style={{ padding: '48px 24px', textAlign: 'center', borderRadius: 16, border: '1px dashed var(--border)', background: 'var(--bg-card)' }}>
                  <p style={{ fontSize: 28, margin: '0 0 8px' }}>✓</p>
                  <p style={{ fontSize: 15, fontWeight: 500, color: '#86efac', margin: 0 }}>No issues found</p>
                  <p style={{ fontSize: 13, color: 'var(--fg-subtle)', marginTop: 6 }}>No operational issues detected.</p>
                </div>
              )}

              {/* Issue cards */}
              <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                {loading
                  ? Array.from({ length: 4 }).map((_, i) => <SkeletonIssue key={i} />)
                  : filteredOpIssues.map(issue => (
                    <div key={issue.id} className="iss-card"
                      style={{ padding: '18px 20px', borderRadius: 12, border: `1px solid ${severityBorder(issue.severity)}`, background: 'var(--bg-card)', display: 'flex', flexDirection: 'column', gap: 10 }}>

                      {/* Title row */}
                      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 12 }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flex: 1, minWidth: 0 }}>
                          <SeverityDot severity={issue.severity} />
                          <p style={{ margin: 0, fontSize: 13, fontWeight: 600, color: 'var(--fg)', wordBreak: 'break-word' }}>{issue.title}</p>
                        </div>
                        <div style={{ display: 'flex', gap: 4, flexShrink: 0, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
                          <span style={{ padding: '2px 7px', borderRadius: 4, fontSize: 10, fontWeight: 600, letterSpacing: '0.05em', textTransform: 'uppercase', background: 'rgba(100,116,139,0.12)', color: '#94a3b8', border: '1px solid rgba(100,116,139,0.22)' }}>
                            {issue.rule_key}
                          </span>
                          {issue.compliance_framework && (
                            <span style={{ padding: '2px 7px', borderRadius: 4, fontSize: 10, fontWeight: 600, letterSpacing: '0.05em', textTransform: 'uppercase', background: 'rgba(96,165,250,0.10)', color: '#60a5fa', border: '1px solid rgba(96,165,250,0.22)' }}>
                              {issue.compliance_framework}
                            </span>
                          )}
                          <span style={{ padding: '2px 7px', borderRadius: 4, fontSize: 10, fontWeight: 600, letterSpacing: '0.05em', textTransform: 'uppercase', ...statusBadgeStyle(issue.status) }}>
                            {issue.status}
                          </span>
                        </div>
                      </div>

                      {/* Description */}
                      {issue.description && (
                        <p style={{ margin: 0, fontSize: 12, color: 'var(--fg-muted)', lineHeight: 1.6 }}>{issue.description}</p>
                      )}

                      {/* Meta */}
                      <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', fontSize: 11, color: 'var(--fg-subtle)' }}>
                        <span>Confidence: {Math.round(issue.confidence_score * 100)}%</span>
                        <span>{fmtDate(issue.created_at)}</span>
                      </div>

                      {/* Related files */}
                      {Array.isArray(issue.related_files) && issue.related_files.length > 0 && (
                        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                          {issue.related_files.map((f: any, idx: number) => {
                            const name = typeof f === 'string' ? f : (f?.filename || f?.name || String(f))
                            return (
                              <span key={idx} style={{ padding: '2px 8px', borderRadius: 4, fontSize: 10, background: 'rgba(100,116,139,0.10)', color: 'var(--fg-muted)', border: '1px solid var(--border)' }}>
                                {name}
                              </span>
                            )
                          })}
                        </div>
                      )}

                      {/* Actions */}
                      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center' }}>
                        {issue.status === 'candidate' && <>
                          <button className="iss-action-btn" onClick={() => updateIssueStatus(issue.id, 'confirmed')} style={{ background: 'rgba(134,239,172,0.10)', color: '#86efac', border: '1px solid rgba(134,239,172,0.22)' }}>Confirm</button>
                          <button className="iss-action-btn" onClick={() => updateIssueStatus(issue.id, 'dismissed')} style={{ background: 'rgba(100,116,139,0.10)', color: '#94a3b8', border: '1px solid rgba(100,116,139,0.22)' }}>Dismiss</button>
                          <button className="iss-action-btn" onClick={() => updateIssueStatus(issue.id, 'ignored')} style={{ background: 'rgba(100,116,139,0.08)', color: '#64748b', border: '1px solid rgba(100,116,139,0.18)' }}>Ignore</button>
                        </>}
                        {issue.status === 'confirmed' && <>
                          <button className="iss-action-btn" onClick={() => updateIssueStatus(issue.id, 'resolved')} style={{ background: 'rgba(96,165,250,0.10)', color: '#60a5fa', border: '1px solid rgba(96,165,250,0.22)' }}>Resolve</button>
                          <button className="iss-action-btn" onClick={() => updateIssueStatus(issue.id, 'dismissed')} style={{ background: 'rgba(100,116,139,0.10)', color: '#94a3b8', border: '1px solid rgba(100,116,139,0.22)' }}>Dismiss</button>
                        </>}
                        {(issue.status === 'resolved' || issue.status === 'dismissed' || issue.status === 'ignored') && (
                          <button className="iss-action-btn" onClick={() => updateIssueStatus(issue.id, 'candidate')} style={{ background: 'rgba(251,191,36,0.10)', color: '#fbbf24', border: '1px solid rgba(251,191,36,0.22)' }}>Reopen</button>
                        )}
                        <button
                          onClick={() => router.push(`/chat?prompt=${encodeURIComponent('Explain this operational issue and what evidence supports it: ' + issue.title)}`)}
                          style={{ padding: '4px 12px', borderRadius: 7, fontSize: 12, background: 'rgba(249,115,22,0.07)', color: '#fdba74', border: '1px solid rgba(249,115,22,0.16)', cursor: 'pointer', transition: 'background .12s', fontWeight: 500, marginLeft: 'auto' }}
                          onMouseEnter={e => { e.currentTarget.style.background = 'rgba(249,115,22,0.13)' }}
                          onMouseLeave={e => { e.currentTarget.style.background = 'rgba(249,115,22,0.07)' }}
                        >Ask PatchBrain →</button>
                      </div>
                    </div>
                  ))}
              </div>
            </>
          )}

          {/* ══════════════════════════════════════════════════════
              LEGACY INTEL FALLBACK UI
              ══════════════════════════════════════════════════════ */}
          {!useNewIssues && (
            <>
              {!loading && (
                <div style={{ display: 'grid', gap: 10, gridTemplateColumns: 'repeat(auto-fill, minmax(130px, 1fr))', marginBottom: 24 }}>
                  {[
                    { label: 'Total Issues',    value: counts.total,  accent: 'var(--fg)' },
                    { label: 'High Severity',   value: counts.high,   accent: '#f87171' },
                    { label: 'Medium Severity', value: counts.medium, accent: '#fbbf24' },
                    { label: 'Needs Review',    value: counts.review, accent: '#fde047' },
                  ].map(s => (
                    <div key={s.label} style={{ padding: '14px 16px', borderRadius: 12, border: '1px solid var(--border)', background: 'var(--bg-card)' }}>
                      <p style={{ margin: 0, fontSize: 10, fontWeight: 600, color: 'var(--fg-subtle)', textTransform: 'uppercase', letterSpacing: '0.1em' }}>{s.label}</p>
                      <p style={{ margin: '6px 0 0', fontSize: 26, fontWeight: 700, color: s.value > 0 ? s.accent : 'var(--fg-subtle)', lineHeight: 1 }}>{s.value}</p>
                    </div>
                  ))}
                </div>
              )}

              {!loading && issueRows.length > 0 && (
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 20 }}>
                  <button className="iss-filter-btn" onClick={() => setActiveFilter('all')}
                    style={{ padding: '5px 12px', borderRadius: 8, fontSize: 12, fontWeight: activeFilter === 'all' ? 600 : 400, background: activeFilter === 'all' ? 'rgba(249,115,22,0.10)' : 'var(--bg-card)', color: activeFilter === 'all' ? '#fdba74' : 'var(--fg-muted)', border: `1px solid ${activeFilter === 'all' ? 'rgba(249,115,22,0.28)' : 'var(--border)'}` }}>
                    All ({counts.total})
                  </button>
                  {Object.entries(counts.byFlag)
                    .sort(([a], [b]) => (SEVERITY_ORDER[RISK_CONFIG[a]?.severity || 'low'] - SEVERITY_ORDER[RISK_CONFIG[b]?.severity || 'low']))
                    .map(([flag, count]) => {
                      const cfg = RISK_CONFIG[flag]
                      const active = activeFilter === flag
                      return (
                        <button key={flag} className="iss-filter-btn" onClick={() => setActiveFilter(flag)}
                          style={{ padding: '5px 12px', borderRadius: 8, fontSize: 12, fontWeight: active ? 600 : 400, background: active ? (cfg?.bg || 'rgba(100,116,139,0.10)') : 'var(--bg-card)', color: active ? (cfg?.color || '#94a3b8') : 'var(--fg-muted)', border: `1px solid ${active ? (cfg?.border || 'rgba(100,116,139,0.22)') : 'var(--border)'}` }}>
                          {cfg?.label || flag} ({count})
                        </button>
                      )
                    })}
                </div>
              )}

              {!loading && !error && issueRows.length === 0 && (
                <div style={{ padding: '48px 24px', textAlign: 'center', borderRadius: 16, border: '1px dashed var(--border)', background: 'var(--bg-card)' }}>
                  <p style={{ fontSize: 28, margin: '0 0 8px' }}>✓</p>
                  <p style={{ fontSize: 15, fontWeight: 500, color: '#86efac', margin: 0 }}>No issues found</p>
                  <p style={{ fontSize: 13, color: 'var(--fg-subtle)', marginTop: 6 }}>All processed files passed the intelligence check.</p>
                </div>
              )}

              <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                {loading
                  ? Array.from({ length: 4 }).map((_, i) => <SkeletonIssue key={i} />)
                  : filtered.map(({ record: r, flags, highestSeverity }) => {
                    const topFlag = flags[0]
                    const cfg = RISK_CONFIG[topFlag] || RISK_CONFIG.needs_review
                    return (
                      <div key={r.id} className="iss-card"
                        style={{ padding: '18px 20px', borderRadius: 12, border: `1px solid ${cfg.border}`, background: 'var(--bg-card)', display: 'flex', flexDirection: 'column', gap: 10 }}>
                        <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 12 }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                            <SeverityDot severity={highestSeverity} />
                            <p style={{ margin: 0, fontSize: 13, fontWeight: 600, color: 'var(--fg)' }}>{displayName(r)}</p>
                          </div>
                          <div style={{ display: 'flex', gap: 4, flexShrink: 0, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
                            {flags.slice(0, 3).map((f, i) => {
                              const fc = RISK_CONFIG[f]
                              return <span key={i} style={{ padding: '2px 7px', borderRadius: 4, fontSize: 10, fontWeight: 600, letterSpacing: '0.05em', textTransform: 'uppercase', background: fc?.bg || 'rgba(100,116,139,0.10)', color: fc?.color || '#94a3b8', border: `1px solid ${fc?.border || 'rgba(100,116,139,0.22)'}` }}>{fc?.label || f}</span>
                            })}
                          </div>
                        </div>
                        <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', fontSize: 11, color: 'var(--fg-subtle)' }}>
                          {r.category && <span>{CATEGORY_LABELS[r.category] || r.category}</span>}
                          {r.document_type && r.document_type !== r.category && <span style={{ opacity: 0.7 }}>· {r.document_type.replace(/_/g, ' ')}</span>}
                          {r.version_status && <span style={{ opacity: 0.7 }}>· {r.version_status}</span>}
                        </div>
                        {r.short_summary && <p style={{ margin: 0, fontSize: 12, color: 'var(--fg-muted)', lineHeight: 1.6 }}>{r.short_summary}</p>}
                        {cfg.action && (
                          <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8, padding: '8px 12px', borderRadius: 8, background: cfg.bg, border: `1px solid ${cfg.border}` }}>
                            <svg width="13" height="13" viewBox="0 0 13 13" fill="none" style={{ flexShrink: 0, marginTop: 1, color: cfg.color }}>
                              <circle cx="6.5" cy="6.5" r="5.75" stroke="currentColor" strokeWidth="1.25"/>
                              <path d="M6.5 5.5v4M6.5 3.75v.5" stroke="currentColor" strokeWidth="1.25" strokeLinecap="round"/>
                            </svg>
                            <p style={{ margin: 0, fontSize: 12, color: cfg.color, lineHeight: 1.5 }}>{cfg.action}</p>
                          </div>
                        )}
                        <button
                          onClick={() => {
                            const name = displayName(r)
                            const flag = flags[0]
                            const prompts: Record<string, string> = {
                              expired_cert:   `Explain the expired certification issue in ${name} and what should be checked next.`,
                              missing_po:     `Explain the missing PO issue in ${name} and what source files mention it.`,
                              spill_incident: `Explain the spill incident in ${name} and what follow-up is needed.`,
                              unknown_category: `Review why ${name} needs classification review.`,
                              failed_parser:  `Why did ${name} fail processing and what should I do next?`,
                              needs_review:   `What issues were flagged in ${name} and what should I review?`,
                            }
                            const prompt = prompts[flag] || `Summarize the issues flagged in ${name} and what action is needed.`
                            router.push(`/chat?prompt=${encodeURIComponent(prompt)}`)
                          }}
                          style={{ alignSelf: 'flex-start', padding: '5px 12px', borderRadius: 7, fontSize: 12, background: 'rgba(249,115,22,0.07)', color: '#fdba74', border: '1px solid rgba(249,115,22,0.16)', cursor: 'pointer', transition: 'background .12s' }}
                          onMouseEnter={e => { e.currentTarget.style.background = 'rgba(249,115,22,0.13)' }}
                          onMouseLeave={e => { e.currentTarget.style.background = 'rgba(249,115,22,0.07)' }}
                        >Ask about this issue →</button>
                      </div>
                    )
                  })}
              </div>
            </>
          )}

        </div>
      </main>
    </>
  )
}
