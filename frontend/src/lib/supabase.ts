import { createClient } from '@supabase/supabase-js'

const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL
const supabaseAnonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY

if (!supabaseUrl || !supabaseAnonKey) {
  throw new Error('Missing Supabase frontend env vars')
}

export const API_URL =
  process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

export const supabase = createClient(supabaseUrl, supabaseAnonKey)

export async function getAuthHeader() {
  const { data, error } = await supabase.auth.getSession()

  if (error || !data.session) {
    throw new Error('Not authenticated')
  }

  return {
    Authorization: `Bearer ${data.session.access_token}`,
  }
}