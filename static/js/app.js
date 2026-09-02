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
      // Stripe re-themes a mounted element in place — no remount needed.
      this.$watch("$store.theme.mode", (m) => {
        if (this.elements) this.elements.update({ appearance: apcPay.appearance(m) });
      });
    },

    cents() {
      const n = Math.round(parseFloat(this.amount || "0") * 100);
      return Number.isFinite(n) ? n : 0;
    },

    mountElement() {
      if (!this.stripe || this.mounted) return;
      this.elements = this.stripe.elements(
        apcPay.elementsOptions({
          mode: "payment",
          amount: this.cents(),
          appearanceMode: Alpine.store("theme").mode,
        }),
      );
      this.paymentElement = this.elements.create("payment", apcPay.paymentElementOptions());
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

/* -------------------------------------------------- send the customer a pay-page link */
function sendPayLink(url) {
  return {
    busy: false,
    send() {
      if (this.busy) return;
      this.busy = true;
      postForm(url, {})
        .then((r) =>
          Alpine.store("toast").push({
            type: "success",
            title: "Payment link sent",
            message: "Sent by " + (r.channel === "sms" ? "text message" : "email") + ".",
          }),
        )
        .catch((e) =>
          Alpine.store("toast").push({
            type: "error",
            title: "Could not send",
            message: e.message || "Please try again.",
          }),
        )
        .finally(() => {
          this.busy = false;
        });
    },
  };
}
window.sendPayLink = sendPayLink;

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
    // The wedding builder modal (spec 2026-08-30 §5.2). Opens itself when the workspace
    // was reached with ?wedding=1, i.e. straight from the New wedding button.
    weddingOpen: !!opts.weddingOpen,
    draftIsNew: false,
    // Set once the agent gives the drop-off a day / a time of its own; until then each
    // follows the pickup.  Component state, not draft state — neither may ride along in
    // the save payload.
    dropoffPinned: false,
    dropoffTimePinned: false,
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
        tripType: "transfer", serviceType: "", date: "", time: "",
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
        // Same trip: the service picker is a Tom Select too, and setValue() is the only
        // way to move it — the underlying <select> is not what the widget renders.
        const st = document.getElementById("f-res-service");
        if (st && st.tomselect) st.tomselect.setValue(this.draft.serviceType || "", true);
      });
    },
    newReservation() {
      this.draft = this.blankReservation();
      this.dropoffPinned = false;
      this.dropoffTimePinned = false;
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
      // A stored trip that already ends on another day, or at a time of its own, keeps
      // both through pickup edits.
      this.dropoffPinned = !!this.draft.dropoffDate && this.draft.dropoffDate !== this.draft.date;
      this.dropoffTimePinned =
        !!this.draft.dropoffTime && this.draft.dropoffTime !== this.derivedDropoffTime();
      this.editorOpen = true;
      // Older rows can predate the derived drop-off — fill it so the hourly
      // read-out shows the end time instead of the "set a date" prompt.
      this.onHoursChanged();
      this.syncVehicleSelect();
    },
    closeEditor() { this.editorOpen = false; },
    /* "Copy Reservation ×N" — the wedding-shuttle case is several identical minibuses on
     * one itinerary (feedback B1). Opens the shared modal (never a native prompt); the
     * chosen count rides on the hidden per-row form, which posts normally. */
    duplicateReservation(pk) {
      const go = (n) => {
        const count = Math.max(1, Math.min(20, Math.floor(Number(n)) || 1));
        const form = document.getElementById(`form-dup-${pk}`);
        if (!form) return;
        form.querySelector('input[name="count"]').value = count;
        form.submit();
      };
      window.__apcDuplicateGo = go;
      Alpine.store("modal").show({
        variant: "info",
        title: "Duplicate reservation",
        confirmText: "Duplicate",
        html: `
          <p class="text-[13px] leading-relaxed">Add copies of this trip to the quote — a wedding
          shuttle running several identical vehicles, say. Each copy stays independently editable.</p>
          <div class="mt-3 flex flex-wrap items-center gap-1.5">
            <button type="button" onclick="window.__apcDuplicateGo(1)" class="px-2.5 py-1 rounded-lg ring-1 ring-line text-[13px] font-semibold text-ink hover:bg-goldl transition-colors">×1</button>
            <button type="button" onclick="window.__apcDuplicateGo(2)" class="px-2.5 py-1 rounded-lg ring-1 ring-line text-[13px] font-semibold text-ink hover:bg-goldl transition-colors">×2</button>
            <button type="button" onclick="window.__apcDuplicateGo(3)" class="px-2.5 py-1 rounded-lg ring-1 ring-line text-[13px] font-semibold text-ink hover:bg-goldl transition-colors">×3</button>
            <button type="button" onclick="window.__apcDuplicateGo(5)" class="px-2.5 py-1 rounded-lg ring-1 ring-line text-[13px] font-semibold text-ink hover:bg-goldl transition-colors">×5</button>
            <span class="inline-block w-20"><input id="apc-dup-count" type="number" min="1" max="20" value="4" aria-label="Number of copies" class="field text-[13px]"></span>
          </div>`,
        onConfirm: () => go(document.getElementById("apc-dup-count").value),
      });
    },
    applyVehicleRateCard() {
      const v = this.vehicles.find((x) => String(x.id) === String(this.draft.vehicle));
      // No (active) vehicle → no rate card → no minimum. Min hours is read-only, so a
      // stale number here would be billed with no way to correct it.
      this.draft.minHours = v ? (this.draft.tripType === "hourly" ? v.hourlyMin : v.transferMin) : 0;
      if (v) this.draft.rate = v.rate;
      this.onHoursChanged();   // the minimum is what's billed until overridden → end time moves
    },
    /* Transfers are same-day unless told otherwise, so the pickup date carries the
       drop-off date with it — one box to fill instead of two.  Give the drop-off its
       own day and it pins: later pickup edits leave it where the agent put it.
       Hourly ignores all of this; there the drop-off is derived from the hours. */
    mirrorDropoffDate() {
      if (this.draft.tripType !== "transfer" || this.dropoffPinned || !this.draft.date) return;
      // Same day — unless the clock has wrapped: an 11:30 PM pickup dropping at 12:30 AM
      // lands tomorrow.
      const d = new Date(this.draft.date + "T00:00");
      if (this.draft.time && this.draft.dropoffTime && this.draft.dropoffTime < this.draft.time) {
        d.setDate(d.getDate() + 1);
      }
      this.draft.dropoffDate = localDate(d);
    },
    onPickupDateChanged() {
      // A pinned drop-off that now falls before the pickup is an impossible trip — let it follow again.
      if (this.dropoffPinned && this.draft.dropoffDate && this.draft.dropoffDate < this.draft.date) {
        this.dropoffPinned = false;
      }
      this.mirrorDropoffDate();
      this.onHoursChanged();
    },
    /* An hour covers most point-to-point runs — a starting point to correct, not a
       claim.  It keeps following the pickup until the agent sets a time of their own;
       from then on the time is theirs and nothing moves it.  Tracking (rather than
       filling an empty box once) is what keeps a moved pickup from leaving a drop-off
       stranded before it — a trip that is invalid and nothing here validates. */
    derivedDropoffTime() {
      if (!this.draft.time) return "";
      const start = new Date("2000-01-01T" + this.draft.time);
      return isNaN(start) ? "" : new Date(start.getTime() + 3600000).toTimeString().slice(0, 5);
    },
    onPickupTimeChanged() {
      if (this.draft.tripType === "transfer" && !this.dropoffTimePinned) {
        this.draft.dropoffTime = this.derivedDropoffTime();
      }
      this.mirrorDropoffDate();
      this.onHoursChanged();
    },
    onDropoffDateChanged() {
      this.dropoffPinned = !!this.draft.dropoffDate && this.draft.dropoffDate !== this.draft.date;
    },
    onDropoffTimeChanged() {
      this.dropoffTimePinned = this.draft.dropoffTime !== this.derivedDropoffTime();
      this.mirrorDropoffDate();
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
      // hidePlaceholder is deliberately absent: Tom Select's own default resolves it to
      // `mode !== "multi"`, which is what we want. Hard-coding false left the "Select…"
      // prompt sitting beside the chosen item on every *searchable* single select (the
      // search:"off" ones have no control input, so they never showed it).
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

/* A calendar closes itself the moment you click a day; the time row has no such
 * gesture — nudging hours and minutes never ends, so Escape or a click outside was
 * the only way out and agents sat there looking for the exit. Give every time picker
 * an explicit Done. Mobile takes the OS picker instead (`setupMobile` skips build(),
 * so there is no calendarContainer to append to) — onReady still fires there. */
function fpAddDone(fp) {
  const panel = fp.calendarContainer;
  if (!panel || panel.querySelector(".fp-done")) return;
  const done = document.createElement("button");
  done.type = "button";
  done.className = "fp-done";
  done.textContent = "Done";
  done.addEventListener("click", () => fp.close());
  panel.appendChild(done);
}

const FP_DATE = { ...FP_BASE, altInput: true, altFormat: "F j, Y", dateFormat: "Y-m-d", minDate: "today" };
const FP_TIME = { ...FP_BASE, enableTime: true, noCalendar: true, dateFormat: "H:i", altInput: true, altFormat: "h:i K", time_24hr: false,
                  onReady: (dates, str, fp) => fpAddDone(fp) };

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

/* ------------------------------------------- customer search (contact modal)
 * Type-ahead over existing customers for the New booking / New lead modal.
 * Picking one links the lead to that contact (hidden `contact_id`) and pre-fills the
 * form; the server then treats any edit as a deliberate profile update.
 *
 * The fields are plain inputs shared with the create-a-new-customer path, so this
 * writes to them by id rather than owning them with x-model. The phone box is an
 * intl-tel-input, which only repaints its flag/formatting via setNumber().
 */
function contactPicker(opts = {}) {
  return {
    searchUrl: opts.searchUrl,
    query: "",
    results: [],
    open: false,
    loading: false,
    active: -1,
    selected: null,
    hint: null, // an existing customer matching the typed phone/email

    fieldEl(name) {
      return document.getElementById(`nl-${name}`);
    },
    setField(name, value) {
      const el = this.fieldEl(name);
      if (!el) return;
      // setNumber() so the country flag and formatting follow the number; a bare
      // .value assignment leaves the widget showing the previous country.
      if (el.iti) el.iti.setNumber(value || "");
      else el.value = value || "";
    },
    readField(name) {
      const el = this.fieldEl(name);
      return el ? el.value.trim() : "";
    },

    subtitle(r) {
      return [r.company, r.phone, r.email].filter(Boolean).join(" · ");
    },
    leadCount(r) {
      if (!r || !r.leads) return "New contact";
      return r.leads === 1 ? "1 lead" : `${r.leads} leads`;
    },

    search() {
      const q = this.query.trim();
      if (!q) { this.closeResults(); return; }
      this.loading = true; this.open = true;
      fetch(`${this.searchUrl}?q=${encodeURIComponent(q)}`, { headers: { "X-Requested-With": "fetch" } })
        .then((r) => r.json())
        .then((d) => { this.results = d.results || []; this.active = this.results.length ? 0 : -1; })
        .catch(() => { this.results = []; this.active = -1; })
        .finally(() => { this.loading = false; });
    },

    /* Does the typed phone/email already belong to someone? Answered server-side —
     * find_match owns the E.164-vs-raw rules and must not be reimplemented here. */
    checkMatch() {
      if (this.selected) return;
      const phone = this.readField("phone");
      const email = this.readField("email");
      if (!phone && !email) { this.hint = null; return; }
      const qs = new URLSearchParams({ phone, email });
      fetch(`${this.searchUrl}?${qs}`, { headers: { "X-Requested-With": "fetch" } })
        .then((r) => r.json())
        .then((d) => { this.hint = d.match || null; })
        .catch(() => { this.hint = null; });
    },

    move(delta) {
      if (!this.results.length) return;
      this.active = (this.active + delta + this.results.length) % this.results.length;
    },
    choose(i) {
      const r = this.results[i];
      if (!r) return;
      this.link(r);
    },
    useHint() {
      if (this.hint) this.link(this.hint);
    },
    link(r) {
      this.selected = r;
      this.hint = null;
      this.setField("name", r.name);
      this.setField("company", r.company);
      this.setField("phone", r.phone);
      this.setField("email", r.email);
      this.query = "";
      this.closeResults();
    },
    /* Unlink keeps the fields: the usual reason to unlink is "same details, different
     * person", so what's on screen is the start of a new customer. */
    unlink() {
      this.selected = null;
      this.$nextTick(() => this.fieldEl("name")?.focus());
    },

    closeResults() { this.open = false; this.results = []; this.active = -1; },

    /* A reopened modal starts clean — otherwise the next booking silently inherits the
     * last one's customer, which is the one mistake this feature must not introduce. */
    reset() {
      this.selected = null; this.hint = null; this.query = "";
      this.closeResults();
      for (const f of ["name", "company", "phone", "email"]) this.setField(f, "");
    },
  };
}
window.contactPicker = contactPicker;

/* ------------------------------------------------------- wedding intake (2026-08-30)
 * The customer describes their wedding; this derives the trips. Seven steps, all state
 * client-side, ONE post at the end — the same shape as quoteSteps() above, just longer.
 *
 * The generation rules below MIRROR apps/public/wedding.py, which is the authority: the
 * server re-derives every vehicle recommendation on submit and ignores whatever this
 * sends. Change one and change the other, or the preview and the quote drift apart.
 */
const WEDDING_STEPS = ["date", "venue", "who", "hotels", "times", "itinerary", "contact"];
/* Tabler icon inner-markup (outline, stroke-width 2), matched to `static/icons/`.
 * The template supplies the <svg> wrapper (h-[22px] w-[22px], currentColor). */
const WEDDING_GROUPS = [
  {
    key: "guests", title: "Our guests", hint: "Shuttles between hotels and the venue",
    icon: '<path d="M10 13a2 2 0 1 0 4 0a2 2 0 0 0 -4 0"/><path d="M8 21v-1a2 2 0 0 1 2 -2h4a2 2 0 0 1 2 2v1"/><path d="M15 5a2 2 0 1 0 4 0a2 2 0 0 0 -4 0"/><path d="M17 10h2a2 2 0 0 1 2 2v1"/><path d="M5 5a2 2 0 1 0 4 0a2 2 0 0 0 -4 0"/><path d="M3 13v-1a2 2 0 0 1 2 -2h2"/>',
  },
  {
    key: "party", title: "The wedding party", hint: "Bridesmaids, groomsmen, the two of you",
    icon: '<path d="M4 5h2"/><path d="M5 4v2"/><path d="M11.5 4l-.5 2"/><path d="M18 5h2"/><path d="M19 4v2"/><path d="M15 9l-1 1"/><path d="M18 13l2 -.5"/><path d="M18 19h2"/><path d="M19 18v2"/><path d="M14 16.518l-6.518 -6.518l-4.39 9.58a1 1 0 0 0 1.329 1.329l9.579 -4.39z"/>',
  },
  {
    key: "family", title: "Family & VIPs", hint: "Parents, grandparents, close family",
    icon: '<path d="M9 7m-4 0a4 4 0 1 0 8 0a4 4 0 1 0 -8 0"/><path d="M3 21v-2a4 4 0 0 1 4 -4h4a4 4 0 0 1 4 4v2"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/><path d="M21 21v-2a4 4 0 0 0 -3 -3.85"/>',
  },
  {
    key: "couple", title: "Just the two of us", hint: "A private car for the exit",
    icon: '<path d="M7 17m-2 0a2 2 0 1 0 4 0a2 2 0 1 0 -4 0"/><path d="M17 17m-2 0a2 2 0 1 0 4 0a2 2 0 1 0 -4 0"/><path d="M5 17h-2v-6l2 -5h9l4 5h1a2 2 0 0 1 2 2v4h-2m-4 0h-6m-6 -6h15m-6 0v-5"/>',
  },
];
const WEDDING_MAX_LEGS = 12;

function weddingPlanner(opts = {}) {
  const saved = opts.resume || null;
  return {
    venuesUrl: opts.venuesUrl,
    groupsMeta: WEDDING_GROUPS,
    steps: WEDDING_STEPS,
    resumed: !!(saved && saved.resume),
    quoteNo: (saved && saved.quote_no) || "",
    // Portal mode (the office's builder): the lead already has a contact, so that step
    // is dropped, the per-step footers give way to one shared one, and each leg carries
    // a real VehicleType the agent picked. An explicit option, NOT something read off
    // the saved plan — a brand-new wedding started from the New wedding button has no
    // saved plan at all, and it is just as much the office's builder.
    portal: !!opts.portal,
    vehicleOptions: opts.vehicleOptions || [],

    step: 0,
    date: (saved && saved.wedding_date) || "",
    venue: (saved && saved.venue) || null,
    venueName: (saved && saved.venue_name) || "",
    sameSite: saved ? saved.same_site !== false : true,
    ceremony: (saved && saved.ceremony) || null,
    ceremonyName: (saved && saved.ceremony && saved.ceremony.name) || "",
    who: (saved && saved.groups) || [],
    counts: {
      guests: (saved && saved.guest_count) || 100,
      party: (saved && saved.party_count) || 12,
      family: (saved && saved.family_count) || 8,
    },
    hotels: (saved && saved.hotels) || [],
    hotelsTbd: !!(saved && saved.hotels_tbd),
    ceremonyTime: (saved && saved.ceremony_time) || "16:00",
    endTime: (saved && saved.end_time) || "23:00",
    timesTbd: !!(saved && saved.times_tbd),
    legs: (saved && saved.legs && saved.legs.length) ? saved.legs.map((l) => ({ ...l })) : null,
    contact: {
      name: (saved && saved.name) || "",
      email: (saved && saved.email) || "",
      phone: (saved && saved.phone) || "",
    },
    submitting: false,

    // typeahead
    query: { venue: "", ceremony: "", hotel: "" },
    results: { venue: [], ceremony: [], hotel: [] },
    open: { venue: false, ceremony: false, hotel: false },
    active: { venue: -1, ceremony: -1, hotel: -1 },
    _ctl: {},

    init() {
      // A plan that already has an itinerary — a customer's resume link, or the office
      // reopening a saved wedding — drops straight onto it. A brand-new one starts at
      // step 1.
      if ((this.resumed || this.portal) && this.legs) {
        this.step = WEDDING_STEPS.indexOf("itinerary");
      }
      this.track(this.stepName);
    },

    /* ------------------------------------------------------------------ step model */
    get stepName() { return WEDDING_STEPS[this.step]; },
    get visibleSteps() {
      // Nobody is being collected from a hotel, so don't ask which hotel.
      const needsHotels = this.who.includes("guests") || this.who.includes("family");
      return WEDDING_STEPS.filter((s) => {
        if (s === "hotels") return needsHotels;
        if (s === "contact") return !this.portal; // the lead already has one
        return true;
      });
    },
    get stepNumber() { return this.visibleSteps.indexOf(this.stepName) + 1; },
    get stepCount() { return this.visibleSteps.length; },
    railState(name) {
      const here = this.visibleSteps.indexOf(this.stepName);
      const i = this.visibleSteps.indexOf(name);
      return i < here ? "done" : i === here ? "on" : "";
    },

    canAdvance() {
      switch (this.stepName) {
        case "date": return !!this.date;
        case "venue": return !!(this.venue || this.venueName.trim());
        case "who": return this.who.length > 0;
        // "We haven't booked hotels yet" is a real answer, not a skip — 43% of
        // inquiries are six months out and genuinely cannot answer this.
        case "hotels": return this.hotelsTbd || this.hotels.length > 0;
        case "contact":
          return !!(this.contact.name.trim() &&
            (this.contact.email.trim() || this.contact.phone.trim()));
        default: return true;
      }
    },

    next() {
      if (!this.canAdvance()) return;
      let i = this.step + 1;
      while (i < WEDDING_STEPS.length && !this.visibleSteps.includes(WEDDING_STEPS[i])) i++;
      if (WEDDING_STEPS[i] === "itinerary" && !this.legs) this.legs = this.generateLegs();
      this.track(this.stepName, "completed");
      this.step = Math.min(i, WEDDING_STEPS.length - 1);
      this.track(this.stepName);
      window.scrollTo({ top: 0, behavior: "smooth" });
    },
    back() {
      let i = this.step - 1;
      while (i > 0 && !this.visibleSteps.includes(WEDDING_STEPS[i])) i--;
      this.step = Math.max(0, i);
      window.scrollTo({ top: 0, behavior: "smooth" });
    },

    /* Step-level drop-off is the only way to tell whether the deep path is worth its
     * length (spec §7.3). No analytics vendor is installed yet, so this pushes to a
     * dataLayer and fires a DOM event — whatever tag manager arrives later picks both up
     * with no further work here. */
    track(step, phase = "viewed") {
      const detail = { event: "wedding_step", step, phase };
      (window.dataLayer = window.dataLayer || []).push(detail);
      window.dispatchEvent(new CustomEvent("wedding-step", { detail }));
    },

    /* ------------------------------------------------------------------- formatting */
    pad(n) { return String(n).padStart(2, "0"); },
    fmtTime(hhmm) {
      if (!hhmm) return "—";
      const [h, m] = hhmm.split(":").map(Number);
      const hour = h % 12 || 12;
      return `${hour}:${this.pad(m)} ${h >= 12 ? "PM" : "AM"}`;
    },
    fmtDate(iso) {
      if (!iso) return "—";
      // Noon, not midnight: a date-only string parses as UTC, and midnight UTC is the
      // previous day everywhere west of Greenwich — including every customer we have.
      return new Date(`${iso}T12:00:00`).toLocaleDateString("en-US",
        { weekday: "long", month: "long", day: "numeric", year: "numeric" });
    },
    daysOut(iso) {
      if (!iso) return null;
      const then = new Date(`${iso}T12:00:00`);
      const now = new Date();
      return Math.round((then - new Date(now.getFullYear(), now.getMonth(), now.getDate(), 12))
        / 86400000);
    },
    get leadTimeNote() {
      const days = this.daysOut(this.date);
      if (days === null) return null;
      if (days < 0) return { tone: "warn", text: "That date has already passed — double-check the year." };
      if (days < 45) return { tone: "warn", text: `That's ${days} days away. We'll flag this as time-sensitive and call you today.` };
      if (days < 200) return { tone: "gold", text: `About ${Math.round(days / 30)} months out — a good time to lock in a Saturday.` };
      return { tone: "info", text: "That's over a year out. We'll only ask what you already know — you can fill in the rest later." };
    },

    /* ---------------------------------------------------------------- the rules */
    shift(hhmm, mins) {
      const [h, m] = hhmm.split(":").map(Number);
      const t = (((h * 60 + m + mins) % 1440) + 1440) % 1440;
      return `${this.pad(Math.floor(t / 60))}:${this.pad(t % 60)}`;
    },
    vehicleFor(count) {
      const cap = (this.venue && this.venue.vehicle_cap) || null;
      const limit = Math.min(cap || 56, 56);
      if (count <= 6) return "Executive SUV";
      if (count <= 14) return "Sprinter van";
      if (count <= 24) return "Mini bus";
      if (count <= 38) return "Executive mini coach";
      const runs = Math.ceil(count / limit);
      return runs <= 1 ? "Motorcoach" : `${runs} × ${limit}-passenger coach`;
    },
    /* Mirror of wedding.py:vehicle_is_certain — show a specific coach size only when the
     * venue's cap is on file or the group is small enough (<= 38) that the class is
     * unambiguous. Otherwise the itinerary shows the "we'll confirm" line (APC-6). */
    vehicleCertain(count) {
      return this.venueCap !== null || count <= 38;
    },
    get anyVehicleTbc() {
      return (this.legs || []).some((l) => !l.skip && !this.vehicleCertain(l.pax));
    },
    shortName(place) {
      const suffix = ` ${place.city || ""}`;
      return place.city && place.name.endsWith(suffix)
        ? place.name.slice(0, -suffix.length) : place.name;
    },
    get hotelLabel() {
      if (this.hotelsTbd || !this.hotels.length) return "Guest hotels (to be confirmed)";
      if (this.hotels.length === 1) return this.hotels[0].name;
      return `${this.hotels.length} hotels — ${this.hotels.map((h) => this.shortName(h)).join(", ")}`;
    },
    siteName(place, typed, fallback) {
      if (place) return place.name;
      return (typed || "").trim() || fallback;
    },
    siteLine(place) {
      return place ? (place.location_line || place.sub || "") : "";
    },
    get venueLabel() { return this.siteName(this.venue, this.venueName, "Your venue"); },
    get ceremonyLabel() {
      if (this.sameSite) return this.venueLabel;
      return this.siteName(this.ceremony, this.ceremonyName, "Ceremony site");
    },
    get venueCap() { return (this.venue && this.venue.vehicle_cap) || null; },

    generateLegs() {
      const cer = this.timesTbd ? "16:00" : (this.ceremonyTime || "16:00");
      const end = this.timesTbd ? "23:00" : (this.endTime || "23:00");
      const venue = { name: this.venueLabel, sub: this.siteLine(this.venue) };
      const site = { name: this.ceremonyLabel, sub: this.sameSite ? venue.sub : this.siteLine(this.ceremony) };
      const hotel = {
        name: this.hotelLabel,
        sub: (this.hotelsTbd || !this.hotels.length) ? "hotel not booked yet"
          : (this.hotels.length === 1 ? this.siteLine(this.hotels[0]) : ""),
      };
      const has = (k) => this.who.includes(k);
      const legs = [];
      if (has("party")) legs.push({
        id: "party-in", time: this.shift(cer, -75), title: "Wedding party to the ceremony",
        from: "Getting-ready location", from_sub: "we'll confirm the address with you",
        to: site.name, to_sub: site.sub, pax: this.counts.party, optional: false,
      });
      if (has("family")) legs.push({
        id: "family-in", time: this.shift(cer, -70), title: "Family & VIPs to the ceremony",
        from: hotel.name, from_sub: hotel.sub, to: site.name, to_sub: site.sub,
        pax: this.counts.family, optional: false,
      });
      if (has("guests")) legs.push({
        id: "guests-in", time: this.shift(cer, -60), title: "Guests to the ceremony",
        from: hotel.name, from_sub: hotel.sub, to: site.name, to_sub: site.sub,
        pax: this.counts.guests, optional: false,
      });
      if (!this.sameSite) {
        const aboard = (has("guests") ? this.counts.guests : 0) + (has("party") ? this.counts.party : 0)
          + (has("family") ? this.counts.family : 0);
        legs.push({
          id: "hop", time: this.shift(cer, 45), title: "Ceremony to reception",
          from: site.name, from_sub: site.sub, to: venue.name, to_sub: venue.sub,
          pax: aboard || this.counts.guests, optional: false,
        });
      }
      if (has("guests")) {
        // No early return run by default (APC-7): the couple opts in with addEarlyReturn()
        // and we never pre-fill an early time. Mirrors wedding.py.
        legs.push({
          id: "final-out", time: end, title: "Final return — last call",
          from: venue.name, from_sub: venue.sub, to: hotel.name, to_sub: "",
          pax: this.counts.guests, optional: false,
        });
      }
      if (has("couple")) legs.push({
        id: "exit", time: end, title: "Your exit", from: venue.name, from_sub: venue.sub,
        to: "Hotel or home", to_sub: "we'll confirm with you", pax: 2, optional: false,
      });
      // The default shape: every movement its own transfer. The office may switch any
      // of them to hourly on the itinerary; a fresh generate returns to the default.
      for (const leg of legs) { leg.trip_type = "transfer"; leg.hours = null; }
      legs.sort((a, b) => a.time.localeCompare(b.time));
      return legs;
    },

    /* ------------------------------------------------------------ itinerary edits */
    get liveLegs() { return (this.legs || []).filter((l) => !l.skip); },
    get legCount() { return this.liveLegs.length; },
    get canAddLeg() { return this.legCount < WEDDING_MAX_LEGS; },
    resort() { this.legs.sort((a, b) => a.time.localeCompare(b.time)); },
    setLegTime(leg, value) {
      if (!value) return;
      leg.time = value;
      this.timesTbd = false;
      this.resort();
    },
    setLegPax(leg, value) {
      leg.pax = Math.max(1, Math.min(400, parseInt(value, 10) || 1));
    },
    /* Office only. A wedding is N transfers by default — a set of movements, not one
     * open-ended charter — but a continuous shuttle is billed by the hour, so each leg
     * can be either. Switching back to a transfer drops the hours, mirroring what
     * services._apply_trip_window does on save. */
    setLegTripType(leg, value) {
      leg.trip_type = value;
      if (value !== "hourly") leg.hours = null;
    },
    setLegHours(leg, value) {
      const hours = parseFloat(value);
      leg.hours = Number.isFinite(hours) && hours >= 1 && hours <= 24 ? hours : null;
    },
    isHourly(leg) { return leg.trip_type === "hourly"; },
    dropLeg(leg) { leg.skip = true; },
    restoreLeg(leg) { leg.skip = false; },
    regenerate() { this.legs = this.generateLegs(); },
    /* Opt-in early return run (APC-7). Not in the generated set and never pre-timed —
     * it lands co-timed with the final run and the couple / office pull it earlier.
     * Mirrors wedding.py:early_return_leg. */
    get hasEarlyReturn() { return (this.legs || []).some((l) => l.id === "early-out"); },
    get canAddEarlyReturn() {
      return this.who.includes("guests") && !this.hasEarlyReturn && this.canAddLeg;
    },
    addEarlyReturn() {
      if (!this.legs || this.hasEarlyReturn) return;
      const end = this.timesTbd ? "23:00" : (this.endTime || "23:00");
      const venue = { name: this.venueLabel, sub: this.siteLine(this.venue) };
      const hotel = {
        name: this.hotelLabel,
        sub: (this.hotelsTbd || !this.hotels.length) ? "hotel not booked yet"
          : (this.hotels.length === 1 ? this.siteLine(this.hotels[0]) : ""),
      };
      this.legs.push({
        id: "early-out", time: end, title: "Early return run",
        from: venue.name, from_sub: venue.sub, to: hotel.name, to_sub: "",
        pax: Math.max(12, Math.round(this.counts.guests * 0.4)), optional: true,
        why: "Set the pickup time with the couple — many guests leave before the last call.",
        trip_type: "transfer", hours: null,
      });
      this.resort();
    },
    bump(key, delta) {
      this.counts[key] = Math.max(1, Math.min(400, (this.counts[key] || 1) + delta));
      this.legs = null;
    },
    setCount(key, value) {
      this.counts[key] = Math.max(1, Math.min(400, parseInt(value, 10) || 1));
      this.legs = null;
    },
    toggleGroup(key) {
      this.who = this.who.includes(key) ? this.who.filter((k) => k !== key) : this.who.concat(key);
      this.legs = null;
    },
    setSameSite(value) {
      this.sameSite = value;
      if (value) { this.ceremony = null; this.ceremonyName = ""; }
      this.legs = null;
    },
    toggleHotelsTbd() {
      this.hotelsTbd = !this.hotelsTbd;
      if (this.hotelsTbd) this.hotels = [];
      this.legs = null;
    },
    toggleTimesTbd() {
      this.timesTbd = !this.timesTbd;
      if (this.timesTbd) { this.ceremonyTime = "16:00"; this.endTime = "23:00"; }
      this.legs = null;
    },
    removeHotel(i) { this.hotels.splice(i, 1); this.legs = null; },
    clearPlace(kind) {
      const field = kind === "venue" ? "venue" : "ceremony";
      if (field === "venue") { this.venue = null; this.venueName = ""; }
      else { this.ceremony = null; this.ceremonyName = ""; }
      // "Change" reveals the same typeahead input. Wipe every trace of the previous
      // pick — the query text, the cached result list, an in-flight request, the
      // open/active flags — so the first keystroke of a new name runs a clean search
      // instead of racing a stale AbortController or reopening the old dropdown.
      this.resetField(field);
      this.legs = null;
    },
    resetField(field) {
      this._ctl[field]?.abort();
      this._ctl[field] = null;
      this.query[field] = "";
      this.results[field] = [];
      this.active[field] = -1;
      this.open[field] = false;
    },

    /* ------------------------------------------------------------------ typeahead */
    kindFor(field) { return field === "hotel" ? "hotel" : field === "ceremony" ? "church" : "venue"; },
    search(field) {
      if (field === "venue") this.venueName = this.query.venue;
      if (field === "ceremony") this.ceremonyName = this.query.ceremony;
      const q = (this.query[field] || "").trim();
      this._ctl[field]?.abort();
      if (q.length < 2) { this.results[field] = []; this.open[field] = false; return; }
      this._ctl[field] = new AbortController();
      const params = new URLSearchParams({ q, kind: this.kindFor(field) });
      fetch(`${this.venuesUrl}?${params}`, { signal: this._ctl[field].signal })
        .then((r) => r.json())
        .then((d) => {
          this.results[field] = d.results || [];
          this.active[field] = this.results[field].length ? 0 : -1;
          this.open[field] = this.results[field].length > 0;
        })
        .catch(() => { /* superseded by a newer keystroke — leave state alone */ });
    },
    move(field, delta) {
      const list = this.results[field];
      if (!list.length) return;
      this.active[field] = (this.active[field] + delta + list.length) % list.length;
    },
    chooseActive(field) {
      if (this.active[field] >= 0) this.pick(field, this.results[field][this.active[field]]);
    },
    pick(field, place) {
      if (!place) return;
      if (field === "venue") { this.venue = place; this.venueName = place.name; }
      else if (field === "ceremony") { this.ceremony = place; this.ceremonyName = place.name; }
      else if (!this.hotels.some((h) => h.name === place.name)) {
        this.hotels.push(place);
        this.hotelsTbd = false;
      }
      this.query[field] = field === "hotel" ? "" : place.name;
      this.closeResults(field);
      this.legs = null;
    },
    closeResults(field) { this.open[field] = false; this.active[field] = -1; },

    /* ----------------------------------------------------------------- submission */
    get groupsCsv() { return this.who.join(","); },
    get hotelsJson() {
      return JSON.stringify(this.hotels.map((h) => ({ venue_id: h.id || null, name: h.name })));
    },
    /* {leg_id: VehicleType pk} for the office's builder. Only assigned legs are sent:
     * an absent key means "leave whatever this trip already had", which is what stops a
     * rebuild after a time change from silently un-pricing the day. */
    get vehiclesJson() {
      const out = {};
      for (const leg of this.liveLegs) if (leg.vehicle_id) out[leg.id] = leg.vehicle_id;
      return JSON.stringify(out);
    },

    get tripTypesJson() {
      const out = {};
      for (const leg of this.liveLegs) if (leg.trip_type) out[leg.id] = leg.trip_type;
      return JSON.stringify(out);
    },
    get hoursJson() {
      const out = {};
      for (const leg of this.liveLegs) if (leg.hours) out[leg.id] = leg.hours;
      return JSON.stringify(out);
    },

    get legsJson() {
      return JSON.stringify(this.liveLegs.map((l) => ({
        id: l.id, time: l.time, title: l.title,
        from: l.from, from_sub: l.from_sub || "",
        to: l.to, to_sub: l.to_sub || "",
        pax: l.pax, optional: !!l.optional,
      })));
    },
    onSubmit(e) {
      if (!this.canAdvance()) { e.preventDefault(); return; }
      this.track("contact", "completed");
      this.submitting = true;
    },
  };
}
window.weddingPlanner = weddingPlanner;

/* --------------------------------------------------- hero service picker (2026-08-30)
 * Four cards; two of them swap the EXISTING booking widget in place rather than
 * navigating, so a visitor can still request a quote without leaving the hero.
 *
 * The cards are real links (see components/service_picker.html) — this only intercepts
 * the two that have somewhere in-page to go.
 */
const SERVICE_HEADINGS = {
  airport: "Your airport transfer",
  corporate: "Your corporate trip",
};

function servicePicker(opts = {}) {
  return {
    services: opts.services || {},
    mode: "picker",
    heading: "Get a quote",

    choose(slug) {
      this.mode = "form";
      this.heading = SERVICE_HEADINGS[slug] || "Get a quote";
      // $nextTick, not immediately: the widget is inside an x-show that has only just
      // flipped, and Tom Select measures a hidden control as zero-width.
      this.$nextTick(() => this.preselect(this.services[slug]));
    },

    /* Set the widget's occasion. Tom Select owns the rendered control, so a bare
     * select.value assignment would update the form but leave the visible item on
     * "Select an occasion". */
    preselect(pk) {
      const select = this.$refs.widget?.querySelector('select[name="service_type"]');
      if (!select) return;
      if (select.tomselect) select.tomselect.setValue(String(pk || ""), true);
      else select.value = String(pk || "");
    },

    reset() {
      this.mode = "picker";
      this.preselect("");
    },
  };
}
window.servicePicker = servicePicker;

/* ------------------------------------ public booking panel (Calendly Scheduling API)
 *
 * Our own two-pane booking UI: details on the left, availability on the right. It
 * books through /schedule/book/, which calls Calendly's API — Calendly still owns the
 * calendar, the invite email, reminders, reschedule and cancel.
 *
 * Calendly's own popup remains loaded as the fallback (window.openCalendlyPopup, in
 * public/_calendly.html). Losing our endpoints must not cost the booking path.
 */
function scheduleBooking(opts = {}) {
  return {
    slotsUrl: opts.slotsUrl || "",
    bookUrl: opts.bookUrl || "",
    days: 45,

    isOpen: false,
    phase: "idle", // idle | loading | ready | error | done
    view: "date",  // date | time — pick a day first, the way Calendly's own page does
    month: "",     // YYYY-MM currently shown
    selectedDay: "",  // local day key of the chosen date
    busy: false,
    offerPopup: false,
    notice: "",
    errors: {},

    slots: [],
    questions: [],
    answers: {},
    // sms_consent starts false and is never pre-ticked — see the panel template.
    form: { name: "", email: "", start_time: "", sms_consent: false },
    tz: "America/New_York",

    init() {
      /* A click that landed before Alpine finished booting still counts. app.js is a
         plain script but Alpine is deferred, so the CTA can fire open-scheduler with
         nothing listening yet — and the button would look simply broken. */
      if (window.__schedulerPending) this.$nextTick(() => this.openPanel());

      this.month = this.monthKey(new Date());

      /* The VISITOR's zone, not the company's. Everything on the All Pro Charter side
         stays America/New_York, but a grid rendered in Eastern to someone in Denver is
         a call they will miss by two hours. They can override it below. */
      try {
        this.tz = Intl.DateTimeFormat().resolvedOptions().timeZone || this.tz;
      } catch (e) {
        /* Fall back to Eastern rather than guessing. */
      }
    },

    openPanel() {
      window.__schedulerPending = false;
      this.isOpen = true;
      document.body.style.overflow = "hidden";
      this.$nextTick(() => {
        this.syncTimezonePicker();
        if (window.initPhoneInputs) window.initPhoneInputs(this.$el);
      });
      // "error" retries too: a visitor who hit a blip and came back deserves a fresh
      // attempt rather than the state that failed them.
      if (this.phase === "idle" || this.phase === "error") this.loadInitial();
    },

    closePanel() {
      this.isOpen = false;
      document.body.style.overflow = "";
    },

    /* ---- timezone -------------------------------------------------------- */

    /** Point the Tom Select at the detected zone, adding it if we don't list it. */
    syncTimezonePicker() {
      const select = this.$el.querySelector('select[name="timezone"]');
      if (!select) return;
      const ts = select.tomselect;
      const known = Array.from(select.options).some((o) => o.value === this.tz);
      if (!known && this.tz) {
        if (ts) ts.addOption({ value: this.tz, text: this.tz });
        else select.add(new Option(this.tz, this.tz));
      }
      if (ts) ts.setValue(this.tz, true);
      else select.value = this.tz;

      if (!select._schedBound) {
        select._schedBound = true;
        // Tom Select fires `change` on the original select it wraps.
        select.addEventListener("change", () => {
          this.tz = select.value || this.tz;
        });
      }
    },

    /** `date` formatted in the visitor's zone, falling back if the zone is unusable. */
    fmt(date, options) {
      try {
        return new Intl.DateTimeFormat("en-US", { timeZone: this.tz, ...options }).format(date);
      } catch (e) {
        return new Intl.DateTimeFormat("en-US", options).format(date);
      }
    },

    /** "EDT" / "PST" for a given moment — never a fixed string. */
    abbr(date) {
      try {
        const parts = new Intl.DateTimeFormat("en-US", {
          timeZone: this.tz,
          timeZoneName: "short",
        }).formatToParts(date);
        return (parts.find((p) => p.type === "timeZoneName") || {}).value || "";
      } catch (e) {
        return "";
      }
    },

    get tzAbbreviation() {
      const first = this.slots.length ? new Date(this.slots[0].start) : new Date();
      return this.abbr(first);
    },

    /* ---- slots ------------------------------------------------------------ */

    get dayGroups() {
      const groups = new Map();
      for (const slot of this.slots) {
        const when = new Date(slot.start);
        if (isNaN(when.getTime())) continue;
        /* Group AFTER converting to the visitor's zone. Grouping on the UTC date puts
           an 8pm Eastern slot under tomorrow's heading for anyone west of us. */
        const key = this.dayKey(when);
        if (!groups.has(key)) {
          groups.set(key, {
            key,
            label: this.fmt(when, { weekday: "long", month: "short", day: "numeric" }),
            abbr: this.abbr(when),
            slots: [],
          });
        }
        groups.get(key).slots.push({
          start: slot.start,
          held: !!slot.held,
          label: this.fmt(when, { hour: "numeric", minute: "2-digit" }),
        });
      }
      return Array.from(groups.values());
    },

    get slotCount() {
      return this.slots.length;
    },

    /* ---- the calendar --------------------------------------------------- */

    /** YYYY-MM for a Date, read in the VISITOR's zone — the month they see. */
    monthKey(date) {
      const parts = this.fmt(date, { year: "numeric", month: "2-digit" }).split("/");
      return parts.length === 2 ? `${parts[1]}-${parts[0]}` : "";
    },

    /** Stable local-day key, so a slot lands on the day the visitor would call it. */
    dayKey(date) {
      return this.fmt(date, { year: "numeric", month: "2-digit", day: "2-digit" });
    },

    get weekdayNames() {
      return ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
    },

    get monthLabel() {
      const [y, m] = this.month.split("-").map(Number);
      if (!y) return "";
      return new Intl.DateTimeFormat("en-US", { month: "long", year: "numeric" }).format(
        new Date(y, m - 1, 1)
      );
    },

    /** Local days with at least one slot nobody else is holding. */
    get openDays() {
      const open = {};
      for (const group of this.dayGroups) {
        if (group.slots.some((s) => !s.held)) open[group.key] = group.slots;
      }
      return open;
    },

    /* Scoped to the month on screen. The fetched window is padded a day at each end so
       local days at the boundaries are complete, and those neighbours must not count
       towards "is there anything to click here". */
    get openDayCount() {
      return this.monthCells.filter((c) => c.day && c.open).length;
    },

    /* Leading blanks then one cell per day, so the 1st sits under its real weekday.
       `open` drives both the styling and the disabled state — the client works Monday
       to Thursday, and a calendar that let you click Friday would be lying. */
    get monthCells() {
      const [y, m] = this.month.split("-").map(Number);
      if (!y) return [];
      const first = new Date(y, m - 1, 1);
      const daysInMonth = new Date(y, m, 0).getDate();
      const cells = [];
      for (let i = 0; i < first.getDay(); i += 1) cells.push({ day: null });
      const open = this.openDays;
      for (let day = 1; day <= daysInMonth; day += 1) {
        const key = `${String(m).padStart(2, "0")}/${String(day).padStart(2, "0")}/${y}`;
        cells.push({
          day,
          key,
          open: !!open[key],
          label: new Intl.DateTimeFormat("en-US", {
            weekday: "long",
            month: "long",
            day: "numeric",
          }).format(new Date(y, m - 1, day)),
        });
      }
      return cells;
    },

    /** No arrowing back into months that are already over. */
    get canGoBack() {
      return this.month > this.monthKey(new Date());
    },

    stepMonth(delta) {
      const [y, m] = this.month.split("-").map(Number);
      const moved = new Date(y, m - 1 + delta, 1);
      this.month = `${moved.getFullYear()}-${String(moved.getMonth() + 1).padStart(2, "0")}`;
      this.selectedDay = "";
      this.view = "date";
    },

    shiftMonth(delta) {
      if (delta < 0 && !this.canGoBack) return;
      this.stepMonth(delta);
      // A month the visitor asked for by name is shown as-is, empty or not — skipping
      // past it would fight the arrow they just pressed.
      this.loadMonth();
    },

    chooseDay(cell) {
      if (!cell.open) return;
      this.selectedDay = cell.key;
      this.view = "time";
    },

    get selectedDaySlots() {
      const group = this.dayGroups.find((g) => g.key === this.selectedDay);
      return group ? group.slots : [];
    },

    get selectedDayLabel() {
      const group = this.dayGroups.find((g) => g.key === this.selectedDay);
      return group ? group.label : "";
    },

    /** YYYY-MM of the earliest day that has something bookable, from what we hold. */
    firstOpenMonth() {
      for (const group of this.dayGroups) {
        if (group.slots.some((s) => !s.held)) {
          // dayKey is en-US MM/DD/YYYY.
          return `${group.key.slice(6, 10)}-${group.key.slice(0, 2)}`;
        }
      }
      return "";
    },

    /* Opening fetches one rolling window rather than a calendar month, because the
       month to show is not known until we know where the availability is. Late in a
       month there is often nothing left in it, and opening onto a dead calendar reads
       as "they have no availability" rather than "look at next month" — but asking for
       this month, discovering it is spent and then asking for the next one cost a
       second round trip and several wasted seconds. One window answers both. */
    async loadInitial() {
      this.phase = "loading";
      try {
        const resp = await fetch(`${this.slotsUrl}?days=${this.days}`, {
          headers: { Accept: "application/json" },
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) return this.fallbackToPopup();
        this.slots = data.slots || [];
        this.questions = data.questions || [];
        this.phase = "ready";
        this.month = this.firstOpenMonth() || this.monthKey(new Date());
        this.$nextTick(() => {
          if (window.initFlatpickr) window.initFlatpickr(this.$el);
        });
      } catch (e) {
        this.fallbackToPopup();
      }
    },

    /** One calendar month, for the arrows. */
    async loadMonth() {
      this.phase = "loading";
      try {
        const resp = await fetch(`${this.slotsUrl}?month=${encodeURIComponent(this.month)}`, {
          headers: { Accept: "application/json" },
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) return this.fallbackToPopup();
        this.slots = data.slots || [];
        this.questions = data.questions || [];
        this.phase = "ready";
        // The question fields only exist now, so their pickers are initialised now.
        this.$nextTick(() => {
          if (window.initFlatpickr) window.initFlatpickr(this.$el);
        });
      } catch (e) {
        this.fallbackToPopup();
      }
    },

    /** Hand over to Calendly's own popup. Returns false if its script never loaded. */
    fallbackToPopup() {
      if (window.openCalendlyPopup && window.openCalendlyPopup()) {
        // Back to idle before closing, or reopening our panel later would find it
        // parked on whatever phase failed — a loading skeleton that never resolves.
        this.phase = "idle";
        this.closePanel();
        return true;
      }
      this.phase = "error";
      return false;
    },

    pick(slot) {
      if (slot.held) return;
      this.form.start_time = slot.start;
      this.errors = { ...this.errors, start_time: "" };
      const when = new Date(slot.start);
      this.notice = `${this.fmt(when, {
        weekday: "long",
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
      })} ${this.abbr(when)}`;
    },

    /* ---- questions --------------------------------------------------------- */

    /* Built from the LIVE event type, never a copy of it: `position` is what Calendly
       matches answers on and the client can reorder his questions whenever he likes.
       A phone_number question is not shown — it is answered from the phone field we
       already validate, rather than asking for the same number twice (decision 1). */
    get visibleQuestions() {
      return (this.questions || [])
        .filter((q) => q.type !== "phone_number")
        .map((q) => ({
          position: q.position,
          label: q.name || "",
          required: !!q.required,
          /* Calendly has no date question type, so the intent has to be read off the
             wording. Getting it wrong costs a picker, not an answer — the field still
             submits whatever the visitor typed. */
          kind: q.type === "text" ? "text" : /date/i.test(q.name || "") ? "date" : "line",
        }));
    },

    /* ---- submit ------------------------------------------------------------ */

    phone() {
      const el = this.$refs.phone;
      if (!el) return "";
      return window.phoneValue ? window.phoneValue(el) : el.value.trim();
    },

    localErrors(phone) {
      const found = {};
      if (!this.form.name.trim()) found.name = "Tell us your name.";
      if (!this.form.email.trim() || !this.form.email.includes("@")) {
        found.email = "We need a valid email for the calendar invite.";
      }
      if (!phone) found.phone = "We call you at this number, so we need one.";
      else if (window.phoneIsValid && !window.phoneIsValid(this.$refs.phone)) {
        found.phone = "That number doesn't look right.";
      }
      if (!this.form.start_time) found.start_time = "Pick a time.";
      for (const q of this.visibleQuestions) {
        if (q.required && !String(this.answers[q.position] || "").trim()) {
          found["q" + q.position] = `${q.label} is required.`;
        }
      }
      return found;
    },

    async submit() {
      if (this.busy) return;
      this.offerPopup = false;
      const phone = this.phone();
      const found = this.localErrors(phone);
      if (Object.keys(found).length) {
        this.errors = found;
        return;
      }

      this.errors = {};
      this.busy = true;
      const body = {
        name: this.form.name.trim(),
        email: this.form.email.trim(),
        phone,
        timezone: this.tz,
        start_time: this.form.start_time,
        // So a lost race hands back the month we are showing, not a rolling window.
        month: this.month,
      };
      if (this.form.sms_consent) body.sms_consent = "1";
      for (const q of this.questions) {
        body["q" + q.position] =
          q.type === "phone_number" ? phone : String(this.answers[q.position] || "");
      }

      try {
        const resp = await fetch(this.bookUrl, {
          method: "POST",
          headers: {
            "Content-Type": "application/x-www-form-urlencoded",
            "X-CSRFToken": getCookie("csrftoken"),
            Accept: "application/json",
          },
          body: new URLSearchParams(body),
        });
        const data = await resp.json().catch(() => ({}));

        if (resp.ok && data.redirect) {
          this.phase = "done";
          window.location.href = data.redirect;
          return;
        }
        if (resp.status === 409) {
          /* The slot went — either to another of our visitors or to someone booking on
             calendly.com. The server sends a refreshed grid so this costs no round
             trip, and the selection is cleared so the next click is deliberate. */
          this.slots = data.slots && data.slots.length ? data.slots : this.slots;
          this.form.start_time = "";
          this.phase = "ready";
          // If that was the day's last slot, the day is gone — send them back to the
          // calendar rather than leaving them staring at an empty column.
          if (!this.selectedDaySlots.length) this.view = "date";
          this.notice = data.error || "That time just went — here are the next available.";
          return;
        }
        if (resp.status === 400 && data.errors) {
          this.errors = this.serverErrors(data.errors);
          return;
        }
        this.notice = data.error || "We couldn't complete that booking.";
        this.offerPopup = true;
      } catch (e) {
        this.notice = "Network error — please try again.";
        this.offerPopup = true;
      } finally {
        this.busy = false;
      }
    },

    /** Server error keys onto field keys; `questions` arrives as a list. */
    serverErrors(errors) {
      const mapped = {};
      for (const [key, value] of Object.entries(errors)) {
        mapped[key] = Array.isArray(value) ? value.join(" ") : value;
      }
      return mapped;
    },
  };
}
window.scheduleBooking = scheduleBooking;

/** Opened from the CTA on any public page — see templates/public/_call_cta.html.
 *  The flag covers the gap before deferred Alpine has bound its listener; the panel
 *  clears it as soon as it opens, whichever of the two got there first. */
function openScheduler() {
  window.__schedulerPending = true;
  window.dispatchEvent(new CustomEvent("open-scheduler"));
}
window.openScheduler = openScheduler;
