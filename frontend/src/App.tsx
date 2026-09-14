import { useState } from 'react'

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

function App() {
  const [date, setDate] = useState(() => new Date().toISOString().split('T')[0])
  const [tasks, setTasks] = useState<Task[]>([])
  const [loading, setLoading] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [result, setResult] = useState<SyncResult | null>(null)
  const [error, setError] = useState('')

  const fetchTasks = async () => {
    setLoading(true)
    setError('')
    setTasks([])
    setResult(null)
    try {
      const resp = await fetch(`/api/tasks/${date}`)
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const data = await resp.json()
      setTasks(data.tasks)
    } catch (e: any) {
      setError(e.message || 'Failed to fetch tasks')
    } finally {
      setLoading(false)
    }
  }

  const syncTasks = async () => {
    setSyncing(true)
    setError('')
    setResult(null)
    try {
      // Start sync job
      const resp = await fetch(`/api/sync/${date}`, { method: 'POST' })
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const { job_id } = await resp.json()

      // Poll for completion
      let attempts = 0
      while (attempts < 30) {
        await new Promise(r => setTimeout(r, 1000))
        const statusResp = await fetch(`/api/sync/status/${job_id}`)
        const status = await statusResp.json()
        if (status.status === 'done') {
          setResult(status.result)
          break
        }
        if (status.status === 'error') {
          throw new Error(status.error)
        }
        attempts++
      }
      if (attempts >= 30) throw new Error('Sync timed out')
    } catch (e: any) {
      setError(e.message || 'Sync failed')
    } finally {
      setSyncing(false)
    }
  }

  const priorityEmoji = (p: number) => {
    if (p >= 5) return '🔴 '
    if (p >= 3) return '🟡 '
    if (p >= 1) return '🟢 '
    return ''
  }

  return (
    <div style={{ maxWidth: 600, margin: '40px auto', fontFamily: 'system-ui, sans-serif', padding: '0 20px' }}>
      <h1>TickTick → Notion Sync</h1>

      <div style={{ display: 'flex', gap: 12, alignItems: 'center', marginBottom: 24 }}>
        <input
          type="date"
          value={date}
          onChange={e => setDate(e.target.value)}
          style={{ padding: '8px 12px', fontSize: 16, borderRadius: 6, border: '1px solid #ccc' }}
        />
        <button
          onClick={fetchTasks}
          disabled={loading}
          style={{ padding: '8px 16px', fontSize: 16, borderRadius: 6, cursor: loading ? 'wait' : 'pointer' }}
        >
          {loading ? 'Loading...' : 'Fetch Tasks'}
        </button>
        <button
          onClick={syncTasks}
          disabled={syncing || tasks.length === 0}
          style={{
            padding: '8px 16px',
            fontSize: 16,
            borderRadius: 6,
            cursor: syncing ? 'wait' : tasks.length === 0 ? 'not-allowed' : 'pointer',
            opacity: tasks.length === 0 ? 0.5 : 1,
            background: '#2563eb',
            color: 'white',
            border: 'none',
          }}
        >
          {syncing ? 'Syncing...' : 'Sync to Notion'}
        </button>
      </div>

      {error && (
        <div style={{ padding: 12, background: '#fef2f2', color: '#dc2626', borderRadius: 6, marginBottom: 16 }}>
          {error}
        </div>
      )}

      {result && (
        <div style={{ padding: 12, background: '#f0fdf4', color: '#16a34a', borderRadius: 6, marginBottom: 16 }}>
          <strong>Sync complete!</strong>{' '}
          {result.created ? 'Created new page' : 'Updated existing page'} for {result.date}.{' '}
          {result.tasks_synced} tasks synced.{' '}
          <a href={result.page_url} target="_blank" rel="noopener noreferrer" style={{ color: '#16a34a' }}>
            Open in Notion →
          </a>
        </div>
      )}

      {tasks.length > 0 && (
        <div>
          <h2>Tasks for {date} ({tasks.length})</h2>
          <ul style={{ listStyle: 'none', padding: 0 }}>
            {tasks.map(t => (
              <li
                key={t.id}
                style={{
                  padding: '8px 12px',
                  borderBottom: '1px solid #e5e7eb',
                  display: 'flex',
                  alignItems: 'center',
                  gap: 8,
                }}
              >
                <input type="checkbox" checked={t.status === 2} readOnly />
                <span>
                  {priorityEmoji(t.priority)}
                  {t.title}
                </span>
                <span style={{ color: '#9ca3af', fontSize: 12, marginLeft: 'auto' }}>
                  {t.projectName}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {!loading && tasks.length === 0 && !error && !result && (
        <p style={{ color: '#6b7280' }}>Select a date and click "Fetch Tasks" to preview.</p>
      )}
    </div>
  )
}

export default App
