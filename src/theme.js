/* Applies the saved light/dark choice before the page paints (loaded in
 * <head>; an inline script would need a CSP hash). No choice = follow the
 * system. app.js owns the theme button. */
'use strict';
try {
  const t = localStorage.getItem('data-explorer:theme');
  if (t === 'light' || t === 'dark') document.documentElement.dataset.theme = t;
} catch {
  /* storage unavailable: follow the system */
}
