// Progressive enhancement only: every page works without this script.
"use strict";

// Disable the submitter once a form is on its way, so a slow network cannot queue a second tap.
document.addEventListener("submit", (event) => {
  const button = event.submitter;
  if (!button || event.defaultPrevented) return;
  // Deferred so the browser has already captured the form data set.
  setTimeout(() => {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
  });
});

// Returning through the back/forward cache must not leave buttons stuck.
window.addEventListener("pageshow", (event) => {
  if (!event.persisted) return;
  for (const button of document.querySelectorAll("button[aria-busy]")) {
    button.disabled = false;
    button.removeAttribute("aria-busy");
  }
});

// Elapsed timers count locally between server updates.
function tick() {
  for (const el of document.querySelectorAll("[data-elapsed]")) {
    el.dataset.since ??= String(Date.now() - Number(el.dataset.elapsed) * 1000);
    const seconds = Math.floor((Date.now() - Number(el.dataset.since)) / 1000);
    el.textContent = `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
  }
}
if (document.querySelector("[data-elapsed]")) setInterval(tick, 1000);

// Pages in progress poll themselves instead of reloading: the server-rendered <main> is swapped
// only when its data-key changes, keeping scroll position and opened <details>. Polling pauses
// while the tab is hidden. Without this script, a <noscript> meta refresh reloads the page.
const main = document.querySelector("main[data-refresh]");
if (main) {
  const every = Number(main.dataset.refresh) * 1000;
  let timer;
  let inflight = false;

  const swap = (doc, next) => {
    const open = [...main.querySelectorAll("details")].map((d) => d.open);
    // Move the parsed nodes over rather than re-serialising them through innerHTML.
    main.replaceChildren(...document.adoptNode(next).childNodes);
    main.dataset.key = next.dataset.key;
    main.querySelectorAll("details").forEach((d, i) => { if (open[i]) d.open = true; });
    document.title = doc.title;
    tick();
  };

  const schedule = () => {
    clearTimeout(timer);
    timer = document.hidden ? undefined : setTimeout(poll, every);
  };

  async function poll() {
    if (inflight) return;
    inflight = true;
    try {
      const res = await fetch(location.href, { cache: "no-store", headers: { accept: "text/html" } });
      if (res.ok) {
        const doc = new DOMParser().parseFromString(await res.text(), "text/html");
        const next = doc.querySelector("main");
        // Finished, redirected (report ready, session expired) or no longer in progress:
        // let the browser navigate for real, including any fragment in the redirect.
        if (res.redirected || !next?.hasAttribute("data-refresh")) return location.reload();
        if (next.dataset.key !== main.dataset.key) swap(doc, next);
      }
    } catch (error) {
      // A dropped connection on a phone is expected; the next round retries.
      console.debug("progress poll failed", error);
    } finally {
      inflight = false;
    }
    schedule();
  }

  document.addEventListener("visibilitychange", () => (document.hidden ? schedule() : poll()));
  schedule();
}
