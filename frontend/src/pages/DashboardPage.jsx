import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";

function Kpi({ label, value, detail }) {
  return (
    <div className="card kpi">
      <span className="kpi-value">{value}</span>
      <span className="muted">{label}</span>
      {detail && <span className="muted small">{detail}</span>}
    </div>
  );
}

function trendTitle(rows, range) {
  if (!range || (!range.from_date && !range.to_date) || rows.length === 0) return "Customer mood, last 7 days";
  if (rows.length === 1) return `Customer mood, ${rows[0].day}`;
  return `Customer mood, ${rows[0].day} to ${rows[rows.length - 1].day}`;
}

function MoodTrend({ rows, range }) {
  const filtered = range && (range.from_date || range.to_date);
  return (
    <div className="card">
      <h2>{trendTitle(rows, range)}</h2>
      {filtered && range.from_date && rows.length > 0 && rows[0].date > range.from_date && (
        <p className="muted small">The range is longer than {rows.length} days, so the bars show its last {rows.length} days.</p>
      )}
      {rows.map((row) => (
        <div className="trend-row" key={row.date}>
          <span className="trend-day">{row.day}</span>
          <div className="trend-bar" role="img" aria-label={`${row.day}: ${row.calls} calls`}>
            <span className="seg mood-calm" style={{ width: `${row.calm}%` }} title={`Calm: ${row.calm_calls} of ${row.rated} calls`} />
            <span
              className="seg mood-frustrated"
              style={{ width: `${row.frustrated}%` }}
              title={`Frustrated: ${row.frustrated_calls} of ${row.rated} calls`}
            />
            <span className="seg mood-angry" style={{ width: `${row.angry}%` }} title={`Angry: ${row.angry_calls} of ${row.rated} calls`} />
          </div>
          <span className="trend-count muted small">{row.calls === 0 ? "no calls" : `${row.calls} ${row.calls === 1 ? "call" : "calls"}`}</span>
        </div>
      ))}
      <p className="muted small">
        Each call counts once, by the worst mood the customer showed. The bar is the share of the day&rsquo;s calls with a mood reading.
      </p>
    </div>
  );
}

function TopSuggestions({ rows }) {
  return (
    <div className="card">
      <h2>Most shown suggestions</h2>
      {rows.length === 0 && <p className="empty">No suggestions have been shown yet.</p>}
      {rows.map((s) => (
        <div className="top-row" key={s.title}>
          <span>{s.title}</span>
          <span>
            <strong>{s.shown}</strong> <span className="muted small">shown, {s.used} used</span>
          </span>
        </div>
      ))}
    </div>
  );
}

function SourceNote({ source }) {
  if (source.fallback_active) {
    return (
      <p className="error">
        The saved calls cannot be read right now ({source.reason || "no reason given"}). These numbers cover only the calls made since the
        app started, and they are lost when it restarts.
      </p>
    );
  }
  return null;
}

// What the dashboard shows for the numbers it was given. Nothing here is sample data.
const NO_DATES = { fromDate: "", toDate: "" };

function DateFilter({ dates, onChange }) {
  const set = (key) => (e) => onChange({ ...dates, [key]: e.target.value });
  const changed = dates.fromDate !== "" || dates.toDate !== "";
  return (
    <form className="ticket-filters dashboard-filters" onSubmit={(e) => e.preventDefault()}>
      <label>
        From
        <input type="date" value={dates.fromDate} max={dates.toDate || undefined} onChange={set("fromDate")} />
      </label>
      <label>
        To
        <input type="date" value={dates.toDate} min={dates.fromDate || undefined} onChange={set("toDate")} />
      </label>
      <div className="ticket-filter-actions">
        <button className="btn btn-ghost" type="button" onClick={() => onChange(NO_DATES)} disabled={!changed}>
          All dates
        </button>
      </div>
    </form>
  );
}

export function DashboardView({ data, loading, onRefresh, dates = NO_DATES, onDatesChange }) {
  const { kpis } = data;
  const rate = kpis.acceptance_rate === null ? "\u2013" : `${kpis.acceptance_rate}%`;
  const filtered = dates.fromDate !== "" || dates.toDate !== "";
  return (
    <div className="dashboard">
      <div className="card-head dashboard-head">
        <h2>Copilot adoption</h2>
        <button className="btn btn-ghost" type="button" onClick={onRefresh} disabled={loading}>
          {loading ? "Refreshing\u2026" : "Refresh"}
        </button>
      </div>
      {onDatesChange && <DateFilter dates={dates} onChange={onDatesChange} />}
      <div className="kpi-grid">
        <Kpi label="Calls that used Copilot" value={kpis.calls_with_copilot} />
        <Kpi label="Suggestions shown" value={kpis.suggestions_shown} />
        <Kpi label="Suggestions accepted" value={rate} detail={`${kpis.suggestions_accepted} of ${kpis.suggestions_shown}`} />
        <Kpi label="Tickets raised from calls" value={kpis.tickets_raised} />
      </div>
      {kpis.calls_with_copilot === 0 && (
        <p className="empty">
          {filtered
            ? "No saved calls started on these dates (Bangladesh time)."
            : "No calls have been saved yet. The numbers fill in as calls are made and ended."}
        </p>
      )}
      <div className="dash-grid">
        <MoodTrend rows={data.mood_trend} range={data.range} />
        <TopSuggestions rows={data.top_suggestions} />
      </div>
      <SourceNote source={data.source} />
    </div>
  );
}

// Adoption of the Copilot. Every number is counted from the saved calls when the page is opened or refreshed.
export default function DashboardPage() {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [dates, setDates] = useState(NO_DATES);
  const latest = useRef(0); // only the answer to the newest request is shown

  const load = useCallback(async () => {
    const id = ++latest.current;
    setLoading(true);
    setError("");
    try {
      const result = await api.dashboard(dates);
      if (id === latest.current) setData(result);
    } catch (e) {
      if (id === latest.current) setError(e.message);
    } finally {
      if (id === latest.current) setLoading(false);
    }
  }, [dates]);

  useEffect(() => {
    load();
  }, [load]);

  if (!data && error) return <p className="error">{error}</p>;
  if (!data) return <p className="muted">Loading&hellip;</p>;
  return (
    <>
      {error && <p className="error">{error}</p>}
      <DashboardView data={data} loading={loading} onRefresh={load} dates={dates} onDatesChange={setDates} />
    </>
  );
}
