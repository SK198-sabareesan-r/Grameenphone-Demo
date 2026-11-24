import { useState } from "react";

export default function LoginPage({ onSignIn, message }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      await onSignIn(username.trim(), password);
    } catch (e) {
      setError(e.message);
      setPassword("");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-page">
      <form className="card login-card form" onSubmit={submit}>
        <div className="login-brand">
          <img src="/assets/gp-icon-192.png" alt="Grameenphone" width="48" height="48" />
          <div>
            <strong>grameenphone</strong>
            <span className="muted"> AI Copilot</span>
          </div>
        </div>
        <h2>Sign in</h2>
        {message && <p className="muted small">{message}</p>}
        <label>
          Username
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            autoFocus
            required
          />
        </label>
        <label>
          Password
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
          />
        </label>
        {error && <p className="error">{error}</p>}
        <button className="btn btn-primary" type="submit" disabled={busy || !username.trim() || !password}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
