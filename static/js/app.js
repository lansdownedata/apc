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

/* -------------------------------------------------- quoteWorkspace */
function quoteWorkspace(opts = {}) {
  return {
    leadId: opts.leadId,
    updateUrl: opts.updateUrl,
    saveUrl: opts.saveUrl,
    reservations: opts.reservations || [],
    vehicles: opts.vehicles || [],
    header: opts.header || {},
    depositPct: 50,
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
