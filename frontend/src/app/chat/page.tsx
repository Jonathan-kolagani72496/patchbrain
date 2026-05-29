'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { useRouter } from 'next/navigation'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { API_URL, getAuthHeader, supabase } from '@/lib/supabase'

type Citation = {
  index: number
  chunk_id: string
  source_type?: string
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
  streaming?: boolean
  created_at?: string
}

type Session = {
  id: string
  title?: string
  message_count?: number
  updated_at?: string
}

function parseCitationList(value: unknown): Citation[] {
  if (Array.isArray(value)) return value as Citation[]
  if (typeof value === 'string') {
    try {
      const parsed = JSON.parse(value)
      return Array.isArray(parsed) ? parsed : []
    } catch {
      return []
    }
  }
  return []
}

function timeAgo(value?: string) {
  if (!value) return ''
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return ''
  const seconds = Math.floor((Date.now() - d.getTime()) / 1000)
  if (seconds < 60) return 'now'
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  return `${days}d ago`
}

function citationLabel(c: Citation) {
  const file = c.filename || 'source'
  if (c.sheet_name || c.row_start || c.row_end) return `${file} · rows ${c.row_start || '?'}-${c.row_end || '?'}`
  if (c.page_number) return `${file} · page ${c.page_number}`
  return file
}

function MessageSkeleton() {
  return (
    <div className="space-y-5 animate-pulse">
      {[70, 45, 86, 62].map((w, i) => (
        <div key={i} className={`flex ${i % 2 === 0 ? 'justify-end' : 'justify-start'}`}>
          <div className={`h-10 rounded-2xl bg-white/[0.06] ${i % 2 === 0 ? 'rounded-tr-md' : 'rounded-tl-md'}`} style={{ width: `${w}%` }} />
        </div>
      ))}
    </div>
  )
}

function MarkdownSegment({ children }: { children: string }) {
  if (!children) return null
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        p: ({ children }) => <p className="mb-3 text-sm leading-7 text-slate-200 last:mb-0">{children}</p>,
        h1: ({ children }) => <h1 className="mb-2 mt-4 text-lg font-semibold text-white">{children}</h1>,
        h2: ({ children }) => <h2 className="mb-2 mt-4 text-base font-semibold text-white">{children}</h2>,
        h3: ({ children }) => <h3 className="mb-2 mt-3 text-sm font-semibold text-white">{children}</h3>,
        ul: ({ children }) => <ul className="mb-3 ml-5 list-disc space-y-1 text-sm leading-6 text-slate-200">{children}</ul>,
        ol: ({ children }) => <ol className="mb-3 ml-5 list-decimal space-y-1 text-sm leading-6 text-slate-200">{children}</ol>,
        strong: ({ children }) => <strong className="font-semibold text-white">{children}</strong>,
        em: ({ children }) => <em className="text-slate-300">{children}</em>,
        code: ({ inline, children }: any) => inline
          ? <code className="rounded bg-slate-800 px-1.5 py-0.5 text-xs text-orange-200">{children}</code>
          : <pre className="mb-3 overflow-x-auto rounded-2xl border border-white/10 bg-slate-950 p-4 text-xs text-slate-300"><code>{children}</code></pre>,
        table: ({ children }) => <div className="mb-3 overflow-x-auto rounded-2xl border border-white/10"><table className="w-full border-collapse text-xs text-slate-300">{children}</table></div>,
        th: ({ children }) => <th className="border-b border-white/10 bg-slate-900 px-3 py-2 text-left font-semibold text-slate-100">{children}</th>,
        td: ({ children }) => <td className="border-b border-white/5 px-3 py-2">{children}</td>,
        blockquote: ({ children }) => <blockquote className="mb-3 border-l-2 border-orange-400/60 pl-3 text-slate-400">{children}</blockquote>,
      }}
    >
      {children}
    </ReactMarkdown>
  )
}

