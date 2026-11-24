const TABS = [
  { id: "live", label: "Live call" },
  { id: "dashboard", label: "Adoption dashboard" },
];

export default function Header({ view, onChange, connected, user, onSignOut }) {
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
        <span className={`pill ${connected ? "pill-ok" : "pill-warn"}`}>
          {connected ? "Connected" : "Connecting…"}
        </span>
        {user && (
          <span className="pill" title={`Signed in as ${user.username} (${user.role})`}>
            {user.name}
          </span>
        )}
        <button className="btn header-signout" onClick={() => onSignOut()}>
          Sign out
        </button>
      </div>
    </header>
  );
}
