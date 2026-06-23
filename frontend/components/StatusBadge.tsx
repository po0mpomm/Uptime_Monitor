import type { Monitor } from '../lib/api';

interface Props {
  state: Monitor['current_state'];
}

export function StatusBadge({ state }: Props) {
  const label = state === 'up' ? 'UP' : state === 'down' ? 'DOWN' : 'UNKNOWN';

  return (
    <span className={`status-badge ${state}`} role="status" aria-label={`Status: ${label}`}>
      <span className="status-dot" />
      {label}
    </span>
  );
}
