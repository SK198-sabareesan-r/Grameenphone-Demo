import { useEffect, useState } from "react";
import { api } from "../api/client";
import CallControls from "../components/CallControls";
import SessionSidebar from "../components/SessionSidebar";
import SummaryTicket from "../components/SummaryTicket";
import Transcript from "../components/Transcript";
import CopilotShell from "../components/copilot/CopilotShell";
import { useSessionHistory } from "../hooks/useSessionHistory";
import { callFromView } from "../state/callReducer";
import { formatDateTime, formatDuration } from "../state/callLabels";

const OPEN_KEY = "gp-session-sidebar-open";

function SavedBanner({ call, onBack }) {
  const duration = formatDuration(call.startedAt, call.endedAt);
  return (
    <div className="card saved-banner">
      <div>
        <strong>Saved session</strong>
        <span className="muted small">
          {" "}
          {call.callSid} &middot; {formatDateTime(call.startedAt)}
          {duration && ` \u00B7 ${duration}`}
        </span>
      </div>
      <button type="button" className="btn btn-ghost" onClick={onBack}>
        Back to live call
      </button>
    </div>
  );
}

// The live call, with the saved sessions in a sidebar on the left. Picking a saved session shows that call
// again, read-only, with the same transcript, customer card, suggested answers and summary it had.
export default function LiveCallPage({ call: liveCall }) {
  const [open, setOpen] = useState(() => localStorage.getItem(OPEN_KEY) !== "false");
  const [selectedId, setSelectedId] = useState(null);
  const [saved, setSaved] = useState(null);
  const [loadError, setLoadError] = useState("");
  const history = useSessionHistory(liveCall);

  useEffect(() => {
    localStorage.setItem(OPEN_KEY, String(open));
  }, [open]);

  useEffect(() => {
    if (!selectedId) {
      setSaved(null);
      return undefined;
    }
    let cancelled = false;
    setLoadError("");
    setSaved(null);
    api
      .getSession(selectedId)
      .then(({ call }) => {
        if (!cancelled) setSaved({ ...callFromView(call, { readOnly: true }), endedAt: call.ended_at });
      })
      .catch((e) => {
        if (!cancelled) {
          setSaved(null);
          setLoadError(e.message);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  const viewing = Boolean(selectedId && saved);
  const call = viewing ? saved : liveCall;

  return (
    <div className="live-layout">
      <SessionSidebar
        open={open}
        onToggle={() => setOpen((v) => !v)}
        sessions={history.sessions}
        loading={history.loading}
        error={history.error}
        selectedId={selectedId}
        onSelect={setSelectedId}
        onReload={history.reload}
      />
      <div className="live-grid">
        <section className="col-main">
          {viewing ? <SavedBanner call={call} onBack={() => setSelectedId(null)} /> : <CallControls call={call} />}
          {selectedId && !saved && !loadError && <p className="muted">Loading the saved session&hellip;</p>}
          {loadError && <p className="error">{loadError}</p>}
          <Transcript turns={call.turns} partials={call.partials} />
          <SummaryTicket call={call} />
        </section>
        <aside className="col-side">
          <CopilotShell call={call} />
        </aside>
      </div>
    </div>
  );
}
