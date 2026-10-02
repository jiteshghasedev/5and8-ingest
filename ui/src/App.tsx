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

// ── dashboard ─────────────────────────────────────────────────────────────────

function Dashboard({ name, onLogout, onAuthFail }: { name: string; onLogout: () => void; onAuthFail: () => void }) {
  const [tab, setTab] = useState<Tab>("ingest");
  const [status, setStatus] = useState("");
  const [project, setProject] = useState("");
  const [q, setQ] = useState("");
  const [selected, setSelected] = useState<number | null>(null);

  const [summary] = usePoll<{ ingest: Record<string, number>; packages: Record<string, number> }>("/api/summary", onAuthFail);
  const qs = new URLSearchParams();
  if (tab === "ingest") {
    if (status) qs.set("status", status);
    if (project) qs.set("project", project);
    if (q) qs.set("q", q);
  }
  const [rows] = usePoll<Row[]>(`/api/${tab}?${qs}`, onAuthFail);

  // packages endpoint returns recent rows; filter those client-side
  const shown = (rows || []).filter(
    (r) =>
      tab === "ingest" ||
      ((!status || r.status === status) &&
        (!project || r.project === project) &&
        (!q || `${r.shot} ${r.task || ""}`.toLowerCase().includes(q.toLowerCase()))),
  );
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
      </section>

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
                  <td className="err">{r.status === "failed" ? r.error_message || r.result : ""}</td>
                </tr>
              ))}
              {rows && shown.length === 0 && (
                <tr>
                  <td colSpan={7} className="empty">
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
  const [me, setMe] = useState<{ user: string; name: string } | null | undefined>(undefined);

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
      onAuthFail={() => setMe(null)}
      onLogout={() => api("/api/logout", { method: "POST" }).finally(() => setMe(null))}
    />
  );
}
