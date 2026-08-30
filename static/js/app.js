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

/* Right-side drawer — fetches an HTML fragment and shows it beside the page. */
function drawer() {
  return {
    open: false,
    body: "",
    async load(url) {
      this.body = '<p class="text-muted text-[13px]">Loading…</p>';
      this.open = true;
      try {
        const resp = await fetch(url, { headers: { "X-Requested-With": "XMLHttpRequest" } });
        if (!resp.ok) throw new Error(resp.status);
        this.body = await resp.text();
      } catch (e) {
        this.body = "";
        this.open = false;
        Alpine.store("toast").push({ type: "danger", title: "Could not open that trip" });
      }
    },
    close() {
      this.open = false;
      this.body = "";
    },
  };
}
window.drawer = drawer;

/* Assign drawer — posts the offer form and the resolve actions, then refreshes the board. */
function assignPanel() {
  return {
    busy: false,
    async send(url, extra) {
      this.busy = true;
      // $el/closest, not $root/querySelector: the offer button sits inside the form's own
      // nested x-data scope, so $root here is the <form> itself and querySelector("form")
      // finds nothing — the POST goes out empty. The resolve buttons have no enclosing
      // form and fall through to an empty body, which is all they need (action via extra).
      const form = new FormData(this.$el.closest("form") || undefined);
      Object.entries(extra || {}).forEach(([k, v]) => form.set(k, v));
      let data;
      try {
        const resp = await fetch(url, {
          method: "POST",
          body: form,
          headers: { "X-CSRFToken": getCookie("csrftoken") },
        });
        data = await resp.json();
      } catch (e) {
        data = { ok: false, error: "Network error — nothing was saved" };
      }
      this.busy = false;
      if (data.ok) window.location.reload();
      else Alpine.store("toast").push({ type: "danger", title: data.error || "Could not save" });
    },
    post(url) {
      return this.send(url, {});
    },
    resolve(url, action) {
      return this.send(url, { action });
    },
    confirmWithdraw(url, copy = {}) {
      Alpine.store("modal").confirm({
        title: copy.title || "Withdraw this assignment?",
        message:
          copy.message ||
          "The trip goes back to unassigned. The affiliate is not notified automatically.",
        variant: "danger",
        confirmText: copy.confirmText || "Withdraw",
        onConfirm: () => this.send(url, { action: "withdraw" }),
      });
    },
  };
}
window.assignPanel = assignPanel;

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

async function postForm(url, data = {}) {
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
  if (!resp.ok || json.ok === false) {
    throw new Error(json.error || "Request failed.");
  }
  return json;
}

