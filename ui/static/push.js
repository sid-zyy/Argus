// "Push a bad change", played out on the dashboard: who pushed, the typed git commands, the diff line by line,
// the deploy pipeline, then the line that will break checkout. Same story as the ShopFast page tells.
const PushShow = (() => {
  const calm = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const sleep = (ms) => new Promise((r) => setTimeout(r, calm ? 0 : ms));
  const initials = (n) => String(n || "?").split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase();
  let ov = null;

  function build() {
    ov = document.createElement("div");
    ov.className = "po-overlay";
    ov.hidden = true;
    ov.innerHTML = `<div class="po" role="dialog" aria-modal="true" aria-labelledby="po-who">
      <div class="po-head"><div class="po-av"></div><div><div class="po-who" id="po-who"></div><div class="po-sub"></div></div>
        <button class="po-x" aria-label="Close">×</button></div>
      <div class="po-term"></div><div class="po-diff" hidden></div><div class="po-note" hidden></div><div class="po-pipe" hidden></div>
      <div class="po-foot"><button class="po-done" hidden>Watch it break</button></div></div>`;
    document.body.appendChild(ov);
    ov.querySelector(".po-x").onclick = close;
    ov.querySelector(".po-done").onclick = close;
    ov.onclick = (e) => { if (e.target === ov) close(); };
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !ov.hidden) close(); });
  }
  const q = (sel) => ov.querySelector(sel);

  function open(commit, sub) {
    if (!ov) build();
    const argus = commit.author === "Argus";
    q(".po-av").textContent = argus ? "A" : initials(commit.author);
    q(".po-av").className = `po-av ${argus ? "argus" : ""}`;
    q(".po-who").textContent = commit.author;
    q(".po-sub").textContent = sub;
    [".po-diff", ".po-note", ".po-pipe", ".po-done"].forEach((s) => (q(s).hidden = true));
    q(".po-note").classList.remove("in");
    q(".po-term").innerHTML = "";
    ov.hidden = false;
    requestAnimationFrame(() => ov.classList.add("show"));
  }
  function close() {
    if (!ov) return;
    ov.classList.remove("show");
    setTimeout(() => (ov.hidden = true), 300);
  }
  async function termLine(html, typed) {
    const line = document.createElement("div");
    q(".po-term").appendChild(line);
    if (!typed) { line.innerHTML = html; return; }
    line.innerHTML = `<span class="p">$</span> <span class="cmd"></span><span class="po-caret"></span>`;
    const cmd = line.querySelector(".cmd");
    for (let i = 1; i <= typed.length; i++) { cmd.textContent = typed.slice(0, i); await sleep(22); }
    line.querySelector(".po-caret").remove();
    await sleep(250);
  }
  function renderDiff(diff) {
    const files = [];
    for (const raw of String(diff || "").split("\n")) {
      if (raw.startsWith("diff --git")) { files.push({ name: raw.split(" b/")[1], lines: [], add: 0, del: 0 }); continue; }
      const f = files[files.length - 1];
      if (!f || /^(index |--- |\+\+\+ |new file|deleted file)/.test(raw)) continue;
      const kind = raw.startsWith("@@") ? "hunk" : raw.startsWith("+") ? "add" : raw.startsWith("-") ? "del" : "ctx";
      if (kind === "add") f.add++;
      if (kind === "del") f.del++;
      f.lines.push({ kind, text: kind === "hunk" ? raw.replace(/^@@[^@]*@@/, "···") : raw.slice(1) });
    }
    q(".po-diff").innerHTML = files.map((f) => `<div class="po-file"><span>${esc(f.name)}</span><span><span class="plus">+${f.add}</span> <span class="minus">−${f.del}</span></span></div>
      <div class="po-lines">${f.lines.map((l) => `<div class="po-ln ${l.kind}"><span>${l.kind === "add" ? "+" : l.kind === "del" ? "−" : ""}</span><span>${esc(l.text) || " "}</span></div>`).join("")}</div>`).join("");
    q(".po-diff").hidden = false;
  }
  async function revealDiff() {
    for (const l of ov.querySelectorAll(".po-ln")) { l.classList.add("in"); await sleep(l.classList.contains("ctx") ? 25 : 70); }
  }
  // the line that matters: a 'return' added before the connection is released
  function spotTheBug() {
    const lines = [...ov.querySelectorAll(".po-ln")];
    const ret = lines.find((l) => l.classList.contains("add") && /return/.test(l.textContent) && /402|error/.test(l.textContent));
    const rel = lines.find((l) => l.classList.contains("del") && /finally/.test(l.textContent));
    [ret, rel].forEach((l) => l && l.classList.add("spot"));
    return !!(ret || rel);
  }
  function pipeline(steps) {
    q(".po-pipe").innerHTML = steps.map((s, i) => `${i ? '<span class="po-arrow">→</span>' : ""}<span class="po-step" data-i="${i}">${esc(s)}</span>`).join("");
    q(".po-pipe").hidden = false;
    return (i, state) => { const el = q(`.po-step[data-i="${i}"]`); if (el) el.className = `po-step ${state}`; };
  }

  // commit: {sha, message, author, diff}. isLive(): resolves true once the app runs this commit.
  async function play(commit, isLive) {
    open(commit, "Pushing to main · checkout-api");
    await sleep(450);
    await termLine("", `git commit -am "${commit.message}"`);
    await termLine(`<span class="dim">[main ${esc(commit.sha)}] ${esc(commit.message)}</span>`);
    await sleep(200);
    renderDiff(commit.diff);
    await revealDiff();
    await sleep(300);
    await termLine("", "git push origin main");
    await termLine(`<span class="ok">✓</span> <span class="dim">pushed ${esc(commit.sha)} to main</span>`);
    const set = pipeline(["Pushed", "Deploying", "Live on ShopFast"]);
    set(0, "done"); set(1, "run");
    for (let i = 0; i < 40 && !(await isLive().catch(() => false)); i++) await sleep(500);
    set(1, "done"); set(2, "done");
    await sleep(500);
    if (spotTheBug()) {
      q(".po-note").innerHTML = "<b>Looks like a tidy refactor.</b> But on a declined card it now returns before <code>pool.release(conn)</code>, so every declined payment keeps a database connection forever.";
      q(".po-note").hidden = false;
      requestAnimationFrame(() => q(".po-note").classList.add("in"));
    }
    q(".po-done").textContent = "Watch it break";
    q(".po-done").hidden = false;
  }
  // just show a commit and its diff (no animation)
  function show(commit) {
    open(commit, `${commit.sha} · ${ago(commit.date)}`);
    termLine(`<span class="dim">${esc(commit.message)}</span>`);
    renderDiff(commit.diff);
    ov.querySelectorAll(".po-ln").forEach((l) => l.classList.add("in"));
    q(".po-done").textContent = "Close";
    q(".po-done").hidden = false;
  }
  return { play, show, close };
})();
