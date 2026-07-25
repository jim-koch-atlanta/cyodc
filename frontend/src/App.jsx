import { useEffect, useRef, useState } from 'react'
import { createSession, getSession, sendTurn } from './api'

const STORAGE_KEY = 'cyodc_session'

export default function App() {
  const [messages, setMessages] = useState([])
  const [sessionId, setSessionId] = useState(null)
  const [llmMode, setLlmMode] = useState(null)
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const scrollRef = useRef(null)

  // Boot: resume the stored session, or start a fresh run.
  useEffect(() => {
    let cancelled = false
    async function boot() {
      try {
        const stored = localStorage.getItem(STORAGE_KEY)
        if (stored) {
          const existing = await getSession(stored)
          if (existing && !cancelled) {
            applySession(existing)
            return
          }
        }
        const fresh = await createSession()
        if (cancelled) return
        localStorage.setItem(STORAGE_KEY, fresh.session_id)
        applySession(fresh)
      } catch (e) {
        if (!cancelled) setError(String(e.message || e))
      }
    }
    boot()
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Keep the newest narration in view.
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages, busy])

  function applySession(data) {
    setSessionId(data.session_id)
    setMessages(data.messages)
    setLlmMode(data.llm_mode)
  }

  async function onSubmit(e) {
    e.preventDefault()
    const text = input.trim()
    if (!text || busy || !sessionId) return

    setInput('')
    setError(null)
    setBusy(true)
    setMessages((m) => [...m, { role: 'player', content: text }])
    try {
      const res = await sendTurn(sessionId, text)
      setMessages((m) => [...m, { role: 'dm', content: res.reply }])
    } catch (e) {
      setError(String(e.message || e))
    } finally {
      setBusy(false)
    }
  }

  async function onNewRun() {
    setError(null)
    setBusy(true)
    try {
      const fresh = await createSession()
      localStorage.setItem(STORAGE_KEY, fresh.session_id)
      applySession(fresh)
    } catch (e) {
      setError(String(e.message || e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          THE DELVE <span className="brand-sub">// season 312</span>
        </div>
        <div className="topbar-right">
          {llmMode === 'stub' && (
            <span className="badge badge-warn" title="No ANTHROPIC_API_KEY set — canned narration.">
              stub narration
            </span>
          )}
          <button className="ghost-btn" onClick={onNewRun} disabled={busy}>
            new run
          </button>
        </div>
      </header>

      <main className="narration" ref={scrollRef}>
        {messages.length === 0 && !error && <div className="loading">Tuning the broadcast…</div>}
        {messages.map((m, i) => (
          <p key={i} className={`line line-${m.role}`}>
            {m.role === 'player' && <span className="prompt-caret">&gt; </span>}
            {m.content}
          </p>
        ))}
        {busy && <p className="line line-dm thinking">the announcer considers you…</p>}
        {error && <p className="line line-error">⚠ {error}</p>}
      </main>

      <form className="composer" onSubmit={onSubmit}>
        <span className="composer-caret">&gt;</span>
        <input
          className="composer-input"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={sessionId ? 'What do you do?' : 'connecting…'}
          disabled={busy || !sessionId}
          autoFocus
          autoComplete="off"
        />
        <button className="send-btn" type="submit" disabled={busy || !sessionId || !input.trim()}>
          send
        </button>
      </form>
    </div>
  )
}