/* -------------------------------------------------- staff card payment (Stripe Payment Element) */
function adminCardPay(opts) {
  return {
    stripe: null,
    elements: null,
    paymentElement: null,
    amount: opts.remaining || "",
    remaining: opts.remaining || "0.00",
    hasCard: !!opts.hasCard,
    cardBrand: opts.cardBrand || "",
    cardLast4: opts.cardLast4 || "",
    replacing: false,
    busy: false,
    error: "",
    mounted: false,

    init() {
      if (!opts.pk || typeof Stripe === "undefined") return;
      this.stripe = Stripe(opts.pk);
      if (!this.hasCard) this.mountElement();
    },

    cents() {
      const n = Math.round(parseFloat(this.amount || "0") * 100);
      return Number.isFinite(n) ? n : 0;
    },

    mountElement() {
      if (!this.stripe || this.mounted) return;
      const amount = Math.max(this.cents(), 50);
      this.elements = this.stripe.elements({
        mode: "payment",
        amount,
        currency: "usd",
        setupFutureUsage: "off_session",
        appearance: {
          theme: "stripe",
          variables: {
            colorPrimary: "#C7A24E",
            colorText: "#17191D",
            fontFamily: "Inter, system-ui, sans-serif",
            borderRadius: "8px",
          },
        },
      });
      this.paymentElement = this.elements.create("payment");
      this.$nextTick(() => {
        if (this.$refs.cardMount) this.paymentElement.mount(this.$refs.cardMount);
      });
      this.mounted = true;
    },

    onAmountChange() {
      if (this.elements && this.cents() >= 50) {
        this.elements.update({ amount: this.cents() });
      }
    },

    startReplace() {
      this.replacing = true;
      this.$nextTick(() => this.mountElement());
    },

    parseAmount() {
      const value = parseFloat(this.amount);
      if (!Number.isFinite(value) || value <= 0) {
        this.error = "Enter an amount greater than zero.";
        return null;
      }
      return this.amount;
    },

    async charge() {
      this.error = "";
      const amount = this.parseAmount();
      if (amount == null) return;
      this.busy = true;
      try {
        if (this.hasCard && !this.replacing) {
          await postForm(opts.chargeSavedUrl, { amount });
          window.location.reload();
          return;
        }
        if (!this.elements) throw new Error("Card field is not ready.");
        const { error: submitError } = await this.elements.submit();
        if (submitError) throw new Error(submitError.message);
        const created = await postForm(opts.intentUrl, { amount });
        const { error, paymentIntent } = await this.stripe.confirmPayment({
          elements: this.elements,
          clientSecret: created.client_secret,
          confirmParams: { return_url: window.location.href },
          redirect: "if_required",
        });
        if (error) throw new Error(error.message);
        await postForm(opts.completeUrl, { payment_intent_id: paymentIntent.id });
        window.location.reload();
      } catch (err) {
        this.error = err.message || "Could not charge the card.";
      } finally {
        this.busy = false;
      }
    },

    async saveCard() {
      this.error = "";
      if (!this.elements) {
        this.error = "Card field is not ready.";
        return;
      }
      this.busy = true;
      try {
        const { error: submitError } = await this.elements.submit();
        if (submitError) throw new Error(submitError.message);
        const { error, paymentMethod } = await this.stripe.createPaymentMethod({
          elements: this.elements,
        });
        if (error) throw new Error(error.message);
        const saved = await postForm(opts.saveCardUrl, { payment_method_id: paymentMethod.id });
        this.hasCard = true;
        this.replacing = false;
        this.cardBrand = saved.card_brand || this.cardBrand;
        this.cardLast4 = saved.card_last4 || this.cardLast4;
        Alpine.store("toast").push({
          type: "success",
          title: "Card saved",
          message: "The card is on file for this quote.",
        });
      } catch (err) {
        this.error = err.message || "Could not save the card.";
      } finally {
        this.busy = false;
      }
    },
  };
}
window.adminCardPay = adminCardPay;

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
    // The seeded Private/tail-number carrier's pk (2026-08-29 §3) — lets the Verify gate
    // recognise it client-side the same way `Stop.verify_available` does server-side.
    privateAirlineId: opts.privateAirlineId ?? null,
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
        rate: v ? v.rate : 0, hours: "", minHours: v ? v.transferMin : 0,
        gratuityPct: 0, gratuityFlat: 0,
        stops: [
          { address: "", note: "", name: "", time: "", lat: "", lng: "", airport: "", airportCode: "", hasScheduledService: false, airline: "", flight: "", direction: "", verify: null, verifying: false },
          { address: "", note: "", name: "", time: "", lat: "", lng: "", airport: "", airportCode: "", hasScheduledService: false, airline: "", flight: "", direction: "", verify: null, verifying: false },
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
      if (!(Number(this.draft.hours) > 0)) this.draft.hours = "";  // a stored 0 is "no override" — show an empty box, not "0"
      // A stop already linked to a cached flight opens with its pill shown. `pill` comes
      // from the server draft; `verify` is client-only and keyed so any edit reverts it.
      this.draft.stops.forEach((s, i) => {
        s.verify = s.pill ? { key: this.flightKey(i), pill: s.pill } : null;
        s.verifying = false;
        delete s.pill;
      });
      this.draftIsNew = false;
      this.editorOpen = true;
      // Older rows can predate the derived drop-off — fill it so the hourly
      // read-out shows the end time instead of the "set a date" prompt.
      this.onHoursChanged();
      this.syncVehicleSelect();
    },
    closeEditor() { this.editorOpen = false; },
    applyVehicleRateCard() {
      const v = this.vehicles.find((x) => String(x.id) === String(this.draft.vehicle));
      // No (active) vehicle → no rate card → no minimum. Min hours is read-only, so a
      // stale number here would be billed with no way to correct it.
      this.draft.minHours = v ? (this.draft.tripType === "hourly" ? v.hourlyMin : v.transferMin) : 0;
      if (v) this.draft.rate = v.rate;
      this.onHoursChanged();   // the minimum is what's billed until overridden → end time moves
    },
    onHoursChanged() {              // hourly: derive drop-off from pickup + billed hours
      if (this.draft.tripType !== "hourly") return;
      const hours = this.billedHours(this.draft);
      if (!this.draft.date || !this.draft.time || !hours) return;
      const start = new Date(this.draft.date + "T" + this.draft.time);
      const end = new Date(start.getTime() + hours * 3600000);
      this.draft.dropoffDate = localDate(end);  // not toISOString(): that is the UTC date
      this.draft.dropoffTime = end.toTimeString().slice(0, 5);
    },
    setDraftType(t) {
      this.draft.tripType = t;
      this.applyVehicleRateCard();
    },
    addStop() { this.draft.stops.splice(this.draft.stops.length - 1, 0, { address: "", note: "", name: "", time: "", lat: "", lng: "", airport: "", airportCode: "", hasScheduledService: false, airline: "", flight: "", direction: "", verify: null, verifying: false }); },
    removeStop(i) { if (this.draft.stops.length > 2) this.draft.stops.splice(i, 1); },
    stopLabel(i, len) { return i === 0 ? "Pickup" : i === len - 1 ? "Drop-off" : "Stop " + i; },
    /* The first and last stop happen at the trip's own times — shown against the row
       instead of asking for them twice. The server mirrors them onto the stops. */
    stopTime(i, len) {
      if (i === 0) return time12(this.draft.time);
      if (i === len - 1) return time12(this.draft.dropoffTime);
      return "";
    },
    /** Hourly drop-off is derived from hours — read out rather than edited. */
    dropoffLabel() {
      const d = this.draft.dropoffDate, t = this.draft.dropoffTime;
      if (!d || !t) return "";
      const dt = new Date(d + "T" + t);
      if (isNaN(dt)) return "";
      const day = dt.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
      return day + " · " + time12(t);
    },

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
      // Picked from the airport directory → the flight row appears for this stop.
      s.airport = r.is_airport ? String(r.airport_id || "") : "";
      s.airportCode = r.is_airport ? (r.airport_code || "") : "";
      // Gates the Verify button (spec 2026-08-29 finding 2) — a real IATA code alone
      // isn't enough; Andrews/Manassas-style fields have one with no flights to look up.
      s.hasScheduledService = r.is_airport ? !!r.has_scheduled_service : false;
      this._stopResults[i] = { open: false, list: [], active: -1 };
    },
    closeStopRow(i) { this._stopResults[i] = { open: false, list: [], active: -1 }; },

    /* ---- flight verification (spec 2026-08-29 §7.3) ---- */
    /* The ends are fixed by position — a pickup meets an arrival, a drop-off catches a
       departure; a middle stop is whatever the user toggled. Mirrors drafts.parse_draft. */
    stopDirection(i) {
      if (i === 0) return "arrival";
      if (i === this.draft.stops.length - 1) return "departure";
      return this.draft.stops[i].direction || "";
    },
    flightKey(i) {
      const s = this.draft.stops[i];
      return [s.airport, s.airline, s.flight, this.stopDirection(i), this.draft.date].join("|");
    },
    /* The pill shown for a row — only while every value it was checked against is unchanged. */
    verifyPill(i) {
      const v = this.draft.stops[i].verify;
      return v && v.key === this.flightKey(i) ? v.pill : null;
    },
    /* A tail number (the seeded Private carrier) has no scheduled flight number for
       aviationstack to look up — Verify must not be offered (2026-08-29 §3), mirroring
       `Stop.verify_available` server-side. */
    isPrivateAirline(i) {
      const s = this.draft.stops[i];
      return this.privateAirlineId != null && String(s.airline || "") === String(this.privateAirlineId);
    },
    canVerify(i) {
      const s = this.draft.stops[i];
      return !s.verifying
        && !this.isPrivateAirline(i)
        && !!(s.airport && s.hasScheduledService && s.airline && s.flight && this.stopDirection(i) && this.draft.date)
        && this.draft.date >= localDate(new Date());
    },
    verifyReason(i) {
      const s = this.draft.stops[i];
      if (!s.hasScheduledService) return "This airport has no scheduled passenger service";
      if (this.isPrivateAirline(i)) return "Private flights aren't in any airline's schedule to verify";
      if (!s.airline) return "Choose the airline first";
      if (!s.flight) return "Enter the flight number";
      if (!this.stopDirection(i)) return "Choose Arriving or Departing to verify";
      if (!this.draft.date) return "Set the trip date first";
      if (this.draft.date < localDate(new Date())) return "Trip date has passed";
      return "Check this flight with aviationstack";
    },
    async verifyStop(i) {
      if (!this.canVerify(i)) return;
      const s = this.draft.stops[i];
      const key = this.flightKey(i);
      s.verifying = true;
      const pill = await verifyFlight({
        airport: s.airport, airline: s.airline, flight: s.flight,
        direction: this.stopDirection(i), date: this.draft.date,
        time: (i === 0 ? this.draft.time : s.time) || this.draft.time || "",
      });
      s.verifying = false;
      if (pill) s.verify = { key, pill };
    },

    /* Override hours replace the rate-card minimum when set — even downward. */
    billedHours(r) {
      const override = Number(r.hours) || 0;
      return override > 0 ? override : (Number(r.minHours) || 0);
    },
    minApplied(r) { return (Number(r.hours) || 0) <= 0 && (Number(r.minHours) || 0) > 0; },
    noMinimum(r) { return (Number(r.hours) || 0) <= 0 && (Number(r.minHours) || 0) <= 0; },
    resSubtotal(r) { return (Number(r.rate) || 0) * this.billedHours(r); },
    resGratuity(r) {
      const flat = Number(r.gratuityFlat) || 0;
      if (flat > 0) return flat;
      return this.resSubtotal(r) * (Number(r.gratuityPct) || 0) / 100;
    },
    resTotal(r) { return this.resSubtotal(r) + this.resGratuity(r); },
    saveReservation() {
      const d = JSON.parse(JSON.stringify(this.draft));
      d.stops.forEach((s) => { delete s.verify; delete s.verifying; delete s.pill; });  // client-only
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
      if (to === "new" && from === "lost") return this.post(`/portal/leads/${id}/reopen/`);
      if (to === "booked" && (from === "quoted" || from === "new")) return this.markBooked(id);
      const why = {
        new: "Leads return to New only by reopening a lost lead.",
        quoted: "Quoted happens when a quote is sent from the workspace.",
        booked: "Booked happens when the deposit is paid or an admin books the lead.",
      }[to];
      const message =
        from === "booked" ? "Booked orders are cancelled from the Orders console." : why;
      Alpine.store("toast").push({ type: "info", title: "Can't move quote", message });
    },

    markBooked(id) {
      Alpine.store("modal").confirm({
        title: "Book this order without a payment?",
        variant: "gold",
        confirmText: "Book now",
        message:
          "The 50% deposit stays due — the customer can pay through the link any time, or you can take a card from the quote workspace.",
        onConfirm: () => this.post(`/portal/leads/${id}/mark-booked/`),
      });
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
          return this.post(`/portal/leads/${id}/mark-lost/`, { reason: el ? el.value : "" });
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
    const options = {
      allowEmptyOption: true,
      create: el.dataset.create !== undefined,
      placeholder: el.dataset.placeholder || "Select…",
      maxOptions: 1000,
      hidePlaceholder: false,
      onChange() {
        if (el.dataset.autosubmit !== undefined && el.form) el.form.submit();
      },
    };
    // Only SET controlInput when search is off. Tom Select tests this setting for
    // truthiness, so passing `undefined` to mean "use the default" disables the typing
    // input exactly as `null` does — which silently made every "searchable" select in
    // the app unsearchable. The key must be absent, not undefined.
    if (el.dataset.search === "off") options.controlInput = null;
    new TomSelect(el, options);
  });
}
document.addEventListener("DOMContentLoaded", () => { initTomSelects(); initAutogrow(); });

