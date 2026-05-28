// chat.js — POST a message, parse SSE stream of {delta: "..."} or [DONE]
// Render assistant messages as markdown via marked + DOMPurify.
// All <a> links open in a new tab via a DOMPurify hook.

(function () {
  // ---- markdown setup ----------------------------------------

  if (typeof marked !== "undefined") {
    marked.setOptions({
      breaks: true,
      gfm: true,
      headerIds: false,
      mangle: false,
    });
  }

  // Make every rendered <a> open in a new tab, safely.
  // DOMPurify's afterSanitizeAttributes hook runs on every sanitize() call.
  if (typeof DOMPurify !== "undefined") {
    DOMPurify.addHook("afterSanitizeAttributes", function (node) {
      if (node.tagName === "A" && node.hasAttribute("href")) {
        node.setAttribute("target", "_blank");
        node.setAttribute("rel", "noopener noreferrer");
      }
    });
  }

  function renderMarkdown(text) {
    if (typeof marked === "undefined" || typeof DOMPurify === "undefined") {
      return null;
    }
    const html = marked.parse(text || "");
    return DOMPurify.sanitize(html, {
      ALLOWED_ATTR: ["href", "title", "target", "rel", "src", "alt"],
    });
  }

  // Render historical assistant bubbles on page load.
  document.querySelectorAll(".chat-bubble.assistant[data-raw]").forEach((el) => {
    const raw = el.getAttribute("data-raw");
    const html = renderMarkdown(raw);
    if (html !== null) el.innerHTML = html;
  });

  // ---- reconnect to in-flight LLM response ------------------
  // If the last message is from the user (no assistant reply yet), there might
  // be a background LLM task still running. Try to connect to its stream.
  async function tryReconnectStream(cid, messagesEl) {
    if (!messagesEl) return;
    const bubbles = messagesEl.querySelectorAll(".chat-bubble");
    if (bubbles.length === 0) return;
    const last = bubbles[bubbles.length - 1];
    if (!last.classList.contains("user")) return;  // already has assistant reply

    // Append placeholder
    const assistantEl = document.createElement("div");
    assistantEl.className = "chat-bubble assistant";
    assistantEl.textContent = "🔍 检索中…（重新连接到后台任务）";
    messagesEl.appendChild(assistantEl);
    messagesEl.scrollTop = messagesEl.scrollHeight;

    let buf = "";
    let firstDelta = true;
    try {
      const resp = await fetch(`/chat/${cid}/stream`);
      if (!resp.ok) {
        assistantEl.remove();
        return;
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let leftover = "";
      while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        leftover += decoder.decode(value, {stream: true});
        const events = leftover.split("\n\n");
        leftover = events.pop() || "";
        for (const ev of events) {
          for (const line of ev.split("\n")) {
            if (!line.startsWith("data:")) continue;
            const payload = line.slice(5).trim();
            if (payload === "[DONE]") continue;
            try {
              const obj = JSON.parse(payload);
              if (obj.delta) {
                if (firstDelta) { assistantEl.textContent = ""; firstDelta = false; }
                buf += obj.delta;
                const html = renderMarkdown(buf);
                if (html !== null) assistantEl.innerHTML = html;
                else assistantEl.textContent = buf;
                messagesEl.scrollTop = messagesEl.scrollHeight;
              } else if (obj.error) {
                assistantEl.textContent = `⚠ ${obj.error}`;
              }
            } catch (e) { /* ignore */ }
          }
        }
      }
      if (!buf) {
        // No in-flight task and no saved msg — remove placeholder
        assistantEl.remove();
      }
    } catch (err) {
      console.warn("reconnect failed:", err);
      assistantEl.remove();
    }
  }

  // ---- chat form ---------------------------------------------

  const form = document.getElementById("ask-form");
  if (!form) return;
  const cid = form.dataset.cid;
  const input = document.getElementById("msg-input");
  const sendBtn = document.getElementById("send-btn");
  const messagesEl = document.getElementById("messages");
  const emptyHint = document.getElementById("empty-hint");

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

  function appendAssistantBubble(placeholder) {
    if (emptyHint) emptyHint.remove();
    const div = document.createElement("div");
    div.className = "chat-bubble assistant";
    div.textContent = placeholder;
    messagesEl.appendChild(div);
    scrollDown();
    return div;
  }

  function updateAssistant(bubble, text) {
    const html = renderMarkdown(text);
    if (html === null) {
      bubble.textContent = text;
    } else {
      bubble.innerHTML = html;
    }
    scrollDown();
  }

  async function send() {
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    sendBtn.disabled = true;
    sendBtn.textContent = "…";

    appendUserBubble(text);
    const assistantEl = appendAssistantBubble("🔍 检索中…");
    let buf = "";
    let firstDelta = true;

    try {
      const resp = await fetch(`/chat/${cid}/ask`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({message: text}),
      });
      if (!resp.ok) {
        const t = await resp.text();
        assistantEl.textContent = `⚠ HTTP ${resp.status}: ${t.slice(0,200)}`;
        return;
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let leftover = "";

      while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        leftover += decoder.decode(value, {stream: true});
        const events = leftover.split("\n\n");
        leftover = events.pop() || "";
        for (const ev of events) {
          for (const line of ev.split("\n")) {
            if (!line.startsWith("data:")) continue;
            const payload = line.slice(5).trim();
            if (payload === "[DONE]") continue;
            try {
              const obj = JSON.parse(payload);
              if (obj.error) {
                assistantEl.textContent = `⚠ ${obj.error}`;
              } else if (typeof obj.delta === "string") {
                if (firstDelta) {
                  assistantEl.textContent = "";
                  firstDelta = false;
                }
                buf += obj.delta;
                updateAssistant(assistantEl, buf);
              }
            } catch (e) {
              console.warn("bad SSE payload", payload, e);
            }
          }
        }
      }
      if (firstDelta) {
        assistantEl.textContent = buf || "(空响应)";
      }
    } catch (err) {
      assistantEl.textContent = "⚠ " + err.message;
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

  // On page load, check for in-flight response
  tryReconnectStream(cid, messagesEl);
})();
