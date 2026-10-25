export const STATE_LABEL = {
  candidate: "Gathering reports",
  confirmed: "Awaiting rider approval",
  filing: "Agent filing",
  filing_failed: "Needs rider (OTP / failed)",
  filed: "Filed",
  in_progress: "Portal: in progress",
  awaiting_verification: "Portal says closed: re-checking",
  verified_fixed: "Verified fixed",
  reopened: "Still broken: reopened",
  escalated: "Escalated",
};

export const SEVERITY_COLOR = { high: "#E5484D", medium: "#F5A623", low: "#8E9AA6" };

export function daysSince(iso) {
  if (!iso) return null;
  const ms = Date.now() - new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z").getTime();
  return Math.max(0, ms / 86400000);
}

export function fmtDays(d) {
  if (d === null || d === undefined) return "–";
  if (d < 1) return `${Math.round(d * 24)} h`;
  return `${Math.round(d)} d`;
}

export function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
  return d.toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

export function describeEvent(e) {
  const d = e.detail || {};
  if (e.action.startsWith("state:")) {
    const [, to] = e.action.slice(6).split("->");
    return `→ ${STATE_LABEL[to] || to}${d.reason ? ` · ${d.reason}` : ""}${d.complaint_id ? ` · ${d.complaint_id}` : ""}`;
  }
  const map = {
    hotspot_created: "First report",
    authority_rules: `Owner guessed: ${d.authority || "unknown"} (${Math.round((d.confidence || 0) * 100)}%)`,
    authority_resolved: `Owner confirmed by TinyFish: ${d.authority_name || d.authority}`,
    authority_from_memory: `Owner from area memory: ${d.authority} (learned by ${d.source}, reused ${d.reused}×, no TinyFish call)`,
    authority_deferred: "Finding the road before choosing the owner",
    authority_set: `Engineer set owner: ${d.authority}${d.whole_area ? " (whole area)" : ""}`,
    geocoded: `Located: ${d.road_name || d.address || "road found"}`,
    approved_filing: `Rider approved filing on ${d.portal}`,
    agent_file_started: "TinyFish agent started filing",
    agent_escalate_started: "TinyFish agent started escalation",
    monitor_created: "TinyFish Monitor watching status page",
    monitor_unavailable: "Monitor unavailable, using scheduled Fetch checks",
    portal_status: `Portal status: ${d.label || d.status}`,
    escalation_queued: `Escalation queued to ${d.target} (level ${d.level})`,
    escalation_filed: `Escalation filed: ${d.complaint_id}`,
    escalation_failed: `Escalation failed: ${d.reason}`,
    escalation_cap_reached: "Escalation limit reached",
  };
  return map[e.action] || e.action;
}

export function actorLabel(actor) {
  if (actor.startsWith("tinyfish:")) return "TinyFish";
  if (actor.startsWith("monitor:")) return "Monitor";
  if (actor.startsWith("rider:")) return "Rider";
  if (actor === "crowd") return "Riders";
  return "RoadLoop";
}