/* Textareas that grow with their content, capped so a long note can't push the
   rest of the form off screen. Opens at its `rows` height. */
function initAutogrow(root = document) {
  root.querySelectorAll("textarea[data-autogrow]").forEach((el) => {
    if (el._autogrow) return;
    el._autogrow = true;
    const line = parseFloat(getComputedStyle(el).lineHeight) || 20;
    const max = line * 8;
    const grow = () => {
      // A field with no layout (the hero hides step 2 behind x-show, and deferred
      // Alpine applies that before this runs) reports scrollHeight 0, so sizing it
      // pins the textarea to its padding and it never recovers.
      if (!el.offsetParent && !el.offsetHeight) return;
      el.style.height = "auto";
      el.style.height = Math.min(el.scrollHeight, max) + "px";
      el.style.overflowY = el.scrollHeight > max ? "auto" : "hidden";
    };
    el.addEventListener("input", grow);
    // Measure once the field is actually rendered — revealing a step resizes it from
    // zero, which is the only moment the deferred first measurement can happen.
    if (typeof ResizeObserver === "function") {
      const ro = new ResizeObserver(() => {
        if (!el.offsetParent && !el.offsetHeight) return;
        ro.disconnect();
        grow();
      });
      ro.observe(el);
    }
    grow();
  });
}
window.initAutogrow = initAutogrow;
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

