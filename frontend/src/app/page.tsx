'use client'

import { useEffect } from 'react'
import { useRouter } from 'next/navigation'
import { supabase } from '@/lib/supabase'

export default function Home() {
  const router = useRouter()

  useEffect(() => {
    let alive = true

    supabase.auth.getSession().then(({ data }) => {
      if (!alive) return
      router.replace(data.session ? '/dashboard' : '/login')
    })

    return () => {
      alive = false
    }
  }, [router])

  return (
    <main className="min-h-screen bg-slate-950 text-white flex items-center justify-center">
      <div className="flex items-center gap-3 rounded-2xl border border-white/10 bg-white/[0.03] px-5 py-4 shadow-2xl shadow-black/30">
        <span className="h-3 w-3 rounded-full bg-orange-400 animate-pulse" />
        <span className="text-sm text-slate-300">Opening PatchBrain…</span>
      </div>
    </main>
  )
}
