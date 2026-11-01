// Shared face helpers (uses face-api.js loaded before this file).
const Face = (() => {
  const MODEL_URL = "/static/models";
  // Smaller input = faster; 416 is a good balance for a webcam.
  const detectorOptions = new faceapi.TinyFaceDetectorOptions({ inputSize: 416, scoreThreshold: 0.5 });

  async function loadModels() {
    await Promise.all([
      faceapi.nets.tinyFaceDetector.loadFromUri(MODEL_URL),
      faceapi.nets.faceLandmark68Net.loadFromUri(MODEL_URL),
      faceapi.nets.faceRecognitionNet.loadFromUri(MODEL_URL),
    ]);
  }

  async function startCamera(video) {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      throw new Error("Camera not available. Open this page on http://localhost or https.");
    }
    const stream = await navigator.mediaDevices.getUserMedia({
      video: { width: { ideal: 1280 }, height: { ideal: 720 }, facingMode: "user" }, audio: false,
    });
    video.srcObject = stream;
    await new Promise(res => (video.readyState >= 2 ? res() : (video.onloadeddata = res)));
    return stream;
  }

  // Returns the single largest face with its 128-number descriptor, or null.
  async function detect(video) {
    const results = await faceapi
      .detectAllFaces(video, detectorOptions)
      .withFaceLandmarks()
      .withFaceDescriptors();
    if (!results.length) return null;
    results.sort((a, b) => b.detection.box.area - a.detection.box.area);
    return results[0];
  }

  // Small JPEG of the current frame (for check-in photos).
  function snapshot(video, width = 320) {
    const c = document.createElement("canvas");
    c.width = width;
    c.height = Math.round(video.videoHeight * (width / video.videoWidth));
    c.getContext("2d").drawImage(video, 0, 0, c.width, c.height);
    return c.toDataURL("image/jpeg", 0.7);
  }

  function drawBox(canvas, video, result, color = "#4ade80", label = "") {
    const w = video.videoWidth, h = video.videoHeight;
    if (canvas.width !== w) canvas.width = w;
    if (canvas.height !== h) canvas.height = h;
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, w, h);
    if (!result) return;
    const { x, y, width, height } = result.detection.box;
    ctx.lineWidth = Math.max(3, w / 300);
    ctx.strokeStyle = color;
    ctx.strokeRect(x, y, width, height);
    if (label) {
      // Canvas is mirrored with CSS, so flip text back to be readable.
      ctx.save();
      ctx.scale(-1, 1);
      ctx.font = `bold ${Math.round(w / 40)}px Segoe UI, sans-serif`;
      ctx.fillStyle = color;
      ctx.fillText(label, -(x + width), y - 10);
      ctx.restore();
    }
  }

  return { loadModels, startCamera, detect, snapshot, drawBox };
})();