/* -------------------------------------------------------- flatpickr auto-init
 * One options object for the whole app: the portal's pickers must read exactly
 * like the public booking widget's (theme lives in app.css, `.flatpickr-*`).
 * `data-fp-past` drops the no-past-dates rule — back-office trips are sometimes
 * entered, or corrected, after they ran.
 *
 * Escape inside a picker must not also close the dialog around it. flatpickr's own
 * Escape handler sits on document and runs before any window listener, so by the
 * time a modal sees the key its calendar is already shut and undetectable — stamp
 * the close instead, and let the modal ask whether one just happened. */
let fpClosedAt = 0;
const FP_BASE = { onClose: () => { fpClosedAt = Date.now(); } };
window.fpJustClosed = () => Date.now() - fpClosedAt < 250;

const FP_DATE = { ...FP_BASE, altInput: true, altFormat: "F j, Y", dateFormat: "Y-m-d", minDate: "today" };
const FP_TIME = { ...FP_BASE, enableTime: true, noCalendar: true, dateFormat: "H:i", altInput: true, altFormat: "h:i K", time_24hr: false };

/** Matches inside `root` plus `root` itself, so x-for rows can pass their own input. */
function fpTargets(root, selector) {
  const found = Array.from(root.querySelectorAll ? root.querySelectorAll(selector) : []);
  if (root.matches && root.matches(selector)) found.unshift(root);
  return found;
}

