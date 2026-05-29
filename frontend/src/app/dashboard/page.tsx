'use client'

import { useEffect, useMemo, useRef, useState, useCallback } from 'react'
import { useRouter } from 'next/navigation'
import { API_URL, getAuthHeader, supabase } from '@/lib/supabase'

/* ─── Types ──────────────────────────────────────────────────────────────── */
type FileStatus =
  | 'uploaded' | 'queued' | 'parsing' | 'parsed'
  | 'chunking' | 'embedding' | 'ready' | 'failed'
  | 'needs_review' | string

type FileVersion = {
  id?: string | null
  status?: FileStatus | null
  original_filename?: string | null
  size_bytes?: number | null
  created_at?: string | null
  error_message?: string | null
}

type FileRecord = {
  id: string
  logical_name?: string | null
  created_at?: string | null
  file_versions?: FileVersion[] | null
}

type UploadItem = {
  id: number
  name: string
  status: 'waiting' | 'uploading' | 'queued' | 'failed'
  progress?: number
  error?: string
}

/* ─── Constants ──────────────────────────────────────────────────────────── */
const ACCEPTED_TYPES = '.pdf,.xlsx,.xls,.docx,.txt,.csv'
const MAX_FILES = 20
const REFRESH_INTERVAL_MS = 5_000

const STATUS_CONFIG: Record<string, { bg: string; text: string; ring: string; dot: string; label: string }> = {
  uploaded:     { bg: 'rgba(100,116,139,0.15)', text: '#94a3b8', ring: 'rgba(100,116,139,0.25)', dot: '#64748b', label: 'Uploaded' },
  queued:       { bg: 'rgba(59,130,246,0.12)',  text: '#93c5fd', ring: 'rgba(59,130,246,0.20)', dot: '#3b82f6', label: 'Queued' },
  parsing:      { bg: 'rgba(249,115,22,0.12)',  text: '#fdba74', ring: 'rgba(249,115,22,0.22)', dot: '#f97316', label: 'Reading…' },
  parsed:       { bg: 'rgba(249,115,22,0.12)',  text: '#fdba74', ring: 'rgba(249,115,22,0.22)', dot: '#f97316', label: 'Parsed' },
  chunking:     { bg: 'rgba(249,115,22,0.12)',  text: '#fdba74', ring: 'rgba(249,115,22,0.22)', dot: '#f97316', label: 'Preparing…' },
  embedding:    { bg: 'rgba(139,92,246,0.12)',  text: '#c4b5fd', ring: 'rgba(139,92,246,0.22)', dot: '#8b5cf6', label: 'Indexing…' },
  ready:        { bg: 'rgba(34,197,94,0.10)',   text: '#86efac', ring: 'rgba(34,197,94,0.20)',  dot: '#22c55e', label: 'Ready' },
  failed:       { bg: 'rgba(239,68,68,0.10)',   text: '#fca5a5', ring: 'rgba(239,68,68,0.20)',  dot: '#ef4444', label: 'Failed' },
  needs_review: { bg: 'rgba(234,179,8,0.10)',   text: '#fde047', ring: 'rgba(234,179,8,0.20)',  dot: '#eab308', label: 'Needs review' },
}

const PROCESSING_STATUSES = new Set(['uploaded', 'queued', 'parsing', 'parsed', 'chunking', 'embedding'])

/* ─── Helpers ────────────────────────────────────────────────────────────── */
function formatBytes(bytes?: number | null): string {
  const n = Number(bytes || 0)
  if (!n) return '—'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

function formatDate(value?: string | null): string {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
}

function currentVersion(file: FileRecord): FileVersion | null {
  const versions = (file.file_versions || []).filter(v => v?.id)
  return versions[0] || null
}

function fileName(file: FileRecord): string {
  return file.logical_name || currentVersion(file)?.original_filename || 'Unnamed file'
}

/* ─── Sub-components ─────────────────────────────────────────────────────── */
function StatusPill({ status }: { status?: FileStatus | null }) {
  const s = String(status || 'unknown')
  const cfg = STATUS_CONFIG[s]
  const isProcessing = PROCESSING_STATUSES.has(s)

  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: '6px',
        padding: '3px 10px',
        borderRadius: '999px',
        fontSize: '11px',
        fontWeight: 500,
        letterSpacing: '0.02em',
        background: cfg?.bg || 'rgba(100,116,139,0.15)',
        color: cfg?.text || '#94a3b8',
        border: `1px solid ${cfg?.ring || 'rgba(100,116,139,0.25)'}`,
        whiteSpace: 'nowrap',
      }}
    >
      <span
        style={{
          width: 6,
          height: 6,
          borderRadius: '50%',
          background: cfg?.dot || '#64748b',
          flexShrink: 0,
          animation: isProcessing ? 'dot-pulse 1.4s ease-in-out infinite' : 'none',
        }}
        aria-hidden="true"
      />
      {cfg?.label || s}
    </span>
  )
}

function SkeletonRow() {
  return (
    <tr>
      {[12, 50, 15, 10, 14, 12].map((w, i) => (
        <td key={i} style={{ padding: '14px 12px' }}>
          <div
            style={{
              height: 14,
              width: `${w}%`,
              minWidth: 40,
              borderRadius: 6,
              background: 'rgba(255,255,255,0.05)',
              animation: 'shimmer 1.6s ease-in-out infinite',
            }}
          />
        </td>
      ))}
    </tr>
  )
}

