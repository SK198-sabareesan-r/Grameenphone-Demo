import { useState } from "react";
import { api } from "../../api/client";
import LocationSection from "./LocationSection";

function Row({ label, children }) {
  return (
    <div className="kv">
      <span className="kv-label">{label}</span>
      <span className="kv-value">{children}</span>
    </div>
  );
}

// Nothing is typed here. The number is picked up from the call, and the card loads by itself.
function Waiting({ call }) {
  const { heard, callerNumber, active, ended } = call;
  if (!active) return <p className="empty">Waiting for a call.</p>;
  return (
    <>
      <p className="empty">
        {ended
          ? "The call ended before a customer number was heard."
          : "Listening for the customer\u2019s mobile number. The card appears when it is said on the call."}
      </p>
      {heard && !heard.found && (
        <p className="prompt-banner">
          No customer found for <strong>{heard.number}</strong>. Ask the customer to say the number again. It is picked up
          automatically.
        </p>
      )}
      {callerNumber && !heard && (
        <p className="muted small">Caller ID {callerNumber}. It is not used until the number is confirmed on the call.</p>
      )}
    </>
  );
}

function Card({ call }) {
  const [error, setError] = useState("");
  const { customer } = call.card;

  async function wrongCustomer() {
    setError("");
    try {
      await api.clearIdentity();
    } catch (e) {
      setError(e.message);
    }
  }

  return (
    <>
      <div className="card-head">
        <div className="customer-head">
          <div className="avatar">{customer.name.slice(0, 1)}</div>
          <div>
            <strong>{customer.name}</strong>
            <div className="muted small">
              {customer.type} &middot; {customer.tier} &middot; {customer.msisdn}
              {call.heard?.found && ` \u00b7 heard from the ${call.heard.speaker === "customer" ? "customer" : "agent"}`}
            </div>
          </div>
        </div>
        <button className="btn btn-ghost btn-small" onClick={wrongCustomer} disabled={call.ended}>
          Not this customer
        </button>
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
      <LocationSection call={call} />
      {error && <p className="error">{error}</p>}
      <p className="mock-note">Mock data built for the demo, not a live GP system.</p>
    </>
  );
}

export default function CustomerSection({ call }) {
  return <section className="shell-section">{call.card ? <Card call={call} /> : <Waiting call={call} />}</section>;
}