function initFlatpickr(root = document) {
  if (typeof window.flatpickr === "undefined") return;
  fpTargets(root, "input[data-flatpickr]").forEach((el) => {
    if (el._flatpickr) return;
    const opts = { ...FP_DATE };
    if (el.hasAttribute("data-fp-past")) delete opts.minDate;
    window.flatpickr(el, opts);
  });
  fpTargets(root, "input[data-flatpickr-time]").forEach((el) => {
    if (el._flatpickr) return;
    window.flatpickr(el, FP_TIME);
  });
}
document.addEventListener("DOMContentLoaded", () => initFlatpickr());
window.initFlatpickr = initFlatpickr;

/* Alpine writes straight to input.value, which the visible altInput never sees —
 * so a picker in reused markup (the reservation editor, an x-for stop row) keeps
 * showing the previous trip's date. Call from x-effect to repaint it. */
function fpSync(el, value) {
  const fp = el && el._flatpickr;
  if (!fp) return;
  // Compare against what flatpickr has *parsed*, never el.value: Alpine writes the
  // raw model straight onto the (now hidden) input, so el.value already matches
  // while the visible altInput is still blank.
  const shown = fp.selectedDates.length
    ? fp.formatDate(fp.selectedDates[0], fp.config.dateFormat)
    : "";
  if ((value || "") === shown) return;
  if (value) fp.setDate(value, false);
  else fp.clear(false);
}
window.fpSync = fpSync;

