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
    acUrl: opts.acUrl,
    reservations: opts.reservations || [],
    vehicles: opts.vehicles || [],
    header: opts.header || {},
    depositPct: 50,
    sending: false,
    editorOpen: false,
    draftIsNew: false,
    draft: null,
    _saved: null,

    onPhoneBlur(e) {
      const el = e.target;
      if (!phoneIsValid(el)) {
        el.classList.add("field-error");
        Alpine.store("toast").push({
          type: "danger",
          title: "Invalid phone number",
          message: "Check the number for the country you selected.",
        });
        return;
      }
      el.classList.remove("field-error");
      this.header.phone = phoneValue(el);
      this.saveHeader();
    },

    saveHeader() {
      const changed = {};
      for (const [key, value] of Object.entries(this.header)) {
        if (!this._saved || this._saved[key] !== value) changed[key] = value;
      }
      if (Object.keys(changed).length === 0) return;

      const body = new URLSearchParams(changed);
      fetch(this.updateUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded",
          "X-CSRFToken": getCookie("csrftoken"),
        },
        body,
      }).then((r) => {
        if (r.ok) {
          this._saved = { ...this.header };
          Alpine.store("toast").push({ type: "success", title: "Saved" });
          return;
        }
        r.json()
          .then((d) =>
            Alpine.store("toast").push({
              type: "danger",
              title: "Could not save",
              message: d.error || "",
            }),
          )
          .catch(() =>
            Alpine.store("toast").push({ type: "danger", title: "Could not save" }),
          );
      }).catch(() =>
        Alpine.store("toast").push({ type: "danger", title: "Network error — could not save" }),
      );
    },

    // Channel picker — never window.confirm. Options reflect whichever contact fields are
    // currently on file (booleans only; no contact PII is interpolated into the html string).
    openSendQuoteModal() {
      if (this.sending) return;
      const hasEmail = !!(this.header.email && this.header.email.trim());
      const hasPhone = !!(this.header.phone && this.header.phone.trim());
      const row = (id, label, available) =>
        '<label class="flex items-center gap-2 py-1' + (available ? "" : " opacity-50") + '">' +
        '<input type="checkbox" id="' + id + '"' + (available ? " checked" : " disabled") + '> ' +
        label + (available ? "" : ' <span class="text-[11px] text-muted">(none on file)</span>') +
        "</label>";
      Alpine.store("modal").show({
        title: "Send quote",
        variant: "gold",
        html:
          '<p class="text-[13px] text-muted mb-2">Choose how to send this quote to the customer.</p>' +
          row("sq-channel-email", "Email", hasEmail) +
          row("sq-channel-sms", "Text message", hasPhone),
        confirmText: "Send",
        showCancel: true,
        cancelText: "Cancel",
        onConfirm: () => {
          const channels = [];
          const emailEl = document.getElementById("sq-channel-email");
          const smsEl = document.getElementById("sq-channel-sms");
          if (emailEl && emailEl.checked) channels.push("email");
          if (smsEl && smsEl.checked) channels.push("sms");
          if (!channels.length) {
            Alpine.store("toast").push({ type: "danger", title: "Choose at least one channel" });
            return;
          }
          return this.sendQuote(channels);
        },
      });
    },

    sendQuote(channels) {
      if (this.sending) return;
      channels = channels && channels.length ? channels : ["email", "sms"];
      this.sending = true;
      const body = new URLSearchParams();
      channels.forEach((c) => body.append("channels", c));
      return fetch(this.sendQuoteUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded",
          "X-CSRFToken": getCookie("csrftoken"),
        },
        body,
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
          const delivery = data.delivery || {};
          const attempted = Object.keys(delivery);
          const sent = attempted.filter((c) => delivery[c] && delivery[c].sent);
          const failed = attempted.filter((c) => !(delivery[c] && delivery[c].sent));

          if (failed.length === 0) {
            Alpine.store("toast").push({
              type: "success", title: "Quote sent",
              message: "Sent via " + sent.join(" and ") + ".",
            });
            setTimeout(() => window.location.reload(), 800);
            return;
          }

          const failLines = failed
            .map((c) => (
              "<p class='text-[12px] text-rose-600 mt-1'>" + escapeHtml(c) + ": " +
              escapeHtml((delivery[c] && delivery[c].error) || "delivery failed") + "</p>"
            ))
            .join("");
          // This runs inside the promise chain that openSendQuoteModal's onConfirm returns,
          // which the modal store's accept() awaits before calling close(). Opening a second
          // modal here directly would be immediately clobbered by that close() — schedule it
          // for the next macrotask so it lands after accept() is done closing the first one.
          const showFailureModal = () => Alpine.store("modal").show({
            title: sent.length ? "Quote saved — partially delivered" : "Quote saved — but not delivered",
            variant: "danger",
            html:
              "<p class='text-[13px] text-muted'>The deposit link is ready" +
              (sent.length ? ", and sent via " + escapeHtml(sent.join(" and ")) + ", but " : ", but ") +
              "the following didn't go out:</p>" + failLines +
              "<p class='text-[12px] text-muted mt-3'>Copy the link to send manually:</p>" +
              "<p class='text-[12px] text-ink break-all mt-1'>" + escapeHtml(data.link) + "</p>",
            confirmText: "Copy link", cancelText: "Close",
            onConfirm: () => {
              if (navigator.clipboard) navigator.clipboard.writeText(data.link || "");
              window.location.reload();
            },
            onCancel: () => window.location.reload(),
          });
          setTimeout(showFailureModal, 0);
        })
        .catch(() =>
          Alpine.store("toast").push({ type: "danger", title: "Network error — could not send quote" })
        )
        .finally(() => { this.sending = false; });
    },

    blankReservation() {
      const v = this.vehicles.length ? this.vehicles[0] : null;
      return {
        tripType: "transfer", service: "", date: "", time: "",
        dropoffDate: "", dropoffTime: "",
        vehicle: v ? v.id : "",
        pax: 1,
        rate: v ? v.rate : 0, hours: 1, minHours: v ? v.transferMin : 0,
        gratuityPct: 0, gratuityFlat: 0,
        stops: [
          { address: "", note: "", name: "", time: "", lat: "", lng: "" },
          { address: "", note: "", name: "", time: "", lat: "", lng: "" },
        ],
      };
    },
    money(n) { return "$" + Math.round(n || 0).toLocaleString(); },
    init() {
      this.draft = this.blankReservation();
      // Snapshot so saveHeader() posts only what changed; posting the whole
      // header would let one invalid stored phone 400 every other edit.
      this._saved = { ...this.header };
    },

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
    applyVehicleRateCard() {
      const v = this.vehicles.find((x) => String(x.id) === String(this.draft.vehicle));
      if (!v) return;
      this.draft.rate = v.rate;
      this.draft.minHours = this.draft.tripType === "hourly" ? v.hourlyMin : v.transferMin;
    },
    onHoursChanged() {              // hourly: derive drop-off from pickup + hours
      if (this.draft.tripType !== "hourly") return;
      if (!this.draft.date || !this.draft.time || !this.draft.hours) return;
      const start = new Date(this.draft.date + "T" + this.draft.time);
      const end = new Date(start.getTime() + Number(this.draft.hours) * 3600000);
      this.draft.dropoffDate = end.toISOString().slice(0, 10);
      this.draft.dropoffTime = end.toTimeString().slice(0, 5);
    },
    onDropoffChanged() {            // transfer: derive hours from drop-off − pickup
      if (this.draft.tripType !== "transfer") return;
      if (!this.draft.date || !this.draft.time || !this.draft.dropoffDate || !this.draft.dropoffTime) return;
      const start = new Date(this.draft.date + "T" + this.draft.time);
      const end = new Date(this.draft.dropoffDate + "T" + this.draft.dropoffTime);
      this.draft.hours = end > start ? Math.round((end - start) / 3600000 * 100) / 100 : 0;
    },
    setDraftType(t) {
      this.draft.tripType = t;
      this.applyVehicleRateCard();
      if (t === "hourly") { this.draft.hours = this.draft.hours || 4; this.onHoursChanged(); }
      else { this.onDropoffChanged(); }
    },
    addStop() { this.draft.stops.splice(this.draft.stops.length - 1, 0, { address: "", note: "", name: "", time: "", lat: "", lng: "" }); },
    removeStop(i) { if (this.draft.stops.length > 2) this.draft.stops.splice(i, 1); },
    stopLabel(i, len) { return i === 0 ? "Pickup" : i === len - 1 ? "Drop-off" : "Stop " + i; },

    /* ---- stop address autocomplete (airport-aware; mirrors bookingStops) ---- */
    _stopResults: {},
    stopRow(i) { return this._stopResults[i] || { open: false, list: [], active: -1 }; },
    searchStop(i) {
      const s = this.draft.stops[i];
      const q = (s.address || "").trim();
      if (!q) { this._stopResults[i] = { open: false, list: [], active: -1 }; return; }
      geocodeSearch(this.acUrl, q, null, null).then((rs) => {
        const list = rankByProximity(rs, null, null);
        this._stopResults[i] = { open: true, list, active: list.length ? 0 : -1 };
      });
    },
    chooseStop(i, j) {
      const row = this.stopRow(i); const r = row.list[j]; if (!r) return;
      const s = this.draft.stops[i];
      s.address = formatAddressLine(r);
      s.lat = r.latitude || ""; s.lng = r.longitude || "";
      this._stopResults[i] = { open: false, list: [], active: -1 };
    },
    closeStopRow(i) { this._stopResults[i] = { open: false, list: [], active: -1 }; },
    billedHours(r) { return Math.max(Number(r.hours) || 0, Number(r.minHours) || 0); },
    minApplied(r) { return (Number(r.hours) || 0) < (Number(r.minHours) || 0); },
    resSubtotal(r) { return (Number(r.rate) || 0) * this.billedHours(r); },
    resGratuity(r) {
      const flat = Number(r.gratuityFlat) || 0;
      if (flat > 0) return flat;
      return this.resSubtotal(r) * (Number(r.gratuityPct) || 0) / 100;
    },
    resTotal(r) { return this.resSubtotal(r) + this.resGratuity(r); },
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

