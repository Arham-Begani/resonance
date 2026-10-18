// One polite live region for status announcements, plus an assertive region reserved for
// blocking errors. Never stream tokens or waveform samples through these.
import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from "react";

interface Announcer {
  announce: (message: string) => void;
  announceError: (message: string) => void;
}

const AnnouncerContext = createContext<Announcer>({ announce: () => {}, announceError: () => {} });

export function AnnouncerProvider({ children }: { children: ReactNode }) {
  const [polite, setPolite] = useState("");
  const [assertive, setAssertive] = useState("");
  const timers = useRef<{ polite?: number; assertive?: number }>({});

  const push = useCallback((kind: "polite" | "assertive", message: string) => {
    const set = kind === "polite" ? setPolite : setAssertive;
    window.clearTimeout(timers.current[kind]);
    // Clear first so repeating the same message is announced again.
    set("");
    timers.current[kind] = window.setTimeout(() => set(message), 50);
  }, []);

  const value = useMemo<Announcer>(
    () => ({
      announce: (message) => push("polite", message),
      announceError: (message) => push("assertive", message),
    }),
    [push],
  );

  return (
    <AnnouncerContext.Provider value={value}>
      {children}
      <div className="visually-hidden" role="status" aria-live="polite" aria-atomic="true" data-testid="live-polite">
        {polite}
      </div>
      <div className="visually-hidden" role="alert" aria-live="assertive" aria-atomic="true" data-testid="live-assertive">
        {assertive}
      </div>
    </AnnouncerContext.Provider>
  );
}

export function useAnnouncer(): Announcer {
  return useContext(AnnouncerContext);
}
