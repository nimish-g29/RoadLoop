import { STATE_LABEL, SEVERITY_COLOR, daysSince, fmtDays } from "../format.js";

function SlaClock({ filedAt, slaDays }) {
  const d = daysSince(filedAt);
  if (d === null) return null;
  const left = slaDays - d;
  const cls = left < 0 ? "sla over" : left < 2 ? "sla near" : "sla";
  return <span className={cls}>{left < 0 ? `SLA +${fmtDays(-left)}` : `SLA ${fmtDays(left)} left`}</span>;
}

export default function Queue({ hotspots, selectedId, onSelect, slaDays }) {
  if (!hotspots.length) {
    return <div className="empty">No open potholes in this view. Run the demo seeder or a dashcam clip to add some.</div>;
  }
  return (
    <ol className="queue">
      {hotspots.map((h, i) => {
        const c = h.complaint;
        return (
          <li key={h.id} className={h.id === selectedId ? "row selected" : "row"} onClick={() => onSelect(h.id)}>
            <span className="rank">{i + 1}</span>
            <div className="row-main">
              <div className="row-top">
                <span className="sev" style={{ background: SEVERITY_COLOR[h.severity] }}>{h.severity}</span>
                <span className="place">{h.address || h.road_name || `${h.lat.toFixed(4)}, ${h.lon.toFixed(4)}`}</span>
              </div>
              <div className="row-meta">
                <span className={`state st-${h.state}`}>{STATE_LABEL[h.state] || h.state}</span>
                <span>{h.distinct_riders} riders</span>
                <span>open {fmtDays(daysSince(h.confirmed_at || h.first_seen))}</span>
                {c?.portal_complaint_id && <span className="mono">{c.portal_complaint_id}</span>}
                {c?.status === "filed" && <SlaClock filedAt={c.filed_at} slaDays={slaDays} />}
                {h.escalation_level > 0 && <span className="esc">L{h.escalation_level}</span>}
                {h.recurrence_of_id && <span className="esc">recurred</span>}
              </div>
            </div>
            <span className="prio" title="severity × traffic exposure × days open">{h.priority.toFixed(0)}</span>
          </li>
        );
      })}
    </ol>
  );
}