/* -------------------------------------------------- contact profile */
// Same blur/change-autosave + field-diffing pattern as quoteWorkspace's header
// (only changed fields post to contact_update).
function contactProfile(opts = {}) {
  return {
    updateUrl: opts.updateUrl,
    header: opts.header || {},
    display: opts.display || {},
    _saved: null,

    onPhoneBlur(e) {
      const el = e.target;
      if (!phoneIsValid(el)) {
        el.classList.add("field-error");
        Alpine.store("toast").push({
          type: "danger",
          title: "Invalid phone number",
          message: "Check the number for the country you selected.",
        });
        return;
      }
      el.classList.remove("field-error");
      this.header.phone = phoneValue(el);
      this.display.phone = el.value;
      this.saveHeader();
    },

    saveHeader() {
      const changed = {};
      for (const [key, value] of Object.entries(this.header)) {
        if (!this._saved || this._saved[key] !== value) changed[key] = value;
      }
      if (Object.keys(changed).length === 0) return;

      const body = new URLSearchParams(changed);
      fetch(this.updateUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded",
          "X-CSRFToken": getCookie("csrftoken"),
        },
        body,
      }).then((r) => {
        if (r.ok) {
          this._saved = { ...this.header };
          Alpine.store("toast").push({ type: "success", title: "Saved" });
          return;
        }
        r.json()
          .then((d) =>
            Alpine.store("toast").push({
              type: "danger",
              title: "Could not save",
              message: d.error || "",
            }),
          )
          .catch(() =>
            Alpine.store("toast").push({ type: "danger", title: "Could not save" }),
          );
      }).catch(() =>
        Alpine.store("toast").push({ type: "danger", title: "Network error — could not save" }),
      );
    },
  };
}
window.contactProfile = contactProfile;

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
        new: "Leads return to New only by reopening a lost lead.",
        quoted: "Quoted happens when a quote is sent from the workspace.",
        booked: "Booked happens when the deposit is paid.",
      }[to];
      const message =
        from === "booked" ? "Booked orders are cancelled from the Orders console." : why;
      Alpine.store("toast").push({ type: "info", title: "Can't move quote", message });
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
      create: el.dataset.create !== undefined,
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

