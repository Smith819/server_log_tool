browser.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.action === "redirect" && message.url && sender.tab?.id) {
    browser.tabs.update(sender.tab.id, { url: message.url });
  }
});

if (typeof browser !== "undefined" && browser.runtime?.onMessageExternal) {
  browser.runtime.onMessageExternal.addListener((message) => {
    if (message?.type === "ext:ping") {
      return Promise.resolve({ ok: true });
    }
    return undefined;
  });
}
