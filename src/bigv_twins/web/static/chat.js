// chat.js — POST a message, parse SSE stream of {delta: "..."} or [DONE]

(function () {
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

  function appendBubble(role, text) {
    if (emptyHint) emptyHint.remove();
    const div = document.createElement("div");
    div.className = "chat-bubble " + role;
    const pre = document.createElement("pre");
    pre.textContent = text;
    div.appendChild(pre);
    messagesEl.appendChild(div);
    scrollDown();
    return pre;
  }

  async function send() {
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    sendBtn.disabled = true;
    sendBtn.textContent = "…";

    appendBubble("user", text);
    const assistantPre = appendBubble("assistant", "🔍 检索中…");
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
        assistantPre.textContent = `⚠ HTTP ${resp.status}: ${t.slice(0,200)}`;
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
            if (payload === "[DONE]") {
              continue;
            }
            try {
              const obj = JSON.parse(payload);
              if (obj.error) {
                assistantPre.textContent = `⚠ ${obj.error}`;
              } else if (typeof obj.delta === "string") {
                if (firstDelta) {
                  assistantPre.textContent = "";
                  firstDelta = false;
                }
                buf += obj.delta;
                assistantPre.textContent = buf;
                scrollDown();
              }
            } catch (e) {
              console.warn("bad SSE payload", payload, e);
            }
          }
        }
      }
      if (firstDelta) {
        // never got a delta
        assistantPre.textContent = buf || "(空响应)";
      }
    } catch (err) {
      assistantPre.textContent = "⚠ " + err.message;
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

  // auto-scroll initial state to bottom
  scrollDown();
})();
