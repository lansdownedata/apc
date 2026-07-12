/* ==========================================================================
   All Pro Charter — Lead Manager · front-end behaviour
   - Alpine store `$store.modal`  → the ONE reusable modal (never native dialogs)
   - Alpine store `$store.toast`  → stacked toast notifications
   - shell()                      → sidebar + notification-tray state
   - Tom Select auto-init         → every <select data-tom> (never native <select>)
   ========================================================================== */

/* -------------------------------------------------- Alpine stores + shell */
document.addEventListener("alpine:init", () => {
  /* ---- reusable modal -------------------------------------------------- */
  Alpine.store("modal", {
    open: false,
    title: "",
    message: "",
    html: "",
    variant: "info", // info | success | danger | gold
    confirmText: "Confirm",
    cancelText: "Cancel",
    showCancel: true,
    busy: false,
    _confirm: null,
    _cancel: null,

    _icon() {
      return {
        info: "ti-info-circle",
        success: "ti-circle-check",
        danger: "ti-alert-triangle",
        gold: "ti-sparkles",
      }[this.variant] || "ti-info-circle";
    },

    show(opts = {}) {
      this.title = opts.title || "";
      this.message = opts.message || "";
      this.html = opts.html || "";
      this.variant = opts.variant || "info";
      this.confirmText = opts.confirmText || "Confirm";
      this.cancelText = opts.cancelText || "Cancel";
      this.showCancel = opts.showCancel !== false;
      this._confirm = opts.onConfirm || null;
      this._cancel = opts.onCancel || null;
      this.busy = false;
      this.open = true;
    },

    /* Convenience: a yes/no confirmation. */
    confirm(opts = {}) {
      this.show({ confirmText: "Confirm", showCancel: true, ...opts });
    },

    /* Convenience: an acknowledgement (single button). */
    alert(opts = {}) {
      this.show({ confirmText: "Got it", showCancel: false, ...opts });
    },

    async accept() {
      if (this._confirm) {
        const result = this._confirm();
        if (result && typeof result.then === "function") {
          this.busy = true;
          try { await result; } finally { this.busy = false; }
        }
      }
      this.close();
    },

    cancel() {
      if (this._cancel) this._cancel();
      this.close();
    },

    close() {
      this.open = false;
      this.busy = false;
      this._confirm = null;
      this._cancel = null;
    },
  });

  /* ---- theme (light / dark) -------------------------------------------- */
  /* The no-flash <head> script already set <html data-theme>; mirror it here
     so the topbar toggle is reactive, and persist the user's choice. */
  Alpine.store("theme", {
    mode: document.documentElement.getAttribute("data-theme") || "light",
    toggle() { this.set(this.mode === "dark" ? "light" : "dark"); },
    set(m) {
      this.mode = m;
      document.documentElement.setAttribute("data-theme", m);
      try { localStorage.setItem("apc-theme", m); } catch (e) { /* private mode */ }
    },
  });

  /* ---- toasts ---------------------------------------------------------- */
  Alpine.store("toast", {
    items: [],
    _seq: 1,
    push({ type = "success", title = "", message = "", timeout = 4200 } = {}) {
      const id = this._seq++;
      this.items.push({ id, type, title, message });
      if (timeout) setTimeout(() => this.dismiss(id), timeout);
      return id;
    },
    dismiss(id) {
      this.items = this.items.filter((t) => t.id !== id);
    },
  });
});

/* The app shell: sidebar collapse + notification tray. */
function shell() {
  return {
    navOpen: window.innerWidth >= 1024,
    notifOpen: false,
    toggleNav() { this.navOpen = !this.navOpen; },
  };
}
window.shell = shell;

/* -------------------------------------------------- CSRF helper */
function getCookie(name) {
  const m = document.cookie.match("(^|;)\\s*" + name + "\\s*=\\s*([^;]+)");
  return m ? decodeURIComponent(m.pop()) : "";
}
window.getCookie = getCookie;

function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}
window.escapeHtml = escapeHtml;

/* -------------------------------------------------- LA sync payload preview */
// LA sync: show a recorded payload (json_script element) in the shared modal.
window.showLaPayload = function (elementId, title) {
  const el = document.getElementById(elementId);
  if (!el) return;
  const pre = document.createElement("pre");
  pre.className = "text-xs leading-relaxed overflow-auto max-h-96 p-3 rounded bg-slate-900/90 text-slate-100";
  pre.textContent = JSON.stringify(JSON.parse(el.textContent), null, 2);
  Alpine.store("modal").show({
    title,
    html: `<p class="text-xs text-muted mb-2">Preview — nothing sent to LimoAnywhere.</p>${pre.outerHTML}`,
    variant: "info",
    confirmText: "Close",
    showCancel: false,
  });
};

