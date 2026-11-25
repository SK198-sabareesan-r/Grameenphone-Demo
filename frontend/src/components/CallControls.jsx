import { useState } from "react";
import { api } from "../api/client";
import { MODE_LABEL, callStatusText } from "../state/callLabels";
import StartCall from "./copilot/StartCall";

// Call status and controls. Voice calls (forwarded by the voice agent) carry a status and who handles
// the call; typed calls do not.
export default function CallControls({ call }) {
  const [error, setError] = useState("");
  const status = !call.active ? "idle" : call.ended ? "ended" : "live";
  const isVoiceCall = Boolean(call.agentMode);

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
          <span>{callStatusText(call)}</span>
          {isVoiceCall && !call.ended && (
            <span className={`mode-badge mode-${call.agentMode}`}>{MODE_LABEL[call.agentMode]}</span>
          )}
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
      {status !== "live" && <StartCall />}
      {!isVoiceCall && (
        <p className="muted small call-bar-note">
          Phone calls appear here when the voice agent forwards them. To run a call without telephony, start one
          above and use the typed lines in the transcript.
        </p>
      )}
      {call.status === "with_human" && (
        <p className="muted small call-bar-note">
          The AI is listening in the background and does not speak to the customer.
        </p>
      )}
      {error && <p className="error">{error}</p>}
    </div>
  );
}
