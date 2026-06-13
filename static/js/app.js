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
