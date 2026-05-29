'use client'

/**
 * PatchBrain Chat — production-grade rewrite
 *
 * Key fixes over v1:
 * - Smart autoscroll: ResizeObserver-based, pauses when user scrolls up, resumes at bottom
 * - Streaming: stays as plain text during stream, parses markdown only after done (no layout jump)
 * - Textarea: useLayoutEffect for accurate height, handles IME composition
 * - Citation panel: slide-over drawer on all viewports (not xl-only)
 * - File attachment: drag-onto-textarea, paste image, click paperclip
 * - Error state: separate from message content — never overwrites assistant text
 * - Keyboard: Cmd/Ctrl+Enter sends, Escape stops stream, / focuses input
 * - Mobile sidebar: proper sheet with backdrop
 * - Follow-up chips: inline after last assistant message
 * - Feedback: thumbs up/down per message, stored locally
 * - Regenerate: properly strips last Q+A pair before re-sending
 * - Empty state: example prompts pre-populated per domain
 */

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  useTransition,
} from 'react'
import { useRouter } from 'next/navigation'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { API_URL, getAuthHeader, supabase } from '@/lib/supabase'

/* ═══════════════════════════════════════════════════════════════════════════
   Types
   ═══════════════════════════════════════════════════════════════════════════ */

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

type Feedback = 'up' | 'down' | null

type Message = {
  id: string
  role: 'user' | 'assistant'
  content: string
  citations?: Citation[]
  attachments?: AttachmentMeta[]
  streaming?: boolean
  error?: boolean       // true = error state, content is error message
  feedback?: Feedback
  created_at?: string
}

type Session = {
  id: string
  title?: string
  message_count?: number
  updated_at?: string
}

type AttachmentMeta = {
  id: string
  name: string
  size: number
  type: string
  file?: File   // only present pre-send
}

/* ═══════════════════════════════════════════════════════════════════════════
   Helpers
   ═══════════════════════════════════════════════════════════════════════════ */

function parseCitationList(value: unknown): Citation[] {
  if (Array.isArray(value)) return value as Citation[]
  if (typeof value === 'string') {
    try {
      const p = JSON.parse(value)
      return Array.isArray(p) ? p : []
    } catch { return [] }
  }
  return []
}

function timeAgo(value?: string) {
  if (!value) return ''
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return ''
  const s = Math.floor((Date.now() - d.getTime()) / 1000)
  if (s < 60) return 'just now'
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
}

function citationLabel(c: Citation) {
  const file = c.filename || 'source'
  if (c.sheet_name) return `${file} · ${c.sheet_name} rows ${c.row_start ?? '?'}–${c.row_end ?? '?'}`
  if (c.page_number) return `${file} · p. ${c.page_number}`
  return file
}