function AssistantContent({ content, citations, streaming, onCitationClick }: {
  content: string
  citations: Citation[]
  streaming?: boolean
  onCitationClick: (citation: Citation) => void
}) {
  if (streaming) {
    return (
      <p className="whitespace-pre-wrap text-sm leading-7 text-slate-200">
        {content}
        <span className="ml-1 inline-block h-4 w-0.5 animate-pulse bg-orange-400 align-text-bottom" />
      </p>
    )
  }

  const parts = content.split(/(\[S\d+\])/g)
  return (
    <div className="prose-chat">
      {parts.map((part, idx) => {
        const match = part.match(/^\[S(\d+)\]$/)
        if (!match) return <MarkdownSegment key={idx}>{part}</MarkdownSegment>
        const sourceIndex = Number(match[1])
        const citation = citations.find(c => Number(c.index) === sourceIndex)
        return (
          <button
            key={idx}
            onClick={() => citation && onCitationClick(citation)}
            disabled={!citation}
            className="mx-1 inline-flex items-center rounded-full border border-orange-400/20 bg-orange-400/10 px-2 py-0.5 text-xs font-semibold text-orange-200 transition hover:bg-orange-400/20 disabled:opacity-50"
          >
            [S{sourceIndex}]
          </button>
        )
      })}
    </div>
  )
}

