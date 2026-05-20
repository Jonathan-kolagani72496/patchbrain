'use client'
import { useState, useEffect, useRef } from 'react'
import { useRouter } from 'next/navigation'
import { supabase, getAuthHeader, API_URL } from '@/lib/supabase'

type FileStatus = 'uploaded' | 'queued' | 'parsing' | 'parsed' | 'chunking' | 'embedding' | 'ready' | 'failed' | 'needs_review'

type FileRecord = {
  id: string
  logical_name: string
  created_at: string
  file_versions: {
    id: string
    status: FileStatus
    original_filename: string
    size_bytes: number
    created_at: string
  }[]
}

type UploadItem = {
  id: number           // index-based, avoids duplicate-filename collisions
  name: string
  status: 'waiting' | 'uploading' | 'queued' | 'failed'
  error?: string
}

const STATUS_STYLES: Record<FileStatus, string> = {
  uploaded:     'bg-gray-700 text-gray-300',
  queued:       'bg-blue-500/20 text-blue-300',
  parsing:      'bg-yellow-500/20 text-yellow-300',
  parsed:       'bg-yellow-500/20 text-yellow-300',
  chunking:     'bg-yellow-500/20 text-yellow-300',
  embedding:    'bg-purple-500/20 text-purple-300',
  ready:        'bg-green-500/20 text-green-300',
  failed:       'bg-red-500/20 text-red-300',
  needs_review: 'bg-orange-500/20 text-orange-300',
}

const STATUS_LABELS: Record<FileStatus, string> = {
  uploaded:     'Uploaded',
  queued:       'Queued',
  parsing:      'Reading file…',
  parsed:       'Parsed',
  chunking:     'Preparing…',
  embedding:    'Indexing…',
  ready:        'Ready',
  failed:       'Failed',
  needs_review: 'Needs review',
}

