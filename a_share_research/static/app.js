// Progressive enhancement only: every page works without this script.
"use strict";

function release(button) {
  button.disabled = false;
  button.removeAttribute("aria-busy");
}

// Disable the submitter once a form is on its way, so a slow network cannot queue a second tap.
// It comes back after a while in case the user stopped the navigation; the server already
// deduplicates repeated submissions by request_id.
document.addEventListener("submit", (event) => {
  const button = event.submitter;
  if (!button || event.defaultPrevented) return;
  // Deferred so the browser has already captured the form data set.
  setTimeout(() => {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    setTimeout(() => release(button), 10000);
  });
});

// Returning through history must match the server-rendered results, not the next
// query typed into that page before leaving. Run after native form restoration.
window.addEventListener("pageshow", (event) => {
  if (event.persisted) document.querySelectorAll("button[aria-busy]").forEach(release);
  setTimeout(() => {
    const query = document.querySelector('form[data-library-search] input[name="q"]');
    if (query) query.value = query.defaultValue;
  }, 0);
});

// Chart readouts: hovering or tapping a period writes its exact values into the figure caption.
// Every value is also in the folded data table, so this only saves a trip there.
function readout(figure, slot) {
  const out = figure.querySelector(".readout");
  if (!out) return;
  out.dataset.rest ??= out.textContent;
  figure.querySelectorAll(".slot.active").forEach((el) => el.classList.remove("active"));
  if (slot) slot.classList.add("active");
  out.textContent = slot ? slot.dataset.tip : out.dataset.rest;
}
for (const type of ["pointerover", "focusin"]) {
  document.addEventListener(type, (event) => {
    const slot = event.target.closest?.("[data-chart] [data-tip]");
    if (slot) readout(slot.closest("[data-chart]"), slot);
  });
}
document.addEventListener("pointerout", (event) => {
  const figure = event.target.closest?.("[data-chart]");
  // Touch keeps the last tapped reading; a mouse leaving the figure restores the summary.
  if (figure && event.pointerType === "mouse" && !figure.contains(event.relatedTarget)) readout(figure, null);
});

// A link to a claim or data block inside a folded <details> opens every fold around it.
function reveal() {
  let id;
  try {
    id = decodeURIComponent(location.hash.slice(1));
  } catch {
    return; // A malformed fragment names nothing; leave the rest of the page enhancements running.
  }
  const target = id && document.getElementById(id);
  if (!target) return;
  for (let el = target; el; el = el.parentElement?.closest("details")) {
    if (el.tagName === "DETAILS") el.open = true;
  }
  target.scrollIntoView();
}
window.addEventListener("hashchange", reveal);
if (location.hash) reveal();

// Home page verdict filter pills: filter the researched companies list in place.
document.addEventListener("click", (event) => {
  const pill = event.target.closest?.("[data-verdict-filter]");
  if (!pill) return;
  const target = pill.dataset.verdictFilter;
  const bar = pill.closest(".filter-pills");
  if (bar) {
    bar.querySelectorAll("[data-verdict-filter]").forEach((btn) => {
      const on = btn === pill;
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-pressed", String(on));
    });
  }
  document.querySelectorAll(".companies > li[data-verdict]").forEach((item) => {
    item.hidden = target !== "all" && item.dataset.verdict !== target;
  });
});

// Section-level citation expand/collapse toggle for report chapters with multiple citations.
for (const section of document.querySelectorAll("article > section[id]")) {
  const heading = section.querySelector(":scope > h2");
  const sources = () => [...section.querySelectorAll(":scope > .items details.source")];
  if (!heading || sources().length < 2) continue;
  const head = document.createElement("div");
  head.className = "section-head";
  heading.before(head);
  head.append(heading);
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "cite-toggle";
  const sync = () => {
    const allOpen = sources().every((d) => d.open);
    btn.textContent = allOpen ? "收起本节出处" : "展开本节出处";
    btn.setAttribute("aria-expanded", String(allOpen));
  };
  sync();
  btn.addEventListener("click", () => {
    const next = !sources().every((d) => d.open);
    sources().forEach((d) => { d.open = next; });
    sync();
  });
  section.addEventListener("toggle", sync, true);
  head.append(btn);
}

// Sticky chapter index: highlight the chapter currently in view.
const tocLinks = [...document.querySelectorAll(".toc a[href^='#']")];
if (tocLinks.length && "IntersectionObserver" in window) {
  const byId = new Map(tocLinks.map((a) => [a.getAttribute("href").slice(1), a]));
  const targets = [...byId.keys()].map((id) => document.getElementById(id)).filter(Boolean);
  const activate = (id) => {
    tocLinks.forEach((a) => a.classList.toggle("active", a.getAttribute("href") === `#${id}`));
  };
  const observer = new IntersectionObserver(
    (entries) => {
      const visible = entries.filter((e) => e.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
      if (visible.length) activate(visible[0].target.id);
    },
    { rootMargin: "-64px 0px -65% 0px", threshold: 0 }
  );
  targets.forEach((el) => observer.observe(el.closest("section") || el));
}

// Elapsed timers count locally between server updates.
function tick() {
  for (const el of document.querySelectorAll("[data-elapsed]")) {
    el.dataset.since ??= String(Date.now() - Number(el.dataset.elapsed) * 1000);
    const seconds = Math.floor((Date.now() - Number(el.dataset.since)) / 1000);
    el.textContent = `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
  }
}

// Pages in progress poll themselves instead of reloading: the server-rendered <main> is swapped
// only when its data-key changes, keeping scroll position, opened <details> and keyboard focus.
// Polling pauses while the tab is hidden. The page also carries a plain meta refresh, so it keeps
// updating when this script is disabled or fails to load; once the script is running it cancels
// that refresh. Removing the <meta> does not cancel it; window.stop() does, and is only safe
// after load, when nothing else is in flight.
const main = document.querySelector("main[data-refresh]");
if (main) {
  const every = Number(main.dataset.refresh) * 1000;
  let timer;
  let inflight = false;
  const focusable = () => [...main.querySelectorAll("a, button, summary, input, select, textarea")];

  const swap = (doc, next) => {
    const open = [...main.querySelectorAll("details")].map((d) => d.open);
    const focused = focusable().indexOf(document.activeElement);
    // Move the parsed nodes over rather than re-serialising them through innerHTML.
    main.replaceChildren(...document.adoptNode(next).childNodes);
    main.dataset.key = next.dataset.key;
    main.querySelectorAll("details").forEach((d, i) => { if (open[i]) d.open = true; });
    if (focused >= 0) focusable()[focused]?.focus({ preventScroll: true });
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

  const start = () => {
    window.stop();
    tick();
    setInterval(tick, 1000);
    document.addEventListener("visibilitychange", () => (document.hidden ? schedule() : poll()));
    schedule();
  };
  if (document.readyState === "complete") start();
  else window.addEventListener("load", start, { once: true });
}
