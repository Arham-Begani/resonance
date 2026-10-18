// Mode navigation for the single workbench shell (Explore, Investigate).
import { Icon, type IconName } from "../components/ui/Icon";
import type { Mode } from "../state/workbench";

const MODES: { id: Mode; label: string; icon: IconName }[] = [
  { id: "explore", label: "Explore", icon: "explore" },
  { id: "investigate", label: "Investigate", icon: "investigate" },
];

export function ModeRail({ mode, onChange, caseOpen }: { mode: Mode; onChange: (mode: Mode) => void; caseOpen: boolean }) {
  return (
    <nav className="rail" aria-label="Workbench mode">
      <p className="rail__brand" aria-hidden="true">
        <span className="rail__mark" />
        Resonance
      </p>
      <ul className="rail__list">
        {MODES.map((m) => (
          <li key={m.id}>
            <button
              type="button"
              className={`rail__item${mode === m.id ? " is-active" : ""}`}
              aria-current={mode === m.id ? "page" : undefined}
              onClick={() => onChange(m.id)}
            >
              <Icon name={m.icon} size={20} />
              <span className="rail__label">{m.label}</span>
              {m.id === "investigate" && caseOpen ? (
                <span className="rail__dot" aria-label="(mystery open)" role="img" />
              ) : null}
            </button>
          </li>
        ))}
      </ul>
    </nav>
  );
}
