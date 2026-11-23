const ICON = { red: "!", pending: "•", green: "✓" };

export default function CompliancePanel({ items }) {
  return (
    <div className="card">
      <h2>Compliance</h2>
      {items.length === 0 ? (
        <p className="empty">No reminders yet.</p>
      ) : (
        <ul className="compliance">
          {items.map((item) => (
            <li key={item.id} className={`compliance-item state-${item.state}`}>
              <span className="compliance-icon">{ICON[item.state]}</span>
              <span>
                <strong>{item.label}</strong>
                <span className="compliance-msg">{item.message}</span>
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
