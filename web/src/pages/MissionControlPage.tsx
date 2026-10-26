import { useEffect, useMemo, useRef, useState } from "react";
import { Clock, FolderOpen, MessageSquare, Play, Radio } from "lucide-react";
import { fetchJSON } from "@/lib/api";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@nous-research/ui/ui/components/card";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { usePageHeader } from "@/contexts/usePageHeader";
import { errorMessage } from "@/lib/api-error";
import { timeAgo } from "@/lib/utils";
import { groupMissionSessions, type MissionSession } from "@/lib/mission-control";

interface MissionControlResponse {
  sessions: MissionSession[];
}

const STATUS_LABEL: Record<MissionSession["status"], string> = {
  working: "Working",
  awaiting_input: "Awaiting input",
  idle: "Idle",
};

function fmtActivity(s: MissionSession) {
  if (s.status === "working") return "now";
  const ts = s.idle_since ?? s.last_active;
  return ts ? `idle ${timeAgo(ts).replace(" ago", "")}` : "—";
}

function statusColor(s: MissionSession["status"]) {
  switch (s) {
    case "working":
      return "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 border-emerald-500/20";
    case "awaiting_input":
      return "bg-amber-500/10 text-amber-600 dark:text-amber-400 border-amber-500/20";
    case "idle":
      return "bg-zinc-500/10 text-zinc-600 dark:text-zinc-400 border-zinc-500/20";
  }
}

export default function MissionControlPage() {
  const { setTitle } = usePageHeader();
  useEffect(() => {
    setTitle("Mission Control");
  }, [setTitle]);

  const [data, setData] = useState<MissionControlResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const timerRef = useRef<number | null>(null);

  const load = async () => {
    try {
      const res = await fetchJSON<MissionControlResponse>("/api/sessions/mission-control");
      setData(res);
      setErr(null);
    } catch (e) {
      setErr(errorMessage(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    timerRef.current = window.setInterval(load, 5000);
    return () => {
      if (timerRef.current) window.clearInterval(timerRef.current);
    };
  }, []);

  const grouped = useMemo(() => groupMissionSessions(data?.sessions ?? []), [data]);

  return (
    <div className="mx-auto max-w-6xl p-6 space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Mission Control</h1>
        <Badge className="gap-1">
          <Radio className="h-3 w-3" /> Live
        </Badge>
      </div>

      {loading && (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Spinner className="h-4 w-4" /> Loading sessions…
        </div>
      )}
      {err && <div className="text-sm text-destructive">{err}</div>}

      {!loading && !err && grouped.length === 0 && (
        <Card>
          <CardContent className="py-10 text-center text-sm text-muted-foreground">
            No open sessions
          </CardContent>
        </Card>
      )}

      <div className="space-y-6">
        {grouped.map((g) => (
          <Card key={g.cwd}>
            <CardHeader className="flex-row items-center gap-2">
              <FolderOpen className="h-4 w-4 text-muted-foreground" />
              <CardTitle className="text-base">{g.cwd}</CardTitle>
              <span className="ml-auto text-xs text-muted-foreground">{g.sessions.length} session(s)</span>
            </CardHeader>
            <CardContent className="space-y-2">
              {g.sessions.map((s) => (
                <div
                  key={s.id}
                  className="flex items-center gap-3 rounded-md border p-3 text-sm"
                >
                  <MessageSquare className="h-4 w-4 text-muted-foreground" />
                  <div className="min-w-0 flex-1">
                    <div className="truncate font-medium">{s.title ?? s.id}</div>
                    <div className="truncate text-xs text-muted-foreground">
                      {s.profile} • {s.source ?? "unknown"}
                    </div>
                  </div>
                  <Badge className={statusColor(s.status)}>
                    {s.status === "working" && <Play className="mr-1 h-3 w-3" />}
                    {STATUS_LABEL[s.status]}
                  </Badge>
                  <div className="flex shrink-0 items-center gap-1 text-xs text-muted-foreground">
                    <Clock className="h-3 w-3" />
                    {fmtActivity(s)}
                  </div>
                </div>
              ))}
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}
