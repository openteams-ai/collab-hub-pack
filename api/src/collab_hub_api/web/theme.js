// The hub pages' theme, menus and dashboard sidebar. Loaded in <head> without defer, so the
// saved or system theme is on <html data-theme>, and a folded sidebar on
// <html data-sidebar>, before the page draws, so neither flashes. The choices
// live in this browser only.
(() => {
  const KEY = "collab-hub-theme";
  const SIDEBAR_KEY = "collab-hub-sidebar";
  const root = document.documentElement;
  const system = window.matchMedia("(prefers-color-scheme: dark)");
  const read = (key) => {
    try {
      return localStorage.getItem(key);
    } catch {
      return null;
    }
  };
  const write = (key, value) => {
    try {
      localStorage.setItem(key, value);
    } catch {
      // Storage refused (private window): the switch still holds for this page.
    }
  };
  const saved = () => {
    const value = read(KEY);
    return value === "light" || value === "dark" ? value : null;
  };
  const apply = () => {
    root.dataset.theme = saved() || (system.matches ? "dark" : "light");
  };
  apply();
  if (read(SIDEBAR_KEY) === "folded") root.dataset.sidebar = "folded";
  system.addEventListener("change", apply);

  document.addEventListener("DOMContentLoaded", () => {
    for (const toggle of document.querySelectorAll("[data-theme-toggle]")) {
      toggle.addEventListener("click", () => {
        const next = root.dataset.theme === "dark" ? "light" : "dark";
        write(KEY, next);
        root.dataset.theme = next;
      });
    }

    // The dashboard sidebar folds to its icons and back.
    for (const fold of document.querySelectorAll("[data-sidebar-toggle]")) {
      const show = () => {
        const folded = root.dataset.sidebar === "folded";
        fold.setAttribute("aria-expanded", String(!folded));
        fold.setAttribute("aria-label", folded ? "Expand sidebar" : "Collapse sidebar");
      };
      show();
      fold.addEventListener("click", () => {
        const folded = root.dataset.sidebar !== "folded";
        if (folded) root.dataset.sidebar = "folded";
        else delete root.dataset.sidebar;
        write(SIDEBAR_KEY, folded ? "folded" : "open");
        show();
      });
    }

    // The account menu closes on a click anywhere else, and on Escape.
    const menus = [...document.querySelectorAll("details.menu")];
    document.addEventListener("click", (event) => {
      for (const menu of menus) if (menu.open && !menu.contains(event.target)) menu.open = false;
    });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      for (const menu of menus) {
        if (menu.open) {
          menu.open = false;
          menu.querySelector("summary").focus();
        }
      }
    });
  });
})();
