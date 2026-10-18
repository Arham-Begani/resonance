// Header: circuit title, revision ordinal (never raw IDs), current goal, AI and simulator status.
import type { AiStatus, Revision, SimulatorStatus } from "../api/types";
import { Badge } from "../components/ui/primitives";
import { Icon } from "../components/ui/Icon";

export function AiBadge({ ai, error }: { ai: AiStatus | null | undefined; error?: boolean }) {
  if (error) return <Badge tone="warning">Status unknown</Badge>;
  if (!ai) return <Badge icon={null}>Checking AI…</Badge>;
  if (ai.state === "dev_fixture") return <Badge tone="warning">Dev fixture · not AI</Badge>;
  if (ai.available) {
    return (
      <Badge tone="accent" icon="ai">
        AI ready{ai.model ? ` · ${ai.model}` : ""}
      </Badge>
    );
  }
  return <Badge tone="warning">AI unavailable</Badge>;
}

export function SimulatorBadge({ sim }: { sim: SimulatorStatus | null | undefined }) {
  if (!sim) return <Badge icon={null}>Checking simulator…</Badge>;
  if (sim.available)
    return (
      <Badge icon="cpu">
        ngspice{sim.version ? ` ${sim.version}` : ""}
      </Badge>
    );
  return <Badge tone="danger">Simulator unavailable</Badge>;
}

interface HeaderStripProps {
  revision: Revision | null;
  revisionText: string | null;
  goalTitle: string | null;
  ai: AiStatus | null | undefined;
  sim: SimulatorStatus | null | undefined;
  healthError: boolean;
}

export function HeaderStrip({ revision, revisionText, goalTitle, ai, sim, healthError }: HeaderStripProps) {
  return (
    <header className="header">
      <div className="header__title">
        <h1 className="header__h1">{revision?.title ?? "Resonance"}</h1>
        {revisionText ? <span className="header__rev">{revisionText}</span> : null}
      </div>
      {goalTitle ? (
        <p className="header__goal" title={goalTitle}>
          <Icon name="flask" size={14} />
          <span className="visually-hidden">Current goal: </span>
          <span className="header__goal-text">{goalTitle}</span>
        </p>
      ) : null}
      <div className="header__status" role="group" aria-label="System status">
        <AiBadge ai={ai} error={healthError} />
        <SimulatorBadge sim={sim} />
      </div>
    </header>
  );
}
