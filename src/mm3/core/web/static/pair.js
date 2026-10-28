// Implements CORE-254 (Core spec 0.11, §10.1 Security): send the display token
// from the URL fragment to /pair. The fragment never reaches the server in the
// request line, and it is removed from the address bar before posting.
(() => {
  "use strict";
  const token = decodeURIComponent(window.location.hash.replace(/^#/, ""));
  history.replaceState(null, "", window.location.pathname);
  const form = document.getElementById("mm3-pair");
  if (!token || !form) return;
  form.elements.namedItem("token").value = token;
  form.submit();
})();
