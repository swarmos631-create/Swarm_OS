import { useState, type FormEvent } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { ApiError } from "../api/client";
import { Field } from "../components/ui";
import { Icon } from "../lib/icons";
import { useSession } from "../state/session";
import { useOnline } from "../state/ui";

export function LoginPage() {
  const { login } = useSession();
  const nav = useNavigate();
  const loc = useLocation();
  const online = useOnline();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!email.includes("@")) return setError("Enter a valid email address");
    if (!password) return setError("Enter your password");
    setBusy(true);
    setError(null);
    try {
      await login(email, password);
      // Return to the deep link the user originally opened (only same-app paths).
      nav(loc.pathname.startsWith("/c/") ? `${loc.pathname}${loc.search}` : "/", { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Sign-in failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ minHeight: "100%", display: "grid", placeItems: "center", padding: 16 }}>
      <form className="panel" onSubmit={submit} style={{ width: "min(420px, 100%)", padding: 26 }} noValidate aria-labelledby="login-title">
        <div className="row" style={{ gap: 12, marginBottom: 18 }}>
          <img src="/icons/favicon.svg" alt="" width={44} height={44} />
          <div>
            <h1 id="login-title">SwarmOS</h1>
            <p className="muted small">Intelligent operations for Indian cities</p>
          </div>
        </div>
        <div className="stack-lg">
          <Field label="Email">
            <input className="input" type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} required />
          </Field>
          <Field label="Password">
            <input className="input" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
          </Field>
          {error && <div className="notice error" role="alert"><Icon name="alert" />{error}</div>}
          {!online && <div className="notice offline"><Icon name="wifiOff" />You are offline. Signing in requires a connection.</div>}
          <button className="btn primary" type="submit" disabled={busy || !online} style={{ height: 40 }}>{busy ? "Signing in…" : "Sign in"}</button>
          <p className="xs faint">
            Access is restricted to authorised city operators. All sign-ins and privileged actions are audited.
            Development accounts are created by <code>swarmos init</code>; their passwords are in <code>var/dev-credentials.txt</code>.
          </p>
        </div>
      </form>
    </div>
  );
}
