"use strict";

(function() {
  const LOCAL_STORAGE_SKIP_QUERIES_KEY = "chatgpt-search-skip-queries";

  /// A set of queries that the user has asked to skip redirecting by using the `!g ` prefix.
  const skipQueries = loadSkipQueries();

  function loadSkipQueries() {
    try {
      return new Set(JSON.parse(sessionStorage.getItem(LOCAL_STORAGE_SKIP_QUERIES_KEY) || "[]"));
    } catch {
      return new Set();
    }
  }

  function saveSkipQueries() {
    sessionStorage.setItem(LOCAL_STORAGE_SKIP_QUERIES_KEY, JSON.stringify([...skipQueries]));
  }

  function getQueryParam(url, param) {
    const urlObj = new URL(url);
    return new URLSearchParams(urlObj.search).get(param);
  }

  function normalizeQuery(q) {
    return q.trim().toLowerCase();
  }

  const url = window.location.href;
  const hostname = window.location.hostname;
  const pathname = window.location.pathname;

  if (!/^www\.google\.[a-z.]+$/.test(hostname) || pathname !== "/search") {
    return;
  }

  if (getQueryParam(url, "client") !== "safari") {
    // This means that we arrived at this page by other means
    // than the user typing a query into the Safari omnibox.
    return;
  }

  const originalQuery = getQueryParam(url, "q");
  if (!originalQuery) {
    return; // no q= param, so do nothing
  }

  function isSkipQuery(query) {
    return skipQueries.has(normalizeQuery(query));
  }

  function setSkipQuery(query) {
    skipQueries.add(normalizeQuery(query));
    saveSkipQueries();
  }

  (async function main() {
    // 1. Check if we should skip
    const skipAlreadySet = isSkipQuery(originalQuery);
    if (skipAlreadySet) {
      return;
    }

    // 2. If the user typed "!g ", let's skip and re-search
    if (originalQuery.startsWith("!g ")) {
      // Strip "!g " from the query
      const strippedQuery = originalQuery.slice(2).trim();

      // Mark this stripped query as skip
      setSkipQuery(strippedQuery);

      // Redirect to Google with the stripped query
      const newUrl = new URL(url);
      newUrl.searchParams.set("q", strippedQuery);

      location.replace(newUrl.toString());
      return;
    }

    const isMacOS = navigator.platform.startsWith("Mac");
    const clientReportedSearchSource = isMacOS ? "safari_macos_browser_bar" : "safari_ios_browser_bar";

    // 3. Otherwise, do the ChatGPT redirect
    const redirectURL = "https://chatgpt.com/?q=" + encodeURIComponent(originalQuery) + "&hints=search&client_reported_search_source=" + clientReportedSearchSource;
    const backgroundColor = window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "#000000"
      : "#ffffff";

    document.documentElement.innerHTML = `
      <meta name="theme-color" content="${backgroundColor}">
      <body style="background:${backgroundColor}"></body>
    `;

    browser.runtime.sendMessage({ action: "redirect", url: redirectURL });
  })();
})();
