function Row({ label, children }) {
  return (
    <div className="kv">
      <span className="kv-label">{label}</span>
      <span className="kv-value">{children}</span>
    </div>
  );
}

export default function CustomerCard({ card }) {
  if (!card?.customer) {
    return (
      <div className="card">
        <h2>Customer</h2>
        <p className="empty">The customer card appears when the customer starts speaking.</p>
      </div>
    );
  }
  const { customer, notice } = card;
  return (
    <div className="card">
      <h2>Customer</h2>
      <div className="customer-head">
        <div className="avatar">{customer.name.slice(0, 1)}</div>
        <div>
          <strong>{customer.name}</strong>
          <div className="muted small">
            {customer.type} · {customer.tier} · {customer.area}
          </div>
        </div>
      </div>
      <Row label="Plan">{customer.pack.name}</Row>
      <Row label="Data left">
        {customer.pack.data_left_gb} GB (until {customer.pack.valid_until})
      </Row>
      <Row label="Balance">Tk {customer.balance_bdt}</Row>
      <Row label="Recent recharges">
        {customer.recent_recharges.length
          ? customer.recent_recharges.map((r) => `Tk ${r.amount_bdt} (${r.date})`).join(", ")
          : "None"}
      </Row>
      <Row label="Open tickets">
        {customer.open_tickets.length
          ? customer.open_tickets.map((t) => `${t.id}: ${t.topic} (${t.age})`).join("; ")
          : "None"}
      </Row>
      {notice && (
        <div className="notice">
          <strong>Network notice</strong>
          <span>
            {notice.message}. Until {notice.until}.
          </span>
        </div>
      )}
      <p className="mock-note">Mock data built for the demo, not a live GP system.</p>
    </div>
  );
}
