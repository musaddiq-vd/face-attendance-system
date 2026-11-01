(async () => {
  const { postUrl, backUrl, needed } = window.REGISTER;
  const video = document.getElementById("video");
  const overlay = document.getElementById("overlay");
  const statusEl = document.getElementById("status");
  const samplesEl = document.getElementById("samples");
  const captureBtn = document.getElementById("captureBtn");
  const saveBtn = document.getElementById("saveBtn");
  const resetBtn = document.getElementById("resetBtn");

  let samples = [];
  let latest = null;

  const setStatus = (msg, color = "") => { statusEl.textContent = msg; statusEl.style.color = color; };

  function renderSamples() {
    samplesEl.innerHTML = "";
    for (let i = 0; i < needed; i++) {
      const d = document.createElement("div");
      d.className = "dot" + (i < samples.length ? " done" : "");
      d.textContent = i < samples.length ? "✓" : i + 1;
      samplesEl.appendChild(d);
    }
    saveBtn.disabled = samples.length < needed;
    captureBtn.disabled = samples.length >= needed;
  }

  try {
    await Face.loadModels();
    setStatus("Starting camera…");
    await Face.startCamera(video);
  } catch (e) {
    setStatus(e.message || "Could not start camera / models.", "var(--red)");
    return;
  }
  renderSamples();
  setStatus(`Look at the camera and press "Capture sample" (${needed} needed).`);

  // Live preview loop
  (async function loop() {
    try {
      latest = await Face.detect(video);
      const good = latest && latest.detection.score > 0.7;
      Face.drawBox(overlay, video, latest, good ? "#4ade80" : "#facc15");
    } catch (_) { /* ignore single-frame errors */ }
    setTimeout(loop, 150);
  })();

  captureBtn.addEventListener("click", async () => {
    captureBtn.disabled = true;
    const r = await Face.detect(video);
    if (!r) {
      setStatus("No face found. Move closer and face the camera.", "var(--red)");
    } else if (r.detection.score < 0.7) {
      setStatus("Face not clear. Improve light and look straight.", "var(--amber)");
    } else {
      samples.push(Array.from(r.descriptor));
      setStatus(samples.length < needed
        ? `Sample ${samples.length} captured. Turn your head slightly and capture again.`
        : "All samples captured. Press \"Save face\".", "var(--green)");
    }
    renderSamples();
  });

  resetBtn.addEventListener("click", () => { samples = []; renderSamples(); setStatus("Cleared. Capture again."); });

  saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    setStatus("Saving…");
    try {
      const res = await fetch(postUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]').content,
        },
        body: JSON.stringify({ descriptors: samples }),
      });
      const data = await res.json();
      if (data.ok) {
        setStatus("Face registered successfully! Redirecting…", "var(--green)");
        setTimeout(() => (location.href = backUrl), 1200);
      } else {
        setStatus(data.error || "Could not save.", "var(--red)");
        saveBtn.disabled = false;
      }
    } catch (e) {
      setStatus("Network error while saving.", "var(--red)");
      saveBtn.disabled = false;
    }
  });
})();