/** "15:30" → "3:30 PM". Empty for anything unparseable. */
function time12(hhmm) {
  const [h, m] = String(hhmm || "").split(":").map(Number);
  if (!Number.isInteger(h) || !Number.isInteger(m)) return "";
  return (h % 12 || 12) + ":" + String(m).padStart(2, "0") + " " + (h >= 12 ? "PM" : "AM");
}
window.time12 = time12;

/* ---------------------------------------------- flight verification (spec 2026-08-29) */
/* Tailwind only emits classes it finds as literals: keep every chip/icon class string
   whole here, never assembled. The server picks `chip`/`icon` per state (Flight.pill()). */
const FLIGHT_CHIP_BASE =
  "inline-flex items-center gap-1 text-[11px] font-semibold px-2 py-0.5 rounded-full num whitespace-nowrap";
window.FLIGHT_CHIP_BASE = FLIGHT_CHIP_BASE;

/** POST one flight to the verify endpoint. Resolves to the pill dict, or null after a toast. */
async function verifyFlight(payload) {
  const meta = document.querySelector('meta[name="flight-verify-url"]');
  if (!meta || !meta.content) return null;
  let status = 0, data = {};
  try {
    const resp = await fetch(meta.content, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": getCookie("csrftoken") },
      body: JSON.stringify(payload),
    });
    status = resp.status;
    data = await resp.json().catch(() => ({}));
  } catch (e) {
    Alpine.store("toast").push({ type: "danger", title: "Network error — could not verify the flight" });
    return null;
  }
  if (status !== 200) {
    Alpine.store("toast").push({
      type: status === 503 ? "info" : "danger",
      title: data.error || "Could not verify the flight",
      timeout: 7000,
    });
    return null;
  }
  return data;
}
window.verifyFlight = verifyFlight;

/** Dispatch drawer block: pill + detail + a Refresh that honours the re-check window. */
function flightStatus(opts = {}) {
  return {
    payload: opts.payload || {},
    pill: opts.initial || null,
    label: opts.label || "",
    enabled: !!opts.enabled,
    busy: false,
    now: Date.now(),
    _timer: null,
    init() { this._timer = setInterval(() => { this.now = Date.now(); }, 15000); },
    destroy() { clearInterval(this._timer); },
    get waitMinutes() {
      if (!this.pill || !this.pill.refresh_allowed_at) return 0;
      const ms = Date.parse(this.pill.refresh_allowed_at) - this.now;
      return ms > 0 ? Math.ceil(ms / 60000) : 0;
    },
    get canCheck() {
      return this.enabled && !this.busy && !!this.payload.direction && !!this.payload.date && this.waitMinutes === 0;
    },
    get reason() {
      if (!this.payload.direction) return "Set Arriving/Departing in the editor";
      if (!this.payload.date) return "Set the trip date first";
      if (this.waitMinutes) return "Cached — next check allowed in " + this.waitMinutes + " min";
      return "Check this flight with aviationstack";
    },
    get buttonLabel() {
      if (!this.pill) return "Verify";
      const verb = this.pill.source === "flights" ? "Refresh" : "Re-check";
      const m = this.waitMinutes;
      if (!m) return verb;
      return verb + " in " + (m >= 60 ? Math.ceil(m / 60) + " h" : m + " min");
    },
    async check() {
      if (!this.canCheck) return;
      this.busy = true;
      const pill = await verifyFlight(this.payload);
      this.busy = false;
      if (pill) this.pill = pill;
    },
  };
}
window.flightStatus = flightStatus;

/** YYYY-MM-DD from a Date's LOCAL components. `toISOString()` gives the UTC date,
 *  which is tomorrow for any evening pickup in the US. */
