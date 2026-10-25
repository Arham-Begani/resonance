// Landing page: an entry point to the workbench, never a substitute for it. Every link leads to
// the real lab (/lab, /lab#learn, /lab#investigate) or the public repository. Copy states only
// what the product does; it carries no metrics, testimonials or unmeasured claims.
import type { ReactNode } from "react";
import { Icon, type IconName } from "../../components/ui/Icon";
import { HeroFigure, InsightFigure } from "./Figures";
import "./landing.css";

export const REPO_URL = "https://github.com/Arham-Begani/resonance";
const LICENSE_URL = `${REPO_URL}/blob/main/LICENSE`;

function Arrow() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
      <path d="M5 12h14M13 6l6 6-6 6" />
    </svg>
  );
}

export function Wordmark() {
  return (
    <>
      <span className="l-mark" aria-hidden="true" />
      <span className="l-wordmark__name">Resonance</span>
    </>
  );
}

function Nav() {
  return (
    <header className="l-nav">
      <div className="l-container l-nav__inner">
        <a className="l-wordmark" href="/" aria-label="Resonance home">
          <Wordmark />
        </a>
        <nav className="l-nav__links" aria-label="Page">
          <ul>
            <li>
              <a href="#modes">Modes</a>
            </li>
            <li>
              <a href="#physics">Physics</a>
            </li>
            <li>
              <a href="#honesty">Honesty</a>
            </li>
            <li>
              <a href={REPO_URL}>GitHub</a>
            </li>
          </ul>
        </nav>
        <a className="btn btn--primary l-nav__cta" href="/lab">
          Open the lab
        </a>
      </div>
    </header>
  );
}

function Hero() {
  return (
    <section className="l-hero" aria-labelledby="hero-title">
      <div className="l-container">
        <p className="eyebrow l-hero__eyebrow">Browser electronics lab</p>
        <h1 className="l-hero__title" id="hero-title">
          <span className="l-hero__line">Hear what a circuit does.</span>{" "}
          <span className="l-hero__line l-hero__line--second">Discover why it fails.</span>
        </h1>
        <div className="l-hero__grid">
          <div className="l-hero__copy">
            <p className="l-lead">
              Change an RC filter, simulate it with ngspice, and hear its input and output at one honest listening
              level. Then track down a hidden fault with an NVIDIA Nemotron lab partner that has to measure, just like
              you.
            </p>
            <div className="l-actions">
              <a className="btn btn--primary l-btn" href="/lab">
                Open the lab
                <Arrow />
              </a>
              <a className="btn l-btn" href="/lab#investigate">
                Solve a mystery
              </a>
            </div>
            <ul className="l-facts" aria-label="Requirements">
              <li>No install or hardware</li>
              <li>Fully keyboard operable</li>
              <li>Completable with sound off</li>
            </ul>
          </div>
          <HeroFigure />
        </div>
      </div>
    </section>
  );
}

interface ModeCardProps {
  id: string;
  icon: IconName;
  mode: string;
  href: string;
  title: string;
  children: ReactNode;
  featured?: boolean;
}

function ModeCard({ id, icon, mode, href, title, children, featured = false }: ModeCardProps) {
  return (
    <article className={`l-card${featured ? " l-card--featured" : ""}`} aria-labelledby={`${id}-title`}>
      <p className="l-card__mode">
        <Icon name={icon} size={16} />
        {mode}
      </p>
      <h3 className="l-card__title" id={`${id}-title`}>
        <a className="l-card__link" href={href}>
          {title}
        </a>
      </h3>
      {children}
      <p className="l-card__go" aria-hidden="true">
        Open {mode} <Arrow />
      </p>
    </article>
  );
}

function Modes() {
  return (
    <section className="l-section" id="modes" aria-labelledby="modes-title">
      <div className="l-container">
        <div className="l-section__head">
          <p className="eyebrow">One workbench, three modes</p>
          <h2 className="l-section__title" id="modes-title">
            Explore, learn, then investigate.
          </h2>
          <p className="l-lead">
            Every mode shares the same schematic, component list, instruments and experiment history. Only the
            question changes.
          </p>
        </div>
        <div className="l-modes">
          <ModeCard id="mode-explore" icon="explore" mode="Explore" href="/lab" title="Change a value. Hear the difference.">
            <p className="l-card__text">
              Edit R1 and C1 in real units, run an AC sweep and a two-tone transient, and play input against output.
              Check a goal such as “keep 440 Hz within 1 dB, cut 4 kHz by 12 dB”, compare revisions, undo.
            </p>
          </ModeCard>
          <ModeCard id="mode-learn" icon="learn" mode="Learn" href="/lab#learn" title="Predict first, then measure.">
            <p className="l-card__text">
              Say whether a component change will raise, lower or leave a tone’s level, then run it. You are graded on
              the measured change, with hints when you want them.
            </p>
          </ModeCard>
          <ModeCard
            id="mode-investigate"
            icon="investigate"
            mode="Investigate"
            href="/lab#investigate"
            title="Find a hidden fault in three experiments."
            featured
          >
            <p className="l-card__text">
              The intended design stays visible; the simulated circuit may not match it. R1 or C1 may have drifted to
              half or double, the capacitor may be disconnected, or nothing may be wrong. Work it out yourself with
              Guide me, or let the AI lab partner run the measurements with Investigate for me.
            </p>
            <ol className="l-steps">
              <li>Hear and see the symptom</li>
              <li>Run up to three bounded experiments</li>
              <li>Diagnose from evidence, or say it is not enough</li>
              <li>Verify the repair numerically</li>
              <li>Close the case to reveal the fault</li>
            </ol>
          </ModeCard>
        </div>
      </div>
    </section>
  );
}

