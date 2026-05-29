'use client'

import { useEffect, useMemo, useRef, useState } from 'react'
import { useRouter } from 'next/navigation'
import { API_URL, getAuthHeader, supabase } from '@/lib/supabase'

type FileStatus = 'uploaded' | 'queued' | 'parsing' | 'parsed' | 'chunking' | 'embedding' | 'ready' | 'failed' | 'needs_review' | string

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
  error?: string
}

const STATUS_STYLES: Record<string, string> = {
  uploaded: 'bg-slate-700/70 text-slate-200 ring-slate-600/50',
  queued: 'bg-blue-500/15 text-blue-200 ring-blue-400/20',
  parsing: 'bg-amber-500/15 text-amber-200 ring-amber-400/20',
  parsed: 'bg-amber-500/15 text-amber-200 ring-amber-400/20',
  chunking: 'bg-amber-500/15 text-amber-200 ring-amber-400/20',
  embedding: 'bg-violet-500/15 text-violet-200 ring-violet-400/20',
  ready: 'bg-emerald-500/15 text-emerald-200 ring-emerald-400/20',
  failed: 'bg-red-500/15 text-red-200 ring-red-400/20',
  needs_review: 'bg-orange-500/15 text-orange-200 ring-orange-400/20',
}

const STATUS_LABELS: Record<string, string> = {
  uploaded: 'Uploaded',
  queued: 'Queued',
  parsing: 'Reading file…',
  parsed: 'Parsed',
  chunking: 'Preparing…',
  embedding: 'Indexing…',
  ready: 'Ready',
  failed: 'Failed',
  needs_review: 'Needs review',
}

const PROCESSING_STATUSES = new Set(['uploaded', 'queued', 'parsing', 'parsed', 'chunking', 'embedding'])