function localDate(d) {
  return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
}
window.localDate = localDate;

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
function geocodeSearch(acUrl, q, biasLat, biasLon, signal) {
  q = (q || "").trim();
  if (!q) return Promise.resolve([]);
  let url = `${acUrl}?q=${encodeURIComponent(q)}`;
  if (biasLat != null && biasLon != null) url += `&lat=${biasLat}&lon=${biasLon}`;
  return fetch(url, { headers: { "X-Requested-With": "fetch" }, signal })
    .then((r) => r.json())
    .then((d) => d.results || [])
    .catch((e) => {
      if (e.name === "AbortError") throw e; // caller drops it; do not clear results
      return [];
    });
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

/* NOTE: address suggestions are biased by ADDRESS_BIAS_CENTER (settings → every
   widget's fallbackLat/fallbackLon), never by the browser's geolocation API. We
   deliberately do not prompt: every trip we quote happens in the DMV, so the
   service-area center already describes where the addresses are, and a visitor
   browsing from another metro would have been biased to the wrong one. */

/* Single-line address input for the public booking form (pickup / drop-off). */
function addressAutocomplete(opts = {}) {
  return {
    acUrl: opts.acUrl,
    value: opts.value || "",
    lat: opts.lat || "",
    lng: opts.lng || "",
    display: opts.display || "",
    airport: opts.airport || "",
    results: [],
    open: false,
    active: -1,
    loading: false,
    biasLat: opts.fallbackLat ?? null,
    biasLon: opts.fallbackLon ?? null,
    _raw: [],
    _ctl: null,
    search() {
      // Not an airport any more → the flight row hides; empty it so a stale value
      // never reaches the server under a field the visitor can't see.
      this.airport = "";
      const root = this.$root || this.$el;
      const flight = root?.querySelector('input[name$="_flight"]'); if (flight) flight.value = "";
      const sel = root?.querySelector('select[name$="_airline"]'); if (sel?.tomselect) sel.tomselect.clear(true);
      const q = this.value.trim();
      this._ctl?.abort();
      if (!q) { this.results = []; this._raw = []; this.open = false; this.loading = false; return; }
      this._ctl = new AbortController();
      this.loading = true; this.open = true;
      geocodeSearch(this.acUrl, q, this.biasLat, this.biasLon, this._ctl.signal).then((rs) => {
        this._raw = rs;
        this.results = rankByProximity(rs, this.biasLat, this.biasLon);
        this.active = this.results.length ? 0 : -1;
        this.loading = false;
      }).catch(() => { /* superseded by a newer keystroke — leave state alone */ });
    },
    statusText() {
      if (this.loading) return "Searching addresses";
      if (!this.results.length) return this.value ? "No matches" : "";
      return `${this.results.length} address suggestions available`;
    },
    move(d) {
      if (!this.results.length) return;
      this.active = (this.active + d + this.results.length) % this.results.length;
    },
    choose(i) {
      const r = this.results[i]; if (!r) return;
      this.value = formatAddressLine(r);
      this.lat = r.latitude || ""; this.lng = r.longitude || ""; this.display = r.display_name || "";
      this.airport = r.is_airport ? String(r.airport_id || "") : "";
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
    // On a server-side error re-render, open the step holding the first error.
    // name/email/phone are the only server-required fields, so that is step 2.
    step: opts.twoStep && !opts.hasErrors ? 1 : 2,
    errors: {},
    // Snapshotted when step 1 is cleared. It cannot be a lazy x-text call: summary()
    // reads field values straight off the DOM, which Alpine's reactivity cannot see,
    // so x-text would only re-run when some *other* reactive prop changed and would
    // render a stale trip.
    summaryText: "",

    submitLabel() {
      if (!this.twoStep) return "Request a quote";
      return this.step === 1 ? "Get my quote" : "Send my request";
    },

    // Step-1 gate. Every visible trip field is required; hours only when hourly.
    // Messages name the fix, not the rule.
    required() {
      const req = [
        ["pickup", "Enter a pickup address"],
        ["dropoff", "Enter a drop-off address"],
        ["pickup_date", "Pick a date"],
        ["pickup_time", "Pick a time"],
        ["passengers", "How many passengers?"],
      ];
      if (this.tripType === "hourly") req.push(["hours", "Enter how many hours you need"]);
      return req;
    },

    validateStepOne() {
      this.errors = {};
      let firstBad = null;
      for (const [name, message] of this.required()) {
        // $root, never $el: Alpine resolves $el to the element the expression runs
        // on, which for @click is the button — and a button contains no fields, so
        // every lookup would come back null and the gate would silently pass.
        const el = this.$root.querySelector(`[name="${name}"]`);
        if (el && !String(el.value || "").trim()) {
          this.errors[name] = message;
          if (!firstBad) firstBad = el;
        }
      }
      if (firstBad) {
        // flatpickr hides the real input behind an altInput sibling; focus that.
        (firstBad._flatpickr?.altInput || firstBad).focus();
        return false;
      }
      return true;
    },

    onSubmit(e) {
      if (!this.twoStep || this.step === 2) return; // let it post
      e.preventDefault();
      if (!this.validateStepOne()) return;
      this.summaryText = this.summary();
      this.step = 2;
    },

    back() {
      this.step = 1;
    },

    // Reads flatpickr's altInput display values, not the raw Y-m-d / H:i.
    summary() {
      const val = (name) => {
        const el = this.$root.querySelector(`[name="${name}"]`); // see validateStepOne
        if (!el) return "";
        return (el._flatpickr?.altInput?.value || el.value || "").trim();
      };
      const label = this.tripType === "hourly" ? "Hourly" : "Transfer";
      // Stops live in a nested bookingStops component, so read the serialised
      // hidden field rather than reaching into its state.
      let stops = 0;
      try {
        stops = (JSON.parse(val("stops_json") || "[]") || []).length;
      } catch (e) {
        stops = 0;
      }
      const route = [
        val("pickup"),
        stops ? `${stops} stop${stops === 1 ? "" : "s"}` : null,
        val("dropoff"),
      ].filter(Boolean).join(" → ");
      const when = [val("pickup_date"), val("pickup_time")].filter(Boolean).join(", ");
      const pax = val("passengers");
      const parts = [label, route, when];
      if (this.tripType === "hourly" && val("hours")) parts.push(`${val("hours")} hrs`);
      if (pax) parts.push(`${pax} passenger${pax === "1" ? "" : "s"}`);
      return parts.filter(Boolean).join(" · ");
    },
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
    max: 4,
    canAdd() { return this.stops.length < this.max; },
    add() {
      if (!this.canAdd()) return;
      this.stops.push({ address: "", lat: "", lng: "", display: "", airport: "", airline: "", flight: "", direction: "" });
    },
    remove(i) { this.stops.splice(i, 1); },
    // Reassign rather than mutate: rowState() hands back a throwaway object when the
    // row has no cached results yet, so mutating that would silently go nowhere.
    move(i, d) {
      const st = this._r[i];
      if (!st || !st.list.length) return;
      this._r[i] = { ...st, active: ((st.active ?? -1) + d + st.list.length) % st.list.length };
    },
    chooseActive(i) {
      const st = this._r[i];
      if (st && st.active >= 0) this.choose(i, st.active);
    },
    search(i) {
      const s = this.stops[i];
      // Not an airport any more → the flight row hides; empty it so a stale value
      // never reaches the server under a row the visitor can't see. The picker itself
      // resyncs via the row's x-effect (keyed rows get reused by Alpine when an
      // earlier stop is removed, so the DOM select must follow this stop's own data,
      // not stay pinned to whatever stop last owned that row).
      s.airport = ""; s.airline = ""; s.flight = ""; s.direction = "";
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
      s.airport = r.is_airport ? String(r.airport_id || "") : "";
      this._r[i] = { open: false, list: [], active: -1 };
    },
    closeRow(i) { this._r[i] = { open: false, list: [], active: -1 }; },
    json() {
      return JSON.stringify(
        this.stops
          .filter((s) => (s.address || "").trim())
          .map((s) => ({
            address: s.address,
            lat: s.lat || null,
            lng: s.lng || null,
            display: s.display,
            airport: s.airport || null,
            airline: s.airline || null,
            flight: s.flight || "",
            direction: s.direction || "",
          })),
      );
    },
  };
}
window.bookingStops = bookingStops;

window.smartAddress = smartAddress;
