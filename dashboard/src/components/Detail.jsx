import { useEffect, useState } from "react";
import { api } from "../api.js";
import { STATE_LABEL, actorLabel, describeEvent, fmtTime } from "../format.js";

export default function Detail({ id, refreshKey, onClose }) {
  const [h, setH] = useState(null);
  const [err, setErr] = useState(null);
  const [auths, setAuths] = useState([]);
  const [pick, setPick] = useState("");
  const [wholeArea, setWholeArea] = useState(false);
  const [saved, setSaved] = useState(null);

  useEffect(() => {
    api.authorities().then(setAuths).catch(() => setAuths([]));
  }, []);

  async function saveAuthority() {
    if (!pick) return;
    try {
      await api.setAuthority(id, pick, wholeArea);
      setSaved("Saved. Nearby potholes on this road will reuse it.");
      setH(await api.hotspot(id));
    } catch (e) {
      setSaved(`Failed: ${e.message}`);
    }
    setTimeout(() => setSaved(null), 5000);
  }
  const canChangeOwner = h && ["candidate", "confirmed", "filing_failed"].includes(h.state);

  useEffect(() => {
    let alive = true;
    api.hotspot(id).then((d) => alive && (setH(d), setErr(null))).catch((e) => alive && setErr(e.message));
    return () => {
      alive = false;
    };
  }, [id, refreshKey]);

  if (err) return <aside className="detail"><p className="error">{err}</p></aside>;
  if (!h) return <aside className="detail"><p className="muted">Loading…</p></aside>;

  return (
    <aside className="detail">
      <header>
        <div>
          <p className="eyebrow">{h.severity} severity · {Math.round(h.confidence * 100)}% crowd confidence</p>
          <h2>{h.address || h.road_name || "Unnamed road"}</h2>
          <p className="muted">
            {h.lat.toFixed(5)}, {h.lon.toFixed(5)} ·{" "}
            <a href={`https://maps.google.com/?q=${h.lat},${h.lon}`} target="_blank" rel="noreferrer">open in Maps</a>
          </p>
        </div>
        <button className="ghost" onClick={onClose} aria-label="Close">×</button>
      </header>

      <div className={`banner st-${h.state}`}>{STATE_LABEL[h.state] || h.state}</div>

      {h.photos.length > 0 && (
        <div className="photos">
          {h.photos.map((p) => (
            <a key={p} href={p} target="_blank" rel="noreferrer"><img src={p} alt="Blurred hazard frame" /></a>
          ))}
        </div>
      )}

      <section>
        <h3>Responsible authority</h3>
        <p>
          <b>{auths.find((a) => a.key === h.authority_key)?.name || h.authority_key || "Not decided yet"}</b>
        </p>
        {canChangeOwner && auths.length > 0 && (
          <div className="owner-edit">
            <select value={pick} onChange={(e) => setPick(e.target.value)} aria-label="Set responsible authority">
              <option value="">Change authority…</option>
              {auths.map((a) => (
                <option key={a.key} value={a.key}>{a.name}{a.usable ? "" : " (portal not set up)"}</option>
              ))}
            </select>
            <label className="toggle"><input type="checkbox" checked={wholeArea} onChange={(e) => setWholeArea(e.target.checked)} /> whole area</label>
            <button className="ghost dark" disabled={!pick} onClick={saveAuthority}>Save</button>
          </div>
        )}
        {saved && <p className="muted">{saved}</p>}
      </section>

      <section>
        <h3>Complaints</h3>
        {h.complaints.length === 0 && <p className="muted">Not filed yet.</p>}
        {h.complaints.map((c) => (
          <div key={c.id} className="complaint">
            <div>
              <b className="mono">{c.portal_complaint_id || "—"}</b> <span className="muted">{c.kind} · {c.portal_key}</span>
            </div>
            <div className="muted">
              {c.status}
              {c.portal_status_label ? ` · portal: ${c.portal_status_label}` : ""}
              {c.photo_attached ? " · photo attached" : ""}
              {c.needs_human_reason ? ` · needs rider: ${c.needs_human_reason}` : ""}
            </div>
            {c.reason && <div className="reason">{c.reason}</div>}
          </div>
        ))}
      </section>

      <section>
        <h3>Timeline</h3>
        <ol className="timeline">
          {h.timeline.map((e, i) => (
            <li key={i} className={e.actor.startsWith("tinyfish") || e.actor.startsWith("monitor") ? "tf" : ""}>
              <span className="when">{fmtTime(e.created_at)}</span>
              <span className="who">{actorLabel(e.actor)}</span>
              <span className="what">{describeEvent(e)}</span>
            </li>
          ))}
        </ol>
      </section>
    </aside>
  );
}
