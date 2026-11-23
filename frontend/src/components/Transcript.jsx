import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";

const SPEAKER_LABEL = { agent: "Agent", customer: "Customer" };

function Line({ speaker, text, partial }) {
  return (
    <div className={`line line-${speaker} ${partial ? "line-partial" : ""}`}>
      <span className="line-who">{SPEAKER_LABEL[speaker]}</span>
      <span className="line-text">{text}</span>
    </div>
  );
}

function TypedLine() {
  const [speaker, setSpeaker] = useState("customer");
  const [text, setText] = useState("");
  const [error, setError] = useState("");

  async function submit(event) {
    event.preventDefault();
    if (!text.trim()) return;
    setError("");
    try {
      await api.addTranscriptLine(speaker, text.trim());
      setText("");
    } catch (e) {
      setError(e.message);
    }
  }

  return (
    <form className="typed-line" onSubmit={submit}>
      <select value={speaker} onChange={(e) => setSpeaker(e.target.value)} aria-label="Speaker">
        <option value="customer">Customer</option>
        <option value="agent">Agent</option>
      </select>
      <input
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder="Type a line if live audio is not available…"
      />
      <button className="btn btn-ghost" type="submit">
        Send
      </button>
      {error && <p className="error">{error}</p>}
    </form>
  );
}

export default function Transcript({ turns, partials }) {
  const endRef = useRef(null);
  useEffect(() => endRef.current?.scrollIntoView({ block: "end" }), [turns, partials]);
  const empty = turns.length === 0 && !partials.customer && !partials.agent;

  return (
    <div className="card transcript-card">
      <h2>Live transcript</h2>
      <div className="transcript" aria-live="polite">
        {empty && <p className="empty">Waiting for a call. The transcript appears as people speak.</p>}
        {turns.map((t) => (
          <Line key={t.id} speaker={t.speaker} text={t.text} />
        ))}
        {Object.entries(partials).map(
          ([speaker, text]) => text && <Line key={`p-${speaker}`} speaker={speaker} text={text} partial />
        )}
        <div ref={endRef} />
      </div>
      <TypedLine />
    </div>
  );
}