/* ------------------------------------------------ intl-tel-input auto-init */
/* The WithUtils CDN bundle includes libphonenumber, so getNumber()/isValidNumber()
   work synchronously — no loadUtils, no dynamic import. */
function initPhoneInputs(root = document) {
  if (typeof window.intlTelInput === "undefined") return;
  root.querySelectorAll("input[data-phone]").forEach((el) => {
    if (el.iti) return; // already enhanced
    el.iti = window.intlTelInput(el, {
      initialCountry: "us", // always US — never geo-locate
      strictMode: true,
      fixDropdownWidth: false, // size the country list to its content, not the narrow input
      // Auto-fill a real, country-appropriate example number as the placeholder
      // (e.g. "(201) 555-0123") instead of a bare "###" mask, when the input has none.
      autoPlaceholder: "polite",
    });
  });
}

/** E.164 for a phone input, or the raw value if the widget never initialized. */
function phoneValue(el) {
  const raw = el.value.trim();
  if (!raw) return "";
  return el.iti ? el.iti.getNumber() || raw : raw;
}

/** False only when the widget is present AND says the non-empty number is invalid. */
function phoneIsValid(el) {
  if (!el.value.trim() || !el.iti) return true;
  return el.iti.isValidNumber() !== false;
}

// Plain (non-Alpine) forms — the New-lead modal posts normally. Rewrite the field
// to E.164 on submit, or block if it cannot be dialled.
document.addEventListener("submit", (e) => {
  const form = e.target;
  if (!form.querySelectorAll) return;
  form.querySelectorAll("input[data-phone]").forEach((el) => {
    if (!phoneIsValid(el)) {
      e.preventDefault();
      el.classList.add("field-error");
      Alpine.store("toast").push({
        type: "danger",
        title: "Invalid phone number",
        message: "Check the number for the country you selected.",
      });
      return;
    }
    el.classList.remove("field-error");
    el.value = phoneValue(el);
  });
});

