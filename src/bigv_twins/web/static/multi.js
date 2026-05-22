// multi.js — POST a message, parse multi-stream SSE, route deltas to the right
// blogger card. After all bloggers done, stream the summary into a separate card.
// Renders markdown via marked + DOMPurify (loaded by base.html).

(function () {
  if (typeof marked !== "undefined") {
    marked.setOptions({ breaks: true, gfm: true, headerIds: false, mangle: false });
  }
  if (typeof DOMPurify !== "undefined") {
    DOMPurify.addHook("afterSanitizeAttributes", function (node) {
      if (node.tagName === "A" && node.hasAttribute("href")) {
        node.setAttribute("target", "_blank");
        node.setAttribute("rel", "noopener noreferrer");
      }
    });
  }
  function renderMarkdown(text) {
    if (typeof marked === "undefined" || typeof DOMPurify === "undefined") return null;
    const html = marked.parse(text || "");
    return DOMPurify.sanitize(html, {
      ALLOWED_ATTR: ["href", "title", "target", "rel", "src", "alt"],
    });
  }

  // Render historical multi cards + summary on page load
  document.querySelectorAll(".multi-card-body[data-raw], .multi-summary[data-raw]").forEach((el) => {
    const raw = el.getAttribute("data-raw");
    if (!raw) return;
    const html = renderMarkdown(raw);
    if (html !== null) el.innerHTML = html;
  });

  const form = document.getElementById("multi-ask-form");
  if (!form) return;
  const cid = form.dataset.cid;
  const input = document.getElementById("msg-input");
  const sendBtn = document.getElementById("send-btn");
  const messagesEl = document.getElementById("messages");
  const emptyHint = document.getElementById("empty-hint");

  const participants = window.MULTI_PARTICIPANTS || [];

  function scrollDown() {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function appendUserBubble(text) {
    if (emptyHint) emptyHint.remove();
    const div = document.createElement("div");
    div.className = "chat-bubble user";
    div.textContent = text;
    messagesEl.appendChild(div);
    scrollDown();
    return div;
  }

  // Build the empty card grid for a new turn. Returns { cards: {slug → bodyEl}, summaryEl }.
  function appendTurnCards() {
    const grid = document.createElement("div");
    grid.className = "multi-cards";
    const cards = {};
    for (const p of participants) {
      const article = document.createElement("article");
      article.className = `multi-card multi-card-${p.kind}`;
      article.dataset.blogger = p.slug;

      const head = document.createElement("header");
      head.className = "multi-card-head";
      const icon = p.kind === "advisor" ? "🤖 " : (p.kind === "master" ? "📜 " : "");
      head.innerHTML = `${icon}<strong>${p.name}</strong><span class="multi-card-status">⌛ 等待…</span>`;
      article.appendChild(head);

      const body = document.createElement("div");
      body.className = "multi-card-body";
      body.textContent = "";
      article.appendChild(body);

      grid.appendChild(article);
      cards[p.slug] = { article, body, head, buf: "" };
    }
    messagesEl.appendChild(grid);

    // Summary placeholder (initially hidden until summary_delta arrives)
    const summary = document.createElement("div");
    summary.className = "multi-summary";
    summary.style.display = "none";
    summary.dataset.raw = "";
    messagesEl.appendChild(summary);

    scrollDown();
    return { cards, summaryEl: summary };
  }

  function setStatus(card, label) {
    const el = card.head.querySelector(".multi-card-status");
    if (el) el.textContent = label;
  }

  async function send() {
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    sendBtn.disabled = true;
    sendBtn.textContent = "…";

    appendUserBubble(text);
    const { cards, summaryEl } = appendTurnCards();
    // Mark all cards as 检索中
    for (const slug of Object.keys(cards)) setStatus(cards[slug], "🔍 检索中…");

    let summaryBuf = "";

    try {
      const resp = await fetch(`/multi/${cid}/ask`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text }),
      });
      if (!resp.ok) {
        const t = await resp.text();
        for (const slug of Object.keys(cards)) {
          cards[slug].body.textContent = `⚠ HTTP ${resp.status}: ${t.slice(0, 200)}`;
          setStatus(cards[slug], "❌");
        }
        return;
      }

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let leftover = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        leftover += decoder.decode(value, { stream: true });
        const events = leftover.split("\n\n");
        leftover = events.pop() || "";
        for (const ev of events) {
          for (const line of ev.split("\n")) {
            if (!line.startsWith("data:")) continue;
            const payload = line.slice(5).trim();
            if (payload === "[DONE]") continue;
            let obj;
            try {
              obj = JSON.parse(payload);
            } catch (e) {
              console.warn("bad SSE payload", payload, e);
              continue;
            }
            const ev = obj.event;
            if (ev === "blogger_start") {
              const c = cards[obj.blogger];
              if (c) setStatus(c, "✍ 回答中…");
            } else if (ev === "blogger_delta") {
              const c = cards[obj.blogger];
              if (!c) continue;
              c.buf += obj.content || "";
              const html = renderMarkdown(c.buf);
              if (html !== null) c.body.innerHTML = html;
              else c.body.textContent = c.buf;
              scrollDown();
            } else if (ev === "blogger_done") {
              const c = cards[obj.blogger];
              if (c) setStatus(c, "✓");
            } else if (ev === "blogger_error") {
              const c = cards[obj.blogger];
              if (!c) continue;
              setStatus(c, "❌ 失败");
              const note = document.createElement("small");
              note.className = "muted";
              note.style.cssText = "display:block; padding:0 0.6rem 0.4rem";
              note.textContent = obj.error || "";
              c.article.appendChild(note);
            } else if (ev === "all_blogger_done") {
              summaryEl.style.display = "";
              summaryEl.innerHTML = '<small class="muted">📊 汇总中…</small>';
            } else if (ev === "summary_delta") {
              summaryBuf += obj.content || "";
              const html = renderMarkdown(summaryBuf);
              if (html !== null) summaryEl.innerHTML = html;
              else summaryEl.textContent = summaryBuf;
              summaryEl.dataset.raw = summaryBuf;
              scrollDown();
            } else if (ev === "summary_done") {
              // nothing — keep rendered content
            } else if (ev === "summary_error" || ev === "fatal_error") {
              summaryEl.style.display = "";
              summaryEl.innerHTML = `<small style="color:#dc2626">⚠ ${obj.error || "summary failed"}</small>`;
            }
          }
        }
      }
    } catch (err) {
      for (const slug of Object.keys(cards)) {
        if (!cards[slug].buf) cards[slug].body.textContent = "⚠ " + err.message;
      }
    } finally {
      sendBtn.disabled = false;
      sendBtn.textContent = "发送";
      input.focus();
    }
  }

  form.addEventListener("submit", (e) => { e.preventDefault(); send(); });
  input.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      send();
    }
  });

  scrollDown();
})();
