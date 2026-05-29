'use client'

import { useEffect } from 'react'
import { useRouter } from 'next/navigation'
import { supabase } from '@/lib/supabase'

/**
 * Root page: immediately redirects to /dashboard (authenticated) or /login.
 * Renders a minimal, accessible loading state while the session check resolves.
 */
export default function Home() {
  const router = useRouter()

  useEffect(() => {
    let alive = true

    supabase.auth.getSession().then(({ data, error }) => {
      if (!alive) return
      // On any error, send to login — don't leave user stranded
      router.replace(data.session && !error ? '/dashboard' : '/login')
    })

    return () => {
      alive = false
    }
  }, [router])

  return (
    /*
     * role="status" + aria-live="polite": screen readers announce loading state
     * without interrupting ongoing speech (WCAG 4.1.3 Status Messages)
     */
    <main
      className="min-h-dvh flex items-center justify-center"
      style={{ background: 'var(--bg)' }}
      aria-label="PatchBrain application"
    >
      <div
        role="status"
        aria-live="polite"
        aria-label="Loading PatchBrain, please wait"
        className="flex items-center gap-3 rounded-2xl px-5 py-4"
        style={{
          border: '1px solid var(--border)',
          background: 'var(--bg-card)',
          boxShadow: 'var(--shadow-card)',
        }}
      >
        {/* Animated brand dot */}
        <span
          aria-hidden="true"
          className="block h-2.5 w-2.5 rounded-full brand-pulse"
          style={{ background: 'var(--brand)' }}
        />
        <span className="text-sm" style={{ color: 'var(--fg-muted)' }}>
          Opening PatchBrain…
        </span>

        {/* Visually hidden text for screen readers that may not read aria-label */}
        <span className="sr-only">Loading, redirecting shortly.</span>
      </div>
    </main>
  )
}