document.addEventListener("DOMContentLoaded", () => initPhoneInputs());
window.initPhoneInputs = initPhoneInputs;

/* -------------------------------------------------------- flatpickr auto-init */
function initFlatpickr(root = document) {
  if (typeof window.flatpickr === "undefined") return;
  root.querySelectorAll("input[data-flatpickr]").forEach((el) => {
    if (el._flatpickr) return;
    window.flatpickr(el, { altInput: true, altFormat: "F j, Y", dateFormat: "Y-m-d", minDate: "today" });
  });
  root.querySelectorAll("input[data-flatpickr-time]").forEach((el) => {
    if (el._flatpickr) return;
    window.flatpickr(el, { enableTime: true, noCalendar: true, dateFormat: "H:i", altInput: true, altFormat: "h:i K", time_24hr: false });
  });
}
document.addEventListener("DOMContentLoaded", () => initFlatpickr());
window.initFlatpickr = initFlatpickr;

/* -------------------------------------------------- image upload (settings)
 * A styled dropzone layered over a real <input type="file"> — the input still
 * carries the value and submits normally; this only adds preview + drag/drop.
 * Preview mirrors the customer-facing vehicle card: the photo sits contained on
 * a tile, so the admin sees exactly what the customer will.
 */
function imageUpload(existingUrl) {
  return {
    existing: existingUrl || "",
    preview: existingUrl || "",
    filename: "",
    picked: false, // a new file was chosen this session
    dragging: false,
    _blobUrl: "", // object URL we own and must revoke

    init() {
      this.input = this.$root.querySelector('input[type="file"]');
      if (this.input) this.input.addEventListener("change", () => this.onChange());
    },

    onDrop(e) {
      this.dragging = false;
      const files = e.dataTransfer && e.dataTransfer.files;
      if (!files || !files.length || !this.input) return;
      // Hand the dropped file to the real input so the form submits it unchanged.
      this.input.files = files;
      this.onChange();
    },

    onChange() {
      const file = this.input.files && this.input.files[0];
      this._releaseBlob();
      if (!file) {
        this.revert();
        return;
      }
      this._blobUrl = URL.createObjectURL(file);
      this.preview = this._blobUrl;
      this.filename = file.name;
      this.picked = true;
    },

    clear() {
      if (this.input) this.input.value = "";
      this._releaseBlob();
      this.revert();
    },

    revert() {
      this.preview = this.existing;
      this.filename = "";
      this.picked = false;
    },

    _releaseBlob() {
      if (this._blobUrl) {
        URL.revokeObjectURL(this._blobUrl);
        this._blobUrl = "";
      }
    },
  };
}
window.imageUpload = imageUpload;