/* -------------------------------------------------- quoteWorkspace */
function quoteWorkspace(opts = {}) {
  return {
    leadId: opts.leadId,
    updateUrl: opts.updateUrl,
    saveUrl: opts.saveUrl,
    sendQuoteUrl: opts.sendQuoteUrl,
    reservations: opts.reservations || [],
    vehicles: opts.vehicles || [],
    header: opts.header || {},
    depositPct: 50,
    sending: false,
    editorOpen: false,
    draftIsNew: false,
    draft: null,

    saveHeader() {
      const body = new URLSearchParams(this.header);
      fetch(this.updateUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded",
          "X-CSRFToken": getCookie("csrftoken"),
        },
        body,
      }).then((r) => {
        if (r.ok) Alpine.store("toast").push({ type: "success", title: "Saved" });
        else Alpine.store("toast").push({ type: "danger", title: "Could not save" });
      }).catch(() => Alpine.store("toast").push({ type: "danger", title: "Network error — could not save" }));
    },

    sendQuote() {
      if (this.sending) return;
      this.sending = true;
      fetch(this.sendQuoteUrl, {
        method: "POST",
        headers: { "X-CSRFToken": getCookie("csrftoken") },
      })
        .then(async (r) => {
          const data = await r.json().catch(() => ({}));
          if (!data.ok) {
            Alpine.store("toast").push({
              type: "danger", title: "Couldn't send quote",
              message: data.error || "Please try again.",
            });
            return;
          }
          if (data.delivery && data.delivery.sent) {
            Alpine.store("toast").push({
              type: "success", title: "Quote sent",
              message: "Emailed to " + (data.delivery.recipient || "the customer") + ".",
            });
            setTimeout(() => window.location.reload(), 800);
          } else {
            const err = (data.delivery && data.delivery.error) || "the Podium send failed";
            Alpine.store("modal").show({
              title: "Quote saved — but not delivered",
              variant: "danger",
              html:
                "<p class='text-[13px] text-muted'>The deposit link is ready, but sending over Podium failed:</p>" +
                "<p class='text-[12px] text-rose-600 mt-1'>" + escapeHtml(err) + "</p>" +
                "<p class='text-[12px] text-muted mt-3'>Copy the link to send manually:</p>" +
                "<p class='text-[12px] text-ink break-all mt-1'>" + escapeHtml(data.link) + "</p>",
              confirmText: "Copy link", cancelText: "Close",
              onConfirm: () => {
                if (navigator.clipboard) navigator.clipboard.writeText(data.link || "");
                window.location.reload();
              },
              onCancel: () => window.location.reload(),
            });
          }
        })
        .catch(() =>
          Alpine.store("toast").push({ type: "danger", title: "Network error — could not send quote" })
        )
        .finally(() => { this.sending = false; });
    },

    blankReservation() {
      return {
        tripType: "transfer", service: "", date: "", time: "",
        vehicle: this.vehicles.length ? this.vehicles[0].id : "",
        pax: 1, baseRate: 0, hours: 4, hourlyRate: 295, minHours: 4,
        stops: [{ address: "", note: "" }, { address: "", note: "" }],
      };
    },
    money(n) { return "$" + Math.round(n || 0).toLocaleString(); },
    init() { this.draft = this.blankReservation(); },

    // ---- reservation editor ----
    syncVehicleSelect() {
      this.$nextTick(() => {
        const el = document.getElementById("f-res-vehicle");
        if (el && el.tomselect) el.tomselect.setValue(this.draft.vehicle || "", true);
      });
    },
    newReservation() {
      this.draft = this.blankReservation();
      this.draftIsNew = true;
      this.editorOpen = true;
      this.syncVehicleSelect();
    },
    editReservation(id) {
      const r = this.reservations.find((x) => x.id === id);
      if (!r) return;
      this.draft = JSON.parse(JSON.stringify(r));
      this.draftIsNew = false;
      this.editorOpen = true;
      this.syncVehicleSelect();
    },
    closeEditor() { this.editorOpen = false; },
    setDraftType(t) {
      this.draft.tripType = t;
      if (t === "hourly") {
        this.draft.hours = this.draft.hours || 4;
        this.draft.hourlyRate = this.draft.hourlyRate || 295;
        this.draft.minHours = this.draft.minHours || 4;
      } else { this.draft.baseRate = this.draft.baseRate || 0; }
    },
    addStop() { this.draft.stops.splice(this.draft.stops.length - 1, 0, { address: "", note: "" }); },
    removeStop(i) { if (this.draft.stops.length > 2) this.draft.stops.splice(i, 1); },
    stopLabel(i, len) { return i === 0 ? "Pickup" : i === len - 1 ? "Drop-off" : "Stop " + i; },
    billedHours(r) { return Math.max(r.hours || 0, r.minHours || 0); },
    minApplied(r) { return r.tripType === "hourly" && (r.hours || 0) < (r.minHours || 0); },
    resTotal(r) {
      return r.tripType === "hourly" ? this.billedHours(r) * (r.hourlyRate || 0) : (r.baseRate || 0);
    },
    saveReservation() {
      const d = JSON.parse(JSON.stringify(this.draft));
      d.lead_id = this.leadId;
      if (this.draftIsNew) delete d.id;
      fetch(this.saveUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": getCookie("csrftoken") },
        body: JSON.stringify(d),
      }).then((r) => {
        if (r.redirected) window.location = r.url;
        else if (r.ok) window.location.reload();
        else Alpine.store("toast").push({ type: "danger", title: "Could not save reservation" });
      }).catch(() => Alpine.store("toast").push({ type: "danger", title: "Network error — could not save" }));
    },
  };
}
window.quoteWorkspace = quoteWorkspace;

