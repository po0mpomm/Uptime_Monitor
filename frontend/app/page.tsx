/**
 * Dashboard — Server Component.
 * Fetches initial monitor list server-side (no loading-spinner flash on first paint).
 * Live updates are handled by the UrlTable client component (polls every 8s).
 */
import { api, type Monitor } from '../lib/api';
import { AddUrlForm } from '../components/AddUrlForm';
import { UrlTable } from '../components/UrlTable';

async function getInitialMonitors(): Promise<Monitor[]> {
  try {
    return await api.monitors.list();
  } catch {
    return [];
  }
}

function DashboardStats({ monitors }: { monitors: Monitor[] }) {
  const up = monitors.filter(m => m.current_state === 'up').length;
  const down = monitors.filter(m => m.current_state === 'down').length;
  const total = monitors.length;

  return (
    <div className="header-stats">
      <div className="stat-item">
        <div className="stat-value up">{up}</div>
        <div className="stat-label">Up</div>
      </div>
      <div className="stat-item">
        <div className="stat-value down">{down}</div>
        <div className="stat-label">Down</div>
      </div>
      <div className="stat-item">
        <div className="stat-value total">{total}</div>
        <div className="stat-label">Total</div>
      </div>
    </div>
  );
}

export default async function DashboardPage() {
  const initial = await getInitialMonitors();

  return (
    <main className="page-wrapper">
      <header className="header">
        <div className="header-icon" role="img" aria-label="Uptime Monitor">📡</div>
        <div>
          <h1 className="header-title">Uptime Monitor</h1>
          <p className="header-subtitle">Live URL health — checks every minute</p>
        </div>
        <DashboardStats monitors={initial} />
      </header>

      {/* AddUrlForm and UrlTable are Client Components — they handle all interactivity */}
      <AddUrlForm />
      <UrlTable initial={initial} />
    </main>
  );
}