/* ------------------------------------------------ smart-address (reusable) */
/* Address ranking — all tunable constants here, no magic numbers in the scorer. */
// score = upstream index (relevance) + a continuous per-mile distance penalty, so the
// closest match wins at ANY distance while a strong upstream #0 still surfaces. Coordless
// results get no distance term (keep their upstream rank). Tune DIST_WEIGHT up to favor local more.
const SMART_ADDRESS_RANKING = { INDEX_WEIGHT: 1.0, DIST_WEIGHT: 0.003, TOP_N: 8 };

function haversineMiles(a, b) {
  if (a.lat == null || a.lon == null || b.lat == null || b.lon == null || Number.isNaN(b.lat) || Number.isNaN(b.lon)) {
    return Infinity;
  }
  const R = 3958.8, rad = (d) => (d * Math.PI) / 180;
  const dLat = rad(b.lat - a.lat), dLon = rad(b.lon - a.lon);
  const s = Math.sin(dLat / 2) ** 2 + Math.cos(rad(a.lat)) * Math.cos(rad(b.lat)) * Math.sin(dLon / 2) ** 2;
  return R * 2 * Math.atan2(Math.sqrt(s), Math.sqrt(1 - s));
}

function smartAddress(opts = {}) {
  return {
    updateUrl: opts.updateUrl,
    acUrl: opts.acUrl,
    fields: opts.fields || {},
    query: "",
    results: [],
    open: false,
    loading: false,
    active: -1,
    _raw: [],
    biasLat: opts.fallbackLat ?? null,
    biasLon: opts.fallbackLon ?? null,
    _locRequested: false,
    _saved: null,

    init() {
      this._saved = JSON.stringify(this.fields);
      this.$nextTick(() => this.syncStateSelect());
    },

    isUS(c) {
      return ["US", "United States", "United States of America"].includes((c || "").trim());
    },

    // Reflect fields.state in the (Tom Select) state dropdown — scoped to THIS component's root so
    // primary/billing don't collide. create=1 means a value not in the list is accepted.
    // Tom Select's setValue() only shows a value that is already a registered option — it never
    // creates one on its own (createItem() only runs from interactive click/enter). Legacy
    // addresses may carry a full state name (e.g. "Virginia") that isn't in us_states, so register
    // it as a synthetic option first or the widget silently blanks instead of showing it.
    syncStateSelect() {
      const el = this.$root && this.$root.querySelector("select[data-tom]");
      if (!el || !el.tomselect) return;
      const ts = el.tomselect;
      const v = this.fields.state;
      if (!v) {
        ts.clear(true); // a later pick with no state must not leave the prior state showing
        return;
      }
      if (!ts.options[v]) ts.addOption({ value: v, text: v });
      ts.setValue(v, true);
    },

    // ---- read-only display (sa-view block) ----
    hasAddress() {
      const f = this.fields;
      return !!(f.landmark_name || f.line1 || f.line2 || f.city || f.state || f.postal);
    },
    streetLine() {
      return [this.fields.line1, this.fields.line2].filter(Boolean).join(", ");
    },
    cityLine() {
      const f = this.fields;
      const statePostal = [f.state, f.postal].filter(Boolean).join(" ");
      return [f.city, statePostal].filter(Boolean).join(", ");
    },

    requestLocation() {
      if (this._locRequested || !("geolocation" in navigator)) return;
      this._locRequested = true;
      navigator.geolocation.getCurrentPosition(
        (pos) => {
          this.biasLat = pos.coords.latitude;
          this.biasLon = pos.coords.longitude;
          if (this._raw.length) { this.results = this.rank(this._raw); this.active = this.results.length ? 0 : -1; }
        },
        () => {}, // denied / unavailable → keep the configured fallback center
        { timeout: 8000, maximumAge: 600000 },
      );
    },

    // Airports are held out of the proximity sort and always lead — see partitionAirports.
    rank(results) {
      const cfg = SMART_ADDRESS_RANKING;
      const [airports, rest] = partitionAirports(results);
      const center = { lat: this.biasLat, lon: this.biasLon };
      const ranked = rest
        .map((r, i) => {
          const d = haversineMiles(center, { lat: parseFloat(r.latitude), lon: parseFloat(r.longitude) });
          const distTerm = Number.isFinite(d) ? d * cfg.DIST_WEIGHT : 0;
          return { r, s: i * cfg.INDEX_WEIGHT + distTerm };
        })
        .sort((a, b) => a.s - b.s)
        .map((x) => x.r);
      return airports.concat(ranked).slice(0, cfg.TOP_N);
    },

    search() {
      const q = this.query.trim();
      if (!q) { this.results = []; this._raw = []; this.open = false; return; }
      this.loading = true; this.open = true;
      let url = `${this.acUrl}?q=${encodeURIComponent(q)}`;
      if (this.biasLat != null && this.biasLon != null) url += `&lat=${this.biasLat}&lon=${this.biasLon}`;
      fetch(url, { headers: { "X-Requested-With": "fetch" } })
        .then((r) => r.json())
        .then((d) => { this._raw = d.results || []; this.results = this.rank(this._raw); this.active = this.results.length ? 0 : -1; })
        .catch(() => { this.results = []; this._raw = []; })
        .finally(() => { this.loading = false; });
    },
    move(delta) {
      if (!this.results.length) return;
      this.active = (this.active + delta + this.results.length) % this.results.length;
    },
    choose(i) {
      const r = this.results[i]; if (!r) return;
      // Populate the bound fields from the picked result; the search box value is discarded.
      for (const k of Object.keys(this.fields)) if (k in r) this.fields[k] = r[k] ?? "";
      this.syncStateSelect();
      this.query = ""; this.closeResults();
      this.saveAll();
    },
    closeResults() { this.open = false; this.results = []; this.active = -1; },

    save(/* field */) { this.saveAll(); },  // per-field blur → persist the whole group (simple + safe)
    saveAll() {
      const snapshot = JSON.stringify(this.fields);
      if (snapshot === this._saved) return;
      const body = new URLSearchParams();
      for (const [k, v] of Object.entries(this.fields)) body.append(k, v ?? "");
      fetch(this.updateUrl, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded", "X-CSRFToken": getCookie("csrftoken") },
        body,
      }).then((r) => {
        if (r.ok) { this._saved = snapshot; Alpine.store("toast").push({ type: "success", title: "Address saved" }); return; }
        Alpine.store("toast").push({ type: "danger", title: "Could not save address" });
      }).catch(() => Alpine.store("toast").push({ type: "danger", title: "Network error — address not saved" }));
    },
  };
}

