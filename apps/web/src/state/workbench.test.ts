import { createInitialState, readableIds, undoTargetId, type WorkbenchData } from "./workbench";
import { makeHighpassRevision, makeRevision } from "../test/fixtures";

function withRevisions(revisions: WorkbenchData["revisions"], switchOrigins: Record<string, string> = {}): WorkbenchData {
  return { ...createInitialState(), revisions, switchOrigins };
}

describe("undoTargetId (undo works across circuit families)", () => {
  const lp1 = makeRevision();
  const lp2 = makeRevision({ id: "rev_lp2", parent_id: lp1.id, origin: "user_edit" });
  const hp = makeHighpassRevision({ id: "rev_hp1" });

  it("goes to the parent within a family and nowhere from the original circuit", () => {
    const s = withRevisions([lp1, lp2]);
    expect(undoTargetId(s, lp2.id)).toBe(lp1.id);
    expect(undoTargetId(s, lp1.id)).toBeNull();
  });

  it("returns from a family switch to the revision it was switched from", () => {
    // Switched to high-pass while Revision 1 (not the latest) was shown.
    const s = withRevisions([lp1, lp2, hp], { [hp.id]: lp1.id });
    expect(undoTargetId(s, hp.id)).toBe(lp1.id);
  });

  it("falls back to the revision created just before a switched root when the tab forgot", () => {
    const s = withRevisions([lp1, lp2, hp]);
    expect(undoTargetId(s, hp.id)).toBe(lp2.id);
  });

  it("ignores case designs, which are never undo targets", () => {
    const frozen = makeRevision({ id: "rev_case", origin: "case_intended", frozen: true });
    const s = withRevisions([lp1, frozen, hp]);
    expect(undoTargetId(s, hp.id)).toBe(lp1.id);
  });
});

describe("readableIds (no raw result IDs in learner copy)", () => {
  it("replaces cited experiment and revision IDs with the workbench numbering", () => {
    const s: WorkbenchData = {
      ...withRevisions([makeRevision(), makeRevision({ id: "rev_m7CKjMpgg", parent_id: "rev_fixture000001" })]),
      experiments: [
        { id: "exp_282fw4Hrww", revision_id: "rev_fixture000001", status: "succeeded", created_at: "2026-10-15T12:00:00Z", goal_passed: null },
        { id: "exp_m7CKjMpggY", revision_id: "rev_m7CKjMpgg", status: "succeeded", created_at: "2026-10-15T12:01:00Z", goal_passed: null },
        // Real IDs are 16 URL-safe characters and may contain "-" and "_".
        { id: "exp_-xngpCDk2nCJ__8q", revision_id: "rev_m7CKjMpgg", status: "succeeded", created_at: "2026-10-15T12:02:00Z", goal_passed: null },
      ],
    };
    expect(readableIds(s, "moved (exp_282fw4Hrww → exp_m7CKjMpggY) on rev_m7CKjMpgg; see exp_unknown1")).toBe(
      "moved (Experiment 1 → Experiment 2) on Revision 2; see an experiment",
    );
    expect(readableIds(s, "(exp_-xngpCDk2nCJ__8q → exp_m7CKjMpggY, interpolated)")).toBe("(Experiment 3 → Experiment 2, interpolated)");
  });
});
