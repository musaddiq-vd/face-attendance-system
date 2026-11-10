(async () => {
  const { postUrl, backUrl, needed } = window.REGISTER;
  const video = document.getElementById("video");
  const overlay = document.getElementById("overlay");
  const statusEl = document.getElementById("status");
  const samplesEl = document.getElementById("samples");
  const poseEl = document.getElementById("pose");
  const stepLabel = document.getElementById("stepLabel");
  const captureBtn = document.getElementById("captureBtn");
  const saveBtn = document.getElementById("saveBtn");
  const resetBtn = document.getElementById("resetBtn");

  // One instruction per capture, so the samples cover slightly different angles
  const POSES = [
    "Look straight at the camera",
    "Turn your head slightly to the left",
    "Turn your head slightly to the right",
    "Lift your chin a little",
    "Lower your chin a little",
  ];

  let samples = [];
  let ready = false;

  const setStatus = (msg, color = "") => { statusEl.textContent = msg; statusEl.style.color = color; };

  function render() {
    samplesEl.innerHTML = "";
    for (let i = 0; i < needed; i++) {
      const d = document.createElement("div");
      d.className = "dot" + (i < samples.length ? " done" : i === samples.length ? " next" : "");
      d.textContent = i < samples.length ? "✓" : i + 1;
      samplesEl.appendChild(d);
    }
    const done = samples.length >= needed;
    stepLabel.textContent = done ? "All captures done" : `Capture ${samples.length + 1} of ${needed}`;
    poseEl.textContent = done ? "Press “Save face” to finish" : POSES[samples.length % POSES.length];
    saveBtn.disabled = !done;
    captureBtn.disabled = !ready || done;
  }

  try {
    await Face.loadModels();
    poseEl.textContent = "Starting camera…";
    await Face.startCamera(video);
  } catch (e) {
    poseEl.textContent = "Camera could not start";
    setStatus(e.name === "NotAllowedError"
      ? "Camera permission was blocked. Allow the camera in the browser's address bar and reload."
      : (e.message || "Could not start the camera or load the face models."), "var(--red)");
    return;
  }
  ready = true;
  render();

  // Live preview loop: green box = good enough to capture
  (async function loop() {
    try {
      const r = document.hidden ? null : await Face.detect(video);
      Face.drawBox(overlay, video, r, r && r.detection.score > 0.7 ? "#4ade80" : "#facc15");
    } catch (_) { /* ignore single-frame errors */ }
    setTimeout(loop, 150);
  })();

  async function capture() {
    if (captureBtn.disabled) return;
    captureBtn.disabled = true;
    const r = await Face.detect(video);
    if (!r) {
      setStatus("No face found. Move closer and face the camera.", "var(--red)");
    } else if (r.detection.score < 0.7) {
      setStatus("Face not clear. Improve the light and try again.", "var(--amber)");
    } else {
      samples.push(Array.from(r.descriptor));
      setStatus("Captured ✓", "var(--green)");
    }
    render();
  }
  captureBtn.addEventListener("click", capture);
  // Space bar = capture, handy when the admin is standing next to the employee
  document.addEventListener("keydown", e => {
    if (e.code === "Space" && !["INPUT", "TEXTAREA", "BUTTON", "A"].includes(document.activeElement.tagName)) {
      e.preventDefault();
      capture();
    }
  });

  resetBtn.addEventListener("click", () => { samples = []; render(); setStatus("Cleared. Start again."); });

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
        poseEl.textContent = "Face registered";
        setStatus("Saved. Going back…", "var(--green)");
        setTimeout(() => (location.href = backUrl), 1200);
      } else {
        setStatus(data.error || "Could not save.", "var(--red)");
        saveBtn.disabled = false;
      }
    } catch (e) {
      setStatus("Network error while saving. Check the connection and try again.", "var(--red)");
      saveBtn.disabled = false;
    }
  });
})();