function formatBytes(n: number) {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

function uid() {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`
}

/* ═══════════════════════════════════════════════════════════════════════════
   Hooks
   ═══════════════════════════════════════════════════════════════════════════ */

/**
 * Smart autoscroll.
 *
 * - Auto-scrolls to bottom whenever content grows, BUT only if the user
 *   is already within `threshold` px of the bottom.
 * - Exposes `isAtBottom` and `scrollToBottom` for the "jump to latest" btn.
 * - Uses ResizeObserver (not message count) so it works during streaming.
 */
function useAutoScroll(threshold = 120) {
  const containerRef = useRef<HTMLDivElement>(null)
  const [isAtBottom, setIsAtBottom] = useState(true)
  const isAtBottomRef = useRef(true)

  const checkIsAtBottom = useCallback(() => {
    const el = containerRef.current
    if (!el) return
    const dist = el.scrollHeight - el.scrollTop - el.clientHeight
    const atBottom = dist < threshold
    isAtBottomRef.current = atBottom
    setIsAtBottom(atBottom)
  }, [threshold])

  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    el.addEventListener('scroll', checkIsAtBottom, { passive: true })
    return () => el.removeEventListener('scroll', checkIsAtBottom)
  }, [checkIsAtBottom])

  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const ro = new ResizeObserver(() => {
      if (isAtBottomRef.current) {
        el.scrollTop = el.scrollHeight
      }
    })
    // Observe the first child (the content wrapper), not the container itself
    if (el.firstElementChild) ro.observe(el.firstElementChild)
    return () => ro.disconnect()
  }, [])

  const scrollToBottom = useCallback((behavior: ScrollBehavior = 'smooth') => {
    const el = containerRef.current
    if (!el) return
    el.scrollTo({ top: el.scrollHeight, behavior })
    isAtBottomRef.current = true
    setIsAtBottom(true)
  }, [])

  return { containerRef, isAtBottom, scrollToBottom }
}

/* ═══════════════════════════════════════════════════════════════════════════
   Sub-components
   ═══════════════════════════════════════════════════════════════════════════ */

function Spinner({ size = 16, color = 'currentColor' }: { size?: number; color?: string }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
      style={{ animation: 'pb-spin 0.75s linear infinite', flexShrink: 0 }}
    >
      <circle cx="12" cy="12" r="10" stroke={color} strokeOpacity={0.2} strokeWidth="3" />
      <path d="M12 2a10 10 0 0110 10" stroke={color} strokeWidth="3" strokeLinecap="round" />
    </svg>
  )
}

function MarkdownRenderer({ content }: { content: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        p: ({ children }) => (
          <p style={{ margin: '0 0 0.75rem', fontSize: 14, lineHeight: 1.8, color: 'var(--fg)', lastChild: 'margin:0' } as any}>
            {children}
          </p>
        ),
        h1: ({ children }) => <h1 style={{ margin: '1.25rem 0 0.5rem', fontSize: 17, fontWeight: 600, color: 'var(--fg)' }}>{children}</h1>,
        h2: ({ children }) => <h2 style={{ margin: '1.1rem 0 0.4rem', fontSize: 15, fontWeight: 600, color: 'var(--fg)' }}>{children}</h2>,
        h3: ({ children }) => <h3 style={{ margin: '1rem 0 0.35rem', fontSize: 14, fontWeight: 600, color: 'var(--fg)' }}>{children}</h3>,
        ul: ({ children }) => <ul style={{ margin: '0 0 0.75rem', paddingLeft: '1.4rem', fontSize: 14, lineHeight: 1.8, color: 'var(--fg)' }}>{children}</ul>,
        ol: ({ children }) => <ol style={{ margin: '0 0 0.75rem', paddingLeft: '1.4rem', fontSize: 14, lineHeight: 1.8, color: 'var(--fg)' }}>{children}</ol>,
        li: ({ children }) => <li style={{ marginBottom: '0.2rem' }}>{children}</li>,
        strong: ({ children }) => <strong style={{ fontWeight: 600, color: 'var(--fg)' }}>{children}</strong>,
        em: ({ children }) => <em style={{ color: 'var(--fg-muted)' }}>{children}</em>,
        code: ({ inline, children, ...props }: any) =>
          inline ? (
            <code style={{
              padding: '1px 6px', borderRadius: 5, fontSize: 12,
              background: 'rgba(249,115,22,0.10)', color: '#fdba74',
              fontFamily: 'var(--font-mono, "IBM Plex Mono", monospace)',
              border: '1px solid rgba(249,115,22,0.15)',
            }}>
              {children}
            </code>
          ) : (
            <pre style={{
              margin: '0.75rem 0', padding: '14px 16px', borderRadius: 10, overflowX: 'auto',
              background: 'rgba(0,0,0,0.35)', border: '1px solid rgba(255,255,255,0.07)',
              fontSize: 12, lineHeight: 1.7, color: '#e2e8f0',
              fontFamily: 'var(--font-mono, "IBM Plex Mono", monospace)',
            }}>
              <code>{children}</code>
            </pre>
          ),
        table: ({ children }) => (
          <div style={{ margin: '0.75rem 0', overflowX: 'auto', borderRadius: 10, border: '1px solid rgba(255,255,255,0.08)' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>{children}</table>
          </div>
        ),
        th: ({ children }) => (
          <th style={{
            padding: '8px 12px', textAlign: 'left', fontSize: 11,
            fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.08em',
            background: 'rgba(255,255,255,0.04)', color: 'var(--fg-muted)',
            borderBottom: '1px solid rgba(255,255,255,0.08)',
          }}>{children}</th>
        ),
        td: ({ children }) => (
          <td style={{
            padding: '8px 12px', borderBottom: '1px solid rgba(255,255,255,0.04)',
            color: 'var(--fg)',
          }}>{children}</td>
        ),
        blockquote: ({ children }) => (
          <blockquote style={{
            margin: '0.75rem 0', padding: '10px 14px',
            borderLeft: '3px solid rgba(249,115,22,0.5)',
            background: 'rgba(249,115,22,0.05)',
            borderRadius: '0 8px 8px 0',
            color: 'var(--fg-muted)', fontSize: 14,
          }}>{children}</blockquote>
        ),
        a: ({ href, children }) => (
          <a href={href} target="_blank" rel="noopener noreferrer"
            style={{ color: '#fb923c', textDecoration: 'underline', textUnderlineOffset: 2 }}>
            {children}
          </a>
        ),
        hr: () => <hr style={{ margin: '1rem 0', border: 'none', borderTop: '1px solid rgba(255,255,255,0.07)' }} />,
      }}
    >
      {content}
    </ReactMarkdown>
  )
}

function AssistantContent({
  message,
  onCitationClick,
}: {
  message: Message
  onCitationClick: (c: Citation) => void
}) {
  const { content, citations = [], streaming, error } = message

  // Error state
  if (error) {
    return (
      <div style={{
        display: 'flex', alignItems: 'flex-start', gap: 10,
        padding: '10px 14px', borderRadius: 10,
        background: 'rgba(248,113,113,0.08)', border: '1px solid rgba(248,113,113,0.18)',
        color: '#fca5a5', fontSize: 13,
      }}>
        <span aria-hidden="true" style={{ flexShrink: 0, marginTop: 1 }}>⚠</span>
        <span>{content}</span>
      </div>
    )
  }

  // During streaming: plain text + cursor — avoids markdown parse thrash
  if (streaming) {
    return (
      <p style={{ margin: 0, fontSize: 14, lineHeight: 1.8, color: 'var(--fg)', whiteSpace: 'pre-wrap' }}>
        {content}
        <span
          aria-hidden="true"
          style={{
            display: 'inline-block', width: 2, height: '1em',
            background: 'var(--brand)', marginLeft: 2,
            animation: 'pb-blink 1s step-end infinite',
            verticalAlign: 'text-bottom',
          }}
        />
      </p>
    )
  }

  // Finished: split on [S{n}] citation markers, render markdown + citation chips
  const parts = content.split(/(\[S\d+\])/g)
  const hasCitations = citations.length > 0
  const hasCitationMarkers = parts.some(p => /^\[S\d+\]$/.test(p))

  return (
    <div>
      <div className="pb-prose">
        {parts.map((part, idx) => {
          const match = part.match(/^\[S(\d+)\]$/)
          if (!match) return <MarkdownRenderer key={idx} content={part} />
          const srcIdx = Number(match[1])
          const cit = citations.find(c => Number(c.index) === srcIdx)
          return (
            <button
              key={idx}
              onClick={() => cit && onCitationClick(cit)}
              disabled={!cit}
              aria-label={cit ? `View source ${srcIdx}: ${citationLabel(cit)}` : `Source ${srcIdx}`}
              style={{
                display: 'inline-flex', alignItems: 'center', gap: 4,
                margin: '0 2px', padding: '1px 7px 1px 5px',
                borderRadius: 20, fontSize: 11, fontWeight: 600,
                background: 'rgba(249,115,22,0.12)',
                color: '#fdba74',
                border: '1px solid rgba(249,115,22,0.22)',
                cursor: cit ? 'pointer' : 'default',
                transition: 'all 0.15s',
                verticalAlign: 'middle',
              }}
              onMouseEnter={e => { if (cit) (e.currentTarget as HTMLButtonElement).style.background = 'rgba(249,115,22,0.22)' }}
              onMouseLeave={e => { if (cit) (e.currentTarget as HTMLButtonElement).style.background = 'rgba(249,115,22,0.12)' }}
            >
              <span style={{ width: 5, height: 5, borderRadius: '50%', background: '#f97316', flexShrink: 0 }} />
              S{srcIdx}
            </button>
          )
        })}
      </div>

      {/* Inline citation list at bottom of message — shown when no [S{n}] markers inline */}
      {hasCitations && !hasCitationMarkers && (
        <div style={{ marginTop: 12, display: 'flex', flexWrap: 'wrap', gap: 6 }}>
          {citations.map(c => (
            <button
              key={c.chunk_id}
              onClick={() => onCitationClick(c)}
              aria-label={`View source: ${citationLabel(c)}`}
              style={{
                display: 'inline-flex', alignItems: 'center', gap: 5,
                padding: '4px 10px', borderRadius: 20, fontSize: 11, fontWeight: 500,
                background: 'rgba(249,115,22,0.08)', color: '#fdba74',
                border: '1px solid rgba(249,115,22,0.18)',
                cursor: 'pointer', transition: 'all 0.15s',
              }}
              onMouseEnter={e => (e.currentTarget.style.background = 'rgba(249,115,22,0.16)')}
              onMouseLeave={e => (e.currentTarget.style.background = 'rgba(249,115,22,0.08)')}
            >
              <svg width="10" height="10" viewBox="0 0 10 10" fill="none" aria-hidden="true">
                <path d="M1 9L9 1M9 1H4M9 1V6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
              {citationLabel(c)}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

function MessageSkeleton() {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
      {[
        { align: 'flex-end', w: '58%', h: 44 },
        { align: 'flex-start', w: '72%', h: 80 },
        { align: 'flex-end', w: '44%', h: 36 },
        { align: 'flex-start', w: '66%', h: 56 },
      ].map((s, i) => (
        <div key={i} style={{ display: 'flex', justifyContent: s.align }}>
          <div style={{
            width: s.w, height: s.h, borderRadius: 16,
            background: 'rgba(255,255,255,0.04)',
            animation: `pb-shimmer 1.5s ease-in-out ${i * 0.1}s infinite`,
          }} />
        </div>
      ))}
    </div>
  )
}

function AttachmentChip({
  meta,
  onRemove,
}: {
  meta: AttachmentMeta
  onRemove?: () => void
}) {
  const ext = meta.name.split('.').pop()?.toUpperCase() || 'FILE'
  return (
    <div style={{
      display: 'inline-flex', alignItems: 'center', gap: 8,
      padding: '5px 10px', borderRadius: 8,
      background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(255,255,255,0.09)',
      fontSize: 12, color: 'var(--fg-muted)', maxWidth: 220, flexShrink: 0,
    }}>
      <span style={{
        padding: '1px 5px', borderRadius: 4, fontSize: 10, fontWeight: 700,
        background: 'rgba(249,115,22,0.15)', color: '#fdba74',
        letterSpacing: '0.04em', flexShrink: 0,
      }}>
        {ext}
      </span>
      <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>
        {meta.name}
      </span>
      <span style={{ color: 'var(--fg-subtle)', flexShrink: 0, fontSize: 11 }}>
        {formatBytes(meta.size)}
      </span>
      {onRemove && (
        <button
          onClick={onRemove}
          aria-label={`Remove ${meta.name}`}
          style={{
            background: 'none', border: 'none', cursor: 'pointer',
            color: 'var(--fg-subtle)', padding: '0 1px', lineHeight: 1,
            fontSize: 14, flexShrink: 0, display: 'flex', alignItems: 'center',
          }}
          onMouseEnter={e => (e.currentTarget.style.color = '#f87171')}
          onMouseLeave={e => (e.currentTarget.style.color = 'var(--fg-subtle)')}
        >
          ×
        </button>
      )}
    </div>
  )
}

function CitationPanel({
  citation,
  onClose,
}: {
  citation: Citation | null
  onClose: () => void
}) {
  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      {/* Header */}
      <div style={{
        padding: '14px 16px', borderBottom: '1px solid var(--border)',
        display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexShrink: 0,
      }}>
        <div>
          <p style={{ fontSize: 11, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.14em', color: 'var(--brand-light)', margin: 0 }}>
            Source evidence
          </p>
          <p style={{ fontSize: 12, color: 'var(--fg-subtle)', margin: '2px 0 0' }}>
            {citation ? citationLabel(citation) : 'Click any citation to inspect'}
          </p>
        </div>
        {citation && (
          <button
            onClick={onClose}
            aria-label="Close citation panel"
            style={{
              width: 28, height: 28, borderRadius: 8, border: '1px solid var(--border)',
              background: 'transparent', cursor: 'pointer', display: 'flex',
              alignItems: 'center', justifyContent: 'center', color: 'var(--fg-subtle)',
              fontSize: 16, transition: 'all 0.15s',
            }}
            onMouseEnter={e => { e.currentTarget.style.background = 'rgba(255,255,255,0.06)'; e.currentTarget.style.color = 'var(--fg)' }}
            onMouseLeave={e => { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.color = 'var(--fg-subtle)' }}
          >
            ×
          </button>
        )}
      </div>

      {/* Body */}
      <div style={{ flex: 1, overflowY: 'auto', padding: 16 }}>
        {citation ? (
          <div>
            {/* Meta grid */}
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, marginBottom: 14 }}>
              {[
                { label: 'Source type', value: citation.source_type || '—' },
                { label: 'Relevance', value: typeof citation.score === 'number' ? (citation.score * 100).toFixed(1) + '%' : '—' },
                ...(citation.page_number ? [{ label: 'Page', value: String(citation.page_number) }] : []),
                ...(citation.sheet_name ? [{ label: 'Sheet', value: citation.sheet_name }] : []),
                ...(citation.row_start ? [{ label: 'Rows', value: `${citation.row_start}–${citation.row_end ?? '?'}` }] : []),
              ].map(item => (
                <div key={item.label} style={{
                  padding: '9px 12px', borderRadius: 8,
                  background: 'rgba(255,255,255,0.03)', border: '1px solid var(--border)',
                }}>
                  <p style={{ margin: 0, fontSize: 10, fontWeight: 500, textTransform: 'uppercase', letterSpacing: '0.1em', color: 'var(--fg-subtle)' }}>
                    {item.label}
                  </p>
                  <p style={{ margin: '4px 0 0', fontSize: 13, color: 'var(--fg-muted)', fontWeight: 500 }}>
                    {item.value}
                  </p>
                </div>
              ))}
            </div>

            {/* Relevance bar */}
            {typeof citation.score === 'number' && (
              <div style={{ marginBottom: 14 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 5 }}>
                  <span style={{ fontSize: 11, color: 'var(--fg-subtle)' }}>Relevance score</span>
                  <span style={{ fontSize: 11, color: 'var(--fg-muted)', fontWeight: 500 }}>{(citation.score * 100).toFixed(1)}%</span>
                </div>
                <div style={{ height: 4, borderRadius: 4, background: 'rgba(255,255,255,0.06)', overflow: 'hidden' }}>
                  <div style={{
                    height: '100%', borderRadius: 4,
                    width: `${Math.min(citation.score * 100, 100)}%`,
                    background: citation.score > 0.8
                      ? '#22c55e'
                      : citation.score > 0.6
                      ? '#f97316'
                      : '#64748b',
                    transition: 'width 0.4s ease',
                  }} />
                </div>
              </div>
            )}

            {/* Preview text */}
            <div>
              <p style={{ fontSize: 11, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.1em', color: 'var(--fg-subtle)', marginBottom: 8 }}>
                Text excerpt
              </p>
              <pre style={{
                margin: 0, padding: 14, borderRadius: 10, overflowX: 'hidden',
                border: '1px solid rgba(255,255,255,0.06)',
                background: 'rgba(0,0,0,0.2)',
                fontSize: 12, lineHeight: 1.8, color: 'var(--fg-muted)',
                fontFamily: 'var(--font-mono, "IBM Plex Mono", monospace)',
                whiteSpace: 'pre-wrap', wordBreak: 'break-word',
              }}>
                {citation.preview || 'No excerpt available for this source.'}
              </pre>
            </div>
          </div>
        ) : (
          <div style={{
            height: '100%', display: 'flex', flexDirection: 'column',
            alignItems: 'center', justifyContent: 'center',
            textAlign: 'center', color: 'var(--fg-subtle)',
            padding: '40px 16px',
          }}>
            <div style={{
              width: 44, height: 44, borderRadius: 12, marginBottom: 14,
              background: 'rgba(249,115,22,0.06)', border: '1px solid rgba(249,115,22,0.12)',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
            }}>
              <svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true">
                <path d="M9 1.5L11.09 6.26L16.5 7.27L12.75 11.14L13.59 16.5L9 14.27L4.41 16.5L5.25 11.14L1.5 7.27L6.91 6.26L9 1.5Z"
                  stroke="rgba(249,115,22,0.5)" strokeWidth="1.5" strokeLinejoin="round"/>
              </svg>
            </div>
            <p style={{ margin: '0 0 6px', fontSize: 13, fontWeight: 500, color: 'var(--fg-muted)' }}>
              No source selected
            </p>
            <p style={{ margin: 0, fontSize: 12, lineHeight: 1.6 }}>
              Click any citation marker in an answer to inspect the source document excerpt and relevance score.
            </p>
          </div>
        )}
      </div>
    </div>
  )
}

/* ═══════════════════════════════════════════════════════════════════════════
   ChatPage
   ═══════════════════════════════════════════════════════════════════════════ */

const EXAMPLE_PROMPTS = [
  'What files do I have access to?',
  'Cimarron Unit 7 revenue by month?',
  'Compare Reeves 14H vs Cimarron Unit 7 by barrels hauled',
  'Which driver generated the most revenue?',
  'Show me any compliance issues flagged in field reports',
  'What invoices are still unpaid?',
]

export default function ChatPage() {
  const router = useRouter()

  /* ── Refs ── */
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const readerRef = useRef<ReadableStreamDefaultReader<Uint8Array> | null>(null)
  const lastQuestionRef = useRef('')
  const composingRef = useRef(false) // IME composition guard
  const liveRef = useRef<HTMLDivElement>(null)

  /* ── Autoscroll ── */
  const { containerRef: scrollRef, isAtBottom, scrollToBottom } = useAutoScroll()

  /* ── State ── */
  const [sessions, setSessions] = useState<Session[]>([])
  const [activeSession, setActiveSession] = useState<string | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [loadingHistory, setLoadingHistory] = useState(false)
  const [input, setInput] = useState('')
  const [attachments, setAttachments] = useState<AttachmentMeta[]>([])
  const [sending, setSending] = useState(false)
  const [selectedCitation, setSelectedCitation] = useState<Citation | null>(null)
  const [citationPanelOpen, setCitationPanelOpen] = useState(false)
  const [companyName, setCompanyName] = useState('PatchBrain')
  const [globalError, setGlobalError] = useState('')
  const [followUps, setFollowUps] = useState<string[]>([])
  const [copiedId, setCopiedId] = useState<string | null>(null)
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false)
  const [, startTransition] = useTransition()

  /* ── Textarea auto-height ── */
  useLayoutEffect(() => {
    const el = inputRef.current
    if (!el) return
    el.style.height = 'auto'
    const next = Math.min(el.scrollHeight, 180)
    el.style.height = `${next}px`
  }, [input])

  /* ── Keyboard shortcuts ── */
  useEffect(() => {
    function handler(e: KeyboardEvent) {
      // / to focus input (when not already focused and not in another input)
      if (e.key === '/' && document.activeElement?.tagName !== 'TEXTAREA' && document.activeElement?.tagName !== 'INPUT') {
        e.preventDefault()
        inputRef.current?.focus()
      }
      // Escape to stop streaming
      if (e.key === 'Escape' && sending) {
        stopStreaming()
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [sending])

  /* ── Init ── */
  useEffect(() => {
    let alive = true
    supabase.auth.getSession().then(({ data }) => {
      if (!alive) return
      if (!data.session) router.replace('/login')
    })
    loadSessions()
    fetchMe()
    return () => { alive = false }
  }, [])

  /* ── Scroll to bottom on new messages (only if already at bottom) ── */
  useEffect(() => {
    if (isAtBottom) scrollToBottom('auto')
  }, [messages.length])

  /* ── Fns ── */

  async function loadSessions() {
    try {
      const headers = await getAuthHeader()
      const res = await fetch(`${API_URL}/conversations`, { headers })
      if (res.ok) setSessions(await res.json())
    } catch {}
  }

  async function fetchMe() {
    try {
      const headers = await getAuthHeader()
      const res = await fetch(`${API_URL}/me`, { headers })
      if (res.ok) {
        const data = await res.json()
        setCompanyName(data.company_name || 'PatchBrain')
      }
    } catch {}
  }

  async function loadSession(sessionId: string) {
    setActiveSession(sessionId)
    setSelectedCitation(null)
    setFollowUps([])
    setGlobalError('')
    setLoadingHistory(true)
    setMessages([])
    setMobileSidebarOpen(false)

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
      // Jump to bottom after loading history
      setTimeout(() => scrollToBottom('auto'), 50)
    } catch (err: any) {
      setGlobalError(err?.message || 'Failed to load conversation')
    } finally {
      setLoadingHistory(false)
    }
  }

  function newChat() {
    setActiveSession(null)
    setMessages([])
    setFollowUps([])
    setSelectedCitation(null)
    setCitationPanelOpen(false)
    setGlobalError('')
    setAttachments([])
    setInput('')
    setMobileSidebarOpen(false)
    setTimeout(() => inputRef.current?.focus(), 50)
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
      announce('Message copied to clipboard')
      setTimeout(() => setCopiedId(null), 1800)
    } catch {}
  }

  function stopStreaming() {
    readerRef.current?.cancel()
    readerRef.current = null
    setSending(false)
    setMessages(prev =>
      prev.map((m, i) =>
        i === prev.length - 1 && m.role === 'assistant'
          ? { ...m, streaming: false }
          : m
      )
    )
  }

  function setMessageFeedback(id: string, feedback: Feedback) {
    setMessages(prev => prev.map(m => m.id === id ? { ...m, feedback } : m))
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

  function announce(msg: string) {
    if (!liveRef.current) return
    liveRef.current.textContent = ''
    setTimeout(() => { if (liveRef.current) liveRef.current.textContent = msg }, 50)
  }

  /* ── File attachment handling ── */
  function addAttachmentFiles(files: File[]) {
    const metas: AttachmentMeta[] = files.map(f => ({
      id: uid(),
      name: f.name,
      size: f.size,
      type: f.type,
      file: f,
    }))
    setAttachments(prev => [...prev, ...metas])
  }

  function removeAttachment(id: string) {
    setAttachments(prev => prev.filter(a => a.id !== id))
  }

  function handleTextareaPaste(e: React.ClipboardEvent<HTMLTextAreaElement>) {
    const files = Array.from(e.clipboardData.files)
    if (files.length) {
      e.preventDefault()
      addAttachmentFiles(files)
    }
  }

  function handleTextareaDrop(e: React.DragEvent<HTMLTextAreaElement>) {
    const files = Array.from(e.dataTransfer.files)
    if (files.length) {
      e.preventDefault()
      addAttachmentFiles(files)
    }
  }

  /* ── Send ── */
  async function sendMessage() {
    const text = input.trim()
    if ((!text && attachments.length === 0) || sending) return
    setInput('')
    setAttachments([])
    await sendMessageWithText(text)
  }

  async function sendMessageWithText(question: string) {
    if (sending) return
    setSending(true)
    setGlobalError('')
    setFollowUps([])
    setSelectedCitation(null)
    lastQuestionRef.current = question

    const userMsg: Message = {
      id: uid(),
      role: 'user',
      content: question,
      attachments: attachments.length ? attachments.map(a => ({ id: a.id, name: a.name, size: a.size, type: a.type })) : undefined,
    }
    setMessages(prev => [...prev, userMsg])

    const assistantId = uid()
    let assistantText = ''
    let finalCitations: Citation[] = []
    let streamError: string | null = null

    try {
      const headers = await getAuthHeader()
      const res = await fetch(`${API_URL}/ask`, {
        method: 'POST',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify({ question, session_id: activeSession }),
      })

      if (res.status === 401) { router.replace('/login'); return }

      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || `Request failed (${res.status})`)
      }

      if (!res.body) throw new Error('No response body')

      const reader = res.body.getReader()
      readerRef.current = reader
      const decoder = new TextDecoder()
      let buffer = ''

      // Optimistically add empty assistant message to trigger scroll
      setMessages(prev => [...prev, { id: assistantId, role: 'assistant', content: '', streaming: true }])

      while (true) {
        const { value, done } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const events = buffer.split('\n\n')
        buffer = events.pop() || ''

        for (const event of events) {
          const line = event.split('\n').find(r => r.startsWith('data: '))
          if (!line) continue
          try {
            const parsed = JSON.parse(line.replace(/^data:\s*/, ''))

            if (parsed.type === 'session' && parsed.session_id) {
              setActiveSession(parsed.session_id)
            }

            if (parsed.type === 'token') {
              assistantText += parsed.text || ''
              setMessages(prev =>
                prev.map(m =>
                  m.id === assistantId
                    ? { ...m, content: assistantText, streaming: true }
                    : m
                )
              )
            }

            if (parsed.type === 'done') {
              finalCitations = parseCitationList(parsed.citations)
              setMessages(prev =>
                prev.map(m =>
                  m.id === assistantId
                    ? { ...m, content: assistantText, citations: finalCitations, streaming: false }
                    : m
                )
              )
              setTimeout(loadSessions, 500)
              fetchFollowUps(question, assistantText)
            }

            if (parsed.type === 'error') {
              streamError = parsed.message || 'The assistant encountered an error'
            }
          } catch (err) {
            if (!(err instanceof SyntaxError)) throw err
          }
        }
      }
    } catch (err: any) {
      if (err?.name === 'AbortError' || err?.message?.includes('cancel')) return
      streamError = err?.message || 'Connection error'
    }

    // On stream error: mark assistant message as error (separate from content)
    if (streamError) {
      setMessages(prev => {
        const existing = prev.find(m => m.id === assistantId)
        if (existing) {
          return prev.map(m =>
            m.id === assistantId
              ? { ...m, content: streamError!, streaming: false, error: true }
              : m
          )
        }
        return [...prev, { id: assistantId, role: 'assistant', content: streamError!, streaming: false, error: true }]
      })
    }

    readerRef.current = null
    setSending(false)
    setTimeout(() => inputRef.current?.focus(), 50)
  }

  function regenerate() {
    if (!lastQuestionRef.current || sending) return
    setFollowUps([])
    setMessages(prev => {
      const copy = [...prev]
      // Strip last assistant + user turn
      if (copy[copy.length - 1]?.role === 'assistant') copy.pop()
      if (copy[copy.length - 1]?.role === 'user') copy.pop()
      return copy
    })
    setTimeout(() => sendMessageWithText(lastQuestionRef.current), 30)
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (composingRef.current) return // Don't send during IME composition
    if (e.key === 'Enter') {
      if (e.shiftKey) return // Shift+Enter = newline
      if (e.metaKey || e.ctrlKey) {
        // Cmd/Ctrl+Enter always sends
        e.preventDefault()
        sendMessage()
        return
      }
      // Plain Enter sends only on desktop (not mobile where Enter is common)
      const isMobile = window.matchMedia('(pointer: coarse)').matches
      if (!isMobile) {
        e.preventDefault()
        sendMessage()
      }
    }
  }

  async function signOut() {
    await supabase.auth.signOut()
    router.replace('/login')
  }

  function openCitation(c: Citation) {
    setSelectedCitation(c)
    setCitationPanelOpen(true)
  }

  /* ═══════════════════════════════════════════════════════════════════════
     Render
     ═══════════════════════════════════════════════════════════════════════ */

  const lastAssistantIdx = messages.reduceRight((acc, m, i) =>
    acc === -1 && m.role === 'assistant' ? i : acc, -1)

  return (
    <>
      <style>{`
        @keyframes pb-spin    { to { transform: rotate(360deg) } }
        @keyframes pb-blink   { 0%,100%{opacity:1} 50%{opacity:0} }
        @keyframes pb-shimmer { 0%,100%{opacity:0.4} 50%{opacity:0.7} }
        @keyframes pb-fade-up { from{opacity:0;transform:translateY(6px)} to{opacity:1;transform:translateY(0)} }
        @keyframes pb-slide-in { from{transform:translateX(100%)} to{transform:translateX(0)} }
        @keyframes pb-slide-up { from{transform:translateY(100%)} to{transform:translateY(0)} }

        .pb-prose p:last-child { margin-bottom: 0 !important }
        .pb-scrollbar::-webkit-scrollbar { width: 5px }
        .pb-scrollbar::-webkit-scrollbar-track { background: transparent }
        .pb-scrollbar::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 99px }
        .pb-scrollbar::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,0.18) }

        .pb-session-item:hover  { background: rgba(255,255,255,0.05) !important }
        .pb-session-active      { background: rgba(249,115,22,0.08) !important; border-color: rgba(249,115,22,0.2) !important }

        .pb-icon-btn {
          display:inline-flex; align-items:center; justify-content:center;
          border-radius:7px; padding:5px 8px; font-size:12px; font-weight:500;
          transition:all 0.15s; cursor:pointer; border:none; background:transparent;
          color:var(--fg-subtle); gap:4px;
        }
        .pb-icon-btn:hover { background:rgba(255,255,255,0.07); color:var(--fg-muted) }
        .pb-icon-btn:active { transform:scale(0.96) }
        .pb-icon-btn:focus-visible { outline:2px solid rgba(249,115,22,0.6); outline-offset:2px }

        .pb-input:focus { outline:none }
        .pb-send-btn:disabled { opacity:0.35; cursor:not-allowed }
        .pb-send-btn:not(:disabled):hover { background:var(--brand-light) !important }
        .pb-send-btn:not(:disabled):active { transform:scale(0.97) }

        .pb-followup-chip:hover { border-color:rgba(249,115,22,0.35) !important; color:var(--fg) !important; background:rgba(249,115,22,0.08) !important }
      `}</style>

      {/* Screen reader live region */}
      <div ref={liveRef} role="status" aria-live="polite" aria-atomic="true"
        style={{ position: 'absolute', width: 1, height: 1, overflow: 'hidden', clip: 'rect(0,0,0,0)' }} />

      <main
        style={{
          height: '100dvh', display: 'flex', overflow: 'hidden',
          background: 'var(--bg)', color: 'var(--fg)',
          fontFamily: 'var(--font-sans, "IBM Plex Sans", sans-serif)',
        }}
        aria-label="PatchBrain chat"
      >
        {/* ══════════════════════════════════════════════════
            LEFT SIDEBAR — conversation history
            ══════════════════════════════════════════════════ */}

        {/* Mobile overlay */}
        {mobileSidebarOpen && (
          <div
            style={{
              position: 'fixed', inset: 0, zIndex: 40,
              background: 'rgba(0,0,0,0.6)', backdropFilter: 'blur(4px)',
            }}
            onClick={() => setMobileSidebarOpen(false)}
            aria-hidden="true"
          />
        )}

        <aside
          aria-label="Conversation history"
          style={{
            width: 272,
            flexShrink: 0,
            display: 'flex',
            flexDirection: 'column',
            borderRight: '1px solid var(--border)',
            background: 'var(--bg-subtle)',
            // Mobile: slide in from left as fixed panel
            ...(typeof window !== 'undefined' && window.innerWidth < 1024 ? {
              position: 'fixed', top: 0, left: 0, bottom: 0, zIndex: 50,
              transform: mobileSidebarOpen ? 'translateX(0)' : 'translateX(-100%)',
              transition: 'transform 0.25s cubic-bezier(0.16,1,0.3,1)',
            } : {}),
          }}
        >
          {/* Sidebar header */}
          <div style={{ padding: '14px 14px 12px', borderBottom: '1px solid var(--border)', flexShrink: 0 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
              <div style={{
                width: 34, height: 34, borderRadius: 10, flexShrink: 0,
                background: 'linear-gradient(135deg, #f97316, #c2410c)',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                boxShadow: '0 2px 10px rgba(249,115,22,0.28)',
              }} aria-hidden="true">
                <span style={{ color: '#fff', fontWeight: 800, fontSize: 14 }}>P</span>
              </div>
              <div style={{ minWidth: 0 }}>
                <p style={{ margin: 0, fontSize: 13, fontWeight: 600, color: 'var(--fg)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {companyName}
                </p>
                <p style={{ margin: 0, fontSize: 11, color: 'var(--fg-subtle)' }}>PatchBrain</p>
              </div>
            </div>

            <button
              onClick={newChat}
              style={{
                width: '100%', padding: '9px 14px', borderRadius: 10,
                background: 'var(--brand)', color: '#fff', fontWeight: 600, fontSize: 13,
                border: 'none', cursor: 'pointer', display: 'flex', alignItems: 'center',
                justifyContent: 'center', gap: 6, transition: 'all 0.15s',
                boxShadow: '0 2px 10px rgba(249,115,22,0.25)',
              }}
              onMouseEnter={e => { e.currentTarget.style.background = 'var(--brand-light)'; e.currentTarget.style.transform = 'translateY(-1px)' }}
              onMouseLeave={e => { e.currentTarget.style.background = 'var(--brand)'; e.currentTarget.style.transform = 'translateY(0)' }}
            >
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
                <path d="M7 1v12M1 7h12" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/>
              </svg>
              New chat
            </button>
          </div>

          {/* Session list */}
          <div
            className="pb-scrollbar"
            style={{ flex: 1, overflowY: 'auto', padding: '8px 8px' }}
            role="list"
            aria-label="Past conversations"
          >
            {sessions.length === 0 ? (
              <p style={{ padding: '20px 8px', fontSize: 12, color: 'var(--fg-subtle)', textAlign: 'center' }}>
                No conversations yet
              </p>
            ) : sessions.map(session => (
              <div
                key={session.id}
                role="listitem"
                onClick={() => loadSession(session.id)}
                className={`pb-session-item ${activeSession === session.id ? 'pb-session-active' : ''}`}
                style={{
                  marginBottom: 2, padding: '9px 10px', borderRadius: 10, cursor: 'pointer',
                  border: '1px solid transparent', transition: 'all 0.12s',
                  display: 'flex', alignItems: 'flex-start', gap: 8,
                }}
                aria-current={activeSession === session.id ? 'true' : undefined}
              >
                <div style={{ flex: 1, minWidth: 0 }}>
                  <p style={{ margin: 0, fontSize: 13, color: 'var(--fg)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontWeight: activeSession === session.id ? 500 : 400 }}>
                    {session.title || 'New conversation'}
                  </p>
                  <p style={{ margin: '2px 0 0', fontSize: 11, color: 'var(--fg-subtle)' }}>
                    {timeAgo(session.updated_at)}
                    {session.message_count ? ` · ${session.message_count} msgs` : ''}
                  </p>
                </div>
                <button
                  onClick={e => deleteSession(session.id, e)}
                  aria-label={`Delete conversation: ${session.title || 'New conversation'}`}
                  style={{
                    flexShrink: 0, width: 22, height: 22, borderRadius: 6,
                    background: 'none', border: 'none', cursor: 'pointer',
                    color: 'var(--fg-subtle)', display: 'flex', alignItems: 'center',
                    justifyContent: 'center', opacity: 0, transition: 'all 0.15s', fontSize: 16,
                  }}
                  onMouseEnter={e => { e.currentTarget.style.background = 'rgba(248,113,113,0.12)'; e.currentTarget.style.color = '#f87171' }}
                  onMouseLeave={e => { e.currentTarget.style.background = 'none'; e.currentTarget.style.color = 'var(--fg-subtle)' }}
                  onFocus={e => (e.currentTarget.style.opacity = '1')}
                  onBlur={e => (e.currentTarget.style.opacity = '0')}
                  // Show on parent hover via JS (CSS group not available here)
                  ref={btn => {
                    const parent = btn?.parentElement
                    if (!parent || !btn) return
                    const show = () => (btn.style.opacity = '1')
                    const hide = () => (btn.style.opacity = '0')
                    parent.addEventListener('mouseenter', show)
                    parent.addEventListener('mouseleave', hide)
                  }}
                >
                  ×
                </button>
              </div>
            ))}
          </div>

          {/* Sidebar footer */}
          <div style={{ borderTop: '1px solid var(--border)', padding: '8px' }}>
            <button
              onClick={() => router.push('/dashboard')}
              className="pb-icon-btn"
              style={{ width: '100%', justifyContent: 'flex-start', padding: '8px 10px', borderRadius: 8 }}
            >
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
                <path d="M9 1H5a4 4 0 000 8h4a4 4 0 000-8z" stroke="currentColor" strokeWidth="1.5"/>
                <path d="M5 5h4M5 7h2" stroke="currentColor" strokeWidth="1.25" strokeLinecap="round"/>
              </svg>
              File Library
            </button>
            <button
              onClick={signOut}
              className="pb-icon-btn"
              style={{ width: '100%', justifyContent: 'flex-start', padding: '8px 10px', borderRadius: 8 }}
            >
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
                <path d="M5 2H2a1 1 0 00-1 1v8a1 1 0 001 1h3M9 10l3-3-3-3M13 7H5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
              </svg>
              Sign out
            </button>
          </div>
        </aside>

        {/* ══════════════════════════════════════════════════
            MAIN CHAT AREA
            ══════════════════════════════════════════════════ */}
        <section
          style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', height: '100dvh' }}
          aria-label="Chat conversation"
        >
          {/* Top bar */}
          <header style={{
            display: 'flex', alignItems: 'center', justifyContent: 'space-between',
            padding: '0 16px', height: 52, flexShrink: 0,
            borderBottom: '1px solid var(--border)',
            background: 'rgba(6,9,16,0.85)', backdropFilter: 'blur(16px)',
            WebkitBackdropFilter: 'blur(16px)',
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              {/* Mobile hamburger */}
              <button
                onClick={() => setMobileSidebarOpen(true)}
                aria-label="Open navigation"
                style={{
                  display: 'none', // shown via CSS on mobile
                  width: 32, height: 32, borderRadius: 8, alignItems: 'center', justifyContent: 'center',
                  background: 'none', border: '1px solid var(--border)', cursor: 'pointer', color: 'var(--fg-muted)',
                }}
              >
                <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                  <path d="M1 3h12M1 7h12M1 11h12" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
                </svg>
              </button>

              <div>
                <p style={{ margin: 0, fontSize: 14, fontWeight: 600, color: 'var(--fg)' }}>
                  Ask PatchBrain
                </p>
                <p style={{ margin: 0, fontSize: 11, color: 'var(--fg-subtle)' }}>
                  {activeSession
                    ? (sessions.find(s => s.id === activeSession)?.title || 'Current conversation')
                    : 'New conversation'}
                </p>
              </div>
            </div>

            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              {/* Citation panel toggle */}
              <button
                onClick={() => setCitationPanelOpen(o => !o)}
                className="pb-icon-btn"
                aria-pressed={citationPanelOpen}
                aria-label={citationPanelOpen ? 'Close source panel' : 'Open source panel'}
                style={{
                  padding: '5px 10px',
                  background: citationPanelOpen ? 'rgba(249,115,22,0.1)' : 'transparent',
                  border: '1px solid',
                  borderColor: citationPanelOpen ? 'rgba(249,115,22,0.25)' : 'var(--border)',
                  color: citationPanelOpen ? '#fdba74' : 'var(--fg-muted)',
                }}
              >
                <svg width="13" height="13" viewBox="0 0 13 13" fill="none" aria-hidden="true">
                  <rect x="0.75" y="0.75" width="11.5" height="11.5" rx="2.25" stroke="currentColor" strokeWidth="1.25"/>
                  <path d="M8.5 0.75v11.5" stroke="currentColor" strokeWidth="1.25"/>
                  <path d="M3 4.5h3.5M3 6.5h3.5M3 8.5h2" stroke="currentColor" strokeWidth="1.15" strokeLinecap="round"/>
                </svg>
                Sources
              </button>
            </div>
          </header>

          {/* Global error banner */}
          {globalError && (
            <div
              role="alert"
              style={{
                padding: '10px 16px', flexShrink: 0,
                background: 'rgba(248,113,113,0.08)', borderBottom: '1px solid rgba(248,113,113,0.18)',
                display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, color: '#fca5a5',
              }}
            >
              <span aria-hidden="true">⚠</span>
              <span style={{ flex: 1 }}>{globalError}</span>
              <button onClick={() => setGlobalError('')} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'inherit', opacity: 0.6 }}>×</button>
            </div>
          )}

          {/* ── Message scroll area ── */}
          <div
            style={{ flex: 1, minHeight: 0, display: 'flex' }}
          >
            <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', position: 'relative' }}>

              <div
                ref={scrollRef}
                className="pb-scrollbar"
                style={{ flex: 1, overflowY: 'auto', padding: '24px 0' }}
                aria-label="Messages"
                role="log"
                aria-live="polite"
                aria-relevant="additions"
              >
                {/* Content wrapper — ResizeObserver watches this */}
                <div style={{ maxWidth: 760, margin: '0 auto', padding: '0 16px' }}>

                  {/* Empty state */}
                  {!loadingHistory && messages.length === 0 && (
                    <div
                      style={{
                        minHeight: 'calc(100dvh - 240px)',
                        display: 'flex', flexDirection: 'column',
                        alignItems: 'center', justifyContent: 'center', textAlign: 'center',
                        animation: 'pb-fade-up 0.4s ease both',
                      }}
                    >
                      <div style={{
                        width: 52, height: 52, borderRadius: 14, marginBottom: 18,
                        background: 'rgba(249,115,22,0.08)', border: '1px solid rgba(249,115,22,0.18)',
                        display: 'flex', alignItems: 'center', justifyContent: 'center',
                        animation: 'pb-fade-up 0.35s ease both',
                      }}>
                        <svg width="22" height="22" viewBox="0 0 22 22" fill="none" aria-hidden="true">
                          <path d="M11 2.5L13.5 8l5.5 1-4 4 1 5.5L11 16l-5 2.5 1-5.5L3 9l5.5-1L11 2.5z"
                            stroke="#f97316" strokeWidth="1.5" strokeLinejoin="round"/>
                        </svg>
                      </div>
                      <h1 style={{ margin: '0 0 10px', fontSize: 20, fontWeight: 600, color: 'var(--fg)' }}>
                        Ask about your operations
                      </h1>
                      <p style={{ margin: '0 0 28px', fontSize: 13, lineHeight: 1.7, color: 'var(--fg-muted)', maxWidth: '42ch' }}>
                        Natural language answers from your run tickets, invoices, compliance records, and field reports — with source citations.
                      </p>
                      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 8, width: '100%', maxWidth: 600 }}>
                        {EXAMPLE_PROMPTS.map((p, i) => (
                          <button
                            key={p}
                            onClick={() => { setInput(p); setTimeout(() => inputRef.current?.focus(), 30) }}
                            style={{
                              padding: '11px 14px', borderRadius: 10, textAlign: 'left', fontSize: 13,
                              background: 'rgba(255,255,255,0.03)', border: '1px solid var(--border)',
                              color: 'var(--fg-muted)', cursor: 'pointer', transition: 'all 0.15s',
                              animation: `pb-fade-up 0.35s ease ${0.05 + i * 0.05}s both`,
                              lineHeight: 1.5,
                            }}
                            onMouseEnter={e => { e.currentTarget.style.borderColor = 'rgba(249,115,22,0.3)'; e.currentTarget.style.color = 'var(--fg)'; e.currentTarget.style.background = 'rgba(249,115,22,0.05)' }}
                            onMouseLeave={e => { e.currentTarget.style.borderColor = 'var(--border)'; e.currentTarget.style.color = 'var(--fg-muted)'; e.currentTarget.style.background = 'rgba(255,255,255,0.03)' }}
                          >
                            {p}
                          </button>
                        ))}
                      </div>
                    </div>
                  )}

                  {/* Loading skeleton */}
                  {loadingHistory && <MessageSkeleton />}

                  {/* Messages */}
                  {!loadingHistory && messages.map((msg, idx) => {
                    const isLast = idx === messages.length - 1
                    const isLastAssistant = idx === lastAssistantIdx

                    return (
                      <div
                        key={msg.id}
                        style={{
                          marginBottom: isLast ? 0 : 20,
                          display: 'flex',
                          justifyContent: msg.role === 'user' ? 'flex-end' : 'flex-start',
                          animation: idx >= messages.length - 2 ? 'pb-fade-up 0.3s ease both' : undefined,
                        }}
                      >
                        {msg.role === 'user' ? (
                          /* User bubble */
                          <div style={{ maxWidth: '72%' }}>
                            {/* Attachments above bubble */}
                            {msg.attachments?.length ? (
                              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, justifyContent: 'flex-end', marginBottom: 6 }}>
                                {msg.attachments.map(a => (
                                  <AttachmentChip key={a.id} meta={a} />
                                ))}
                              </div>
                            ) : null}
                            <div style={{
                              padding: '10px 16px', borderRadius: '18px 18px 4px 18px',
                              background: 'var(--brand)', color: '#fff', fontSize: 14, lineHeight: 1.7,
                              boxShadow: '0 2px 12px rgba(249,115,22,0.18)',
                              whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                            }}>
                              {msg.content}
                            </div>
                            {msg.created_at && (
                              <p style={{ margin: '4px 2px 0', fontSize: 10, color: 'var(--fg-subtle)', textAlign: 'right' }}>
                                {timeAgo(msg.created_at)}
                              </p>
                            )}
                          </div>
                        ) : (
                          /* Assistant bubble */
                          <div style={{ maxWidth: '100%', width: '100%' }}>
                            {/* Assistant avatar + header */}
                            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
                              <div style={{
                                width: 24, height: 24, borderRadius: 6, flexShrink: 0,
                                background: 'linear-gradient(135deg, #f97316, #c2410c)',
                                display: 'flex', alignItems: 'center', justifyContent: 'center',
                              }} aria-hidden="true">
                                <span style={{ color: '#fff', fontWeight: 800, fontSize: 10 }}>P</span>
                              </div>
                              <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--fg-muted)' }}>PatchBrain</span>
                              {msg.streaming && (
                                <span style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 11, color: 'var(--fg-subtle)' }}>
                                  <Spinner size={11} />
                                  Thinking…
                                </span>
                              )}
                            </div>

                            <div style={{
                              padding: '14px 16px', borderRadius: '4px 18px 18px 18px',
                              background: 'var(--bg-card)', border: '1px solid var(--border)',
                            }}>
                              <AssistantContent message={msg} onCitationClick={openCitation} />
                            </div>

                            {/* Action bar — only on non-streaming, non-error assistant messages */}
                            {!msg.streaming && !msg.error && (
                              <div style={{ display: 'flex', alignItems: 'center', gap: 2, marginTop: 6, paddingLeft: 32 }}>
                                <button
                                  className="pb-icon-btn"
                                  onClick={() => copyMessage(msg.id, msg.content)}
                                  aria-label={copiedId === msg.id ? 'Copied' : 'Copy message'}
                                >
                                  {copiedId === msg.id ? (
                                    <>
                                      <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">
                                        <path d="M2 6l2.5 2.5L10 3.5" stroke="#22c55e" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
                                      </svg>
                                      <span style={{ color: '#22c55e' }}>Copied</span>
                                    </>
                                  ) : (
                                    <>
                                      <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">
                                        <rect x="4" y="4" width="7" height="7" rx="1.5" stroke="currentColor" strokeWidth="1.25"/>
                                        <path d="M8 4V2.5A1.5 1.5 0 006.5 1h-4A1.5 1.5 0 001 2.5v4A1.5 1.5 0 002.5 8H4" stroke="currentColor" strokeWidth="1.25"/>
                                      </svg>
                                      Copy
                                    </>
                                  )}
                                </button>

                                {isLastAssistant && (
                                  <button
                                    className="pb-icon-btn"
                                    onClick={regenerate}
                                    disabled={sending}
                                    aria-label="Regenerate response"
                                  >
                                    <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">
                                      <path d="M10 4A4.5 4.5 0 102 8" stroke="currentColor" strokeWidth="1.25" strokeLinecap="round"/>
                                      <path d="M10 1.5V4H7.5" stroke="currentColor" strokeWidth="1.25" strokeLinecap="round" strokeLinejoin="round"/>
                                    </svg>
                                    Regenerate
                                  </button>
                                )}

                                {/* Thumbs feedback */}
                                <div style={{ display: 'flex', gap: 1, marginLeft: 2 }}>
                                  {(['up', 'down'] as const).map(dir => (
                                    <button
                                      key={dir}
                                      className="pb-icon-btn"
                                      onClick={() => setMessageFeedback(msg.id, msg.feedback === dir ? null : dir)}
                                      aria-label={`${dir === 'up' ? 'Thumbs up' : 'Thumbs down'} — mark as ${dir === 'up' ? 'helpful' : 'unhelpful'}`}
                                      aria-pressed={msg.feedback === dir}
                                      style={{
                                        color: msg.feedback === dir
                                          ? dir === 'up' ? '#22c55e' : '#f87171'
                                          : 'var(--fg-subtle)',
                                      }}
                                    >
                                      {dir === 'up' ? (
                                        <svg width="12" height="12" viewBox="0 0 12 12" fill={msg.feedback === 'up' ? 'currentColor' : 'none'} aria-hidden="true">
                                          <path d="M4 11V6.5L1 5.5V11h3zM4 6.5l1.5-4.5a1 1 0 011 1V5h3.5l-1 5.5H4" stroke="currentColor" strokeWidth="1.1" strokeLinecap="round" strokeLinejoin="round"/>
                                        </svg>
                                      ) : (
                                        <svg width="12" height="12" viewBox="0 0 12 12" fill={msg.feedback === 'down' ? 'currentColor' : 'none'} aria-hidden="true">
                                          <path d="M8 1v4.5L11 6.5V1H8zM8 5.5L6.5 10a1 1 0 01-1-1V7H2L3 1.5H8" stroke="currentColor" strokeWidth="1.1" strokeLinecap="round" strokeLinejoin="round"/>
                                        </svg>
                                      )}
                                    </button>
                                  ))}
                                </div>

                                {/* Citation count badge */}
                                {msg.citations && msg.citations.length > 0 && (
                                  <button
                                    className="pb-icon-btn"
                                    onClick={() => { openCitation(msg.citations![0]); setCitationPanelOpen(true) }}
                                    style={{ marginLeft: 4 }}
                                    aria-label={`View ${msg.citations.length} source${msg.citations.length > 1 ? 's' : ''}`}
                                  >
                                    <span style={{
                                      padding: '1px 6px', borderRadius: 20, fontSize: 11, fontWeight: 600,
                                      background: 'rgba(249,115,22,0.1)', color: '#fdba74',
                                      border: '1px solid rgba(249,115,22,0.2)',
                                    }}>
                                      {msg.citations.length} source{msg.citations.length > 1 ? 's' : ''}
                                    </span>
                                  </button>
                                )}
                              </div>
                            )}

                            {/* Follow-up chips — only after last assistant message */}
                            {isLastAssistant && !msg.streaming && !msg.error && followUps.length > 0 && (
                              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 10, paddingLeft: 32 }}>
                                {followUps.map(q => (
                                  <button
                                    key={q}
                                    className="pb-followup-chip"
                                    onClick={() => { setFollowUps([]); sendMessageWithText(q) }}
                                    style={{
                                      padding: '6px 12px', borderRadius: 20, fontSize: 12,
                                      background: 'transparent', border: '1px solid var(--border)',
                                      color: 'var(--fg-muted)', cursor: 'pointer', transition: 'all 0.15s',
                                      textAlign: 'left',
                                    }}
                                  >
                                    {q}
                                  </button>
                                ))}
                              </div>
                            )}
                          </div>
                        )}
                      </div>
                    )
                  })}

                  {/* Spacer so last message isn't hidden under input */}
                  <div style={{ height: 16 }} />
                </div>
              </div>

              {/* Scroll-to-bottom FAB */}
              {!isAtBottom && (
                <button
                  onClick={() => scrollToBottom()}
                  aria-label="Scroll to latest message"
                  style={{
                    position: 'absolute', bottom: 16, left: '50%', transform: 'translateX(-50%)',
                    display: 'flex', alignItems: 'center', gap: 6,
                    padding: '7px 14px', borderRadius: 20, fontSize: 12, fontWeight: 500,
                    background: 'var(--bg-elevated)', border: '1px solid var(--border)',
                    color: 'var(--fg-muted)', cursor: 'pointer', boxShadow: '0 4px 20px rgba(0,0,0,0.4)',
                    transition: 'all 0.15s', animation: 'pb-fade-up 0.2s ease both',
                    zIndex: 10,
                  }}
                  onMouseEnter={e => { e.currentTarget.style.background = 'var(--bg-card)'; e.currentTarget.style.color = 'var(--fg)' }}
                  onMouseLeave={e => { e.currentTarget.style.background = 'var(--bg-elevated)'; e.currentTarget.style.color = 'var(--fg-muted)' }}
                >
                  <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">
                    <path d="M2 4l4 4 4-4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
                  </svg>
                  Jump to latest
                </button>
              )}

              {/* ── Input area ── */}
              <div style={{
                flexShrink: 0, padding: '12px 16px 16px',
                borderTop: '1px solid var(--border)',
                background: 'rgba(6,9,16,0.88)', backdropFilter: 'blur(16px)',
                WebkitBackdropFilter: 'blur(16px)',
              }}>
                <div style={{ maxWidth: 760, margin: '0 auto' }}>

                  {/* Attachment preview strip */}
                  {attachments.length > 0 && (
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 8 }}>
                      {attachments.map(a => (
                        <AttachmentChip key={a.id} meta={a} onRemove={() => removeAttachment(a.id)} />
                      ))}
                    </div>
                  )}

                  {/* Input container */}
                  <div style={{
                    display: 'flex', alignItems: 'flex-end', gap: 8,
                    borderRadius: 14, border: '1px solid var(--border)',
                    background: 'var(--bg-card)', padding: '8px 8px 8px 14px',
                    transition: 'border-color 0.15s',
                  }}
                    onFocusCapture={e => e.currentTarget.style.borderColor = 'rgba(249,115,22,0.35)'}
                    onBlurCapture={e => e.currentTarget.style.borderColor = 'var(--border)'}
                  >
                    {/* Attach file button */}
                    <button
                      onClick={() => fileInputRef.current?.click()}
                      className="pb-icon-btn"
                      aria-label="Attach file"
                      style={{ flexShrink: 0, marginBottom: 2 }}
                      tabIndex={0}
                    >
                      <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                        <path d="M14 7.5L7.5 14A4.5 4.5 0 011 7.5L7 1.5a3 3 0 014.5 4L6 11a1.5 1.5 0 01-2-2.5L9.5 3" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"/>
                      </svg>
                    </button>
                    <input
                      ref={fileInputRef}
                      type="file"
                      multiple
                      accept=".pdf,.xlsx,.xls,.docx,.txt,.csv,.png,.jpg,.jpeg"
                      style={{ display: 'none' }}
                      aria-hidden="true"
                      onChange={e => { if (e.target.files) addAttachmentFiles(Array.from(e.target.files)); e.target.value = '' }}
                    />

                    {/* Textarea */}
                    <textarea
                      ref={inputRef}
                      value={input}
                      onChange={e => setInput(e.target.value)}
                      onKeyDown={handleKeyDown}
                      onCompositionStart={() => { composingRef.current = true }}
                      onCompositionEnd={() => { composingRef.current = false }}
                      onPaste={handleTextareaPaste}
                      onDrop={handleTextareaDrop}
                      onDragOver={e => e.preventDefault()}
                      disabled={sending && !input}
                      rows={1}
                      placeholder="Ask about tickets, invoices, compliance reports… (/ to focus)"
                      aria-label="Message input"
                      className="pb-input pb-scrollbar"
                      style={{
                        flex: 1, resize: 'none', background: 'transparent',
                        border: 'none', outline: 'none', fontSize: 14, lineHeight: 1.6,
                        color: 'var(--fg)', minHeight: 36, maxHeight: 180,
                        fontFamily: 'inherit', padding: '6px 0',
                      }}
                    />

                    {/* Send / Stop button */}
                    {sending ? (
                      <button
                        onClick={stopStreaming}
                        aria-label="Stop generating"
                        style={{
                          flexShrink: 0, width: 36, height: 36, borderRadius: 10,
                          background: 'rgba(248,113,113,0.1)', border: '1px solid rgba(248,113,113,0.25)',
                          cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center',
                          color: '#f87171', transition: 'all 0.15s',
                        }}
                        onMouseEnter={e => e.currentTarget.style.background = 'rgba(248,113,113,0.2)'}
                        onMouseLeave={e => e.currentTarget.style.background = 'rgba(248,113,113,0.1)'}
                      >
                        <svg width="12" height="12" viewBox="0 0 12 12" fill="currentColor" aria-hidden="true">
                          <rect x="2" y="2" width="8" height="8" rx="1.5"/>
                        </svg>
                      </button>
                    ) : (
                      <button
                        onClick={sendMessage}
                        disabled={!input.trim() && attachments.length === 0}
                        className="pb-send-btn"
                        aria-label="Send message"
                        style={{
                          flexShrink: 0, width: 36, height: 36, borderRadius: 10,
                          background: 'var(--brand)', border: 'none',
                          cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center',
                          color: '#fff', transition: 'all 0.15s',
                          boxShadow: '0 2px 8px rgba(249,115,22,0.25)',
                        }}
                      >
                        <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
                          <path d="M1 7h12M8 3l4 4-4 4" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round"/>
                        </svg>
                      </button>
                    )}
                  </div>

                  {/* Input hints */}
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 6, padding: '0 4px' }}>
                    <p style={{ margin: 0, fontSize: 11, color: 'var(--fg-subtle)' }}>
                      Enter to send · Shift+Enter for newline · / to focus · Esc to stop
                    </p>
                    {input.length > 400 && (
                      <p style={{ margin: 0, fontSize: 11, color: input.length > 1800 ? '#f87171' : 'var(--fg-subtle)' }}>
                        {input.length.toLocaleString()} chars
                      </p>
                    )}
                  </div>
                </div>
              </div>
            </div>

            {/* ══════════════════════════════════════════════════
                RIGHT PANEL — source evidence
                Slides in, works on all screen sizes
                ══════════════════════════════════════════════════ */}
            {citationPanelOpen && (
              <aside
                aria-label="Source evidence panel"
                style={{
                  width: 340, flexShrink: 0,
                  borderLeft: '1px solid var(--border)',
                  background: 'var(--bg-subtle)',
                  display: 'flex', flexDirection: 'column',
                  animation: 'pb-slide-in 0.22s cubic-bezier(0.16,1,0.3,1) both',
                  overflow: 'hidden',
                }}
              >
                <CitationPanel
                  citation={selectedCitation}
                  onClose={() => { setSelectedCitation(null); setCitationPanelOpen(false) }}
                />
              </aside>
            )}
          </div>
        </section>
      </main>
    </>
  )
}
