// X-LLM-Search — frontend

// ── Mobile detection ──────────────────────────────────────────────────────
// Uses User-Agent + coarse pointer (touch screen) + narrow viewport.
// Result is stamped as a body class so CSS can respond immediately.
const isMobile = (() => {
  const ua =
    /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(
      navigator.userAgent,
    );
  const touch = window.matchMedia("(pointer: coarse)").matches;
  const narrow = window.matchMedia("(max-width: 900px)").matches;
  return ua || (touch && narrow);
})();

if (isMobile) document.body.classList.add("is-mobile");

// ── Viewport height tracker ────────────────────────────────────────────────
// window.innerHeight is the usable height AFTER browser chrome is subtracted.
// We write it to --real-vh so the CSS layout never gets clipped behind a
// mobile address bar, gesture bar, or Samsung's nav strip.
function updateVh() {
  document.documentElement.style.setProperty(
    "--real-vh",
    window.innerHeight + "px",
  );
}
updateVh();
window.addEventListener("resize", updateVh);
// orientationchange fires before the new dimensions settle — wait briefly.
window.addEventListener("orientationchange", () => setTimeout(updateVh, 300));

// ── State ──────────────────────────────────────────────────────────────────
const state = {
  selectedModel: null,
  isSearching: false,
  conversationHistory: [], // [{role:"user"|"assistant", content:"..."}] — in-memory session
};

// ── DOM refs ───────────────────────────────────────────────────────────────
const chatHistory = document.getElementById("chatHistory");
const queryInput = document.getElementById("queryInput");
const btnSearch = document.getElementById("btnSearch");
const errorPanel = document.getElementById("errorPanel");
const errorMessage = document.getElementById("errorMessage");
const modelBtn = document.getElementById("modelBtn");
const modelLabel = document.getElementById("modelLabel");
const modelMenu = document.getElementById("modelMenu");

// ── Auto-grow textarea ─────────────────────────────────────────────────────
queryInput.addEventListener("input", () => {
  queryInput.style.height = "auto";
  queryInput.style.height = queryInput.scrollHeight + "px";
});

queryInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    if (!state.isSearching) handleSearch();
  }
});

// ── Model picker ───────────────────────────────────────────────────────────
modelBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  modelMenu.classList.toggle("hidden");
});
document.addEventListener("click", () => modelMenu.classList.add("hidden"));

async function loadModels() {
  try {
    const res = await fetch("/api/models");
    const data = await res.json();
    const models = data.models || [];

    modelMenu.innerHTML = "";

    if (data.error && models.length === 0) {
      const msg = data.error.includes("refused")
        ? "Cannot reach Ollama at 11434"
        : data.error;
      modelMenu.innerHTML = `<li class="no-models" title="${msg}">⚠ ${msg}</li>`;
      modelLabel.textContent = "Offline";
      return;
    }

    if (models.length === 0) {
      modelMenu.innerHTML =
        '<li class="no-models">No models found — pull one with: ollama pull &lt;name&gt;</li>';
      modelLabel.textContent = "No models";
      return;
    }

    models.forEach((id, i) => {
      const li = document.createElement("li");
      li.textContent = id;
      li.setAttribute("role", "option");
      li.addEventListener("click", (e) => {
        e.stopPropagation();
        selectModel(id);
        modelMenu.classList.add("hidden");
      });
      modelMenu.appendChild(li);
      if (i === 0) selectModel(id); // auto-select first
    });
  } catch (err) {
    console.error("Failed to fetch models:", err);
    modelLabel.textContent = "Offline";
    modelMenu.innerHTML = '<li class="no-models">⚠ Cannot reach backend</li>';
  }
}

function selectModel(id) {
  state.selectedModel = id;
  modelLabel.textContent = id;
  modelMenu
    .querySelectorAll("li")
    .forEach((li) => li.classList.toggle("active", li.textContent === id));
}

// ── Conversation history helpers ───────────────────────────────────────────
function addToHistory(role, content) {
  state.conversationHistory.push({ role, content });
  // Keep at most 20 messages (10 exchanges) to avoid ballooning memory
  if (state.conversationHistory.length > 20) {
    state.conversationHistory = state.conversationHistory.slice(-20);
  }
}

function getHistory() {
  // Send last 6 messages (3 exchanges) to the backend for context
  return state.conversationHistory.slice(-6);
}

