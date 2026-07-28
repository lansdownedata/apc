/**
 * Tailwind config — lifted verbatim from the inline `tailwind.config` blocks that
 * previously lived in base.html / public/base_public.html / registration/login.html.
 *
 * Colours are space-separated RGB *channels* in CSS variables (see static/css/app.css)
 * so `<alpha-value>` keeps alpha modifiers working: text-ink/80, bg-paper/80, …
 * Flip the whole app by toggling <html data-theme="dark">.
 */
const plugin = require("tailwindcss/plugin");

module.exports = {
  content: [
    "./templates/**/*.html",
    "./apps/**/*.html", // app-level templates (integrations/podium_callback.html)
    "./apps/**/*.py", // form widget attrs, e.g. {"class": "field w-full"}
    "./static/js/**/*.js", // modal/toast markup built in app.js
  ],
  darkMode: ["selector", '[data-theme="dark"]'],
  theme: {
    extend: {
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
        display: ["Fraunces", "Georgia", "serif"],
        mono: ["Spline Sans Mono", "ui-monospace", "monospace"],
      },
      colors: {
        /* sidebar frame — always dark, both themes */
        charcoal: "#14181f",
        charcoal2: "#1b212b",
        charcoal3: "#222b37",
        onyx: "#17191D",
        silver: "#B9B4B7",
        /* theme-aware tokens (flip with <html data-theme>); channels live in app.css */
        gold: "rgb(var(--gold) / <alpha-value>)",
        goldd: "rgb(var(--gold-deep) / <alpha-value>)",
        goldl: "rgb(var(--gold-soft) / <alpha-value>)",
        ink: "rgb(var(--text) / <alpha-value>)",
        muted: "rgb(var(--muted) / <alpha-value>)",
        paper: "rgb(var(--bg) / <alpha-value>)",
        surface: "rgb(var(--surface) / <alpha-value>)",
        surface2: "rgb(var(--surface-2) / <alpha-value>)",
        line: "rgb(var(--border) / <alpha-value>)",
      },
    },
  },
  plugins: [
    /**
     * Gold-tinted wash for active rows and hover states.
     *
     * Lives here rather than in app.css because it is used almost exclusively as
     * `hover:wash-gold` — a plain CSS class can't produce a variant, so the app.css
     * version silently did nothing at all 18 call sites. Registering it as a real
     * utility makes every variant (hover:, focus:, group-hover:, …) work.
     */
    plugin(({ addUtilities }) => {
      addUtilities({
        ".wash-gold": {
          background:
            "linear-gradient(90deg, rgb(var(--gold) / .10), rgb(var(--gold) / .03))",
        },
      });
    }),
  ],
};