/* ---------------------------------------------- public booking autocomplete */
/* Shared low-level fetch against the PUBLIC geocode proxy — no auth, no auto-save. */
function geocodeSearch(acUrl, q, biasLat, biasLon) {
  q = (q || "").trim();
  if (!q) return Promise.resolve([]);
  let url = `${acUrl}?q=${encodeURIComponent(q)}`;
  if (biasLat != null && biasLon != null) url += `&lat=${biasLat}&lon=${biasLon}`;
  return fetch(url, { headers: { "X-Requested-With": "fetch" } })
    .then((r) => r.json())
    .then((d) => d.results || [])
    .catch(() => []);
}
window.geocodeSearch = geocodeSearch;

/* One-line label from a decomposed LocationIQ result (see geocoding._decompose). */
function formatAddressLine(r) {
  const head = r.landmark_name || r.line1 || "";
  const cityState = [r.city, [r.state, r.postal].filter(Boolean).join(" ")].filter(Boolean).join(", ");
  const line = [head, cityState].filter(Boolean).join(", ");
  return line || r.display_name || "";
}
window.formatAddressLine = formatAddressLine;

/* Split airport results (server-ranked, must stay on top) from the rest. The proximity
   sort below would otherwise sink an airport beneath any closer street address — and the
   TOP_N truncation could drop it entirely. */
