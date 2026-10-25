import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { useParams } from "react-router-dom";

import { api } from "../api/client";
import { ErrorState, Field, Panel, StatusBadge } from "../components/ui";
import { fmtTime, titleCase } from "../lib/format";

interface Submission {
  reference: string;
  city_id: string;
  status: string;
  category: string | null;
  created_at: string;
  privacy: string;
}
interface RequestStatus {
  reference: string;
  status: string;
  category: string | null;
  created_at: string;
  resolved_at: string | null;
}

export default function CitizenReportPage() {
  const { cityId = "" } = useParams();
  const [text, setText] = useState("");
  const [reference, setReference] = useState("");
  const [status, setStatus] = useState<RequestStatus | null>(null);
  const [statusError, setStatusError] = useState<unknown>(null);
  const submit = useMutation({
    mutationFn: () => api<Submission>("/public/cities/" + cityId + "/service-requests", { method: "POST", body: { text: text.trim() } }),
    onSuccess: (result) => { setReference(result.reference); setStatus(null); setStatusError(null); setText(""); },
  });
  const check = useMutation({
    mutationFn: (token: string) => api<RequestStatus>("/public/cities/" + cityId + "/service-requests/" + encodeURIComponent(token.trim())),
    onSuccess: (result) => { setStatus(result); setStatusError(null); },
    onError: (error) => { setStatus(null); setStatusError(error); },
  });
  const cityLabel = cityId.replaceAll("-", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());

  return <div className="content" style={{ maxWidth: 860, margin: "0 auto" }}>
    <header className="page-head"><div><div className="brand-text"><b>SwarmOS</b><small>Citizen reporting</small></div>
      <h1>Report a city issue — {cityLabel}</h1>
      <p>Submit a service issue for staff triage. Your report is classified by SwarmOS’s local model or rules.</p>
    </div></header>
    <div className="grid cols-2">
      <Panel title="Submit a report" icon="plus">
        <form className="stack-lg" onSubmit={(event) => { event.preventDefault(); submit.mutate(); }}>
          <Field label="What happened?" hint="Avoid names, phone numbers, email addresses, or other personal details. Do not include emergency or life-threatening reports here.">
            <textarea className="textarea" value={text} onChange={(event) => setText(event.target.value)} minLength={10} maxLength={2000} rows={6} required placeholder="Describe the public service issue and its general area (for example, a road or ward; no exact address needed)." />
          </Field>
          {submit.error && <ErrorState error={submit.error} />}
          <button className="btn primary" type="submit" disabled={text.trim().length < 10 || submit.isPending}>{submit.isPending ? "Submitting…" : "Submit report"}</button>
          <p className="xs faint">The form does not ask for your name, contact details, or precise location. The network service applies short-lived rate limiting and may retain standard service logs.</p>
          {reference && <div className="notice info" role="status">
            <span><b>Report received.</b> Save this private reference to check status. We cannot recover it if you lose it.</span>
            <code className="mono" style={{ overflowWrap: "anywhere" }}>{reference}</code>
          </div>}
        </form>
      </Panel>
      <Panel title="Check a report">
        <form className="stack-lg" onSubmit={(event) => { event.preventDefault(); setStatusError(null); check.mutate(reference); }}>
          <Field label="Private report reference" hint="This reference is a secret. Anyone who has it can view the limited status shown here.">
            <input className="input mono" value={reference} onChange={(event) => { setReference(event.target.value); setStatus(null); setStatusError(null); }} maxLength={64} autoComplete="off" spellCheck={false} />
          </Field>
          <button className="btn" type="submit" disabled={reference.trim().length < 32 || check.isPending}>{check.isPending ? "Checking…" : "Check status"}</button>
          {statusError != null && <ErrorState error={statusError} />}
          {status && <div className="notice info" role="status">
            <div className="stack"><b>{status.category ? titleCase(status.category) : "Service request"}</b>
              <span className="small">Received {fmtTime(status.created_at)}</span>
              {status.resolved_at && <span className="small">Marked resolved {fmtTime(status.resolved_at)}</span>}
            </div><StatusBadge s={status.status} />
          </div>}
        </form>
      </Panel>
    </div>
    <p className="xs faint" style={{ marginTop: 18 }}>For immediate danger or emergency response, contact local emergency services. Public status lookup never displays report text, contact information, or internal staff notes.</p>
  </div>;
}