function Physics() {
  return (
    <section className="l-section" id="physics" aria-labelledby="physics-title">
      <div className="l-container l-physics">
        <div className="l-physics__copy">
          <p className="eyebrow">The physics</p>
          <h2 className="l-section__title" id="physics-title">
            Why a doubled resistor and a doubled capacitor sound the same.
          </h2>
          <p className="l-formula">
            <span className="visually-hidden">The low-pass transfer function: </span>
            H(jω) = 1 / (1 + jωRC)
          </p>
          <p className="l-body">
            The output depends only on the product RC. Double either part and the cutoff lands in exactly the same
            place, so the output sounds identical. Measuring it at more frequencies cannot tell the two apart.
          </p>
          <p className="l-body">
            The source current can. At every frequency the doubled capacitor draws twice the current of the doubled
            resistor.
          </p>
          <p className="l-note">
            In Investigate, the AI never sees which part changed. It has to choose the current measurement within its
            three experiments, or report that the evidence is ambiguous.
          </p>
        </div>
        <InsightFigure />
      </div>
    </section>
  );
}

const PRINCIPLES: { term: string; detail: string }[] = [
  {
    term: "A real simulator",
    detail:
      "Responses come from ngspice runs of a netlist compiled from the circuit on screen. Textbook equations appear only as cross-checks and labeled predictions.",
  },
  {
    term: "The AI cannot see the answer",
    detail:
      "Nemotron receives the intended schematic and the observations it asked for. Hidden values, seeds and the actual netlist stay with the evaluator until the case closes.",
  },
  {
    term: "Ambiguity is an allowed answer",
    detail:
      "When two causes still fit, the diagnosis can be “insufficient evidence”. Missing data never rules a cause out, and no likelihood percentages are invented.",
  },
  {
    term: "One listening level",
    detail:
      "Input and output play at a shared, fixed gain. Clips are never normalized, so the attenuation you hear is the attenuation that was measured.",
  },
  {
    term: "Repairs are verified by numbers",
    detail:
      "A repair is checked across a frequency grid against the intended circuit. Identifying the fault and restoring the behavior are reported separately.",
  },
  {
    term: "Readable without sound",
    detail:
      "Charts come with tables and summaries, every control works from the keyboard, and each task can be finished muted.",
  },
];

function Honesty() {
  return (
    <section className="l-section" id="honesty" aria-labelledby="honesty-title">
      <div className="l-container">
        <div className="l-section__head">
          <p className="eyebrow">Honest by design</p>
          <h2 className="l-section__title" id="honesty-title">
            Every number has a source.
          </h2>
        </div>
        <dl className="l-principles">
          {PRINCIPLES.map((p) => (
            <div className="l-principle" key={p.term}>
              <dt>{p.term}</dt>
              <dd>{p.detail}</dd>
            </div>
          ))}
        </dl>
      </div>
    </section>
  );
}

const STACK: { name: string; role: string; detail: string }[] = [
  { name: "NVIDIA Nemotron", role: "AI lab partner: picks experiments, explains evidence", detail: "nvidia/nemotron-3-super-120b-a12b" },
  { name: "Nebius Token Factory", role: "Serves Nemotron through an OpenAI-compatible API", detail: "server-side key only" },
  { name: "ngspice", role: "AC and transient circuit simulation", detail: "isolated subprocess per job" },
  { name: "React and FastAPI", role: "Workbench interface and typed API", detail: "TypeScript · Python · Pydantic" },
];

function BuiltWith() {
  return (
    <section className="l-section" aria-labelledby="stack-title">
      <div className="l-container">
        <div className="l-section__head">
          <p className="eyebrow">Built with</p>
          <h2 className="l-section__title" id="stack-title">
            What runs underneath.
          </h2>
        </div>
        <ul className="l-stack">
          {STACK.map((s) => (
            <li key={s.name}>
              <p className="l-stack__name">{s.name}</p>
              <p className="l-stack__role">{s.role}</p>
              <p className="l-stack__detail mono">{s.detail}</p>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}

function FinalCta() {
  return (
    <section className="l-section l-section--cta" aria-labelledby="cta-title">
      <div className="l-container">
        <div className="l-cta">
          <div>
            <h2 className="l-cta__title" id="cta-title">
              The lab opens on a working circuit.
            </h2>
            <p className="l-body">A low-pass filter, already simulated. Nothing plays until you press Play.</p>
          </div>
          <div className="l-actions">
            <a className="btn btn--primary l-btn" href="/lab">
              Open the lab
              <Arrow />
            </a>
            <a className="btn l-btn" href="/lab#investigate">
              Solve a mystery
            </a>
          </div>
        </div>
      </div>
    </section>
  );
}

export function Footer() {
  return (
    <footer className="l-footer">
      <div className="l-container l-footer__inner">
        <p className="l-footer__brand">
          <Wordmark />
        </p>
        <p className="l-footer__note">Simulated circuits, not physical measurements.</p>
        <ul className="l-footer__links">
          <li>
            <a href={REPO_URL}>GitHub</a>
          </li>
          <li>
            <a href={LICENSE_URL}>MIT License</a>
          </li>
        </ul>
      </div>
    </footer>
  );
}

export function SkipLink() {
  return (
    <a className="l-skip" href="#main">
      Skip to content
    </a>
  );
}

export function Landing() {
  return (
    <div className="landing">
      <SkipLink />
      <Nav />
      <main id="main" tabIndex={-1}>
        <Hero />
        <Modes />
        <Physics />
        <Honesty />
        <BuiltWith />
        <FinalCta />
      </main>
      <Footer />
    </div>
  );
}
