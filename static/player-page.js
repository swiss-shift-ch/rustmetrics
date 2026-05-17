// Player-Profil-Page: Auth-State erkennen.
// Wenn der User eingeloggt ist, blenden wir die "Sign in"-Elemente aus
// und zeigen stattdessen die data-auth-only Hinweise.
// Wird CSP-konform als externes Script geladen (script-src 'self').
(function () {
  fetch("/api/me", { credentials: "same-origin" })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (me) {
      if (!me || !me.authenticated) return;
      document.querySelectorAll("[data-anon-only]").forEach(function (el) {
        el.style.display = "none";
      });
      document.querySelectorAll("[data-auth-only]").forEach(function (el) {
        el.style.display = "";
      });
    })
    .catch(function () {
      // Offline / API down: anon-Default bleibt sichtbar, das ist OK.
    });
})();
