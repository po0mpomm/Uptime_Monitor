'use client';

import { useEffect, useState, useCallback, useRef } from 'react';
import Link from 'next/link';
import { api, type Monitor } from '../lib/api';
import { StatusBadge } from './StatusBadge';

const POLL_INTERVAL_MS = 8000;

function formatRelativeTime(dateStr: string): string {
  const diff = Math.floor((Date.now() - new Date(dateStr).getTime()) / 1000);
  if (diff < 5) return 'just now';
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function getResponseTimeClass(ms: number | null): string {
  if (ms === null) return 'none';
  if (ms < 200) return 'fast';
  if (ms < 800) return 'medium';
  return 'slow';
}

interface Props {
  initial: Monitor[];
}

export function UrlTable({ initial }: Props) {
  const [monitors, setMonitors] = useState<Monitor[]>(initial);
  const [checkingIds, setCheckingIds] = useState<Set<string>>(new Set());
  const [deletingIds, setDeletingIds] = useState<Set<string>>(new Set());
  const [, setTick] = useState(0); // force relative-time re-renders
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const fetchMonitors = useCallback(async () => {
    try {
      const data = await api.monitors.list();
      setMonitors(data);
    } catch {
      // Silently ignore poll errors — don't flash an error on a background poll
    }
  }, []);

  // Poll every 8 seconds
  useEffect(() => {
    pollRef.current = setInterval(fetchMonitors, POLL_INTERVAL_MS);
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [fetchMonitors]);

  // Re-render relative timestamps every 30 seconds
  useEffect(() => {
    const t = setInterval(() => setTick(n => n + 1), 30000);
    return () => clearInterval(t);
  }, []);

  async function handleCheckNow(id: string) {
    setCheckingIds(s => new Set(s).add(id));
    try {
      await api.monitors.check(id);
      await fetchMonitors(); // refresh immediately after manual check
    } catch {
      // Silently absorb — the UI will show state on next poll
    } finally {
      setCheckingIds(s => { const n = new Set(s); n.delete(id); return n; });
    }
  }

  async function handleDelete(id: string) {
    if (!confirm('Remove this monitor and all its history?')) return;
    setDeletingIds(s => new Set(s).add(id));
    // Optimistic removal
    setMonitors(prev => prev.filter(m => m.id !== id));
    try {
      await api.monitors.delete(id);
    } catch {
      // Restore on failure
      await fetchMonitors();
    } finally {
      setDeletingIds(s => { const n = new Set(s); n.delete(id); return n; });
    }
  }

  async function handleToggle(id: string, currentlyActive: boolean) {
    try {
      const updated = await api.monitors.toggle(id, !currentlyActive);
      setMonitors(prev => prev.map(m => m.id === id ? { ...m, ...updated } : m));
    } catch {
      await fetchMonitors();
    }
  }

  // Stats for the header
  const upCount = monitors.filter(m => m.current_state === 'up').length;
  const downCount = monitors.filter(m => m.current_state === 'down').length;

  return (
    <div data-stats={JSON.stringify({ upCount, downCount, total: monitors.length })}>
      <div className="card animate-in">
        <div className="table-header">
          <span className="table-title">Monitors ({monitors.length})</span>
          <div className="poll-indicator">
            <div className="poll-dot" />
            Live · refreshes every 8s
          </div>
        </div>

        {monitors.length === 0 ? (
          <div className="empty-state">
            <div className="empty-icon">📡</div>
            <p className="empty-title">No monitors yet</p>
            <p className="empty-desc">
              Add a URL above to start tracking its uptime.
            </p>
          </div>
        ) : (
          <div className="monitor-table-wrap">
            <table className="monitor-table" role="table" aria-label="Monitor list">
              <thead>
                <tr>
                  <th scope="col">Status</th>
                  <th scope="col">Monitor</th>
                  <th scope="col">Response</th>
                  <th scope="col">Last Check</th>
                  <th scope="col">Error</th>
                  <th scope="col">Actions</th>
                </tr>
              </thead>
              <tbody>
                {monitors.map(monitor => {
                  const check = monitor.latest_check;
                  const isChecking = checkingIds.has(monitor.id);
                  const isDeleting = deletingIds.has(monitor.id);

                  return (
                    <tr
                      key={monitor.id}
                      style={{ opacity: isDeleting ? 0.4 : 1, transition: 'opacity 0.2s' }}
                    >
                      <td>
                        <StatusBadge state={monitor.current_state} />
                      </td>

                      <td>
                        <div className="url-cell">
                          {monitor.label && (
                            <span className="url-label">{monitor.label}</span>
                          )}
                          <Link
                            href={`/monitors/${monitor.id}`}
                            className="url-href"
                            title={monitor.url}
                          >
                            {monitor.url}
                          </Link>
                        </div>
                      </td>

                      <td>
                        {check?.response_time_ms != null ? (
                          <span className={`response-time ${getResponseTimeClass(check.response_time_ms)}`}>
                            {check.response_time_ms.toFixed(0)} ms
                          </span>
                        ) : (
                          <span className="response-time none">—</span>
                        )}
                      </td>

                      <td>
                        <span className="last-checked">
                          {check?.checked_at
                            ? formatRelativeTime(check.checked_at)
                            : '—'}
                        </span>
                      </td>

                      <td>
                        {check?.error ? (
                          <span className="error-tag">{check.error}</span>
                        ) : (
                          <span className="text-muted" style={{ color: 'var(--text-muted)', fontSize: '12px' }}>—</span>
                        )}
                      </td>

                      <td>
                        <div className="actions-cell">
                          <button
                            id={`check-now-${monitor.id}`}
                            className={`btn btn-ghost btn-sm${isChecking ? ' btn-loading' : ''}`}
                            onClick={() => handleCheckNow(monitor.id)}
                            disabled={isChecking}
                            title="Run an immediate check"
                            aria-label={`Check ${monitor.url} now`}
                          >
                            {isChecking ? '' : '⚡ Check'}
                          </button>

                          <button
                            id={`toggle-${monitor.id}`}
                            className="btn btn-ghost btn-sm btn-icon"
                            onClick={() => handleToggle(monitor.id, monitor.is_active)}
                            title={monitor.is_active ? 'Pause monitoring' : 'Resume monitoring'}
                            aria-label={monitor.is_active ? 'Pause' : 'Resume'}
                          >
                            {monitor.is_active ? '⏸' : '▶'}
                          </button>

                          <button
                            id={`delete-${monitor.id}`}
                            className="btn btn-danger btn-sm btn-icon"
                            onClick={() => handleDelete(monitor.id)}
                            disabled={isDeleting}
                            title="Delete monitor"
                            aria-label={`Delete monitor for ${monitor.url}`}
                          >
                            ✕
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
