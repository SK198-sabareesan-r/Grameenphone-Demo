const MOODS = [
  { id: "calm", label: "Calm" },
  { id: "neutral", label: "Neutral" },
  { id: "frustrated", label: "Frustrated" },
  { id: "angry", label: "Angry" },
];
const ICON = { red: "!", pending: "\u2022", green: "\u2713" };

// Customer mood and the compliance reminders, side by side in one strip.
export default function SignalsSection({ mood, compliance }) {
  return (
    <section className="shell-section">
      <p className="section-label">Customer mood</p>
      <div className="mood-bar" role="img" aria-label={`Customer mood: ${mood.customer}`}>
        {MOODS.map((m) => (
          <span key={m.id} className={`mood-seg mood-${m.id} ${mood.customer === m.id ? "on" : ""}`}>
            {m.label}
          </span>
        ))}
      </div>
      {mood.tip && <p className="tip">{mood.tip}</p>}
      {compliance.length > 0 && (
        <>
          <p className="section-label">Compliance</p>
          <ul className="compliance">
            {compliance.map((item) => (
              <li key={item.id} className={`compliance-item state-${item.state}`}>
                <span className="compliance-icon">{ICON[item.state]}</span>
                <span>
                  <strong>{item.label}</strong>
                  <span className="compliance-msg">{item.message}</span>
                </span>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
