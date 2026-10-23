// Small conveniences for the owner panel on phones.
(() => {
  // Keep the current section visible in the sideways-scrolling menu bar.
  const current = document.querySelector('.sidenav [aria-current="page"]');
  if (current) current.scrollIntoView({ block: "nearest", inline: "center" });

  // Coming back to the main panel returns to where you were in the list of sections.
  if (document.querySelector(".cards")) {
    const KEY = "owner-panel-scroll";
    try {
      const y = sessionStorage.getItem(KEY);
      if (y !== null) window.scrollTo(0, Number(y));
    } catch (e) {}
    addEventListener("pagehide", () => {
      try { sessionStorage.setItem(KEY, String(window.scrollY)); } catch (e) {}
    });
  }
})();