// ── Markdown renderer ──────────────────────────────────────────────────────
// Supports: headings, bold/italic/code, links, tables, ul, ol, citations.
function md(text, sources = []) {
  if (!text) return "";

  const srcMap = {};
  sources.forEach((s) => {
    srcMap[s.num] = s;
  });

  // HTML-escape a raw string (used on content before inserting into elements)
  const esc = (s) =>
    String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");

  // Apply inline markdown + citation links to an already-escaped string
  const inline = (s) =>
    s
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/\*(.+?)\*/g, "<em>$1</em>")
      .replace(/`(.+?)`/g, "<code>$1</code>")
      .replace(
        /\[(.+?)\]\((.+?)\)/g,
        '<a href="$2" target="_blank" rel="noopener">$1</a>',
      )
      // [Source N]
      .replace(/\[Source (\d+)\]/g, (_m, num) => {
        const n = parseInt(num, 10);
        const s = srcMap[n];
        return s
          ? `<a href="${s.url}" target="_blank" rel="noopener" class="src-ref" title="${esc(s.title)}">[${n}]</a>`
          : "";
      })
      // bare [N] or [N, M, ...]
      .replace(/\[(\d+(?:,\s*\d+)*)\]/g, (_m, nums) => {
        const links = nums
          .split(",")
          .map((p) => parseInt(p.trim(), 10))
          .map((n) => {
            const s = srcMap[n];
            return s
              ? `<a href="${s.url}" target="_blank" rel="noopener" class="src-ref" title="${esc(s.title)}">[${n}]</a>`
              : null;
          })
          .filter(Boolean);
        return links.join("\u202f");
      });

  // Parse a pipe-separated table row into an array of cell strings
  const parseRow = (line) =>
    line
      .replace(/^\s*\|/, "")
      .replace(/\|\s*$/, "")
      .split("|")
      .map((c) => c.trim());

  const isTableRow = (line) => /^\s*\|/.test(line);
  const isSepRow = (line) => /^\s*\|[\s\-:|]+\|/.test(line);

  const lines = text.split("\n");
  let html = "";
  let i = 0;
  let inPara = false;

  const openPara = () => {
    if (!inPara) {
      html += "<p>";
      inPara = true;
    }
  };
  const closePara = () => {
    if (inPara) {
      html += "</p>";
      inPara = false;
    }
  };

  while (i < lines.length) {
    const raw = lines[i];
    const line = raw.trimEnd();

    // ── Blank line → paragraph break
    if (line.trim() === "") {
      closePara();
      i++;
      continue;
    }

    // ── ATX headings
    let m;
    if ((m = line.match(/^(#{1,3}) (.+)$/))) {
      closePara();
      const level = m[1].length;
      html += `<h${level}>${inline(esc(m[2]))}</h${level}>`;
      i++;
      continue;
    }

    // ── Horizontal rule
    if (/^[\-*_]{3,}\s*$/.test(line)) {
      closePara();
      html += "<hr>";
      i++;
      continue;
    }

    // ── Fenced code block ```
    if (line.startsWith("```")) {
      closePara();
      const lang = line.slice(3).trim();
      i++;
      const codeLines = [];
      while (i < lines.length && !lines[i].startsWith("```")) {
        codeLines.push(esc(lines[i]));
        i++;
      }
      i++; // consume closing ```
      html += `<pre><code${lang ? ` class="language-${esc(lang)}"` : ""}>${codeLines.join("\n")}</code></pre>`;
      continue;
    }

    // ── Markdown table (pipe-based)
    if (isTableRow(line)) {
      // Collect all consecutive table rows
      const tableLines = [];
      while (i < lines.length && isTableRow(lines[i])) {
        tableLines.push(lines[i]);
        i++;
      }
      // Need at least header + separator rows
      if (tableLines.length >= 2 && isSepRow(tableLines[1])) {
        closePara();
        const headers = parseRow(tableLines[0]);
        const dataRows = tableLines.slice(2);
        const thead =
          `<thead><tr>` +
          headers.map((h) => `<th>${inline(esc(h))}</th>`).join("") +
          `</tr></thead>`;
        const tbody =
          `<tbody>` +
          dataRows
            .map(
              (r) =>
                `<tr>` +
                parseRow(r)
                  .map((c) => `<td>${inline(esc(c))}</td>`)
                  .join("") +
                `</tr>`,
            )
            .join("") +
          `</tbody>`;
        html += `<div class="md-table-wrap"><table class="md-table">${thead}${tbody}</table></div>`;
      } else {
        // Not a proper table — treat rows as plain text
        tableLines.forEach((tl) => {
          openPara();
          html += inline(esc(tl)) + "<br>";
        });
      }
      continue;
    }

    // ── Unordered list
    if (/^[\*\-] /.test(line)) {
      closePara();
      html += "<ul>";
      while (i < lines.length && /^[\*\-] /.test(lines[i].trimEnd())) {
        html += `<li>${inline(esc(lines[i].replace(/^[\*\-] /, "")))}</li>`;
        i++;
      }
      html += "</ul>";
      continue;
    }

    // ── Ordered list
    if (/^\d+\. /.test(line)) {
      closePara();
      html += "<ol>";
      while (i < lines.length && /^\d+\. /.test(lines[i].trimEnd())) {
        html += `<li>${inline(esc(lines[i].replace(/^\d+\. /, "")))}</li>`;
        i++;
      }
      html += "</ol>";
      continue;
    }

    // ── Regular paragraph text
    openPara();
    html += inline(esc(line)) + " ";
    i++;
  }

  closePara();

  // ── Collapsible references list
  if (sources.length > 0) {
    const items = sources
      .map((s) => {
        let domain = s.url;
        try {
          domain = new URL(s.url).hostname;
        } catch {
          /* ok */
        }
        return (
          `<li><a href="${s.url}" target="_blank" rel="noopener">` +
          `<img src="https://www.google.com/s2/favicons?domain=${domain}&sz=16" ` +
          `width="12" height="12" alt="" onerror="this.style.display='none'" />` +
          ` ${esc(s.title)}</a></li>`
        );
      })
      .join("");
    html +=
      `<div class="src-list">` +
      `<button class="src-list-toggle" onclick="this.closest('.src-list').classList.toggle('open')" type="button">` +
      `<span class="src-list-toggle-label">References</span>` +
      `<span class="src-list-count">${sources.length}</span>` +
      `<span class="src-list-chevron">&#9656;</span>` +
      `</button>` +
      `<div class="src-list-body"><ol>${items}</ol></div>` +
      `</div>`;
  }

  return html;
}

// ── Example query chips ────────────────────────────────────────────────────
document.addEventListener("click", (e) => {
  const btn = e.target.closest(".example-query");
  if (!btn || state.isSearching) return;
  const q = btn.dataset.query;
  if (!q) return;
  queryInput.value = q;
  queryInput.style.height = "auto";
  queryInput.style.height = queryInput.scrollHeight + "px";
  handleSearch();
});

// ── Chat helpers ───────────────────────────────────────────────────────────
function clearWelcome() {
  const w = chatHistory.querySelector(".welcome-message");
  if (w) w.remove();
}

function scrollBottom() {
  chatHistory.scrollTop = chatHistory.scrollHeight;
}

// Append a regular chat bubble (user or assistant)
function appendMessage(role, html) {
  clearWelcome();
  const wrap = document.createElement("div");
  wrap.className = `msg ${role}`;
  const avatar = document.createElement("div");
  avatar.className = "msg-avatar";
  avatar.textContent = role === "user" ? "🧑" : "✦";
  const body = document.createElement("div");
  body.className = "msg-body";
  body.innerHTML = html;
  wrap.appendChild(avatar);
  wrap.appendChild(body);
  chatHistory.appendChild(wrap);
  scrollBottom();
  return body;
}

// ── Thinking indicator (normal search) ────────────────────────────────────
function appendThinking() {
  const body = appendMessage("assistant", "");
  body.classList.add("thinking");
  body.innerHTML = `
        <div class="dot-pulse">
            <span></span><span></span><span></span>
        </div>`;
  return body;
}

// ── Research card (deep search) ────────────────────────────────────────────
// Tracks the current active step so we can mark it "done" when the next one arrives.
let rcSubtitle = null; // subtitle element in the card header
let rcStepsEl = null; // the <div class="rc-steps"> list container
let rcSpinner = null; // the spinner wrapper in the header
let rcCountEl = null; // the "N steps" badge
let rcActiveStep = null; // the currently-active step element
let rcStepCount = 0; // total steps added
let rcProgressFill = null; // progress bar fill element
let rcBrowsing = null; // "currently browsing" strip
let rcBrowsingIconWrap = null; // favicon/emoji wrapper in strip
let rcBrowsingDomain = null; // domain text in strip
let rcBrowsingPath = null; // path text in strip
let rcSources = null; // sources footer element
let rcSourcesList = null; // sources pill container
let rcVisitedUrls = []; // deduplicated list of {url, domain} visited
let rcMaxIterations = 5; // total iterations (for progress %)
let rcPlan = null; // plan panel element
let rcPlanSteps = null; // plan steps list container
let rcPlanStepEls = []; // individual plan step DOM elements
let rcStepResults = []; // [{title, content, sources}] streamed per step
let rcInMultiSearch = false; // suppress individual search rows during multi-query expansion

function createResearchCard(maxIter = 5) {
  clearWelcome();
  rcMaxIterations = maxIter;
  rcVisitedUrls = [];

  const wrap = document.createElement("div");
  wrap.className = "msg assistant";

  const avatar = document.createElement("div");
  avatar.className = "msg-avatar";
  avatar.textContent = "✦";

  // The card itself sits where .msg-body would normally be
  const card = document.createElement("div");
  card.className = "msg-body research-card";
  card.innerHTML = `
        <div class="rc-header">
            <div class="rc-spinner" id="rcSpinner">
                <div class="rc-ring"></div>
            </div>
            <div class="rc-header-text">
                <div class="rc-title">Research</div>
                <div class="rc-subtitle" id="rcSubtitle">Planning…</div>
            </div>
            <div class="rc-iter-badge" id="rcCount">
                planning
            </div>
        </div>
        <div class="rc-progress-track">
            <div class="rc-progress-fill" id="rcProgressFill"></div>
        </div>
        <div class="rc-plan hidden" id="rcPlan">
            <div class="rc-plan-label">Research Plan</div>
            <div class="rc-plan-steps" id="rcPlanSteps"></div>
        </div>
        <div class="rc-browsing hidden" id="rcBrowsing">
            <div class="rc-browsing-favicon-wrap" id="rcBrowsingIconWrap">🌐</div>
            <div class="rc-browsing-text">
                <div class="rc-browsing-label">Reading</div>
                <div class="rc-browsing-domain" id="rcBrowsingDomain">—</div>
                <div class="rc-browsing-path" id="rcBrowsingPath"></div>
            </div>
            <div class="rc-browsing-indicator">
                <div class="rc-browsing-spin"></div>
            </div>
        </div>
        <div class="rc-steps" id="rcSteps"></div>
        <div class="rc-sources hidden" id="rcSources">
            <div class="rc-sources-label">Sources</div>
            <div class="rc-sources-list" id="rcSourcesList"></div>
        </div>
    `;

  wrap.appendChild(avatar);
  wrap.appendChild(card);
  chatHistory.appendChild(wrap);
  scrollBottom();

  // Cache refs for later updates
  rcSubtitle = card.querySelector("#rcSubtitle");
  rcStepsEl = card.querySelector("#rcSteps");
  rcSpinner = card.querySelector("#rcSpinner");
  rcCountEl = card.querySelector("#rcCount");
  rcProgressFill = card.querySelector("#rcProgressFill");
  rcBrowsing = card.querySelector("#rcBrowsing");
  rcBrowsingIconWrap = card.querySelector("#rcBrowsingIconWrap");
  rcBrowsingDomain = card.querySelector("#rcBrowsingDomain");
  rcBrowsingPath = card.querySelector("#rcBrowsingPath");
  rcSources = card.querySelector("#rcSources");
  rcSourcesList = card.querySelector("#rcSourcesList");
  rcPlan = card.querySelector("#rcPlan");
  rcPlanSteps = card.querySelector("#rcPlanSteps");
  rcPlanStepEls = [];
  rcStepResults = [];
  rcInMultiSearch = false;
  rcActiveStep = null;
  rcStepCount = 0;
}

// ── Search card (normal search — no plan panel) ────────────────────────────
function createSearchCard() {
  clearWelcome();
  rcVisitedUrls = [];
  rcStepResults = [];
  rcPlanStepEls = [];
  rcInMultiSearch = false;

  const wrap = document.createElement("div");
  wrap.className = "msg assistant";

  const avatar = document.createElement("div");
  avatar.className = "msg-avatar";
  avatar.textContent = "✦";

  const card = document.createElement("div");
  card.className = "msg-body research-card";
  card.innerHTML = `
        <div class="rc-header">
            <div class="rc-spinner" id="rcSpinner">
                <div class="rc-ring"></div>
            </div>
            <div class="rc-header-text">
                <div class="rc-title">Search</div>
                <div class="rc-subtitle" id="rcSubtitle">Generating queries…</div>
            </div>
        </div>
        <div class="rc-progress-track">
            <div class="rc-progress-fill" id="rcProgressFill"></div>
        </div>
        <div class="rc-browsing hidden" id="rcBrowsing">
            <div class="rc-browsing-favicon-wrap" id="rcBrowsingIconWrap">🌐</div>
            <div class="rc-browsing-text">
                <div class="rc-browsing-label">Reading</div>
                <div class="rc-browsing-domain" id="rcBrowsingDomain">—</div>
                <div class="rc-browsing-path" id="rcBrowsingPath"></div>
            </div>
            <div class="rc-browsing-indicator">
                <div class="rc-browsing-spin"></div>
            </div>
        </div>
        <div class="rc-steps" id="rcSteps"></div>
        <div class="rc-sources hidden" id="rcSources">
            <div class="rc-sources-label">Sources</div>
            <div class="rc-sources-list" id="rcSourcesList"></div>
        </div>
    `;

  wrap.appendChild(avatar);
  wrap.appendChild(card);
  chatHistory.appendChild(wrap);
  scrollBottom();

  rcSubtitle = card.querySelector("#rcSubtitle");
  rcStepsEl = card.querySelector("#rcSteps");
  rcSpinner = card.querySelector("#rcSpinner");
  rcCountEl = null;
  rcProgressFill = card.querySelector("#rcProgressFill");
  rcBrowsing = card.querySelector("#rcBrowsing");
  rcBrowsingIconWrap = card.querySelector("#rcBrowsingIconWrap");
  rcBrowsingDomain = card.querySelector("#rcBrowsingDomain");
  rcBrowsingPath = card.querySelector("#rcBrowsingPath");
  rcSources = card.querySelector("#rcSources");
  rcSourcesList = card.querySelector("#rcSourcesList");
  rcPlan = null;
  rcPlanSteps = null;
  rcActiveStep = null;
  rcStepCount = 0;
}

// Mark the previous active step as done and add a new one
function addStep({ label, main, sub = "", url = null, emoji = "⚙️" }) {
  // Complete the previous step
  if (rcActiveStep) {
    rcActiveStep.classList.remove("active");
    rcActiveStep.classList.add("done");
    const st = rcActiveStep.querySelector(".rc-step-status");
    if (st) st.innerHTML = '<span class="step-done-icon">✓</span>';
  }

  // Build icon — favicon for web pages, emoji otherwise
  let iconHtml = "";
  if (url) {
    try {
      const domain = new URL(url).hostname;
      iconHtml = `<img
                class="rc-favicon"
                src="https://www.google.com/s2/favicons?domain=${domain}&sz=32"
                alt="${domain}"
                onerror="this.outerHTML='🌐'"
            />`;
    } catch {
      iconHtml = "🌐";
    }
  } else {
    iconHtml = emoji;
  }

  const step = document.createElement("div");
  step.className = "rc-step active";
  step.innerHTML = `
        <div class="rc-step-icon">${iconHtml}</div>
        <div class="rc-step-text">
            <div class="rc-step-label">${label}</div>
            <div class="rc-step-main">${main}</div>
            ${sub ? `<div class="rc-step-sub">${sub}</div>` : ""}
        </div>
        <div class="rc-step-status"><div class="step-mini-spin"></div></div>
    `;

  rcStepsEl.appendChild(step);
  rcStepsEl.scrollTop = rcStepsEl.scrollHeight;
  scrollBottom();

  rcActiveStep = step;
  rcStepCount++;
}

// Build an HTML report from accumulated step results when the LLM report is absent
function buildFallbackFromSteps() {
  if (!rcStepResults.length) {
    return "<em style='color:var(--text-muted)'>The research finished but no content was returned. Try a more specific query or a model with a larger context window.</em>";
  }
  // Collect all sources across steps for the source map
  const allSrcs = rcStepResults.flatMap((r) => r.sources || []);
  let html = `<p style="color:var(--text-muted);font-size:12px;margin-bottom:12px">&#9432; The final synthesis step returned no content — showing per-step findings instead.</p>`;
  rcStepResults.forEach(({ title, content }) => {
    if (!content) return;
    html += `<h3>${title}</h3>` + md(content, allSrcs);
  });
  if (allSrcs.length) {
    const items = allSrcs
      .map((s) => {
        let domain = s.url;
        try {
          domain = new URL(s.url).hostname;
        } catch {
          /* ok */
        }
        return (
          `<li><a href="${s.url}" target="_blank" rel="noopener">` +
          `<img src="https://www.google.com/s2/favicons?domain=${domain}&sz=16" width="12" height="12" alt="" onerror="this.style.display='none'" />` +
          ` ${s.title}</a></li>`
        );
      })
      .join("");
    html +=
      `<div class="src-list">` +
      `<button class="src-list-toggle" onclick="this.closest('.src-list').classList.toggle('open')" type="button">` +
      `<span class="src-list-toggle-label">Sources</span>` +
      `<span class="src-list-count">${allSrcs.length}</span>` +
      `<span class="src-list-chevron">&#9656;</span>` +
      `</button>` +
      `<div class="src-list-body"><ol>${items}</ol></div>` +
      `</div>`;
  }
  return html;
}

// Finish the research card — swap spinner for green check, show sources
function finishResearchCard() {
  if (rcActiveStep) {
    rcActiveStep.classList.remove("active");
    rcActiveStep.classList.add("done");
    const st = rcActiveStep.querySelector(".rc-step-status");
    if (st) st.innerHTML = '<span class="step-done-icon">✓</span>';
  }
  if (rcSpinner) rcSpinner.innerHTML = '<div class="rc-done">✓</div>';
  if (rcSubtitle) rcSubtitle.textContent = "Search complete";
  if (rcProgressFill) rcProgressFill.style.width = "100%";

  // Mark all plan steps done
  rcPlanStepEls.forEach((el) => {
    el.className = "rc-plan-step done";
    const ic = el.querySelector(".rc-plan-step-icon");
    if (ic) ic.textContent = "✓";
  });

  // Reveal sources footer with favicon pills for every visited URL
  if (rcSources && rcSourcesList && rcVisitedUrls.length > 0) {
    rcSourcesList.innerHTML = rcVisitedUrls
      .map(
        ({ url, domain }) =>
          `<a href="${url}" target="_blank" rel="noopener" class="rc-source-pill" title="${url}">` +
          `<img src="https://www.google.com/s2/favicons?domain=${domain}&sz=32" alt="" onerror="this.style.display='none'" />` +
          `<span>${domain}</span></a>`,
      )
      .join("");
    rcSources.classList.remove("hidden");
  }
}

// Translate a WebSocket event from the backend into a research card step
function handleProgressEvent(event) {
  if (!rcSubtitle) return;

  // Collapse the browsing strip for non-fetch events
  function hideBrowsing() {
    if (rcBrowsing) rcBrowsing.classList.add("hidden");
  }

  switch (event.type) {
    case "thinking": {
      // Chain-of-thought from a reasoning/thinking model.
      // Attaches an expandable section to the current active step row.
      const raw = (event.content || "").trim();
      if (!raw || !rcActiveStep) break;

      const escaped = raw
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");

      // Don't add a second thinking block to the same step
      if (rcActiveStep.querySelector(".rc-think-section")) break;

      const section = document.createElement("div");
      section.className = "rc-think-section";
      section.innerHTML =
        `<div class="rc-think-toggle">` +
        `<span class="rc-think-icon">💭</span>` +
        `<span>${event.label || "Chain-of-thought"}</span>` +
        `<span class="rc-think-chevron">▾</span>` +
        `</div>` +
        `<div class="rc-think-body">${escaped}</div>`;

      section
        .querySelector(".rc-think-toggle")
        .addEventListener("click", (e) => {
          e.stopPropagation();
          section.classList.toggle("open");
          section.querySelector(".rc-think-chevron").textContent =
            section.classList.contains("open") ? "▴" : "▾";
        });

      const stepText = rcActiveStep.querySelector(".rc-step-text");
      if (stepText) stepText.appendChild(section);
      scrollBottom();
      break;
    }

    case "queries": {
      // LLM-generated search queries — show as chips in the activity log
      const qs = event.queries || [];
      if (rcProgressFill) rcProgressFill.style.width = "15%";

      if (qs.length > 1) {
        // Complete whatever was the previous active step
        if (rcActiveStep) {
          rcActiveStep.classList.remove("active");
          rcActiveStep.classList.add("done");
          const st = rcActiveStep.querySelector(".rc-step-status");
          if (st) st.innerHTML = '<span class="step-done-icon">✓</span>';
        }
        // Build a multi-search row with query chips
        const step = document.createElement("div");
        step.className = "rc-step active";
        step.innerHTML =
          `<div class="rc-step-icon">🔎</div>` +
          `<div class="rc-step-text">` +
          `<div class="rc-step-label">Searching ${qs.length} queries</div>` +
          `<div class="rc-query-list">${qs.map((q) => `<span class="rc-query-chip">${q}</span>`).join("")}</div>` +
          `</div>` +
          `<div class="rc-step-status"><div class="step-mini-spin"></div></div>`;
        rcStepsEl.appendChild(step);
        rcStepsEl.scrollTop = rcStepsEl.scrollHeight;
        scrollBottom();
        rcActiveStep = step;
        rcStepCount++;
        rcInMultiSearch = true;
      }
      rcSubtitle.textContent = `Searching ${qs.length} quer${qs.length !== 1 ? "ies" : "y"}…`;
      break;
    }

    case "search":
      hideBrowsing();
      rcSubtitle.textContent = `Searching: ${event.query || ""}`;
      // During multi-query expansion the active row already covers all queries;
      // skip creating individual search rows.
      if (!rcInMultiSearch) {
        addStep({
          label: "Web Search",
          main: event.query || event.status || "…",
          emoji: "🔍",
        });
      }
      break;

    case "search_complete":
      rcSubtitle.textContent = `Found ${event.result_count ?? "?"} results for "${event.query}"`;
      break;

    case "fetch_page": {
      let domain = event.url || "";
      let path = "";
      const full = event.url || "";
      try {
        const u = new URL(event.url);
        domain = u.hostname;
        path = u.pathname + u.search;
      } catch {
        /* ok */
      }

      // Show the browsing strip with favicon + URL breakdown
      if (rcBrowsing) {
        rcBrowsing.classList.remove("hidden");
        if (rcBrowsingIconWrap) {
          rcBrowsingIconWrap.innerHTML =
            `<img class="rc-browsing-favicon"` +
            ` src="https://www.google.com/s2/favicons?domain=${domain}&sz=32"` +
            ` alt="" onerror="this.outerHTML='\ud83c\udf10'" />`;
        }
        if (rcBrowsingDomain) rcBrowsingDomain.textContent = domain;
        if (rcBrowsingPath) rcBrowsingPath.textContent = path || full;
      }

      rcSubtitle.textContent = `Reading ${domain}`;

      // Collect unique URLs for the sources footer
      if (full && !rcVisitedUrls.some((v) => v.url === full)) {
        rcVisitedUrls.push({ url: full, domain });
      }

      addStep({
        label: "Reading Page",
        main: domain,
        sub: full,
        url: event.url,
      });
      break;
    }

    case "analyze":
      hideBrowsing();
      rcInMultiSearch = false; // synthesis phase — individual rows resume
      rcSubtitle.textContent = event.status || "Analyzing…";
      // Bump progress bar for the final summarisation step
      if (event.status && event.status.startsWith("Synthesizing")) {
        if (rcProgressFill) rcProgressFill.style.width = "85%";
      } else if (event.status && event.status.startsWith("Checking")) {
        if (rcProgressFill) rcProgressFill.style.width = "70%";
      } else if (event.status && event.status.startsWith("Reading")) {
        if (rcProgressFill) rcProgressFill.style.width = "50%";
      }
      // Only create a new row when the label changes
      if (
        !rcActiveStep ||
        rcActiveStep.querySelector(".rc-step-label")?.textContent !==
          "Analyzing"
      ) {
        addStep({
          label: "Analyzing",
          main: event.status || "Reviewing findings…",
          emoji: "🧠",
        });
      } else {
        // Update text of the existing analyze row in-place
        const m = rcActiveStep.querySelector(".rc-step-main");
        if (m) m.textContent = event.status || "Reviewing findings…";
      }
      break;

    case "plan": {
      const steps = event.steps || [];
      if (rcPlan && rcPlanSteps && steps.length > 0) {
        rcPlanStepEls = [];
        rcPlanSteps.innerHTML = "";
        steps.forEach((text, i) => {
          const el = document.createElement("div");
          el.className = "rc-plan-step pending";
          el.innerHTML =
            `<span class="rc-plan-step-num">${i + 1}</span>` +
            `<span class="rc-plan-step-text">${text}</span>` +
            `<span class="rc-plan-step-icon">○</span>`;
          rcPlanSteps.appendChild(el);
          rcPlanStepEls.push(el);
        });
        rcPlan.classList.remove("hidden");
        if (rcCountEl) {
          rcCountEl.innerHTML = `<span class="rc-iter-num">0</span> / ${steps.length} steps`;
        }
        rcMaxIterations = steps.length;
      }
      rcSubtitle.textContent = `${(event.steps || []).length} research steps planned`;
      break;
    }

    case "step": {
      hideBrowsing();
      rcInMultiSearch = false;
      const idx = event.index ?? 0;
      const total = event.total ?? rcMaxIterations;
      rcPlanStepEls.forEach((el, i) => {
        if (i < idx) {
          el.className = "rc-plan-step done";
          const ic = el.querySelector(".rc-plan-step-icon");
          if (ic) ic.textContent = "✓";
        } else if (i === idx) {
          el.className = "rc-plan-step active";
          const ic = el.querySelector(".rc-plan-step-icon");
          if (ic) ic.innerHTML = '<div class="rc-plan-step-spin"></div>';
          el.scrollIntoView({ behavior: "smooth", block: "nearest" });
        }
      });
      if (rcCountEl) {
        rcCountEl.innerHTML = `<span class="rc-iter-num">${idx + 1}</span> / ${total} steps`;
      }
      if (rcProgressFill) {
        rcProgressFill.style.width = `${(idx / total) * 85}%`;
      }
      rcSubtitle.textContent = event.query || `Step ${idx + 1}`;
      break;
    }

    case "step_result": {
      if (event.title || event.content) {
        rcStepResults.push({
          title: event.title || "",
          content: event.content || "",
          sources: event.sources || [],
        });
      }
      break;
    }

    case "step_complete": {
      const doneIdx = event.index ?? 0;
      const el = rcPlanStepEls[doneIdx];
      if (el) {
        el.className = "rc-plan-step done";
        const ic = el.querySelector(".rc-plan-step-icon");
        if (ic) ic.textContent = "✓";
      }
      break;
    }

    case "report":
      hideBrowsing();
      rcSubtitle.textContent = "Writing report…";
      if (rcProgressFill) rcProgressFill.style.width = "95%";
      addStep({
        label: "Writing Report",
        main: "Compiling all findings into a report…",
        emoji: "📝",
      });
      break;

    case "complete":
      hideBrowsing();
      finishResearchCard();
      break;
  }
}

// ── UI busy state ──────────────────────────────────────────────────────────
function setBusy(busy) {
  state.isSearching = busy;
  btnSearch.disabled = busy;
  queryInput.disabled = busy;
}

function showError(msg) {
  errorPanel.classList.remove("hidden");
  errorMessage.textContent = msg;
}

function clearError() {
  errorPanel.classList.add("hidden");
  errorMessage.textContent = "";
}

// ── Normal search (WebSocket with live progress events) ────────────────────
function runNormalSearch(query) {
  clearError();
  addToHistory("user", query);
  appendMessage("user", query);
  setBusy(true);

  createSearchCard();

  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/search`);

  ws.onopen = () => {
    ws.send(
      JSON.stringify({
        query,
        model: state.selectedModel,
        history: getHistory(),
        is_mobile: isMobile,
      }),
    );
  };

  ws.onmessage = (ev) => {
    const event = JSON.parse(ev.data);

    if (event.type === "answer" && event.content !== undefined) {
      finishResearchCard();
      setBusy(false);
      const html = md(event.content || "", event.sources || []);
      appendMessage("assistant", html || buildFallbackFromSteps());
      // Record assistant answer in conversation history
      addToHistory("assistant", event.content || "");
      rcStepResults = [];
    } else if (event.type === "error") {
      finishResearchCard();
      setBusy(false);
      showError(event.message);
    } else {
      handleProgressEvent(event);
    }
  };

  ws.onerror = () => {
    finishResearchCard();
    setBusy(false);
    showError("WebSocket connection failed. Is the server running?");
  };

  ws.onclose = () => {
    if (state.isSearching) setBusy(false);
  };
}

// ── Entry point ────────────────────────────────────────────────────────────
function handleSearch() {
  const query = queryInput.value.trim();
  if (!query || state.isSearching) return;
  queryInput.value = "";
  queryInput.style.height = "auto";
  runNormalSearch(query);
}

btnSearch.addEventListener("click", () => handleSearch());

// ── Init ───────────────────────────────────────────────────────────────────
loadModels();
