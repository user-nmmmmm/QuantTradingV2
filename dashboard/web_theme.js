/* Theme is a local display preference; it never changes trading state. */
(() => {
  const key = "stillwater-dashboard-theme";
  const root = document.documentElement;
  const system = window.matchMedia("(prefers-color-scheme: dark)");

  function savedTheme() {
    try {
      const value = window.localStorage.getItem(key);
      return value === "dark" || value === "light" ? value : null;
    } catch (_) {
      return null;
    }
  }

  function setTheme(theme) {
    root.dataset.theme = theme;
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = theme === "dark" ? "#101b1f" : "#f2f5f3";
    const button = document.getElementById("themeToggle");
    if (button) {
      const action = theme === "dark" ? "浅色" : "深色";
      const label = `切换到${action}模式`;
      button.setAttribute("aria-label", label);
      button.title = label;
      document.getElementById("themeToggleText").textContent = action;
    }
    document.dispatchEvent(new CustomEvent("dashboard:theme", { detail: { theme } }));
  }

  setTheme(savedTheme() || (system.matches ? "dark" : "light"));
  system.addEventListener("change", () => {
    if (!savedTheme()) setTheme(system.matches ? "dark" : "light");
  });
  document.addEventListener("DOMContentLoaded", () => {
    setTheme(root.dataset.theme);
    document.getElementById("themeToggle").addEventListener("click", () => {
      const next = root.dataset.theme === "dark" ? "light" : "dark";
      try { window.localStorage.setItem(key, next); } catch (_) { /* Display still switches. */ }
      setTheme(next);
    });
  });
})();
