/* ==========================================================================
   All Pro Charter — shared Stripe Payment Element config
   - apcPay.appearance(mode)          → 'light' | 'dark' Stripe appearance object
   - apcPay.cardFields({...})         → our own card form: name, number, expiry, CVV, ZIP
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
    placeholder: "#9AA1AB",
    danger: "#B4453A",
    ring: "rgba(199, 162, 78, 0.30)",
  };
  var DARK = {
    primary: "#CDAA5A",
    background: "#1A1D24",
    text: "#EBE9E3",
    border: "#2A2F38",
    placeholder: "#767D88",
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

  /* The style for the individual card elements. They predate the appearance API and
     take their own `style` object, so the tokens above are applied here by hand. */
  function cardStyle(mode) {
    var t = mode === "dark" ? DARK : LIGHT;
    return {
      base: {
        color: t.text,
        fontFamily: "Inter, system-ui, sans-serif",
        fontSize: "14px",
        fontSmoothing: "antialiased",
        iconColor: t.primary,
        "::placeholder": { color: t.placeholder },
      },
      invalid: { color: t.danger, iconColor: t.danger },
    };
  }

  /* Our own card form, instead of the tabbed Payment Element.

     The Payment Element renders whatever the Stripe ACCOUNT has switched on: a Bank
     (ACH) tab, a "save my info with Link" block asking for an email and a mobile
     number, and a Country select. None of it belongs on a checkout that takes a card,
     and none of it can be configured away — it rides on top of the card tab.
     cardNumber / cardExpiry / cardCvc cannot render any of it.

     The name and the ZIP are OUR inputs, which is the point: the layout is ours, the
     name is required (the client asks for it), and the ZIP stays optional — the
     Payment Element's own address block makes it mandatory for US cards.

     `root` is any element containing [data-card-number], [data-card-expiry],
     [data-card-cvc], [data-card-name] and optionally [data-card-zip].
  */
  function cardFields(opts) {
    var stripe = opts.stripe;
    var root = opts.root;
    if (!stripe || !root) return null;

    var style = cardStyle(opts.appearanceMode || "light");
    var elements = stripe.elements();
    var parts = {
      cardNumber: elements.create("cardNumber", { style: style, showIcon: true }),
      cardExpiry: elements.create("cardExpiry", { style: style }),
      cardCvc: elements.create("cardCvc", { style: style }),
    };
    var complete = { cardNumber: false, cardExpiry: false, cardCvc: false };

    function find(attr) {
      return root.querySelector("[" + attr + "]");
    }
    function value(attr) {
      var el = find(attr);
      return el && el.value ? el.value.trim() : "";
    }

    Object.keys(parts).forEach(function (key) {
      var slot = find("data-" + key.replace(/[A-Z]/g, function (c) {
        return "-" + c.toLowerCase();
      }));
      if (slot) parts[key].mount(slot);
      parts[key].on("change", function (event) {
        complete[event.elementType] = event.complete;
        if (opts.onError) opts.onError(event.error ? event.error.message : "");
      });
    });

    return {
      /* What createPaymentMethod / confirmCardPayment want. Throws rather than letting
         Stripe take a nameless card — the name is the one field we validate ourselves. */
      paymentMethod: function () {
        var name = value("data-card-name");
        if (!name) throw new Error("Enter the cardholder's name as printed on the card.");
        var details = { name: name };
        var zip = value("data-card-zip");
        // Omitted rather than sent empty: a blank postal_code fails Stripe's own check.
        if (zip) details.address = { postal_code: zip };
        return { card: parts.cardNumber, billing_details: details };
      },
      isComplete: function () {
        return complete.cardNumber && complete.cardExpiry && complete.cardCvc;
      },
      setTheme: function (mode) {
        var next = cardStyle(mode);
        Object.keys(parts).forEach(function (key) {
          parts[key].update({ style: next });
        });
      },
      clear: function () {
        Object.keys(parts).forEach(function (key) {
          parts[key].clear();
        });
      },
    };
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
    var card = cardFields({
      stripe: stripe,
      root: mountEl,
      appearanceMode: "light",
      onError: function (message) {
        if (errorEl) errorEl.textContent = message;
      },
    });
    if (!card) return;

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

      Promise.resolve()
        .then(function () {
          var method = card.paymentMethod();  // throws when the name is blank
          return postForm(opts.intentUrl, {}).then(function (created) {
            return stripe.confirmCardPayment(created.client_secret, {
              payment_method: method,
              return_url: opts.returnUrl,
            });
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
    cardStyle: cardStyle,
    cardFields: cardFields,
    mount: mount,
  };
})();
