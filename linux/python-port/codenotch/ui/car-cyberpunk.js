/* Car Analysor Codenotch skin: quota semantics and DOM decoration only. */
(function () {
  'use strict';
  const cfg = window.CAR_ANALYSOR_NOTCH_CONFIG;
  if (!cfg) return;
  const colors = () => document.documentElement.dataset.theme === 'light' ? (cfg.light_colors || cfg.colors) : cfg.colors;
  document.documentElement.dataset.carAnalysor = 'on';
  function applyColors() {
    const root = document.documentElement;
    if (cfg.theme_mode && root.dataset.theme !== cfg.theme_mode) root.dataset.theme = cfg.theme_mode;
    const t = cfg.theme_tokens;
    if (t && cfg.theme_mode === 'light') {
      const tokens = {pill:t.surface,card:t.surface,hole:t.surface,edge:t.accent_text,'card-line':t.control_line,
        'card-rule':t.line,ink:t.ink,'ink-2':t.ink,'ink-3':t.muted,'ink-4':t.muted,'ink-dim':t.muted,
        btn:t.bg,'btn-line':t.control_line,'btn-hover':t.surface,bar:t.line,track:t.line,
        pane:t.bg,side:t.surface,group:t.surface,text:t.ink,text2:t.muted,text3:t.muted,
        'seg-bg':t.bg,line:t.control_line,sep:t.line,'on-accent':t.on_accent,accent:t.accent,
        'btn-line-hover':t.accent_text,'line-on':t.accent_text,danger:t.error,
        hi:t.accent+'12',hover:t.accent+'12','sel-soft':t.accent+'20','danger-bg':t.error+'18',
        armed:t.surface+'ed',wash:t.surface+'88','wash-on':t.surface+'d9',
        texture:t.accent+'09','texture-strong':t.accent+'12'};
      for (const [key,value] of Object.entries(tokens)) root.style.setProperty('--' + key,value);
    }
    for (const [key, value] of Object.entries(colors())) document.documentElement.style.setProperty('--ca-' + key, value);
    document.documentElement.dataset.caEffects = document.documentElement.dataset.theme === 'light' && cfg.light_effects === true ? 'on' : 'off';
  }
  applyColors();
  function tone(used) {
    const palette = colors();
    if (typeof used !== 'number' || !Number.isFinite(used) || used < 0) return palette.unknown;
    const left = 100 * (1 - Math.min(1, Math.max(0,used)));
    return left <= cfg.critical_remaining_percent + 1e-8 ? palette.critical : left <= cfg.warning_remaining_percent + 1e-8 ? palette.warning : palette.normal;
  }
  function state(snap, work, now = Date.now()) {
    const palette = colors();
    const result = (level, label, priority, left = null, windowLabel = '') => ({level,label,priority,left,windowLabel,color:palette[level === 'exhausted' ? 'critical' : level === 'attention' ? 'warning' : level]});
    if (!snap || ['none','absent','needsAuth','error','backoff'].includes(snap.status)) return result('unknown', snap?.status === 'needsAuth' ? 'SEM LOGIN' : snap?.status === 'backoff' ? 'AGUARDE' : snap?.status === 'error' ? 'ERRO' : 'SEM DADO',0);
    if (snap.status !== 'ok' || !Number.isFinite(snap.fetched_at) || snap.fetched_at <= 0 || now - snap.fetched_at > cfg.stale_after_minutes * 60000 || snap.fetched_at > now + 60000) return result('unknown','DESATUAL.',0);
    const windows = (Array.isArray(snap.windows) ? snap.windows : []).filter(w => w && w.count == null && typeof w.used === 'number' && Number.isFinite(w.used) && w.used >= 0);
    const tightest = windows.reduce((a,b) => !a || b.used > a.used ? b : a, null);
    const left = tightest ? 100 * (1 - Math.min(1, Math.max(0,tightest.used))) : null;
    const label = (tightest?.label || tightest?.id || '') + (tightest?.derived ? ' (estimativa)' : '');
    if (left !== null && left <= 0) return result('exhausted','ESGOTADO',6,left,label);
    if (left !== null && left <= cfg.critical_remaining_percent + 1e-8) return result('critical','CRÍTICO',5,left,label);
    if (left !== null && left <= cfg.warning_remaining_percent + 1e-8) return result('warning','ATENÇÃO',4,left,label);
    if (work === 'attention') return result('attention','AGUARD.',3,left,label);
    if (work === 'running') return result('active','ATIVO',2,left,label);
    return left === null ? result('unknown','SEM COTA',0) : result('normal','SALDO OK',1,left,label);
  }
  function leftText(value) { return value > 0 && value < .1 ? '<0.1' : value < 1 && value > 0 ? value.toFixed(1) : String(Math.floor(value + 1e-8)); }
  window.CarAnalysorNotch = {tone,state,leftText};
  function attach() {
    if (typeof window.renderRing !== 'function' || typeof window.providers !== 'function') return;
    const capsule = document.getElementById('pill');
    if (capsule && !capsule.querySelector('.ca-heartbeat')) {
      const heartbeat = document.createElement('span');
      heartbeat.className = 'ca-heartbeat';
      heartbeat.setAttribute('aria-hidden','true');
      capsule.appendChild(heartbeat);
    }
    const originalRing = window.renderRing;
    const originalCard = window.renderCard;
    const originalShapes = window.pillShapes;
    const originalGlyph = window.glyphHtml;
    if (typeof originalGlyph === 'function') window.glyphHtml = function (p,small) {
      const mark = cfg.marks?.[p.base || p.id];
      return mark ? '<span class="mark ca-brand">' + mark + '</span>' : originalGlyph.apply(this,arguments);
    };
    function decorate() {
      let overall = null;
      for (const p of providers()) {
        const cell = document.querySelector('.cell[data-p="' + CSS.escape(p.id) + '"]');
        if (!cell) continue;
        const s = state(p.snap,workState(p.base));
        cell.dataset.caLevel = s.level;
        let badge = cell.querySelector('.ca-state');
        if (!badge) {badge = document.createElement('div');badge.className = 'ca-state';badge.appendChild(document.createElement('strong'));const metric = document.createElement('span');metric.className = 'ca-metric';badge.appendChild(metric);cell.appendChild(badge);}
        badge.querySelector('strong').textContent = s.label;
        const h = headlineOf(p.snap,p.base);
        badge.querySelector('.ca-metric').textContent = h?.count != null ? 'CONTAGEM' : s.label === 'DESATUAL.' ? 'USO ANT.' : 'USADO';
        cell.setAttribute('aria-label',p.name + ': ' + s.label + (s.left === null ? '' : ', ' + leftText(s.left) + '% restantes na cota mais restrita'));
        const arc = cell.querySelector('svg.activity');
        // Activity remains distinct from the usage ring. Critical quota takes priority.
        if (arc) for (const item of arc.querySelectorAll('[stroke]')) item.setAttribute('stroke',s.level === 'unknown' ? colors().active : s.color);
        if (!overall || s.priority > overall.priority) overall = s;
      }
      const pill = document.getElementById('pill');
      if (pill && overall) {pill.style.setProperty('--ca-signal',overall.color);pill.classList.toggle('ca-dense',providers().length > 4);}
      // The added label changes height: refresh the native input rectangles now.
      if (typeof reportHot === 'function') reportHot();
    }
    window.renderRing = function () {originalRing.apply(this,arguments);decorate();};
    window.renderCard = function () {
      originalCard.apply(this,arguments);
      const p = providers().find(x => x.id === hoverId) || providers()[0];
      if (!p) return;
      const s = state(p.snap,workState(p.base));
      const card = document.getElementById('card');
      card.style.setProperty('--ca-signal',s.color);
      const summary = document.createElement('div');summary.className = 'ca-summary';
      summary.textContent = 'LINK // ' + s.label + (s.left === null ? '' : ' · ' + leftText(s.left) + '% RESTANTES');
      const detail = document.createElement('small');
      detail.textContent = s.left === null ? 'Saldo atual não confirmado. O indicador de atividade é independente da leitura de cota.' : 'Cota mais restrita: ' + s.windowLabel + '. Os números dos anéis mostram uso consumido.';
      summary.appendChild(detail);
      card.querySelector('.c-head')?.after(summary);
      placeCard();reportHot();
    };
    if (typeof originalShapes === 'function') window.pillShapes = function () {
      const old = originalShapes.apply(this,arguments);
      const pill = document.getElementById('pill');
      if (!pill) return old;
      return {depth:edgeIsVertical()?pill.offsetWidth:pill.offsetHeight,length:edgeIsVertical()?pill.offsetHeight:pill.offsetWidth};
    };
    readPalette();renderRing();
    if (document.getElementById('card')?.classList.contains('show')) renderCard();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded',attach,{once:true}); else attach();
  new MutationObserver(() => { applyColors(); window.renderRing?.(); }).observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});
})();
