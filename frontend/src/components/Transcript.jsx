import { useEffect, useRef } from "react";
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
    </div>
  );
}
