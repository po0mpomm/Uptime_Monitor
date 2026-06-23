'use client';

import { useState, useRef } from 'react';
import { api, type CreateMonitorPayload } from '../lib/api';

export function AddUrlForm() {
  const [url, setUrl] = useState('');
  const [label, setLabel] = useState('');
  const [intervalSecs, setIntervalSecs] = useState(60);
  const [timeoutMs, setTimeoutMs] = useState(5000);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const urlRef = useRef<HTMLInputElement>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!url.trim()) return;

    setLoading(true);
    setError(null);

    const payload: CreateMonitorPayload = {
      url: url.trim(),
      label: label.trim() || undefined,
      interval_seconds: intervalSecs,
      timeout_ms: timeoutMs,
    };

    try {
      await api.monitors.create(payload);
      setUrl('');
      setLabel('');
      setIntervalSecs(60);
      setTimeoutMs(5000);
      setShowAdvanced(false);
      urlRef.current?.focus();
    } catch (err: unknown) {
      const apiErr = err as { message?: string; status?: number };
      if (apiErr.status === 409) {
        setError('This URL is already being monitored.');
      } else if (apiErr.status === 400) {
        setError(apiErr.message || 'Invalid URL — check the address and try again.');
      } else if (apiErr.status === 429) {
        setError('Too many requests — slow down and try again shortly.');
      } else {
        setError(apiErr.message || 'Failed to add monitor. Please try again.');
      }
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="card add-form-card animate-in">
      <p className="add-form-header">Add Monitor</p>

      <form onSubmit={handleSubmit} noValidate>
        <div className="add-form-grid">
          <div className="form-field">
            <label className="form-label" htmlFor="url-input">URL</label>
            <input
              id="url-input"
              ref={urlRef}
              type="url"
              className="form-input"
              placeholder="https://example.com"
              value={url}
              onChange={e => setUrl(e.target.value)}
              required
              autoComplete="url"
              spellCheck={false}
            />
          </div>

          <div className="form-field">
            <label className="form-label" htmlFor="label-input">Label (optional)</label>
            <input
              id="label-input"
              type="text"
              className="form-input"
              placeholder="My site"
              value={label}
              maxLength={255}
              onChange={e => setLabel(e.target.value)}
            />
          </div>

          <div className="form-field">
            <label className="form-label">&nbsp;</label>
            <button
              id="add-monitor-btn"
              type="submit"
              className={`btn btn-primary${loading ? ' btn-loading' : ''}`}
              disabled={loading || !url.trim()}
              aria-busy={loading}
            >
              {loading ? '' : '+ Add'}
            </button>
          </div>
        </div>

        <button
          type="button"
          className="form-toggle-advanced"
          onClick={() => setShowAdvanced(v => !v)}
        >
          {showAdvanced ? '▲ Hide advanced options' : '▼ Advanced options'}
        </button>

        {showAdvanced && (
          <div className="add-form-advanced">
            <div className="form-field">
              <label className="form-label" htmlFor="interval-input">
                Check interval (seconds, 30–3600)
              </label>
              <input
                id="interval-input"
                type="number"
                className="form-input"
                min={30}
                max={3600}
                value={intervalSecs}
                onChange={e => setIntervalSecs(Number(e.target.value))}
              />
            </div>
            <div className="form-field">
              <label className="form-label" htmlFor="timeout-input">
                Timeout (ms, 1000–30000)
              </label>
              <input
                id="timeout-input"
                type="number"
                className="form-input"
                min={1000}
                max={30000}
                step={500}
                value={timeoutMs}
                onChange={e => setTimeoutMs(Number(e.target.value))}
              />
            </div>
          </div>
        )}

        {error && (
          <div className="form-error" role="alert">
            ⚠ {error}
          </div>
        )}
      </form>
    </div>
  );
}
