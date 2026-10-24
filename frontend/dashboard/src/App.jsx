import { useCallback, useEffect, useState } from "react";
import { api } from "./api.js";
import MapView from "./components/MapView.jsx";
import Queue from "./components/Queue.jsx";
import Detail from "./components/Detail.jsx";

const SLA_DAYS = Number(import.meta.env.VITE_SLA_DAYS || 7);
const FILTERS = [
  { key: "", label: "All open" },
  { key: "high", label: "High" },
  { key: "medium", label: "Medium" },
  { key: "low", label: "Low" },
];

export default function App() {
  const [hotspots, setHotspots] = useState([]);
  const [stats, setStats] = useState(null);
  const [selected, setSelected] = useState(null);
  const [severity, setSeverity] = useState("");
  const [bbox, setBbox] = useState(null);
  const [inView, setInView] = useState(false);
  const [showFixed, setShowFixed] = useState(false);
  const [tick, setTick] = useState(0);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [busyJob, setBusyJob] = useState(null);

  async function runJob(job) {
    setBusyJob(job);
    try {
      const r = await api.runJob(job);
      setNotice(r.message || "Done.");
      await load();
    } catch (e) {
      setNotice(`Failed: ${e.message}`);
    } finally {
      setBusyJob(null);
      setTimeout(() => setNotice(null), 6000);
    }
  }

  const load = useCallback(async () => {
    try {
      const [q, s] = await Promise.all([
        api.queue({ severity, include_fixed: showFixed, bbox: inView ? bbox : undefined }),
        api.stats(),
      ]);
      setHotspots(q);
      setStats(s);
      setError(null);
    } catch (e) {
      setError(`Cannot reach the API (${e.message}). Is the backend running on :8000?`);
    }
  }, [severity, showFixed, inView, bbox]);

  useEffect(() => {
    load();
  }, [load, tick]);

  useEffect(() => {
    const t = setInterval(() => setTick((x) => x + 1), 5000); // live demo refresh
    return () => clearInterval(t);
  }, []);

  const openCount = hotspots.filter((h) => h.state !== "verified_fixed").length;
  const caught = stats?.closures_caught_unfixed ?? 0;
  const medians = Object.values(stats?.median_days_to_verified_fix || {});

  return (
    <div className="app">
      <header className="top">
        <div className="brand">
          <span className="logo">◎</span> RoadLoop <span className="muted">· Engineer queue</span>
        </div>
        <div className="kpis">
          <Kpi label="Open in queue" value={openCount} />
          <Kpi label="Open high-severity" value={stats?.open_high ?? "–"} accent />
          <Kpi label="Closures caught unfixed" value={caught} />
          <Kpi label="Median days to verified fix" value={medians.length ? Math.min(...medians) : "–"} />
        </div>
        <div className="actions">
          <button className="ghost" disabled={!!busyJob} onClick={() => runJob("check_statuses")}>
            {busyJob === "check_statuses" ? "Checking…" : "Check portals now"}
          </button>
          <button className="ghost" disabled={!!busyJob} onClick={() => runJob("check_sla")}>
            {busyJob === "check_sla" ? "Checking…" : "Run SLA check"}
          </button>
        </div>
      </header>

      {error && <div className="error-bar">{error}</div>}
      {notice && <div className="notice-bar" role="status">{notice}</div>}

      <main className={selected ? "body with-detail" : "body"}>
        <section className="left">
          <div className="filters">
            {FILTERS.map((f) => (
              <button key={f.key} className={severity === f.key ? "chip on" : "chip"} onClick={() => setSeverity(f.key)}>
                {f.label}
              </button>
            ))}
            <label className="toggle"><input type="checkbox" checked={inView} onChange={(e) => setInView(e.target.checked)} /> In map view</label>
            <label className="toggle"><input type="checkbox" checked={showFixed} onChange={(e) => setShowFixed(e.target.checked)} /> Show fixed</label>
          </div>
          <Queue hotspots={hotspots} selectedId={selected} onSelect={setSelected} slaDays={SLA_DAYS} />
        </section>
        <MapView hotspots={hotspots} selectedId={selected} onSelect={setSelected} onBounds={setBbox} />
        {selected && <Detail id={selected} refreshKey={tick} onClose={() => setSelected(null)} />}
      </main>
    </div>
  );
}

function Kpi({ label, value, accent }) {
  return (
    <div className={accent ? "kpi accent" : "kpi"}>
      <span className="v">{value}</span>
      <span className="l">{label}</span>
    </div>
  );
}
