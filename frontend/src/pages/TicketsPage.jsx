import { Fragment, useEffect, useState } from "react";
import { api } from "../api/client";

const STATUS_LABEL = { open: "Open", in_progress: "In progress", resolved: "Resolved", closed: "Closed", all: "All" };
const STATUS_PILL = { open: "pill-warn", in_progress: "", resolved: "pill-ok", closed: "" };
const DEFAULT_FILTERS = { mobile: "", ticket: "", fromDate: "", toDate: "", status: "open" };
const TYPING_DELAY_MS = 300;

// Dates are shown, and filtered, as Bangladesh time.
function formatCreated(value) {
  const date = new Date(value);
  if (!value || Number.isNaN(date.getTime())) return "";
  return date.toLocaleString("en-GB", { timeZone: "Asia/Dhaka", dateStyle: "medium", timeStyle: "short" });
}

const DETAILS = [
  ["problem", "Problem"],
  ["checked", "What was checked"],
  ["outcome", "Outcome"],
  ["follow_up", "Follow-up"],
];

function TicketRow({ ticket, open, onToggle }) {
  const mobile = ticket.mobile_number || ticket.caller_number || "";
  return (
    <Fragment>
      <tr className={open ? "ticket-row open" : "ticket-row"} onClick={onToggle}>
        <td>
          <button className="link-button" type="button" aria-expanded={open}>
            {ticket.ticket_id}
          </button>
        </td>
        <td>{formatCreated(ticket.created_at)}</td>
        <td>{mobile || <span className="muted">Not identified</span>}</td>
        <td>{ticket.customer_name || ""}</td>
        <td>{ticket.category}</td>
        <td>{ticket.subject}</td>
        <td>
          <span className={`pill ${STATUS_PILL[ticket.status] || ""}`}>{STATUS_LABEL[ticket.status] || ticket.status}</span>
        </td>
      </tr>
      {open && (
        <tr className="ticket-details">
          <td colSpan={7}>
            {DETAILS.map(([key, label]) =>
              ticket[key] ? (
                <p key={key}>
                  <strong>{label}:</strong> {ticket[key]}
                </p>
              ) : null
            )}
            {ticket.call_sid && <p className="muted small">Call {ticket.call_sid}</p>}
          </td>
        </tr>
      )}
    </Fragment>
  );
}

// All tickets, filtered by mobile number, ticket number, a date range and the status. Open tickets by default.
export default function TicketsPage() {
  const [filters, setFilters] = useState(DEFAULT_FILTERS);
  const [statuses, setStatuses] = useState(["open", "in_progress", "resolved", "closed"]);
  const [result, setResult] = useState({ tickets: [], count: 0, total: 0 });
  const [openId, setOpenId] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    api.ticketStatuses().then((r) => setStatuses(r.statuses)).catch(() => {});
  }, []);

  useEffect(() => {
    let current = true;
    const timer = setTimeout(async () => {
      setLoading(true);
      setError("");
      try {
        const data = await api.listTickets(filters);
        if (current) setResult(data);
      } catch (e) {
        if (current) setError(e.message);
      } finally {
        if (current) setLoading(false);
      }
    }, TYPING_DELAY_MS);
    return () => {
      current = false;
      clearTimeout(timer);
    };
  }, [filters, reload]);

  const set = (key) => (event) => setFilters({ ...filters, [key]: event.target.value });
  const changed = JSON.stringify(filters) !== JSON.stringify(DEFAULT_FILTERS);

  return (
    <div className="card">
      <div className="card-head">
        <h2>Tickets</h2>
        <span className="muted small">
          {loading ? "Loading\u2026" : `Showing ${result.count} of ${result.total} tickets`}
        </span>
      </div>
      <form className="ticket-filters" onSubmit={(e) => e.preventDefault()}>
        <label>
          Mobile number
          <input value={filters.mobile} onChange={set("mobile")} placeholder="9500799295" inputMode="tel" autoComplete="off" />
        </label>
        <label>
          Ticket number
          <input value={filters.ticket} onChange={set("ticket")} placeholder="TKT-10234" autoComplete="off" />
        </label>
        <label>
          From
          <input type="date" value={filters.fromDate} max={filters.toDate || undefined} onChange={set("fromDate")} />
        </label>
        <label>
          To
          <input type="date" value={filters.toDate} min={filters.fromDate || undefined} onChange={set("toDate")} />
        </label>
        <label>
          Status
          <select value={filters.status} onChange={set("status")}>
            {[...statuses, "all"].map((s) => (
              <option key={s} value={s}>
                {STATUS_LABEL[s] || s}
              </option>
            ))}
          </select>
        </label>
        <div className="ticket-filter-actions">
          <button className="btn btn-ghost" type="button" onClick={() => setFilters(DEFAULT_FILTERS)} disabled={!changed}>
            Clear filters
          </button>
          <button className="btn btn-ghost" type="button" onClick={() => setReload(reload + 1)}>
            Refresh
          </button>
        </div>
      </form>
      {error && <p className="error">{error}</p>}
      {!error && !loading && result.tickets.length === 0 && <p className="empty">No tickets match these filters.</p>}
      {result.tickets.length > 0 && (
        <div className="ticket-table-wrap">
          <table className="ticket-table">
            <thead>
              <tr>
                <th>Ticket</th>
                <th>Created (Bangladesh time)</th>
                <th>Mobile number</th>
                <th>Customer</th>
                <th>Category</th>
                <th>Subject</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {result.tickets.map((t) => (
                <TicketRow
                  key={`${t.customer_id}-${t.ticket_id}`}
                  ticket={t}
                  open={openId === t.ticket_id}
                  onToggle={() => setOpenId(openId === t.ticket_id ? "" : t.ticket_id)}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="muted small">Dates are Bangladesh dates. Click a ticket to see its details.</p>
    </div>
  );
}
