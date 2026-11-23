const TABS = [
  { id: "live", label: "Live call" },
  { id: "dashboard", label: "Adoption dashboard" },
];

function sttLabel(sttStatus) {
  const values = Object.values(sttStatus);
  if (values.includes("failed")) return { text: "Speech-to-text failed", tone: "danger" };
  if (values.includes("reconnecting")) return { text: "Speech-to-text reconnecting", tone: "warn" };
  if (values.length && values.every((v) => v === "connected")) {
    return { text: "Speech-to-text live", tone: "ok" };
  }
  return null;
}

export default function Header({ view, onChange, connected, sttStatus }) {
  const stt = sttLabel(sttStatus);
  return (
    <header className="gp-header">
      <div className="brand">
        <img src="/assets/gp-icon-192.png" alt="Grameenphone" width="40" height="40" />
        <div className="brand-text">
          <strong>grameenphone</strong>
          <span>AI Copilot</span>
        </div>
      </div>
      <nav className="tabs" aria-label="Views">
        {TABS.map((tab) => (
          <button
            key={tab.id}
            className={`tab ${view === tab.id ? "active" : ""}`}
            onClick={() => onChange(tab.id)}
          >
            {tab.label}
          </button>
        ))}
      </nav>
      <div className="header-status">
        {stt && <span className={`pill pill-${stt.tone}`}>{stt.text}</span>}
        <span className={`pill ${connected ? "pill-ok" : "pill-warn"}`}>
          {connected ? "Connected" : "Connecting…"}
        </span>
      </div>
    </header>
  );
}
