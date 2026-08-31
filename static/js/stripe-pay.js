/* ==========================================================================
   All Pro Charter — shared Stripe Payment Element config
   - apcPay.appearance(mode)          → 'light' | 'dark' Stripe appearance object
   - apcPay.elementsOptions({...})    → card-only Elements options, wallets off
   - apcPay.mount({...})              → vanilla controller for the public pay pages

   Loaded BEFORE app.js in every shell. `adminCardPay` (app.js) consumes the first
   two; the public pay page uses mount(). Card data lives only in Stripe's iframe —
   nothing here ever sees a PAN.
   ========================================================================== */
(function () {
  "use strict";

  // Charcoal/gold tokens, kept in step with static/css/app.css (:root and
  // :root[data-theme="dark"]). Stripe's appearance API wants literal hex.
  var LIGHT = {
    primary: "#C7A24E",
    background: "#FFFFFF",
    text: "#17191D",
    border: "#E5E2D9",
    danger: "#B4453A",
    ring: "rgba(199, 162, 78, 0.30)",
  };
  var DARK = {
    primary: "#CDAA5A",
    background: "#1A1D24",
    text: "#EBE9E3",
    border: "#2A2F38",
    danger: "#E88C82",
    ring: "rgba(205, 170, 90, 0.38)",
  };

  function appearance(mode) {
    var t = mode === "dark" ? DARK : LIGHT;
    return {
      theme: mode === "dark" ? "night" : "stripe",
      variables: {
        colorPrimary: t.primary,
        colorBackground: t.background,
        colorText: t.text,
        colorDanger: t.danger,
        fontFamily: "Inter, system-ui, sans-serif",
        borderRadius: "8px",
        spacingUnit: "4px",
      },
      rules: {
        ".Input": { border: "1px solid " + t.border, boxShadow: "none" },
        ".Input:focus": {
          border: "1px solid " + t.primary,
          boxShadow: "0 0 0 3px " + t.ring,
        },
        ".Label": { fontWeight: "500" },
      },
    };
  }

  // Card-only, wallets off. mode 'payment' carries amount + setupFutureUsage;
  // mode 'setup' (staff save-a-card) takes neither.
  function elementsOptions(opts) {
    opts = opts || {};
    var mode = opts.mode || "payment";
    var out = {
      mode: mode,
      currency: "usd",
      paymentMethodTypes: ["card"],
      appearance: appearance(opts.appearanceMode || "light"),
    };
    if (mode === "payment") {
      out.amount = Math.max(Math.round(opts.amount || 0), 50);
      out.setupFutureUsage = "off_session";
    }
    return out;
  }

  function paymentElementOptions() {
    return { wallets: { applePay: "never", googlePay: "never" } };
  }

  function readCookie(name) {
    var m = document.cookie.match("(^|;)\\s*" + name + "\\s*=\\s*([^;]+)");
    return m ? decodeURIComponent(m.pop()) : "";
  }

  function postForm(url, data) {
    var body = new URLSearchParams(data || {});
    return fetch(url, {
      method: "POST",
      headers: {
        "X-CSRFToken": readCookie("csrftoken"),
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded",
      },
      body: body.toString(),
    }).then(function (r) {
      return r.json().then(function (j) {
        if (!r.ok || j.ok === false) {
          throw new Error(j.error || "That did not go through. Please try again.");
        }
        return j;
      });
    });
  }

  /* The public pay page: one fixed amount decided server-side, one Pay button.
     el ids default to #card-mount / #pay-button / #pay-error / #pay-busy.
     opts: { pk, amount (cents), intentUrl, completeUrl, returnUrl, onDone } */
  function mount(opts) {
    var mountEl = opts.mountEl || document.getElementById("card-mount");
    var button = opts.button || document.getElementById("pay-button");
    var errorEl = opts.errorEl || document.getElementById("pay-error");
    if (!mountEl || !button || typeof Stripe === "undefined") return;

    var stripe = Stripe(opts.pk);
    var elements = stripe.elements(
      elementsOptions({ mode: "payment", amount: opts.amount, appearanceMode: "light" })
    );
    elements.create("payment", paymentElementOptions()).mount(mountEl);

    var busy = false;
    function setBusy(on) {
      busy = on;
      button.disabled = on;
      button.classList.toggle("is-busy", on);
    }
    function fail(message) {
      if (errorEl) errorEl.textContent = message;
      setBusy(false);
    }

    button.addEventListener("click", function () {
      if (busy) return;
      if (errorEl) errorEl.textContent = "";
      setBusy(true);

      elements
        .submit()
        .then(function (r) {
          if (r.error) throw new Error(r.error.message);
          return postForm(opts.intentUrl, {});
        })
        .then(function (created) {
          return stripe.confirmPayment({
            elements: elements,
            clientSecret: created.client_secret,
            confirmParams: { return_url: opts.returnUrl },
            redirect: "if_required",
          });
        })
        .then(function (result) {
          if (result.error) throw new Error(result.error.message);
          // No redirect was needed — reconcile now. (A 3-D Secure redirect never
          // reaches here; quote_deposit_success reconciles that path on return.)
          return postForm(opts.completeUrl, {
            payment_intent_id: result.paymentIntent.id,
          });
        })
        .then(function () {
          if (opts.onDone) opts.onDone();
          else window.location.assign(opts.returnUrl);
        })
        .catch(function (err) {
          fail(err.message || "Could not process the payment.");
        });
    });
  }

  window.apcPay = {
    appearance: appearance,
    elementsOptions: elementsOptions,
    paymentElementOptions: paymentElementOptions,
    mount: mount,
  };
})();
