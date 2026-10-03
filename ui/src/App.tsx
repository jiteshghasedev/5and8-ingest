import { useCallback, useEffect, useState } from "react";

type Row = Record<string, any>;
type Tab = "ingest" | "packages";

const ACTIVE = ["pending", "validating", "copying", "verifying", "running"];

class Unauthorized extends Error {}

async function api<T = any>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    ...init,
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
  });
  if (res.status === 401) throw new Unauthorized();
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || res.statusText);
  return body;
}

// Re-fetch every `ms` while the tab is visible. `bump` forces a refresh after an action.
function usePoll<T>(url: string | null, onAuthFail: () => void, ms = 2000) {
  const [data, setData] = useState<T>();
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!url) return;
    let alive = true;
    const load = () => {
      if (document.visibilityState !== "visible") return;
      api<T>(url)
        .then((d) => alive && setData(d))
        .catch((e) => e instanceof Unauthorized && onAuthFail());
    };
    load();
    const id = setInterval(load, ms);
    document.addEventListener("visibilitychange", load);
    return () => {
      alive = false;
      clearInterval(id);
      document.removeEventListener("visibilitychange", load);
    };
  }, [url, tick]);
  return [data, () => setTick((t) => t + 1)] as const;
}

// ingest rows use SQLite UTC strings, package rows use epoch seconds
function toDate(v: string | number | null): Date | null {
  if (v == null) return null;
  return typeof v === "number" ? new Date(v * 1000) : new Date(v.replace(" ", "T") + "Z");
}

