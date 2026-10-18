// Resonance workbench: providers wrap one shell (Explore and Investigate share it).
import { PlaybackProvider } from "./components/audio/PlaybackProvider";
import { AgentRunsProvider } from "./features/agent/agentRuns";
import { CaseProvider } from "./features/investigate/caseStore";
import { Shell } from "./layout/Shell";
import { AnnouncerProvider } from "./state/announcer";
import { WorkbenchProvider } from "./state/workbench";

export function App() {
  return (
    <AnnouncerProvider>
      <WorkbenchProvider>
        <AgentRunsProvider>
          <CaseProvider>
            <PlaybackProvider>
              <Shell />
            </PlaybackProvider>
          </CaseProvider>
        </AgentRunsProvider>
      </WorkbenchProvider>
    </AnnouncerProvider>
  );
}
