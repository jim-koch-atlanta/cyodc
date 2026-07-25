// Thin wrapper over the M1 backend contract. Relative URLs are proxied to
// FastAPI by Vite in dev (see vite.config.js).

const JSON_HEADERS = { 'Content-Type': 'application/json' }

export async function createSession() {
  const res = await fetch('/api/session', { method: 'POST' })
  if (!res.ok) throw new Error(`create session failed (${res.status})`)
  return res.json()
}

// Returns null when the session no longer exists (e.g. server DB was reset).
export async function getSession(sessionId) {
  const res = await fetch(`/api/session/${sessionId}`)
  if (res.status === 404) return null
  if (!res.ok) throw new Error(`get session failed (${res.status})`)
  return res.json()
}

export async function sendTurn(sessionId, message) {
  const res = await fetch(`/api/session/${sessionId}/turn`, {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify({ message }),
  })
  if (!res.ok) throw new Error(`turn failed (${res.status})`)
  return res.json()
}
