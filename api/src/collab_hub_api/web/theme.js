// The hub pages' theme and menus. Loaded in <head> without defer, so the
// saved or system theme is on <html data-theme> before the page draws and it
// never flashes the other one. The choice lives in this browser only.
(() => {
  const KEY = "collab-hub-theme";
  const root = document.documentElement;
  const system = window.matchMedia("(prefers-color-scheme: dark)");
  const saved = () => {
    try {
      const value = localStorage.getItem(KEY);
      return value === "light" || value === "dark" ? value : null;
    } catch {
      return null;
    }
  };
  const apply = () => {
    root.dataset.theme = saved() || (system.matches ? "dark" : "light");
  };
  apply();
  system.addEventListener("change", apply);

  document.addEventListener("DOMContentLoaded", () => {
    for (const toggle of document.querySelectorAll("[data-theme-toggle]")) {
      toggle.addEventListener("click", () => {
        const next = root.dataset.theme === "dark" ? "light" : "dark";
        try {
          localStorage.setItem(KEY, next);
        } catch {
          // Storage refused (private window): the switch still holds for this page.
        }
        root.dataset.theme = next;
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
