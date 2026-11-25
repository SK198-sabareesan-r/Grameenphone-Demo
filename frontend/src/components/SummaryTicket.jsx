import { useEffect, useState } from "react";
import { api } from "../api/client";

const FIELDS = [
  ["problem", "Problem"],
  ["checked", "What was checked"],
  ["outcome", "Outcome"],
  ["follow_up", "Follow-up"],
];

// A saved session: the summary and the ticket as they were stored. Nothing to edit or submit.
function SavedSummary({ summary, ticket }) {
  return (
    <div className="card">
      <h2>Call summary and ticket</h2>
      {!summary && <p className="empty">No summary was saved for this call.</p>}
      {summary && (
        <dl className="saved-summary">
          {summary.subject && (
            <>
              <dt>Subject</dt>
              <dd>{summary.subject}</dd>
            </>
          )}
          {summary.category && (
            <>
              <dt>Category</dt>
              <dd>{summary.category}</dd>
            </>
          )}
          {FIELDS.map(([key, label]) =>
            summary[key] ? (
              <div key={key}>
                <dt>{label}</dt>
                <dd>{summary[key]}</dd>
              </div>
            ) : null
          )}
        </dl>
      )}
      {ticket ? (
        <p className="ticket-result">
          Ticket created: <strong>{ticket.ticket_id}</strong>
        </p>
      ) : (
        <p className="muted small">No ticket was raised for this call.</p>
      )}
    </div>
  );
}

export default function SummaryTicket({ call }) {
  const { summary, ticket, ended, callSid, card } = call;
  const [form, setForm] = useState(null);
  const [categories, setCategories] = useState([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.ticketCategories().then((r) => setCategories(r.categories)).catch(() => {});
  }, []);
  useEffect(() => {
    setForm(summary ? { ...summary } : null);
  }, [summary]);

  if (!ended) return null;
  if (call.readOnly) return <SavedSummary summary={summary} ticket={ticket} />;

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.createTicket({ ...form, call_sid: callSid, customer_id: card?.customer?.id || "" });
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const update = (key) => (e) => setForm({ ...form, [key]: e.target.value });

  return (
    <div className="card">
      <h2>Call summary and ticket</h2>
      {!form && <p className="muted">Writing the summary…</p>}
      {form && !ticket && (
        <form className="form" onSubmit={submit}>
          <label>
            Subject
            <input value={form.subject} maxLength={120} onChange={update("subject")} />
          </label>
          <label>
            Category
            <select value={form.category} onChange={update("category")}>
              {categories.map((c) => (
                <option key={c}>{c}</option>
              ))}
            </select>
          </label>
          {FIELDS.map(([key, label]) => (
            <label key={key}>
              {label}
              <textarea rows={2} value={form[key]} onChange={update(key)} />
            </label>
          ))}
          {error && <p className="error">{error}</p>}
          <button className="btn btn-primary" type="submit" disabled={busy}>
            Submit ticket
          </button>
        </form>
      )}
      {ticket && (
        <p className="ticket-result">
          Ticket created: <strong>{ticket.ticket_id}</strong>
        </p>
      )}
    </div>
  );
}
