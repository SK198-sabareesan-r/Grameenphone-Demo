import { useState } from "react";
import { api } from "../api/client";

const STATUS = {
  idle: "No call on screen",
  live: "Call in progress",
  ended: "Call ended",
};

// Call status and controls. Telephony is not connected to the Copilot yet: calls on screen come from
// typed lines until the voice-agent integration (Vobiz) is wired in.
export default function CallControls({ call }) {
  const [error, setError] = useState("");
  const status = !call.active ? "idle" : call.ended ? "ended" : "live";

  async function endCall() {
    setError("");
    try {
      await api.endCall();
    } catch (e) {
      setError(e.message);
    }
  }

  return (
    <div className="card call-bar">
      <div className="call-bar-row">
        <div className="call-bar-info">
          <span className={`dot dot-${status}`} />
          <span>{STATUS[status]}</span>
          {call.callSid && <span className="muted small">{call.callSid}</span>}
        </div>
        <button
          className="btn btn-ghost"
          onClick={endCall}
          disabled={status !== "live"}
          title="Close the call on screen and write the summary"
        >
          End call and summarise
        </button>
      </div>
      <p className="muted small call-bar-note">
        Phone calls are not connected to the Copilot yet (voice integration pending). Use typed lines
        in the transcript to run a call.
      </p>
      {error && <p className="error">{error}</p>}
    </div>
  );
}
