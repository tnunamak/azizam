/* Shared navigation and the one real installation tab. No gateway requests. */
(() => {
  "use strict";
  const docsHeader = document.querySelector(".docs-top");
  if (docsHeader) {
    const search = docsHeader.querySelector(".search-open");
    const header = document.createElement("header");
    header.className = "site-header";
    header.innerHTML = "  <a class=\"brand\" href=\"/showcase/\" ><svg class=\"mark\" viewBox=\"0 0 48 48\" fill=\"currentColor\" aria-hidden=\"true\" focusable=\"false\"><path d=\"M22.83 18.05L23.1 18.74L23.43 19.39L23.83 20.01L24.55 20.85L25.1 21.34L26.01 21.97L26.66 22.3L27.71 22.67L28.43 22.82L29.16 22.9L29.89 22.9L30.99 22.76L31.7 22.57L32.58 22.23L33.42 21.81L34.21 21.3L34.96 20.72L35.64 20.06L36.25 19.34L36.79 18.56L37.25 17.73L37.62 16.85L37.9 15.93L38.01 15.46L38.15 14.5L38.19 13.53L38.14 12.56L37.98 11.59L37.68 10.5L37.46 9.9L36.92 8.74L36.61 8.18L35.89 7.1L35.07 6.1L34.14 5.18L33.64 4.76L32.57 4L31.42 3.35L30.21 2.81L29.58 2.6L28.29 2.26L26.96 2.06L25.62 2L24.94 2.02L23.59 2.18L22.92 2.31L22.26 2.47L21.41 2.74L20.58 3.06L19.76 3.41L18.96 3.82L18.18 4.26L17.43 4.75L16.7 5.27L16 5.83L15.33 6.44L14.69 7.07L14.08 7.74L13.51 8.45L12.98 9.18L12.48 9.94L12.03 10.73L11.62 11.54L11.25 12.37L10.92 13.23L10.65 14.1L10.41 14.98L10.23 15.88L10.09 16.78L10 17.7L9.96 18.61L9.97 19.53L10.03 20.45L10.13 21.37L10.29 22.27L10.49 23.17L10.74 24.06L11.15 25.24L11.62 26.39L12.15 27.52L12.75 28.62L13.4 29.68L14.11 30.71L14.88 31.7L15.7 32.64L16.57 33.54L17.48 34.39L18.44 35.18L19.45 35.93L21.61 37.36L22.7 38.17L23.74 39.03L24.74 39.94L25.69 40.9L26.58 41.92L27.42 42.97L28.56 44.53L28.97 44.93L29.42 45.28L29.92 45.56L30.45 45.78L31.01 45.93L31.58 46L32.15 46L32.72 45.92L33.28 45.77L33.81 45.55L34.3 45.26L34.76 44.91L35.16 44.5L35.51 44.04L35.79 43.54L36.01 43.01L36.16 42.46L36.23 41.89L36.23 41.31L36.15 40.74L36 40.19L35.78 39.66L35.49 39.16L34.43 37.69L33.31 36.27L32.11 34.92L30.84 33.62L29.5 32.4L28.11 31.25L26.65 30.17L24.49 28.74L23.87 28.28L23.27 27.78L22.7 27.25L22.16 26.7L21.64 26.11L21.16 25.5L20.71 24.87L20.29 24.21L19.91 23.53L19.57 22.83L19.26 22.11L18.84 20.93L18.62 20.02L18.55 19.56L18.47 18.61L18.46 18.14L18.53 17.19L18.6 16.72L18.81 15.79L18.95 15.33L19.3 14.43L19.51 14L19.99 13.16L20.27 12.76L20.89 11.99L21.22 11.64L21.96 10.98L22.35 10.67L23.18 10.13L24.07 9.67L24.54 9.47L25.1 9.26L25.69 9.11L26.29 9.02L26.91 8.99L27.53 9.03L28.16 9.13L28.77 9.3L29.37 9.54L29.95 9.84L30.49 10.21L31 10.64L31.46 11.12L32.06 11.94L32.38 12.54L32.6 13.03L32.78 13.61L32.85 14.02L32.87 14.43L32.81 15.06L32.71 15.47L32.57 15.88L32.26 16.45L32.01 16.8L31.71 17.12L31.2 17.53L30.63 17.85L30.22 18.01L29.88 18.09L29.53 18.12L29.18 18.09L28.61 17.93L28.11 17.62L27.78 17.29L27.52 16.89L27.27 16.28L27.12 16L26.93 15.75L26.71 15.53L26.47 15.33L26.2 15.17L25.91 15.05L25.61 14.97L25.3 14.92L24.99 14.92L24.68 14.95L24.37 15.03L24.08 15.15L23.81 15.3L23.56 15.49L23.33 15.71L23.14 15.95L22.98 16.22L22.85 16.51L22.77 16.81L22.72 17.12L22.72 17.43L22.76 17.74Z\"/></svg>Azizam</a>\n  <button class=\"site-menu\" type=\"button\" aria-controls=\"site-nav\" aria-expanded=\"false\" hidden>Menu</button>\n  <nav id=\"site-nav\" class=\"site-nav\" aria-label=\"Main navigation\">\n    <a href=\"/showcase/examples/\" >Examples</a>\n    <a href=\"/showcase/docs/\">Docs</a>\n    <a href=\"/showcase/docs/api/\">API</a>\n    <a class=\"primary-link\" href=\"/showcase/docs/start/install/\">Get started</a>\n  </nav>";
    const docsLink = header.querySelector('a[href="/showcase/docs/"]');
    const apiLink = header.querySelector('a[href="/showcase/docs/api/"]');
    if (location.pathname === "/showcase/docs/") docsLink.setAttribute("aria-current", "page");
    if (location.pathname === "/showcase/docs/api/") apiLink.setAttribute("aria-current", "page");
    if (location.pathname === "/showcase/docs/start/install/") header.querySelector(".primary-link").setAttribute("aria-current", "page");
    if (search) header.querySelector("nav").append(search);
    docsHeader.replaceWith(header);
  }
  const menu = document.querySelector(".site-menu");
  const nav = document.getElementById("site-nav");
  if (menu && nav) {
    const phone = matchMedia("(max-width: 639px)");
    function close() {
      menu.setAttribute("aria-expanded", "false");
      nav.hidden = phone.matches;
    }
    menu.hidden = false;
    close();
    phone.addEventListener("change", close);
    menu.addEventListener("click", () => {
      const open = menu.getAttribute("aria-expanded") !== "true";
      menu.setAttribute("aria-expanded", String(open));
      nav.hidden = !open;
    });
    nav.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && phone.matches) {
        close(); menu.focus();
      }
    });
    nav.addEventListener("click", (event) => {
      if (event.target.closest("a") && phone.matches) close();
    });
  }
  const tab = document.getElementById("compose-tab");
  if (tab) {
    // There is exactly one supported installation tab; storage failure is harmless.
    try { localStorage.setItem("azizam.site.installTab", "compose"); } catch {}
  }
  const copy = document.getElementById("copy-command");
  if (copy) {
    copy.hidden = false;
    copy.addEventListener("click", async () => {
      const status = document.getElementById("copy-status");
      try {
        await navigator.clipboard.writeText(document.getElementById("compose-command").textContent);
        status.textContent = "Command copied";
        copy.textContent = "Copied";
      } catch {
        status.textContent = "Copy failed. Select the command and copy it.";
        copy.textContent = "Select and copy";
      }
    });
  }
})();
