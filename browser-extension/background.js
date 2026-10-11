// StreamBlur Time Bridge — service worker.
// Принимает время от content script и кладёт его в локальный мост StreamBlur
// (по умолчанию http://127.0.0.1:8799/time). URL моста можно поменять в storage.

const DEFAULT_BRIDGE = "http://127.0.0.1:8799/time";
let lastSend = 0;

async function bridgeUrl() {
  const data = await chrome.storage.local.get("bridge");
  return data.bridge || DEFAULT_BRIDGE;
}

chrome.runtime.onMessage.addListener((message) => {
  if (!message || message.type !== "time") return;
  const now = Date.now();
  if (now - lastSend < 200) return; // не чаще 5 раз в секунду
  lastSend = now;
  bridgeUrl()
    .then((url) =>
      fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(message),
      })
    )
    .catch(() => {
      /* мост не запущен — молча игнорируем */
    });
});

chrome.action.onClicked.addListener(async () => {
  const url = await bridgeUrl();
  chrome.tabs.create({ url: "http://127.0.0.1:8765/" });
  console.log("StreamBlur bridge →", url);
});