function UploadIcon() {
  return (
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M12 16V8M12 8L9 11M12 8L15 11" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round"/>
      <path d="M20.39 18.39A5 5 0 0018 9h-1.26A8 8 0 103 16.3" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round"/>
    </svg>
  )
}

function CheckboxIcon({ checked, indeterminate }: { checked: boolean; indeterminate?: boolean }) {
  return (
    <span
      aria-hidden="true"
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        width: 16,
        height: 16,
        borderRadius: 4,
        border: checked || indeterminate ? '2px solid #f97316' : '2px solid rgba(255,255,255,0.2)',
        background: checked || indeterminate ? '#f97316' : 'transparent',
        transition: 'all 0.15s ease',
        flexShrink: 0,
      }}
    >
      {checked && (
        <svg width="10" height="10" viewBox="0 0 10 10" fill="none">
          <path d="M2 5L4 7L8 3" stroke="white" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
        </svg>
      )}
      {indeterminate && !checked && (
        <svg width="10" height="10" viewBox="0 0 10 10" fill="none">
          <path d="M2.5 5H7.5" stroke="white" strokeWidth="1.5" strokeLinecap="round"/>
        </svg>
      )}
    </span>
  )
}

/* ═══════════════════════════════════════════════════════════════════════════
   DashboardPage
   ═══════════════════════════════════════════════════════════════════════════ */
