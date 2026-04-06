document.addEventListener("DOMContentLoaded", async function() {
    try {
        const i18n = browser.i18n;
        document.getElementById('header').textContent = i18n.getMessage("title");
        document.getElementById('description').textContent = i18n.getMessage("description");
        document.getElementById('learn_more').textContent = i18n.getMessage("learn_more");
    } catch (error) {
        console.error(`Exception while localizing: ${error}`);
    }
});
