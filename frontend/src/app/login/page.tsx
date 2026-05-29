'use client'

import { useState, useId, useRef, useEffect } from 'react'
import { useRouter } from 'next/navigation'
import { supabase } from '@/lib/supabase'

type AuthMode = 'login' | 'signup'

/* ─── Tiny icon components (no extra deps) ───────────────────────────────── */
function IconBolt({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
      <path
        fillRule="evenodd"
        d="M11.3 1.046A1 1 0 0112 2v5h4a1 1 0 01.82 1.573l-7 10A1 1 0 018 18v-5H4a1 1 0 01-.82-1.573l7-10a1 1 0 011.12-.38z"
        clipRule="evenodd"
      />
    </svg>
  )
}

function IconCheck({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        d="M3 8l3.5 3.5L13 4.5"
        stroke="currentColor"
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

function IconEye({
  visible,
  className,
}: {
  visible: boolean
  className?: string
}) {
  return visible ? (
    <svg className={className} viewBox="0 0 20 20" fill="none" aria-hidden="true">
      <path
        d="M2 10s3-6 8-6 8 6 8 6-3 6-8 6-8-6-8-6z"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
      <circle cx="10" cy="10" r="2.5" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  ) : (
    <svg className={className} viewBox="0 0 20 20" fill="none" aria-hidden="true">
      <path
        d="M3 3l14 14M8.585 8.586A2.5 2.5 0 0012.5 12.5M6.354 6.354C4.53 7.425 3 10 3 10s3 6 7 6c1.637 0 3.1-.655 4.292-1.708M10 4c4 0 7 6 7 6a13.16 13.16 0 01-1.645 2.354"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
    </svg>
  )
}

/* ─── Feature pill ───────────────────────────────────────────────────────── */
const FEATURES = [
  {
    label: 'Private company workspace',
    detail: 'All documents and chats scoped to your org only.',
  },
  {
    label: 'Source-cited answers',
    detail: 'Every answer links back to the exact document and page.',
  },
  {
    label: 'Runs on your oilfield data',
    detail: 'Tickets, invoices, field records, compliance PDFs.',
  },
]

/* ─── Social proof / stats ───────────────────────────────────────────────── */
const STATS = [
  { value: 'Instant', label: 'answers from your docs' },
  { value: '100%', label: 'private — your data stays yours' },
  { value: 'Any file', label: 'PDF, Excel, email, Word' },
]

/* ═══════════════════════════════════════════════════════════════════════════
   LoginPage
   ═══════════════════════════════════════════════════════════════════════════ */
export default function LoginPage() {
  const router = useRouter()

  /* ── Form state ── */
  const [email, setEmail]       = useState('')
  const [password, setPassword] = useState('')
  const [showPw, setShowPw]     = useState(false)
  const [mode, setMode]         = useState<AuthMode>('login')
  const [loading, setLoading]   = useState(false)
  const [error, setError]       = useState('')
  const [success, setSuccess]   = useState('')

  /* ── Accessibility IDs ── */
  const emailId    = useId()
  const passwordId = useId()
  const statusId   = useId()

  /* ── Auto-focus email on mode switch ── */
  const emailRef = useRef<HTMLInputElement>(null)
  useEffect(() => {
    emailRef.current?.focus()
  }, [mode])

  /* ─── Submit ─────────────────────────────────────────────────── */
  async function handleSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault()

    // Basic client-side guard — real validation happens in Supabase
    if (!email.trim()) {
      setError('Please enter your email address.')
      emailRef.current?.focus()
      return
    }
    if (password.length < 6) {
      setError('Password must be at least 6 characters.')
      return
    }

    setLoading(true)
    setError('')
    setSuccess('')

    try {
      if (mode === 'login') {
        const { error: authErr } = await supabase.auth.signInWithPassword({
          email: email.trim(),
          password,
        })
        if (authErr) throw authErr
        router.replace('/dashboard')
        return
      }

      // Sign-up
      const { error: signUpErr } = await supabase.auth.signUp({
        email: email.trim(),
        password,
        options: {
          emailRedirectTo: `${window.location.origin}/dashboard`,
        },
      })
      if (signUpErr) throw signUpErr

      setSuccess(
        'Account created. If email confirmation is enabled, check your inbox — then sign in below.',
      )
      setMode('login')
      setPassword('')
    } catch (err: unknown) {
      const msg =
        err instanceof Error
          ? err.message
          : 'Authentication failed. Please try again.'
      // Humanize common Supabase error messages
      setError(humanizeAuthError(msg))
    } finally {
      setLoading(false)
    }
  }

  function switchMode() {
    setMode(prev => (prev === 'login' ? 'signup' : 'login'))
    setError('')
    setSuccess('')
  }

  /* ─── Render ─────────────────────────────────────────────────── */
  return (
    <main className="min-h-dvh grid lg:grid-cols-[1.15fr_0.85fr]">

      {/* ── Left: brand panel (desktop only) ──────────────────────── */}
      <aside
        className="hidden lg:flex flex-col justify-between px-14 py-12"
        aria-label="PatchBrain product overview"
        style={{
          borderRight: '1px solid var(--border)',
          background: 'linear-gradient(160deg, var(--bg-subtle) 0%, var(--bg) 100%)',
        }}
      >
        {/* Logo */}
        <div className="flex items-center gap-3 animate-fade-up">
          <BrandIcon size="lg" />
          <div>
            <p className="text-sm font-semibold tracking-tight" style={{ color: 'var(--fg)' }}>
              PatchBrain
            </p>
            <p className="text-xs" style={{ color: 'var(--fg-subtle)' }}>
              Oilfield operations intelligence
            </p>
          </div>
        </div>

        {/* Hero copy */}
        <div className="max-w-lg animate-fade-up animate-fade-up-delay-1">
          {/* Category tag */}
          <div
            className="mb-6 inline-flex items-center gap-2 rounded-full px-3 py-1 text-xs font-medium"
            style={{
              border: '1px solid rgba(249,115,22,0.25)',
              background: 'rgba(249,115,22,0.08)',
              color: '#fbd5b0',
            }}
          >
            <IconBolt className="h-3 w-3" />
            <span>Built for field operations teams</span>
          </div>

          <h1
            className="text-[2.75rem] font-semibold leading-[1.15] tracking-tight"
            style={{ color: 'var(--fg)' }}
          >
            Turn messy oilfield files into{' '}
            <span style={{ color: 'var(--brand)' }}>operational answers.</span>
          </h1>

          <p
            className="mt-5 text-base leading-relaxed"
            style={{ color: 'var(--fg-muted)', maxWidth: '42ch' }}
          >
            Upload run tickets, invoices, safety records, compliance PDFs,
            spreadsheets, and field communications. Ask direct questions and
            get answers with source evidence.
          </p>

          {/* Stats row */}
          <div className="mt-10 flex items-start gap-8">
            {STATS.map(s => (
              <div key={s.value}>
                <p
                  className="text-xl font-semibold"
                  style={{ color: 'var(--brand-light)' }}
                >
                  {s.value}
                </p>
                <p className="mt-0.5 text-xs leading-snug" style={{ color: 'var(--fg-subtle)' }}>
                  {s.label}
                </p>
              </div>
            ))}
          </div>
        </div>

        {/* Feature list */}
        <div className="space-y-3 animate-fade-up animate-fade-up-delay-2" aria-label="Key features">
          {FEATURES.map(f => (
            <div
              key={f.label}
              className="flex items-start gap-3 rounded-xl px-4 py-3.5"
              style={{
                border: '1px solid var(--border)',
                background: 'var(--bg-card)',
              }}
            >
              <span
                className="mt-0.5 flex h-5 w-5 flex-shrink-0 items-center justify-center rounded-full"
                style={{ background: 'var(--brand-dim)', color: 'var(--brand)' }}
                aria-hidden="true"
              >
                <IconCheck className="h-3 w-3" />
              </span>
              <div>
                <p className="text-sm font-medium" style={{ color: 'var(--fg)' }}>
                  {f.label}
                </p>
                <p className="mt-0.5 text-xs leading-snug" style={{ color: 'var(--fg-subtle)' }}>
                  {f.detail}
                </p>
              </div>
            </div>
          ))}
        </div>
      </aside>

      {/* ── Right: auth form ───────────────────────────────────────── */}
      <section
        className="flex min-h-dvh items-center justify-center px-5 py-12"
        style={{ background: 'var(--bg)' }}
      >
        <div className="w-full max-w-[400px]">

          {/* Mobile logo */}
          <div className="mb-8 flex flex-col items-center lg:hidden animate-fade-up">
            <BrandIcon size="lg" />
            <p className="mt-3 text-lg font-semibold" style={{ color: 'var(--fg)' }}>
              PatchBrain
            </p>
            <p className="text-sm" style={{ color: 'var(--fg-subtle)' }}>
              Oilfield operations intelligence
            </p>
          </div>

          {/* Card */}
          <div
            className="rounded-2xl p-7 animate-fade-up"
            style={{
              border: '1px solid var(--border)',
              background: 'var(--bg-card)',
              boxShadow: 'var(--shadow-card)',
            }}
          >
            {/* Card header */}
            <div className="mb-6">
              <p
                className="text-xs font-semibold uppercase tracking-[0.2em]"
                style={{ color: 'var(--brand-light)' }}
              >
                {mode === 'login' ? 'Welcome back' : 'Get started'}
              </p>
              <h2
                className="mt-1.5 text-2xl font-semibold tracking-tight"
                style={{ color: 'var(--fg)' }}
              >
                {mode === 'login' ? 'Sign in to PatchBrain' : 'Create your account'}
              </h2>
              <p className="mt-1.5 text-sm leading-relaxed" style={{ color: 'var(--fg-muted)' }}>
                {mode === 'login'
                  ? 'Access your company workspace, documents, and chat history.'
                  : 'Start with email and password. Your admin assigns workspace access.'}
              </p>
            </div>

            {/* Status region for screen readers */}
            <div
              id={statusId}
              role="status"
              aria-live="polite"
              aria-atomic="true"
              className="sr-only"
            >
              {error || success || ''}
            </div>

            {/* Feedback banners */}
            {error && (
              <div
                role="alert"
                className="mb-4 flex items-start gap-2.5 rounded-xl px-4 py-3 text-sm"
                style={{
                  border: '1px solid rgba(248,113,113,0.25)',
                  background: 'var(--error-dim)',
                  color: '#fca5a5',
                }}
              >
                <span aria-hidden="true" className="mt-0.5 flex-shrink-0 text-base leading-none">
                  ⚠
                </span>
                <span>{error}</span>
              </div>
            )}

            {success && (
              <div
                role="status"
                className="mb-4 flex items-start gap-2.5 rounded-xl px-4 py-3 text-sm"
                style={{
                  border: '1px solid rgba(34,197,94,0.25)',
                  background: 'var(--success-dim)',
                  color: '#86efac',
                }}
              >
                <span aria-hidden="true" className="mt-0.5 flex-shrink-0">
                  ✓
                </span>
                <span>{success}</span>
              </div>
            )}

            {/* Form */}
            <form
              onSubmit={handleSubmit}
              noValidate
              aria-label={mode === 'login' ? 'Sign in form' : 'Create account form'}
            >
              <div className="space-y-4">
                {/* Email field */}
                <div>
                  <label
                    htmlFor={emailId}
                    className="mb-1.5 block text-sm font-medium"
                    style={{ color: 'var(--fg)' }}
                  >
                    Email address
                  </label>
                  <input
                    id={emailId}
                    ref={emailRef}
                    type="email"
                    value={email}
                    onChange={e => setEmail(e.target.value)}
                    required
                    autoComplete="email"
                    autoCapitalize="none"
                    autoCorrect="off"
                    spellCheck={false}
                    placeholder="you@company.com"
                    aria-required="true"
                    aria-describedby={error ? statusId : undefined}
                    disabled={loading}
                    className="w-full rounded-xl px-4 py-3 text-sm transition-all duration-150 disabled:cursor-not-allowed disabled:opacity-50"
                    style={{
                      border: '1px solid var(--border-hover)',
                      background: 'var(--bg-elevated)',
                      color: 'var(--fg)',
                      outline: 'none',
                    }}
                    onFocus={e => {
                      e.currentTarget.style.borderColor = 'rgba(249,115,22,0.6)'
                      e.currentTarget.style.boxShadow = '0 0 0 3px var(--focus-ring)'
                    }}
                    onBlur={e => {
                      e.currentTarget.style.borderColor = 'var(--border-hover)'
                      e.currentTarget.style.boxShadow = 'none'
                    }}
                  />
                </div>

                {/* Password field */}
                <div>
                  <div className="mb-1.5 flex items-center justify-between">
                    <label
                      htmlFor={passwordId}
                      className="text-sm font-medium"
                      style={{ color: 'var(--fg)' }}
                    >
                      Password
                    </label>
                    {mode === 'login' && (
                      <span
                        className="text-xs"
                        style={{ color: 'var(--fg-subtle)' }}
                      >
                        {/* Forgot password — placeholder for future feature */}
                        {/* 
                        <a href="/forgot-password" className="hover:text-orange-400 transition-colors">
                          Forgot password?
                        </a>
                        */}
                        6+ characters
                      </span>
                    )}
                  </div>

                  {/* Password wrapper with show/hide toggle */}
                  <div className="relative">
                    <input
                      id={passwordId}
                      type={showPw ? 'text' : 'password'}
                      value={password}
                      onChange={e => setPassword(e.target.value)}
                      required
                      autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
                      placeholder="••••••••"
                      minLength={6}
                      aria-required="true"
                      disabled={loading}
                      className="w-full rounded-xl px-4 py-3 pr-12 text-sm transition-all duration-150 disabled:cursor-not-allowed disabled:opacity-50"
                      style={{
                        border: '1px solid var(--border-hover)',
                        background: 'var(--bg-elevated)',
                        color: 'var(--fg)',
                        outline: 'none',
                        fontFamily: showPw ? 'inherit' : 'var(--font-mono, monospace)',
                        letterSpacing: showPw ? 'normal' : '0.15em',
                      }}
                      onFocus={e => {
                        e.currentTarget.style.borderColor = 'rgba(249,115,22,0.6)'
                        e.currentTarget.style.boxShadow = '0 0 0 3px var(--focus-ring)'
                      }}
                      onBlur={e => {
                        e.currentTarget.style.borderColor = 'var(--border-hover)'
                        e.currentTarget.style.boxShadow = 'none'
                      }}
                    />
                    <button
                      type="button"
                      onClick={() => setShowPw(v => !v)}
                      aria-label={showPw ? 'Hide password' : 'Show password'}
                      aria-pressed={showPw}
                      disabled={loading}
                      className="absolute right-3 top-1/2 -translate-y-1/2 flex h-7 w-7 items-center justify-center rounded-lg transition-colors"
                      style={{ color: 'var(--fg-subtle)' }}
                      onMouseEnter={e =>
                        (e.currentTarget.style.color = 'var(--fg-muted)')
                      }
                      onMouseLeave={e =>
                        (e.currentTarget.style.color = 'var(--fg-subtle)')
                      }
                    >
                      <IconEye visible={showPw} className="h-4 w-4" />
                    </button>
                  </div>
                </div>

                {/* Submit */}
                <button
                  type="submit"
                  disabled={loading}
                  aria-busy={loading}
                  className="mt-2 w-full rounded-xl px-4 py-3 text-sm font-semibold transition-all duration-150 disabled:cursor-not-allowed disabled:opacity-60"
                  style={{
                    background: 'var(--brand)',
                    color: '#fff',
                    boxShadow: 'var(--shadow-brand)',
                  }}
                  onMouseEnter={e => {
                    if (!loading) {
                      e.currentTarget.style.background = 'var(--brand-light)'
                      e.currentTarget.style.transform = 'translateY(-1px)'
                    }
                  }}
                  onMouseLeave={e => {
                    e.currentTarget.style.background = 'var(--brand)'
                    e.currentTarget.style.transform = 'translateY(0)'
                  }}
                >
                  {loading ? (
                    <span className="flex items-center justify-center gap-2">
                      <LoadingSpinner />
                      {mode === 'login' ? 'Signing in…' : 'Creating account…'}
                    </span>
                  ) : mode === 'login' ? (
                    'Sign in'
                  ) : (
                    'Create account'
                  )}
                </button>
              </div>
            </form>

            {/* Divider */}
            <div
              className="my-5 flex items-center gap-3"
              aria-hidden="true"
            >
              <div className="flex-1 h-px" style={{ background: 'var(--border)' }} />
              <span className="text-xs" style={{ color: 'var(--fg-subtle)' }}>
                or
              </span>
              <div className="flex-1 h-px" style={{ background: 'var(--border)' }} />
            </div>

            {/* Mode switch */}
            <button
              type="button"
              onClick={switchMode}
              className="w-full rounded-xl py-2.5 text-sm transition-colors duration-150"
              style={{
                border: '1px solid var(--border)',
                background: 'transparent',
                color: 'var(--fg-muted)',
              }}
              onMouseEnter={e => {
                e.currentTarget.style.borderColor = 'var(--border-hover)'
                e.currentTarget.style.color = 'var(--fg)'
              }}
              onMouseLeave={e => {
                e.currentTarget.style.borderColor = 'var(--border)'
                e.currentTarget.style.color = 'var(--fg-muted)'
              }}
            >
              {mode === 'login'
                ? "Don't have an account? Sign up"
                : 'Already have an account? Sign in'}
            </button>
          </div>

          {/* Footer note */}
          <p
            className="mt-5 text-center text-xs leading-relaxed"
            style={{ color: 'var(--fg-subtle)' }}
          >
            Files are scoped to your company workspace and never shared.
          </p>
        </div>
      </section>
    </main>
  )
}

/* ─── Sub-components ─────────────────────────────────────────────────────── */

function BrandIcon({ size = 'md' }: { size?: 'md' | 'lg' }) {
  const dim = size === 'lg' ? 'h-12 w-12' : 'h-9 w-9'
  const text = size === 'lg' ? 'text-xl' : 'text-base'
  return (
    <div
      className={`${dim} flex flex-shrink-0 items-center justify-center rounded-2xl icon-animate`}
      style={{
        background: 'linear-gradient(135deg, #f97316 0%, #ea6500 100%)',
        boxShadow: 'var(--shadow-brand)',
      }}
      aria-hidden="true"
    >
      <span className={`${text} font-black text-white`}>P</span>
    </div>
  )
}

function LoadingSpinner() {
  return (
    <svg
      className="h-4 w-4 animate-spin"
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
    >
      <circle
        cx="12" cy="12" r="10"
        stroke="rgba(255,255,255,0.25)"
        strokeWidth="3"
      />
      <path
        d="M12 2a10 10 0 0110 10"
        stroke="white"
        strokeWidth="3"
        strokeLinecap="round"
      />
    </svg>
  )
}

/* ─── Helpers ────────────────────────────────────────────────────────────── */

/**
 * Map raw Supabase error messages to user-friendly copy.
 * Avoids leaking internal error codes to the UI.
 */
function humanizeAuthError(msg: string): string {
  const lower = msg.toLowerCase()
  if (lower.includes('invalid login credentials') || lower.includes('invalid_credentials')) {
    return 'Incorrect email or password. Please try again.'
  }
  if (lower.includes('email not confirmed')) {
    return 'Please confirm your email address before signing in.'
  }
  if (lower.includes('user already registered') || lower.includes('already registered')) {
    return 'An account with this email already exists. Try signing in instead.'
  }
  if (lower.includes('rate limit') || lower.includes('too many')) {
    return 'Too many attempts. Please wait a moment and try again.'
  }
  if (lower.includes('network') || lower.includes('fetch')) {
    return 'Connection error. Check your internet and try again.'
  }
  if (lower.includes('password') && lower.includes('short')) {
    return 'Password must be at least 6 characters.'
  }
  // Fallback: return original but strip brackets/codes
  return msg.replace(/\[.*?\]/g, '').trim() || 'Something went wrong. Please try again.'
}
