import type { Metadata, Viewport } from 'next'
import { IBM_Plex_Sans, IBM_Plex_Mono } from 'next/font/google'
import './globals.css'

const ibmPlexSans = IBM_Plex_Sans({
  subsets: ['latin'],
  weight: ['300', '400', '500', '600', '700'],
  style: ['normal', 'italic'],
  variable: '--font-sans',
  display: 'swap',
})

const ibmPlexMono = IBM_Plex_Mono({
  subsets: ['latin'],
  weight: ['400', '500'],
  variable: '--font-mono',
  display: 'swap',
})

export const metadata: Metadata = {
  title: {
    default: 'PatchBrain',
    template: '%s · PatchBrain',
  },
  description:
    'Oilfield document intelligence — ask questions across run tickets, invoices, compliance records, and field reports.',
  keywords: ['oilfield', 'document AI', 'field operations', 'compliance', 'run tickets', 'invoices'],
  robots: {
    index: false, // Private SaaS — don't index
    follow: false,
  },
}

export const viewport: Viewport = {
  themeColor: '#060910',
  colorScheme: 'dark',
  width: 'device-width',
  initialScale: 1,
}

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode
}>) {
  return (
    <html
      lang="en"
      className={`${ibmPlexSans.variable} ${ibmPlexMono.variable}`}
    >
      {/*
        No body className from font — we set font-family in CSS via variable.
        This keeps font token consistent with the design system.
      */}
      <body>{children}</body>
    </html>
  )
}
