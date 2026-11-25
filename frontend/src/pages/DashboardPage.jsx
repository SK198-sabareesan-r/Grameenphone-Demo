import { useCallback, useEffect, useState } from "react";
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

function MoodTrend({ rows }) {
  return (
    <div className="card">
      <h2>Customer mood, last 7 days</h2>
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
  return <p className="mock-note">Counted from the saved calls ({source.backend === "dynamodb" ? "DynamoDB" : "memory"}) when this page was opened.</p>;
}

// What the dashboard shows for the numbers it was given. Nothing here is sample data.
export function DashboardView({ data, loading, onRefresh }) {
  const { kpis } = data;
  const rate = kpis.acceptance_rate === null ? "\u2013" : `${kpis.acceptance_rate}%`;
  return (
    <div className="dashboard">
      <div className="card-head dashboard-head">
        <h2>Copilot adoption</h2>
        <button className="btn btn-ghost" type="button" onClick={onRefresh} disabled={loading}>
          {loading ? "Refreshing\u2026" : "Refresh"}
        </button>
      </div>
      <div className="kpi-grid">
        <Kpi label="Calls that used Copilot" value={kpis.calls_with_copilot} />
        <Kpi label="Suggestions shown" value={kpis.suggestions_shown} />
        <Kpi label="Suggestions accepted" value={rate} detail={`${kpis.suggestions_accepted} of ${kpis.suggestions_shown}`} />
        <Kpi label="Tickets raised from calls" value={kpis.tickets_raised} />
      </div>
      {kpis.calls_with_copilot === 0 && <p className="empty">No calls have been saved yet. The numbers fill in as calls are made and ended.</p>}
      <div className="dash-grid">
        <MoodTrend rows={data.mood_trend} />
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

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setData(await api.dashboard());
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (error) return <p className="error">{error}</p>;
  if (!data) return <p className="muted">Loading&hellip;</p>;
  return <DashboardView data={data} loading={loading} onRefresh={load} />;
}