function age(v: string | number | null): string {
  const d = toDate(v);
  if (!d) return "";
  const s = Math.max(0, (Date.now() - d.getTime()) / 1000);
  if (s < 60) return `${Math.floor(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

// Created-date filter -> [since, until) in epoch seconds, on the viewer's local days
type Range = "" | "today" | "yesterday" | "7d" | "30d" | "custom";

function dateBounds(range: Range, from: string, to: string): [number?, number?] {
  const sec = (d: Date) => Math.floor(d.getTime() / 1000);
  const daysAgo = (n: number) => {
    const d = new Date();
    d.setHours(0, 0, 0, 0);
    d.setDate(d.getDate() - n);
    return d;
  };
  switch (range) {
    case "today":
      return [sec(daysAgo(0))];
    case "yesterday":
      return [sec(daysAgo(1)), sec(daysAgo(0))];
    case "7d":
      return [sec(daysAgo(6))];
    case "30d":
      return [sec(daysAgo(29))];
    case "custom": {
      const end = to ? new Date(`${to}T00:00`) : null;
      end?.setDate(end.getDate() + 1); // "to" day is inclusive
      return [from ? sec(new Date(`${from}T00:00`)) : undefined, end ? sec(end) : undefined];
    }
    default:
      return [];
  }
}

function Badge({ status }: { status: string }) {
  return <span className={`badge ${status}`}>{status}</span>;
}

// ── login ─────────────────────────────────────────────────────────────────────

function LoginPage({ onLogin }: { onLogin: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api("/api/login", { method: "POST", body: JSON.stringify({ username, password }) });
      setPassword("");
      onLogin();
    } catch (err: any) {
      setError(err instanceof Unauthorized ? "Login failed" : err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="login" onSubmit={submit}>
      <h1>5and8 Ingest</h1>
      <label>
        Username
        <input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" autoFocus />
      </label>
      <label>
        Password
        <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
      </label>
      {error && <p className="error">{error}</p>}
      <button disabled={busy || !username || !password}>{busy ? "Signing in…" : "Sign in"}</button>
      <p className="hint">Use your 5AND8 domain login.</p>
    </form>
  );
}

// ── detail panel ──────────────────────────────────────────────────────────────

function Detail({ tab, id, onClose, onAuthFail }: { tab: Tab; id: number; onClose: () => void; onAuthFail: () => void }) {
  const [data, refresh] = usePoll<Row>(`/api/${tab}/${id}`, onAuthFail);
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState({ project: "", sequence: "", shot: "" });
  const [msg, setMsg] = useState("");

  const job = data?.job;
  const failed = job?.status === "failed";

  const act = async (fn: () => Promise<unknown>, done: string) => {
    setMsg("");
    try {
      await fn();
      setMsg(done);
      setEditing(false);
      refresh();
    } catch (e: any) {
      if (e instanceof Unauthorized) onAuthFail();
      else setMsg(e.message);
    }
  };

  const retry = () => act(() => api(`/api/${tab}/${id}/retry`, { method: "POST" }), "Re-queued.");
  const save = () =>
    act(() => api(`/api/ingest/${id}`, { method: "PATCH", body: JSON.stringify(form) }), "Saved. Retry when ready.");

  return (
    <aside className="panel">
      <header>
        <h2>
          {tab === "ingest" ? "Ingest" : "Package"} #{id}
        </h2>
        <button className="ghost" onClick={onClose} aria-label="Close">
          ✕
        </button>
      </header>
      {!job ? (
        <p>Loading…</p>
      ) : (
        <>
          <dl>
            <dt>Status</dt>
            <dd>
              <Badge status={job.status} />
            </dd>
            <dt>Project</dt>
            <dd>{job.project}</dd>
            {tab === "ingest" && (
              <>
                <dt>Sequence</dt>
                <dd>{job.sequence}</dd>
              </>
            )}
            <dt>Shot</dt>
            <dd>{job.shot}</dd>
            {tab === "ingest" ? (
              <>
                <dt>Source</dt>
                <dd className="path">
                  {job.source_path}
                  {!data!.source_exists && <em> (gone)</em>}
                </dd>
                <dt>Destination</dt>
                <dd className="path">{job.destination_path || "—"}</dd>
                <dt>Retries</dt>
                <dd>{job.retry_count}</dd>
              </>
            ) : (
              <>
                <dt>Task</dt>
                <dd>{job.task || "all"}</dd>
                <dt>Attempts</dt>
                <dd>{job.attempts}</dd>
                <dt>Worker</dt>
                <dd>{job.claimed_by || "—"}</dd>
              </>
            )}
          </dl>

          {(job.error_message || (job.status === "failed" && job.result)) && (
            <pre className="error-box">{job.error_message || job.result}</pre>
          )}
          {tab === "packages" && job.status === "done" && job.result && <pre className="result-box">{job.result}</pre>}

          {failed && (
            <div className="actions">
              <button onClick={retry} disabled={tab === "ingest" && !data!.source_exists}>
                Retry
              </button>
              {tab === "ingest" && !editing && (
                <button
                  className="secondary"
                  onClick={() => {
                    setForm({ project: job.project, sequence: job.sequence, shot: job.shot });
                    setEditing(true);
                  }}
                >
                  Edit
                </button>
              )}
            </div>
          )}

          {editing && (
            <div className="edit">
              {(["project", "sequence", "shot"] as const).map((k) => (
                <label key={k}>
                  {k}
                  <input value={form[k]} onChange={(e) => setForm({ ...form, [k]: e.target.value })} />
                </label>
              ))}
              <div className="actions">
                <button onClick={save}>Save</button>
                <button className="secondary" onClick={() => setEditing(false)}>
                  Cancel
                </button>
              </div>
            </div>
          )}
          {msg && <p className="msg">{msg}</p>}

          {data!.audit?.length > 0 && (
            <>
              <h3>History</h3>
              <ul className="audit">
                {data!.audit.map((a: Row) => (
                  <li key={a.id}>
                    {a.ts} — <b>{a.user}</b> {a.action} {a.detail && <span className="muted">{a.detail}</span>}
                  </li>
                ))}
              </ul>
            </>
          )}

          <h3>Log</h3>
          <pre className="log">{data!.log?.length ? data!.log.join("\n") : "No log lines."}</pre>
        </>
      )}
    </aside>
  );
}

// ── services strip ────────────────────────────────────────────────────────────

function Services({ admin, onAuthFail }: { admin: boolean; onAuthFail: () => void }) {
  const [units, refresh] = usePoll<Row[]>("/api/services", onAuthFail, 5000);
  const [open, setOpen] = useState<string | null>(null);
  const [log] = usePoll<{ log: string[] }>(open ? `/api/services/${open}/log` : null, onAuthFail, 5000);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  const restart = async (unit: string, verb: string) => {
    if (!confirm(`${verb} ${unit}? This is logged and mailed.`)) return;
    setBusy(true);
    setMsg("");
    try {
      await api(`/api/services/${unit}/restart`, { method: "POST" });
      setMsg(`${unit}: ${verb.toLowerCase()} done.`);
      refresh();
    } catch (e: any) {
      if (e instanceof Unauthorized) onAuthFail();
      else setMsg(e.message);
    } finally {
      setBusy(false);
    }
  };

  const current = units?.find((u) => u.name === open);

  return (
    <section className="services">
      <div className="service-pills">
        <span className="muted">Services</span>
        {(units || []).map((u) => (
          <button
            key={u.name}
            className={`service ${u.state} ${open === u.name ? "on" : ""}`}
            onClick={() => {
              setOpen(open === u.name ? null : u.name);
              setMsg("");
            }}
            title={u.since ? `${u.state} since ${u.since}, restarts: ${u.restarts}` : u.state}
          >
            <i className="dot" aria-hidden /> {u.name} <span className="muted">{u.state}</span>
          </button>
        ))}
      </div>
      {current && (
        <div className="service-detail">
          <div className="service-head">
            <b>{current.name}</b>
            <span className="muted">
              {current.state}
              {current.sub && ` (${current.sub})`}
              {current.since && ` since ${current.since}`}
              {current.restarts !== "" && current.restarts != null && ` · restarts ${current.restarts}`}
            </span>
            {admin && (
              <button onClick={() => restart(current.name, current.state === "active" ? "Restart" : "Start")} disabled={busy}>
                {busy ? "Working…" : current.state === "active" ? "Restart" : "Start"}
              </button>
            )}
          </div>
          {msg && <p className="msg">{msg}</p>}
          <pre className="log">{log?.log?.length ? log.log.join("\n") : "No journal lines."}</pre>
        </div>
      )}
    </section>
  );
}

// ── dashboard ─────────────────────────────────────────────────────────────────

function Dashboard({
  name,
  admin,
  onLogout,
  onAuthFail,
}: {
  name: string;
  admin: boolean;
  onLogout: () => void;
  onAuthFail: () => void;
}) {
  const [tab, setTab] = useState<Tab>("ingest");
  const [status, setStatus] = useState("");
  const [project, setProject] = useState("");
  const [q, setQ] = useState("");
  const [range, setRange] = useState<Range>("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [selected, setSelected] = useState<number | null>(null);

  const [summary] = usePoll<{ ingest: Record<string, number>; packages: Record<string, number> }>("/api/summary", onAuthFail);
  const qs = new URLSearchParams();
  if (status) qs.set("status", status);
  if (project) qs.set("project", project);
  if (q) qs.set("q", q);
  const [since, until] = dateBounds(range, from, to);
  if (since != null) qs.set("since", String(since));
  if (until != null) qs.set("until", String(until));
  const [rows, refreshRows] = usePoll<Row[]>(`/api/${tab}?${qs}`, onAuthFail);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [rowMsg, setRowMsg] = useState<{ id: number; text: string } | null>(null);

  const retryRow = async (e: React.MouseEvent, id: number) => {
    e.stopPropagation(); // don't open the side panel
    setBusyId(id);
    setRowMsg(null);
    try {
      await api(`/api/${tab}/${id}/retry`, { method: "POST" });
      refreshRows();
    } catch (err: any) {
      if (err instanceof Unauthorized) onAuthFail();
      else setRowMsg({ id, text: err.message });
    } finally {
      setBusyId(null);
    }
  };

  const shown = rows || [];
  const filtered = !!(status || project || q || range);
  const clearFilters = () => {
    setStatus("");
    setProject("");
    setQ("");
    setRange("");
    setFrom("");
    setTo("");
  };
  const projects = Array.from(new Set((rows || []).map((r) => r.project))).sort();
  const counts = summary?.[tab] || {};
  const statuses =
    tab === "ingest"
      ? ["pending", "validating", "copying", "verifying", "completed", "failed"]
      : ["pending", "running", "done", "failed"];

  const switchTab = (t: Tab) => {
    setTab(t);
    setStatus("");
    setSelected(null);
    setRowMsg(null);
  };

  return (
    <div className="app">
      <header className="top">
        <h1>5and8 Ingest</h1>
        <nav>
          <button className={tab === "ingest" ? "tab on" : "tab"} onClick={() => switchTab("ingest")}>
            Ingest
          </button>
          <button className={tab === "packages" ? "tab on" : "tab"} onClick={() => switchTab("packages")}>
            Packaging
          </button>
        </nav>
        <span className="user">
          {name}{" "}
          <button className="ghost" onClick={onLogout}>
            Sign out
          </button>
        </span>
      </header>

      <Services admin={admin} onAuthFail={onAuthFail} />

      <section className="chips">
        <button className={status === "" ? "chip on" : "chip"} onClick={() => setStatus("")}>
          all
        </button>
        {statuses.map((s) => (
          <button key={s} className={status === s ? `chip on ${s}` : `chip ${s}`} onClick={() => setStatus(status === s ? "" : s)}>
            {s} <b>{counts[s] || 0}</b>
          </button>
        ))}
      </section>

      <section className="filters">
        <select value={project} onChange={(e) => setProject(e.target.value)}>
          <option value="">All projects</option>
          {projects.map((p) => (
            <option key={p}>{p}</option>
          ))}
        </select>
        <input placeholder="Search shot…" value={q} onChange={(e) => setQ(e.target.value)} />
        <select value={range} onChange={(e) => setRange(e.target.value as Range)} aria-label="Created">
          <option value="">Created: any time</option>
          <option value="today">Created today</option>
          <option value="yesterday">Created yesterday</option>
          <option value="7d">Created last 7 days</option>
          <option value="30d">Created last 30 days</option>
          <option value="custom">Created between…</option>
        </select>
        {range === "custom" && (
          <>
            <input type="date" className="date" value={from} max={to || undefined} onChange={(e) => setFrom(e.target.value)} aria-label="From" />
            <input type="date" className="date" value={to} min={from || undefined} onChange={(e) => setTo(e.target.value)} aria-label="To" />
          </>
        )}
        {filtered && (
          <button className="ghost" onClick={clearFilters}>
            Clear filters
          </button>
        )}
        <button className="secondary export" onClick={() => (window.location.href = `/api/export/${tab}?${qs}`)}>
          Export CSV
        </button>
      </section>
      {shown.length >= 500 && <p className="muted note">Showing the newest 500. Export CSV includes every matching job.</p>}

      <main className={selected ? "with-panel" : ""}>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>#</th>
                <th>Project</th>
                {tab === "ingest" && <th>Seq</th>}
                <th>Shot</th>
                {tab === "packages" && <th>Task</th>}
                <th>Status</th>
                <th>Updated</th>
                <th>Error</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {shown.map((r) => (
                <tr
                  key={r.id}
                  className={`${selected === r.id ? "sel" : ""} ${ACTIVE.includes(r.status) ? "active" : ""}`}
                  onClick={() => setSelected(r.id)}
                >
                  <td>{r.id}</td>
                  <td>{r.project}</td>
                  {tab === "ingest" && <td>{r.sequence}</td>}
                  <td>{r.shot}</td>
                  {tab === "packages" && <td>{r.task || "all"}</td>}
                  <td>
                    <Badge status={r.status} />
                  </td>
                  <td title={String(r.updated_at ?? r.finished_at ?? r.claimed_at ?? r.created_at)}>
                    {age(r.updated_at ?? r.finished_at ?? r.claimed_at ?? r.created_at)}
                  </td>
                  <td className="err" title={rowMsg && rowMsg.id === r.id ? rowMsg.text : undefined}>
                    {rowMsg && rowMsg.id === r.id ? `Retry refused: ${rowMsg.text}` : r.status === "failed" ? r.error_message || r.result : ""}
                  </td>
                  <td>
                    {r.status === "failed" && (
                      <button className="row-retry" onClick={(e) => retryRow(e, r.id)} disabled={busyId === r.id}>
                        {busyId === r.id ? "…" : "Retry"}
                      </button>
                    )}
                  </td>
                </tr>
              ))}
              {rows && shown.length === 0 && (
                <tr>
                  <td colSpan={8} className="empty">
                    No jobs match.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        {selected && <Detail key={`${tab}-${selected}`} tab={tab} id={selected} onClose={() => setSelected(null)} onAuthFail={onAuthFail} />}
      </main>
    </div>
  );
}

export default function App() {
  const [me, setMe] = useState<{ user: string; name: string; admin: boolean } | null | undefined>(undefined);

  const check = useCallback(() => {
    api("/api/me")
      .then(setMe)
      .catch(() => setMe(null));
  }, []);
  useEffect(check, [check]);

  if (me === undefined) return null;
  if (me === null) return <LoginPage onLogin={check} />;
  return (
    <Dashboard
      name={me.name}
      admin={me.admin}
      onAuthFail={() => setMe(null)}
      onLogout={() => api("/api/logout", { method: "POST" }).finally(() => setMe(null))}
    />
  );
}
