export type MissionStatus = "working" | "awaiting_input" | "idle";

export interface MissionSession {
  id: string;
  title: string | null;
  source: string | null;
  profile: string;
  cwd: string | null;
  status: MissionStatus;
  idle_since?: number | null;
  last_active?: number | null;
  started_at?: number | null;
}

export interface MissionGroup {
  cwd: string;
  sessions: MissionSession[];
}

const STATUS_ORDER: Record<MissionStatus, number> = { working: 0, awaiting_input: 1, idle: 2 };

const recency = (s: MissionSession) => s.idle_since ?? s.last_active ?? 0;

/** Working first, then most recently idle. */
export function compareMissionSessions(a: MissionSession, b: MissionSession): number {
  return STATUS_ORDER[a.status] - STATUS_ORDER[b.status] || recency(b) - recency(a);
}

/** Group by cwd; groups ordered by their top session using the same rule. */
export function groupMissionSessions(sessions: MissionSession[]): MissionGroup[] {
  const map = new Map<string, MissionSession[]>();
  for (const s of sessions) {
    const key = s.cwd || "(no working directory)";
    const bucket = map.get(key) ?? [];
    bucket.push(s);
    map.set(key, bucket);
  }
  return Array.from(map, ([cwd, rows]) => ({ cwd, sessions: [...rows].sort(compareMissionSessions) }))
    .sort((a, b) => compareMissionSessions(a.sessions[0], b.sessions[0]));
}
