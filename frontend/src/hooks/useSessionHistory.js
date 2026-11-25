import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";

// The saved calls for the sidebar. Reloads when a call is saved: its summary is written (summary) and the
// ticket is raised (ticket), which are the two moments the backend stores the session.
export function useSessionHistory(call) {
  const [sessions, setSessions] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const reload = useCallback(async () => {
    try {
      const body = await api.listSessions();
      setSessions(body.sessions);
      setError("");
    } catch (e) {
      if (e.status !== 401) setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    reload();
  }, [reload, call.callSid, call.summary, call.ticket]);

  return { sessions, loading, error, reload };
}