function formatBytes(bytes: number) {
  if (!bytes) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

export default function DashboardPage() {
  const router = useRouter()
  const [files, setFiles] = useState<FileRecord[]>([])
  const [loading, setLoading] = useState(true)
  const [uploadQueue, setUploadQueue] = useState<UploadItem[]>([])
  const [isDragging, setIsDragging] = useState(false)
  const [uploadError, setUploadError] = useState('')
  const [selectedIds, setSelectedIds] = useState<string[]>([])
  const [deleting, setDeleting] = useState(false)
  const [downloading, setDownloading] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)

  const isUploading = uploadQueue.some(q => q.status === 'waiting' || q.status === 'uploading')

  useEffect(() => {
    checkAuth()
    loadFiles()
    const interval = setInterval(loadFiles, 5000)
    return () => clearInterval(interval)
  }, [])

  async function checkAuth() {
    const { data } = await supabase.auth.getSession()
    if (!data.session) router.push('/login')
  }

  async function loadFiles() {
    try {
      const headers = await getAuthHeader()
      const res = await fetch(`${API_URL}/files`, { headers })
      if (res.status === 401) { router.push('/login'); return }
      const data = await res.json()
      setFiles(data || [])
    } catch (err) {
      console.error('Failed to load files', err)
    } finally {
      setLoading(false)
    }
  }

  async function uploadOneFile(file: File, headers: Record<string, string>): Promise<void> {
    const requestRes = await fetch(`${API_URL}/upload/request`, {
      method: 'POST',
      headers: { ...headers, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        filename: file.name,
        mime_type: file.type || 'application/octet-stream',
        size_bytes: file.size,
      }),
    })

    const requestData = await requestRes.json()
    if (!requestRes.ok) throw new Error(requestData.detail || 'Failed to request upload URL')

    // Must use supabase client — raw fetch PUT to Supabase signed upload URLs doesn't work
    const { error: storageError } = await supabase.storage
      .from('company-files')
      .uploadToSignedUrl(requestData.storage_path, requestData.upload_token, file)

    if (storageError) throw new Error(storageError.message || 'Failed to upload to storage')

    const confirmRes = await fetch(`${API_URL}/upload/confirm`, {
      method: 'POST',
      headers: { ...headers, 'Content-Type': 'application/json' },
      body: JSON.stringify({ file_version_id: requestData.file_version_id }),
    })

    const confirmData = await confirmRes.json()
    if (!confirmRes.ok) throw new Error(confirmData.detail || 'Failed to queue file')
  }

  async function handleFilesUpload(rawFiles: File[]) {
    if (!rawFiles.length) return

    if (rawFiles.length > 20) {
      setUploadError('Max 20 files at a time.')
      return
    }

    setUploadError('')

    // Fetch auth once for the whole batch — avoids per-file session lookups
    // that can blip and falsely mark a file as failed after storage accepted it
    let headers: Record<string, string>
    try {
      headers = await getAuthHeader()
    } catch (err: any) {
      setUploadError(err.message || 'Not authenticated')
      return
    }

    // Build queue using index as stable ID — avoids same-filename collision
    const queue: UploadItem[] = rawFiles.map((f, i) => ({
      id: i,
      name: f.name,
      status: 'waiting',
    }))
    setUploadQueue(queue)

    for (let i = 0; i < rawFiles.length; i++) {
      setUploadQueue(prev =>
        prev.map(item => item.id === i ? { ...item, status: 'uploading' } : item)
      )

      try {
        await uploadOneFile(rawFiles[i], headers)
        setUploadQueue(prev =>
          prev.map(item => item.id === i ? { ...item, status: 'queued' } : item)
        )
      } catch (err: any) {
        setUploadQueue(prev =>
          prev.map(item =>
            item.id === i
              ? { ...item, status: 'failed', error: err.message || 'Upload failed' }
              : item
          )
        )
      }
    }

    await loadFiles()
    // Clear queue after a short delay so user can see all-green before it disappears
    setTimeout(() => setUploadQueue([]), 3000)
    if (fileInputRef.current) fileInputRef.current.value = ''
  }

  async function handleSignOut() {
    await supabase.auth.signOut()
    router.push('/login')
  }

  // ── Selection helpers ──────────────────────────────────────────────────────

  const selectedCount = selectedIds.length
  const allSelected = files.length > 0 && selectedIds.length === files.length

  function toggleFileSelected(fileId: string) {
    setSelectedIds(prev =>
      prev.includes(fileId)
        ? prev.filter(id => id !== fileId)
        : [...prev, fileId]
    )
  }

  function toggleAllSelected() {
    setSelectedIds(allSelected ? [] : files.map(f => f.id))
  }

  // ── Core delete/download by explicit ID list ───────────────────────────────
  // These take an explicit id list so hover-row buttons don't race with state.

  async function deleteFileIds(ids: string[]) {
    if (ids.length === 0) return

    const ok = window.confirm(
      `Delete ${ids.length} file(s)? This permanently removes the file, chunks, jobs, and storage object.`
    )
    if (!ok) return

    setDeleting(true)
    setUploadError('')

    try {
      const headers = await getAuthHeader()
      const res = await fetch(`${API_URL}/files/delete`, {
        method: 'POST',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify({ file_ids: ids }),
      })

      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.detail || 'Failed to delete files')

      setSelectedIds(prev => prev.filter(id => !ids.includes(id)))
      await loadFiles()
    } catch (err: any) {
      setUploadError(err.message || 'Delete failed')
    } finally {
      setDeleting(false)
    }
  }

  async function downloadFileIds(ids: string[]) {
    if (ids.length === 0) return

    setDownloading(true)
    setUploadError('')

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
      setUploadError(err.message || 'Download failed')
    } finally {
      setDownloading(false)
    }
  }

  // Bulk wrappers that use current selectedIds
  const handleDeleteSelected  = () => deleteFileIds(selectedIds)
  const handleDownloadSelected = () => downloadFileIds(selectedIds)

  const currentVersion = (f: FileRecord) => f.file_versions?.[0]

  return (
    <div className="min-h-screen bg-gray-950 text-white">

      {/* Header */}
      <header className="border-b border-gray-800 px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 bg-orange-500 rounded-lg flex items-center justify-center">
            <span className="text-white font-bold text-sm">P</span>
          </div>
          <span className="font-semibold text-white">PatchBrain</span>
        </div>
        <div className="flex items-center gap-3">
          <button
            onClick={() => router.push('/chat')}
            className="bg-orange-500 hover:bg-orange-600 text-white text-sm font-medium px-4 py-2 rounded-lg transition-colors"
          >
            Open Chat →
          </button>
          <button
            onClick={handleSignOut}
            className="text-gray-400 hover:text-gray-300 text-sm transition-colors"
          >
            Sign out
          </button>
        </div>
      </header>

      <main className="max-w-4xl mx-auto px-6 py-8">

        {/* Upload section */}
        <div className="mb-8">
          <div className="flex items-center justify-between mb-4">
            <div>
              <h1 className="text-xl font-semibold">File Library</h1>
              <p className="text-gray-400 text-sm mt-0.5">Upload field tickets, invoices, reports, and spreadsheets</p>
            </div>
          </div>

          {/* Drag-and-drop zone */}
          <div
            onDragOver={(e) => { e.preventDefault(); setIsDragging(true) }}
            onDragLeave={() => setIsDragging(false)}
            onDrop={(e) => {
              e.preventDefault()
              setIsDragging(false)
              const dropped = Array.from(e.dataTransfer.files || [])
              if (dropped.length) handleFilesUpload(dropped)
            }}
            onClick={() => !isUploading && fileInputRef.current?.click()}
            className={`relative border-2 border-dashed rounded-xl px-6 py-8 text-center transition-colors cursor-pointer
              ${isDragging
                ? 'border-orange-500 bg-orange-500/10'
                : isUploading
                  ? 'border-gray-700 bg-gray-900/50 cursor-not-allowed'
                  : 'border-gray-700 bg-gray-900 hover:border-gray-600 hover:bg-gray-900/80'
              }`}
          >
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept=".pdf,.xlsx,.xls,.docx,.txt,.csv"
              className="hidden"
              onChange={(e) => {
                if (e.target.files) handleFilesUpload(Array.from(e.target.files))
              }}
            />

            {isUploading ? (
              <div className="flex items-center justify-center gap-2 text-gray-400 text-sm">
                <span className="w-4 h-4 border-2 border-gray-500 border-t-orange-400 rounded-full animate-spin" />
                Uploading files…
              </div>
            ) : (
              <>
                <p className="text-gray-300 text-sm font-medium">
                  Drag files here or <span className="text-orange-400">click to upload</span>
                </p>
                <p className="text-gray-600 text-xs mt-1">PDF, Excel, Word, CSV, TXT · up to 20 files at once</p>
              </>
            )}
          </div>

          {/* Per-file upload queue */}
          {uploadQueue.length > 0 && (
            <div className="mt-3 space-y-1.5">
              {uploadQueue.map((item) => (
                <div
                  key={item.id}
                  className="flex items-center justify-between rounded-lg border border-gray-800 bg-gray-900 px-4 py-2 text-sm"
                >
                  <span className="text-gray-300 truncate max-w-xs">{item.name}</span>
                  <span className={
                    item.status === 'queued'    ? 'text-green-400 ml-4 shrink-0' :
                    item.status === 'failed'    ? 'text-red-400 ml-4 shrink-0' :
                    item.status === 'uploading' ? 'text-orange-400 ml-4 shrink-0' :
                                                  'text-gray-500 ml-4 shrink-0'
                  }>
                    {item.status === 'failed' && item.error ? item.error : item.status}
                  </span>
                </div>
              ))}
            </div>
          )}

          {uploadError && (
            <div className="text-sm text-red-400 bg-red-400/10 border border-red-400/20 rounded-lg px-4 py-3 mt-3">
              {uploadError}
            </div>
          )}
        </div>

        {/* File list */}
        <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
          {loading ? (
            <div className="px-6 py-12 text-center text-gray-500 text-sm">Loading files…</div>
          ) : files.length === 0 ? (
            <div className="px-6 py-12 text-center">
              <p className="text-gray-400 text-sm">No files uploaded yet.</p>
              <p className="text-gray-600 text-xs mt-1">Upload a PDF or Excel file to get started.</p>
            </div>
          ) : (
            // FIX 1: wrap in a fragment <> so JSX is valid
            <>
              {/* Bulk action bar — only shows when something is selected */}
              {selectedCount > 0 && (
                <div className="flex items-center justify-between px-6 py-3 border-b border-gray-800 bg-gray-900/80">
                  <span className="text-sm text-gray-400">{selectedCount} selected</span>
                  <div className="flex items-center gap-2">
                    <button
                      onClick={handleDownloadSelected}
                      disabled={downloading}
                      className="text-xs px-3 py-1.5 rounded-lg border border-gray-700 text-gray-300 hover:text-white hover:border-gray-600 disabled:opacity-50"
                    >
                      {downloading ? 'Downloading…' : 'Download'}
                    </button>
                    <button
                      onClick={handleDeleteSelected}
                      disabled={deleting}
                      className="text-xs px-3 py-1.5 rounded-lg border border-red-500/40 text-red-300 hover:bg-red-500/10 disabled:opacity-50"
                    >
                      {deleting ? 'Deleting…' : 'Delete'}
                    </button>
                  </div>
                </div>
              )}

              <table className="w-full">
                <thead>
                  <tr className="border-b border-gray-800">
                    <th className="px-6 py-3 w-10">
                      <input
                        type="checkbox"
                        checked={allSelected}
                        onChange={toggleAllSelected}
                        className="accent-orange-500"
                      />
                    </th>
                    <th className="text-left px-6 py-3 text-gray-500 text-xs font-medium">File</th>
                    <th className="text-left px-6 py-3 text-gray-500 text-xs font-medium">Size</th>
                    <th className="text-left px-6 py-3 text-gray-500 text-xs font-medium">Status</th>
                    <th className="text-left px-6 py-3 text-gray-500 text-xs font-medium">Uploaded</th>
                    {/* FIX 2: Actions column is last, not second */}
                    <th className="text-right px-6 py-3 text-gray-500 text-xs font-medium">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {files.map((file, i) => {
                    const v = currentVersion(file)
                    const status = v?.status as FileStatus || 'uploaded'
                    return (
                      <tr
                        key={file.id}
                        className={`${i < files.length - 1 ? 'border-b border-gray-800' : ''} group hover:bg-gray-800/50 transition-colors`}
                      >
                        {/* Checkbox */}
                        <td className="px-6 py-4">
                          <input
                            type="checkbox"
                            checked={selectedIds.includes(file.id)}
                            onChange={() => toggleFileSelected(file.id)}
                            className="accent-orange-500"
                          />
                        </td>

                        {/* File name */}
                        <td className="px-6 py-4">
                          <span className="text-white text-sm font-medium">{file.logical_name}</span>
                        </td>

                        {/* Size */}
                        <td className="px-6 py-4 text-gray-400 text-sm">
                          {formatBytes(v?.size_bytes || 0)}
                        </td>

                        {/* Status */}
                        <td className="px-6 py-4">
                          <span className={`inline-flex items-center gap-1.5 text-xs font-medium px-2.5 py-1 rounded-full ${STATUS_STYLES[status]}`}>
                            {['queued','parsing','parsed','chunking','embedding'].includes(status) && (
                              <span className="w-1.5 h-1.5 rounded-full bg-current animate-pulse" />
                            )}
                            {STATUS_LABELS[status]}
                          </span>
                        </td>

                        {/* Uploaded date */}
                        <td className="px-6 py-4 text-gray-500 text-sm">
                          {new Date(file.created_at).toLocaleDateString()}
                        </td>

                        {/* FIX 3: Actions — pass ID directly, no state race */}
                        <td className="px-6 py-4 text-right">
                          <div className="opacity-0 group-hover:opacity-100 transition-opacity flex items-center justify-end gap-2">
                            <button
                              onClick={() => downloadFileIds([file.id])}
                              disabled={downloading}
                              className="text-xs text-gray-400 hover:text-white disabled:opacity-30"
                            >
                              Download
                            </button>
                            <button
                              onClick={() => deleteFileIds([file.id])}
                              disabled={deleting}
                              className="text-xs text-red-400 hover:text-red-300 disabled:opacity-30"
                            >
                              Delete
                            </button>
                          </div>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </>
          )}
        </div>

      </main>
    </div>
  )
}