/* -------------------------------------------------- inbox */
function inbox(opts = {}) {
  return {
    convoId: opts.selectedId || null,
    channel: "sms",
    body: "",
    sending: false,

    send() {
      const text = this.body.trim();
      if (!text || this.sending) return;
      const form = document.getElementById("composer-form");
      const url = form.action;
      this.sending = true;

      // optimistic append
      const thread = document.getElementById("thread-messages");
      let bubble = null;
      if (thread) {
        bubble = document.createElement("div");
        bubble.className = "flex flex-col items-end";
        bubble.innerHTML =
          '<div class="max-w-[78%] rounded-2xl px-3.5 py-2 text-[12.5px] leading-snug bg-charcoal text-slate-100 rounded-br-sm">' +
          escapeHtml(text) + "</div>";
        thread.appendChild(bubble);
        thread.scrollTop = thread.scrollHeight;
      }

      fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": getCookie("csrftoken") },
        body: JSON.stringify({ body: text, channel: this.channel }),
      })
        .then(async (r) => {
          const data = await r.json().catch(() => ({}));
          if (!data.ok) {
            if (bubble) bubble.remove();
            Alpine.store("toast").push({
              type: "danger", title: "Couldn't send message",
              message: data.error || "Please try again.",
            });
            return;
          }
          this.body = "";
        })
        .catch(() => {
          if (bubble) bubble.remove();
          Alpine.store("toast").push({ type: "danger", title: "Network error — could not send" });
        })
        .finally(() => { this.sending = false; });
    },
  };
}
window.inbox = inbox;

/* -------------------------------------------------- pipeline (kanban) */
function pipelineBoard() {
  return {
    dragId: null,
    dragFrom: null,
    dragOver: null,

    onDrag(id, from) {
      this.dragId = id;
      this.dragFrom = from;
    },

    onDrop(to) {
      const id = this.dragId;
      const from = this.dragFrom;
      this.dragId = this.dragFrom = null;
      if (!id || from === to) return;
      if (to === "lost" && (from === "new" || from === "quoted")) return this.markLost(id);
      if (to === "new" && from === "lost") return this.post(`/leads/${id}/reopen/`);
      const why = {
        quoted: "Quoted happens when a quote is sent from the workspace.",
        booked: "Booked happens when the deposit is paid.",
      }[to] || "Booked orders are cancelled from the Orders console.";
      Alpine.store("toast").push({ type: "info", title: "Can't move quote", message: why });
    },

    // Reuses the same $store.modal reason-prompt flow as the lead-detail "Mark lost" button.
    markLost(id) {
      Alpine.store("modal").show({
        title: "Mark lead as lost",
        variant: "danger",
        html:
          "<label class='block text-[12.5px] font-medium text-ink mb-1'>Reason (optional)</label>" +
          "<input type='text' id='pipeline-lost-reason' class='w-full border border-line rounded-lg px-3 py-2 text-[13px] focus:outline-none focus:ring-2 focus:ring-rose-300' placeholder='e.g. Booked elsewhere'>",
        confirmText: "Mark lost",
        showCancel: true,
        cancelText: "Cancel",
        onConfirm: () => {
          const el = document.getElementById("pipeline-lost-reason");
          return this.post(`/leads/${id}/mark-lost/`, { reason: el ? el.value : "" });
        },
      });
    },

    async post(url, data = {}) {
      const resp = await fetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded",
          "X-CSRFToken": getCookie("csrftoken"),
          Accept: "application/json",
        },
        body: new URLSearchParams(data),
      });
      const json = await resp.json().catch(() => ({}));
      if (resp.ok && json.ok) {
        window.location.reload();
        return;
      }
      Alpine.store("toast").push({
        type: "danger", title: "Could not update", message: json.error || "Please try again.",
      });
    },
  };
}
window.pipelineBoard = pipelineBoard;

/* -------------------------------------------------- Tom Select auto-init */
function initTomSelects(root = document) {
  if (typeof TomSelect === "undefined") return;
  root.querySelectorAll("select[data-tom]").forEach((el) => {
    if (el.tomselect) return; // already enhanced
    new TomSelect(el, {
      allowEmptyOption: true,
      create: false,
      placeholder: el.dataset.placeholder || "Select…",
      maxOptions: 1000,
      hidePlaceholder: false,
      controlInput: el.dataset.search === "off" ? null : undefined,
      onChange() {
        if (el.dataset.autosubmit !== undefined && el.form) el.form.submit();
      },
    });
  });
}
document.addEventListener("DOMContentLoaded", () => initTomSelects());
window.initTomSelects = initTomSelects;
