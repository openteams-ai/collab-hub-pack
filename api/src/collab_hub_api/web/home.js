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

  // The made-up reply. Some carry a chart, drawn into the chat after the
  // text, and a closing line after the chart.
  const reply = (text, frames) => {
    const lower = text.toLowerCase();
    const context = frames.length
      ? `Using your ${frames.join(" and ")} Frame${frames.length > 1 ? "s" : ""}`
      : "No Frames are on for this chat, so this is without your team's context";
    if (/q4|quarter|revenue|report/.test(lower)) {
      return {
        tool: { name: "read_sheet", query: "Finance / Revenue by month 2026" },
        text: `${context}. Q4 revenue came to $1.41M, up 18% on Q3. Every month beat the one before it, and December was the best month of the year at $531k, carried by the healthcare renewals.`,
        chart: {
          kind: "columns",
          title: "Revenue by month, $k",
          legend: [
            ["Q3", "lo"],
            ["Q4", "hi"],
          ],
          rows: [
            ["Jul", 371, "lo"],
            ["Aug", 388, "lo"],
            ["Sep", 436, "lo"],
            ["Oct", 412, "hi"],
            ["Nov", 468, "hi"],
            ["Dec", 531, "hi"],
          ],
        },
        after: "Want this as a one-page report for Monday's leadership meeting?",
      };
    }
    if (/churn|at risk|customers|health score/.test(lower)) {
      return {
        tool: { name: "search_slack", query: "#customer-health, last 30 days" },
        text: `${context}. Four accounts look at risk. Northwind and Globex both opened support escalations this month, and usage at Initech has halved since their admin left.`,
        chart: {
          kind: "bars",
          title: "Account health score, out of 100",
          rows: [
            ["Northwind", 38, "hi"],
            ["Globex", 44, "hi"],
            ["Initech", 51, "hi"],
            ["Umbrella", 58, "hi"],
            ["Acme", 86, "lo"],
            ["Hooli", 91, "lo"],
          ],
        },
        after: "I can draft a check-in email to each account owner. Shall I?",
      };
    }
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

  // A chart as SVG. "columns" stand up from a baseline; "bars" run left to
  // right with their names beside them. Each bar's delay staggers its growth.
  const SVG = "http://www.w3.org/2000/svg";
  const svgEl = (tag, attrs, text) => {
    const node = document.createElementNS(SVG, tag);
    for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, value);
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const drawChart = (chart) => {
    const card = el("figure", "chart");
    const head = el("figcaption", "chart-head");
    head.append(el("span", "chart-title", chart.title));
    if (chart.legend) {
      const legend = el("span", "chart-legend");
      for (const [label, tone] of chart.legend) {
        const item = el("span", "chart-key");
        item.append(el("i", tone), document.createTextNode(label));
        legend.append(item);
      }
      head.append(legend);
    }
    const max = Math.max(...chart.rows.map(([, value]) => value));
    const svg = svgEl("svg", { viewBox: "0 0 360 170", role: "img", "aria-label": chart.title });
    if (chart.kind === "columns") {
      const base = 146;
      const top = 16;
      const step = 340 / chart.rows.length;
      const width = step * 0.56;
      for (const f of [0.25, 0.5, 0.75, 1]) {
        const y = base - (base - top) * f;
        svg.append(svgEl("line", { class: "grid", x1: 10, x2: 350, y1: y, y2: y }));
      }
      chart.rows.forEach(([label, value, tone], i) => {
        const h = ((base - top) * value) / max;
        const x = 10 + step * i + (step - width) / 2;
        const bar = svgEl("rect", { class: `bar ${tone}`, x, y: base - h, width, height: h, rx: 3 });
        bar.style.animationDelay = `${i * 110}ms`;
        const num = svgEl("text", { class: "num", x: x + width / 2, y: base - h - 5, "text-anchor": "middle" }, value);
        num.style.animationDelay = `${i * 110 + 400}ms`;
        const axis = svgEl("text", { class: "axis", x: x + width / 2, y: base + 16, "text-anchor": "middle" }, label);
        svg.append(bar, num, axis);
      });
      svg.append(svgEl("line", { class: "base", x1: 10, x2: 350, y1: base, y2: base }));
    } else {
      const left = 78;
      const right = 326;
      const step = 166 / chart.rows.length;
      const height = step * 0.58;
      chart.rows.forEach(([label, value, tone], i) => {
        const y = 2 + step * i + (step - height) / 2;
        const w = ((right - left) * value) / max;
        const axis = svgEl("text", { class: "axis", x: left - 8, y: y + height / 2 + 4, "text-anchor": "end" }, label);
        const bar = svgEl("rect", { class: `hbar ${tone}`, x: left, y, width: w, height, rx: 3 });
        bar.style.animationDelay = `${i * 90}ms`;
        const num = svgEl("text", { class: "num", x: left + w + 6, y: y + height / 2 + 4 }, value);
        num.style.animationDelay = `${i * 90 + 400}ms`;
        svg.append(axis, bar, num);
      });
    }
    card.append(head, svg);
    return card;
  };

  const scrollDown = () => {
    msgs.scrollTop = msgs.scrollHeight;
  };
  const wait = (ms) => new Promise((done) => setTimeout(done, ms));

  // Writes text into a node a few words at a time, the way a model streams.
  const stream = (node, text) =>
    new Promise((done) => {
      const words = text.split(/(\s+)/);
      let i = 0;
      const tick = setInterval(() => {
        node.textContent += words.slice(i, i + 3).join("");
        i += 3;
        scrollDown();
        if (i >= words.length) {
          clearInterval(tick);
          done();
        }
      }, 40);
    });

  const send = async (text) => {
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

    await wait(700);
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
    await stream(body, answer.text);
    if (answer.chart) {
      out.append(drawChart(answer.chart));
      scrollDown();
      await wait(1200);
    }
    if (answer.after) {
      const after = el("div", "msg-body msg-after");
      out.append(after);
      await stream(after, answer.after);
    }
    busy = false;
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
  const wireStarters = () => {
    for (const starter of msgs.querySelectorAll("[data-starter]")) starter.addEventListener("click", () => send(starter.textContent));
  };
  wireStarters();

  // New Chat: back to the empty state the page was served with.
  const empty = msgs.cloneNode(true);
  const newChat = () => {
    msgs.replaceChildren(...empty.cloneNode(true).childNodes);
    wireStarters();
    $("[data-title]").textContent = "New chat";
  };
  $("[data-new]").addEventListener("click", () => {
    if (busy) return;
    newChat();
    input.focus();
  });

  // The window plays itself once it is on screen: a prompt is typed and
  // answered, then the next one in a fresh chat. It stops for good the
  // moment the visitor clicks or types in the window (scrolling past does not
  // count), so it never types over them.
  const DEMOS = ["Create a report for Q4", "Which customers are at risk of churning?", "Draft the launch update for Friday"];
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  let touched = false;
  let visible = false;
  const stop = (event) => {
    if (event.isTrusted) touched = true;
  };
  for (const type of ["click", "keydown"]) app.addEventListener(type, stop);
  const chatShown = () => document.getElementById("view-chat")?.checked ?? true;
  const typeIn = async (text) => {
    for (const char of text) {
      if (touched) {
        // Clear the half-typed prompt, unless the visitor has typed into it.
        if (text.startsWith(input.value)) input.value = "";
        return false;
      }
      input.value += char;
      await wait(38 + Math.random() * 40);
    }
    return true;
  };
  const play = async () => {
    for (let i = 0; !touched; i = (i + 1) % DEMOS.length) {
      while (!touched && !(visible && chatShown())) await wait(400);
      if (touched) return;
      if ($("[data-title]").textContent !== "New chat") newChat();
      await wait(600);
      if (!(await typeIn(DEMOS[i]))) return;
      await wait(350);
      if (touched) return;
      await send(input.value);
      await wait(3500);
    }
  };
  let started = false;
  new IntersectionObserver(
    ([entry]) => {
      visible = entry.isIntersecting;
      if (visible && !started) {
        started = true;
        play();
      }
    },
    { threshold: 0.45 },
  ).observe(app);
})();
