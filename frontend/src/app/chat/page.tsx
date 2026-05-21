'use client'
import { useState, useEffect, useRef, useCallback } from 'react'
import { useRouter } from 'next/navigation'
import { supabase, getAuthHeader, API_URL } from '@/lib/supabase'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

// ── Types ──────────────────────────────────────────────────────────────────────

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
  streaming?: boolean
  created_at?: string
}

type Session = {
  id: string
  title: string
  message_count: number
  updated_at: string
}

// ── Skeleton component ─────────────────────────────────────────────────────────

function MessageSkeleton() {
  return (
    <div className="space-y-5 animate-pulse">
      {[80, 55, 95, 65].map((w, i) => (
        <div key={i} className={`flex ${i % 2 === 0 ? 'justify-end' : 'justify-start'}`}>
          <div
            className={`h-9 rounded-2xl bg-gray-800 ${i % 2 === 0 ? 'rounded-tr-sm' : 'rounded-tl-sm'}`}
            style={{ width: `${w}%` }}
          />
        </div>
      ))}
    </div>
  )
}

// ── Markdown renderer with citation injection ──────────────────────────────────

function AssistantMarkdown({
  content,
  citations,
  streaming,
  onCitationClick,
}: {
  content: string
  citations: Citation[]
  streaming?: boolean
  onCitationClick: (c: Citation) => void
}) {
  // During streaming: avoid markdown flickering on partial bold/headers.
  // Render plain text with cursor until streaming finishes.
  if (streaming) {
    return (
      <p className="text-sm leading-7 text-gray-200 whitespace-pre-wrap">
        {content}
        <span className="inline-block w-0.5 h-4 bg-orange-400 animate-pulse ml-0.5 align-text-bottom" />
      </p>
    )
  }

  // Post-stream: split on [S1] tags BEFORE passing to ReactMarkdown so we can
  // inject clickable citation buttons inline with the rendered markdown.
  // Strategy: replace [S1] with a placeholder, render markdown, then swap back.
  const CITE_RE = /\[S(\d+)\]/g

  // Build a version where citation tags become unique unicode placeholders
  // that survive markdown rendering untouched.
  const placeholders: { placeholder: string; index: number }[] = []
  let idx = 0
  const withPlaceholders = content.replace(CITE_RE, (_, num) => {
    const ph = `\u{E0000}${idx}\u{E0001}` // Private Use Area chars — won't appear in real text
    placeholders.push({ placeholder: ph, index: parseInt(num) })
    idx++
    return ph
  })

  // Custom renderer that splits text nodes on placeholders and injects buttons
  const renderText = (text: string): React.ReactNode[] => {
    if (!placeholders.length || !text.includes('\u{E0000}')) return [text]
    const parts = text.split(/(\u{E0000}\d+\u{E0001})/u)
    return parts.map((part, i) => {
      const ph = placeholders.find(p => p.placeholder === part)
      if (ph) {
        const c = citations.find(c => c.index === ph.index)
        return (
          <button
            key={i}
            onClick={() => c && onCitationClick(c)}
            className="inline-flex items-center bg-orange-500/20 hover:bg-orange-500/30 text-orange-300 text-xs font-semibold px-1.5 py-0.5 rounded mx-0.5 transition-colors cursor-pointer"
          >
            [S{ph.index}]
          </button>
        )
      }
      return part
    })
  }

  return (
    <div className="prose-chat">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          // Paragraphs
          p: ({ children }) => (
            <p className="text-sm leading-7 text-gray-200 mb-3 last:mb-0">
              {processChildren(children, renderText)}
            </p>
          ),
          // Headings
          h1: ({ children }) => <h1 className="text-base font-semibold text-white mt-4 mb-2">{children}</h1>,
          h2: ({ children }) => <h2 className="text-sm font-semibold text-white mt-4 mb-2">{children}</h2>,
          h3: ({ children }) => <h3 className="text-sm font-medium text-gray-100 mt-3 mb-1">{children}</h3>,
          // Lists
          ul: ({ children }) => <ul className="text-sm text-gray-200 space-y-1 mb-3 ml-4 list-disc">{children}</ul>,
          ol: ({ children }) => <ol className="text-sm text-gray-200 space-y-1 mb-3 ml-4 list-decimal">{children}</ol>,
          li: ({ children }) => (
            <li className="leading-6">
              {processChildren(children, renderText)}
            </li>
          ),
          // Bold / italic
          strong: ({ children }) => <strong className="font-semibold text-white">{children}</strong>,
          em: ({ children }) => <em className="italic text-gray-300">{children}</em>,
          // Code
          code: ({ inline, children }: any) =>
            inline ? (
              <code className="bg-gray-800 text-orange-300 text-xs px-1.5 py-0.5 rounded font-mono">{children}</code>
            ) : (
              <pre className="bg-gray-800 rounded-lg p-3 overflow-x-auto mb-3">
                <code className="text-xs text-gray-300 font-mono">{children}</code>
              </pre>
            ),
          // Tables
          table: ({ children }) => (
            <div className="overflow-x-auto mb-3">
              <table className="text-xs text-gray-300 border-collapse w-full">{children}</table>
            </div>
          ),
          th: ({ children }) => <th className="border border-gray-700 bg-gray-800 px-3 py-1.5 text-left font-medium text-gray-200">{children}</th>,
          td: ({ children }) => <td className="border border-gray-700 px-3 py-1.5">{children}</td>,
          // Blockquote
          blockquote: ({ children }) => (
            <blockquote className="border-l-2 border-orange-500/50 pl-3 text-gray-400 italic mb-3">{children}</blockquote>
          ),
          // Horizontal rule
          hr: () => <hr className="border-gray-700 my-4" />,
          // Text nodes — inject citation buttons
          text: ({ children }: any) => <>{renderText(String(children))}</>,
        }}
      >
        {withPlaceholders}
      </ReactMarkdown>
    </div>
  )
}

