/* CodeWars replay viewer: renders the decoded CodeCraft 2020 replay (see codewars/replay.py).
   Data: {map_size, tick_count, properties, players:[[id,index,score,resource]], entities:[[id,player,type,x,y,hp,active]],
          actions:{id:action}, ticks:[{t, p:[players], a:[added], r:[removed ids], u:[updated], act:{id:action}, actr:[ids], ev:[[tag,a,b]]}]} */
(function () {
  const TYPES = ["WALL", "HOUSE", "BUILDER_BASE", "BUILDER_UNIT", "MELEE_BASE", "MELEE_UNIT", "RANGED_BASE", "RANGED_UNIT", "RESOURCE", "TURRET"];
  const PLAYER_COLORS = ["#2f7df6", "#e5484d", "#30a46c", "#f5a524"];
  const KEYFRAME_EVERY = 25;

  class Viewer {
    constructor(root) {
      this.root = root;
      this.url = root.dataset.replay;
      this.labels = JSON.parse(root.dataset.players || "[]");
      this.tick = 0;
      this.playing = false;
      this.autoplay = !!root.dataset.autoplay;
      this.speed = +(root.dataset.speed || 10); // ticks per second
      this.selected = null;
      this.build();
      this.load();
    }

    build() {
      this.root.innerHTML = `
        <div class="cw-toolbar" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:8px">
          <button class="cw-play small">▶ Play</button>
          <input type="range" class="cw-slider" min="0" max="0" value="0" style="flex:1;min-width:200px">
          <span class="cw-tick" style="font-variant-numeric:tabular-nums;min-width:110px">tick 0 / 0</span>
          <select class="cw-speed"><option value="2">2 t/s</option><option value="5">5 t/s</option><option value="10" selected>10 t/s</option><option value="25">25 t/s</option><option value="60">60 t/s</option></select>
          <label style="display:inline;font-weight:normal"><input type="checkbox" class="cw-attacks" checked> attacks</label>
          <label style="display:inline;font-weight:normal"><input type="checkbox" class="cw-moves"> move targets</label>
        </div>
        <div style="display:flex;gap:16px;flex-wrap:wrap;align-items:flex-start">
          <canvas class="cw-canvas" style="border:1px solid var(--line);border-radius:4px;max-width:100%;cursor:crosshair"></canvas>
          <div class="cw-side" style="flex:1;min-width:240px;font-size:14px;overflow-x:auto">
            <div class="cw-players"></div>
            <div class="cw-info" style="margin-top:12px"></div>
            <div class="muted cw-status" style="margin-top:12px">loading replay…</div>
          </div>
        </div>`;
      this.$ = (sel) => this.root.querySelector(sel);
      this.canvas = this.$(".cw-canvas");
      this.ctx = this.canvas.getContext("2d");
      this.$(".cw-play").onclick = () => this.togglePlay();
      this.$(".cw-slider").oninput = (e) => { this.pause(); this.goTo(+e.target.value); };
      this.$(".cw-speed").onchange = (e) => { this.speed = +e.target.value; };
      this.$(".cw-attacks").onchange = () => this.draw();
      this.$(".cw-moves").onchange = () => this.draw();
      this.canvas.onclick = (e) => this.onClick(e);
      if (this.speed !== 10) { const opt = [...this.$(".cw-speed").options].find((o) => +o.value === this.speed); if (opt) opt.selected = true; }
      document.addEventListener("keydown", (e) => {
        if (this.autoplay || e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;
        if (e.key === " ") { e.preventDefault(); this.togglePlay(); }
        if (e.key === "ArrowRight") { this.pause(); this.goTo(this.tick + (e.shiftKey ? 10 : 1)); }
        if (e.key === "ArrowLeft") { this.pause(); this.goTo(this.tick - (e.shiftKey ? 10 : 1)); }
      });
    }

    async load() {
      try {
        const resp = await fetch(this.url);
        if (!resp.ok) throw new Error(resp.status + " " + resp.statusText);
        this.data = await resp.json();
      } catch (err) {
        this.$(".cw-status").textContent = "cannot load the replay: " + err;
        return;
      }
      const d = this.data;
      this.size = d.map_size;
      this.props = d.properties;
      this.byIndex = {};
      d.players.forEach((p) => { this.byIndex[p[0]] = p[1]; });
      this.keyframes = [];
      this.buildKeyframes();
      this.$(".cw-slider").max = d.tick_count;
      this.$(".cw-status").textContent = `${d.tick_count} ticks · ${d.fog_of_war ? "fog of war (god view shown)" : "no fog"} · click an entity`;
      this.resize();
      window.addEventListener("resize", () => this.resize());
      this.goTo(0);
      if (this.autoplay) this.play();
    }

    // State ---------------------------------------------------------------
    initialState() {
      const s = { players: new Map(), entities: new Map(), actions: new Map(), events: [] };
      this.data.players.forEach((p) => s.players.set(p[0], p.slice()));
      this.data.entities.forEach((e) => s.entities.set(e[0], e.slice()));
      Object.entries(this.data.actions || {}).forEach(([id, a]) => s.actions.set(+id, a));
      return s;
    }
    cloneState(s) {
      return { players: new Map([...s.players].map(([k, v]) => [k, v.slice()])),
               entities: new Map([...s.entities].map(([k, v]) => [k, v.slice()])),
               actions: new Map(s.actions), events: s.events };
    }
    applyTick(s, d) {
      (d.p || []).forEach((p) => s.players.set(p[0], p.slice()));
      (d.r || []).forEach((id) => s.entities.delete(id));
      (d.a || []).forEach((e) => s.entities.set(e[0], e.slice()));
      (d.u || []).forEach((e) => s.entities.set(e[0], e.slice()));
      (d.actr || []).forEach((id) => s.actions.delete(id));
      Object.entries(d.act || {}).forEach(([id, a]) => s.actions.set(+id, a));
      s.events = d.ev || [];
    }
    buildKeyframes() {
      let s = this.initialState();
      this.keyframes[0] = this.cloneState(s);
      this.data.ticks.forEach((d, i) => {
        this.applyTick(s, d);
        if ((i + 1) % KEYFRAME_EVERY === 0) this.keyframes[i + 1] = this.cloneState(s);
      });
    }
    stateAt(t) {
      if (this.cache && this.cache.t === t) return this.cache.s;
      let s, from;
      if (this.cache && this.cache.t < t && t - this.cache.t < KEYFRAME_EVERY) {
        s = this.cache.s; from = this.cache.t;
      } else {
        from = t - (t % KEYFRAME_EVERY);
        while (!this.keyframes[from]) from -= KEYFRAME_EVERY;
        s = this.cloneState(this.keyframes[from]);
      }
      for (let i = from; i < t; i++) this.applyTick(s, this.data.ticks[i]);
      this.cache = { t, s };
      return s;
    }

    // Playback --------------------------------------------------------------
    togglePlay() { this.playing ? this.pause() : this.play(); }
    play() {
      if (!this.data) return;
      if (this.tick >= this.data.tick_count) this.goTo(0);
      this.playing = true;
      this.$(".cw-play").textContent = "❚❚ Pause";
      let last = performance.now(), acc = 0;
      const step = (now) => {
        if (!this.playing) return;
        acc += (now - last) / 1000 * this.speed; last = now;
        const n = Math.floor(acc);
        if (n > 0) { acc -= n; this.goTo(this.tick + n); }
        if (this.tick >= this.data.tick_count) {
          if (this.autoplay) { this.goTo(0); } else { this.pause(); return; }
        }
        requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    }
    pause() { this.playing = false; this.$(".cw-play").textContent = "▶ Play"; }
    goTo(t) {
      t = Math.max(0, Math.min(this.data.tick_count, t));
      this.tick = t;
      this.$(".cw-slider").value = t;
      this.$(".cw-tick").textContent = `tick ${t} / ${this.data.tick_count}`;
      this.draw();
    }

    // Drawing ----------------------------------------------------------------
    resize() {
      const w = Math.min(640, this.root.clientWidth - 280 > 320 ? this.root.clientWidth - 280 : this.root.clientWidth - 8);
      this.cell = Math.max(4, Math.floor(w / this.size));
      this.canvas.width = this.canvas.height = this.cell * this.size;
      this.draw();
    }
    px(x) { return x * this.cell; }
    py(y) { return (this.size - 1 - y) * this.cell; }   // y grows upwards, as in the official viewer
    color(e) {
      const type = TYPES[e[2]];
      if (type === "RESOURCE") return "#8a9bb0";
      if (e[1] == null) return "#555";
      return PLAYER_COLORS[this.byIndex[e[1]] % PLAYER_COLORS.length] || "#999";
    }
    draw() {
      if (!this.data) return;
      const s = this.stateAt(this.tick), ctx = this.ctx, c = this.cell;
      ctx.fillStyle = "#101418";
      ctx.fillRect(0, 0, this.canvas.width, this.canvas.height);
      ctx.strokeStyle = "rgba(255,255,255,0.05)";
      for (let i = 0; i <= this.size; i += 10) {
        ctx.beginPath(); ctx.moveTo(i * c, 0); ctx.lineTo(i * c, this.size * c); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(0, i * c); ctx.lineTo(this.size * c, i * c); ctx.stroke();
      }
      const showMoves = this.$(".cw-moves").checked;
      for (const e of s.entities.values()) {
        const type = TYPES[e[2]], size = (this.props[type] || {}).size || 1, hp = e[5], maxHp = (this.props[type] || {}).max_health || hp;
        const x = this.px(e[3]), y = this.py(e[4] + size - 1), w = size * c;
        ctx.fillStyle = this.color(e);
        ctx.globalAlpha = e[6] ? 1 : 0.45;
        if (type === "RESOURCE") { ctx.fillRect(x + 1, y + 1, w - 2, w - 2); }
        else if (type.endsWith("_UNIT")) {
          ctx.beginPath(); ctx.arc(x + w / 2, y + w / 2, w * 0.42, 0, Math.PI * 2); ctx.fill();
          if (type === "RANGED_UNIT") { ctx.fillStyle = "#fff"; ctx.fillRect(x + w / 2 - 1, y + w / 2 - 1, 2, 2); }
        } else {
          ctx.fillRect(x + 1, y + 1, w - 2, w - 2);
          ctx.fillStyle = "rgba(0,0,0,0.35)";
          ctx.font = `${Math.max(8, c * 1.2)}px sans-serif`; ctx.textAlign = "center"; ctx.textBaseline = "middle";
          ctx.fillText(type === "TURRET" ? "T" : type === "HOUSE" ? "H" : type[0], x + w / 2, y + w / 2);
        }
        ctx.globalAlpha = 1;
        if (hp < maxHp && size > 1) {
          ctx.fillStyle = "rgba(0,0,0,0.5)"; ctx.fillRect(x + 2, y + w - 4, w - 4, 3);
          ctx.fillStyle = "#4ade80"; ctx.fillRect(x + 2, y + w - 4, (w - 4) * hp / maxHp, 3);
        }
        if (showMoves) {
          const a = s.actions.get(e[0]);
          if (a && a.move) {
            ctx.strokeStyle = "rgba(255,255,255,0.35)"; ctx.beginPath();
            ctx.moveTo(x + w / 2, y + w / 2); ctx.lineTo(this.px(a.move.target[0]) + c / 2, this.py(a.move.target[1]) + c / 2); ctx.stroke();
          }
        }
      }
      if (this.$(".cw-attacks").checked) {
        ctx.lineWidth = Math.max(1, c / 5);
        for (const ev of s.events) {
          if (ev[0] !== 0) continue;
          const a = s.entities.get(ev[1]), b = s.entities.get(ev[2]);
          if (!a || !b) continue;
          const sa = (this.props[TYPES[a[2]]] || {}).size || 1, sb = (this.props[TYPES[b[2]]] || {}).size || 1;
          ctx.strokeStyle = TYPES[b[2]] === "RESOURCE" ? "rgba(255,255,255,0.25)" : "rgba(255,80,80,0.8)";
          ctx.beginPath();
          ctx.moveTo(this.px(a[3]) + sa * c / 2, this.py(a[4] + sa - 1) + sa * c / 2);
          ctx.lineTo(this.px(b[3]) + sb * c / 2, this.py(b[4] + sb - 1) + sb * c / 2);
          ctx.stroke();
        }
        ctx.lineWidth = 1;
      }
      if (this.selected != null) {
        const e = s.entities.get(this.selected);
        if (e) {
          const size = (this.props[TYPES[e[2]]] || {}).size || 1;
          ctx.strokeStyle = "#fff"; ctx.lineWidth = 2;
          ctx.strokeRect(this.px(e[3]) - 1, this.py(e[4] + size - 1) - 1, size * c + 2, size * c + 2);
          ctx.lineWidth = 1;
        }
      }
      this.drawSide(s);
    }
    drawSide(s) {
      const rows = [...s.players.values()].sort((a, b) => a[1] - b[1]);
      const counts = {};
      for (const e of s.entities.values()) if (e[1] != null) { counts[e[1]] = counts[e[1]] || { units: 0, buildings: 0 }; TYPES[e[2]].endsWith("_UNIT") ? counts[e[1]].units++ : counts[e[1]].buildings++; }
      let html = `<table style="font-size:13px"><tr><th></th><th>Player</th><th class="num">Score</th><th class="num">Res</th><th class="num">Units</th><th class="num">Bldg</th></tr>`;
      for (const p of rows) {
        const col = PLAYER_COLORS[p[1] % PLAYER_COLORS.length], cnt = counts[p[0]] || { units: 0, buildings: 0 };
        html += `<tr><td><span style="display:inline-block;width:12px;height:12px;border-radius:3px;background:${col}"></span></td><td>${esc(this.labels[p[1]] || ("player " + (p[1] + 1)))}</td><td class="num">${p[2]}</td><td class="num">${p[3]}</td><td class="num">${cnt.units}</td><td class="num">${cnt.buildings}</td></tr>`;
      }
      this.$(".cw-players").innerHTML = html + "</table>";
      const info = this.$(".cw-info");
      if (this.selected == null) { info.innerHTML = ""; return; }
      const e = s.entities.get(this.selected);
      if (!e) { info.innerHTML = `<span class="muted">entity #${this.selected} is gone</span>`; return; }
      const type = TYPES[e[2]], owner = e[1] == null ? "-" : esc(this.labels[this.byIndex[e[1]]] || ("player " + (this.byIndex[e[1]] + 1)));
      const a = s.actions.get(e[0]);
      let act = "";
      if (a) {
        if (a.move) act += `<div>move → (${a.move.target[0]}, ${a.move.target[1]})${a.move.find_closest_position ? " closest" : ""}${a.move.break_through ? " break through" : ""}</div>`;
        if (a.build) act += `<div>build ${TYPES[a.build.entity_type]} at (${a.build.position[0]}, ${a.build.position[1]})</div>`;
        if (a.attack) act += `<div>attack ${a.attack.target != null ? "#" + a.attack.target : ""}${a.attack.auto_attack ? ` auto(range ${a.attack.auto_attack.pathfind_range}, ${a.attack.auto_attack.valid_targets.map((t) => TYPES[t]).join("/") || "any"})` : ""}</div>`;
        if (a.repair) act += `<div>repair #${a.repair.target}</div>`;
      }
      info.innerHTML = `<b>#${e[0]} ${type}</b><div>owner ${owner} · at (${e[3]}, ${e[4]}) · health ${e[5]}/${(this.props[type] || {}).max_health || "?"}${e[6] ? "" : " · inactive"}</div>${act || "<div class='muted'>no action stored</div>"}`;
    }
    onClick(ev) {
      const rect = this.canvas.getBoundingClientRect();
      const scale = this.canvas.width / rect.width;
      const cx = Math.floor((ev.clientX - rect.left) * scale / this.cell), cy = this.size - 1 - Math.floor((ev.clientY - rect.top) * scale / this.cell);
      const s = this.stateAt(this.tick);
      let found = null;
      for (const e of s.entities.values()) {
        const size = (this.props[TYPES[e[2]]] || {}).size || 1;
        if (cx >= e[3] && cx < e[3] + size && cy >= e[4] && cy < e[4] + size) { found = e[0]; break; }
      }
      this.selected = found;
      this.draw();
    }
  }

  function esc(s) { return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

  window.CodeWarsViewer = { mount: (root) => new Viewer(root) };
})();
