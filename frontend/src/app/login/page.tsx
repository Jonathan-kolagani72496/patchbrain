'use client'

import { useState } from 'react'
import { useRouter } from 'next/navigation'
import { supabase } from '@/lib/supabase'

type AuthMode = 'login' | 'signup'

export default function LoginPage() {
  const router = useRouter()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [mode, setMode] = useState<AuthMode>('login')

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setLoading(true)
    setError('')
    setMessage('')

    try {
      if (mode === 'login') {
        const { error } = await supabase.auth.signInWithPassword({ email, password })
        if (error) throw error
        router.replace('/dashboard')
        return
      }

      const { error } = await supabase.auth.signUp({ email, password })
      if (error) throw error
      setMessage('Account created. Check your email if confirmation is enabled, then sign in.')
      setMode('login')
    } catch (err: any) {
      setError(err?.message || 'Authentication failed')
    } finally {
      setLoading(false)
    }
  }

  function switchMode() {
    setMode(prev => (prev === 'login' ? 'signup' : 'login'))
    setError('')
    setMessage('')
  }

  return (
    <main className="min-h-screen bg-slate-950 text-white grid lg:grid-cols-[1.1fr_0.9fr]">
      <section className="hidden lg:flex flex-col justify-between border-r border-white/10 bg-gradient-to-br from-slate-950 via-slate-900 to-slate-950 p-10">
        <div className="flex items-center gap-3">
          <div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-orange-500 shadow-lg shadow-orange-500/20">
            <span className="text-lg font-black">P</span>
          </div>
          <div>
            <p className="font-semibold tracking-tight">PatchBrain</p>
            <p className="text-xs text-slate-500">Oilfield operations intelligence</p>
          </div>
        </div>

        <div className="max-w-xl">
          <div className="mb-5 inline-flex rounded-full border border-orange-400/20 bg-orange-400/10 px-3 py-1 text-xs font-medium text-orange-200">
            Built for dispatch, compliance, tickets, invoices, and field records
          </div>
          <h1 className="text-5xl font-semibold tracking-tight leading-tight">
            Turn messy oilfield files into operational answers.
          </h1>
          <p className="mt-5 max-w-lg text-sm leading-7 text-slate-400">
            Upload run tickets, invoices, safety records, compliance PDFs, spreadsheets, and field communications. Ask direct questions and get answers with source evidence.
          </p>
        </div>

        <div className="grid grid-cols-3 gap-3 text-xs text-slate-400">
          {['Private company workspace', 'Cited answers', 'Deterministic calculations'].map(item => (
            <div key={item} className="rounded-2xl border border-white/10 bg-white/[0.03] p-4">
              <div className="mb-2 h-1.5 w-8 rounded-full bg-orange-400" />
              {item}
            </div>
          ))}
        </div>
      </section>

      <section className="flex min-h-screen items-center justify-center px-5 py-10">
        <div className="w-full max-w-md">
          <div className="mb-8 text-center lg:hidden">
            <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-2xl bg-orange-500 shadow-lg shadow-orange-500/20">
              <span className="text-xl font-black">P</span>
            </div>
            <h1 className="text-2xl font-semibold">PatchBrain</h1>
            <p className="mt-1 text-sm text-slate-500">Oilfield operations intelligence</p>
          </div>

          <div className="rounded-3xl border border-white/10 bg-white/[0.04] p-7 shadow-2xl shadow-black/40 backdrop-blur">
            <div className="mb-6">
              <p className="text-xs font-medium uppercase tracking-[0.22em] text-orange-300">
                {mode === 'login' ? 'Welcome back' : 'Create workspace access'}
              </p>
              <h2 className="mt-2 text-2xl font-semibold tracking-tight">
                {mode === 'login' ? 'Sign in to PatchBrain' : 'Create your account'}
              </h2>
              <p className="mt-2 text-sm text-slate-400">
                {mode === 'login'
                  ? 'Use your company account to access files and chat history.'
                  : 'Start with email/password. Company assignment is handled by your backend user record.'}
              </p>
            </div>

            <form onSubmit={handleSubmit} className="space-y-4">
              <div>
                <label className="mb-1.5 block text-sm font-medium text-slate-300">Email</label>
                <input
                  type="email"
                  value={email}
                  onChange={e => setEmail(e.target.value)}
                  required
                  autoComplete="email"
                  className="w-full rounded-xl border border-white/10 bg-slate-950/70 px-4 py-3 text-sm text-white outline-none transition focus:border-orange-400/70 focus:ring-4 focus:ring-orange-400/10"
                  placeholder="you@company.com"
                />
              </div>

              <div>
                <label className="mb-1.5 block text-sm font-medium text-slate-300">Password</label>
                <input
                  type="password"
                  value={password}
                  onChange={e => setPassword(e.target.value)}
                  required
                  autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
                  className="w-full rounded-xl border border-white/10 bg-slate-950/70 px-4 py-3 text-sm text-white outline-none transition focus:border-orange-400/70 focus:ring-4 focus:ring-orange-400/10"
                  placeholder="••••••••"
                />
              </div>

              {error && (
                <div className="rounded-xl border border-red-400/20 bg-red-400/10 px-4 py-3 text-sm text-red-200">
                  {error}
                </div>
              )}

              {message && (
                <div className="rounded-xl border border-emerald-400/20 bg-emerald-400/10 px-4 py-3 text-sm text-emerald-200">
                  {message}
                </div>
              )}

              <button
                type="submit"
                disabled={loading}
                className="w-full rounded-xl bg-orange-500 px-4 py-3 text-sm font-semibold text-white shadow-lg shadow-orange-500/20 transition hover:bg-orange-400 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {loading ? 'Working…' : mode === 'login' ? 'Sign in' : 'Create account'}
              </button>
            </form>

            <div className="mt-5 text-center">
              <button onClick={switchMode} className="text-sm text-slate-400 transition hover:text-white">
                {mode === 'login' ? "Don't have an account? Sign up" : 'Already have an account? Sign in'}
              </button>
            </div>
          </div>

          <p className="mt-6 text-center text-xs text-slate-600">
            Files stay scoped to your company workspace.
          </p>
        </div>
      </section>
    </main>
  )
}
