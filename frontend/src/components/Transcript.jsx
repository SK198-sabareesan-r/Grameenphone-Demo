import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { SPEAKER_LABEL, formatClock } from "../state/callLabels";

function Line({ speaker, text, partial, ts }) {
  const time = partial ? "" : formatClock(ts);
  return (
    <div className={`line line-${speaker} ${partial ? "line-partial" : ""}`}>
      <span className="line-who">
        {SPEAKER_LABEL[speaker] ?? speaker}
        {time && <span className="line-time">{time}</span>}
      </span>
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
        <option value="human_agent">Agent</option>
        <option value="ai_agent">AI agent</option>
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
  const boxRef = useRef(null);
  const stickRef = useRef(true); // follow new lines only while the reader is at the bottom of the transcript
  const onScroll = () => {
    const box = boxRef.current;
    if (box) stickRef.current = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  };
  useEffect(() => {
    const box = boxRef.current;
    // Scroll only the transcript box. scrollIntoView would also scroll the page and pull the agent away from
    // the suggested answer they are reading.
    if (box && stickRef.current) box.scrollTop = box.scrollHeight;
  }, [turns, partials]);
  const empty = turns.length === 0 && !Object.values(partials).some(Boolean);

  return (
    <div className="card transcript-card">
      <h2>Live transcript</h2>
      <div className="transcript" aria-live="polite" ref={boxRef} onScroll={onScroll}>
        {empty && <p className="empty">Waiting for a call. The transcript appears as people speak.</p>}
        {turns.map((t) => (
          <Line key={t.id} speaker={t.speaker} text={t.text} ts={t.ts} />
        ))}
        {Object.entries(partials).map(
          ([speaker, text]) => text && <Line key={`p-${speaker}`} speaker={speaker} text={text} partial />
        )}
      </div>
      <TypedLine />
    </div>
  );
}