export default function ChatPage() {
  const router = useRouter()
  const bottomRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const scrollAreaRef = useRef<HTMLDivElement>(null)
  const readerRef = useRef<ReadableStreamDefaultReader<Uint8Array> | null>(null)
  const lastQuestionRef = useRef('')

  const [sessions, setSessions] = useState<Session[]>([])
  const [activeSession, setActiveSession] = useState<string | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [loadingHistory, setLoadingHistory] = useState(false)
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [selectedCitation, setSelectedCitation] = useState<Citation | null>(null)
  const [companyName, setCompanyName] = useState('PatchBrain')
  const [error, setError] = useState('')
  const [followUps, setFollowUps] = useState<string[]>([])
  const [showScrollBtn, setShowScrollBtn] = useState(false)
  const [copiedId, setCopiedId] = useState<string | null>(null)

  const loadSessions = useCallback(async () => {
    try {
      const headers = await getAuthHeader()
      const res = await fetch(`${API_URL}/conversations`, { headers })
      if (res.ok) setSessions(await res.json())
    } catch {}
  }, [])

  const loadMe = useCallback(async () => {
    try {
      const headers = await getAuthHeader()
      const res = await fetch(`${API_URL}/me`, { headers })
      if (res.ok) {
        const data = await res.json()
        setCompanyName(data.company_name || 'PatchBrain')
      }
    } catch {}
  }, [])

  useEffect(() => {
    let alive = true
    supabase.auth.getSession().then(({ data }) => {
      if (!alive) return
      if (!data.session) router.replace('/login')
    })
    loadSessions()
    loadMe()
    return () => { alive = false }
  }, [loadMe, loadSessions, router])

  useEffect(() => {
    if (!showScrollBtn) bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, showScrollBtn])

  useEffect(() => {
    const el = inputRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 140)}px`
  }, [input])

  function handleScroll() {
    const el = scrollAreaRef.current
    if (!el) return
    setShowScrollBtn(el.scrollHeight - el.scrollTop - el.clientHeight > 220)
  }

  function scrollToBottom() {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
    setShowScrollBtn(false)
  }

  async function loadSession(sessionId: string) {
    setActiveSession(sessionId)
    setSelectedCitation(null)
    setFollowUps([])
    setError('')
    setLoadingHistory(true)
    setMessages([])

    try {
      const headers = await getAuthHeader()
      const res = await fetch(`${API_URL}/conversations/${sessionId}/messages`, { headers })
      if (!res.ok) throw new Error('Failed to load conversation')
      const data = await res.json()
      setMessages((data.messages || []).map((m: any) => ({
        id: m.id,
        role: m.role,
        content: m.content || '',
        citations: parseCitationList(m.citations_json),
        created_at: m.created_at,
      })))
    } catch (err: any) {
      setError(err?.message || 'Failed to load conversation')
    } finally {
      setLoadingHistory(false)
    }
  }

  function newChat() {
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
      if (activeSession === sessionId) newChat()
      await loadSessions()
    } catch {}
  }

  async function copyMessage(id: string, content: string) {
    try {
      await navigator.clipboard.writeText(content)
      setCopiedId(id)
      window.setTimeout(() => setCopiedId(null), 1600)
    } catch {}
  }

  function stopStreaming() {
    readerRef.current?.cancel()
    readerRef.current = null
    setSending(false)
    setMessages(prev => prev.map((m, i) => i === prev.length - 1 && m.role === 'assistant' ? { ...m, streaming: false } : m))
  }

  async function fetchFollowUps(question: string, answer: string) {
    try {
      const headers = await getAuthHeader()
      const res = await fetch(`${API_URL}/suggest-followups`, {
        method: 'POST',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify({ question, answer: answer.slice(0, 700) }),
      })
      if (res.ok) {
        const data = await res.json()
        setFollowUps((data.suggestions || []).slice(0, 3))
      }
    } catch {}
  }

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

    const userMsg: Message = { id: `${Date.now()}-u`, role: 'user', content: question }
    setMessages(prev => [...prev, userMsg])

    const assistantId = `${Date.now()}-a`
    let assistantText = ''
    let finalCitations: Citation[] = []

    try {
      const headers = await getAuthHeader()
      const res = await fetch(`${API_URL}/ask`, {
        method: 'POST',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify({ question, session_id: activeSession }),
      })

      if (res.status === 401) {
        router.replace('/login')
        return
      }

      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || 'Question failed')
      }

      if (!res.body) throw new Error('No response body')

      const reader = res.body.getReader()
      readerRef.current = reader
      const decoder = new TextDecoder()
      let buffer = ''

      while (true) {
        const { value, done } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const events = buffer.split('\n\n')
        buffer = events.pop() || ''

        for (const event of events) {
          const line = event.split('\n').find(row => row.startsWith('data: '))
          if (!line) continue

          try {
            const parsed = JSON.parse(line.replace(/^data:\s*/, ''))

            if (parsed.type === 'session' && parsed.session_id) {
              setActiveSession(parsed.session_id)
            }

            if (parsed.type === 'token') {
              assistantText += parsed.text || ''
              setMessages(prev => {
                const index = prev.findIndex(m => m.id === assistantId)
                if (index >= 0) {
                  const copy = [...prev]
                  copy[index] = { ...copy[index], content: assistantText, streaming: true }
                  return copy
                }
                return [...prev, { id: assistantId, role: 'assistant', content: assistantText, streaming: true }]
              })
            }

            if (parsed.type === 'done') {
              finalCitations = parseCitationList(parsed.citations)
              setMessages(prev => prev.map(m => m.id === assistantId ? { ...m, content: assistantText, citations: finalCitations, streaming: false } : m))
              window.setTimeout(loadSessions, 500)
              fetchFollowUps(question, assistantText)
            }

            if (parsed.type === 'error') {
              throw new Error(parsed.message || 'Backend returned an error')
            }
          } catch (err) {
            if (err instanceof SyntaxError) console.error('Bad SSE event:', line)
            else throw err
          }
        }
      }
    } catch (err: any) {
      if (err?.name === 'AbortError' || err?.message?.includes('cancel')) return
      const msg = err?.message || 'Connection error — is the backend running?'
      setError(msg)
      setMessages(prev => {
        const index = prev.findIndex(m => m.id === assistantId)
        if (index >= 0) {
          const copy = [...prev]
          copy[index] = { ...copy[index], content: msg, streaming: false }
          return copy
        }
        return [...prev, { id: assistantId, role: 'assistant', content: msg }]
      })
    } finally {
      readerRef.current = null
      setSending(false)
      inputRef.current?.focus()
    }
  }

  function regenerate() {
    if (!lastQuestionRef.current || sending) return
    setMessages(prev => {
      const copy = [...prev]
      if (copy[copy.length - 1]?.role === 'assistant') copy.pop()
      if (copy[copy.length - 1]?.role === 'user') copy.pop()
      return copy
    })
    setTimeout(() => sendMessageWithText(lastQuestionRef.current), 50)
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      sendMessage()
    }
  }

  async function signOut() {
    await supabase.auth.signOut()
    router.replace('/login')
  }

  const examples = [
    'What files do I have access to?',
    'What was Cimarron Unit 7 revenue by month?',
    'Compare Reeves 14H and Cimarron Unit 7 by barrels hauled.',
    'Which driver generated the most revenue?',
  ]

  return (
    <main className="h-screen overflow-hidden bg-slate-950 text-white lg:grid lg:grid-cols-[280px_1fr]">
      <aside className="hidden border-r border-white/10 bg-slate-950/90 lg:flex lg:flex-col">
        <div className="border-b border-white/10 p-4">
          <div className="mb-4 flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-2xl bg-orange-500 shadow-lg shadow-orange-500/20">
              <span className="font-black">P</span>
            </div>
            <div className="min-w-0">
              <p className="truncate text-sm font-semibold">{companyName}</p>
              <p className="text-xs text-slate-500">PatchBrain Chat</p>
            </div>
          </div>
          <button onClick={newChat} className="w-full rounded-xl bg-orange-500 px-4 py-2.5 text-sm font-semibold shadow-lg shadow-orange-500/20 transition hover:bg-orange-400">
            + New chat
          </button>
        </div>

        <div className="custom-scrollbar flex-1 overflow-y-auto p-2">
          {sessions.length === 0 ? (
            <p className="px-3 py-5 text-xs text-slate-600">No conversations yet</p>
          ) : sessions.map(session => (
            <div
              key={session.id}
              onClick={() => loadSession(session.id)}
              className={`group mb-1 cursor-pointer rounded-2xl px-3 py-3 transition hover:bg-white/[0.06] ${activeSession === session.id ? 'bg-white/[0.08]' : ''}`}
            >
              <div className="flex items-start gap-2">
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm text-slate-200">{session.title || 'New conversation'}</p>
                  <p className="mt-1 text-xs text-slate-600">{timeAgo(session.updated_at)}</p>
                </div>
                <button onClick={e => deleteSession(session.id, e)} className="opacity-0 rounded-lg px-1.5 text-slate-600 transition hover:bg-red-500/10 hover:text-red-300 group-hover:opacity-100">
                  ×
                </button>
              </div>
            </div>
          ))}
        </div>

        <div className="border-t border-white/10 p-3 space-y-1">
          <button onClick={() => router.push('/dashboard')} className="w-full rounded-xl px-3 py-2 text-left text-sm text-slate-400 transition hover:bg-white/[0.06] hover:text-white">← File Library</button>
          <button onClick={signOut} className="w-full rounded-xl px-3 py-2 text-left text-sm text-slate-500 transition hover:bg-white/[0.06] hover:text-white">Sign out</button>
        </div>
      </aside>

      <section className="flex h-screen min-w-0 flex-col">
        <header className="flex items-center justify-between border-b border-white/10 bg-slate-950/85 px-4 py-3 backdrop-blur lg:px-6">
          <div>
            <p className="text-sm font-semibold">Ask PatchBrain</p>
            <p className="text-xs text-slate-500">Answers from your uploaded files, tools, and citations.</p>
          </div>
          <div className="flex items-center gap-2 lg:hidden">
            <button onClick={() => router.push('/dashboard')} className="rounded-xl border border-white/10 px-3 py-2 text-xs text-slate-300">Files</button>
            <button onClick={signOut} className="rounded-xl border border-white/10 px-3 py-2 text-xs text-slate-300">Sign out</button>
          </div>
        </header>

        {error && (
          <div className="border-b border-red-400/20 bg-red-400/10 px-6 py-3 text-sm text-red-200">
            {error}
          </div>
        )}

        <div className="flex min-h-0 flex-1">
          <div className="relative flex min-w-0 flex-1 flex-col">
            <div ref={scrollAreaRef} onScroll={handleScroll} className="custom-scrollbar flex-1 overflow-y-auto px-4 py-6 lg:px-8">
              {!loadingHistory && messages.length === 0 && (
                <div className="mx-auto flex min-h-full max-w-2xl flex-col items-center justify-center text-center">
                  <div className="mb-5 flex h-14 w-14 items-center justify-center rounded-3xl border border-orange-400/20 bg-orange-400/10 text-xl text-orange-200">⚡</div>
                  <h1 className="text-2xl font-semibold tracking-tight">Ask about your operations documents</h1>
                  <p className="mt-3 max-w-md text-sm leading-6 text-slate-400">
                    Use natural language for file inventory, exact searches, structured calculations, summaries, and source-backed answers.
                  </p>
                  <div className="mt-7 grid w-full gap-2 sm:grid-cols-2">
                    {examples.map(example => (
                      <button key={example} onClick={() => { setInput(example); inputRef.current?.focus() }} className="rounded-2xl border border-white/10 bg-white/[0.035] px-4 py-3 text-left text-sm text-slate-300 transition hover:border-orange-400/30 hover:bg-orange-400/10 hover:text-white">
                        {example}
                      </button>
                    ))}
                  </div>
                </div>
              )}

              {loadingHistory && <MessageSkeleton />}

              {!loadingHistory && messages.map(msg => (
                <div key={msg.id} className={`mb-6 flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                  <div className={`max-w-3xl ${msg.role === 'user' ? 'rounded-3xl rounded-tr-md bg-orange-500 px-5 py-3 text-white shadow-lg shadow-orange-500/10' : 'w-full rounded-3xl rounded-tl-md border border-white/10 bg-white/[0.04] px-5 py-4'}`}>
                    {msg.role === 'user' ? (
                      <p className="whitespace-pre-wrap text-sm leading-6">{msg.content}</p>
                    ) : (
                      <AssistantContent content={msg.content} citations={msg.citations || []} streaming={msg.streaming} onCitationClick={setSelectedCitation} />
                    )}
                    {msg.role === 'assistant' && !msg.streaming && (
                      <div className="mt-3 flex items-center gap-2 border-t border-white/10 pt-3">
                        <button onClick={() => copyMessage(msg.id, msg.content)} className="rounded-lg px-2 py-1 text-xs text-slate-500 transition hover:bg-white/5 hover:text-slate-200">
                          {copiedId === msg.id ? 'Copied' : 'Copy'}
                        </button>
                        <button onClick={regenerate} disabled={sending} className="rounded-lg px-2 py-1 text-xs text-slate-500 transition hover:bg-white/5 hover:text-slate-200 disabled:opacity-40">
                          Regenerate
                        </button>
                      </div>
                    )}
                  </div>
                </div>
              ))}

              {followUps.length > 0 && !sending && (
                <div className="mx-auto mb-6 flex max-w-3xl flex-wrap gap-2">
                  {followUps.map(q => (
                    <button key={q} onClick={() => sendMessageWithText(q)} className="rounded-full border border-white/10 bg-white/[0.04] px-3 py-1.5 text-xs text-slate-300 transition hover:border-orange-400/30 hover:bg-orange-400/10 hover:text-white">
                      {q}
                    </button>
                  ))}
                </div>
              )}

              <div ref={bottomRef} />
            </div>

            {showScrollBtn && (
              <button onClick={scrollToBottom} className="absolute bottom-28 left-1/2 -translate-x-1/2 rounded-full border border-white/10 bg-slate-900 px-3 py-2 text-xs text-slate-300 shadow-xl transition hover:bg-slate-800">
                Jump to latest
              </button>
            )}

            <div className="border-t border-white/10 bg-slate-950/90 px-4 py-4 backdrop-blur lg:px-8">
              <div className="mx-auto flex max-w-4xl items-end gap-3 rounded-3xl border border-white/10 bg-white/[0.04] p-2 shadow-2xl shadow-black/20">
                <textarea
                  ref={inputRef}
                  value={input}
                  onChange={e => setInput(e.target.value)}
                  onKeyDown={handleKeyDown}
                  disabled={sending}
                  rows={1}
                  placeholder="Ask about tickets, invoices, compliance reports, or field activity…"
                  className="custom-scrollbar max-h-36 min-h-[44px] flex-1 resize-none bg-transparent px-4 py-3 text-sm text-white outline-none placeholder:text-slate-600 disabled:opacity-60"
                />
                {sending ? (
                  <button onClick={stopStreaming} className="mb-0.5 rounded-2xl border border-red-400/20 bg-red-500/10 px-4 py-3 text-sm font-semibold text-red-200 transition hover:bg-red-500/15">
                    Stop
                  </button>
                ) : (
                  <button onClick={sendMessage} disabled={!input.trim()} className="mb-0.5 rounded-2xl bg-orange-500 px-4 py-3 text-sm font-semibold text-white shadow-lg shadow-orange-500/20 transition hover:bg-orange-400 disabled:cursor-not-allowed disabled:opacity-40">
                    Send
                  </button>
                )}
              </div>
              <p className="mx-auto mt-2 max-w-4xl text-xs text-slate-600">Enter sends. Shift + Enter adds a new line.</p>
            </div>
          </div>

          <aside className="hidden w-96 shrink-0 border-l border-white/10 bg-slate-950/80 xl:block">
            <div className="border-b border-white/10 p-4">
              <p className="text-sm font-semibold">Source evidence</p>
              <p className="mt-1 text-xs text-slate-500">Click a citation in an answer to inspect the source preview.</p>
            </div>
            <div className="custom-scrollbar h-[calc(100vh-73px)] overflow-y-auto p-4">
              {selectedCitation ? (
                <div className="rounded-3xl border border-white/10 bg-white/[0.04] p-4">
                  <div className="mb-3 flex items-start justify-between gap-3">
                    <div>
                      <p className="text-xs font-medium uppercase tracking-[0.18em] text-orange-300">[S{selectedCitation.index}]</p>
                      <h3 className="mt-1 text-sm font-semibold text-white">{citationLabel(selectedCitation)}</h3>
                    </div>
                    <button onClick={() => setSelectedCitation(null)} className="rounded-lg px-2 py-1 text-slate-500 transition hover:bg-white/5 hover:text-white">×</button>
                  </div>
                  <dl className="mb-4 grid grid-cols-2 gap-3 text-xs">
                    <div className="rounded-2xl bg-slate-950/60 p-3">
                      <dt className="text-slate-600">Type</dt>
                      <dd className="mt-1 text-slate-300">{selectedCitation.source_type || '—'}</dd>
                    </div>
                    <div className="rounded-2xl bg-slate-950/60 p-3">
                      <dt className="text-slate-600">Score</dt>
                      <dd className="mt-1 text-slate-300">{typeof selectedCitation.score === 'number' ? selectedCitation.score.toFixed(3) : '—'}</dd>
                    </div>
                  </dl>
                  <pre className="whitespace-pre-wrap rounded-2xl border border-white/10 bg-slate-950 p-4 text-xs leading-6 text-slate-300">
                    {selectedCitation.preview || 'No preview returned for this citation.'}
                  </pre>
                </div>
              ) : (
                <div className="rounded-3xl border border-dashed border-white/10 p-8 text-center text-sm text-slate-500">
                  No source selected.
                </div>
              )}
            </div>
          </aside>
        </div>
      </section>
    </main>
  )
}
