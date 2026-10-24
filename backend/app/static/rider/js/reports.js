// "My reports": everything this volunteer reported, live complaint status, and the
// one-tap approval that lets a TinyFish agent file the complaint under their name.
import { api } from "./api.js";
import { STATE_LABEL, h, toast } from "./util.js";

const SEV = { high: "High", medium: "Medium", low: "Low" };

export function mountReports(root, { getRiderId, uploader }) {
  const list = h("div", { class: "cards" });
  const pending = h("p", { class: "banner", hidden: true });
  const refreshBtn = h("button", { class: "btn ghost small" }, "Refresh");
  root.append(h("section", { class: "pane" },
    h("div", { class: "row between" }, h("h2", {}, "My reports"), refreshBtn), pending, list));

  uploader.addEventListener("count", (e) => {
    pending.hidden = !e.detail;
    pending.textContent = `${e.detail} report${e.detail > 1 ? "s" : ""} waiting to upload (no connection). They will send automatically.`;
  });

  async function approve(hs, btn) {
    const ok = confirm(
      "RoadLoop will file a complaint about this pothole on the city's grievance portal, " +
      "in your name and with your mobile number, and keep following it up.\n\nApprove filing?",
    );
    if (!ok) return;
    btn.disabled = true;
    try {
      await api.approve(hs.id, getRiderId());
      toast("Approved. The agent is filing it now.", "ok");
      setTimeout(refresh, 1500);
    } catch (e) {
      toast(e.message, "error");
      btn.disabled = false;
    }
  }

  function card(hs) {
    const c = hs.complaint;
    const needsMe = hs.state === "confirmed" || hs.state === "filing_failed";
    const btn = needsMe ? h("button", { class: "btn primary" }, hs.state === "filing_failed" ? "Retry filing" : "Approve filing") : null;
    btn?.addEventListener("click", () => approve(hs, btn));
    const art = h("article", { class: `card st-${hs.state}${hs.best_photo_url ? "" : " no-photo"}` },
      // Same-origin path, so photos load through whatever URL the phone used (tunnel or LAN).
      hs.best_photo_url ? h("img", { src: new URL(hs.best_photo_url, location.href).pathname, alt: "Pothole photo (blurred)", loading: "lazy" }) : null,
      h("div", { class: "card-body" },
        h("div", { class: "row" }, h("span", { class: `sev ${hs.severity}` }, SEV[hs.severity] || hs.severity),
          h("span", { class: "muted" }, `${hs.distinct_riders} reporter${hs.distinct_riders > 1 ? "s" : ""}`)),
        h("h3", {}, hs.address || hs.road_name || `${hs.lat.toFixed(5)}, ${hs.lon.toFixed(5)}`),
        h("p", { class: "state" }, STATE_LABEL[hs.state] || hs.state),
        c?.portal_complaint_id ? h("p", { class: "mono" }, `Complaint ${c.portal_complaint_id}`,
          c.portal_status_label ? ` · portal: ${c.portal_status_label}` : "") : null,
        c?.needs_human_reason && hs.state === "filing_failed"
          ? h("p", { class: "warn" }, `The portal needs you: ${c.needs_human_reason}`) : null,
        btn,
      ),
    );
    art.querySelector("img")?.addEventListener("error", (e) => {
      e.target.remove();
      art.classList.add("no-photo");
    });
    return art;
  }

  async function refresh() {
    const id = getRiderId();
    if (!id) return;
    refreshBtn.disabled = true;
    try {
      const items = await api.myHotspots(id);
      list.replaceChildren(...(items.length ? items.map(card) : [h("p", { class: "muted" },
        "Nothing yet. Use Drive mode or Report to add your first pothole.")]));
    } catch (e) {
      if (!list.children.length) list.replaceChildren(h("p", { class: "muted" }, "Can't load right now. Check your connection."));
    } finally {
      refreshBtn.disabled = false;
    }
  }

  refreshBtn.addEventListener("click", refresh);
  uploader.addEventListener("uploaded", () => refresh());
  return { refresh };
}
