'use client'
import { useState, useEffect, useRef } from 'react'
import { useRouter } from 'next/navigation'
import { supabase, getAuthHeader, API_URL } from '@/lib/supabase'

type Citation = {
  index: number
  chunk_id: string
  source_type: string
  page_number?: number
  sheet_name?: string
  row_start?: number
  row_end?: number
  filename?: string
  preview?: string
  score?: number
}

type Message = {
  id: string
  role: 'user' | 'assistant'
  content: string
  citations?: Citation[]
  loading?: boolean
  created_at?: string
}

type Session = {
  id: string
  title: string
  message_count: number
  updated_at: string
}

export default function ChatPage() {
  const router = useRouter()
  const [sessions, setSessions]                 = useState<Session[]>([])
  const [activeSession, setActiveSession]       = useState<string | null>(null)
  const [messages, setMessages]                 = useState<Message[]>([])
  const [input, setInput]                       = useState('')
  const [sending, setSending]                   = useState(false)
  const [selectedCitation, setSelectedCitation] = useState<Citation | null>(null)
  const [companyName, setCompanyName]           = useState('PatchBrain')
  const [error, setError]                       = useState('')
  const bottomRef = useRef<HTMLDivElement>(null)
  const inputRef  = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => {
      if (!data.session) router.push('/login')
    })
    loadSessions()
    loadMe()
  }, [])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  async function loadMe() {
    try {
      const headers = await getAuthHeader()
      const res = await fetch(`${API_URL}/me`, { headers })
      if (res.ok) { const d = await res.json(); setCompanyName(d.company_name || 'PatchBrain') }
    } catch {}
  }

  async function loadSessions() {
    try {
      const headers = await getAuthHeader()
      const res = await fetch(`${API_URL}/conversations`, { headers })
      if (res.ok) setSessions(await res.json())
    } catch {}
  }

  async function loadSession(sessionId: string) {
    setActiveSession(sessionId)
    setSelectedCitation(null)
    setError('')
    try {
      const headers = await getAuthHeader()
      const res = await fetch(`${API_URL}/conversations/${sessionId}/messages`, { headers })
      if (!res.ok) return
      const data = await res.json()
      setMessages((data.messages || []).map((m: any) => ({
        id: m.id, role: m.role, content: m.content,
        citations: m.citations_json || [], created_at: m.created_at,
      })))
    } catch {}
  }

  async function newChat() {
    setActiveSession(null)
    setMessages([])
    setSelectedCitation(null)
    setError('')
    inputRef.current?.focus()
  }

  async function deleteSession(sessionId: string, e: React.MouseEvent) {
    e.stopPropagation()
    try {
      const headers = await getAuthHeader()
      await fetch(`${API_URL}/conversations/${sessionId}`, { method: 'DELETE', headers })
      if (activeSession === sessionId) { setActiveSession(null); setMessages([]) }
      await loadSessions()
    } catch {}
  }

  async function sendMessage() {
    if (!input.trim() || sending) return
    const question = input.trim()
    setInput('')
    setSending(true)
    setError('')
    setSelectedCitation(null)

    const userMsg: Message   = { id: Date.now().toString(), role: 'user', content: question }
    const loadingMsg: Message = { id: Date.now().toString() + '-a', role: 'assistant', content: '', loading: true }
    setMessages(prev => [...prev, userMsg, loadingMsg])

    try {
      const headers = await getAuthHeader()
      const res = await fetch(`${API_URL}/ask`, {
        method: 'POST',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify({ question, session_id: activeSession }),
      })
      if (res.status === 401) { router.push('/login'); return }
      const data = await res.json()
      if (!res.ok) {
        const msg = data.detail || 'Something went wrong'
        setMessages(prev => prev.map(m => m.id === loadingMsg.id ? { ...m, content: msg, loading: false } : m))
        if (res.status === 402) setError(msg)
        return
      }
      if (data.session_id && !activeSession) {
        setActiveSession(data.session_id)
      }
      setMessages(prev => prev.map(m =>
        m.id === loadingMsg.id ? { ...m, content: data.answer, citations: data.citations, loading: false } : m
      ))
      await loadSessions()
    } catch {
      setMessages(prev => prev.map(m =>
        m.id === loadingMsg.id ? { ...m, content: 'Connection error — is the backend running?', loading: false } : m
      ))
    } finally {
      setSending(false)
      inputRef.current?.focus()
    }
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage() }
  }

  function renderAnswer(text: string, citations: Citation[] = []) {
    return text.split(/(\[S\d+\])/g).map((part, i) => {
      const match = part.match(/\[S(\d+)\]/)
      if (match) {
        const c = citations.find(c => c.index === parseInt(match[1]))
        return (
          <button key={i} onClick={() => c && setSelectedCitation(c)}
            className="inline-flex items-center bg-orange-500/20 hover:bg-orange-500/30 text-orange-300 text-xs font-medium px-1.5 py-0.5 rounded mx-0.5 transition-colors">
            {part}
          </button>
        )
      }
      return <span key={i}>{part}</span>
    })
  }

  function citationLabel(c: Citation) {
    const fname = c.filename ? (c.filename.length > 18 ? c.filename.slice(0, 18) + '…' : c.filename) : ''
    if (fname && c.sheet_name) return `${fname} · r${c.row_start}–${c.row_end}`
    if (fname && c.page_number) return `${fname} · p.${c.page_number}`
    if (fname) return fname
    if (c.sheet_name) return `${c.sheet_name} r${c.row_start}–${c.row_end}`
    if (c.page_number) return `Page ${c.page_number}`
    return `Source ${c.index}`
  }

  function timeAgo(d: string) {
    const m = Math.floor((Date.now() - new Date(d).getTime()) / 60000)
    if (m < 1) return 'just now'
    if (m < 60) return `${m}m ago`
    const h = Math.floor(m / 60)
    if (h < 24) return `${h}h ago`
    return `${Math.floor(h / 24)}d ago`
  }

  return (
    <div className="h-screen bg-gray-950 text-white flex overflow-hidden">

      {/* ── Sidebar ── */}
      <div className="w-60 border-r border-gray-800 flex flex-col shrink-0 bg-gray-900">
        <div className="p-3 border-b border-gray-800">
          <div className="flex items-center gap-2 mb-3">
            <div className="w-6 h-6 bg-orange-500 rounded flex items-center justify-center shrink-0">
              <span className="text-white font-bold text-xs">P</span>
            </div>
            <span className="text-white text-sm font-medium truncate">{companyName}</span>
          </div>
          <button onClick={newChat}
            className="w-full bg-orange-500 hover:bg-orange-600 text-white text-sm font-medium py-2 rounded-lg transition-colors">
            + New chat
          </button>
        </div>

        <div className="flex-1 overflow-y-auto py-1">
          {sessions.length === 0
            ? <p className="text-gray-600 text-xs px-4 py-4">No conversations yet</p>
            : sessions.map(s => (
              <div key={s.id} onClick={() => loadSession(s.id)}
                className={`group flex items-start gap-2 px-3 py-2.5 cursor-pointer hover:bg-gray-800 mx-1 rounded-lg transition-colors ${activeSession === s.id ? 'bg-gray-800' : ''}`}>
                <div className="flex-1 min-w-0">
                  <p className={`text-xs truncate leading-snug ${activeSession === s.id ? 'text-white' : 'text-gray-300'}`}>
                    {s.title || 'New conversation'}
                  </p>
                  <p className="text-gray-600 text-xs mt-0.5">{timeAgo(s.updated_at)}</p>
                </div>
                <button onClick={e => deleteSession(s.id, e)}
                  className="opacity-0 group-hover:opacity-100 text-gray-600 hover:text-red-400 text-xs shrink-0 transition-all mt-0.5">
                  ✕
                </button>
              </div>
            ))
          }
        </div>

        <div className="p-3 border-t border-gray-800 space-y-1">
          <button onClick={() => router.push('/dashboard')}
            className="w-full text-left text-gray-400 hover:text-gray-300 text-xs px-3 py-2 rounded-lg hover:bg-gray-800 transition-colors">
            ← File Library
          </button>
          <button onClick={async () => { await supabase.auth.signOut(); router.push('/login') }}
            className="w-full text-left text-gray-500 hover:text-gray-400 text-xs px-3 py-2 rounded-lg hover:bg-gray-800 transition-colors">
            Sign out
          </button>
        </div>
      </div>

      {/* ── Chat + source panel ── */}
      <div className="flex flex-1 overflow-hidden">

        {/* Messages column */}
        <div className="flex flex-col flex-1 overflow-hidden">
          {error && (
            <div className="bg-red-500/10 border-b border-red-500/20 px-6 py-3 text-sm text-red-400 flex items-center justify-between shrink-0">
              <span>⚠ {error}</span>
              <button onClick={() => setError('')} className="hover:text-red-300 ml-4">✕</button>
            </div>
          )}

          <div className="flex-1 overflow-y-auto px-6 py-6 space-y-5">
            {messages.length === 0 && (
              <div className="h-full flex flex-col items-center justify-center text-center">
                <div className="w-12 h-12 bg-orange-500/10 border border-orange-500/20 rounded-2xl flex items-center justify-center mb-4">
                  <span className="text-orange-400 text-xl">⚡</span>
                </div>
                <h2 className="text-white font-medium mb-1">Ask about your documents</h2>
                <p className="text-gray-500 text-sm max-w-xs mb-6">Questions are answered from your uploaded files with source citations.</p>
                <div className="grid grid-cols-1 gap-2 w-full max-w-sm">
                  {['Which invoices are overdue?','Are any trucks overdue for service?','Which employees have expired H2S certs?','Show run tickets missing signatures'].map(q => (
                    <button key={q} onClick={() => { setInput(q); inputRef.current?.focus() }}
                      className="text-left text-sm text-gray-400 hover:text-gray-300 bg-gray-900 hover:bg-gray-800 border border-gray-800 rounded-lg px-4 py-2.5 transition-colors">
                      {q}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {messages.map(msg => (
              <div key={msg.id} className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                <div className={`max-w-2xl ${msg.role === 'user' ? 'bg-gray-800 rounded-2xl rounded-tr-sm px-4 py-3' : ''}`}>
                  {msg.loading ? (
                    <div className="flex items-center gap-2 text-gray-400 text-sm">
                      <span className="w-4 h-4 border-2 border-gray-600 border-t-orange-400 rounded-full animate-spin" />
                      Searching your files…
                    </div>
                  ) : (
                    <>
                      <div className={`text-sm leading-relaxed whitespace-pre-wrap ${msg.role === 'user' ? 'text-white' : 'text-gray-200'}`}>
                        {msg.role === 'assistant' && msg.citations?.length
                          ? renderAnswer(msg.content, msg.citations)
                          : msg.content}
                      </div>
                      {msg.citations && msg.citations.length > 0 && (
                        <div className="mt-2.5 flex flex-wrap gap-1.5">
                          {msg.citations.slice(0, 8).map(c => (
                            <button key={c.chunk_id}
                              onClick={() => setSelectedCitation(selectedCitation?.chunk_id === c.chunk_id ? null : c)}
                              className={`text-xs px-2.5 py-1 rounded-full border transition-colors ${
                                selectedCitation?.chunk_id === c.chunk_id
                                  ? 'bg-orange-500/20 border-orange-500/40 text-orange-300'
                                  : 'bg-gray-800 border-gray-700 text-gray-400 hover:border-gray-600'
                              }`}>
                              [{c.index}] {citationLabel(c)}
                            </button>
                          ))}
                        </div>
                      )}
                      {msg.created_at && (
                        <p className="text-gray-600 text-xs mt-1.5">{timeAgo(msg.created_at)}</p>
                      )}
                    </>
                  )}
                </div>
              </div>
            ))}
            <div ref={bottomRef} />
          </div>

          {/* Input bar */}
          <div className="border-t border-gray-800 px-6 py-4 shrink-0">
            <div className="flex items-end gap-3 bg-gray-900 border border-gray-700 focus-within:border-gray-600 rounded-xl px-4 py-3 transition-colors">
              <textarea ref={inputRef} value={input}
                onChange={e => setInput(e.target.value)} onKeyDown={handleKeyDown}
                placeholder="Ask about your files… (Enter to send)"
                rows={1} style={{ maxHeight: '120px' }}
                className="flex-1 bg-transparent text-white text-sm placeholder-gray-500 resize-none focus:outline-none" />
              <button onClick={sendMessage} disabled={!input.trim() || sending}
                className="shrink-0 bg-orange-500 hover:bg-orange-600 disabled:bg-gray-700 disabled:text-gray-500 text-white w-8 h-8 rounded-lg flex items-center justify-center transition-colors">
                ↑
              </button>
            </div>
            <p className="text-gray-600 text-xs mt-2 text-center">
              Answers grounded in your uploaded files · Click [S1] tags to see exact sources
            </p>
          </div>
        </div>

        {/* Source panel */}
        {selectedCitation && (
          <div className="w-72 border-l border-gray-800 bg-gray-900 flex flex-col shrink-0">
            <div className="px-4 py-3 border-b border-gray-800 flex items-center justify-between">
              <span className="text-white text-sm font-medium">Source [{selectedCitation.index}]</span>
              <button onClick={() => setSelectedCitation(null)} className="text-gray-500 hover:text-gray-300 text-lg leading-none">×</button>
            </div>
            <div className="p-4 flex-1 overflow-y-auto space-y-4">
              {selectedCitation.filename && (
                <div>
                  <span className="text-gray-500 text-xs uppercase tracking-wide">File</span>
                  <p className="text-gray-200 text-sm mt-1 font-medium break-all">{selectedCitation.filename}</p>
                </div>
              )}
              {selectedCitation.page_number && (
                <div>
                  <span className="text-gray-500 text-xs uppercase tracking-wide">Page</span>
                  <p className="text-gray-300 text-sm mt-1">{selectedCitation.page_number}</p>
                </div>
              )}
              {selectedCitation.sheet_name && (
                <div>
                  <span className="text-gray-500 text-xs uppercase tracking-wide">Sheet · Rows</span>
                  <p className="text-gray-300 text-sm mt-1">{selectedCitation.sheet_name} — rows {selectedCitation.row_start}–{selectedCitation.row_end}</p>
                </div>
              )}
              {selectedCitation.preview && (
                <div>
                  <span className="text-gray-500 text-xs uppercase tracking-wide">Content preview</span>
                  <pre className="text-gray-400 text-xs mt-2 bg-gray-800 rounded-lg p-3 whitespace-pre-wrap break-words leading-relaxed font-mono overflow-x-auto">
                    {selectedCitation.preview}
                  </pre>
                </div>
              )}
              {selectedCitation.score != null && (
                <div>
                  <span className="text-gray-500 text-xs uppercase tracking-wide">Relevance</span>
                  <div className="mt-2 flex items-center gap-2">
                    <div className="flex-1 h-1.5 bg-gray-700 rounded-full overflow-hidden">
                      <div className="h-full bg-orange-500 rounded-full"
                        style={{ width: `${Math.min(100, Math.round((selectedCitation.score||0)*100))}%` }} />
                    </div>
                    <span className="text-gray-400 text-xs">{Math.min(100, Math.round((selectedCitation.score||0)*100))}%</span>
                  </div>
                </div>
              )}
              <div className="pt-2 border-t border-gray-800">
                <span className="text-gray-600 text-xs uppercase tracking-wide">Type</span>
                <p className="text-gray-500 text-xs mt-1 capitalize">{selectedCitation.source_type?.replace(/_/g, ' ')}</p>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
