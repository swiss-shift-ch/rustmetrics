(function () {
  "use strict";

  const C = document.getElementById("game");
  const X = C.getContext("2d");
  const W = C.width, H = C.height;
  const GROUND_H = 90;

  // ---- Skalierung auf Viewport ----
  function fit() {
    const r = W / H;
    const h = window.innerHeight;
    const w = window.innerWidth;
    if (w / h > r) { C.style.height = h + "px"; C.style.width = (h * r) + "px"; }
    else            { C.style.width  = w + "px"; C.style.height = (w / r) + "px"; }
  }
  window.addEventListener("resize", fit);
  fit();

  // ---- Konstanten (1:1 Flappy-Bird-Feel) ----
  const GRAV      = 0.45;
  const FLAP      = -7.6;
  const PIPE_W    = 64;
  const PIPE_GAP  = 145;
  const PIPE_SPD  = 2.3;
  const PIPE_DIST = 220;
  const BIRD_R    = 13;
  const HIT_R     = BIRD_R - 3;

  const DEATH_REASONS = [
    "Wooden Spike Wall",
    "Wall (Wood)",
    "Wooden Barricade",
    "A grumpy spike",
    "Bad architecture",
    "Decay — but mostly the wall",
  ];

  // ---- Spielstatus ----
  let state = "ready";          // "ready" | "play" | "dead"
  let bird, pipes, score, lastScore, frame, deathTimer, killedBy, shake;
  let groundX = 0, treesX = 0, treesX2 = 0;
  let lastFlap = -999;

  let best = 0;
  try { best = parseInt(localStorage.getItem("rustflap_best") || "0", 10) || 0; } catch (e) {}

  // ── Embedded-Mode: postMessage-Bridge zum Parent (rustmetrics.eu PLAY-Tab) ──
  const inIframe = (() => { try { return window.self !== window.top; } catch (e) { return true; } })();
  let serverSessionToken = null;
  let serverStartTime    = 0;
  function postToParent(payload) {
    if (!inIframe) return;
    try { window.parent.postMessage(payload, "*"); } catch (e) {}
  }
  window.addEventListener("message", (e) => {
    if (!e.data || typeof e.data !== "object") return;
    if (e.data.type === "rustflap_set_best" && typeof e.data.best === "number") {
      if (e.data.best > best) best = e.data.best;
    }
    if (e.data.type === "rustflap_set_token" && typeof e.data.token === "string") {
      serverSessionToken = e.data.token;
      serverStartTime    = Date.now();
    }
  });
  // Auf Parent zugehen sobald wir laden — Parent kann dann best + token zurück senden
  if (inIframe) postToParent({ type: "rustflap_ready" });

  // ---- Loadout / Spielfigur ----
  let loadout = "hazmat"; // "hazmat" | "metal"
  try {
    const saved = localStorage.getItem("rustflap_loadout");
    if (saved === "hazmat" || saved === "metal") loadout = saved;
  } catch (e) {}
  const LOADOUT_BTNS = [
    { id: "hazmat", label: "HAZMAT",     x: 0, y: 0, w: 0, h: 0 },
    { id: "metal",  label: "FULL METAL", x: 0, y: 0, w: 0, h: 0 },
  ];
  function setLoadout(id) {
    if (id !== "hazmat" && id !== "metal") return;
    if (loadout === id) return;
    loadout = id;
    try { localStorage.setItem("rustflap_loadout", id); } catch (e) {}
    playSound("flap");
  }

  function reset() {
    bird = { x: W * 0.28, y: H * 0.45, vy: 0 };
    pipes = [];
    score = 0;
    lastScore = 0;
    frame = 0;
    deathTimer = 0;
    shake = 0;
    lastFlap = -999;
  }
  reset();

  function spawnPipe(x) {
    const margin = 70;
    const topMax = H - GROUND_H - PIPE_GAP - margin;
    const top = margin + Math.random() * (topMax - margin);
    pipes.push({ x: x, top: top, passed: false });
  }

  // ---- Eingabe ----
  function flap() {
    // Audio-Kontext bei erster User-Geste initialisieren/fortsetzen (Mobile/Safari).
    const _c = ac();
    if (_c && _c.state === "suspended") { try { _c.resume(); } catch (e) {} }
    if (state === "ready") {
      state = "play";
      bird.y = H * 0.45;   // fairer Start in der Bildschirmmitte
      bird.vy = FLAP;
      lastFlap = frame;
      playSound("flap");
      // Parent informieren: neues Spiel gestartet (Parent kann Token issuen für Anti-Cheat)
      postToParent({ type: "rustflap_start" });
    } else if (state === "play") {
      bird.vy = FLAP;
      lastFlap = frame;
      playSound("flap");
    } else if (state === "dead" && deathTimer > 45) {
      reset();
      state = "ready";
    }
  }

  window.addEventListener("keydown", (e) => {
    if (state === "ready" && (e.code === "Digit1" || e.code === "Numpad1")) {
      e.preventDefault(); setLoadout("hazmat"); return;
    }
    if (state === "ready" && (e.code === "Digit2" || e.code === "Numpad2")) {
      e.preventDefault(); setLoadout("metal"); return;
    }
    if (e.code === "Space" || e.code === "ArrowUp" || e.code === "KeyW") {
      e.preventDefault();
      flap();
    }
  });

  function canvasCoords(clientX, clientY) {
    const rect = C.getBoundingClientRect();
    return {
      x: (clientX - rect.left) * (W / rect.width),
      y: (clientY - rect.top)  * (H / rect.height),
    };
  }
  function handlePointer(cx, cy) {
    // Im Ready-Screen: Loadout-Buttons abfangen, sonst flap.
    if (state === "ready") {
      for (const btn of LOADOUT_BTNS) {
        if (cx >= btn.x && cx <= btn.x + btn.w && cy >= btn.y && cy <= btn.y + btn.h) {
          setLoadout(btn.id);
          return;
        }
      }
    }
    flap();
  }
  C.addEventListener("mousedown", (e) => {
    e.preventDefault();
    const p = canvasCoords(e.clientX, e.clientY);
    handlePointer(p.x, p.y);
  });
  C.addEventListener("touchstart", (e) => {
    e.preventDefault();
    if (!e.touches[0]) { flap(); return; }
    const p = canvasCoords(e.touches[0].clientX, e.touches[0].clientY);
    handlePointer(p.x, p.y);
  }, { passive: false });

  // ---- Procedurale Pine-Layer ----
  const pineLayer1 = [];
  const pineLayer2 = [];
  for (let i = 0; i < 26; i++) pineLayer1.push({ x: i * 22 + Math.random() * 14, h: 36 + Math.random() * 22 });
  for (let i = 0; i < 16; i++) pineLayer2.push({ x: i * 34 + Math.random() * 20, h: 56 + Math.random() * 32 });

  // ---- Embers / Staub ----
  const embers = [];
  for (let i = 0; i < 18; i++) {
    embers.push({
      x: Math.random() * W,
      y: Math.random() * (H - GROUND_H),
      vy: -0.2 - Math.random() * 0.3,
      vx: -0.4 - Math.random() * 0.3,
      r: 0.6 + Math.random() * 1.2,
      a: 0.2 + Math.random() * 0.4,
    });
  }

  // ---- Rendering ----
  function bgGradient() {
    const g = X.createLinearGradient(0, 0, 0, H - GROUND_H);
    g.addColorStop(0.00, "#8a3a18");
    g.addColorStop(0.35, "#a85a2c");
    g.addColorStop(0.65, "#9a6b48");
    g.addColorStop(1.00, "#54452f");
    X.fillStyle = g;
    X.fillRect(0, 0, W, H - GROUND_H);

    // Sonne / Dunst
    const sx = W * 0.78, sy = H * 0.22;
    const sg = X.createRadialGradient(sx, sy, 5, sx, sy, 150);
    sg.addColorStop(0,   "rgba(255,210,140,0.85)");
    sg.addColorStop(0.4, "rgba(220,120,50,0.35)");
    sg.addColorStop(1,   "rgba(160,60,30,0)");
    X.fillStyle = sg;
    X.fillRect(0, 0, W, H - GROUND_H);

    // Smogband
    X.fillStyle = "rgba(50,30,20,0.18)";
    X.fillRect(0, H * 0.36, W, H * 0.12);
  }

  function drawMountains() {
    const baseY = H - GROUND_H - 30;
    // Hintere Berge
    X.fillStyle = "#3a2a22";
    X.beginPath();
    X.moveTo(-20, baseY);
    for (let i = 0; i <= W + 40; i += 18) {
      const s = Math.sin(i * 0.12) * 0.5 + Math.sin(i * 0.05) * 0.5;
      X.lineTo(i, baseY - 55 - s * 38 - ((i * 0.1) % 14));
    }
    X.lineTo(W + 20, baseY);
    X.closePath();
    X.fill();
    // Vordere Berge
    X.fillStyle = "#2a1f17";
    X.beginPath();
    X.moveTo(-20, baseY);
    for (let i = 0; i <= W + 40; i += 14) {
      const s = Math.sin(i * 0.18 + 1) * 0.5 + Math.sin(i * 0.07 + 0.5) * 0.5;
      X.lineTo(i, baseY - 26 - s * 22);
    }
    X.lineTo(W + 20, baseY);
    X.closePath();
    X.fill();
  }

  function drawPine(px, py, h, color) {
    const w = h * 0.6;
    X.fillStyle = color;
    // Stamm
    X.fillRect(px - 2, py - 4, 4, 8);
    // Dreiecks-Etagen
    X.beginPath();
    for (let k = 0; k < 3; k++) {
      const ky = py - 4 - k * (h * 0.32);
      const kw = w * (1 - k * 0.18);
      X.moveTo(px - kw / 2, ky);
      X.lineTo(px + kw / 2, ky);
      X.lineTo(px, ky - h * 0.42);
      X.closePath();
    }
    X.fill();
  }

  function drawPines(offset, layer, color) {
    const baseY = H - GROUND_H - 4;
    const span = W + 80;
    for (const t of layer) {
      let x = ((t.x + offset) % span + span) % span - 30;
      drawPine(x, baseY, t.h, color);
    }
  }

  function drawGround() {
    const gy = H - GROUND_H;
    const g = X.createLinearGradient(0, gy, 0, H);
    g.addColorStop(0,   "#5a3d22");
    g.addColorStop(0.4, "#4a3018");
    g.addColorStop(1,   "#2e1d0e");
    X.fillStyle = g;
    X.fillRect(0, gy, W, GROUND_H);
    // Oberkante
    X.fillStyle = "#3d2a14";
    X.fillRect(0, gy, W, 4);

    // Scrollende Details
    X.save();
    X.translate(-((groundX % 40) + 40) % 40, 0);
    for (let i = -1; i < W / 40 + 2; i++) {
      const x = i * 40;
      // Stein
      X.fillStyle = "#6b5644";
      X.beginPath();
      X.ellipse(x + 12, gy + 10, 5, 3, 0, 0, Math.PI * 2);
      X.fill();
      // Grasbüschel
      X.strokeStyle = "#5a4a2a";
      X.lineWidth = 1.2;
      X.beginPath();
      X.moveTo(x + 22, gy + 2); X.lineTo(x + 20, gy - 3);
      X.moveTo(x + 26, gy + 2); X.lineTo(x + 26, gy - 4);
      X.moveTo(x + 30, gy + 2); X.lineTo(x + 32, gy - 3);
      X.stroke();
      // Kleiner Kies
      X.fillStyle = "#3a2a18";
      X.fillRect(x + 34, gy + 18, 3, 2);
    }
    X.restore();
    // Horizontale Linien für Erdschichten
    X.strokeStyle = "rgba(20,10,4,0.25)";
    X.lineWidth = 1;
    for (let yy = gy + 24; yy < H; yy += 14) {
      X.beginPath(); X.moveTo(0, yy); X.lineTo(W, yy); X.stroke();
    }
  }

  function drawEmbers() {
    for (const e of embers) {
      X.fillStyle = "rgba(255,180,90," + e.a + ")";
      X.beginPath();
      X.arc(e.x, e.y, e.r, 0, Math.PI * 2);
      X.fill();
    }
  }

  function updateEmbers() {
    for (const e of embers) {
      e.x += e.vx;
      e.y += e.vy;
      if (e.y < -4 || e.x < -4) {
        e.x = W + Math.random() * 30;
        e.y = (H - GROUND_H) - Math.random() * 30;
      }
    }
  }

  // ---- Holzwand mit Brettern ----
  function drawWoodPlankColumn(x, y, w, h) {
    if (h <= 0) return;
    X.fillStyle = "#3a2614";
    X.fillRect(x, y, w, h);

    const plankCount = 4;
    const pw = w / plankCount;
    const woodColors = ["#7a5230", "#6b4828", "#8a5d34", "#5e3f22"];

    for (let i = 0; i < plankCount; i++) {
      X.fillStyle = woodColors[i];
      X.fillRect(x + i * pw + 1, y, pw - 2, h);
      // Maserung
      X.strokeStyle = "rgba(35,20,8,0.55)";
      X.lineWidth = 1;
      X.beginPath();
      for (let k = 0; k < 3; k++) {
        const gx = x + i * pw + pw * (0.25 + k * 0.25);
        X.moveTo(gx, y);
        for (let yy = y; yy < y + h; yy += 8) {
          X.lineTo(gx + Math.sin(yy * 0.3 + i) * 0.6, yy);
        }
      }
      X.stroke();
      // Astloch
      if ((i + Math.floor(x / 13)) % 3 === 0 && h > 50) {
        X.fillStyle = "#2a1808";
        X.beginPath();
        X.ellipse(x + i * pw + pw / 2, y + 30 + ((i * 17) % Math.max(1, h - 40)), 2.5, 1.8, 0, 0, Math.PI * 2);
        X.fill();
      }
    }
    // Rahmen
    X.strokeStyle = "#1f1308";
    X.lineWidth = 2;
    X.strokeRect(x + 1, y + 1, w - 2, h - 2);
    // Nägel
    X.fillStyle = "#3a3a3a";
    X.fillRect(x + 3, y + 3, 2, 2);
    X.fillRect(x + w - 5, y + 3, 2, 2);
    X.fillRect(x + 3, y + h - 5, 2, 2);
    X.fillRect(x + w - 5, y + h - 5, 2, 2);
  }

  // ---- Metallspikes ----
  function drawSpikes(x, y, w, dir) {
    // dir = +1: Spikes zeigen nach UNTEN (untere Kante der oberen Wand)
    // dir = -1: Spikes zeigen nach OBEN  (obere Kante der unteren Wand)
    const count = 7;
    const sw = w / count;
    const sl = 12;

    // dunkler Sockel
    X.fillStyle = "#222";
    X.fillRect(x, dir === -1 ? y - 1 : y - 1, w, 3);

    for (let i = 0; i < count; i++) {
      const sx = x + i * sw;
      X.fillStyle = "#4a4a4a";
      X.beginPath();
      if (dir === -1) {
        X.moveTo(sx, y);
        X.lineTo(sx + sw, y);
        X.lineTo(sx + sw / 2, y - sl);
      } else {
        X.moveTo(sx, y);
        X.lineTo(sx + sw, y);
        X.lineTo(sx + sw / 2, y + sl);
      }
      X.closePath();
      X.fill();
      // Highlight
      X.fillStyle = "#9a9a9a";
      X.beginPath();
      if (dir === -1) {
        X.moveTo(sx + sw * 0.35, y);
        X.lineTo(sx + sw / 2, y - sl);
        X.lineTo(sx + sw / 2 - 1, y);
      } else {
        X.moveTo(sx + sw * 0.35, y);
        X.lineTo(sx + sw / 2, y + sl);
        X.lineTo(sx + sw / 2 - 1, y);
      }
      X.closePath();
      X.fill();
    }
  }

  function drawPipe(p) {
    const topH = p.top;
    const botY = p.top + PIPE_GAP;
    const botH = (H - GROUND_H) - botY;

    // Wände
    drawWoodPlankColumn(p.x, 0, PIPE_W, topH);
    drawWoodPlankColumn(p.x, botY, PIPE_W, botH);
    // Kappen (etwas breiter)
    const capH = 14, capExtra = 6;
    drawWoodPlankColumn(p.x - capExtra / 2, topH - capH, PIPE_W + capExtra, capH);
    drawWoodPlankColumn(p.x - capExtra / 2, botY,        PIPE_W + capExtra, capH);
    // Spikes in den Spalt
    drawSpikes(p.x - capExtra / 2, topH, PIPE_W + capExtra, +1);
    drawSpikes(p.x - capExtra / 2, botY, PIPE_W + capExtra, -1);
  }

  // ---- Rust Player Models ----
  // Beide Figuren sind ca. 22 px breit, 36 px hoch (Kopfspitze bei -18, Stiefel bei +18).
  // Rotation moderat (kein Vogel-Salto): -0.3 .. +0.7 rad.

  function armAngle() {
    const wf = frame - lastFlap;
    if (wf < 14) return -1.25 + (wf / 14) * 1.75;
    return Math.sin(frame * 0.18) * 0.25 - 0.15;
  }

  function drawHazmat(b) {
    X.save();
    X.translate(b.x, b.y);
    const rot = Math.max(-0.3, Math.min(0.7, b.vy * 0.055));
    X.rotate(rot);

    // Boden-Schatten
    X.fillStyle = "rgba(0,0,0,0.28)";
    X.beginPath(); X.ellipse(0, 16, 13, 3, 0, 0, Math.PI * 2); X.fill();

    // Hinterer Arm (kommt aus dem Rücken)
    const aa = armAngle();
    X.save();
    X.translate(-6, -3);
    X.rotate(aa - 0.25);
    X.fillStyle = "#b89218";
    X.fillRect(-2.5, 0, 5, 12);
    X.fillStyle = "#2a1f10";
    X.fillRect(-2.5, 10, 5, 4);
    X.restore();

    // Beine (dunkler Gummi-Look)
    X.fillStyle = "#2a2218";
    X.fillRect(-6, 6, 5, 10);
    X.fillRect(1, 6, 5, 10);
    // Stiefel
    X.fillStyle = "#0e0a06";
    X.fillRect(-7, 14, 7, 4);
    X.fillRect(0, 14, 7, 4);

    // Anzug-Körper (Hazmat-Gelb)
    X.fillStyle = "#e8c428";
    X.fillRect(-10, -6, 20, 14);
    // Highlight links
    X.fillStyle = "#f4d850";
    X.fillRect(-10, -6, 5, 14);
    // Schatten rechts
    X.fillStyle = "#a88018";
    X.fillRect(7, -6, 3, 14);
    // Reißverschluss
    X.strokeStyle = "#5a4408";
    X.lineWidth = 1;
    X.beginPath(); X.moveTo(0, -6); X.lineTo(0, 8); X.stroke();
    // Hüftnaht
    X.strokeStyle = "#7a5e10";
    X.beginPath(); X.moveTo(-10, 4); X.lineTo(10, 4); X.stroke();
    // Brusttasche
    X.fillStyle = "#b89218";
    X.fillRect(-8, -3, 5, 4);
    X.strokeStyle = "#5a4408";
    X.strokeRect(-8 + 0.5, -3 + 0.5, 5, 4);
    // Strahlungs-Trefoil-Symbol auf Brust (klein)
    X.fillStyle = "#1a1a08";
    X.beginPath(); X.arc(5, -1, 1.4, 0, Math.PI * 2); X.fill();
    X.fillStyle = "#e8c428";
    X.beginPath(); X.arc(5, -1, 0.5, 0, Math.PI * 2); X.fill();

    // Kapuze hinter dem Kopf
    X.fillStyle = "#c8a018";
    X.beginPath(); X.arc(-1, -10, 10, 0, Math.PI * 2); X.fill();
    X.fillStyle = "#a88018";
    X.beginPath(); X.arc(-4, -8, 5, 0, Math.PI * 2); X.fill();

    // Gasmaske: olivgrüner Korpus
    X.fillStyle = "#4a5028";
    X.beginPath(); X.ellipse(2, -9, 8, 8.5, 0, 0, Math.PI * 2); X.fill();
    // Maske unten dunkler (Schatten)
    X.fillStyle = "#363a1a";
    X.beginPath(); X.ellipse(2, -6, 7.5, 4, 0, 0, Math.PI * 2); X.fill();

    // Zwei runde Linsen (Hazmat-typisch)
    X.fillStyle = "#0a0a0a";
    X.beginPath(); X.arc(-1, -10, 2.4, 0, Math.PI * 2); X.fill();
    X.beginPath(); X.arc(5,  -10, 2.4, 0, Math.PI * 2); X.fill();
    // Linsen-Ring
    X.strokeStyle = "#1a1a1a";
    X.lineWidth = 1.2;
    X.beginPath(); X.arc(-1, -10, 2.7, 0, Math.PI * 2); X.stroke();
    X.beginPath(); X.arc(5,  -10, 2.7, 0, Math.PI * 2); X.stroke();
    // Glanzpunkt in den Linsen
    X.fillStyle = "rgba(200,220,255,0.7)";
    X.beginPath(); X.arc(-1.7, -10.8, 0.7, 0, Math.PI * 2); X.fill();
    X.beginPath(); X.arc(4.3,  -10.8, 0.7, 0, Math.PI * 2); X.fill();

    // Filterpatrone (Zylinder rechts unten an Maske)
    X.fillStyle = "#2a2a1a";
    X.fillRect(2, -5, 8, 4);
    X.fillStyle = "#3a3a26";
    X.fillRect(2, -5, 8, 1);
    X.fillStyle = "#0e0e06";
    X.fillRect(9, -4, 1.5, 2);
    // Gurte/Riemen am Hinterkopf andeutung
    X.strokeStyle = "#3a3a1a";
    X.lineWidth = 1;
    X.beginPath();
    X.moveTo(-7, -10); X.lineTo(-2, -9);
    X.moveTo(-6, -7);  X.lineTo(-2, -7);
    X.stroke();

    // Vorderer Arm (über Körper)
    X.save();
    X.translate(7, -3);
    X.rotate(aa);
    X.fillStyle = "#e8c428";
    X.fillRect(-2.5, 0, 5, 12);
    X.fillStyle = "#c8a018";
    X.fillRect(2, 0, 0.5, 12);
    X.fillStyle = "#2a1f10";
    X.fillRect(-2.5, 10, 5, 4);
    X.restore();

    // Body-Outline
    X.strokeStyle = "#3a2a08";
    X.lineWidth = 1;
    X.strokeRect(-10, -6, 20, 14);
    X.beginPath(); X.ellipse(2, -9, 8, 8.5, 0, 0, Math.PI * 2); X.stroke();

    X.restore();
  }

  function drawMetalPlayer(b) {
    X.save();
    X.translate(b.x, b.y);
    const rot = Math.max(-0.3, Math.min(0.7, b.vy * 0.055));
    X.rotate(rot);

    // Boden-Schatten
    X.fillStyle = "rgba(0,0,0,0.3)";
    X.beginPath(); X.ellipse(0, 16, 13, 3, 0, 0, Math.PI * 2); X.fill();

    // Hinterer Arm
    const aa = armAngle();
    X.save();
    X.translate(-7, -3);
    X.rotate(aa - 0.25);
    X.fillStyle = "#4a3220";
    X.fillRect(-2.5, 0, 5, 12);
    X.fillStyle = "#a87a55";
    X.fillRect(-2.5, 10, 5, 4);
    X.restore();

    // Beine (graue Hose)
    X.fillStyle = "#3a3a36";
    X.fillRect(-6, 6, 5, 10);
    X.fillRect(1, 6, 5, 10);
    // Kniepolster
    X.fillStyle = "#5a5a52";
    X.fillRect(-6, 10, 5, 2);
    X.fillRect(1, 10, 5, 2);
    // Stiefel
    X.fillStyle = "#0e0e08";
    X.fillRect(-7, 14, 7, 4);
    X.fillRect(0, 14, 7, 4);

    // Braunes Hemd (Körper)
    X.fillStyle = "#6b4a2b";
    X.fillRect(-10, -6, 20, 14);
    X.fillStyle = "#8a5d34";
    X.fillRect(-10, -6, 4, 14);
    X.fillStyle = "#4a3220";
    X.fillRect(8, -6, 2, 14);

    // Metal Chestplate (Brustpanzer, grauer Stahl)
    X.fillStyle = "#7a7a78";
    X.fillRect(-9, -5, 18, 12);
    // Highlight
    X.fillStyle = "#a8a8a4";
    X.fillRect(-9, -5, 4, 12);
    // Mittelnaht
    X.strokeStyle = "#2a2a26";
    X.lineWidth = 1;
    X.beginPath(); X.moveTo(0, -5); X.lineTo(0, 7); X.stroke();
    // Rost/Schmutz
    X.fillStyle = "rgba(120,60,20,0.35)";
    X.fillRect(-7, 2, 4, 2);
    X.fillRect(3, -2, 3, 2);
    // Nieten
    X.fillStyle = "#1a1a18";
    [[-7, -3], [6, -3], [-7, 5], [6, 5], [-7, 1], [6, 1]].forEach(([nx, ny]) => {
      X.fillRect(nx, ny, 1.5, 1.5);
    });
    // Schultergurte
    X.fillStyle = "#3a2a14";
    X.fillRect(-5, -6, 2, 4);
    X.fillRect(3, -6, 2, 4);

    // Hinterkopf / Haare
    X.fillStyle = "#a87a55";
    X.beginPath(); X.arc(-1, -10, 8, 0, Math.PI * 2); X.fill();
    X.fillStyle = "#5a3a20";
    X.beginPath(); X.ellipse(-4, -13, 5, 4, 0, 0, Math.PI * 2); X.fill();

    // Metal Facemask (graue Stahlmaske, abgenutzt)
    X.fillStyle = "#6e6e6a";
    X.beginPath();
    X.moveTo(-5, -16);
    X.lineTo(8, -16);
    X.lineTo(9, -10);
    X.lineTo(7, -4);
    X.lineTo(-4, -4);
    X.lineTo(-6, -10);
    X.closePath();
    X.fill();
    // Highlight
    X.fillStyle = "#9a9a96";
    X.beginPath();
    X.moveTo(-5, -16);
    X.lineTo(1, -16);
    X.lineTo(0, -4);
    X.lineTo(-4, -4);
    X.lineTo(-6, -10);
    X.closePath();
    X.fill();
    // Rost-Patches
    X.fillStyle = "rgba(160,80,30,0.55)";
    X.fillRect(3, -14, 3, 2);
    X.fillRect(-2, -7, 2, 2);

    // Augenschlitz (horizontaler dunkler Streifen)
    X.fillStyle = "#0a0a0a";
    X.fillRect(-3, -12, 10, 2.5);
    // Glühen im Augenschlitz
    X.fillStyle = "rgba(220,170,80,0.5)";
    X.fillRect(-2, -11.5, 8, 1);

    // Mund-Gitter (vertikale Stäbe)
    X.fillStyle = "#0a0a0a";
    X.fillRect(-2, -8, 1, 3);
    X.fillRect(0,  -8, 1, 3);
    X.fillRect(2,  -8, 1, 3);
    X.fillRect(4,  -8, 1, 3);

    // Maskennieten
    X.fillStyle = "#1a1a16";
    X.fillRect(-4, -15, 1.5, 1.5);
    X.fillRect(6,  -15, 1.5, 1.5);
    X.fillRect(-5, -6,  1.5, 1.5);
    X.fillRect(6,  -6,  1.5, 1.5);

    // Maskenkontur
    X.strokeStyle = "#2a2a26";
    X.lineWidth = 1;
    X.beginPath();
    X.moveTo(-5, -16);
    X.lineTo(8, -16);
    X.lineTo(9, -10);
    X.lineTo(7, -4);
    X.lineTo(-4, -4);
    X.lineTo(-6, -10);
    X.closePath();
    X.stroke();

    // Vorderer Arm (über Körper)
    X.save();
    X.translate(7, -3);
    X.rotate(aa);
    X.fillStyle = "#6b4a2b";
    X.fillRect(-2.5, 0, 5, 12);
    X.fillStyle = "#4a3220";
    X.fillRect(2, 0, 0.5, 12);
    X.fillStyle = "#a87a55";
    X.fillRect(-2.5, 10, 5, 4);
    X.restore();

    // Chestplate-Outline
    X.strokeStyle = "#1a1a14";
    X.lineWidth = 1;
    X.strokeRect(-9 + 0.5, -5 + 0.5, 18, 12);

    X.restore();
  }

  function drawPlayer(b) {
    if (loadout === "metal") drawMetalPlayer(b);
    else drawHazmat(b);
  }

  // ---- HUD ----
  function drawScore() {
    X.font = 'bold 44px "Trebuchet MS", "Arial Black", sans-serif';
    X.textAlign = "center";
    X.textBaseline = "middle";
    const txt = score.toString();
    const sx = W / 2, sy = 70;
    X.lineWidth = 6;
    X.strokeStyle = "#1a0e06";
    X.strokeText(txt, sx, sy);
    X.fillStyle = "#f0d8a8";
    X.fillText(txt, sx, sy);
    X.lineWidth = 2;
    X.strokeStyle = "#a85a20";
    X.strokeText(txt, sx, sy);
  }

  function drawReady() {
    X.fillStyle = "rgba(20,10,4,0.35)";
    X.fillRect(0, 0, W, H - GROUND_H);

    // ---- Titel-Panel ----
    const bx = W / 2 - 130, by = 36, bw = 260, bh = 96;
    X.fillStyle = "rgba(35,22,12,0.88)";
    X.fillRect(bx, by, bw, bh);
    X.strokeStyle = "#a85a20";
    X.lineWidth = 3;
    X.strokeRect(bx + 1.5, by + 1.5, bw - 3, bh - 3);
    X.fillStyle = "#3a3a3a";
    for (const [cx, cy] of [[bx + 8, by + 8], [bx + bw - 11, by + 8], [bx + 8, by + bh - 11], [bx + bw - 11, by + bh - 11]]) {
      X.fillRect(cx, cy, 3, 3);
    }

    X.textAlign = "center";
    X.font = 'italic bold 36px "Trebuchet MS", "Times New Roman", serif';
    X.lineWidth = 5;
    X.strokeStyle = "#1a0e06";
    X.strokeText("RUST FLAP", W / 2, by + 42);
    X.fillStyle = "#e8b67c";
    X.fillText("RUST FLAP", W / 2, by + 42);

    X.font = '12px "Trebuchet MS", sans-serif';
    X.fillStyle = "#c8a884";
    X.fillText("Überlebe das Ödland", W / 2, by + 66);
    X.fillStyle = "#a89070";
    X.font = '11px "Trebuchet MS", sans-serif';
    X.fillText("SPACE  /  KLICK  /  TAP  zum Flattern", W / 2, by + 84);

    // ---- Loadout-Picker ----
    const labelY = by + bh + 22;
    X.font = 'bold 11px "Trebuchet MS", sans-serif';
    X.fillStyle = "#a89070";
    X.fillText("LOADOUT WÄHLEN", W / 2, labelY);

    const btnY = labelY + 12;
    const btnW = 108, btnH = 88, btnGap = 16;
    const totalW = 2 * btnW + btnGap;
    const startX = W / 2 - totalW / 2;

    for (let i = 0; i < LOADOUT_BTNS.length; i++) {
      const btn = LOADOUT_BTNS[i];
      btn.x = startX + i * (btnW + btnGap);
      btn.y = btnY;
      btn.w = btnW;
      btn.h = btnH;

      const isSel = btn.id === loadout;
      X.fillStyle = isSel ? "rgba(168,90,32,0.55)" : "rgba(25,16,8,0.85)";
      X.fillRect(btn.x, btn.y, btn.w, btn.h);
      X.strokeStyle = isSel ? "#f0c890" : "#5a4628";
      X.lineWidth = isSel ? 2.5 : 1;
      X.strokeRect(btn.x + 0.5, btn.y + 0.5, btn.w - 1, btn.h - 1);
      // Nieten am Panel
      X.fillStyle = isSel ? "#e8b67c" : "#3a2e1c";
      X.fillRect(btn.x + 4, btn.y + 4, 2, 2);
      X.fillRect(btn.x + btn.w - 6, btn.y + 4, 2, 2);
      X.fillRect(btn.x + 4, btn.y + btn.h - 6, 2, 2);
      X.fillRect(btn.x + btn.w - 6, btn.y + btn.h - 6, 2, 2);

      // Mini-Preview (Figur in der Mitte)
      X.save();
      X.translate(btn.x + btn.w / 2, btn.y + btn.h / 2 + 4);
      X.scale(1.05, 1.05);
      const previewBird = { x: 0, y: 0, vy: 0 };
      if (btn.id === "hazmat") drawHazmat(previewBird);
      else                     drawMetalPlayer(previewBird);
      X.restore();

      // Tasten-Hint oben links
      X.font = 'bold 10px "Trebuchet MS", sans-serif';
      X.fillStyle = isSel ? "#f0c890" : "#7a6244";
      X.textAlign = "left";
      X.fillText("[" + (i + 1) + "]", btn.x + 10, btn.y + 14);

      // Label unten
      X.font = 'bold 11px "Trebuchet MS", sans-serif';
      X.fillStyle = isSel ? "#f0c890" : "#a89070";
      X.textAlign = "center";
      X.fillText(btn.label, btn.x + btn.w / 2, btn.y + btn.h - 6);
    }

    // ---- Best-Score Anzeige ----
    if (best > 0) {
      X.font = 'bold 12px "Trebuchet MS", sans-serif';
      X.fillStyle = "#a89070";
      X.textAlign = "center";
      X.fillText("BESTE: " + best, W / 2, btnY + btnH + 22);
    }

    // ---- Blinkender Aufruf am Boden ----
    const blink = Math.floor(frame / 22) % 2 === 0;
    if (blink) {
      X.font = 'bold 15px "Trebuchet MS", sans-serif';
      X.fillStyle = "#e8b67c";
      X.textAlign = "center";
      const pty = H - GROUND_H - 14 + Math.sin(frame * 0.1) * 3;
      X.fillText("▲ TAP / SPACE  —  FLATTERN ▲", W / 2, pty);
    }
  }

  function drawDead() {
    // Roter Flash
    const a = Math.min(0.55, deathTimer * 0.04);
    X.fillStyle = "rgba(120,20,10," + a + ")";
    X.fillRect(0, 0, W, H);
    // Vignette
    const vg = X.createRadialGradient(W / 2, H / 2, 80, W / 2, H / 2, Math.max(W, H));
    vg.addColorStop(0, "rgba(0,0,0,0)");
    vg.addColorStop(1, "rgba(0,0,0,0.7)");
    X.fillStyle = vg;
    X.fillRect(0, 0, W, H);

    if (deathTimer < 12) return;

    const bx = W / 2 - 140, by = H * 0.20, bw = 280, bh = 230;
    X.fillStyle = "rgba(20,8,4,0.93)";
    X.fillRect(bx, by, bw, bh);
    X.strokeStyle = "#8a1a14";
    X.lineWidth = 3;
    X.strokeRect(bx + 1.5, by + 1.5, bw - 3, bh - 3);
    X.fillStyle = "#3a3a3a";
    for (const [cx, cy] of [[bx + 8, by + 8], [bx + bw - 11, by + 8], [bx + 8, by + bh - 11], [bx + bw - 11, by + bh - 11]]) {
      X.fillRect(cx, cy, 3, 3);
    }

    X.textAlign = "center";
    // "YOU DIED" — Dark-Souls/Rust Look
    X.font = 'bold italic 46px "Trebuchet MS", "Times New Roman", serif';
    X.lineWidth = 5;
    X.strokeStyle = "#1a0000";
    X.strokeText("YOU DIED", W / 2, by + 58);
    X.fillStyle = "#c8201a";
    X.fillText("YOU DIED", W / 2, by + 58);

    X.font = '13px "Trebuchet MS", sans-serif';
    X.fillStyle = "#a87060";
    X.fillText("Killed by:", W / 2, by + 92);
    X.font = 'bold 15px "Trebuchet MS", sans-serif';
    X.fillStyle = "#e8b67c";
    X.fillText(killedBy, W / 2, by + 114);

    X.font = '12px "Trebuchet MS", sans-serif';
    X.fillStyle = "#a89070";
    X.fillText("SCORE", W / 2 - 60, by + 152);
    X.fillText("BEST",  W / 2 + 60, by + 152);
    X.font = 'bold 28px "Trebuchet MS", sans-serif';
    X.fillStyle = "#e8e0c8";
    X.fillText(score.toString(), W / 2 - 60, by + 184);
    X.fillStyle = (score >= best && score > 0) ? "#e8b67c" : "#a89070";
    X.fillText(best.toString(),  W / 2 + 60, by + 184);
    if (score >= best && score > 0) {
      X.font = 'bold 11px "Trebuchet MS", sans-serif';
      X.fillStyle = "#e8b67c";
      X.fillText("★ NEW BEST ★", W / 2 + 60, by + 202);
    }

    if (deathTimer > 45) {
      const blink = Math.floor(frame / 20) % 2 === 0;
      if (blink) {
        X.font = 'bold 13px "Trebuchet MS", sans-serif';
        X.fillStyle = "#c8a884";
        X.fillText("TAP — RESPAWN", W / 2, by + bh - 14);
      }
    }
  }

  // ---- Kollision (Kreis vs. Rechteck) ----
  function collideRect(cx, cy, cr, rx, ry, rw, rh) {
    const closeX = Math.max(rx, Math.min(cx, rx + rw));
    const closeY = Math.max(ry, Math.min(cy, ry + rh));
    const dx = cx - closeX, dy = cy - closeY;
    return (dx * dx + dy * dy) < cr * cr;
  }

  function die(reason) {
    if (state !== "play") return;
    state = "dead";
    killedBy = reason;
    deathTimer = 0;
    shake = 14;
    const newBest = (score > best);
    if (newBest) {
      best = score;
      try { localStorage.setItem("rustflap_best", String(best)); } catch (e) {}
    }
    playSound("hit");
    setTimeout(() => playSound("die"), 220);
    // Parent über Tod informieren — score + reason + game-duration + token
    postToParent({
      type: "rustflap_die",
      score: score,
      reason: reason,
      duration_ms: serverStartTime ? (Date.now() - serverStartTime) : null,
      token: serverSessionToken,
      new_best: newBest,
    });
  }

  // ---- Update ----
  function update() {
    frame++;
    updateEmbers();

    if (state === "ready") {
      // Im Ready-Screen tiefer positionieren, damit Loadout-Panel oben Platz hat.
      bird.y = H * 0.68 + Math.sin(frame * 0.08) * 6;
      bird.vy = 0;
      groundX += PIPE_SPD;
      treesX  -= 0.5;
      treesX2 -= 1.0;
      return;
    }

    if (state === "play") {
      bird.vy += GRAV;
      bird.y  += bird.vy;

      if (bird.y < BIRD_R) { bird.y = BIRD_R; bird.vy = 0; }

      if (bird.y > H - GROUND_H - BIRD_R) {
        bird.y = H - GROUND_H - BIRD_R;
        die("Fall Damage");
        return;
      }

      for (const p of pipes) p.x -= PIPE_SPD;
      pipes = pipes.filter((p) => p.x + PIPE_W + 12 > 0);

      const last = pipes[pipes.length - 1];
      if (!last) spawnPipe(W + 60);
      else if (last.x < W - PIPE_DIST) spawnPipe(last.x + PIPE_DIST);

      for (const p of pipes) {
        if (!p.passed && p.x + PIPE_W < bird.x - HIT_R) {
          p.passed = true;
          score++;
        }
        // obere Wand (inkl. Spike-Zone)
        if (collideRect(bird.x, bird.y, HIT_R, p.x, 0, PIPE_W, p.top + 6) ||
            collideRect(bird.x, bird.y, HIT_R, p.x, p.top + PIPE_GAP - 6, PIPE_W, (H - GROUND_H) - (p.top + PIPE_GAP) + 6)) {
          die(DEATH_REASONS[Math.floor(Math.random() * DEATH_REASONS.length)]);
          return;
        }
      }

      groundX += PIPE_SPD;
      treesX  -= 0.5;
      treesX2 -= 1.0;
    } else if (state === "dead") {
      deathTimer++;
      if (shake > 0) shake *= 0.85;
      if (bird.y < H - GROUND_H - BIRD_R) {
        bird.vy += GRAV * 1.3;
        bird.y  += bird.vy;
        if (bird.y > H - GROUND_H - BIRD_R) {
          bird.y = H - GROUND_H - BIRD_R;
          bird.vy = 0;
        }
      }
    }
  }

  // ---- Render ----
  function render() {
    X.save();
    if (shake > 0.5) {
      X.translate((Math.random() - 0.5) * shake, (Math.random() - 0.5) * shake);
    }
    X.clearRect(0, 0, W, H);
    bgGradient();
    drawMountains();
    drawPines(treesX2, pineLayer2, "#15200f");
    drawPines(treesX,  pineLayer1, "#1f2818");
    drawEmbers();
    for (const p of pipes) drawPipe(p);
    drawGround();
    drawPlayer(bird);
    if (state === "play")  drawScore();
    if (state === "ready") drawReady();
    if (state === "dead")  drawDead();
    X.restore();
  }

  // ---- Sound (Web Audio) ----
  let AC = null;
  function ac() {
    if (!AC) {
      try { AC = new (window.AudioContext || window.webkitAudioContext)(); } catch (e) { AC = null; }
    }
    return AC;
  }
  function playSound(type) {
    const c = ac();
    if (!c) return;
    try {
      const o = c.createOscillator();
      const g = c.createGain();
      o.connect(g); g.connect(c.destination);
      const t = c.currentTime;
      if (type === "flap") {
        o.type = "square";
        o.frequency.setValueAtTime(700, t);
        o.frequency.exponentialRampToValueAtTime(300, t + 0.08);
        g.gain.setValueAtTime(0.06, t);
        g.gain.exponentialRampToValueAtTime(0.001, t + 0.1);
        o.start(t); o.stop(t + 0.12);
      } else if (type === "score") {
        o.type = "triangle";
        o.frequency.setValueAtTime(880, t);
        o.frequency.setValueAtTime(1175, t + 0.07);
        g.gain.setValueAtTime(0.07, t);
        g.gain.exponentialRampToValueAtTime(0.001, t + 0.18);
        o.start(t); o.stop(t + 0.2);
      } else if (type === "hit") {
        o.type = "sawtooth";
        o.frequency.setValueAtTime(140, t);
        o.frequency.exponentialRampToValueAtTime(40, t + 0.15);
        g.gain.setValueAtTime(0.12, t);
        g.gain.exponentialRampToValueAtTime(0.001, t + 0.2);
        o.start(t); o.stop(t + 0.22);
      } else if (type === "die") {
        o.type = "sawtooth";
        o.frequency.setValueAtTime(220, t);
        o.frequency.exponentialRampToValueAtTime(60, t + 0.4);
        g.gain.setValueAtTime(0.1, t);
        g.gain.exponentialRampToValueAtTime(0.001, t + 0.5);
        o.start(t); o.stop(t + 0.55);
      }
    } catch (e) {}
  }

  // ---- Hauptschleife ----
  function loop() {
    update();
    if (score !== lastScore) {
      playSound("score");
      lastScore = score;
    }
    render();
    requestAnimationFrame(loop);
  }
  requestAnimationFrame(loop);
})();