function partitionAirports(results) {
  const airports = [], rest = [];
  for (const r of results) (r.is_airport ? airports : rest).push(r);
  return [airports, rest];
}

/* Reorder autocomplete results by proximity to a bias center (mirrors smartAddress.rank).
   With no center (geolocation denied and no fallback) it preserves the server order.
   Airports are held out of the sort and always lead. */
function rankByProximity(results, lat, lon) {
  const cfg = SMART_ADDRESS_RANKING;
  const [airports, rest] = partitionAirports(results);
  if (lat == null || lon == null) return airports.concat(rest).slice(0, cfg.TOP_N);
  const center = { lat, lon };
  const ranked = rest
    .map((r, i) => {
      const d = haversineMiles(center, { lat: parseFloat(r.latitude), lon: parseFloat(r.longitude) });
      const distTerm = Number.isFinite(d) ? d * cfg.DIST_WEIGHT : 0;
      return { r, s: i * cfg.INDEX_WEIGHT + distTerm };
    })
    .sort((a, b) => a.s - b.s)
    .map((x) => x.r);
  return airports.concat(ranked).slice(0, cfg.TOP_N);
}

/* Ask the browser for location once; call onOk(lat, lon) if the user grants it.
   Denied/unavailable → no-op, so callers keep their configured fallback center. */
function requestBrowserLocation(onOk) {
  if (!("geolocation" in navigator)) return;
  navigator.geolocation.getCurrentPosition(
    (pos) => onOk(pos.coords.latitude, pos.coords.longitude),
    () => {},
    { timeout: 8000, maximumAge: 600000 },
  );
}

