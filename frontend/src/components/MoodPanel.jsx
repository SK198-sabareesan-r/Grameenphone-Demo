const MOODS = [
  { id: "calm", label: "Calm" },
  { id: "neutral", label: "Neutral" },
  { id: "frustrated", label: "Frustrated" },
  { id: "angry", label: "Angry" },
];

export default function MoodPanel({ mood }) {
  return (
    <div className="card">
      <h2>Customer mood</h2>
      <div className="mood-bar" role="img" aria-label={`Customer mood: ${mood.customer}`}>
        {MOODS.map((m) => (
          <span key={m.id} className={`mood-seg mood-${m.id} ${mood.customer === m.id ? "on" : ""}`}>
            {m.label}
          </span>
        ))}
      </div>
      {mood.tip && <p className="tip">{mood.tip}</p>}
      <p className="muted small">Agent tone: {mood.agent}</p>
    </div>
  );
}
