(async () => {
  const { key, scanUrl, pingUrl, savePhotos } = window.KIOSK;
  const video = document.getElementById("video");
  const overlay = document.getElementById("overlay");
  const resultEl = document.getElementById("result");
  const logEl = document.getElementById("log");
  const countEl = document.getElementById("count");
  const guideEl = document.getElementById("guide");

  // Clock
  const clockEl = document.getElementById("clock"), todayEl = document.getElementById("today");
  const tick = () => {
    const n = new Date();
    clockEl.textContent = n.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    todayEl.textContent = n.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long", year: "numeric" });
  };
  tick(); setInterval(tick, 1000);

  let resetTimer = null;
  function show(cls, big, small, holdMs = 4000) {
    resultEl.className = "result " + cls;
    resultEl.innerHTML = `<div class="big"></div><div class="small"></div>`;
    resultEl.querySelector(".big").textContent = big;
    resultEl.querySelector(".small").textContent = small;
    clearTimeout(resetTimer);
    if (holdMs) resetTimer = setTimeout(idle, holdMs);
  }
  const idle = () => show("", "Please look at the camera", "Attendance is marked automatically", 0);

  function addLog(text) {
    const s = document.createElement("span");
    s.textContent = text;
    logEl.prepend(s);
    while (logEl.children.length > 6) logEl.lastChild.remove();
  }

  try {
    show("", "Loading face models…", "Please wait", 0);
    await Face.loadModels();
    show("", "Starting camera…", "Please allow camera access", 0);
    await Face.startCamera(video);
  } catch (e) {
    if (e.name === "NotAllowedError") {
      show("unknown", "Camera is blocked", "Allow the camera in the browser's address bar, then reload this page", 0);
    } else if (e.name === "NotFoundError") {
      show("unknown", "No camera found", "Connect a webcam, then reload this page", 0);
    } else {
      show("unknown", "Camera / model error", e.message || String(e), 0);
    }
    return;
  }
  idle();
  guideEl.hidden = false;

  // Tell the server this kiosk is alive (the admin dashboard shows it) and show today's count
  async function ping() {
    try {
      const res = await fetch(pingUrl, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ key }),
      });
      const d = await res.json();
      countEl.className = "";
      countEl.textContent = `${d.checked_in} of ${d.total} checked in today`;
    } catch (_) {
      countEl.className = "offline";
      countEl.textContent = "No connection to the server";
    }
  }
  ping(); setInterval(ping, 30000);

  // Keep the screen awake while the kiosk is open (supported in Chrome and Edge)
  async function stayAwake() {
    try { if ("wakeLock" in navigator) await navigator.wakeLock.request("screen"); } catch (_) { /* not allowed */ }
  }
  stayAwake();
  document.addEventListener("visibilitychange", () => { if (!document.hidden) stayAwake(); });

  // Short sound so people know the scan worked without looking at the screen
  let audio = null;
  function beep(freq) {
    try {
      audio = audio || new (window.AudioContext || window.webkitAudioContext)();
      const osc = audio.createOscillator(), gain = audio.createGain();
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.15, audio.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, audio.currentTime + 0.25);
      osc.connect(gain).connect(audio.destination);
      osc.start(); osc.stop(audio.currentTime + 0.25);
    } catch (_) { /* sound is optional */ }
  }

  // A face must be seen in this many consecutive frames before we send it,
  // so people just walking past the edge of the frame are ignored.
  const STABLE_FRAMES = 3;
  const MIN_FACE_RATIO = 0.12; // face width as share of frame width
  let stable = 0;
  let pauseUntil = 0;

  async function loop() {
    try {
      if (document.hidden) {
        // Tab is not visible: skip the heavy face detection until it is shown again
      } else if (Date.now() < pauseUntil) {
        Face.drawBox(overlay, video, null);
      } else {
        const r = await Face.detect(video);
        const bigEnough = r && r.detection.box.width / video.videoWidth >= MIN_FACE_RATIO;
        const good = r && bigEnough && r.detection.score > 0.75;
        stable = good ? stable + 1 : 0;
        Face.drawBox(overlay, video, r, good ? "#4ade80" : "#facc15", r && !bigEnough ? "Come closer" : "");
        guideEl.hidden = Boolean(r);  // the oval is only a hint while nobody is in front of the camera
        if (good && stable >= STABLE_FRAMES) {
          stable = 0;
          await send(Array.from(r.descriptor), savePhotos ? Face.snapshot(video) : null);
        }
      }
    } catch (e) {
      console.error(e);
    }
    setTimeout(loop, 120);
  }

  async function send(descriptor, photo) {
    try {
      const res = await fetch(scanUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key, descriptor, photo }),
      });
      if (res.status === 403) {
        // The kiosk key was changed; this page can no longer mark attendance
        show("unknown", "Kiosk link is no longer valid", "Ask the admin for the new kiosk link", 0);
        pauseUntil = Infinity;
        return;
      }
      const d = await res.json();
      if (!d.ok) throw new Error(d.error || "Server error");

      if (!d.matched) {
        show("unknown", "Face not recognized", "Please ask the admin to register your face", 2500);
        pauseUntil = Date.now() + 2500;
        return;
      }
      if (d.action === "check_in") {
        const late = d.status === "late";
        show(late ? "late" : "in", `Welcome, ${d.name}!`, `Checked in at ${d.time}${late ? " · Late" : ""}`);
        addLog(`✓ ${d.name} in ${d.time}`);
        beep(880); ping();
      } else if (d.action === "check_out") {
        show(d.early ? "late" : "out", `Goodbye, ${d.name}!`, `Checked out at ${d.time}${d.early ? " · Early leave" : ""}`);
        addLog(`↩ ${d.name} out ${d.time}`);
        beep(660);
      } else if (d.action === "locked") {
        show("repeat", `Hi ${d.name}`, `Today's attendance was set by admin (out ${d.time})`, 3000);
      } else {
        show("repeat", `Hi ${d.name}`, `Already marked at ${d.time}`, 2500);
      }
      pauseUntil = Date.now() + 3500; // give the person time to walk away
    } catch (e) {
      show("unknown", "Connection problem", "Retrying…", 2000);
      pauseUntil = Date.now() + 2000;
    }
  }

  loop();
})();