function formatBytes(bytes?: number | null) {
  const n = Number(bytes || 0)
  if (!n) return '—'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

function formatDate(value?: string | null) {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
}

function currentVersion(file: FileRecord): FileVersion | null {
  const versions = (file.file_versions || []).filter(v => v && v.id)
  return versions[0] || null
}

function fileName(file: FileRecord) {
  return file.logical_name || currentVersion(file)?.original_filename || 'Unnamed file'
}

function StatusPill({ status }: { status?: FileStatus | null }) {
  const s = String(status || 'unknown')
  return (
    <span className={`inline-flex items-center rounded-full px-2.5 py-1 text-xs font-medium ring-1 ${STATUS_STYLES[s] || 'bg-slate-700/70 text-slate-200 ring-slate-600/50'}`}>
      {STATUS_LABELS[s] || s}
    </span>
  )
}

export default function DashboardPage() {
  const router = useRouter()
  const fileInputRef = useRef<HTMLInputElement>(null)

  const [files, setFiles] = useState<FileRecord[]>([])
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [uploadQueue, setUploadQueue] = useState<UploadItem[]>([])
  const [isDragging, setIsDragging] = useState(false)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const [selectedIds, setSelectedIds] = useState<string[]>([])
  const [deleting, setDeleting] = useState(false)
  const [downloading, setDownloading] = useState(false)

  const isUploading = uploadQueue.some(item => item.status === 'waiting' || item.status === 'uploading')

  const stats = useMemo(() => {
    let ready = 0
    let processing = 0
    let failed = 0
    let review = 0

    for (const file of files) {
      const status = String(currentVersion(file)?.status || 'unknown')
      if (status === 'ready') ready++
      else if (status === 'failed') failed++
      else if (status === 'needs_review') review++
      else if (PROCESSING_STATUSES.has(status)) processing++
    }

    return { total: files.length, ready, processing, failed, review }
  }, [files])

  const selectedCount = selectedIds.length
  const allSelected = files.length > 0 && selectedIds.length === files.length

  useEffect(() => {
    let alive = true

    async function init() {
      const { data } = await supabase.auth.getSession()
      if (!alive) return
      if (!data.session) {
        router.replace('/login')
        return
      }
      await loadFiles(false)
    }

    init()
    const interval = window.setInterval(() => loadFiles(true), 5000)

    return () => {
      alive = false
      window.clearInterval(interval)
    }
  }, [router])

  async function loadFiles(silent = false) {
    if (silent) setRefreshing(true)
    try {
      const headers = await getAuthHeader()
      const res = await fetch(`${API_URL}/files`, { headers })
      if (res.status === 401) {
        router.replace('/login')
        return
      }
      const data = await res.json().catch(() => [])
      if (!res.ok) throw new Error(data?.detail || 'Failed to load files')
      setFiles(Array.isArray(data) ? data : [])
      setSelectedIds(prev => prev.filter(id => (Array.isArray(data) ? data : []).some((f: FileRecord) => f.id === id)))
    } catch (err: any) {
      if (!silent) setError(err?.message || 'Failed to load files')
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }

  async function uploadOneFile(file: File, headers: Record<string, string>) {
    const requestRes = await fetch(`${API_URL}/upload/request`, {
      method: 'POST',
      headers: { ...headers, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        filename: file.name,
        mime_type: file.type || 'application/octet-stream',
        size_bytes: file.size,
      }),
    })

    const requestData = await requestRes.json().catch(() => ({}))
    if (!requestRes.ok) throw new Error(requestData.detail || 'Failed to request upload URL')

    const { error: storageError } = await supabase.storage
      .from('company-files')
      .uploadToSignedUrl(requestData.storage_path, requestData.upload_token, file)

    if (storageError) throw new Error(storageError.message || 'Failed to upload to storage')

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

    if (rawFiles.length > 20) {
      setError('Max 20 files at a time.')
      return
    }

    setError('')
    setNotice('')

    let headers: Record<string, string>
    try {
      headers = await getAuthHeader()
    } catch (err: any) {
      setError(err?.message || 'Not authenticated')
      return
    }

    const queue = rawFiles.map((file, index) => ({ id: index, name: file.name, status: 'waiting' as const }))
    setUploadQueue(queue)

    for (let i = 0; i < rawFiles.length; i++) {
      setUploadQueue(prev => prev.map(item => item.id === i ? { ...item, status: 'uploading' } : item))
      try {
        await uploadOneFile(rawFiles[i], headers)
        setUploadQueue(prev => prev.map(item => item.id === i ? { ...item, status: 'queued' } : item))
      } catch (err: any) {
        setUploadQueue(prev => prev.map(item => item.id === i ? { ...item, status: 'failed', error: err?.message || 'Upload failed' } : item))
      }
    }

    await loadFiles(true)
    setNotice('Upload submitted. Files will become searchable after processing.')
    window.setTimeout(() => setUploadQueue([]), 3500)
    if (fileInputRef.current) fileInputRef.current.value = ''
  }

  async function handleSignOut() {
    await supabase.auth.signOut()
    router.replace('/login')
  }

  function toggleFileSelected(fileId: string) {
    setSelectedIds(prev => prev.includes(fileId) ? prev.filter(id => id !== fileId) : [...prev, fileId])
  }

  function toggleAllSelected() {
    setSelectedIds(allSelected ? [] : files.map(file => file.id))
  }

  async function deleteFileIds(ids: string[]) {
    if (!ids.length) return
    const ok = window.confirm(`Delete ${ids.length} file(s)? This removes the file, chunks, jobs, and storage object.`)
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
      setSelectedIds(prev => prev.filter(id => !ids.includes(id)))
      setNotice(`Deleted ${ids.length} file(s).`)
      await loadFiles(true)
    } catch (err: any) {
      setError(err?.message || 'Delete failed')
    } finally {
      setDeleting(false)
    }
  }

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

      for (const item of data.files || []) {
        if (!item.url) continue
        const a = document.createElement('a')
        a.href = item.url
        a.download = item.filename || 'download'
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

  return (
    <main className="min-h-screen bg-slate-950 text-white">
      <header className="sticky top-0 z-20 border-b border-white/10 bg-slate-950/85 backdrop-blur">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-5 py-4">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-2xl bg-orange-500 shadow-lg shadow-orange-500/20">
              <span className="font-black">P</span>
            </div>
            <div>
              <p className="font-semibold tracking-tight">PatchBrain</p>
              <p className="text-xs text-slate-500">File Library</p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button onClick={() => loadFiles(false)} className="hidden rounded-xl border border-white/10 px-3 py-2 text-sm text-slate-300 transition hover:bg-white/5 sm:inline-flex">
              {refreshing ? 'Refreshing…' : 'Refresh'}
            </button>
            <button onClick={() => router.push('/chat')} className="rounded-xl bg-orange-500 px-4 py-2 text-sm font-semibold text-white shadow-lg shadow-orange-500/20 transition hover:bg-orange-400">
              Open Chat
            </button>
            <button onClick={handleSignOut} className="rounded-xl px-3 py-2 text-sm text-slate-400 transition hover:bg-white/5 hover:text-white">
              Sign out
            </button>
          </div>
        </div>
      </header>

      <div className="mx-auto max-w-7xl px-5 py-8">
        <section className="mb-6 grid gap-4 lg:grid-cols-[1.25fr_0.75fr]">
          <div className="rounded-3xl border border-white/10 bg-white/[0.04] p-6 shadow-2xl shadow-black/20">
            <p className="text-xs font-medium uppercase tracking-[0.22em] text-orange-300">Operations workspace</p>
            <h1 className="mt-3 text-3xl font-semibold tracking-tight">Company document library</h1>
            <p className="mt-3 max-w-2xl text-sm leading-6 text-slate-400">
              Upload run tickets, invoices, compliance reports, maintenance files, CSVs, spreadsheets, and field notes. Ready files are available to PatchBrain chat and deterministic calculation tools.
            </p>
          </div>

          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-2">
            {[
              ['Total', stats.total],
              ['Ready', stats.ready],
              ['Processing', stats.processing],
              ['Failed', stats.failed],
            ].map(([label, value]) => (
              <div key={label} className="rounded-2xl border border-white/10 bg-white/[0.04] p-4">
                <p className="text-xs text-slate-500">{label}</p>
                <p className="mt-2 text-2xl font-semibold">{value}</p>
              </div>
            ))}
          </div>
        </section>

        {(error || notice) && (
          <div className={`mb-5 rounded-2xl border px-4 py-3 text-sm ${error ? 'border-red-400/20 bg-red-400/10 text-red-200' : 'border-emerald-400/20 bg-emerald-400/10 text-emerald-200'}`}>
            {error || notice}
          </div>
        )}

        <section className="mb-6 rounded-3xl border border-white/10 bg-white/[0.035] p-5">
          <div
            onDragOver={e => { e.preventDefault(); setIsDragging(true) }}
            onDragLeave={() => setIsDragging(false)}
            onDrop={e => {
              e.preventDefault()
              setIsDragging(false)
              handleFilesUpload(Array.from(e.dataTransfer.files || []))
            }}
            onClick={() => !isUploading && fileInputRef.current?.click()}
            className={`cursor-pointer rounded-3xl border-2 border-dashed p-8 text-center transition ${isDragging ? 'border-orange-400 bg-orange-400/10' : isUploading ? 'cursor-not-allowed border-slate-700 bg-slate-900/60' : 'border-slate-700 bg-slate-900/40 hover:border-orange-400/60 hover:bg-slate-900/70'}`}
          >
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept=".pdf,.xlsx,.xls,.docx,.txt,.csv"
              className="hidden"
              onChange={e => e.target.files && handleFilesUpload(Array.from(e.target.files))}
            />
            <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-2xl bg-orange-500/15 text-orange-200 ring-1 ring-orange-400/20">
              ↑
            </div>
            <p className="font-medium text-slate-100">Drop files here or click to upload</p>
            <p className="mt-1 text-xs text-slate-500">PDF, Excel, Word, CSV, TXT · max 20 files per batch</p>
          </div>

          {uploadQueue.length > 0 && (
            <div className="mt-4 grid gap-2 md:grid-cols-2">
              {uploadQueue.map(item => (
                <div key={item.id} className="flex items-center justify-between gap-3 rounded-2xl border border-white/10 bg-slate-950/50 px-4 py-3 text-sm">
                  <div className="min-w-0">
                    <p className="truncate text-slate-200">{item.name}</p>
                    {item.error && <p className="mt-1 truncate text-xs text-red-300">{item.error}</p>}
                  </div>
                  <span className={`shrink-0 rounded-full px-2.5 py-1 text-xs ${item.status === 'failed' ? 'bg-red-500/15 text-red-200' : item.status === 'queued' ? 'bg-emerald-500/15 text-emerald-200' : 'bg-orange-500/15 text-orange-200'}`}>
                    {item.status}
                  </span>
                </div>
              ))}
            </div>
          )}
        </section>

        <section className="overflow-hidden rounded-3xl border border-white/10 bg-white/[0.035] shadow-2xl shadow-black/20">
          <div className="flex flex-col gap-3 border-b border-white/10 px-5 py-4 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <h2 className="font-semibold">Files</h2>
              <p className="text-xs text-slate-500">Auto-refreshes every 5 seconds while processing.</p>
            </div>
            <div className="flex flex-wrap gap-2">
              {selectedCount > 0 && (
                <>
                  <button disabled={downloading} onClick={() => downloadFileIds(selectedIds)} className="rounded-xl border border-white/10 px-3 py-2 text-sm text-slate-200 transition hover:bg-white/5 disabled:opacity-50">
                    {downloading ? 'Downloading…' : `Download ${selectedCount}`}
                  </button>
                  <button disabled={deleting} onClick={() => deleteFileIds(selectedIds)} className="rounded-xl border border-red-400/20 bg-red-500/10 px-3 py-2 text-sm text-red-200 transition hover:bg-red-500/15 disabled:opacity-50">
                    {deleting ? 'Deleting…' : `Delete ${selectedCount}`}
                  </button>
                </>
              )}
            </div>
          </div>

          {loading ? (
            <div className="space-y-3 p-5">
              {[1, 2, 3].map(i => <div key={i} className="h-16 animate-pulse rounded-2xl bg-white/[0.04]" />)}
            </div>
          ) : files.length === 0 ? (
            <div className="p-10 text-center">
              <p className="font-medium text-slate-200">No files uploaded yet</p>
              <p className="mt-1 text-sm text-slate-500">Upload a few sample CSVs, PDFs, or Excel files to start testing PatchBrain.</p>
            </div>
          ) : (
            <div className="overflow-x-auto custom-scrollbar">
              <table className="w-full min-w-[760px] text-left text-sm">
                <thead className="bg-slate-950/60 text-xs uppercase tracking-wide text-slate-500">
                  <tr>
                    <th className="w-12 px-5 py-3">
                      <input type="checkbox" checked={allSelected} onChange={toggleAllSelected} className="h-4 w-4 rounded border-slate-600 bg-slate-900 accent-orange-500" />
                    </th>
                    <th className="px-3 py-3">File</th>
                    <th className="px-3 py-3">Status</th>
                    <th className="px-3 py-3">Size</th>
                    <th className="px-3 py-3">Uploaded</th>
                    <th className="px-5 py-3 text-right">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-white/10">
                  {files.map(file => {
                    const version = currentVersion(file)
                    const status = version?.status || 'unknown'
                    const selected = selectedIds.includes(file.id)
                    return (
                      <tr key={file.id} className={`transition hover:bg-white/[0.035] ${selected ? 'bg-orange-500/[0.04]' : ''}`}>
                        <td className="px-5 py-4">
                          <input type="checkbox" checked={selected} onChange={() => toggleFileSelected(file.id)} className="h-4 w-4 rounded border-slate-600 bg-slate-900 accent-orange-500" />
                        </td>
                        <td className="px-3 py-4">
                          <p className="max-w-[340px] truncate font-medium text-slate-100">{fileName(file)}</p>
                          {version?.error_message && <p className="mt-1 max-w-[420px] truncate text-xs text-red-300">{version.error_message}</p>}
                        </td>
                        <td className="px-3 py-4"><StatusPill status={status} /></td>
                        <td className="px-3 py-4 text-slate-400">{formatBytes(version?.size_bytes)}</td>
                        <td className="px-3 py-4 text-slate-400">{formatDate(version?.created_at || file.created_at)}</td>
                        <td className="px-5 py-4">
                          <div className="flex justify-end gap-2">
                            <button onClick={() => downloadFileIds([file.id])} className="rounded-lg px-2.5 py-1.5 text-xs text-slate-300 transition hover:bg-white/10 hover:text-white">Download</button>
                            <button onClick={() => deleteFileIds([file.id])} className="rounded-lg px-2.5 py-1.5 text-xs text-red-300 transition hover:bg-red-500/10">Delete</button>
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
  )
}