export default function DashboardPage() {
  const router = useRouter()
  const fileInputRef = useRef<HTMLInputElement>(null)
  const liveRef = useRef<HTMLDivElement>(null)

  const [files, setFiles] = useState<FileRecord[]>([])
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [uploadQueue, setUploadQueue] = useState<UploadItem[]>([])
  const [isDragging, setIsDragging] = useState(false)
  const [dragCounter, setDragCounter] = useState(0) // Fix: counter prevents flicker on child drag
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  const [deleting, setDeleting] = useState(false)
  const [downloading, setDownloading] = useState(false)
  const [sortBy, setSortBy] = useState<'date' | 'name' | 'status' | 'size'>('date')
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc')
  const [searchQuery, setSearchQuery] = useState('')
  const [companyName, setCompanyName] = useState('PatchBrain')

  const isUploading = uploadQueue.some(i => i.status === 'waiting' || i.status === 'uploading')

  /* ── Stats ── */
  const stats = useMemo(() => {
    let ready = 0, processing = 0, failed = 0, review = 0
    for (const file of files) {
      const s = String(currentVersion(file)?.status || '')
      if (s === 'ready') ready++
      else if (s === 'failed') failed++
      else if (s === 'needs_review') review++
      else if (PROCESSING_STATUSES.has(s)) processing++
    }
    return { total: files.length, ready, processing, failed, review }
  }, [files])

  /* ── Filtered + sorted files ── */
  const displayedFiles = useMemo(() => {
    let result = [...files]

    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase()
      result = result.filter(f =>
        fileName(f).toLowerCase().includes(q) ||
        String(currentVersion(f)?.status || '').includes(q)
      )
    }

    result.sort((a, b) => {
      let av: string | number = ''
      let bv: string | number = ''
      if (sortBy === 'name')   { av = fileName(a).toLowerCase(); bv = fileName(b).toLowerCase() }
      if (sortBy === 'status') { av = String(currentVersion(a)?.status || ''); bv = String(currentVersion(b)?.status || '') }
      if (sortBy === 'size')   { av = currentVersion(a)?.size_bytes || 0; bv = currentVersion(b)?.size_bytes || 0 }
      if (sortBy === 'date')   { av = currentVersion(a)?.created_at || a.created_at || ''; bv = currentVersion(b)?.created_at || b.created_at || '' }
      if (av < bv) return sortDir === 'asc' ? -1 : 1
      if (av > bv) return sortDir === 'asc' ? 1 : -1
      return 0
    })

    return result
  }, [files, searchQuery, sortBy, sortDir])

  const selectedCount = selectedIds.size
  const allSelected = displayedFiles.length > 0 && displayedFiles.every(f => selectedIds.has(f.id))
  const someSelected = selectedCount > 0 && !allSelected

  /* ── Auth + load ── */
  useEffect(() => {
    let alive = true

    async function init() {
      const { data } = await supabase.auth.getSession()
      if (!alive) return
      if (!data.session) { router.replace('/login'); return }

      // Load company name
      try {
        const headers = await getAuthHeader()
        const res = await fetch(`${API_URL}/me`, { headers })
        if (res.ok && alive) {
          const me = await res.json()
          setCompanyName(me.company_name || 'PatchBrain')
        }
      } catch {}

      await loadFiles(false)
    }

    init()
    const interval = window.setInterval(() => loadFiles(true), REFRESH_INTERVAL_MS)
    return () => { alive = false; window.clearInterval(interval) }
  }, [router])

  const loadFiles = useCallback(async (silent = false) => {
    if (!silent) return setLoading(true)
    setRefreshing(true)
    try {
      const headers = await getAuthHeader()
      const res = await fetch(`${API_URL}/files`, { headers })
      if (res.status === 401) { router.replace('/login'); return }
      const data = await res.json().catch(() => [])
      if (!res.ok) throw new Error(data?.detail || 'Failed to load files')
      const list: FileRecord[] = Array.isArray(data) ? data : []
      setFiles(list)
      setSelectedIds(prev => {
        const next = new Set<string>()
        for (const id of prev) if (list.some(f => f.id === id)) next.add(id)
        return next
      })
    } catch (err: any) {
      if (!silent) setError(err?.message || 'Failed to load files')
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }, [router])

  // Fix: separate useEffect so loadFiles can be in the dep array without triggering loop
  useEffect(() => { loadFiles(false) }, [loadFiles])

  /* ── Upload ── */
  async function uploadOneFile(file: File, headers: Record<string, string>, itemId: number) {
    const requestRes = await fetch(`${API_URL}/upload/request`, {
      method: 'POST',
      headers: { ...headers, 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename: file.name, mime_type: file.type || 'application/octet-stream', size_bytes: file.size }),
    })
    const requestData = await requestRes.json().catch(() => ({}))
    if (!requestRes.ok) throw new Error(requestData.detail || 'Failed to request upload URL')

    const { error: storageError } = await supabase.storage
      .from('company-files')
      .uploadToSignedUrl(requestData.storage_path, requestData.upload_token, file)

    if (storageError) throw new Error(storageError.message || 'Storage upload failed')

    const confirmRes = await fetch(`${API_URL}/upload/confirm`, {
      method: 'POST',
      headers: { ...headers, 'Content-Type': 'application/json' },
      body: JSON.stringify({ file_version_id: requestData.file_version_id }),
    })
    const confirmData = await confirmRes.json().catch(() => ({}))
    if (!confirmRes.ok) throw new Error(confirmData.detail || 'Failed to queue file')
  }

  async function handleFilesUpload(rawFiles: File[]) {
    if (!rawFiles.length || isUploading) return
    if (rawFiles.length > MAX_FILES) { setError(`Max ${MAX_FILES} files at a time.`); return }

    setError('')
    setNotice('')

    let headers: Record<string, string>
    try {
      headers = await getAuthHeader()
    } catch (err: any) {
      setError(err?.message || 'Not authenticated')
      return
    }

    const queue: UploadItem[] = rawFiles.map((file, i) => ({ id: i, name: file.name, status: 'waiting' }))
    setUploadQueue(queue)

    for (let i = 0; i < rawFiles.length; i++) {
      setUploadQueue(prev => prev.map(item => item.id === i ? { ...item, status: 'uploading' } : item))
      try {
        await uploadOneFile(rawFiles[i], headers, i)
        setUploadQueue(prev => prev.map(item => item.id === i ? { ...item, status: 'queued' } : item))
      } catch (err: any) {
        setUploadQueue(prev => prev.map(item => item.id === i ? { ...item, status: 'failed', error: err?.message } : item))
      }
    }

    await loadFiles(true)
    announce('Upload complete. Files will be searchable after processing.')
    setNotice('Upload submitted. Files will become searchable after processing.')
    window.setTimeout(() => setUploadQueue([]), 4000)
    if (fileInputRef.current) fileInputRef.current.value = ''
  }

  /* ── Announce to screen readers ── */
  function announce(msg: string) {
    if (!liveRef.current) return
    liveRef.current.textContent = ''
    window.setTimeout(() => { if (liveRef.current) liveRef.current.textContent = msg }, 50)
  }

  /* ── Selection ── */
  function toggleFileSelected(fileId: string) {
    setSelectedIds(prev => {
      const next = new Set(prev)
      if (next.has(fileId)) next.delete(fileId)
      else next.add(fileId)
      return next
    })
  }

  function toggleAllSelected() {
    if (allSelected) {
      setSelectedIds(new Set())
    } else {
      setSelectedIds(new Set(displayedFiles.map(f => f.id)))
    }
  }

  /* ── Sort ── */
  function handleSort(col: typeof sortBy) {
    if (sortBy === col) setSortDir(d => d === 'asc' ? 'desc' : 'asc')
    else { setSortBy(col); setSortDir('asc') }
  }

  /* ── Delete ── */
  async function deleteFileIds(ids: string[]) {
    if (!ids.length) return
    const ok = window.confirm(`Permanently delete ${ids.length} file${ids.length > 1 ? 's' : ''}? This removes the file, chunks, jobs, and storage object.`)
    if (!ok) return

    setDeleting(true)
    setError('')
    try {
      const headers = await getAuthHeader()
      const res = await fetch(`${API_URL}/files/delete`, {
        method: 'POST',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify({ file_ids: ids }),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.detail || 'Delete failed')
      setSelectedIds(prev => { const next = new Set(prev); ids.forEach(id => next.delete(id)); return next })
      const msg = `Deleted ${ids.length} file${ids.length > 1 ? 's' : ''}.`
      setNotice(msg)
      announce(msg)
      await loadFiles(true)
    } catch (err: any) {
      setError(err?.message || 'Delete failed')
    } finally {
      setDeleting(false)
    }
  }

  /* ── Download ── */
  async function downloadFileIds(ids: string[]) {
    if (!ids.length) return
    setDownloading(true)
    setError('')
    try {
      const headers = await getAuthHeader()
      const res = await fetch(`${API_URL}/files/download-url`, {
        method: 'POST',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify({ file_ids: ids }),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.detail || 'Failed to get download URL')
      for (const item of (data.files || [])) {
        if (!item.url) continue
        const a = document.createElement('a')
        a.href = item.url
        a.download = item.filename || 'download'
        a.rel = 'noopener noreferrer'
        a.target = '_blank'
        document.body.appendChild(a)
        a.click()
        a.remove()
      }
    } catch (err: any) {
      setError(err?.message || 'Download failed')
    } finally {
      setDownloading(false)
    }
  }

  /* ── Sign out ── */
  async function handleSignOut() {
    await supabase.auth.signOut()
    router.replace('/login')
  }

  /* ── Drag handlers (fixed counter to prevent child-element flicker) ── */
  function onDragEnter(e: React.DragEvent) {
    e.preventDefault()
    setDragCounter(c => c + 1)
    setIsDragging(true)
  }
  function onDragLeave(e: React.DragEvent) {
    e.preventDefault()
    setDragCounter(c => {
      const next = c - 1
      if (next <= 0) { setIsDragging(false); return 0 }
      return next
    })
  }
  function onDragOver(e: React.DragEvent) { e.preventDefault() }
  function onDrop(e: React.DragEvent) {
    e.preventDefault()
    setIsDragging(false)
    setDragCounter(0)
    handleFilesUpload(Array.from(e.dataTransfer.files || []))
  }

  /* ── Sort indicator ── */
  function SortIcon({ col }: { col: typeof sortBy }) {
    if (sortBy !== col) return <span style={{ opacity: 0.3, marginLeft: 4 }}>↕</span>
    return <span style={{ marginLeft: 4, color: '#f97316' }}>{sortDir === 'asc' ? '↑' : '↓'}</span>
  }

  /* ─── Render ──────────────────────────────────────────────────────────── */
  return (
    <>
      {/* Global keyframe styles */}
      <style>{`
        @keyframes dot-pulse {
          0%, 100% { opacity: 1; transform: scale(1); }
          50% { opacity: 0.4; transform: scale(0.75); }
        }
        @keyframes shimmer {
          0%, 100% { opacity: 0.5; }
          50% { opacity: 0.9; }
        }
        @keyframes fade-in-up {
          from { opacity: 0; transform: translateY(8px); }
          to { opacity: 1; transform: translateY(0); }
        }
        @keyframes slide-down {
          from { opacity: 0; transform: translateY(-6px); }
          to { opacity: 1; transform: translateY(0); }
        }
        .pb-row-hover:hover { background: rgba(249,115,22,0.03) !important; }
        .pb-row-selected { background: rgba(249,115,22,0.06) !important; }
        .pb-btn-icon {
          display: inline-flex; align-items: center; justify-content: center;
          border-radius: 8px; padding: 5px 10px; font-size: 12px; font-weight: 500;
          transition: all 0.15s ease; cursor: pointer; border: none; background: transparent;
        }
        .pb-btn-icon:hover { background: rgba(255,255,255,0.07); }
        .pb-sort-th { cursor: pointer; user-select: none; }
        .pb-sort-th:hover { color: var(--fg) !important; }
        .focus-ring:focus-visible {
          outline: 2px solid rgba(249,115,22,0.7);
          outline-offset: 2px;
          border-radius: 8px;
        }
      `}</style>

      {/* Screen reader announcements */}
      <div ref={liveRef} role="status" aria-live="polite" aria-atomic="true" className="sr-only" />

      <main
        style={{ minHeight: '100dvh', background: 'var(--bg)', color: 'var(--fg)', fontFamily: 'var(--font-sans, "IBM Plex Sans", sans-serif)' }}
        aria-label="PatchBrain file library"
      >
        {/* ── Header ── */}
        <header
          style={{
            position: 'sticky', top: 0, zIndex: 30,
            borderBottom: '1px solid var(--border)',
            background: 'rgba(6,9,16,0.88)',
            backdropFilter: 'blur(16px)',
            WebkitBackdropFilter: 'blur(16px)',
          }}
        >
          <div style={{ maxWidth: 1280, margin: '0 auto', padding: '0 20px', display: 'flex', alignItems: 'center', justifyContent: 'space-between', height: 60 }}>
            {/* Logo */}
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <div
                style={{
                  width: 36, height: 36, borderRadius: 10,
                  background: 'linear-gradient(135deg, #f97316 0%, #c2410c 100%)',
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  boxShadow: '0 2px 12px rgba(249,115,22,0.30)',
                  flexShrink: 0,
                }}
                aria-hidden="true"
              >
                <span style={{ color: '#fff', fontSize: 15, fontWeight: 800, lineHeight: 1 }}>P</span>
              </div>
              <div>
                <p style={{ fontSize: 14, fontWeight: 600, color: 'var(--fg)', lineHeight: 1.2 }}>{companyName}</p>
                <p style={{ fontSize: 11, color: 'var(--fg-subtle)', lineHeight: 1.2 }}>File Library</p>
              </div>
            </div>

            {/* Nav */}
            <nav style={{ display: 'flex', alignItems: 'center', gap: 6 }} aria-label="Main navigation">
              <button
                onClick={() => loadFiles(true)}
                className="focus-ring"
                aria-label="Refresh file list"
                style={{
                  display: 'flex', alignItems: 'center', gap: 5,
                  padding: '6px 12px', borderRadius: 8, fontSize: 13, fontWeight: 500,
                  color: 'var(--fg-muted)', background: 'transparent',
                  border: '1px solid var(--border)', cursor: 'pointer',
                  transition: 'all 0.15s ease',
                }}
                onMouseEnter={e => { e.currentTarget.style.borderColor = 'var(--border-hover)'; e.currentTarget.style.color = 'var(--fg)' }}
                onMouseLeave={e => { e.currentTarget.style.borderColor = 'var(--border)'; e.currentTarget.style.color = 'var(--fg-muted)' }}
              >
                <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true" style={{ flexShrink: 0, ...(refreshing ? { animation: 'spin 1s linear infinite' } : {}) }}>
                  <path d="M13.65 2.35A8 8 0 102 8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
                  <path d="M2 3.5V8H6.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
                </svg>
                {refreshing ? 'Refreshing…' : 'Refresh'}
              </button>
              <button
                onClick={() => router.push('/chat')}
                className="focus-ring"
                style={{
                  display: 'flex', alignItems: 'center', gap: 6,
                  padding: '6px 14px', borderRadius: 8, fontSize: 13, fontWeight: 600,
                  color: '#fff', background: '#f97316',
                  border: 'none', cursor: 'pointer',
                  boxShadow: '0 2px 10px rgba(249,115,22,0.28)',
                  transition: 'all 0.15s ease',
                }}
                onMouseEnter={e => { e.currentTarget.style.background = '#fb923c'; e.currentTarget.style.transform = 'translateY(-1px)'; e.currentTarget.style.boxShadow = '0 4px 14px rgba(249,115,22,0.38)' }}
                onMouseLeave={e => { e.currentTarget.style.background = '#f97316'; e.currentTarget.style.transform = 'translateY(0)'; e.currentTarget.style.boxShadow = '0 2px 10px rgba(249,115,22,0.28)' }}
              >
                <svg width="13" height="13" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M8 1.333A6.667 6.667 0 100 8a6.667 6.667 0 008-6.667zM8 9.333H5.333A.667.667 0 015.333 8H8V5.333a.667.667 0 011.333 0V8a1.333 1.333 0 01-1.333 1.333z"/></svg>
                Ask PatchBrain
              </button>
              <button
                onClick={handleSignOut}
                className="focus-ring"
                style={{
                  padding: '6px 10px', borderRadius: 8, fontSize: 13, fontWeight: 500,
                  color: 'var(--fg-subtle)', background: 'transparent',
                  border: 'none', cursor: 'pointer', transition: 'all 0.15s ease',
                }}
                onMouseEnter={e => { e.currentTarget.style.color = 'var(--fg-muted)'; e.currentTarget.style.background = 'rgba(255,255,255,0.05)' }}
                onMouseLeave={e => { e.currentTarget.style.color = 'var(--fg-subtle)'; e.currentTarget.style.background = 'transparent' }}
              >
                Sign out
              </button>
            </nav>
          </div>
        </header>

        <div style={{ maxWidth: 1280, margin: '0 auto', padding: '28px 20px', animation: 'fade-in-up 0.4s ease both' }}>

          {/* ── Hero + Stats ── */}
          <section
            style={{ display: 'grid', gap: 16, gridTemplateColumns: 'minmax(0,1fr)', marginBottom: 24 }}
            aria-label="Library overview"
          >
            <div style={{ display: 'grid', gap: 16, gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))' }}>
              {/* Hero card */}
              <div
                style={{
                  gridColumn: 'span 2',
                  padding: '24px 28px',
                  borderRadius: 20,
                  border: '1px solid var(--border)',
                  background: 'linear-gradient(135deg, rgba(249,115,22,0.06) 0%, rgba(15,21,32,0.9) 60%)',
                  display: 'flex', flexDirection: 'column', justifyContent: 'space-between', gap: 12,
                }}
              >
                <div>
                  <span style={{
                    display: 'inline-block', fontSize: 10, fontWeight: 600,
                    letterSpacing: '0.18em', textTransform: 'uppercase', color: '#fdba74',
                    padding: '3px 8px', borderRadius: 4,
                    background: 'rgba(249,115,22,0.1)', border: '1px solid rgba(249,115,22,0.18)',
                    marginBottom: 10,
                  }}>
                    Operations workspace
                  </span>
                  <h1 style={{ fontSize: 22, fontWeight: 600, color: 'var(--fg)', margin: 0, lineHeight: 1.3 }}>
                    Company document library
                  </h1>
                  <p style={{ marginTop: 8, fontSize: 13, lineHeight: 1.7, color: 'var(--fg-muted)', maxWidth: 520 }}>
                    Upload run tickets, invoices, compliance reports, field notes, spreadsheets. Ready files are immediately available to PatchBrain chat.
                  </p>
                </div>
              </div>

              {/* Stat cards */}
              {([
                { label: 'Total files', value: stats.total, accent: '#64748b' },
                { label: 'Ready', value: stats.ready, accent: '#22c55e' },
                { label: 'Processing', value: stats.processing, accent: '#f97316' },
                { label: 'Failed', value: stats.failed, accent: '#ef4444' },
              ] as const).map(s => (
                <div
                  key={s.label}
                  style={{
                    padding: '18px 20px', borderRadius: 16,
                    border: '1px solid var(--border)', background: 'var(--bg-card)',
                  }}
                >
                  <p style={{ fontSize: 11, fontWeight: 500, color: 'var(--fg-subtle)', textTransform: 'uppercase', letterSpacing: '0.1em' }}>
                    {s.label}
                  </p>
                  <p style={{ marginTop: 8, fontSize: 28, fontWeight: 700, color: s.value > 0 && s.label !== 'Total files' ? s.accent : 'var(--fg)', lineHeight: 1 }}>
                    {loading ? '—' : s.value}
                  </p>
                </div>
              ))}
            </div>
          </section>

          {/* ── Banners ── */}
          {(error || notice) && (
            <div
              role={error ? 'alert' : 'status'}
              aria-live="polite"
              style={{
                marginBottom: 20,
                padding: '12px 16px', borderRadius: 12, fontSize: 13,
                display: 'flex', alignItems: 'flex-start', gap: 10,
                animation: 'slide-down 0.25s ease both',
                ...(error
                  ? { border: '1px solid rgba(239,68,68,0.22)', background: 'rgba(239,68,68,0.09)', color: '#fca5a5' }
                  : { border: '1px solid rgba(34,197,94,0.20)', background: 'rgba(34,197,94,0.08)', color: '#86efac' }
                ),
              }}
            >
              <span aria-hidden="true" style={{ flexShrink: 0, fontSize: 14, marginTop: 1 }}>{error ? '⚠' : '✓'}</span>
              <span>{error || notice}</span>
              <button
                onClick={() => { setError(''); setNotice('') }}
                aria-label="Dismiss notification"
                style={{ marginLeft: 'auto', background: 'none', border: 'none', cursor: 'pointer', color: 'inherit', opacity: 0.6, padding: '0 2px' }}
              >×</button>
            </div>
          )}

          {/* ── Upload zone ── */}
          <section
            aria-label="Upload files"
            style={{
              marginBottom: 20,
              borderRadius: 20,
              border: '1px solid var(--border)',
              background: 'var(--bg-card)',
              padding: 16,
            }}
          >
            <div
              onDragEnter={onDragEnter}
              onDragLeave={onDragLeave}
              onDragOver={onDragOver}
              onDrop={onDrop}
              onClick={() => !isUploading && fileInputRef.current?.click()}
              onKeyDown={e => { if ((e.key === 'Enter' || e.key === ' ') && !isUploading) fileInputRef.current?.click() }}
              role="button"
              tabIndex={isUploading ? -1 : 0}
              aria-disabled={isUploading}
              aria-label={`Drop files here or press Enter to upload. Accepts PDF, Excel, Word, CSV, TXT. Max ${MAX_FILES} files.`}
              className="focus-ring"
              style={{
                cursor: isUploading ? 'not-allowed' : 'pointer',
                borderRadius: 14,
                border: `2px dashed ${isDragging ? '#f97316' : 'rgba(255,255,255,0.1)'}`,
                padding: '28px 20px',
                textAlign: 'center',
                transition: 'all 0.2s ease',
                background: isDragging ? 'rgba(249,115,22,0.06)' : 'transparent',
                transform: isDragging ? 'scale(1.005)' : 'scale(1)',
              }}
            >
              <input
                ref={fileInputRef}
                type="file"
                multiple
                accept={ACCEPTED_TYPES}
                tabIndex={-1}
                aria-hidden="true"
                style={{ display: 'none' }}
                onChange={e => e.target.files && handleFilesUpload(Array.from(e.target.files))}
              />
              <div
                style={{
                  margin: '0 auto 12px',
                  width: 44, height: 44, borderRadius: 12,
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  background: isDragging ? 'rgba(249,115,22,0.15)' : 'rgba(255,255,255,0.04)',
                  border: `1px solid ${isDragging ? 'rgba(249,115,22,0.3)' : 'rgba(255,255,255,0.08)'}`,
                  color: isDragging ? '#f97316' : 'var(--fg-muted)',
                  transition: 'all 0.2s ease',
                }}
              >
                <UploadIcon />
              </div>
              <p style={{ fontSize: 14, fontWeight: 500, color: 'var(--fg)', margin: '0 0 4px' }}>
                {isDragging ? 'Release to upload' : 'Drop files here or click to browse'}
              </p>
              <p style={{ fontSize: 12, color: 'var(--fg-subtle)' }}>
                PDF · Excel · Word · CSV · TXT — max {MAX_FILES} files per batch
              </p>
            </div>

            {/* Upload progress items */}
            {uploadQueue.length > 0 && (
              <div style={{ marginTop: 12, display: 'grid', gap: 8, gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))' }}>
                {uploadQueue.map(item => (
                  <div
                    key={item.id}
                    style={{
                      display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10,
                      padding: '10px 14px', borderRadius: 10,
                      border: '1px solid var(--border)', background: 'var(--bg-elevated)',
                    }}
                  >
                    <p style={{ fontSize: 12, color: 'var(--fg-muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1, margin: 0 }}>
                      {item.name}
                      {item.error && <span style={{ display: 'block', color: '#fca5a5', fontSize: 11, marginTop: 2 }}>{item.error}</span>}
                    </p>
                    <StatusPill status={item.status === 'queued' ? 'ready' : item.status === 'failed' ? 'failed' : 'parsing'} />
                  </div>
                ))}
              </div>
            )}
          </section>

          {/* ── File table ── */}
          <section
            aria-label="Uploaded files"
            style={{
              borderRadius: 20,
              border: '1px solid var(--border)',
              background: 'var(--bg-card)',
              overflow: 'hidden',
              boxShadow: '0 4px 32px rgba(0,0,0,0.3)',
            }}
          >
            {/* Table toolbar */}
            <div
              style={{
                display: 'flex', flexWrap: 'wrap', gap: 10, alignItems: 'center', justifyContent: 'space-between',
                padding: '14px 20px',
                borderBottom: '1px solid var(--border)',
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <h2 style={{ fontSize: 14, fontWeight: 600, color: 'var(--fg)', margin: 0 }}>Files</h2>
                <span style={{ fontSize: 11, color: 'var(--fg-subtle)', padding: '2px 7px', borderRadius: 20, background: 'rgba(255,255,255,0.05)' }}>
                  {displayedFiles.length}
                </span>
                {stats.processing > 0 && (
                  <span style={{ fontSize: 11, color: '#fdba74', display: 'flex', alignItems: 'center', gap: 4 }}>
                    <span style={{ width: 6, height: 6, borderRadius: '50%', background: '#f97316', display: 'inline-block', animation: 'dot-pulse 1.4s ease-in-out infinite' }} aria-hidden="true" />
                    {stats.processing} processing
                  </span>
                )}
              </div>

              <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                {/* Search */}
                <div style={{ position: 'relative' }}>
                  <input
                    type="search"
                    value={searchQuery}
                    onChange={e => setSearchQuery(e.target.value)}
                    placeholder="Search files…"
                    aria-label="Search uploaded files"
                    className="focus-ring"
                    style={{
                      padding: '6px 10px 6px 30px', borderRadius: 8, fontSize: 12,
                      border: '1px solid var(--border)', background: 'var(--bg-elevated)',
                      color: 'var(--fg)', outline: 'none', width: 180,
                    }}
                  />
                  <svg
                    width="13" height="13" viewBox="0 0 16 16" fill="none"
                    aria-hidden="true"
                    style={{ position: 'absolute', left: 9, top: '50%', transform: 'translateY(-50%)', color: 'var(--fg-subtle)' }}
                  >
                    <circle cx="6.5" cy="6.5" r="4.5" stroke="currentColor" strokeWidth="1.5"/>
                    <path d="M11 11L14 14" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
                  </svg>
                </div>

                {/* Bulk actions */}
                {selectedCount > 0 && (
                  <>
                    <button
                      onClick={() => downloadFileIds([...selectedIds])}
                      disabled={downloading}
                      className="focus-ring"
                      style={{
                        padding: '6px 12px', borderRadius: 8, fontSize: 12, fontWeight: 500,
                        border: '1px solid var(--border)', background: 'transparent', color: 'var(--fg-muted)',
                        cursor: downloading ? 'not-allowed' : 'pointer', opacity: downloading ? 0.5 : 1,
                        transition: 'all 0.15s',
                      }}
                      onMouseEnter={e => { if (!downloading) { e.currentTarget.style.borderColor = 'var(--border-hover)'; e.currentTarget.style.color = 'var(--fg)' }}}
                      onMouseLeave={e => { e.currentTarget.style.borderColor = 'var(--border)'; e.currentTarget.style.color = 'var(--fg-muted)' }}
                    >
                      {downloading ? 'Downloading…' : `↓ Download (${selectedCount})`}
                    </button>
                    <button
                      onClick={() => deleteFileIds([...selectedIds])}
                      disabled={deleting}
                      className="focus-ring"
                      style={{
                        padding: '6px 12px', borderRadius: 8, fontSize: 12, fontWeight: 500,
                        border: '1px solid rgba(239,68,68,0.25)', background: 'rgba(239,68,68,0.08)', color: '#fca5a5',
                        cursor: deleting ? 'not-allowed' : 'pointer', opacity: deleting ? 0.5 : 1,
                        transition: 'all 0.15s',
                      }}
                      onMouseEnter={e => { if (!deleting) e.currentTarget.style.background = 'rgba(239,68,68,0.14)' }}
                      onMouseLeave={e => { e.currentTarget.style.background = 'rgba(239,68,68,0.08)' }}
                    >
                      {deleting ? 'Deleting…' : `✕ Delete (${selectedCount})`}
                    </button>
                  </>
                )}
              </div>
            </div>

            {/* Table */}
            {loading ? (
              <div style={{ padding: '0 0 8px' }}>
                <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                  <tbody>
                    {[1, 2, 3, 4].map(i => <SkeletonRow key={i} />)}
                  </tbody>
                </table>
              </div>
            ) : displayedFiles.length === 0 ? (
              <div style={{ padding: '56px 20px', textAlign: 'center' }}>
                {searchQuery ? (
                  <>
                    <p style={{ fontSize: 14, fontWeight: 500, color: 'var(--fg)' }}>No files match "{searchQuery}"</p>
                    <button onClick={() => setSearchQuery('')} style={{ marginTop: 8, fontSize: 12, color: '#f97316', background: 'none', border: 'none', cursor: 'pointer' }}>
                      Clear search
                    </button>
                  </>
                ) : (
                  <>
                    <div style={{ width: 48, height: 48, borderRadius: 12, background: 'rgba(255,255,255,0.04)', border: '1px solid var(--border)', display: 'flex', alignItems: 'center', justifyContent: 'center', margin: '0 auto 14px', color: 'var(--fg-subtle)' }}>
                      <UploadIcon />
                    </div>
                    <p style={{ fontSize: 14, fontWeight: 500, color: 'var(--fg)' }}>No files yet</p>
                    <p style={{ marginTop: 4, fontSize: 13, color: 'var(--fg-subtle)' }}>
                      Upload run tickets, invoices, or CSVs to start using PatchBrain.
                    </p>
                  </>
                )}
              </div>
            ) : (
              <div style={{ overflowX: 'auto' }}>
                <table
                  style={{ width: '100%', minWidth: 680, borderCollapse: 'collapse', fontSize: 13 }}
                  aria-label="Uploaded files table"
                >
                  <thead>
                    <tr style={{ borderBottom: '1px solid var(--border)' }}>
                      <th style={{ width: 44, padding: '10px 16px 10px 20px' }}>
                        <span style={{ cursor: 'pointer' }} onClick={toggleAllSelected} aria-label={allSelected ? 'Deselect all files' : 'Select all files'} role="button" tabIndex={0} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') toggleAllSelected() }}>
                          <CheckboxIcon checked={allSelected} indeterminate={someSelected} />
                        </span>
                      </th>
                      {(['name', 'status', 'size', 'date'] as const).map(col => (
                        <th
                          key={col}
                          onClick={() => handleSort(col === 'date' ? 'date' : col)}
                          className="pb-sort-th"
                          aria-sort={sortBy === col ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}
                          style={{
                            padding: '10px 12px', textAlign: 'left', fontSize: 11, fontWeight: 600,
                            textTransform: 'uppercase', letterSpacing: '0.1em', color: 'var(--fg-subtle)',
                          }}
                        >
                          {col === 'date' ? 'Uploaded' : col.charAt(0).toUpperCase() + col.slice(1)}
                          <SortIcon col={col === 'date' ? 'date' : col} />
                        </th>
                      ))}
                      <th style={{ padding: '10px 20px 10px 12px', textAlign: 'right', fontSize: 11, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.1em', color: 'var(--fg-subtle)' }}>
                        Actions
                      </th>
                    </tr>
                  </thead>
                  <tbody style={{ borderTop: 'none' }}>
                    {displayedFiles.map((file, idx) => {
                      const version = currentVersion(file)
                      const status = version?.status || 'unknown'
                      const selected = selectedIds.has(file.id)
                      return (
                        <tr
                          key={file.id}
                          className={`pb-row-hover ${selected ? 'pb-row-selected' : ''}`}
                          style={{
                            borderBottom: idx < displayedFiles.length - 1 ? '1px solid rgba(255,255,255,0.04)' : 'none',
                            transition: 'background 0.1s ease',
                          }}
                        >
                          <td style={{ padding: '12px 16px 12px 20px' }}>
                            <span
                              onClick={() => toggleFileSelected(file.id)}
                              role="checkbox"
                              aria-checked={selected}
                              aria-label={`Select ${fileName(file)}`}
                              tabIndex={0}
                              style={{ cursor: 'pointer' }}
                              onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') toggleFileSelected(file.id) }}
                            >
                              <CheckboxIcon checked={selected} />
                            </span>
                          </td>
                          <td style={{ padding: '12px' }}>
                            <p style={{ maxWidth: 320, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontWeight: 500, color: 'var(--fg)', margin: 0 }}>
                              {fileName(file)}
                            </p>
                            {version?.error_message && (
                              <p style={{ marginTop: 3, fontSize: 11, color: '#fca5a5', maxWidth: 380, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                {version.error_message}
                              </p>
                            )}
                          </td>
                          <td style={{ padding: '12px' }}>
                            <StatusPill status={status} />
                          </td>
                          <td style={{ padding: '12px', color: 'var(--fg-muted)', whiteSpace: 'nowrap' }}>
                            {formatBytes(version?.size_bytes)}
                          </td>
                          <td style={{ padding: '12px', color: 'var(--fg-muted)', whiteSpace: 'nowrap' }}>
                            {formatDate(version?.created_at || file.created_at)}
                          </td>
                          <td style={{ padding: '12px 20px 12px 12px' }}>
                            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 4 }}>
                              <button
                                onClick={() => downloadFileIds([file.id])}
                                className="pb-btn-icon focus-ring"
                                style={{ color: 'var(--fg-muted)' }}
                                aria-label={`Download ${fileName(file)}`}
                              >
                                ↓
                              </button>
                              <button
                                onClick={() => deleteFileIds([file.id])}
                                className="pb-btn-icon focus-ring"
                                style={{ color: '#f87171' }}
                                aria-label={`Delete ${fileName(file)}`}
                                onMouseEnter={e => (e.currentTarget.style.background = 'rgba(239,68,68,0.1)')}
                                onMouseLeave={e => (e.currentTarget.style.background = 'transparent')}
                              >
                                ✕
                              </button>
                            </div>
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </div>
      </main>
    </>
  )
}
