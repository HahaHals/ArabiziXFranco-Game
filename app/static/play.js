// The player round (spec 3.2): one box per Arabic word, link toggles to join neighbours.
(() => {
  const KEY = "franco-player-id";
  const $ = (id) => document.getElementById(id);
  const LINK_ICON = '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/></svg>';

  let playerId = null;
  let sentence = null;
  let links = [];      // links[i]: word i is joined to word i+1 (0-based)
  let values = {};     // "start-end" (0-based) -> typed text
  let errors = {};     // "start-end" -> message

  async function api(path, options) {
    const r = await fetch(path, options);
    const body = await r.json().catch(() => ({}));
    return { ok: r.ok, status: r.status, body };
  }

  async function ensurePlayer(forceNew = false) {
    if (!forceNew) {
      try { playerId = localStorage.getItem(KEY); } catch (e) {}
    }
    if (!playerId || forceNew) {
      const r = await api("/api/players", { method: "POST" });
      if (!r.ok) throw new Error("player");
      playerId = r.body.player_id;
      try { localStorage.setItem(KEY, playerId); } catch (e) {}
    }
  }

  function groups() {
    const out = [];
    for (let s = 0; s < sentence.words.length; ) {
      let e = s;
      while (e < links.length && links[e]) e++;
      out.push([s, e]);
      s = e + 1;
    }
    return out;
  }
  const key = ([s, e]) => `${s}-${e}`;

  function toggle(i) {
    const before = groups();
    const a = before.find(([s, e]) => s <= i && i <= e);
    if (links[i]) {
      // Split. If the text has one part per word ("han ro7"), the split follows the spaces;
      // otherwise ("hanro7") the first box keeps it all.
      links[i] = false;
      const text = values[key(a)] || "";
      const parts = text.trim().split(/\s+/);
      const nLeft = i - a[0] + 1;
      if (parts.length === a[1] - a[0] + 1) {
        values[`${a[0]}-${i}`] = parts.slice(0, nLeft).join(" ");
        values[`${i + 1}-${a[1]}`] = parts.slice(nLeft).join(" ");
      } else {
        values[`${a[0]}-${i}`] = text;
        values[`${i + 1}-${a[1]}`] = "";
      }
    } else {
      const b = before.find(([s]) => s === i + 1);
      links[i] = true;
      values[`${a[0]}-${b[1]}`] = [values[key(a)], values[key(b)]].filter(Boolean).join(" ");
    }
    errors = {};
    render();
  }

  function linkButton(i) {
    const on = links[i];
    return `<button type="button" class="link-toggle${on ? " on" : ""}" data-link="${i}" aria-pressed="${on}"
      aria-label="${on ? "Split these words" : "Join these words"}" title="${on ? "Split" : "Join"}">${LINK_ICON}</button>`;
  }

  const esc = (v) => String(v).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  function render() {
    const gs = groups();
    let html = "";
    gs.forEach((g, n) => {
      const [s, e] = g;
      let label = "";
      for (let w = s; w <= e; w++) {
        label += `<span lang="ar">${esc(sentence.words[w])}</span>`;
        if (w < e) label += linkButton(w);
      }
      const err = errors[key(g)];
      html += `<div class="box${e > s ? " joined" : ""}${err ? " has-error" : ""}">
        <div class="ar" dir="rtl">${label}</div>
        <input dir="ltr" data-key="${key(g)}" value="${esc(values[key(g)] || "")}"
          aria-label="Franco for ${esc(sentence.words.slice(s, e + 1).join(" "))}">
        ${err ? `<p class="error">${esc(err)}</p>` : ""}
      </div>`;
      if (n < gs.length - 1) html += linkButton(e);
    });
    $("boxes").innerHTML = html;
  }

  async function loadRound() {
    for (const id of ["round", "empty", "failed"]) $(id).hidden = true;
    let r = await api(`/api/round?player_id=${encodeURIComponent(playerId)}`);
    if (r.status === 404) {
      await ensurePlayer(true);
      r = await api(`/api/round?player_id=${encodeURIComponent(playerId)}`);
    }
    if (!r.ok) throw new Error("round");
    sentence = r.body.sentence;
    if (!sentence) { $("empty").hidden = false; return; }
    links = new Array(sentence.words.length - 1).fill(false);
    values = {}; errors = {};
    $("sentence").textContent = sentence.text;
    $("gloss").textContent = sentence.english;
    $("gloss").hidden = !sentence.english;
    $("form-error").hidden = true;
    closeReport();
    render();
    $("round").hidden = false;
    window.scrollTo(0, 0);
  }

  $("boxes").addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-link]");
    if (b) toggle(Number(b.dataset.link));
  });
  $("boxes").addEventListener("input", (ev) => {
    if (ev.target.dataset.key) values[ev.target.dataset.key] = ev.target.value;
  });

  $("answers").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const gs = groups();
    errors = {};
    for (const g of gs) if (!(values[key(g)] || "").trim()) errors[key(g)] = "Fill in this box";
    if (Object.keys(errors).length) { render(); focusFirstError(); return; }

    $("submit").disabled = true;
    try {
      const r = await api("/api/answers", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          player_id: playerId, sentence_id: sentence.id,
          boxes: gs.map((g) => ({ start: g[0] + 1, end: g[1] + 1, text: values[key(g)] })),
        }),
      });
      if (r.ok) {
        $("toast").hidden = false;
        setTimeout(() => { $("toast").hidden = true; }, 1200);
        await loadRound();
      } else if (r.status === 422 && r.body.detail && r.body.detail.errors) {
        for (const [start, msg] of Object.entries(r.body.detail.errors)) {
          const g = gs.find(([s]) => s === Number(start) - 1);
          if (g) errors[key(g)] = msg;
        }
        render(); focusFirstError();
      } else if (r.status === 409) {
        await loadRound();  // sentence gone or already answered: move on
      } else if (r.status === 429) {
        $("form-error").textContent = "Slow down a little. Wait a minute and try again · براحة شوية";
        $("form-error").hidden = false;
      } else {
        $("form-error").textContent = "Couldn't save. Check your connection and try again.";
        $("form-error").hidden = false;
      }
    } catch (e) {
      $("form-error").textContent = "Couldn't save. Check your connection and try again.";
      $("form-error").hidden = false;
    } finally {
      $("submit").disabled = false;
    }
  });

  // ---- Report (spec 3.3): skips the round; nothing typed is saved. ----
  function closeReport() {
    $("report").hidden = true;
    $("report").reset();
    $("report-error").hidden = true;
    $("answers").hidden = false;
    $("report-open").hidden = false;
  }
  $("report-open").addEventListener("click", () => {
    $("answers").hidden = true;
    $("report-open").hidden = true;
    $("report").hidden = false;
    $("report").querySelector("input[name=reason]").focus();
  });
  $("report-cancel").addEventListener("click", closeReport);
  $("report").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const picked = $("report").querySelector("input[name=reason]:checked");
    if (!picked) {
      $("report-error").textContent = "Pick a reason · اختار سبب";
      $("report-error").hidden = false;
      return;
    }
    $("report-send").disabled = true;
    try {
      const r = await api("/api/reports", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          player_id: playerId, sentence_id: sentence.id,
          reason: picked.value, comment: $("report-comment").value,
        }),
      });
      if (r.ok || r.status === 409) {
        $("toast").textContent = "Reported, thanks · شكرا";
        $("toast").hidden = false;
        setTimeout(() => { $("toast").hidden = true; $("toast").textContent = "Saved · اتحفظ ✓"; }, 1200);
        await loadRound();
      } else if (r.status === 429) {
        $("report-error").textContent = "Slow down a little. Wait a minute and try again · براحة شوية";
        $("report-error").hidden = false;
      } else {
        $("report-error").textContent = "Couldn't send. Check your connection and try again.";
        $("report-error").hidden = false;
      }
    } catch (e) {
      $("report-error").textContent = "Couldn't send. Check your connection and try again.";
      $("report-error").hidden = false;
    } finally {
      $("report-send").disabled = false;
    }
  });

  function focusFirstError() {
    const el = document.querySelector(".box.has-error input");
    if (el) el.focus();
  }

  async function start() {
    try {
      await ensurePlayer();
      await loadRound();
    } catch (e) {
      $("failed").hidden = false;
    }
  }
  $("retry").addEventListener("click", start);
  start();
})();
