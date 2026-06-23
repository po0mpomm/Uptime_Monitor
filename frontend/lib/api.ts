/**
 * Typed API client for the Uptime Monitor backend.
 * Uses relative /api/* paths in the browser (proxied via next.config rewrites).
 * Server Components can also use relative paths since rewrites apply server-side too.
 */

export interface HealthCheckResponse {
  id: number;
  url_id: string;
  checked_at: string;
  status_code: number | null;
  response_time_ms: number | null;
  is_up: boolean;
  error: string | null;
}

export interface Monitor {
  id: string;
  url: string;
  label: string | null;
  interval_seconds: number;
  timeout_ms: number;
  is_active: boolean;
  current_state: 'unknown' | 'up' | 'down';
  created_at: string;
  latest_check: HealthCheckResponse | null;
}

export interface CreateMonitorPayload {
  url: string;
  label?: string;
  interval_seconds?: number;
  timeout_ms?: number;
}

const API_BASE = '/api';

async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    const error = new Error(body.detail || 'API error') as Error & { status: number };
    error.status = res.status;
    throw error;
  }
  // 204 No Content
  if (res.status === 204) return undefined as T;
  return res.json();
}

export const api = {
  monitors: {
    list: () => apiFetch<Monitor[]>('/monitors'),
    create: (payload: CreateMonitorPayload) =>
      apiFetch<Monitor>('/monitors', { method: 'POST', body: JSON.stringify(payload) }),
    delete: (id: string) => apiFetch<void>(`/monitors/${id}`, { method: 'DELETE' }),
    toggle: (id: string, is_active: boolean) =>
      apiFetch<Monitor>(`/monitors/${id}`, {
        method: 'PATCH',
        body: JSON.stringify({ is_active }),
      }),
    check: (id: string) =>
      apiFetch<HealthCheckResponse>(`/monitors/${id}/check`, { method: 'POST' }),
    history: (id: string, limit = 50) =>
      apiFetch<HealthCheckResponse[]>(`/monitors/${id}/history?limit=${limit}`),
  },
};
