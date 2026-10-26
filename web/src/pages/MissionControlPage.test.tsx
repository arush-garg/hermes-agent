import { describe, expect, it } from "vitest";
import { groupMissionSessions, type MissionSession } from "@/lib/mission-control";

const row = (id: string, cwd: string, status: MissionSession["status"], idle?: number): MissionSession => ({
  id, cwd, status, title: id, source: "cli", profile: "default", idle_since: idle ?? null, last_active: idle ?? 0,
});

describe("groupMissionSessions", () => {
  it("groups by cwd and orders working first, then most recently idle", () => {
    const groups = groupMissionSessions([
      row("old", "/a", "awaiting_input", 100),
      row("recent", "/b", "awaiting_input", 300),
      row("busy", "/a", "working"),
      row("newer", "/a", "idle", 200),
    ]);
    expect(groups.map((g) => g.cwd)).toEqual(["/a", "/b"]);
    expect(groups[0].sessions.map((s) => s.id)).toEqual(["busy", "old", "newer"]);
  });

  it("orders idle-only groups by most recent idle time", () => {
    const groups = groupMissionSessions([row("x", "/x", "awaiting_input", 10), row("y", "/y", "awaiting_input", 50)]);
    expect(groups.map((g) => g.cwd)).toEqual(["/y", "/x"]);
  });

  it("buckets sessions without a cwd", () => {
    expect(groupMissionSessions([row("n", "", "idle", 1)])[0].cwd).toBe("(no working directory)");
  });
});
