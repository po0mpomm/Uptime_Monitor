/**
 * Monitor history detail page — Server Component.
 * Shows the last 50 checks for a specific monitor.
 */
import type { Metadata } from 'next';
import Link from 'next/link';
import { notFound } from 'next/navigation';
import { api } from '../../../lib/api';
import { StatusBadge } from '../../../components/StatusBadge';

interface Props {
  params: Promise<{ id: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { id } = await params;
  return {
    title: `Monitor History — ${id.slice(0, 8)}… | Uptime Monitor`,
  };
}

function formatDate(dateStr: string): string {
  return new Date(dateStr).toLocaleString('en-US', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

function getResponseTimeClass(ms: number | null): string {
  if (ms === null) return 'none';
  if (ms < 200) return 'fast';
  if (ms < 800) return 'medium';
  return 'slow';
}

export default async function MonitorHistoryPage({ params }: Props) {
  const { id } = await params;

  let monitor = null;
  let history = null;

  try {
    [monitor, history] = await Promise.all([
      // Fetch monitor info via the list endpoint (check for the specific one)
      api.monitors.list().then(list => list.find(m => m.id === id) ?? null),
      api.monitors.history(id, 50),
    ]);
  } catch {
    notFound();
  }

  if (!monitor) notFound();

  const upCount = history?.filter(c => c.is_up).length ?? 0;
  const total = history?.length ?? 0;
  const uptimePct = total > 0 ? ((upCount / total) * 100).toFixed(1) : '—';

  return (
    <main className="page-wrapper">
      <div className="history-header animate-in">
        <Link href="/">← Dashboard</Link>
        <span style={{ color: 'var(--border-default)' }}>›</span>
        <div>
          <h1 className="history-title">
            {monitor.label || 'Monitor'}&nbsp;
            <StatusBadge state={monitor.current_state} />
          </h1>
          <p className="history-url">{monitor.url}</p>
        </div>
      </div>

      {/* Summary stats */}
      <div className="card animate-in" style={{ marginBottom: '20px' }}>
        <div style={{ display: 'flex', gap: '32px', flexWrap: 'wrap' }}>
          <div className="stat-item">
            <div className="stat-value total">{total}</div>
            <div className="stat-label">Checks shown</div>
          </div>
          <div className="stat-item">
            <div className="stat-value up">{uptimePct}%</div>
            <div className="stat-label">Uptime</div>
          </div>
          <div className="stat-item">
            <div className="stat-value total">{monitor.interval_seconds}s</div>
            <div className="stat-label">Check interval</div>
          </div>
          <div className="stat-item">
            <div className="stat-value total">{monitor.timeout_ms}ms</div>
            <div className="stat-label">Timeout</div>
          </div>
        </div>
      </div>

      {/* History table */}
      <div className="card animate-in">
        <p className="table-title" style={{ marginBottom: '16px' }}>
          Check History (last {total})
        </p>
        {!history || history.length === 0 ? (
          <div className="empty-state">
            <div className="empty-icon">🔍</div>
            <p className="empty-title">No checks yet</p>
            <p className="empty-desc">The worker will check this monitor soon.</p>
          </div>
        ) : (
          <div className="monitor-table-wrap">
            <table className="monitor-table history-table" aria-label="Check history">
              <thead>
                <tr>
                  <th scope="col">Result</th>
                  <th scope="col">Time</th>
                  <th scope="col">Status Code</th>
                  <th scope="col">Response Time</th>
                  <th scope="col">Error</th>
                </tr>
              </thead>
              <tbody>
                {history.map(check => (
                  <tr key={check.id}>
                    <td>
                      <span
                        className={`status-badge ${check.is_up ? 'up' : 'down'}`}
                        style={{ fontSize: '10px' }}
                      >
                        <span className="status-dot" />
                        {check.is_up ? 'UP' : 'DOWN'}
                      </span>
                    </td>
                    <td>
                      <span className="last-checked" style={{ fontSize: '12px' }}>
                        {formatDate(check.checked_at)}
                      </span>
                    </td>
                    <td>
                      <span
                        style={{
                          fontFamily: 'JetBrains Mono, monospace',
                          fontSize: '13px',
                          color: check.status_code
                            ? check.status_code < 400
                              ? 'var(--up-text)'
                              : 'var(--down-text)'
                            : 'var(--text-muted)',
                        }}
                      >
                        {check.status_code ?? '—'}
                      </span>
                    </td>
                    <td>
                      <span className={`response-time ${getResponseTimeClass(check.response_time_ms)}`}>
                        {check.response_time_ms != null
                          ? `${check.response_time_ms.toFixed(0)} ms`
                          : '—'}
                      </span>
                    </td>
                    <td>
                      {check.error ? (
                        <span className="error-tag">{check.error}</span>
                      ) : (
                        <span style={{ color: 'var(--text-muted)', fontSize: '12px' }}>—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </main>
  );
}
