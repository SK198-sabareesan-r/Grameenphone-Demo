import { useState } from "react";
import { api } from "../api/client";
import NetworkStatus from "./NetworkStatus";

function Row({ label, children }) {
  return (
    <div className="kv">
      <span className="kv-label">{label}</span>
      <span className="kv-value">{children}</span>
    </div>
  );
}

// Only rendered after the agent has confirmed the mobile number.
export default function CustomerCard({ card, callEnded }) {
  const [error, setError] = useState("");
  const { customer, registered_status: registered } = card;

  async function wrongCustomer() {
    setError("");
    try {
      await api.clearIdentity();
    } catch (e) {
      setError(e.message);
    }
  }

  return (
    <div className="card">
      <div className="card-head">
        <h2>Customer</h2>
        <button className="btn btn-ghost btn-small" onClick={wrongCustomer} disabled={callEnded}>
          Not this customer
        </button>
      </div>
      <div className="customer-head">
        <div className="avatar">{customer.name.slice(0, 1)}</div>
        <div>
          <strong>{customer.name}</strong>
          <div className="muted small">
            {customer.type} · {customer.tier} · {customer.msisdn}
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
      <p className="section-label">Network at the registered location</p>
      <NetworkStatus status={registered} />
      {error && <p className="error">{error}</p>}
      <p className="mock-note">Mock data built for the demo, not a live GP system.</p>
    </div>
  );
}
