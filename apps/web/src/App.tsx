// Resonance workbench: providers wrap one shell (Explore, Learn, and Investigate share it).
import { PlaybackProvider } from "./components/audio/PlaybackProvider";
import { AgentRunsProvider } from "./features/agent/agentRuns";
import { CaseProvider } from "./features/investigate/caseStore";
import { LearnProvider } from "./features/learn/learnStore";
import { Shell } from "./layout/Shell";
import { AnnouncerProvider } from "./state/announcer";
import { WorkbenchProvider } from "./state/workbench";

export function App() {
  return (
    <AnnouncerProvider>
      <WorkbenchProvider>
        <AgentRunsProvider>
          <CaseProvider>
            <LearnProvider>
              <PlaybackProvider>
                <Shell />
              </PlaybackProvider>
            </LearnProvider>
          </CaseProvider>
        </AgentRunsProvider>
      </WorkbenchProvider>
    </AnnouncerProvider>
  );
}
