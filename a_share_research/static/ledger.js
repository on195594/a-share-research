"use strict";

(() => {
  const form = document.getElementById("ledger-form");
  const posted = document.getElementById("ledger-posted");
  const account = form?.elements.account_id.value || posted?.dataset.account;
  if (!account) return;

  const prefix = `a-share:ledger:${encodeURIComponent(account)}:`;
  const draftKey = (version) => prefix + version;
  const discardKey = prefix + "discard";
  function storedDrafts() {
    const drafts = [];
    for (const key of Object.keys(localStorage).filter((key) => key.startsWith(prefix))) {
      const raw = localStorage.getItem(key);
      try {
        const draft = JSON.parse(raw);
        if (!draft?.request || !Number.isFinite(draft.updated) ||
            !Array.isArray(draft.values) || !draft.values.every((pair) =>
          Array.isArray(pair) && pair.length === 2 && pair.every((value) => typeof value === "string"))) {
          throw new TypeError("Invalid ledger draft fields");
        }
        drafts.push({ ...draft, key });
      } catch {
        localStorage.removeItem(key);
      }
    }
    return drafts;
  }

  function clearDrafts(version, request) {
    for (const draft of storedDrafts()) {
      if (draft.request === request || Number(draft.key.slice(prefix.length)) < version) {
        localStorage.removeItem(draft.key);
      }
    }
  }

  if (posted) {
    if (posted.dataset.request) {
      try {
        clearDrafts(Number(posted.dataset.version), posted.dataset.request);
      } catch (error) {
        console.warn("Ledger draft cleanup failed", error.name);
      }
    }
    return;
  }

  const field = (name) => form.elements.namedItem(name);
  const draftStatus = form.querySelector("[data-draft-status]");
  const retry = document.getElementById("ledger-retry");
  const originalFields = ([name]) => !["csrf", "intent", "awaiting_reply"].includes(name);
  function saveDraft(awaitingReply) {
    const values = [...new FormData(retry || form)].filter(originalFields);
    try {
      localStorage.setItem(draftKey(field("expected_version").value), JSON.stringify({
        request: field("request_id").value, updated: Date.now(), values, awaitingReply,
      }));
    } catch {
      draftStatus.textContent = "当前浏览器无法保存草稿，请保持此页面打开。";
    }
  }

  if (form.dataset.fresh === "yes" && !form.querySelector("fieldset").disabled) {
    try {
      // A discard belongs to this tab. Use the fresh ledger version to preserve newer drafts in other tabs.
      const discard = sessionStorage.getItem(discardKey);
      if (discard) {
        clearDrafts(Number(field("expected_version").value), discard);
        sessionStorage.removeItem(discardKey);
      }
      const draft = !discard && storedDrafts().sort((a, b) => b.updated - a.updated)[0];
      if (draft) {
        // Restore through the read-only form action so old holdings and evidence survive version changes.
        const restore = document.createElement("form");
        restore.method = "post";
        restore.action = form.action;
        const values = [...draft.values.filter(originalFields), ["csrf", field("csrf").value], ["intent", "restore"]];
        if (draft.awaitingReply) values.push(["awaiting_reply", "1"]);
        for (const [name, value] of values) {
          const input = document.createElement("input");
          input.type = "hidden";
          input.name = name;
          input.value = value;
          restore.append(input);
        }
        document.body.append(restore);
        restore.submit();
        return;
      }
    } catch {
      draftStatus.textContent = "本机草稿无法读取，可按当前账本重新填写。";
    }
  }

  document.querySelector("[data-new-draft]")?.addEventListener("click", (event) => {
    try {
      sessionStorage.setItem(discardKey, field("request_id").value);
    } catch {
      event.preventDefault();
      draftStatus.textContent = "当前浏览器无法清除草稿。";
    }
  });
  if (retry) {
    saveDraft(true);
    return;
  }

  const rows = [...form.querySelectorAll("[data-holding]")];
  const cashPreview = document.getElementById("cash-preview");
  const marketPreview = document.getElementById("market-preview");
  const display = new Intl.NumberFormat("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const number = (name) => {
    const value = field(name)?.value;
    return value !== undefined && value.trim() !== "" ? Number(value) : NaN;
  };
  function preview(changed) {
    const code = field("code").value;
    const sign = field("kind").value === "buy" ? 1 : -1;
    const shares = number("shares");
    const old = rows.find((row) => row.dataset.holding === code);
    for (const row of rows) row.hidden = row === old;
    const context = document.getElementById("ledger-entry");
    if (context) {
      context.hidden = Boolean(old) || sign < 0;
      context.querySelector("fieldset").disabled = context.hidden;
      if (!context.hidden && code.length === 6 && changed === "code") context.open = true;
    }
    const profile = field("policy_identity");
    const framework = field("framework");
    if (framework && (!changed || changed === "policy_identity")) {
      for (const group of framework.querySelectorAll("optgroup")) {
        group.hidden = group.disabled = group.dataset.profile !== profile.value;
      }
      if (framework.selectedOptions[0]?.parentElement.disabled) framework.value = "";
      framework.required = Boolean(profile.value);
      framework.disabled = !profile.value;
    }
    if (["code", "shares", "kind"].includes(changed)) {
      const ending = (old ? number("opening." + code) : 0) + sign * shares;
      field("trade_ending").value = Number.isInteger(ending) && ending >= 0 ? ending : "";
    }
    const projected = number("opening_cash") - sign * shares * number("price") - number("fees");
    const cash = number("cash");
    const market = number("total_assets") - cash;
    const itemized = rows.filter((row) => row !== old)
      .reduce((sum, row) => sum + number("value." + row.dataset.holding), number("trade_value"));
    cashPreview.textContent = Number.isFinite(projected) && Number.isFinite(cash)
      ? `账本推算现金 ${display.format(projected)} 元，券商现金 ${display.format(cash)} 元，差额 ${display.format(projected - cash)} 元。`
      : "填写成交明细和券商现金后可核对现金差额。";
    marketPreview.textContent = Number.isFinite(market) && Number.isFinite(itemized)
      ? `持仓明细合计 ${display.format(itemized)} 元，总资产减现金 ${display.format(market)} 元，市值差额 ${display.format(itemized - market)} 元。以对账回执为准。`
      : "填写各持仓市值和券商总资产后可核对市值差额。";
  }
  if (form.querySelector("fieldset").disabled) return;
  if (form.dataset.fresh === "no") saveDraft(false);
  document.getElementById("ledger-preview").hidden = false;
  preview();
  form.addEventListener("input", (event) => { preview(event.target.name); saveDraft(false); });
  form.addEventListener("submit", () => saveDraft(true));
})();
