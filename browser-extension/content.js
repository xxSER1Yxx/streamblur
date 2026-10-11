// StreamBlur Time Bridge — content script.
// Находит самый крупный активный <video> на странице и раз в 250 мс отправляет
// его currentTime в фоновый скрипт, а тот — в локальный мост StreamBlur.

const STATE = { video: null, timer: null };

function pickVideo() {
  const videos = Array.from(document.querySelectorAll("video"));
  if (!videos.length) return null;
  // Приоритет: видео, которое сейчас играет; иначе самое большое по площади.
  const playing = videos.find((v) => !v.paused && v.readyState > 1);
  if (playing) return playing;
  return videos.sort(
    (a, b) => (b.clientWidth * b.clientHeight) - (a.clientWidth * a.clientHeight)
  )[0];
}

function ensure() {
  const video = pickVideo();
  if (!video || video === STATE.video) return;
  STATE.video = video;
  video.addEventListener("emptied", () => { STATE.video = null; }, { once: true });
}

function tick() {
  ensure();
  const video = STATE.video;
  if (!video || !Number.isFinite(video.currentTime)) return;
  chrome.runtime
    .sendMessage({
      type: "time",
      position: video.currentTime,
      duration: Number.isFinite(video.duration) ? video.duration : null,
      paused: video.paused,
      title: document.title,
      url: location.href,
    })
    .catch(() => {});
}

STATE.timer = setInterval(tick, 250);
window.addEventListener("beforeunload", () => clearInterval(STATE.timer));
