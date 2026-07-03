// Tailwind v4 is wired in as a PostCSS plugin. No tailwind.config.js needed
// for the defaults; utilities come from the `@import "tailwindcss"` in globals.css.
const config = {
  plugins: {
    "@tailwindcss/postcss": {},
  },
};

export default config;
