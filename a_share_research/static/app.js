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
  // Native fragments place new search results; focus without moving the page.
  // Back/forward navigation belongs to the browser's scroll restoration.
  if (!event.persisted && location.hash === "#results"
      && performance.getEntriesByType("navigation")[0]?.type !== "back_forward") {
    document.getElementById("results")?.focus({ preventScroll: true });
  }
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
  if (id === "results") return; // Native search anchor; never override history restoration.
  const target = id && document.getElementById(id);
  if (!target) return;
  for (let el = target; el; el = el.parentElement?.closest("details")) {
    if (el.tagName === "DETAILS") el.open = true;
  }
  target.scrollIntoView();
}
window.addEventListener("hashchange", reveal);
if (location.hash) reveal();

// Home page verdict filter: each pill links to its group, so without a script it jumps there;
// with one it shows only that group in place.
document.addEventListener("click", (event) => {
  const pill = event.target.closest?.("[data-verdict-filter]");
  if (!pill) return;
  event.preventDefault();
  const target = pill.dataset.verdictFilter;
  pill.parentElement.querySelectorAll("[data-verdict-filter]").forEach((el) => {
    el.classList.toggle("active", el === pill);
    if (el === pill) el.setAttribute("aria-current", "true"); else el.removeAttribute("aria-current");
  });
  let shown = 0;
  document.querySelectorAll(".companies > [data-verdict]").forEach((group) => {
    const match = target === "all" || group.dataset.verdict === target;
    group.hidden = !match;
    if (match) shown += group.querySelectorAll(".card").length;
  });
  const status = document.querySelector("[data-filter-status]");
  if (status) status.textContent = `当前显示 ${shown} 家公司`;
});

// Section-level citation expand/collapse toggle for report chapters with multiple citations.
for (const section of document.querySelectorAll("article > section[id]")) {
  const heading = section.querySelector(":scope > h2");
  const sources = () => [
    ...section.querySelectorAll(":scope > .items details.source, :scope > details.more details.source"),
  ];
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
    if (next) section.querySelectorAll(":scope > details.more").forEach((d) => { d.open = true; });
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
  const idOf = new Map(targets.map((el) => [el.closest("section") || el, el.id]));
  const activate = (id) => {
    if (!id) return;
    tocLinks.forEach((a) => a.classList.toggle("active", a.getAttribute("href") === `#${id}`));
  };
  tocLinks.forEach((a) => {
    a.addEventListener("click", () => activate(a.getAttribute("href").slice(1)));
  });
  const observer = new IntersectionObserver(
    (entries) => {
      if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4 && targets.length) {
        return activate(targets[targets.length - 1].id);
      }
      const visible = entries.filter((e) => e.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
      if (visible.length) activate(idOf.get(visible[0].target));
    },
    { rootMargin: "-60px 0px -65% 0px", threshold: 0 }
  );
  idOf.forEach((_, node) => observer.observe(node));
  window.addEventListener("scroll", () => {
    if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4 && targets.length) {
      activate(targets[targets.length - 1].id);
    }
  }, { passive: true });
}

// Elapsed timers count locally between server updates.
function tick() {
  for (const el of document.querySelectorAll("[data-elapsed]")) {
    if (el.closest('main[data-disconnected="true"]')) continue;
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
  let lastSync = new Date();
  const connection = document.createElement("p");
  connection.className = "hint connection-status";
  connection.setAttribute("role", "status");
  main.before(connection);
  // A live region: speaks only when the state changes, and stays empty while polling is healthy.
  const showConnection = (text) => {
    if (connection.textContent !== text) connection.textContent = text;
  };
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
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 12000);
    try {
      const res = await fetch(location.href, { cache: "no-store", signal: controller.signal, headers: { accept: "text/html" } });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const doc = new DOMParser().parseFromString(await res.text(), "text/html");
      const next = doc.querySelector("main");
      if (!next) throw new Error("Missing page content");
      if (res.redirected || !next.hasAttribute("data-refresh")) return location.reload();
      const recovered = main.dataset.disconnected === "true";
      lastSync = new Date();
      main.dataset.disconnected = "false";
      showConnection(recovered ? `连接已恢复 · 最后同步 ${lastSync.toLocaleTimeString()}` : "");
      if (recovered || next.dataset.key !== main.dataset.key) swap(doc, next);
    } catch (error) {
      main.dataset.disconnected = "true";
      showConnection(`连接中断或响应异常，正在重连；进度与计时暂停更新 · 最后同步 ${lastSync.toLocaleTimeString()}`);
      console.debug("progress poll failed", error);
    } finally {
      clearTimeout(timeout);
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

// Draft only: retain the saved parent form, require an explicit submit, never overwrite a draft.
document.addEventListener("click", (event) => {
  const link = event.target.closest?.("[data-followup-location]");
  const field = document.getElementById("followup");
  const status = document.getElementById("followup-status");
  if (!link || !field || !status) return;
  event.preventDefault();
  if (field.value.trim()) {
    status.textContent = "已保留你正在编辑的问题；可修改后提交。";
  } else {
    const prefix = `请继续研究原报告 ${link.dataset.followupLocation}，哪些证据会改变判断，还需要核对什么？`;
    const draft = `${prefix}\n原文：${link.dataset.followupText}`;
    field.value = draft.length <= field.maxLength ? draft : prefix;
    status.textContent = "已带入原报告条目。请检查、修改后点击继续追问。";
  }
  field.focus();
});
