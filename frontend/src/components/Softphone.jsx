import { useState } from "react";
import { api } from "../api/client";

const LABELS = {
  offline: "Softphone offline",
  connecting: "Softphone connecting…",
  ready: "Softphone ready",
  ringing: "Incoming call",
  "in-call": "On a call",
  error: "Softphone error",
};

export default function Softphone({ softphone, callActive, callEnded }) {
  const { status, message, answer, hangUp } = softphone;
  const [error, setError] = useState("");

  async function endCall() {
    setError("");
    try {
      await api.endCall();
    } catch (e) {
      setError(e.message);
    }
  }

  return (
    <div className="card softphone">
      <div className="softphone-info">
        <span className={`dot dot-${status}`} />
        <span>{LABELS[status]}</span>
        {status === "error" && <span className="muted small">{message}</span>}
      </div>
      <div className="softphone-actions">
        {status === "ringing" && (
          <button className="btn btn-primary" onClick={answer}>
            Answer
          </button>
        )}
        {status === "in-call" && (
          <button className="btn btn-danger" onClick={hangUp}>
            Hang up
          </button>
        )}
        <button
          className="btn btn-ghost"
          onClick={endCall}
          disabled={!callActive || callEnded}
          title="Close the call on screen and write the summary"
        >
          End call and summarise
        </button>
      </div>
      {error && <p className="error">{error}</p>}
    </div>
  );
}
