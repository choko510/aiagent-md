import type { CapabilityItem } from "../api/client";

type Props = {
  capabilities: CapabilityItem[];
};

export default function CapabilitiesBanner({ capabilities }: Props) {
  if (capabilities.length === 0) return null;
  return (
    <div className="panel" data-testid="capabilities-panel">
      <h2>System Capabilities（誠実ステータス）</h2>
      <ul className="compact-list">
        {capabilities.map((cap) => (
          <li key={cap.component}>
            <strong>{cap.component}</strong>: {cap.integrationState} · externalActions=
            {String(cap.externalActionsEnabled)} · hostVerified={String(cap.hostVerified)} · prodReady=
            {String(cap.productionReady)}
          </li>
        ))}
      </ul>
    </div>
  );
}