/* Single-line address input for the public booking form (pickup / drop-off). */
function addressAutocomplete(opts = {}) {
  return {
    acUrl: opts.acUrl,
    value: opts.value || "",
    lat: opts.lat || "",
    lng: opts.lng || "",
    display: opts.display || "",
    results: [],
    open: false,
    active: -1,
    loading: false,
    biasLat: opts.fallbackLat ?? null,
    biasLon: opts.fallbackLon ?? null,
    _raw: [],
    _locRequested: false,
    // On first focus, ask the browser for the visitor's location; on grant, re-rank
    // any results already showing. Denied → keep the service-area fallback center.
    requestLocation() {
      if (this._locRequested) return;
      this._locRequested = true;
      requestBrowserLocation((lat, lon) => {
        this.biasLat = lat; this.biasLon = lon;
        if (this._raw.length) {
          this.results = rankByProximity(this._raw, this.biasLat, this.biasLon);
          this.active = this.results.length ? 0 : -1;
        }
      });
    },
    search() {
      const q = this.value.trim();
      if (!q) { this.results = []; this._raw = []; this.open = false; return; }
      this.loading = true; this.open = true;
      geocodeSearch(this.acUrl, q, this.biasLat, this.biasLon).then((rs) => {
        this._raw = rs;
        this.results = rankByProximity(rs, this.biasLat, this.biasLon);
        this.active = this.results.length ? 0 : -1;
      }).finally(() => { this.loading = false; });
    },
    move(d) {
      if (!this.results.length) return;
      this.active = (this.active + d + this.results.length) % this.results.length;
    },
    choose(i) {
      const r = this.results[i]; if (!r) return;
      this.value = formatAddressLine(r);
      this.lat = r.latitude || ""; this.lng = r.longitude || ""; this.display = r.display_name || "";
      this.closeResults();
    },
    closeResults() { this.open = false; this.results = []; this.active = -1; },
  };
}
window.addressAutocomplete = addressAutocomplete;

/* Public quote form. Owns the trip-type toggle everywhere, and (when twoStep is set,
   i.e. the homepage hero) the two-step progressive disclosure. */
function quoteSteps(opts = {}) {
  return {
    twoStep: !!opts.twoStep,
    tripType: opts.tripType || "transfer",
  };
}
window.quoteSteps = quoteSteps;

/* Repeater for optional in-between stops; serializes to one hidden stops_json field. */
function bookingStops(opts = {}) {
  return {
    acUrl: opts.acUrl,
    stops: [],
    _r: {}, // per-row results cache keyed by index
    biasLat: opts.fallbackLat ?? null,
    biasLon: opts.fallbackLon ?? null,
    _locRequested: false,
    // Same location bias as the pickup/drop-off inputs: ask once on focus, fall back
    // to the service-area center when denied. The next debounced search picks it up.
    requestLocation() {
      if (this._locRequested) return;
      this._locRequested = true;
      requestBrowserLocation((lat, lon) => { this.biasLat = lat; this.biasLon = lon; });
    },
    add() { this.stops.push({ address: "", lat: "", lng: "", display: "" }); },
    remove(i) { this.stops.splice(i, 1); },
    search(i) {
      const s = this.stops[i];
      const q = (s.address || "").trim();
      if (!q) { this._r[i] = { open: false, list: [], active: -1 }; return; }
      geocodeSearch(this.acUrl, q, this.biasLat, this.biasLon).then((rs) => {
        const list = rankByProximity(rs, this.biasLat, this.biasLon);
        this._r[i] = { open: true, list, active: list.length ? 0 : -1 };
      });
    },
    rowState(i) { return this._r[i] || { open: false, list: [], active: -1 }; },
    choose(i, j) {
      const rs = this.rowState(i); const r = rs.list[j]; if (!r) return;
      const s = this.stops[i];
      s.address = formatAddressLine(r);
      s.lat = r.latitude || ""; s.lng = r.longitude || ""; s.display = r.display_name || "";
      this._r[i] = { open: false, list: [], active: -1 };
    },
    closeRow(i) { this._r[i] = { open: false, list: [], active: -1 }; },
    json() {
      return JSON.stringify(
        this.stops
          .filter((s) => (s.address || "").trim())
          .map((s) => ({ address: s.address, lat: s.lat || null, lng: s.lng || null, display: s.display })),
      );
    },
  };
}
window.bookingStops = bookingStops;

window.smartAddress = smartAddress;
