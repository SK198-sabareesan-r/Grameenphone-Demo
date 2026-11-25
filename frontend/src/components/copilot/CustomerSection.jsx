import { useState } from "react";
import { api } from "../../api/client";
import LocationSection from "./LocationSection";
import NumberEntry from "./NumberEntry";

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
      <NumberEntry
        initial={callerNumber}
        label="Show card"
        placeholder="Or type the mobile number"
        hint={callerNumber ? "Pre-filled from the caller ID. Confirm it with the customer." : ""}
        callActive={active}
        callEnded={ended}
      />
    </>
  );
}

function Card({ call }) {
  const [error, setError] = useState("");
  const { customer, confirmed } = call.card;

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
            </div>
          </div>
        </div>
        <button className="btn btn-ghost btn-small" onClick={wrongCustomer} disabled={call.ended}>
          Not this customer
        </button>
      </div>
      {confirmed ? (
        <p className="confirm-state">
          <span className="pill pill-ok">Number confirmed</span>
        </p>
      ) : (
        <div className="confirm-state unconfirmed">
          <span className="pill pill-warn">Not confirmed yet</span>
          <p className="small">
            Found from the caller ID. Ask the customer to say their mobile number. It is picked up automatically,
            or confirm it here.
          </p>
          <NumberEntry
            initial={customer.msisdn}
            label="Confirm number"
            placeholder="Mobile number the customer gave"
            callActive={call.active}
            callEnded={call.ended}
          />
        </div>
      )}
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
