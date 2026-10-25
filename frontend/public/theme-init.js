// Applies the saved theme (light / dark / system) and colour theme before the app renders, so the page never flashes the wrong colours.
(function () {
  var pref = "system", accent = "aurora";
  try {
    pref = localStorage.getItem("swarmos.theme") || "system";
    accent = localStorage.getItem("swarmos.accent") || "aurora";
  } catch (e) { /* storage blocked */ }
  var dark = pref === "dark" || (pref !== "light" && !(window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches));
  var root = document.documentElement;
  root.setAttribute("data-theme", dark ? "dark" : "light");
  root.setAttribute("data-accent", accent);
  root.style.colorScheme = dark ? "dark" : "light";
})();
