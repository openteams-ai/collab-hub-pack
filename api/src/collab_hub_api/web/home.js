// The Collab Hub home page's demo window: a model picker and a mock chat.
// Nothing leaves the page: replies are written here, from the message, the
// chosen model and the Frames that are on. User text only ever reaches the
// page through textContent.
(() => {
  const app = document.querySelector("[data-app]");
  if (!app) return;

  const $ = (sel) => app.querySelector(sel);
  const $$ = (sel) => [...app.querySelectorAll(sel)];
  const msgs = $("[data-msgs]");
  const input = $("[data-input]");
  const menu = $("[data-menu]");
  const modelBtn = $("[data-model-btn]");
  let model = $("[data-model-label]").textContent;
  let busy = false;

  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  // Popover menus: the model picker and the connections. Opening one closes
  // the other; a click outside or Escape closes both.
  const connBtn = $("[data-conn-btn]");
  const connMenu = $("[data-conn-menu]");
  const popovers = [
    [modelBtn, menu],
    [connBtn, connMenu],
  ];
  for (const [button, pop] of popovers) {
    button.addEventListener("click", () => {
      const open = pop.hidden;
      popovers.forEach(([, other]) => (other.hidden = true));
      pop.hidden = !open;
    });
  }
  document.addEventListener("click", (event) => {
    for (const [button, pop] of popovers) {
      if (!pop.hidden && !pop.contains(event.target) && !button.contains(event.target)) pop.hidden = true;
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") popovers.forEach(([, pop]) => (pop.hidden = true));
  });

  // Connections: each switch turns a tool on or off for this chat.
  const connected = () =>
    $$("[data-conn]")
      .filter((o) => o.getAttribute("aria-checked") === "true")
      .map((o) => o.dataset.conn);
  for (const option of $$("[data-conn]")) {
    option.addEventListener("click", () => {
      option.setAttribute("aria-checked", option.getAttribute("aria-checked") === "true" ? "false" : "true");
      const on = connected();
      $("[data-conn-label]").textContent = on.length ? `Connections: ${on.join(", ")}` : "No connections";
    });
  }
  for (const option of $$(".model-opt")) {
    option.addEventListener("click", () => {
      model = option.dataset.model;
      $$(".model-opt").forEach((o) => o.classList.toggle("on", o === option));
      $("[data-model-label]").textContent = model;
      $("[data-model-name]").textContent = model;
      const logo = option.querySelector(".logo").cloneNode(true);
      modelBtn.querySelector(".logo").replaceWith(logo);
      menu.hidden = true;
      input.focus();
    });
  }

  // Frames: the chips above the input and the panel's checkboxes agree.
  const activeFrames = () => $$("[data-frame-toggle]").filter((box) => box.checked).map((box) => box.dataset.frameToggle);
  const renderChips = () => {
    const bar = $(".frame-bar");
    bar.querySelectorAll(".fchip").forEach((chip) => chip.remove());
    for (const name of activeFrames()) {
      const chip = el("span", "fchip", name);
      chip.dataset.frame = name;
      const remove = el("button", "", "×");
      remove.type = "button";
      remove.setAttribute("aria-label", `Remove ${name}`);
      remove.addEventListener("click", () => {
        app.querySelector(`[data-frame-toggle="${CSS.escape(name)}"]`).checked = false;
        renderChips();
      });
      chip.append(remove);
      bar.append(chip);
    }
    $("[data-frame-count]").textContent = `Frames · ${activeFrames().length}`;
  };
  $$("[data-frame-toggle]").forEach((box) => box.addEventListener("change", renderChips));
  renderChips();

  // The made-up reply.
  const reply = (text, frames) => {
    const lower = text.toLowerCase();
    const context = frames.length
      ? `Using your ${frames.join(" and ")} Frame${frames.length > 1 ? "s" : ""}`
      : "No Frames are on for this chat, so this is without your team's context";
    if (/drive|search|find|past performance/.test(lower)) {
      if (!connected().includes("Drive")) {
        return {
          tool: null,
          text: `${context}. Google Drive is switched off for this chat, so I can't search it. Turn it on under Connections and ask again.`,
        };
      }
      return {
        tool: { name: "search_drive", query: text.slice(0, 48) },
        text: `${context}. I searched Google Drive and found three matching files: "Healthcare past performance 2025", "Clinic rollout case study" and "Q3 pipeline review". The case study is the strongest fit: it covers a 14-site rollout finished two weeks early. Want a one-page summary of it?`,
      };
    }
    if (/launch|update|friday|draft/.test(lower)) {
      return {
        tool: null,
        text: `${context}, here is a draft for Friday.\n\nWe launch on 18 September. Collab gives every team one AI workspace that already knows their projects, works with the tools they use, and runs on their own hub. This week: the pricing page is final, docs are in review, and support has its playbook. Next: the customer webinar on the 22nd.`,
      };
    }
    return {
      tool: null,
      text: `${context}. This is a demo, so I'm ${model} in name only: in Collab this answer would come from ${model} through your hub's gateway, with your Frames and connected tools in reach.`,
    };
  };

  const scrollDown = () => {
    msgs.scrollTop = msgs.scrollHeight;
  };

  const send = (text) => {
    text = text.trim();
    if (!text || busy) return;
    busy = true;
    $("[data-empty]")?.remove();
    if ($("[data-title]").textContent === "New chat") {
      $("[data-title]").textContent = text.length > 44 ? `${text.slice(0, 44)}...` : text;
      $("[data-history]").textContent = $("[data-title]").textContent;
    }
    msgs.append(el("div", "msg me", text));
    input.value = "";
    const frames = activeFrames();
    const answer = reply(text, frames);
    const thinking = el("div", "msg ai thinking", `${model} is thinking`);
    msgs.append(thinking);
    scrollDown();

    setTimeout(() => {
      thinking.remove();
      if (answer.tool) {
        const call = el("details", "toolcall");
        call.append(el("summary", "", "1 tool called"));
        const body = el("div", "toolbody");
        body.append(el("code", "", answer.tool.name), el("span", "", `query: "${answer.tool.query}"`));
        call.append(body);
        msgs.append(call);
      }
      const out = el("div", "msg ai");
      const meta = el("div", "msg-meta", `${model}${frames.length ? ` · ${frames.length} Frame${frames.length > 1 ? "s" : ""}` : ""}`);
      const body = el("div", "msg-body");
      out.append(meta, body);
      msgs.append(out);
      const words = answer.text.split(/(\s+)/);
      let i = 0;
      const tick = setInterval(() => {
        body.textContent += words.slice(i, i + 3).join("");
        i += 3;
        scrollDown();
        if (i >= words.length) {
          clearInterval(tick);
          busy = false;
        }
      }, 40);
    }, 700);
  };

  $("[data-form]").addEventListener("submit", (event) => {
    event.preventDefault();
    send(input.value);
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      send(input.value);
    }
  });
  for (const starter of $$("[data-starter]")) starter.addEventListener("click", () => send(starter.textContent));

  // New Chat: back to the empty state the page was served with.
  const empty = msgs.cloneNode(true);
  $("[data-new]").addEventListener("click", () => {
    if (busy) return;
    msgs.replaceChildren(...empty.cloneNode(true).childNodes);
    for (const starter of msgs.querySelectorAll("[data-starter]")) starter.addEventListener("click", () => send(starter.textContent));
    $("[data-title]").textContent = "New chat";
    input.focus();
  });
})();