// Helper: recursively process react children to inject citation buttons into text nodes
function processChildren(children: React.ReactNode, renderText: (s: string) => React.ReactNode[]): React.ReactNode {
  if (typeof children === 'string') return <>{renderText(children)}</>
  if (Array.isArray(children)) return <>{children.map((c, i) => <span key={i}>{processChildren(c, renderText)}</span>)}</>
  return children
}

// ── Main component ─────────────────────────────────────────────────────────────

export default function ChatPage() {
  const router = useRouter()

  const [sessions, setSessions]                   = useState<Session[]>([])
  const [activeSession, setActiveSession]         = useState<string | null>(null)
  const [messages, setMessages]                   = useState<Message[]>([])
  const [loadingHistory, setLoadingHistory]       = useState(false)
  const [input, setInput]                         = useState('')
  const [sending, setSending]                     = useState(false)
  const [selectedCitation, setSelectedCitation]   = useState<Citation | null>(null)
  const [companyName, setCompanyName]             = useState('PatchBrain')
  const [error, setError]                         = useState('')
  const [followUps, setFollowUps]                 = useState<string[]>([])
  const [showScrollBtn, setShowScrollBtn]         = useState(false)
  const [copiedId, setCopiedId]                   = useState<string | null>(null)
  const [hoveredMsg, setHoveredMsg]               = useState<string | null>(null)

  const bottomRef      = useRef<HTMLDivElement>(null)
  const inputRef       = useRef<HTMLTextAreaElement>(null)
  const scrollAreaRef  = useRef<HTMLDivElement>(null)
  const readerRef      = useRef<ReadableStreamDefaultReader<Uint8Array> | null>(null)
  const lastQuestionRef = useRef<string>('')

  // ── Auth + init ──────────────────────────────────────────────────────────────

  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => {
      if (!data.session) router.push('/login')
    })
    loadSessions()
    loadMe()
  }, [])

  // ── Auto-scroll to bottom ────────────────────────────────────────────────────

  useEffect(() => {
    if (!showScrollBtn) {
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
  }, [messages])

  // ── Scroll-to-bottom button visibility ──────────────────────────────────────

  function handleScroll() {
    const el = scrollAreaRef.current
    if (!el) return
    const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight
    setShowScrollBtn(distFromBottom > 200)
  }

  function scrollToBottom() {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
    setShowScrollBtn(false)
  }

  // ── Auto-grow textarea ───────────────────────────────────────────────────────

  useEffect(() => {
    const ta = inputRef.current
    if (!ta) return
    ta.style.height = 'auto'
    ta.style.height = Math.min(ta.scrollHeight, 120) + 'px'
  }, [input])

  // ── Data loading ─────────────────────────────────────────────────────────────

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
    setFollowUps([])
    setError('')
    setLoadingHistory(true)
    setMessages([]) // clear immediately so skeleton shows
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
    finally { setLoadingHistory(false) }
  }

  async function newChat() {
    setActiveSession(null)
    setMessages([])
    setFollowUps([])
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

  // ── Stop streaming ───────────────────────────────────────────────────────────

  function stopStreaming() {
    readerRef.current?.cancel()
    readerRef.current = null
    setSending(false)
    // Mark last assistant message as no longer streaming
    setMessages(prev => {
      const copy = [...prev]
      const last = copy[copy.length - 1]
      if (last?.role === 'assistant') copy[copy.length - 1] = { ...last, streaming: false }
      return copy
    })
  }

  // ── Copy message ─────────────────────────────────────────────────────────────

  async function copyMessage(id: string, content: string) {
    try {
      await navigator.clipboard.writeText(content)
      setCopiedId(id)
      setTimeout(() => setCopiedId(null), 2000)
    } catch {}
  }

  // ── Regenerate ───────────────────────────────────────────────────────────────

  function regenerate() {
    if (!lastQuestionRef.current || sending) return
    // Remove last assistant message and re-send
    setMessages(prev => {
      const copy = [...prev]
      if (copy[copy.length - 1]?.role === 'assistant') copy.pop()
      if (copy[copy.length - 1]?.role === 'user') copy.pop()
      return copy
    })
    setInput(lastQuestionRef.current)
    setTimeout(() => sendMessageWithText(lastQuestionRef.current), 50)
  }

  // ── Follow-up suggestions ────────────────────────────────────────────────────

  async function fetchFollowUps(question: string, answer: string) {
    try {
      const headers = await getAuthHeader()
      const res = await fetch(`${API_URL}/suggest-followups`, {
        method: 'POST',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify({ question, answer: answer.slice(0, 600) }),
      })
      if (res.ok) {
        const d = await res.json()
        setFollowUps((d.suggestions || []).slice(0, 3))
      }
    } catch {}
  }

  // ── Send message ─────────────────────────────────────────────────────────────

  async function sendMessage() {
    const text = input.trim()
    if (!text || sending) return
    setInput('')
    await sendMessageWithText(text)
  }

  async function sendMessageWithText(question: string) {
    setSending(true)
    setError('')
    setFollowUps([])
    setSelectedCitation(null)
    lastQuestionRef.current = question

    const userMsg: Message = { id: Date.now().toString(), role: 'user', content: question }
    setMessages(prev => [...prev, userMsg])

    try {
      const headers = await getAuthHeader()
      const res = await fetch(`${API_URL}/ask`, {
        method: 'POST',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify({ question, session_id: activeSession }),
      })

      if (res.status === 401) { router.push('/login'); return }

      if (!res.ok) {
        let msg = 'Something went wrong'
        try { const d = await res.json(); msg = d.detail || msg } catch {}
        setMessages(prev => [...prev, { id: Date.now().toString() + '-a', role: 'assistant', content: msg }])
        if (res.status === 402) setError(msg)
        return
      }

      if (!res.body) throw new Error('No response body')

      const reader  = res.body.getReader()
      readerRef.current = reader
      const decoder = new TextDecoder()
      const aId     = Date.now().toString() + '-a'
      let assistantText   = ''
      let finalCitations: Citation[] = []
      let buffer = ''

      while (true) {
        const { value, done } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const events = buffer.split('\n\n')
        buffer = events.pop() ?? ''

        for (const event of events) {
          if (!event.startsWith('data: ')) continue
          const jsonText = event.replace(/^data:\s*/, '')
          try {
            const parsed = JSON.parse(jsonText)

            if (parsed.type === 'session') {
              setActiveSession(parsed.session_id)
            }

            if (parsed.type === 'token') {
              assistantText += parsed.text
              setMessages(prev => {
                const copy = [...prev]
                const last = copy[copy.length - 1]
                if (last?.id === aId) {
                  copy[copy.length - 1] = { ...last, content: assistantText, streaming: true }
                } else {
                  copy.push({ id: aId, role: 'assistant', content: assistantText, streaming: true })
                }
                return copy
              })
            }

            if (parsed.type === 'done') {
              finalCitations = parsed.citations || []
              setMessages(prev => {
                const copy = [...prev]
                const last = copy[copy.length - 1]
                if (last?.id === aId) {
                  copy[copy.length - 1] = { ...last, content: assistantText, citations: finalCitations, streaming: false }
                }
                return copy
              })
              // Delay session reload so it doesn't interrupt the done animation
              setTimeout(loadSessions, 600)
              // Fetch follow-up suggestions in background
              fetchFollowUps(question, assistantText)
            }

            if (parsed.type === 'error') {
              const errMsg = parsed.message || 'An error occurred'
              setMessages(prev => {
                const copy = [...prev]
                const last = copy[copy.length - 1]
                if (last?.id === aId) {
                  copy[copy.length - 1] = { ...last, content: errMsg, streaming: false }
                } else {
                  copy.push({ id: aId, role: 'assistant', content: errMsg })
                }
                return copy
              })
              if (errMsg.toLowerCase().includes('billing')) setError(errMsg)
            }

          } catch (err) {
            console.error('Bad SSE event:', jsonText, err)
          }
        }
      }

    } catch (e: any) {
      if (e?.name === 'AbortError' || e?.message?.includes('cancel')) return // user stopped
      setMessages(prev => {
        const copy = [...prev]
        const last = copy[copy.length - 1]
        if (last?.role === 'assistant') {
          copy[copy.length - 1] = { ...last, content: 'Connection error — is the backend running?', streaming: false }
        } else {
          copy.push({ id: Date.now().toString() + '-a', role: 'assistant', content: 'Connection error — is the backend running?' })
        }
        return copy
      })
    } finally {
      readerRef.current = null
      setSending(false)
      inputRef.current?.focus()
    }
  }

  // ── Keyboard ─────────────────────────────────────────────────────────────────

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage() }
  }

  // ── Citation helpers ──────────────────────────────────────────────────────────

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

  // ── Render ────────────────────────────────────────────────────────────────────

  const isLastAssistant = (msg: Message) => {
    const assistantMsgs = messages.filter(m => m.role === 'assistant')
    return assistantMsgs[assistantMsgs.length - 1]?.id === msg.id
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
        <div className="flex flex-col flex-1 overflow-hidden relative">
          {error && (
            <div className="bg-red-500/10 border-b border-red-500/20 px-6 py-3 text-sm text-red-400 flex items-center justify-between shrink-0">
              <span>⚠ {error}</span>
              <button onClick={() => setError('')} className="hover:text-red-300 ml-4">✕</button>
            </div>
          )}

          {/* Scroll area */}
          <div
            ref={scrollAreaRef}
            onScroll={handleScroll}
            className="flex-1 overflow-y-auto px-6 py-6 space-y-6"
          >
            {/* Empty state */}
            {!loadingHistory && messages.length === 0 && (
              <div className="h-full flex flex-col items-center justify-center text-center">
                <div className="w-12 h-12 bg-orange-500/10 border border-orange-500/20 rounded-2xl flex items-center justify-center mb-4">
                  <span className="text-orange-400 text-xl">⚡</span>
                </div>
                <h2 className="text-white font-medium mb-1">Ask about your documents</h2>
                <p className="text-gray-500 text-sm max-w-xs mb-6">Questions are answered from your uploaded files with source citations.</p>
                <div className="grid grid-cols-1 gap-2 w-full max-w-sm">
                  {['Which invoices are overdue?', 'Are any trucks overdue for service?', 'Which employees have expired H2S certs?', 'Show run tickets missing signatures'].map(q => (
                    <button key={q} onClick={() => { setInput(q); inputRef.current?.focus() }}
                      className="text-left text-sm text-gray-400 hover:text-gray-300 bg-gray-900 hover:bg-gray-800 border border-gray-800 rounded-lg px-4 py-2.5 transition-colors">
                      {q}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {/* Skeleton while loading history */}
            {loadingHistory && <MessageSkeleton />}

            {/* Messages */}
            {!loadingHistory && messages.map(msg => (
              <div
                key={msg.id}
                className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'} group/msg`}
                onMouseEnter={() => setHoveredMsg(msg.id)}
                onMouseLeave={() => setHoveredMsg(null)}
              >
                <div className={`max-w-3xl w-full ${msg.role === 'user' ? 'flex justify-end' : ''}`}>

                  {/* User bubble */}
                  {msg.role === 'user' && (
                    <div className="bg-gray-800 rounded-2xl rounded-tr-sm px-4 py-3 max-w-xl">
                      <p className="text-sm text-white leading-relaxed">{msg.content}</p>
                    </div>
                  )}

                  {/* Assistant message */}
                  {msg.role === 'assistant' && (
                    <div className="w-full">
                      {msg.loading ? (
                        <div className="flex items-center gap-2 text-gray-400 text-sm py-2">
                          <span className="w-4 h-4 border-2 border-gray-600 border-t-orange-400 rounded-full animate-spin" />
                          Searching your files…
                        </div>
                      ) : (
                        <>
                          {/* Markdown content */}
                          <AssistantMarkdown
                            content={msg.content}
                            citations={msg.citations || []}
                            streaming={msg.streaming}
                            onCitationClick={setSelectedCitation}
                          />

                          {/* Citation pills */}
                          {msg.citations && msg.citations.length > 0 && !msg.streaming && (
                            <div className="mt-3 flex flex-wrap gap-1.5">
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

                          {/* Action buttons — visible on hover, only when not streaming */}
                          {!msg.streaming && hoveredMsg === msg.id && (
                            <div className="flex items-center gap-1 mt-2 transition-opacity">
                              {/* Copy */}
                              <button
                                onClick={() => copyMessage(msg.id, msg.content)}
                                title="Copy response"
                                className="flex items-center gap-1 text-gray-500 hover:text-gray-300 text-xs px-2 py-1 rounded-md hover:bg-gray-800 transition-colors"
                              >
                                {copiedId === msg.id ? (
                                  <><span className="text-green-400">✓</span><span className="text-green-400">Copied</span></>
                                ) : (
                                  <><CopyIcon /><span>Copy</span></>
                                )}
                              </button>

                              {/* Regenerate — only on the last assistant message */}
                              {isLastAssistant(msg) && !sending && (
                                <button
                                  onClick={regenerate}
                                  title="Regenerate response"
                                  className="flex items-center gap-1 text-gray-500 hover:text-gray-300 text-xs px-2 py-1 rounded-md hover:bg-gray-800 transition-colors"
                                >
                                  <RegenerateIcon />
                                  <span>Regenerate</span>
                                </button>
                              )}

                              {/* Timestamp — on hover only */}
                              {msg.created_at && (
                                <span className="text-gray-600 text-xs ml-1">{timeAgo(msg.created_at)}</span>
                              )}
                            </div>
                          )}
                        </>
                      )}
                    </div>
                  )}
                </div>
              </div>
            ))}

            {/* Follow-up suggestion chips */}
            {followUps.length > 0 && !sending && (
              <div className="flex flex-wrap gap-2 pt-1 animate-fade-in">
                {followUps.map((q, i) => (
                  <button
                    key={i}
                    onClick={() => { setFollowUps([]); setInput(q); setTimeout(sendMessage, 50) }}
                    className="text-xs text-gray-300 bg-gray-800 hover:bg-gray-700 border border-gray-700 hover:border-gray-600 rounded-full px-3 py-1.5 transition-colors"
                  >
                    {q} →
                  </button>
                ))}
              </div>
            )}

            <div ref={bottomRef} />
          </div>

          {/* Scroll-to-bottom button */}
          {showScrollBtn && (
            <button
              onClick={scrollToBottom}
              className="absolute bottom-24 right-6 w-8 h-8 bg-gray-800 hover:bg-gray-700 border border-gray-700 rounded-full flex items-center justify-center text-gray-300 hover:text-white shadow-lg transition-all z-10"
            >
              ↓
            </button>
          )}

          {/* Input bar */}
          <div className="border-t border-gray-800 px-6 py-4 shrink-0">
            <div className="flex items-end gap-3 bg-gray-900 border border-gray-700 focus-within:border-gray-600 rounded-xl px-4 py-3 transition-colors">
              <textarea
                ref={inputRef}
                value={input}
                onChange={e => setInput(e.target.value)}
                onKeyDown={handleKeyDown}
                placeholder="Ask about your files… (Enter to send, Shift+Enter for new line)"
                rows={1}
                style={{ maxHeight: '120px', resize: 'none' }}
                className="flex-1 bg-transparent text-white text-sm placeholder-gray-500 focus:outline-none overflow-y-auto"
              />
              {/* Stop button while streaming / Send button otherwise */}
              {sending ? (
                <button
                  onClick={stopStreaming}
                  title="Stop generating"
                  className="shrink-0 bg-gray-700 hover:bg-red-600 text-white w-8 h-8 rounded-lg flex items-center justify-center transition-colors"
                >
                  <StopIcon />
                </button>
              ) : (
                <button
                  onClick={sendMessage}
                  disabled={!input.trim()}
                  className="shrink-0 bg-orange-500 hover:bg-orange-600 disabled:bg-gray-700 disabled:text-gray-500 text-white w-8 h-8 rounded-lg flex items-center justify-center transition-colors"
                >
                  ↑
                </button>
              )}
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
                        style={{ width: `${Math.min(100, Math.round((selectedCitation.score || 0) * 100))}%` }} />
                    </div>
                    <span className="text-gray-400 text-xs">{Math.min(100, Math.round((selectedCitation.score || 0) * 100))}%</span>
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

// ── Inline SVG icons (no icon library needed) ──────────────────────────────────

function CopyIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
    </svg>
  )
}

function RegenerateIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="1 4 1 10 7 10" />
      <path d="M3.51 15a9 9 0 1 0 .49-3.5" />
    </svg>
  )
}

function StopIcon() {
  return (
    <svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor">
      <rect x="3" y="3" width="18" height="18" rx="2" />
    </svg>
  )
}
