import { useCallback, useEffect, useRef, useState } from 'react'
import './App.css'

interface Task {
  id: string
  title: string
  status: number
  priority: number
  dueDate: string
  completedTime: string
  desc: string
  projectName: string
}

interface SyncResult {
  date: string
  page_id: string
  page_url: string
  tasks_synced: number
  created: boolean
}

interface WeeklyItem {
  id: string
  name: string
  done: boolean
  week: string
  priority: string
}

interface WeekData {
  week_start: string
  week_end: string
  items: WeeklyItem[]
}

interface HistoryEntry {
  job_id: string
  status: string
  date: string
  kind?: string
  result?: SyncResult & { pages?: unknown[] }
  error?: string
  finished_at: string
}

const PRIORITIES = ['1. High', '2. Medium', '3. Low'] as const

const todayStr = () => {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

const shiftDate = (dateStr: string, days: number) => {
  const d = new Date(dateStr + 'T00:00:00')
  d.setDate(d.getDate() + days)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

const fmtDate = (dateStr: string) => {
  const d = new Date(dateStr + 'T00:00:00')
  return d.toLocaleDateString('en-US', { weekday: 'long', month: 'short', day: 'numeric' })
}

const prioColor = (p: number) => (p >= 5 ? 'prio-high' : p >= 3 ? 'prio-med' : p >= 1 ? 'prio-low' : 'prio-none')

const authHeaders = (): Record<string, string> => {
  const token = localStorage.getItem('auth_token') || ''
  return token ? { Authorization: `Bearer ${token}` } : {}
}

async function apiFetch(url: string, opts: RequestInit = {}): Promise<Response> {
  const resp = await fetch(url, {
    ...opts,
    headers: { ...authHeaders(), ...(opts.headers || {}) },
  })
  if (resp.status === 401) {
    localStorage.removeItem('auth_token')
    window.dispatchEvent(new Event('auth-expired'))
    throw new Error('Unauthorized — log in again')
  }
  return resp
}

function App() {
  const [authed, setAuthed] = useState<boolean | null>(null)
  const [password, setPassword] = useState('')
  const [authError, setAuthError] = useState('')

  useEffect(() => {
    const token = localStorage.getItem('auth_token')
    if (!token) { setAuthed(false); return }
    fetch('/api/auth-check', { headers: authHeaders() })
      .then(r => setAuthed(r.ok))
      .catch(() => setAuthed(false))
  }, [])

  useEffect(() => {
    const onExpired = () => setAuthed(false)
    window.addEventListener('auth-expired', onExpired)
    return () => window.removeEventListener('auth-expired', onExpired)
  }, [])

  const login = async (e: React.FormEvent) => {
    e.preventDefault()
    setAuthError('')
    try {
      const resp = await fetch('/api/auth-check', { headers: { Authorization: `Bearer ${password}` } })
      if (resp.ok) {
        localStorage.setItem('auth_token', password)
        setAuthed(true)
      } else {
        setAuthError('Wrong password')
      }
    } catch {
      setAuthError('Connection failed')
    }
  }

  if (authed === null) return null
  if (!authed) {
    return (
      <div className="shell login-shell">
        <form className="login-card" onSubmit={login}>
          <div className="brand-icon">⇄</div>
          <h1>TickTick → Notion</h1>
          <p>Enter the dashboard password</p>
          <input
            type="password"
            value={password}
            onChange={e => setPassword(e.target.value)}
            placeholder="Password"
            autoFocus
          />
          {authError && <div className="login-error">{authError}</div>}
          <button className="btn btn-primary" type="submit">Log in</button>
        </form>
      </div>
    )
  }

  return <Dashboard onLogout={() => { localStorage.removeItem('auth_token'); setAuthed(false) }} />
}

function Dashboard({ onLogout }: { onLogout: () => void }) {
  const [date, setDate] = useState(todayStr)
  const [tasks, setTasks] = useState<Task[]>([])
  const [loading, setLoading] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [result, setResult] = useState<SyncResult | null>(null)
  const [error, setError] = useState('')
  const [health, setHealth] = useState<{ ticktick: boolean; notion: boolean } | null>(null)

  const [week, setWeek] = useState<WeekData | null>(null)
  const [weekLoading, setWeekLoading] = useState(false)
  const [newItem, setNewItem] = useState('')
  const [newPrio, setNewPrio] = useState<string>('2. Medium')
  const [addingItem, setAddingItem] = useState(false)
  const [weekSyncing, setWeekSyncing] = useState(false)

  const [history, setHistory] = useState<HistoryEntry[]>([])
  const [tab, setTab] = useState<'day' | 'week' | 'history'>('day')

  const pollRef = useRef<number | null>(null)

  useEffect(() => {
    fetch('/api/health')
      .then(r => r.json())
      .then(d => setHealth({ ticktick: d.ticktick_configured, notion: d.notion_configured }))
      .catch(() => setHealth({ ticktick: false, notion: false }))
  }, [])

  const loadHistory = useCallback(() => {
    fetch('/api/sync/history')
      .then(r => r.json())
      .then(d => setHistory(d.history || []))
      .catch(() => {})
  }, [])

  useEffect(() => {
    loadHistory()
    const iv = setInterval(loadHistory, 10000)
    return () => clearInterval(iv)
  }, [loadHistory])

  const fetchTasks = useCallback(async (d: string) => {
    setLoading(true)
    setError('')
    setTasks([])
    setResult(null)
    try {
      const resp = await apiFetch(`/api/tasks/${d}`)
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const data = await resp.json()
      setTasks(data.tasks)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to fetch tasks')
    } finally {
      setLoading(false)
    }
  }, [])

  const fetchWeek = useCallback(async (d: string) => {
    setWeekLoading(true)
    try {
      const resp = await apiFetch(`/api/week/${d}`)
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      setWeek(await resp.json())
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load weekly items')
    } finally {
      setWeekLoading(false)
    }
  }, [])

  // Auto-load on date change
  useEffect(() => {
    fetchTasks(date)
    fetchWeek(date)
  }, [date, fetchTasks, fetchWeek])

  useEffect(() => () => { if (pollRef.current) window.clearTimeout(pollRef.current) }, [])

  const pollJob = async (jobId: string, onDone: (entry: Record<string, unknown>) => void) => {
    for (let i = 0; i < 60; i++) {
      await new Promise(r => setTimeout(r, 1000))
      const resp = await apiFetch(`/api/sync/status/${jobId}`)
      const status = await resp.json()
      if (status.status === 'done') { onDone(status); return }
      if (status.status === 'error') throw new Error(status.error || 'Job failed')
    }
    throw new Error('Sync timed out')
  }

  const syncTasks = async () => {
    setSyncing(true)
    setError('')
    setResult(null)
    try {
      const resp = await apiFetch(`/api/sync/${date}`, { method: 'POST' })
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const { job_id } = await resp.json()
      await pollJob(job_id, status => {
        setResult(status.result as SyncResult)
        loadHistory()
      })
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Sync failed')
    } finally {
      setSyncing(false)
    }
  }

  const syncWeek = async () => {
    setWeekSyncing(true)
    setError('')
    try {
      const resp = await apiFetch(`/api/sync-week/${date}`, { method: 'POST' })
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const { job_id } = await resp.json()
      await pollJob(job_id, () => loadHistory())
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Weekly sync failed')
    } finally {
      setWeekSyncing(false)
    }
  }

  const addItem = async () => {
    const name = newItem.trim()
    if (!name) return
    setAddingItem(true)
    try {
      const resp = await apiFetch(`/api/week/${date}/items`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ name, priority: newPrio }),
      })
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      setNewItem('')
      await fetchWeek(date)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to add item')
    } finally {
      setAddingItem(false)
    }
  }

  const toggleItem = async (item: WeeklyItem) => {
    // Optimistic update
    setWeek(w => w ? { ...w, items: w.items.map(i => i.id === item.id ? { ...i, done: !i.done } : i) } : w)
    try {
      const resp = await apiFetch(`/api/items/${item.id}/done`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ done: !item.done }),
      })
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    } catch {
      // Revert on failure
      setWeek(w => w ? { ...w, items: w.items.map(i => i.id === item.id ? { ...i, done: item.done } : i) } : w)
      setError('Failed to update item')
    }
  }

  const changePrio = async (item: WeeklyItem, priority: string) => {
    setWeek(w => w ? { ...w, items: w.items.map(i => i.id === item.id ? { ...i, priority } : i) } : w)
    try {
      const resp = await apiFetch(`/api/items/${item.id}/priority`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ priority }),
      })
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    } catch {
      setError('Failed to update priority')
    }
  }

  const removeItem = async (item: WeeklyItem) => {
    setWeek(w => w ? { ...w, items: w.items.filter(i => i.id !== item.id) } : w)
    try {
      const resp = await apiFetch(`/api/items/${item.id}`, { method: 'DELETE' })
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    } catch {
      setError('Failed to delete item')
      fetchWeek(date)
    }
  }

  const moveItem = async (item: WeeklyItem) => {
    setWeek(w => w ? { ...w, items: w.items.filter(i => i.id !== item.id) } : w)
    try {
      const resp = await apiFetch(`/api/items/${item.id}/move`, { method: 'POST' })
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    } catch {
      setError('Failed to move item')
      fetchWeek(date)
    }
  }

  const doneCount = tasks.filter(t => t.status === 2).length
  const weekDone = week?.items.filter(i => i.done).length ?? 0
  const weekTotal = week?.items.length ?? 0
  const allOk = health?.ticktick && health?.notion

  return (
    <div className="shell">
      <header className="header">
        <div className="brand">
          <div className="brand-icon">⇄</div>
          <div>
            <h1>TickTick → Notion</h1>
            <p>Sync dashboard</p>
          </div>
        </div>
        <div className="status-dot">
          <span className={`dot ${allOk ? '' : 'off'}`} />
          {allOk ? 'Connected' : 'Check config'}
          <button className="logout-btn" onClick={onLogout} title="Log out">⎋</button>
        </div>
      </header>

      <div className="stats">
        <div className="stat">
          <div className="label">Tasks today</div>
          <div className="value">{doneCount}/{tasks.length}</div>
        </div>
        <div className="stat">
          <div className="label">Week progress</div>
          <div className="value accent">{weekDone}/{weekTotal}</div>
        </div>
        <div className="stat">
          <div className="label">Syncs run</div>
          <div className="value">{history.filter(h => h.status === 'done').length}</div>
        </div>
        <div className="stat">
          <div className="label">Failures</div>
          <div className={`value ${history.some(h => h.status === 'error') ? '' : 'green'}`}>
            {history.filter(h => h.status === 'error').length}
          </div>
        </div>
      </div>

      <div className="datebar">
        <button className="nav-btn" onClick={() => setDate(d => shiftDate(d, -1))} title="Previous day">‹</button>
        <input type="date" value={date} onChange={e => e.target.value && setDate(e.target.value)} />
        <button className="nav-btn" onClick={() => setDate(d => shiftDate(d, 1))} title="Next day">›</button>
        <button className="today-btn" onClick={() => setDate(todayStr())}>Today</button>
        <div className="spacer" />
        <button className="btn btn-primary" onClick={syncTasks} disabled={syncing}>
          {syncing ? <><span className="spinner" /> Syncing…</> : <>Sync {fmtDate(date).split(',')[0]} to Notion</>}
        </button>
      </div>

      {error && <div className="alert error">⚠ {error}</div>}
      {result && (
        <div className="alert success">
          ✓ {result.created ? 'Created' : 'Updated'} journal page for {result.date} — {result.tasks_synced} tasks synced.
        </div>
      )}

      <div className="tabs">
        <button className={`tab ${tab === 'day' ? 'active' : ''}`} onClick={() => setTab('day')}>Day</button>
        <button className={`tab ${tab === 'week' ? 'active' : ''}`} onClick={() => setTab('week')}>Week</button>
        <button className={`tab ${tab === 'history' ? 'active' : ''}`} onClick={() => setTab('history')}>History</button>
      </div>

      {tab === 'day' && (
        <div className="card">
          <div className="card-head">
            <h2>📋 {fmtDate(date)}</h2>
            {tasks.length > 0 && <span className="count">{doneCount}/{tasks.length} done</span>}
          </div>
          {tasks.length > 0 && (
            <div className="progress">
              <div
                className={`fill ${doneCount === tasks.length ? 'complete' : ''}`}
                style={{ width: `${tasks.length ? (doneCount / tasks.length) * 100 : 0}%` }}
              />
            </div>
          )}
          <div className="card-body">
            {loading ? (
              <div className="empty"><span className="spinner dark" style={{ display: 'inline-block' }} /></div>
            ) : tasks.length === 0 ? (
              <div className="empty">
                <div className="icon">🌤</div>
                No tasks for this day
              </div>
            ) : (
              tasks.map(t => (
                <div className="row" key={t.id}>
                  <span className={`checkbox static ${t.status === 2 ? 'checked' : ''}`}>✓</span>
                  <span className={`prio-dot ${prioColor(t.priority)}`} />
                  <span className={`title ${t.status === 2 ? 'done' : ''}`}>{t.title}</span>
                  <span className="proj-tag">{t.projectName || 'Inbox'}</span>
                </div>
              ))
            )}
          </div>
        </div>
      )}

      {tab === 'week' && (
        <div className="card">
          <div className="week-banner">
            <span>Week of <strong>{week ? fmtDate(week.week_start) : '…'}</strong> → <strong>{week ? fmtDate(week.week_end) : ''}</strong></span>
            <button className="btn btn-ghost" onClick={syncWeek} disabled={weekSyncing} style={{ padding: '6px 14px', fontSize: 13 }}>
              {weekSyncing ? <><span className="spinner dark" /> Syncing…</> : '↻ Push to journal'}
            </button>
          </div>
          {weekTotal > 0 && (
            <div className="progress">
              <div
                className={`fill ${weekDone === weekTotal ? 'complete' : ''}`}
                style={{ width: `${weekTotal ? (weekDone / weekTotal) * 100 : 0}%` }}
              />
            </div>
          )}
          <div className="card-body">
            {weekLoading ? (
              <div className="empty"><span className="spinner dark" style={{ display: 'inline-block' }} /></div>
            ) : weekTotal === 0 ? (
              <div className="empty">
                <div className="icon">🗓</div>
                No weekly items yet — add your first below
              </div>
            ) : (
              week!.items.map(item => (
                <div className="row" key={item.id}>
                  <button className={`checkbox ${item.done ? 'checked' : ''}`} onClick={() => toggleItem(item)}>✓</button>
                  <span className={`title ${item.done ? 'done' : ''}`}>{item.name}</span>
                  <select
                    className="prio-select"
                    value={item.priority}
                    onChange={e => changePrio(item, e.target.value)}
                  >
                    {PRIORITIES.map(p => <option key={p} value={p}>{p}</option>)}
                    {!item.priority && <option value="">—</option>}
                  </select>
                  <button className="btn-ghost-move" onClick={() => moveItem(item)} title="Move to next week">→</button>
                  <button className="btn-danger-ghost" onClick={() => removeItem(item)} title="Delete">✕</button>
                </div>
              ))
            )}
          </div>
          <div className="add-row">
            <input
              type="text"
              placeholder="Add a weekly item…"
              value={newItem}
              onChange={e => setNewItem(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && addItem()}
            />
            <select value={newPrio} onChange={e => setNewPrio(e.target.value)}>
              {PRIORITIES.map(p => <option key={p} value={p}>{p}</option>)}
            </select>
            <button className="btn btn-primary" onClick={addItem} disabled={addingItem || !newItem.trim()}>
              {addingItem ? '…' : 'Add'}
            </button>
          </div>
        </div>
      )}

      {tab === 'history' && (
        <div className="card">
          <div className="card-head">
            <h2>🕘 Recent syncs</h2>
            <span className="count">{history.length}</span>
          </div>
          <div className="card-body">
            {history.length === 0 ? (
              <div className="empty">
                <div className="icon">💤</div>
                No syncs run yet in this session
              </div>
            ) : (
              history.map(h => (
                <div className="hist-row" key={h.job_id}>
                  <span className={`badge ${h.status}`}>{h.status}</span>
                  <span>{h.kind === 'week' ? 'Weekly items' : `Daily ${h.date}`}</span>
                  {h.status === 'error' && <span style={{ color: 'var(--red)', fontSize: 12 }}>{h.error}</span>}
                  <span className="when">
                    {new Date(h.finished_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                  </span>
                </div>
              ))
            )}
          </div>
        </div>
      )}

      <footer className="footer">
        TickTick → Notion Sync · runs every 2h via launchd · <a href="https://ticktick.com" target="_blank" rel="noopener noreferrer">TickTick</a> · <a href="https://notion.so" target="_blank" rel="noopener noreferrer">Notion</a>
      </footer>
    </div>
  )
}

export default App
