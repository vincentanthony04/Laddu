(() => {
  'use strict';
  const INTERNAL_CHART_ENABLED = false; // CUSTOM_CHART_DISABLED: broker chart only; internal chart is never a decision/live authority

  const $ = id => document.getElementById(id);
  const all = selector => [...document.querySelectorAll(selector)];
  const intervals = [
    ['1m', '1minute'], ['3m', '3minute'], ['5m', '5minute'], ['15m', '15minute'],
    ['30m', '30minute'], ['1H', '60minute'], ['4H', '240minute'], ['1D', 'day'],
    ['1W', 'week'], ['1M', 'month'],
  ];
  const state = {
    page: 'workspace', workspace: null, stock: null, symbol: '', stockMode: 'delivery',
    desk: 'intraday', metricMode: 'all', performanceMode: 'all', performancePeriod: 'all',
    interval: 'day', performance: null, modelPaper: null, modelPaperBook: 'final', modelPaperScope: 'all', research: null, researchPlane: null, researchReplay: null, ready: null,
    chart: null, candleSeries: null, candles: [], priceLines: [], chartBefore: null,
    volumeChart: null, volumeSeries: null, volumeAvgSeries: null, rsiChart: null, rsiSeries: null,
    macdChart: null, macdLineSeries: null, macdSignalSeries: null, macdHistogramSeries: null,
    chartLoadingOlder: false, chartHasMore: true, followLive: true, liveTimer: null,
    chartProjection: {}, overlaySeries: {}, tableSort: {}, rangeSyncing: false,
    chartRangeProgrammaticUntil: 0, chartUserRange: null,
    projectionRefreshTimer: null, projectionRefreshEpoch: 0, projectionRefreshAttempts: 0,
    chartWarmRetryTimer: null, chartWarmRetryAttempts: 0, snapshotWarmRetryTimer: null, snapshotWarmRetryAttempts: 0,
    overlayEnabled: {volume:true, trade:true, major_sr:true, vwap:false, ema:false, supertrend:false, rsi:false, macd:false, camarilla:false},
    searchEpoch: 0, stockEpoch: 0, workspaceEpoch: 0, toastTimer: null, theme: 'light',
    workspacePollBusy: false, workspaceStale: false, lastWorkspaceSuccessAt: 0, liveTruth: null, liveTruthPollBusy: false, frontendIdentity: null, frontendIdentityValid: false, frontendIdentityReason: 'Frontend identity not verified', livePrices: {}, liveCursor: 0, opportunityCandidates: [], workspaceSignalMode: 'all', workspaceSignalLimit: 5,
    operations: null, operationsForward: null, operationsClock: null, operationsLogs: null, operationsProblems: [], operationsEvidence: [], operationsPollBusy: false, operationsConsolePayload: null, forwardEvidence: null,
    candidateInspectKey: '', candidateInspectSnapshot: null,
    researchScope: 'all', researchMode: 'all', researchOutcome: 'all', researchPage: 0, researchPageSize: 50, researchFocusCandidate: '',
    workspaceOutcomeRefreshAt: 0, workspaceOutcomeBusy: false,
    animatedNumberValues: {},
  };

  const text = value => value == null || value === '' ? '' : String(value);
  const esc = value => text(value).replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
  const number = value => {
    if (value === null || value === undefined) return null;
    if (typeof value === 'string' && value.trim() === '') return null;
    if (typeof value === 'boolean') return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  };
  const positivePrice = value => { const parsed = number(value); return parsed !== null && parsed > 0 ? parsed : null; };
  const pick = (source, ...keys) => {
    for (const key of keys) if (source && source[key] !== undefined && source[key] !== null && source[key] !== '') return source[key];
    return null;
  };
  const rows = value => Array.isArray(value) ? value : [];
  const safeStorageGet = key => { try { return window.localStorage?.getItem(key) || ''; } catch { return ''; } };
  const safeStorageSet = (key, value) => { try { window.localStorage?.setItem(key, value); return true; } catch { return false; } };
  const label = value => text(value || 'Unavailable').replaceAll('_', ' ').replace(/\b\w/g, char => char.toUpperCase());
  function researchStageLabel(value, {geometryComplete=false, marketOpen=true, evidenceState='UNKNOWN'} = {}) {
    const raw = text(value || 'WATCH').trim().toUpperCase().replace(/[\s-]+/g,'_');
    const evidence = text(evidenceState).toUpperCase();
    if (/REJECT|FAIL|BLOCK|INVALID/.test(raw)) return label(raw);
    if (evidence === 'INCOMPLETE' || evidence === 'STALE') return evidence === 'STALE' ? 'STALE EVIDENCE' : 'EVIDENCE INCOMPLETE';
    if (!marketOpen && /PROMOT|FINAL|ACTIONABLE|OPEN|SELECTED|VALIDAT|QUALIF|ARMED|READY/.test(raw) && !geometryComplete) return 'NEXT SESSION';
    if (/PROMOT|FINAL|ACTIONABLE|OPEN/.test(raw)) return geometryComplete ? 'FINAL' : 'LIVE VALIDATION';
    if (/SELECTED|VALIDAT/.test(raw)) return 'LIVE VALIDATION';
    if (/QUALIF|ARMED|READY/.test(raw)) return 'QUALIFIED';
    if (/POTENTIAL|WATCH|PREPARED|PREPARING|UNDER_REVIEW/.test(raw)) return 'WATCH';
    if (/SCREEN|SHORTLIST/.test(raw)) return 'SHORTLIST';
    if (/RESEARCH/.test(raw)) return 'WATCH';
    return label(raw);
  }
  function researchStageTone(display) {
    const value = text(display).toUpperCase();
    if (value === 'FINAL') return 'semantic-positive';
    if (value === 'QUALIFIED' || value === 'LIVE VALIDATION') return 'semantic-info';
    if (value === 'NEXT SESSION' || value === 'EVIDENCE INCOMPLETE' || value === 'STALE EVIDENCE') return 'semantic-warning';
    if (value === 'WATCH' || value === 'SHORTLIST' || /PENDING|DEFER/.test(value)) return 'semantic-warning';
    if (/REJECT|FAIL|BLOCK|INVALID/.test(value)) return 'semantic-negative';
    return 'semantic-neutral';
  }
  const formatNumber = (value, digits = 2) => {
    const parsed = number(value);
    return parsed === null ? 'Unavailable' : new Intl.NumberFormat('en-IN', {maximumFractionDigits: digits}).format(parsed);
  };
  const money = value => {
    const parsed = number(value);
    return parsed === null ? 'Unavailable' : new Intl.NumberFormat('en-IN', {style:'currency', currency:'INR', maximumFractionDigits:2}).format(parsed);
  };
  const pct = value => {
    const parsed = number(value);
    return parsed === null ? 'Unavailable' : `${parsed >= 0 ? '+' : ''}${parsed.toFixed(2)}%`;
  };
  const compactTime = value => {
    const stamp = Date.parse(text(value));
    if (!Number.isFinite(stamp)) return 'Time unavailable';
    return `${new Intl.DateTimeFormat('en-IN', {timeZone:'Asia/Kolkata', day:'2-digit', month:'short', hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false}).format(new Date(stamp))} IST`;
  };
  const chartTimestamp = time => {
    if (typeof time === 'number' && Number.isFinite(time)) return time;
    if (time && typeof time === 'object' && Number.isFinite(time.year) && Number.isFinite(time.month) && Number.isFinite(time.day)) {
      return Date.UTC(time.year, time.month - 1, time.day) / 1000;
    }
    const stamp = Date.parse(text(time));
    return Number.isFinite(stamp) ? stamp / 1000 : null;
  };
  const chartTimeFormatter = time => {
    const seconds = chartTimestamp(time);
    if (seconds === null) return 'Time unavailable';
    const date = new Date(seconds * 1000);
    if (/minute$/.test(state.interval)) {
      return `${new Intl.DateTimeFormat('en-IN', {timeZone:'Asia/Kolkata', day:'2-digit', month:'short', year:'numeric', hour:'2-digit', minute:'2-digit', hour12:false}).format(date)} IST`;
    }
    return new Intl.DateTimeFormat('en-IN', {timeZone:'Asia/Kolkata', day:'2-digit', month:'short', year:'numeric'}).format(date);
  };
  const axisTick = (time, tickMarkType = 3) => {
    const seconds = chartTimestamp(time);
    if (seconds === null) return '';
    const date = new Date(seconds * 1000);
    const fmt = options => new Intl.DateTimeFormat('en-IN', {timeZone:'Asia/Kolkata', ...options}).format(date);
    if (/minute$/.test(state.interval)) {
      // Intraday uses one grammar: session separators are dates; ordinary ticks are time only.
      if (tickMarkType <= 2) return fmt({day:'2-digit', month:'short'});
      return fmt({hour:'2-digit', minute:'2-digit', hour12:false});
    }
    // Daily/weekly/monthly never mix "Mar 26" with "13 Mar" on the same axis.
    // Year boundaries alone use YYYY; every other tick is dd MMM.
    if (tickMarkType === 0) return fmt({year:'numeric'});
    return fmt({day:'2-digit', month:'short'});
  };
  const chartRangeSummary = candles => {
    if (!candles?.length) return 'No verified bars';
    const first = candles[0]?.time, last = candles.at(-1)?.time;
    const fmt = time => {
      const seconds = chartTimestamp(time); if (seconds === null) return '—';
      const date = new Date(seconds * 1000);
      return /minute$/.test(state.interval)
        ? new Intl.DateTimeFormat('en-IN',{timeZone:'Asia/Kolkata',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit',hour12:false}).format(date)
        : new Intl.DateTimeFormat('en-IN',{timeZone:'Asia/Kolkata',day:'2-digit',month:'short',year:'2-digit'}).format(date);
    };
    return `${candles.length} bars · ${fmt(first)} → ${fmt(last)} IST`;
  };
  const sourceTime = source => pick(source, 'provider_timestamp', 'source_time', 'exchange_timestamp', 'timestamp', 'as_of', 'last_refresh');
  const freshness = (rawState, stamp, marketOpen = state.workspace?.market_open) => {
    const raw = text(rawState).toLowerCase();
    const parsed = Date.parse(text(stamp));
    const age = Number.isFinite(parsed) ? Math.max(0, (Date.now() - parsed) / 1000) : null;
    if (/completed_session_close|verified_close|historical_close/.test(raw)) return {state:'closed', label:'Verified close', age};
    if (marketOpen === false && (age !== null || /current_at_close|closed_market|market_closed|closed/.test(raw))) return {state:'closed', label:'Market closed', age};
    if (/unavailable|missing|unknown/.test(raw) && age === null) return {state:'unavailable', label:'Unavailable', age};
    if (age !== null && age <= 2) return {state:'live', label:`Live · ${age.toFixed(1)}s`, age};
    if (age !== null && age <= 10) return {state:'delayed', label:`Delayed · ${Math.ceil(age)}s`, age};
    if (age !== null) return {state:'stale', label:`Stale · ${Math.round(age)}s`, age};
    if (/live|current/.test(raw)) return {state:'unavailable', label:'Freshness unknown', age};
    if (/warm|pending|start/.test(raw)) return {state:'warming', label:'Warming', age};
    return {state:'unavailable', label:label(rawState), age};
  };

  function livePriceMotion(key, value) {
    const parsed = number(value);
    if (!key || parsed === null) return '';
    const prior = number(state.livePrices[key]);
    state.livePrices[key] = parsed;
    if (prior === null || prior === parsed) return '';
    return parsed > prior ? 'tick-up' : 'tick-down';
  }

  function animatedNumberString(value, digits, prefix='', suffix='') {
    const parsed = number(value);
    if (parsed === null) return '—';
    const body = new Intl.NumberFormat('en-IN', {minimumFractionDigits:digits, maximumFractionDigits:digits}).format(parsed);
    return `${prefix}${body}${suffix}`;
  }
  function animatedNumberHtml(key, value, {digits=0, prefix='', suffix=''} = {}) {
    const parsed = number(value);
    if (parsed === null) return `<span class="animated-number unavailable">—</span>`;
    // The authoritative final value is real DOM text from first paint. Animation is enhancement only.
    const display = animatedNumberString(parsed,digits,prefix,suffix);
    return `<span class="animated-number" data-number-key="${esc(key)}" data-number-target="${parsed}" data-number-digits="${digits}" data-number-prefix="${esc(prefix)}" data-number-suffix="${esc(suffix)}">${esc(display)}</span>`;
  }
  function activateNumberAnimations(root=document) {
    if (!root?.querySelectorAll) return;
    root.querySelectorAll('[data-number-key]').forEach(node => {
      const key=node.dataset.numberKey; const target=number(node.dataset.numberTarget);
      const digits=Math.max(0, Number(node.dataset.numberDigits || 0));
      const prefix=node.dataset.numberPrefix || ''; const suffix=node.dataset.numberSuffix || '';
      const finalText=animatedNumberString(target,digits,prefix,suffix);
      if (!key || target === null) { node.textContent=finalText; return; }
      const previous=number(state.animatedNumberValues[key]);
      state.animatedNumberValues[key]=target;
      // Fail safe: the final value already exists as text before any animation is attempted.
      if (previous === null || previous === target || !window.requestAnimationFrame || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
        node.textContent=finalText;
        return;
      }
      const duration=520; const started=performance.now();
      const direction=target>previous?'number-up':'number-down';
      node.classList.add(direction);
      try {
        const tick=now => {
          if (!node.isConnected) return;
          const t=Math.min(1,Math.max(0,(now-started)/duration));
          const eased=1-Math.pow(1-t,3);
          const value=previous+(target-previous)*eased;
          node.textContent=animatedNumberString(digits===0?Math.round(value):value,digits,prefix,suffix);
          if (t<1) requestAnimationFrame(tick);
          else { node.textContent=finalText; node.classList.remove(direction); }
        };
        requestAnimationFrame(tick);
      } catch (_) {
        node.textContent=finalText;
        node.classList.remove(direction);
      }
    });
  }

  function cssVar(name, fallback) {
    const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || fallback;
  }
  function chartPalette() {
    return {
      bg: cssVar('--bg', state.theme === 'light' ? '#f5f7fb' : '#08101d'),
      text: cssVar('--muted', state.theme === 'light' ? '#5e6f84' : '#9eb0c6'),
      line: cssVar('--line', state.theme === 'light' ? '#d8e1ec' : '#26364d'),
      grid: state.theme === 'light' ? '#e9eef5' : '#17243a',
      green: cssVar('--green', '#43d39e'), red: cssVar('--red', '#ff6b7a'), cyan: cssVar('--cyan', '#00c2ff'),
      blue: cssVar('--blue', '#70b7ff'), accent: cssVar('--accent', '#5ca8ff'), amber: cssVar('--amber', '#f6be5b'),
    };
  }
  function resizeCharts() {
    if (!INTERNAL_CHART_ENABLED) return;
    const pairs = [
      [state.chart, $('chartHost')], [state.volumeChart, $('volumeChart')],
      [state.rsiChart, $('rsiChart')], [state.macdChart, $('macdChart')],
    ];
    for (const [chart, host] of pairs) {
      if (!chart || !host) continue;
      const rect = host.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0) try { chart.resize(Math.floor(rect.width), Math.floor(rect.height)); } catch {}
    }
  }
  function applyChartTheme() {
    const p = chartPalette();
    for (const chart of [state.chart,state.volumeChart,state.rsiChart,state.macdChart]) {
      if (!chart) continue;
      try { chart.applyOptions({layout:{background:{color:p.bg},textColor:p.text},grid:{vertLines:{color:p.grid},horzLines:{color:p.grid}},rightPriceScale:{borderColor:p.line},timeScale:{borderColor:p.line,tickMarkFormatter:axisTick},localization:{timeFormatter:chartTimeFormatter}}); } catch {}
    }
    try { state.candleSeries?.applyOptions({upColor:p.green,downColor:p.red,wickUpColor:p.green,wickDownColor:p.red}); } catch {}
    renderChartOverlays();
  }
  function applyTheme(value, {persist=true} = {}) {
    const next = value === 'light' ? 'light' : 'dark';
    state.theme = next;
    document.documentElement.dataset.theme = next;
    if (persist) safeStorageSet('projectLadduTheme', next);
    const button = $('themeToggle');
    if (button) {
      const destination = next === 'light' ? 'Dark' : 'Light';
      button.textContent = destination;
      button.setAttribute('aria-label', `Switch to ${destination.toLowerCase()} theme`);
      button.title = `Switch to ${destination.toLowerCase()} theme`;
    }
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', next === 'light' ? '#f5f7fb' : '#0b1220');
    applyChartTheme();
  }
  function initialiseTheme() {
    const stored = safeStorageGet('projectLadduTheme');
    applyTheme(stored === 'dark' ? 'dark' : 'light', {persist:false});
  }

  async function api(path, options = {}) {
    const controller = new AbortController();
    const timeout = options.timeout || 3000;
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetch(path, {
        method: options.method || 'GET', signal: controller.signal, cache: 'no-store',
        headers: {'Accept':'application/json', ...(options.body ? {'Content-Type':'application/json'} : {})},
        body: options.body ? JSON.stringify(options.body) : undefined,
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok && !['WARMING', 'STALE_LAST_KNOWN'].includes(text(payload.state).toUpperCase())) {
        const error = new Error(payload.error || payload.message || `HTTP ${response.status}`);
        error.payload = payload;
        throw error;
      }
      return payload;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error(`Timed out after ${(timeout / 1000).toFixed(1)}s`);
      throw error;
    } finally {
      clearTimeout(timer);
    }
  }

  function setStatePill(target, status, display) {
    if (!target) return;
    const normal = text(status || 'neutral').toLowerCase();
    target.className = `state-pill ${normal}`;
    target.innerHTML = `<i></i><span>${esc(display || label(status))}</span>`;
  }
  function trustRevision(trust = {}) {
    const stamp=Date.parse(text(trust.evaluated_at));
    const sequence=number(trust.sequence_us);
    return {stamp:Number.isFinite(stamp) ? stamp : 0, sequence:sequence ?? 0};
  }
  function currentTrust(workspaceTrust = {}) {
    const live=state.liveTruth?.trust || {};
    if (!Object.keys(live).length) return workspaceTrust;
    const lv=trustRevision(live), wv=trustRevision(workspaceTrust);
    return lv.stamp > wv.stamp || (lv.stamp === wv.stamp && lv.sequence >= wv.sequence) ? live : workspaceTrust;
  }
  function customerTrust(workspaceTrust = {}) {
    const trust=currentTrust(workspaceTrust);
    if (state.frontendIdentityValid === true) return trust;
    return {...trust, state:'DO_NOT_TRUST', decision_admission_allowed:false, reason:state.frontendIdentityReason || 'Frontend identity is not verified'};
  }
  function staleWorkspaceTrust(reason='Workspace refresh unavailable') {
    return {state:'DEGRADED', decision_admission_allowed:false, evaluated_at:new Date().toISOString(), reason:`${reason} · retained rows are stale and non-actionable`};
  }
  function renderTrustStrip(trust = {}) {
    const strip = $('trustStrip');
    if (!strip) return;
    const raw = text(trust.state || 'WARMING').toUpperCase();
    const css = raw === 'TRUSTED' ? 'trust-trusted' : raw === 'DO_NOT_TRUST' ? 'trust-blocked' : raw === 'DEGRADED' ? 'trust-degraded' : 'trust-warming';
    strip.classList.remove('trust-trusted','trust-degraded','trust-blocked','trust-warming');
    strip.classList.add(css);
    if ($('trustState')) $('trustState').textContent = raw === 'DO_NOT_TRUST' ? 'DO NOT TRUST' : label(raw);
    if ($('trustReason')) $('trustReason').textContent = trust.reason || (raw === 'TRUSTED' ? 'Customer read path current' : 'Trust projection warming');
    for (const [desk, id] of [['intraday','trustCadenceIntraday'],['delivery','trustCadenceDelivery']]) {
      const cadence = trust.scanner_cadence?.[`${desk}_scanner`] || {};
      const cadenceState = text(cadence.state || 'WARMING').toUpperCase();
      const displayState = cadenceState === 'EXPECTED_IDLE' ? 'SLEEPING' : cadenceState;
      const cadenceHealthy = cadence.healthy === true;
      const remaining = number(cadence.seconds_to_next);
      const heartbeat = number(cadence.heartbeat_age_sec);
      const cadenceParts = [`${label(desk)} · ${displayState}`];
      if (cadence.last_cycle_at) cadenceParts.push(`Last ${compactTime(cadence.last_cycle_at)}`);
      if (cadence.next_cycle_at) cadenceParts.push(`Next ${compactTime(cadence.next_cycle_at)}`);
      if (remaining !== null) cadenceParts.push(`${Math.max(0,Math.round(remaining))}s`);
      if (heartbeat !== null) cadenceParts.push(`HB ${formatNumber(heartbeat,1)}s`);
      if ($(id)) {
        $(id).textContent = cadenceParts.join(' · ');
        $(id).className = `trust-cadence ${cadenceHealthy && /SLEEPING|RUNNING|CONTINUING|EXPECTED_IDLE|READY/.test(cadenceState) ? 'healthy' : /FAILED|BLOCKED/.test(cadenceState) ? 'failed' : 'neutral'}`;
      }
    }
    const p95 = number(trust.latency?.customer_read_p95_ms);
    const samples = number(trust.latency?.customer_read_samples);
    if ($('trustLatency')) $('trustLatency').textContent = p95 === null ? '' : `Read p95 ${p95 < 1000 ? `${formatNumber(p95,0)}ms` : `${formatNumber(p95/1000,1)}s`}${samples===null?'':` · n=${formatNumber(samples,0)}`}`;
  }

  function trustBlocksAdmission(trust = {}) {
    return state.frontendIdentityValid !== true || text(trust.state).toUpperCase() === 'DO_NOT_TRUST' || trust.decision_admission_allowed === false;
  }

  function notice(message, tone = 'warning') {
    const node = $('globalNotice');
    node.hidden = !message;
    node.textContent = message || '';
    node.dataset.tone = tone;
  }
  function toast(message) {
    const node = $('toast');
    node.hidden = false;
    node.textContent = message;
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => { node.hidden = true; }, 4200);
  }
  function renderStats(target, items) {
    target.innerHTML = items.map(item => `<div class="stat ${esc(item.tone || '')}"><span>${esc(item.label)}</span><b>${esc(item.value)}</b>${item.detail ? `<small>${esc(item.detail)}</small>` : ''}</div>`).join('');
  }
  function emptyRow(columns, message) {
    return `<tr class="empty-row"><td colspan="${columns}">${esc(message)}</td></tr>`;
  }
  function rowSymbol(row) {
    return text(pick(row, 'symbol', 'trading_symbol', 'stock')).trim().toUpperCase();
  }
  function rowMode(row) {
    const mode = text(pick(row, 'mode', 'desk')).toLowerCase();
    return mode === 'intraday' ? 'intraday' : 'delivery';
  }
  function candidateStableKey(row) {
    return `${rowMode(row)}|${text(row?.instrument_key || row?.provider_instrument_key)}|${rowSymbol(row)}`;
  }

  function evidenceScoreValue(row) {
    return number(pick(row?.trader_explanation || {}, 'score')) ?? number(pick(row, 'rank_score', 'evidence_score'));
  }
  function candidateStageWeight(row) {
    const stage = text(pick(row,'display_stage','candidate_stage','opportunity_stage','lifecycle_state','canonical_state','status','decision')).toUpperCase();
    if (/OPEN|SETTLED|FINAL|PROMOTED|ACTIONABLE/.test(stage)) return 60;
    if (/RESEARCH|QUALIFIED|ARMED|VALIDATING/.test(stage)) return 50;
    if (/PREPARED|WATCH|UNDER_REVIEW|SCREENED/.test(stage)) return 30;
    if (/REJECT|FAILED|INVALID|BLOCKED/.test(stage)) return 5;
    return 10;
  }
  function candidateRecency(row) {
    for (const key of ['last_seen_at','updated_at','last_update','finalized_at','generated_at','observed_at','created_at']) {
      const stamp = Date.parse(text(row?.[key]));
      if (Number.isFinite(stamp)) return stamp;
    }
    return 0;
  }
  function dedupeDeskCandidates(input, desk) {
    // One human row per canonical symbol+desk. Same symbol in Delivery and
    // Intraday remains legitimate because those are independent theses. If
    // multiple projections exist inside one desk, prefer the most advanced,
    // then freshest, then strongest ranked evidence.
    const best = new Map();
    for (const row of rows(input)) {
      if (rowMode(row) !== desk) continue;
      const symbol = rowSymbol(row);
      if (!symbol) continue;
      const score = [candidateStageWeight(row), candidateRecency(row), evidenceScoreValue(row) ?? -1];
      const prior = best.get(symbol);
      if (!prior || score[0] > prior.score[0] || (score[0] === prior.score[0] && (score[1] > prior.score[1] || (score[1] === prior.score[1] && score[2] > prior.score[2])))) {
        best.set(symbol, {row, score});
      }
    }
    return [...best.values()].map(item => item.row);
  }
  function displayGeometry(row, field) {
    const pending = text(row?.trade_geometry_display_state).toUpperCase() === 'PENDING_LIVE_CONFIRMATION';
    if (pending) return null;
    const raw = field === 'entry' ? pick(row,'display_entry','entry','planned_entry')
      : field === 'target' ? pick(row,'display_target','target','t1','planned_t1')
      : field === 'stop' ? pick(row,'display_stop','stop','sl','planned_sl')
      : field === 'rr' ? pick(row,'display_rr','rr','planned_rr') : null;
    const value = number(raw);
    // Zero is never a valid Indian cash-equity entry/target/stop and is used by
    // legacy projections as a missing-value sentinel. Do not display it as a plan.
    if (value === null || value <= 0) return null;
    return value;
  }
  function humanGateReason(value) {
    const raw = text(value).trim();
    const key = raw.toUpperCase();
    const known = {
      QUOTE_UNAVAILABLE:'Current quote unavailable',
      LOCAL_HISTORY_PENDING:'Required local candle history is not ready',
      LOCAL_CONTEXT_PREPARATION_FAILED:'Local analysis context could not be prepared',
      ANALYSIS_TIMEOUT:'Analysis did not finish within the governed cycle',
      ANALYSIS_CAPACITY:'Analysis deferred by available analysis capacity',
      ANALYSIS_BUDGET_EXHAUSTED:'Analysis deferred to the next cycle; coverage is preserved',
      ANALYSIS_RETURNED_NO_QUALIFIED_DECISION:'Mathematics found no qualifying setup',
      CAPACITY_DEFERRED:'Deferred to the next governed cycle',
      DATA_PENDING:'Required current evidence is still pending',
      BLOCKED:'A mandatory qualification gate failed',
    };
    return known[key] || label(raw);
  }
  function compactReason(value, fallback = 'No additional explanation published') {
    const result = text(value).trim();
    return result || fallback;
  }
  function currentCapturedPriceHtml(row) {
    const current = number(pick(row,'display_price','current_price'));
    const captured = number(pick(row,'captured_price','ltp'));
    const authority = text(row?.current_price_authority);
    const verified = authority && authority !== 'CANDIDATE_SNAPSHOT';
    if (current === null && captured === null) return '<span class="muted">Unavailable</span>';
    if (!verified) {
      const value = current ?? captured;
      return `<div class="price-stack"><b>${esc(money(value))}</b><small>Captured snapshot</small></div>`;
    }
    const different = captured !== null && current !== null && Math.abs(current - captured) > 0.005;
    return `<div class="price-stack"><b>${esc(money(current))}</b><small>${different ? `Captured ${esc(money(captured))}` : esc(label(row.current_price_state || 'verified'))}</small></div>`;
  }
  function modelInfluenceHtml(model = {}) {
    const authority = number(model.authority_pct) ?? 0;
    const contribution = number(model.rank_contribution);
    const score = number(model.score);
    const influenced = model.influence_applied === true && authority > 0;
    const stateLabel = label(model.state || (score === null ? 'No model score' : 'Shadow'));
    if (!influenced) {
      return `<b>0% ranking authority</b><small>${esc(stateLabel)}${score === null ? '' : ` · model score ${esc(formatNumber(score,1))}`}</small>`;
    }
    return `<b>${esc(formatNumber(authority,1))}% ranking authority</b><small>${esc(stateLabel)}${score === null ? '' : ` · score ${esc(formatNumber(score,1))}`}${contribution === null ? '' : ` · rank ${contribution >= 0 ? '+' : ''}${esc(formatNumber(contribution,2))}`}</small>`;
  }
  function candidateExplanationHtml(row, ordinal = null) {
    const explain = row?.trader_explanation || {};
    const components = rows(explain.components);
    const blockers = rows(explain.blockers);
    const missing = rows(explain.missing_inputs);
    const score = evidenceScoreValue(row);
    const positive = components.filter(item => number(item.points) !== null && number(item.points) > 0).sort((a,b) => (number(b.points)||0) - (number(a.points)||0)).slice(0,5);
    const why = positive.length ? positive.map(item => `<li><b>${esc(item.name || 'Evidence')}</b><span>${esc(formatNumber(item.points,1))}/${esc(formatNumber(item.max_points,0))} · ${esc(compactReason(item.reason))}</span></li>`).join('') : `<li><span>${esc(compactReason(row.ranking_explanation || row.reason || row.setup, 'Detailed rank components have not been materialized for this candidate yet.'))}</span></li>`;
    const held = blockers.length ? blockers.slice(0,6).map(reason => `<li><span>${esc(humanGateReason(reason))}</span></li>`).join('') : missing.length ? missing.slice(0,6).map(reason => `<li><span>Waiting for ${esc(label(reason))}</span></li>`).join('') : '<li><span>No explicit blocker is published in the current projection.</span></li>';
    return `<div class="candidate-explain-head"><div><span>${esc(rowSymbol(row))} · ${esc(label(rowMode(row)))}</span><b>${ordinal ? `#${esc(formatNumber(ordinal,0))}` : 'Candidate'}${score === null ? '' : ` · Evidence ${esc(formatNumber(score,1))}`}</b><small>List position starts at #1. Evidence score is diagnostic, not probability of profit.</small></div><div class="candidate-explain-actions"><button class="secondary-button" data-open-stock="${esc(rowSymbol(row))}" data-instrument-key="${esc(text(row.instrument_key || row.provider_instrument_key))}" data-mode="${esc(rowMode(row))}">Open Stock Report</button><button class="secondary-button" type="button" data-close-candidate-inspect>Close</button></div></div><div class="explain-grid"><section><h4>Why it reached this list</h4><ul>${why}</ul></section><section><h4>What still blocks Final</h4><ul>${held}</ul></section><section><h4>ML / model influence</h4><div class="model-influence">${modelInfluenceHtml(explain.model || {})}</div><p>${esc(label(explain.scoring_state || explain.readiness || 'Evidence state unavailable'))}</p></section></div><div class="explain-price-line"><span>Current: <b>${number(row.current_price) === null ? 'Unavailable' : esc(money(row.current_price))}</b></span><span>Captured: <b>${number(row.captured_price) === null ? 'Unavailable' : esc(money(row.captured_price))}</b></span><span>${esc(row.trade_geometry_display_state === 'PENDING_LIVE_CONFIRMATION' ? 'Entry / target / stop pending live confirmation' : 'Trade geometry shown only when authorized')}</span></div>`;
  }
  function renderGatedDetails(deskRow = {}) {
    const analysis = deskRow.analysis || {};
    const stageMembers = analysis.stage_members || deskRow.stage_members || {};
    const categories = [
      ['data_pending','Data pending'], ['data_blocked','Blocked'], ['capacity_deferred','Deferred'], ['mathematically_rejected','Mathematical rejection'],
    ];
    const material = [];
    for (const [key, display] of categories) {
      for (const row of rows(stageMembers[key])) material.push({...row, _displayStage:display});
    }
    const seen = new Set();
    const unique = material.filter(row => {
      const key = `${rowSymbol(row)}|${text(row.state)}|${text(row.reason)}`;
      if (!rowSymbol(row) || seen.has(key)) return false;
      seen.add(key); return true;
    }).slice(0,24);
    const blockers = analysis.top_blockers || deskRow.top_blockers || {};
    const blockerEntries = Object.entries(blockers).slice(0,5);
    $('gatedSummary').innerHTML = blockerEntries.length ? blockerEntries.map(([reason,count]) => `<span class="reason-chip"><b>${esc(formatNumber(count,0))}</b>${esc(humanGateReason(reason))}</span>`).join('') : '<span class="muted">No aggregate blocker summary is published for the latest cycle.</span>';
    $('gatedRows').innerHTML = unique.map(row => `<tr><td><button type="button" class="stock-link" data-open-stock="${esc(rowSymbol(row))}" data-instrument-key="${esc(text(row.instrument_key || row.provider_instrument_key))}" data-mode="${esc(rowMode(row))}">${esc(rowSymbol(row))}</button></td><td>${esc(row._displayStage)}</td><td title="${esc(text(row.reason))}">${esc(humanGateReason(row.reason || row.state))}</td><td>${esc(number(row.ltp) === null ? '—' : money(row.ltp))}</td></tr>`).join('') || emptyRow(4, 'No per-symbol gated/rejected samples are retained in the current scanner cycle.');
    applyStoredSort($('gatedRows'));
  }
  function decisionExplanationHtml(decision = {}, map = {}, node = {}) {
    const components = rows(decision.rank_components);
    const positive = components.filter(item => (number(item.points) || 0) > 0).sort((a,b) => (number(b.points)||0) - (number(a.points)||0)).slice(0,4);
    const blockersRaw = [decision.rank_veto_reasons, decision.promotion_blocked_by, decision.rank_gate_failures, decision.rank_conflicts, decision.rejection_reasons, decision.waiting_for];
    const blockers = [];
    const seen = new Set();
    for (const value of blockersRaw) {
      const values = Array.isArray(value) ? value : value ? [value] : [];
      for (const item of values) { const reason=text(item).trim(); if(reason && !seen.has(reason)){seen.add(reason);blockers.push(reason);} }
    }
    const model = {
      state:pick(decision,'model_state','model_ranking_stage','research_factor_state'),
      score:pick(decision,'model_score'), authority_pct:pick(decision,'model_ranking_authority_pct'),
      influence_applied:decision.model_influence_applied === true,
      rank_contribution:pick(decision,'model_rank_contribution','research_factor_points'),
    };
    const why = positive.length ? positive.map(item => `<li><b>${esc(item.name || 'Evidence')}</b><span>${esc(formatNumber(item.points,1))}/${esc(formatNumber(item.max_points,0))} · ${esc(compactReason(item.reason))}</span></li>`).join('') : `<li><span>${esc(compactReason(decision.ranking_explanation || decision.thesis || decision.reason || map.block_reason, 'No canonical decision explanation is materialized yet.'))}</span></li>`;
    const held = blockers.length ? blockers.slice(0,5).map(reason => `<li><span>${esc(humanGateReason(reason))}</span></li>`).join('') : '<li><span>No explicit blocker in this decision snapshot.</span></li>';
    return `<div class="explain-grid stock-explain"><section><h4>Why this decision</h4><ul>${why}</ul></section><section><h4>What can block / invalidate it</h4><ul>${held}</ul></section><section><h4>ML / model influence</h4><div class="model-influence">${modelInfluenceHtml(model)}</div><p>${esc(ageLabel(decision) || 'Signal age unavailable')} · ${esc(label(reassessmentState(decision) || node.research_state || 'No reassessment'))}</p></section></div>`;
  }

  function sortCellValue(cell) {
    if (!cell) return {missing:true, numeric:false, value:''};
    const explicit = cell.dataset.sortValue;
    const raw = text(explicit !== undefined ? explicit : cell.textContent).trim();
    if (!raw || /^(—|unavailable|warming|pending|n\/a)$/i.test(raw)) return {missing:true, numeric:false, value:''};
    const dateCandidate = Date.parse(raw.replace(/\s+IST$/i, ' +05:30'));
    if (/\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b/i.test(raw) && Number.isFinite(dateCandidate)) {
      return {missing:false, numeric:true, value:dateCandidate};
    }
    const normalized = raw.replace(/[₹,%]/g, '').replace(/,/g, '').replace(/^\+/, '').trim();
    if (/^-?\d+(?:\.\d+)?$/.test(normalized)) return {missing:false, numeric:true, value:Number(normalized)};
    return {missing:false, numeric:false, value:raw.toLocaleLowerCase('en-IN')};
  }
  function applyStoredSort(tbody) {
    if (!tbody?.id) return;
    const material = [...tbody.querySelectorAll('tr:not(.empty-row)')];
    material.forEach((row, index) => {
      if (row.dataset.originalIndex === undefined) row.dataset.originalIndex = String(index);
    });
    const spec = state.tableSort[tbody.id];
    const table = tbody.closest('table');
    if (!table) return;
    [...table.querySelectorAll('thead th')].forEach(th => {
      th.classList.remove('sort-asc', 'sort-desc');
      th.setAttribute('aria-sort', 'none');
    });
    if (!spec) {
      material.sort((a,b) => Number(a.dataset.originalIndex) - Number(b.dataset.originalIndex)).forEach(row => tbody.appendChild(row));
      return;
    }
    material.sort((a,b) => {
      const av = sortCellValue(a.children[spec.index]);
      const bv = sortCellValue(b.children[spec.index]);
      if (av.missing !== bv.missing) return av.missing ? 1 : -1;
      let comparison = 0;
      if (av.numeric && bv.numeric) comparison = av.value - bv.value;
      else comparison = String(av.value).localeCompare(String(bv.value), 'en-IN', {numeric:true, sensitivity:'base'});
      if (!comparison) comparison = Number(a.dataset.originalIndex) - Number(b.dataset.originalIndex);
      return spec.direction === 'asc' ? comparison : -comparison;
    }).forEach(row => tbody.appendChild(row));
    const active = table.querySelectorAll('thead th')[spec.index];
    if (active) {
      active.classList.add(spec.direction === 'asc' ? 'sort-asc' : 'sort-desc');
      active.setAttribute('aria-sort', spec.direction === 'asc' ? 'ascending' : 'descending');
    }
  }
  function sortTableFromHeader(th) {
    const table = th?.closest('table');
    const tbody = table?.querySelector('tbody[id]');
    if (!table || !tbody || !text(th.textContent).trim()) return;
    const index = [...th.parentElement.children].indexOf(th);
    const current = state.tableSort[tbody.id];
    if (!current || current.index !== index) state.tableSort[tbody.id] = {index, direction:'asc'};
    else if (current.direction === 'asc') current.direction = 'desc';
    else delete state.tableSort[tbody.id];
    applyStoredSort(tbody);
  }
  function enableSortableTables() {
    all('table').forEach(table => {
      const tbody = table.querySelector('tbody[id]');
      if (!tbody) return;
      [...table.querySelectorAll('thead th')].forEach(th => {
        if (!text(th.textContent).trim()) return;
        th.classList.add('sortable');
        th.setAttribute('tabindex', '0');
        th.setAttribute('role', 'button');
        th.setAttribute('aria-sort', 'none');
      });
    });
  }

  function parseHashRoute(raw = location.hash.slice(1)) {
    const [pagePart,query='']=text(raw).split('?',2);
    const page=pagePart || 'workspace'; const params=Object.fromEntries(new URLSearchParams(query).entries());
    return {page,params};
  }
  function routeHash(page, params = {}) {
    const query=new URLSearchParams(Object.entries(params).filter(([,value])=>text(value).trim()));
    return `#${page}${query.toString()?`?${query.toString()}`:''}`;
  }
  function showPage(page, {push = true, params = {}} = {}) {
    if (page === 'performance' || page === 'backtest') page = 'accuracy';
    if (!document.querySelector(`[data-page-panel="${page}"]`)) page = 'workspace';
    const leavingReport = state.page === 'report' && page !== 'report';
    const enteringReport = state.page !== 'report' && page === 'report';
    if (state.page === 'opportunities' && page !== 'opportunities') { state.candidateInspectKey = ''; state.candidateInspectSnapshot = null; }
    if (leavingReport) parkStockChartSurface();
    state.page = page;
    all('[data-page-panel]').forEach(node => node.classList.toggle('active', node.dataset.pagePanel === page));
    all('[data-page]').forEach(node => node.classList.toggle('active', node.dataset.page === page));
    document.querySelector('.app-shell').classList.remove('nav-open');
    if (enteringReport) restoreStockChartSurface();
    if (push) history.replaceState(null, '', routeHash(page,params));
    if (page === 'workspace') loadWorkspace();
    if (page === 'opportunities') ensureWorkspace().then(renderOpportunities);
    if (page === 'model-paper') loadModelPaper();
    if (page === 'accuracy') loadPerformance().then(payload => { renderAccuracy(payload); renderPerformance(payload); renderBacktestPageState(); });
    if (page === 'research') loadResearch();
    if (page === 'system') loadSystem();
  }

  async function ensureWorkspace() {
    if (state.workspace) return state.workspace;
    return loadWorkspace();
  }

  async function loadWorkspace() {
    if (state.workspacePollBusy) return state.workspace;
    state.workspacePollBusy = true;
    const epoch = ++state.workspaceEpoch;
    const delays = [0, 300, 800];
    let lastError = null;
    try {
      for (let attempt = 0; attempt < delays.length; attempt += 1) {
        if (delays[attempt]) await new Promise(resolve => setTimeout(resolve, delays[attempt]));
        if (epoch !== state.workspaceEpoch) return state.workspace;
        try {
          const payload = await api('/api/trader-workspace?mode=all', {timeout:1800});
          if (epoch !== state.workspaceEpoch) return state.workspace;
          state.workspace = payload;
          renderWorkspace(payload);
          if (state.page === 'opportunities') renderOpportunities();
          notice('');
          return payload;
        } catch (error) {
          lastError = error;
        }
      }
      if (!state.workspace) {
        $('marketDecisionRail').innerHTML = '<div class="empty">Workspace projection unavailable. No zero-filled replacement was created.</div>';
        $('deskCards').innerHTML = '<div class="empty">Desk status unavailable.</div>';
        if ($('topEntriesRows')) $('topEntriesRows').innerHTML = emptyRow(8, lastError?.message || 'Workspace unavailable');
        if ($('watchNextRows')) $('watchNextRows').innerHTML = emptyRow(9, 'Research watch unavailable.');
        if ($('workspaceOutcomeRows')) $('workspaceOutcomeRows').innerHTML = emptyRow(8, 'Outcome authority unavailable.');
        notice(`Backend connection unavailable after bounded retries: ${lastError?.message || 'request failed'}.`, 'warning');
        return null;
      }
      const retainedAt = compactTime(state.workspace.as_of);
      state.workspaceStale = true;
      const staleTrust=staleWorkspaceTrust(lastError?.message || 'Workspace refresh unavailable');
      renderTrustStrip(staleTrust);
      renderWorkspacePulse({...state.workspace,trust:staleTrust}, text(state.workspace.market_state || 'UNKNOWN').toLowerCase());
      notice(`Workspace refresh interrupted · retained rows from ${retainedAt} are STALE / NOT ACTIONABLE · automatic retry continues.`, 'warning');
      return state.workspace;
    } finally {
      state.workspacePollBusy = false;
    }
  }

  async function loadLiveTruth() {
    if (state.liveTruthPollBusy) return state.liveTruth;
    state.liveTruthPollBusy=true;
    try {
      const payload=await api('/api/trader-live-state',{timeout:1200});
      state.liveTruth=payload;
      if (state.workspace) {
        const marketState=text(payload.market_state || state.workspace.market_state || 'UNKNOWN').toLowerCase();
        const trust=state.workspaceStale ? staleWorkspaceTrust('Workspace rows are not current') : customerTrust(state.workspace.trust || {});
        renderTrustStrip(trust);
        renderWorkspacePulse({...state.workspace,market_state:payload.market_state || state.workspace.market_state,trust},marketState);
        setStatePill($('globalMarketState'), marketState === 'live' ? 'live' : marketState === 'closed' ? 'closed' : 'unavailable', marketState === 'live' ? 'Market live' : marketState === 'closed' ? 'Market closed' : label(marketState));
      } else {
        renderTrustStrip(customerTrust(payload.trust || {}));
      }
      return payload;
    } catch {
      if (!state.workspace) renderTrustStrip(staleWorkspaceTrust('Live trust refresh unavailable'));
      return state.liveTruth;
    } finally { state.liveTruthPollBusy=false; }
  }

  function scannerProgress(row, desk, fallbackUniverse) {
    row = row || {};
    const analysis = row.analysis || {};
    const coverage = row.coverage || {};
    const contract = row.progress_contract || analysis.progress_contract || {};
    const expected = number(pick(contract, 'population_count')) ?? number(pick(row, 'universe_count','universe_size','total','expected','population_count')) ?? number(pick(analysis,'universe_size')) ?? number(pick(coverage,'universe_size')) ?? number(fallbackUniverse) ?? 0;
    const current = number(pick(contract, 'current_sweep_scanned'));
    const lastFull = number(pick(contract, 'last_completed_sweep_count'));
    const fallbackCurrent = number(pick(row,'scanned','processed','deep_scanned')) ?? number(pick(analysis,'current_sweep_scanned','sweep_scanned','cycle_scanned')) ?? number(pick(coverage,'sweep_attempted','covered'));
    const paused = text(pick(contract,'state') || row.state || row.status).toUpperCase() === 'PAUSED';
    const shown = current ?? (paused && lastFull !== null ? lastFull : fallbackCurrent) ?? 0;
    const promoted = number(pick(row,'promoted','selected','candidates','screening_shortlisted')) ?? number(pick(contract,'screening_shortlisted')) ?? number(pick(coverage,'screening_shortlisted')) ?? 0;
    const status = pick(contract,'state') || row.state || row.status || 'WARMING';
    const tele = row.runtime_telemetry || row.analysis?.runtime_telemetry || {};
    const eligible = number(pick(contract,'eligible_count','screening_eligible')) ?? number(pick(coverage,'screening_eligible')) ?? 0;
    const shortlist = number(pick(contract,'shortlist_count','screening_shortlisted')) ?? number(pick(coverage,'screening_shortlisted')) ?? promoted;
    const deep = number(pick(contract,'deep_analysed')) ?? number(pick(analysis,'cycle_scanned','current_cycle_scanned')) ?? 0;
    const research = number(pick(contract,'research_map_count')) ?? 0;
    const finalCount = number(pick(contract,'final_count')) ?? 0;
    const cps = number(tele.coverage_symbols_per_sec);
    const lastAge = number(contract.last_progress_age_seconds ?? tele.last_progress_age_seconds);
    const retry = number(contract.next_retry_in_seconds ?? tele.next_retry_in_seconds);
    const blocker = text(contract.blocker_reason || tele.blocker_reason);
    return {
      expected, shown: Math.max(0, shown), promoted, status, eligible, shortlist, deep, research, finalCount, cps, lastAge, retry, blocker,
      progress: expected ? Math.max(0, Math.min(100, shown / expected * 100)) : 0,
      detail: pick(contract,'display_detail') || '',
      asOf: pick(contract,'last_progress_at','last_completed_at') || pick(row,'last_progress_at','last_run','updated_at','as_of'),
      paused,
    };
  }

  function activeDeskProjection(payload = state.stock) {
    const desk = state.stockMode === 'intraday' ? 'intraday' : 'delivery';
    const node = payload?.desk_decisions?.[desk];
    if (node) return {desk, decision:node.decision || {}, tradeMap:node.trade_map || {}, node};
    return {desk, decision:payload?.decision || {}, tradeMap:payload?.trade_map || payload?.decision?.trade_map || {}, node:{}};
  }

  function lifecycleState(decision, node = {}) {
    return text(pick(decision,'lifecycle_state','canonical_state','status','decision') || node.research_state || 'NO SETUP').toUpperCase();
  }
  function reassessmentState(decision) {
    const reassessment = decision?.thesis_reassessment || decision?.reassessment || {};
    return text(pick(reassessment,'state','status') || pick(decision,'reassessment_state','thesis_state') || (Object.keys(decision || {}).length ? 'VALID' : '')).toUpperCase();
  }
  function ageLabel(decision) {
    const nested = decision?.signal_age || {};
    const authoritativeSeconds = number(pick(nested,'generation_age_seconds','signal_age_seconds') ?? pick(decision,'signal_age_seconds'));
    const generated = pick(decision,'generated_at','decision_generated_at','created_at','finalized_at') || pick(nested,'generated_at');
    const stamp = Date.parse(text(generated));
    const minutes = authoritativeSeconds !== null ? Math.max(0, Math.floor(authoritativeSeconds / 60)) : Number.isFinite(stamp) ? Math.max(0, Math.floor((Date.now() - stamp) / 60000)) : null;
    if (minutes === null) return '';
    if (minutes < 60) return `Age ${minutes}m`;
    if (minutes < 1440) return `Age ${(minutes / 60).toFixed(minutes < 600 ? 1 : 0)}h`;
    return `Age ${(minutes / 1440).toFixed(minutes < 14400 ? 1 : 0)}d`;
  }
  function validPastTimestamp(value, toleranceMs=5000) {
    const stamp=Date.parse(text(value));
    if (!Number.isFinite(stamp)) return {valid:false, stamp:null, reason:'timestamp unavailable'};
    if (stamp > Date.now() + toleranceMs) return {valid:false, stamp, reason:'future timestamp'};
    return {valid:true, stamp, reason:''};
  }
  function candidateAgeLabel(row) {
    const semantics = row?.time_semantics || {};
    const ageKind = text(semantics.display_age_kind || '').toUpperCase();
    const seconds = number(row?.signal_age_seconds);
    if (ageKind === 'SIGNAL' && seconds !== null) {
      if (seconds < 0) return '—';
      const minutes = Math.floor(seconds / 60);
      if (minutes < 60) return `${minutes}m`;
      if (minutes < 1440) return `${(minutes / 60).toFixed(minutes < 600 ? 1 : 0)}h`;
      return `${(minutes / 1440).toFixed(minutes < 14400 ? 1 : 0)}d`;
    }
    const anchor = ageKind === 'SIGNAL'
      ? pick(semantics,'generated_at') || pick(row,'generated_at','decision_generated_at','created_at')
      : pick(semantics,'first_seen_at') || pick(row,'first_seen_at','occurred_at','observed_at','created_at');
    const temporal=validPastTimestamp(anchor);
    if (!temporal.valid) return ageKind === 'SIGNAL' ? 'Missing signal time' : 'Not timestamped';
    const minutes = Math.floor((Date.now() - temporal.stamp) / 60000);
    if (minutes < 60) return `${minutes}m`;
    if (minutes < 1440) return `${(minutes / 60).toFixed(minutes < 600 ? 1 : 0)}h`;
    return `${(minutes / 1440).toFixed(minutes < 14400 ? 1 : 0)}d`;
  }
  function priceChangeHtml(row) {
    const price = number(pick(row,'display_price','current_price','ltp','last_price'));
    const abs = number(pick(row,'display_change_abs','rupee_change','day_change_abs','point_change'));
    const percent = number(pick(row,'display_change_pct','change_pct','pChange'));
    const tone = percent === null ? '' : percent > 0 ? 'positive-text' : percent < 0 ? 'negative-text' : '';
    const priceLabel = price === null ? 'Unavailable' : money(price);
    const change = abs === null && percent === null ? '' : `${abs === null ? '' : `${abs >= 0 ? '+' : ''}${money(abs)} `}${percent === null ? '' : `(${pct(percent)})`}`.trim();
    return `<div class="price-stack"><b>${esc(priceLabel)}</b><small class="${tone}">${change ? esc(change) : esc(label(row.current_price_state || 'captured snapshot'))}</small></div>`;
  }
  function compactPlanHtml(row) {
    if (text(row.trade_geometry_display_state).toUpperCase() === 'PENDING_LIVE_CONFIRMATION') return '<span class="pending-geometry">Entry/T1/SL pending</span>';
    const entry=displayGeometry(row,'entry'), target=displayGeometry(row,'target'), stop=displayGeometry(row,'stop');
    if ([entry,target,stop].every(value => number(value) === null)) return '<span class="muted">No authorized geometry</span>';
    return `<span class="compact-plan">E ${esc(formatNumber(entry,2))} · T1 ${esc(formatNumber(target,2))} · SL ${esc(formatNumber(stop,2))}</span>`;
  }
  function candidateHitState(row) {
    const raw = text(pick(row,'hit_status','display_result','exit_reason','economic_outcome','outcome','result')).toUpperCase();
    if (/STOP|SL[_ ]?HIT|STOP[_ ]?LOSS/.test(raw)) return {label:'SL HIT', tone:'negative'};
    if (/TARGET|T1[_ ]?HIT|T2[_ ]?HIT|TAKE[_ ]?PROFIT/.test(raw)) return {label:'TARGET HIT', tone:'positive'};
    return {label:'—', tone:'quiet'};
  }
  function candidateNextAction(row) {
    const action = text(pick(row,'display_action','management_action','current_action','decision_action','action')).toUpperCase();
    if (/EXIT|CLOSE/.test(action)) return 'EXIT';
    if (/CONTINUE/.test(action)) return 'CONTINUE';
    if (/HOLD/.test(action)) return 'HOLD';
    return action || (text(row.trade_geometry_display_state).toUpperCase()==='PENDING_LIVE_CONFIRMATION' ? 'VALIDATE' : 'WATCH');
  }
  function candidateOutcomeState(row) {
    const signal = text(pick(row,'signal_outcome','accuracy_state')).toUpperCase();
    if (signal === 'SUCCESS') return {label:'SUCCESS', tone:'positive'};
    if (signal === 'FAILURE') return {label:'FAILURE', tone:'negative'};
    const stage = text(pick(row,'display_stage','lifecycle_state','candidate_stage','opportunity_stage','status')).toUpperCase();
    if (/RECONCILIATION_REQUIRED|RECONCILE/.test(stage) || row?.reconciliation_required === true) return {label:'RECONCILE', tone:'warning'};
    if (/OPEN|OPENED|SIGNAL_OPEN/.test(stage)) return {label:'ACTIVE', tone:'info'};
    if (/FINAL|PROMOTED|ACTIONABLE|CONFIRMED|TRIGGERED/.test(stage)) return {label:'READY', tone:'positive'};
    return {label:'PENDING', tone:'warning'};
  }
  function workspaceFinalSignal(row) {
    const desk = rowMode(row);
    if (!['delivery','intraday'].includes(desk) || row?.display_terminal === true || row?.research_only === true) return false;
    if (!text(row?.final_signal_authority).trim()) return false;
    if (!text(row?.decision_id || row?.signal_id).trim()) return false;
    const generated = pick(row,'generated_at','decision_generated_at','created_at');
    if (!generated || !validPastTimestamp(generated).valid) return false;
    const stage = text(pick(row,'display_stage','lifecycle_state','canonical_state','status')).toUpperCase();
    const finalState = /OPEN|OPENED|SIGNAL_OPEN|FINAL|PROMOTED|ACTIONABLE|CONFIRMED|TRIGGERED|WEAKENING|RECONCILIATION_REQUIRED/.test(stage);
    if (!finalState) return false;
    const entry = positivePrice(displayGeometry(row,'entry'));
    const target = positivePrice(displayGeometry(row,'target'));
    const stop = positivePrice(displayGeometry(row,'stop'));
    const rr = number(displayGeometry(row,'rr')) ?? number(pick(row,'reward_risk','room_rr','intended_rr'));
    const signalAge = number(row?.signal_age_seconds);
    const holding = text(pick(row,'holding_period','target_window','horizon','expected_horizon')).trim();
    return entry !== null && target !== null && stop !== null && rr !== null && rr > 0 && signalAge !== null && signalAge >= 0 && Boolean(holding);
  }
  function workspaceSignalScore(row) {
    return evidenceScoreValue(row) ?? number(pick(row,'rank_normalized_score','rank_score','evidence_score','priority_score','score'));
  }
  function workspaceSignalAge(row) {
    return candidateAgeLabel(row);
  }
  function workspaceHoldingPeriod(row) {
    // Never invent a generic Delivery horizon. Only strategy/canonical declarations qualify.
    const raw = text(pick(row,'holding_period','target_window','horizon','expected_horizon')).trim();
    if (raw) return raw;
    const stateName=text(row?.time_semantics?.holding_period_state).toUpperCase();
    return stateName === 'PENDING_FINAL_ADMISSION' ? 'Set at final admission' : 'Missing holding period';
  }
  function workspacePositionAge(row) {
    const seconds = number(row?.position_age_seconds);
    if (seconds !== null) {
      const minutes = Math.max(0, Math.floor(seconds / 60));
      if (minutes < 60) return `${minutes}m`;
      if (minutes < 1440) return `${(minutes / 60).toFixed(minutes < 600 ? 1 : 0)}h`;
      return `${(minutes / 1440).toFixed(minutes < 14400 ? 1 : 0)}d`;
    }
    const opened = pick(row,'position_opened_at','opened_at');
    if (!opened || !/OPEN|OPENED|SIGNAL_OPEN|RECONCILIATION_REQUIRED/.test(text(pick(row,'display_stage','status')).toUpperCase())) return '—';
    const temporal=validPastTimestamp(opened);
    if (!temporal.valid) return '—';
    const minutes = Math.floor((Date.now() - temporal.stamp) / 60000);
    if (minutes < 60) return `${minutes}m`;
    if (minutes < 1440) return `${(minutes / 60).toFixed(minutes < 600 ? 1 : 0)}h`;
    return `${(minutes / 1440).toFixed(minutes < 14400 ? 1 : 0)}d`;
  }
  function workspaceFinalRows(payload) {
    const admissionBlocked = state.workspaceStale === true || trustBlocksAdmission(payload?.trust || {});
    const pool = rows(payload?.final_signals).filter(workspaceFinalSignal).filter(row => {
      if (!admissionBlocked) return true;
      const stage=text(pick(row,'display_stage','lifecycle_state','canonical_state','status')).toUpperCase();
      // Existing open risk remains visible for monitoring. New READY/FINAL admissions
      // are suppressed whenever browser identity, workspace freshness or runtime trust
      // is not current.
      return /OPEN|OPENED|SIGNAL_OPEN|RECONCILIATION_REQUIRED|RECONCILE/.test(stage);
    });
    const best = new Map();
    for (const row of pool) {
      const symbol = rowSymbol(row), desk = rowMode(row);
      if (!symbol) continue;
      const decisionId = text(row?.decision_id || row?.signal_id).trim();
      if (!decisionId) continue;
      const key = `${desk}|${decisionId}`;
      const stage = text(pick(row,'display_stage','lifecycle_state','status')).toUpperCase();
      const open = /OPEN|OPENED|SIGNAL_OPEN/.test(stage) ? 1 : 0;
      const score = workspaceSignalScore(row) ?? -1;
      const recency = candidateRecency(row);
      const quality = [open, score, recency];
      const prior = best.get(key);
      if (!prior || quality[0] > prior.quality[0] || (quality[0] === prior.quality[0] && (quality[1] > prior.quality[1] || (quality[1] === prior.quality[1] && quality[2] > prior.quality[2])))) best.set(key,{row,quality});
    }
    let ranked = [...best.values()].map(item => item.row);
    if (state.workspaceSignalMode !== 'all') ranked = ranked.filter(row => rowMode(row) === state.workspaceSignalMode);
    ranked.sort((a,b) => {
      const as = workspaceSignalScore(a), bs = workspaceSignalScore(b);
      if (as !== bs) return (bs ?? -1) - (as ?? -1);
      const ao = /OPEN|OPENED|SIGNAL_OPEN/.test(text(pick(a,'display_stage','lifecycle_state','status')).toUpperCase()) ? 1 : 0;
      const bo = /OPEN|OPENED|SIGNAL_OPEN/.test(text(pick(b,'display_stage','lifecycle_state','status')).toUpperCase()) ? 1 : 0;
      if (ao !== bo) return bo - ao;
      return candidateRecency(b) - candidateRecency(a);
    });
    const limit = state.workspaceSignalLimit === 10 ? 10 : 5;
    const visible = ranked.slice(0, limit);
    const visibleKeys = new Set(visible.map(row => `${rowMode(row)}|${text(row?.decision_id || row?.signal_id).trim()}`));
    for (const row of ranked) {
      const stage = text(pick(row,'display_stage','lifecycle_state','status')).toUpperCase();
      if (!/OPEN|OPENED|SIGNAL_OPEN/.test(stage)) continue;
      const key = `${rowMode(row)}|${text(row?.decision_id || row?.signal_id).trim()}`;
      if (!visibleKeys.has(key)) { visible.push(row); visibleKeys.add(key); }
    }
    return {ranked, visible};
  }
  function workspaceSetupLabel(row) {
    const value = text(pick(row,'setup','setup_type','setup_name','pattern','strategy_name','thesis_type')).trim();
    return value ? label(value) : 'Canonical setup';
  }
  function workspaceRewardRisk(row, entry, target, stop) {
    const declared = number(displayGeometry(row,'rr')) ?? number(pick(row,'reward_risk','room_rr','intended_rr'));
    if (declared !== null && declared > 0) return declared;
    // R:R is governed backend trade geometry. The browser may display it but
    // must never synthesize missing trade truth from three independent cells.
    return null;
  }
  function workspaceAfterState(row) {
    const raw = text(pick(row,'after','after_state','follow_through_state','post_exit_state')).trim().toUpperCase();
    if (!raw) return {label:'—', tone:'quiet'};
    if (/CONTINUED/.test(raw)) return {label:'CONTINUED', tone:'positive'};
    if (/RECOVERED/.test(raw)) return {label:'RECOVERED', tone:'warning'};
    if (/REVERSED/.test(raw)) return {label:'REVERSED', tone:'negative'};
    if (/FLAT|NEUTRAL/.test(raw)) return {label:'FLAT', tone:'quiet'};
    return {label:label(raw), tone:'quiet'};
  }
  function workspaceTradeAction(row) {
    const raw=text(pick(row,'final_action','action','decision','side')).toUpperCase();
    if (/EXIT|SELL|CLOSE|REDUCE/.test(raw)) return {label:'EXIT',tone:'negative'};
    if (/BUY|LONG|ENTER|OPEN|HOLD|CONTINUE/.test(raw)) return {label:'BUY',tone:'positive'};
    return {label:raw ? label(raw) : 'FINAL',tone:'info'};
  }
  function workspaceFreshness(row) {
    const raw=text(pick(row,'freshness_state','evidence_freshness_state','quote_freshness_state','price_freshness_state')).toUpperCase();
    if (!raw) return {label:'—',tone:'quiet'};
    if (/LIVE|FRESH|VERIFIED/.test(raw)) return {label:label(raw),tone:'positive'};
    if (/STALE|EXPIRED|INVALID|BLOCK/.test(raw)) return {label:label(raw),tone:'negative'};
    return {label:label(raw),tone:'warning'};
  }
  function workspaceInvalidation(row) {
    return text(pick(row,'invalidation','invalidation_condition','waiting_for','latest_reason','blocker','block_reason')).trim();
  }
  function workspaceAuthority(row) {
    return text(row?.final_signal_authority).trim();
  }
  // Setup win-rate lookup, sourced from /api/learning-health (mode+side outcome
  // attribution). Best-effort only: a fetch failure or missing key leaves the
  // expanded row showing "—" rather than fabricating a number.
  const learningHealthCache = { byKey: new Map(), fetchedAt: 0 };
  async function ensureLearningHealth() {
    if (Date.now() - learningHealthCache.fetchedAt < 120000 && learningHealthCache.byKey.size) return;
    try {
      const payload = await api('/api/learning-health');
      const map = new Map();
      for (const item of rows(payload?.attribution ?? payload?.rows ?? payload)) {
        const mode = text(pick(item,'mode','desk')).toLowerCase();
        const side = text(pick(item,'side','action')).toLowerCase();
        const winRate = number(pick(item,'win_rate','winrate'));
        if (!mode || winRate === null) continue;
        map.set(`${mode}|${side}`, winRate);
      }
      learningHealthCache.byKey = map;
      learningHealthCache.fetchedAt = Date.now();
    } catch (err) { /* best-effort: expanded rows show — until this succeeds */ }
  }
  function workspaceSetupWinRate(row) {
    const mode = rowMode(row);
    const side = text(pick(row,'final_action','action','decision','side')).toLowerCase().includes('sell') ? 'sell' : 'buy';
    const rate = learningHealthCache.byKey.get(`${mode}|${side}`);
    return rate === undefined ? null : rate;
  }
  function renderWorkspaceFinalSignals(payload) {
    const projection = workspaceFinalRows(payload || {});
    const actionablePanel=$('actionablePanel'); if (actionablePanel) { actionablePanel.classList.toggle('is-empty', projection.visible.length===0); actionablePanel.classList.toggle('has-actionable', projection.visible.length>0); }
    const workspaceRoot=document.querySelector('[data-page-panel="workspace"]'); if (workspaceRoot) { workspaceRoot.classList.toggle('no-actionable', projection.visible.length===0); workspaceRoot.classList.toggle('has-actionable', projection.visible.length>0); }
    if ($('finalSignalCount')) $('finalSignalCount').textContent = `${projection.ranked.length} actionable${projection.ranked.length === projection.visible.length ? '' : ` · top ${projection.visible.length}`}`;
    if (!$('topEntriesRows')) return;
    ensureLearningHealth().then(() => { if (state.currentPage === 'workspace') renderWorkspaceFinalSignals.__lastPayload && renderWorkspaceFinalSignals(renderWorkspaceFinalSignals.__lastPayload); }).catch(()=>{});
    renderWorkspaceFinalSignals.__lastPayload = payload;
    $('topEntriesRows').innerHTML = projection.visible.map((row,index) => {
      const symbol=rowSymbol(row), desk=rowMode(row);
      const ltp=positivePrice(pick(row,'display_price','current_price','ltp','last_price'));
      const changeAbs=number(pick(row,'display_change_abs','rupee_change','day_change_abs','point_change'));
      const changePct=number(pick(row,'display_change_pct','change_pct','pChange'));
      const changeSignal=changePct ?? changeAbs;
      const changeTone=changeSignal===null?'':changeSignal>0?'positive-text':changeSignal<0?'negative-text':'';
      const entry=positivePrice(displayGeometry(row,'entry')), target=positivePrice(displayGeometry(row,'target')), stop=positivePrice(displayGeometry(row,'stop'));
      const rr=workspaceRewardRisk(row,entry,target,stop);
      const outcome=candidateOutcomeState(row), hit=candidateHitState(row);
      const stage=text(pick(row,'display_stage','lifecycle_state','canonical_state','status')).toUpperCase() || 'FINAL';
      const pnl=number(pick(row,'net_pnl','mtm_pnl','pnl'));
      const pnlTone=pnl===null?'':pnl>0?'positive-text':pnl<0?'negative-text':'';
      const key=text(row.instrument_key || row.provider_instrument_key);
      const setup=workspaceSetupLabel(row);
      const decisionId=text(row?.decision_id || row?.signal_id);
      const action=workspaceTradeAction(row);
      const quantity=number(pick(row,'quantity','qty','model_quantity','admitted_quantity'));
      const riskAmount=number(pick(row,'risk_amount','risk_rupees','position_risk','risk_budget_rupees','max_loss_rupees'));
      const freshness=workspaceFreshness(row);
      const score=workspaceSignalScore(row);
      const invalidation=workspaceInvalidation(row);
      const authority=workspaceAuthority(row);
      const winRate=workspaceSetupWinRate(row);
      const rowId=`ar-${index}-${esc(symbol)}`;
      const mainRow=`<tr class="candidate-focus-row actionable-row ${esc(action.tone)}" data-open-stock="${esc(symbol)}" data-instrument-key="${esc(key)}" data-mode="${esc(desk)}" data-decision-id="${esc(decisionId)}" data-final-authority="${esc(authority)}" data-expand-target="${rowId}" tabindex="0" aria-label="Open ${esc(symbol)} ${esc(label(desk))} Stock Intelligence">`+
        `<td class="rank-cell"><button type="button" class="row-expand-toggle" data-row-toggle="${rowId}" aria-expanded="false" aria-label="Show details for ${esc(symbol)}">›</button></td>`+
        `<td class="stock-mode-cell"><button type="button" class="stock-link actionable-stock" data-open-stock="${esc(symbol)}" data-instrument-key="${esc(key)}" data-mode="${esc(desk)}" data-decision-id="${esc(decisionId)}"><b>${esc(symbol)}</b><small>${esc(label(desk))} · ${esc(setup)}</small></button></td>`+
        `<td><span class="simple-state ${esc(action.tone)}">${esc(action.label)}</span></td>`+
        `<td class="numeric ltp-cell"><b>${ltp===null?'—':esc(money(ltp))}</b><small class="${changeTone}">${changePct===null?'—':esc(pct(changePct))}</small></td>`+
        `<td class="numeric geometry entry">${entry===null?'—':esc(money(entry))}</td>`+
        `<td class="numeric"><span class="geometry target">${target===null?'—':esc(money(target))}</span> / <span class="geometry stop">${stop===null?'—':esc(money(stop))}</span></td>`+
        `<td class="numeric rr-cell">${rr===null?'—':esc(formatNumber(rr,2))}</td>`+
        `<td><span class="simple-state ${esc(hit.tone==='quiet'?outcome.tone:hit.tone)}">${esc(hit.label==='—'?outcome.label:hit.label)}</span></td></tr>`;
      const detailRow=`<tr class="actionable-detail-row" id="${rowId}" hidden><td colspan="8"><div class="actionable-detail-grid">`+
        `<div><span>Signal score</span><b>${score===null?'—':esc(formatNumber(score,1))}</b></div>`+
        `<div><span>Setup win rate</span><b>${winRate===null?'—':esc(pct(winRate*100))}</b></div>`+
        `<div><span>Quantity</span><b>${quantity===null?'—':esc(formatNumber(quantity,0))}</b></div>`+
        `<div><span>₹ Risk</span><b>${riskAmount===null?'—':esc(money(riskAmount))}</b></div>`+
        `<div><span>Freshness</span><b class="simple-state ${esc(freshness.tone)}">${esc(freshness.label)}</b></div>`+
        `<div><span>Signal age</span><b>${esc(workspaceSignalAge(row))}</b></div>`+
        `<div><span>Holding</span><b>${esc(workspaceHoldingPeriod(row))}</b></div>`+
        `<div><span>Status</span><b>${esc(label(stage))}</b></div>`+
        `<div><span>Net P&amp;L</span><b class="${pnlTone}">${pnl===null?'—':esc(money(pnl))}</b></div>`+
        `<div class="wide"><span>Invalidation / waiting for</span><b>${invalidation?esc(invalidation):'—'}</b></div>`+
        `<div class="wide"><span>Authority</span><b>${authority?esc(label(authority)):'—'}</b></div>`+
        `</div></td></tr>`;
      return mainRow+detailRow;
    }).join('') || (()=>{
      const trustState=text(payload?.trust?.state).toUpperCase();
      const trustReason=text(payload?.trust?.reason).trim();
      const prefix=`No ${state.workspaceSignalMode === 'all' ? '' : `${label(state.workspaceSignalMode)} `}canonical trade is actionable now.`;
      const detail=trustState==='DO_NOT_TRUST' ? ` Admission blocked: ${trustReason || 'runtime trust is not current'}.` : ' Research candidates remain in Watch Next until final admission.';
      return emptyRow(8,`NO TRADE READY DECISIONS — ${prefix}${detail}`);
    })();
  }
  function workspaceCoverage(payload, desk) {
    const explicit = payload?.coverage?.[desk];
    if (explicit && typeof explicit === 'object') {
      const pctValue = number(explicit.pct);
      return {processed:number(explicit.processed), total:number(explicit.total), pct:pctValue, complete:explicit.complete === true, scope:text(explicit.ranking_scope || '')};
    }
    const progress = scannerProgress((payload?.mode_status || {})[desk] || {}, desk, payload?.counts?.universe);
    const pctValue = progress.expected && progress.shown !== null ? Math.max(0, Math.min(100, progress.shown * 100 / progress.expected)) : null;
    return {processed:progress.shown, total:progress.expected, pct:pctValue, complete:Boolean(progress.expected && progress.shown !== null && progress.shown >= progress.expected), scope:'EVALUATED_SUBSET_ONLY'};
  }
  function candidateEvidenceValidity(row) {
    const generated = pick(row,'generated_at','decision_generated_at','created_at','observed_at','last_seen_at');
    if (generated && !validPastTimestamp(generated).valid) return {state:'INCOMPLETE',score:null,reason:'Signal/evidence timestamp is causally invalid'};
    const readiness = text(pick(row,'rank_readiness','evidence_readiness')).toUpperCase();
    const scoring = text(pick(row,'rank_scoring_state','scoring_state')).toUpperCase();
    const freshness = text(pick(row,'feature_freshness','freshness_state','evidence_freshness','price_freshness')).toUpperCase();
    const snapshot = text(pick(row,'feature_snapshot_state','snapshot_state','evidence_snapshot_state')).toUpperCase();
    const missing = rows(row?.rank_missing_inputs).filter(Boolean);
    const gates = rows(row?.rank_gate_failures).filter(Boolean);
    const vetoes = rows(row?.rank_veto_reasons).filter(Boolean);
    const explicitBad = /PARTIAL|INCOMPLETE|MISSING|UNKNOWN|STALE|INVALID|BLOCK/.test(`${readiness} ${scoring} ${freshness} ${snapshot}`) || missing.length > 0 || gates.length > 0 || vetoes.length > 0;
    if (explicitBad) return {state:/STALE/.test(freshness)?'STALE':'INCOMPLETE', score:null, reason:missing[0] || gates[0] || vetoes[0] || 'Required evidence is incomplete or freshness is not proven'};
    const explicitGood = readiness === 'READY' && scoring === 'NORMAL';
    if (!explicitGood) return {state:'INCOMPLETE', score:null, reason:'Canonical evidence completeness is not explicitly proven'};
    return {state:'COMPLETE', score:evidenceScoreValue(row), reason:''};
  }

  function workspaceWatchRows(payload) {
    const finalKeys = new Set(rows(payload?.final_signals).map(row => `${rowMode(row)}|${rowSymbol(row)}`));
    const best = new Map();
    for (const row of [...rows(payload?.preparing), ...rows(payload?.candidates)]) {
      const symbol = rowSymbol(row), desk = rowMode(row);
      if (!symbol || finalKeys.has(`${desk}|${symbol}`) || row?.research_only === false && workspaceFinalSignal(row)) continue;
      // Watch Next is an evidence surface, not a loose symbol list. Rows without
      // an authoritative First Seen timestamp remain in diagnostics/research
      // storage and are not rendered with a misleading dash for Age.
      if (!validPastTimestamp(row?.time_semantics?.first_seen_at || pick(row,'first_seen_at','occurred_at','observed_at','created_at','generated_at')).valid) continue;
      const stage = text(pick(row,'candidate_stage','opportunity_stage','status','decision')).toUpperCase();
      if (/REJECT|FAIL|INVALID|BLOCKED/.test(stage)) continue;
      const key = `${desk}|${symbol}`;
      const quality = [candidateStageWeight(row), evidenceScoreValue(row) ?? -1, candidateRecency(row)];
      const prior = best.get(key);
      if (!prior || quality[0] > prior.quality[0] || (quality[0] === prior.quality[0] && (quality[1] > prior.quality[1] || (quality[1] === prior.quality[1] && quality[2] > prior.quality[2])))) best.set(key,{row,quality});
    }
    return [...best.values()].map(item => item.row).sort((a,b) => {
      const aw=candidateStageWeight(a), bw=candidateStageWeight(b); if (aw!==bw) return bw-aw;
      const as=evidenceScoreValue(a), bs=evidenceScoreValue(b); if (as!==bs) return (bs??-1)-(as??-1);
      return candidateRecency(b)-candidateRecency(a);
    }).slice(0,8);
  }
  function workspaceWaitingFor(row) {
    const blockers = Array.isArray(row?.blockers) ? row.blockers.filter(Boolean) : [];
    const value = text(pick(row,'next_action','waiting_on','trigger_required','block_reason','reason','coverage_message') || blockers[0]).trim();
    if (value) return value.replace(/[_]+/g,' ');
    if (text(row?.trade_geometry_display_state).toUpperCase() === 'PENDING_LIVE_CONFIRMATION') return 'Live price confirmation';
    return 'Final admission evidence';
  }
  function renderWorkspaceWatchNext(payload) {
    const node=$('watchNextRows'); if(!node) return;
    const watch=workspaceWatchRows(payload || {});
    const marketOpen = payload?.market_open === true || text(payload?.market_state).toUpperCase() === 'LIVE';
    const deliveryCoverage=workspaceCoverage(payload,'delivery'), intradayCoverage=workspaceCoverage(payload,'intraday');
    const incompleteDesks=new Set(watch.map(row=>rowMode(row)).filter(desk=>!workspaceCoverage(payload,desk).complete));
    const provisional=incompleteDesks.size>0;
    if ($('watchNextMeta')) {
      const bits=[];
      for (const [desk,cov] of [['Delivery',deliveryCoverage],['Intraday',intradayCoverage]]) if (cov.pct!==null) bits.push(`${desk} ${formatNumber(cov.pct,1)}%`);
      $('watchNextMeta').textContent = `${provisional?'PROVISIONAL · evaluated subset':'FULL SWEEP'}${bits.length?` · ${bits.join(' · ')}`:''}`;
      $('watchNextMeta').className=`watch-next-meta ${provisional?'warning':'positive'}`;
    }
    node.innerHTML=watch.map((row,index) => {
      const symbol=rowSymbol(row), desk=rowMode(row), evidence=candidateEvidenceValidity(row);
      const ltp=positivePrice(pick(row,'display_price','current_price','captured_price','ltp','last_price'));
      const rawStage=text(pick(row,'candidate_stage','opportunity_stage','status','decision') || 'WATCH');
      const stage=researchStageLabel(rawStage,{geometryComplete:false,marketOpen,evidenceState:evidence.state}), tone=researchStageTone(stage);
      let waiting=workspaceWaitingFor(row);
      if (!marketOpen && desk==='intraday' && /live|quote|confirm/i.test(waiting)) waiting='Next session live confirmation';
      const rank = workspaceCoverage(payload,desk).complete ? String(index+1) : '•';
      return `<tr><td title="${workspaceCoverage(payload,desk).complete?'Full-universe rank':'Provisional order within evaluated subset'}">${rank}</td><td><button type="button" class="stock-link compact-stock-link" data-open-stock="${esc(symbol)}" data-mode="${esc(desk)}" data-instrument-key="${esc(text(row.instrument_key || row.provider_instrument_key))}">${esc(symbol)}</button></td>`+
        `<td><span class="mode-chip ${esc(desk)}">${esc(label(desk))}</span></td><td class="setup-cell">${esc(workspaceSetupLabel(row))}</td>`+
        `<td title="${esc(evidence.reason)}">${evidence.score===null?'—':esc(formatNumber(evidence.score,1))}</td><td>${ltp===null?'—':esc(money(ltp))}</td>`+
        `<td class="waiting-cell" title="${esc(waiting)}">${esc(waiting)}</td><td>${esc(candidateAgeLabel(row))}</td>`+
        `<td><span class="simple-state ${esc(tone.replace('semantic-',''))}">${esc(stage)}</span></td></tr>`;
    }).join('') || emptyRow(9,'No qualified research candidate is currently close enough to final admission.');
  }
  function workspaceResultLabel(row) {
    const raw=text(pick(row,'exit_reason','result','display_result')).toUpperCase();
    if (/TARGET/.test(raw)) return {label:'TARGET HIT',tone:'positive'};
    if (/SL_HIT|STOP_HIT|STOP LOSS|STOP_LOSS/.test(raw)) return {label:'SL HIT',tone:'negative'};
    if (/TIME/.test(raw)) return {label:'TIME EXIT',tone:'warning'};
    if (/FORCED/.test(raw)) return {label:'FORCED EXIT',tone:'warning'};
    return {label:raw ? label(raw) : 'CLOSED',tone:'quiet'};
  }
  function workspaceOutcomeLabel(row) {
    const raw=text(pick(row,'signal_outcome','accuracy_state','economic_outcome')).toUpperCase();
    if (/SUCCESS|WIN|PROFIT/.test(raw)) return {label:'SUCCESS',tone:'positive'};
    if (/FAILURE|FAIL|LOSS/.test(raw)) return {label:'FAILURE',tone:'negative'};
    if (/NEUTRAL/.test(raw)) return {label:'NEUTRAL',tone:'warning'};
    return {label:raw ? label(raw) : '—',tone:'quiet'};
  }
  function renderWorkspaceRecentOutcomes() {
    const node=$('workspaceOutcomeRows'); if(!node) return;
    const payload=state.performance || {};
    const lifecycle=payload.canonical_lifecycle || payload.performance_evidence?.signal_accuracy || {};
    const records=rows(lifecycle.records).filter(row => row?.accuracy_eligible === true || row?.performance_eligible === true).sort((a,b)=>Date.parse(text(b.closed_at||b.settled_at||b.updated_at))-Date.parse(text(a.closed_at||a.settled_at||a.updated_at))).slice(0,6);
    const outcomesPanel=$('recentOutcomesPanel'), supportGrid=$('workspaceSupportGrid');
    if (outcomesPanel) outcomesPanel.hidden = records.length===0;
    if (supportGrid) supportGrid.classList.toggle('outcomes-empty', records.length===0);
    node.innerHTML=records.map(row => {
      const result=workspaceResultLabel(row), outcome=workspaceOutcomeLabel(row), after=workspaceAfterState(row);
      const pnl=number(row.net_pnl), r=number(row.realized_r), held=number(row.holding_minutes);
      const heldText=held===null?'—':held<60?`${formatNumber(held,0)}m`:held<1440?`${formatNumber(held/60,1)}h`:`${formatNumber(held/1440,1)}d`;
      return `<tr><td>${settlementSymbolCell(row)}</td><td><span class="mode-chip ${esc(rowMode(row))}">${esc(label(rowMode(row)))}</span></td>`+
        `<td><span class="simple-state ${esc(result.tone)}">${esc(result.label)}</span></td><td><span class="simple-state ${esc(outcome.tone)}">${esc(outcome.label)}</span></td>`+
        `<td class="${pnl===null?'':pnl>0?'positive-text':pnl<0?'negative-text':''}">${pnl===null?'—':esc(money(pnl))}</td>`+
        `<td>${r===null?'—':esc(formatNumber(r,2))}</td><td>${esc(heldText)}</td><td><span class="simple-state ${esc(after.tone)}">${esc(after.label)}</span></td></tr>`;
    }).join('') || emptyRow(8, state.performance?.ok === false ? 'Outcome authority unavailable.' : 'No settled canonical trade is available yet.');
  }
  function renderWorkspaceSummary(payload) {
    const counts = payload.counts || {};
    const market = label(payload.market_state || 'Unknown');
    const marketTone = text(payload.market_state).toUpperCase()==='CLOSED'?'closed':'info';
    const cards = [
      `<span class="workspace-inline-stat ${marketTone}"><small>Market</small><b>${esc(market)}</b></span>`,
      `<span class="workspace-inline-stat ${number(counts.active)>0?'positive':'quiet'}"><small>Active decisions</small><b>${animatedNumberHtml('workspace:active',counts.active,{digits:0})}</b></span>`,
      `<span class="workspace-inline-stat ${number(counts.candidates)>0?'info':'quiet'}"><small>Prepared</small><b>${animatedNumberHtml('workspace:prepared',counts.candidates,{digits:0})}</b></span>`,
      `<span class="workspace-inline-stat info"><small>Universe</small><b>${number(counts.universe)===null?'Warming':animatedNumberHtml('workspace:universe',counts.universe,{digits:0})}</b></span>`,
    ];
    $('workspaceStats').innerHTML = cards.join('');
  }

  function marketIdentityTokens(row) {
    return [pick(row,'display_name'), pick(row,'name'), pick(row,'trading_symbol'), pick(row,'symbol')]
      .filter(Boolean)
      .map(value => text(value).toUpperCase().replace(/[^A-Z0-9]/g,''));
  }
  function marketRowByAlias(marketRows, ...aliases) {
    const wanted = new Set(aliases.filter(Boolean).map(value => text(value).toUpperCase().replace(/[^A-Z0-9]/g,'')));
    return marketRows.find(row => marketIdentityTokens(row).some(token => wanted.has(token))) || null;
  }
  function marketMoveClass(change) {
    const value = number(change);
    if (value === null || Math.abs(value) < 0.001) return 'neutral move-0';
    const intensity = Math.abs(value) >= 1.5 ? 3 : Math.abs(value) >= 0.65 ? 2 : 1;
    return `${value > 0 ? 'positive' : 'negative'} move-${intensity}`;
  }
  function marketCompactCell(row, shortName, {showPrice=false} = {}) {
    if (!row) return `<span class="market-map-cell unavailable"><b>${esc(shortName)}</b><strong>—</strong></span>`;
    const canonical = text(pick(row,'display_name','name','trading_symbol','symbol') || shortName).toUpperCase();
    const price = number(pick(row,'ltp','close'));
    const change = number(pick(row,'change_pct','pChange'));
    const arrow = change === null ? '•' : change > 0 ? '▲' : change < 0 ? '▼' : '•';
    const value = showPrice
      ? `${price === null ? '—' : formatNumber(price,2)} ${arrow}${change === null ? '—' : pct(change)}`
      : `${arrow}${change === null ? '—' : pct(change)}`;
    return `<span class="market-map-cell ${marketMoveClass(change)}" title="${esc(canonical)}"><b>${esc(shortName)}</b><strong>${esc(value)}</strong></span>`;
  }
  function marketMoverCell(row, side) {
    if (!row) return `<td class="market-mover-empty">—</td><td>—</td>`;
    const symbol = rowSymbol(row);
    const change = number(pick(row,'change_pct','pChange'));
    const tone = change === null ? '' : change > 0 ? 'positive-text' : change < 0 ? 'negative-text' : '';
    const key = text(row.instrument_key || row.provider_instrument_key);
    const stock = symbol && symbol !== '—'
      ? `<button type="button" class="stock-link compact-stock-link" data-open-stock="${esc(symbol)}" data-mode="${esc(rowMode(row))}" data-instrument-key="${esc(key)}">${esc(symbol)}</button>`
      : '—';
    return `<td>${stock}</td><td class="${tone}">${change === null ? '—' : esc(pct(change))}</td>`;
  }
  function renderMarketContextRail(payload, marketState) {
    const root=$('marketDecisionRail'); if(!root) return;
    const marketRows=rows(payload.indices).filter(row => pick(row,'ltp','close') !== null || pick(row,'change_pct','pChange') !== null);
    const nifty=marketRowByAlias(marketRows,'NIFTY','NIFTY 50','NIFTY50');
    const sensex=marketRowByAlias(marketRows,'SENSEX');
    const bank=marketRowByAlias(marketRows,'BANK','NIFTY BANK','BANKNIFTY');
    const vix=marketRowByAlias(marketRows,'VIX','INDIA VIX');
    const movers=payload.market_movers || {};
    let advances=number(movers.advances), declines=number(movers.declines);
    if (advances===null || declines===null) {
      const breadth=marketRows.find(row => number(row.advances)!==null && number(row.declines)!==null);
      advances=number(breadth?.advances); declines=number(breadth?.declines);
    }
    const ad=advances!==null && declines!==null && declines>0 ? advances/declines : null;
    const g=rows(movers.top_gainers)[0], l=rows(movers.top_losers)[0];
    const mover=(row,tone) => row ? `<span class="simple-mover"><small>${tone==='positive'?'Leader':'Laggard'}</small><button type="button" class="stock-link compact-stock-link" data-open-stock="${esc(rowSymbol(row))}" data-mode="${esc(rowMode(row))}" data-instrument-key="${esc(text(row.instrument_key||row.provider_instrument_key))}">${esc(rowSymbol(row))}</button><b class="${tone}-text">${number(pick(row,'change_pct','pChange'))===null?'—':esc(pct(number(pick(row,'change_pct','pChange'))))}</b></span>` : '';
    root.innerHTML=`<div class="simple-market-row"><span class="market-map-state ${marketState==='live'?'live':'closed'}">${marketState==='live'?'LIVE':'CLOSED'}</span>${marketCompactCell(nifty,'NIFTY 50',{showPrice:true})}${marketCompactCell(sensex,'SENSEX',{showPrice:true})}${marketCompactCell(bank,'BANK',{showPrice:true})}${marketCompactCell(vix,'VIX',{showPrice:true})}<span class="simple-breadth"><small>Breadth</small><b>${advances===null||declines===null?'—':`${formatNumber(advances,0)} / ${formatNumber(declines,0)}`}</b><em>A/D ${ad===null?'—':formatNumber(ad,2)}</em></span>${mover(g,'positive')}${mover(l,'negative')}</div>`;
  }
  function renderPriceChangeStrip(payload, quote) {
    const perf = payload?.period_returns || payload?.price_performance || {};
    const ltp = number(pick(quote,'ltp','last_price','close'));
    const previous = number(pick(quote,'previous_close','prev_close'));
    const oneDay = number(pick(quote,'change_pct','pChange')) ?? (ltp !== null && previous ? (ltp / previous - 1) * 100 : null);
    const values = [['1D',oneDay],['1W',perf['1w']],['2W',perf['2w']],['1M',perf['1m']],['3M',perf['3m']],['6M',perf['6m']],['1Y',perf['1y']],['2Y',perf['2y']],['3Y',perf['3y']],['5Y',perf['5y']]];
    $('priceChangeStrip').innerHTML = values.map(([key,value]) => {
      const n=number(value), tone=n===null?'':n>0?'positive-text':n<0?'negative-text':'';
      return `<span class="period-chip"><small>${key}</small><b class="${tone}">${n===null?'—':esc(pct(n))}</b></span>`;
    }).join('');
  }
  function renderDeskDecisions(payload) {
    const desks = payload?.desk_decisions || {};
    $('deskDecisionStrip').innerHTML = ['delivery','intraday'].map(desk => {
      const node = desks[desk] || (state.stockMode === desk ? {decision:payload?.decision || {},trade_map:payload?.trade_map || {}} : {});
      const decision = node.decision || {};
      const map = node.trade_map || {};
      const lifecycle = lifecycleState(decision,node);
      const reassessment = reassessmentState(decision);
      const age = ageLabel(decision);
      const selected = state.stockMode === desk ? ' selected' : '';
      const tone = /FINAL|OPEN|ACTIONABLE/.test(lifecycle) ? ' ready' : /RESEARCH|WATCH|DEVELOP/.test(lifecycle) ? ' warming' : /INVALID/.test(reassessment) ? ' invalid' : '';
      const entry=number(pick(map,'entry')??pick(decision,'entry')); const target=number(pick(map,'target_1','target')??pick(decision,'target','t1')); const stop=number(pick(map,'stop')??pick(decision,'stop','sl')); const rr=number(pick(map,'room_rr','rr')??pick(decision,'reward_risk','rr'));
      const geometry = [entry,target,stop].every(value => value !== null && value > 0);
      const truth = payload?.selected_stock_truth || {};
      const selectedTruth = state.stockMode === desk ? truth : {};
      const headline = Object.keys(decision).length ? `${label(lifecycle)}${reassessment ? ` · ${label(reassessment)}` : ''}${age ? ` · ${age}` : ''}` : (Object.keys(selectedTruth).length ? `No admitted setup · ${label(selectedTruth.decision_status || selectedTruth.data_status || 'waiting')}` : 'No admitted setup');
      const detail = geometry ? `Entry ${formatNumber(entry,2)} · T1 ${formatNumber(target,2)} · SL ${formatNumber(stop,2)}${rr!==null&&rr>0?` · R ${formatNumber(rr,2)}`:''}` : esc(map.block_reason || decision.reason || selectedTruth.reason || selectedTruth.coverage_message || (/FINAL|OPEN/.test(lifecycle) ? 'Authorized geometry unavailable' : 'Waiting for valid setup / trigger'));
      return `<button type="button" class="desk-decision-card${selected}${tone}" data-stock-desk="${desk}"><span>${label(desk)}</span><b>${esc(headline)}</b><small>${detail}</small></button>`;
    }).join('');
  }

  function renderWorkspacePulse(payload, marketState) {
    const root=$('workspacePulse');
    const live=$('workspaceLiveState');
    const trust=payload?.trust || {};
    const trustState=text(trust.state || 'UNKNOWN').toUpperCase();
    const admission=trust.decision_admission_allowed === true && state.workspaceStale !== true;
    const marketRows=rows(payload?.indices);
    const nifty=marketRowByAlias(marketRows,'NIFTY','NIFTY 50','NIFTY50');
    const vix=marketRowByAlias(marketRows,'VIX','INDIA VIX');
    const movers=payload?.market_movers || {};
    let advances=number(movers.advances), declines=number(movers.declines);
    if (advances===null || declines===null) {
      const breadth=marketRows.find(row => number(row.advances)!==null && number(row.declines)!==null);
      advances=number(breadth?.advances); declines=number(breadth?.declines);
    }
    const niftyPrice=number(pick(nifty||{},'ltp','close'));
    const niftyChange=number(pick(nifty||{},'change_pct','pChange'));
    const vixLevel=number(pick(vix||{},'ltp','close'));
    const vixChange=number(pick(vix||{},'change_pct','pChange'));
    const changeTone=value => value===null?'quiet':value>0?'positive':value<0?'negative':'quiet';
    const systemLabel=admission?'READY':/DO_NOT_TRUST|BLOCK|FAIL|ERROR/.test(trustState)?'BLOCKED':label(trustState);
    const systemTone=admission?'positive':/DO_NOT_TRUST|BLOCK|FAIL|ERROR/.test(trustState)?'negative':'warning';
    if (root) root.innerHTML=`<span class="tape-item ${changeTone(niftyChange)}"><small>NIFTY 50</small><b>${niftyPrice===null?'—':esc(formatNumber(niftyPrice,2))}</b><em>${niftyChange===null?'—':esc(pct(niftyChange))}</em></span>`+
      `<span class="tape-item ${advances===null||declines===null?'quiet':advances>declines?'positive':declines>advances?'negative':'warning'}"><small>BREADTH</small><b>${advances===null||declines===null?'—':`${formatNumber(advances,0)} / ${formatNumber(declines,0)}`}</b><em>Adv / Dec</em></span>`+
      `<span class="tape-item ${changeTone(vixChange)}"><small>INDIA VIX</small><b>${vixLevel===null?'—':esc(formatNumber(vixLevel,2))}</b><em>${vixChange===null?'—':esc(pct(vixChange))}</em></span>`+
      `<span class="tape-item ${systemTone}"><small>SYSTEM</small><b>${esc(systemLabel)}</b><em>${marketState==='closed'?'Next-session preparation only':admission?'Decision admission enabled':esc(text(trust.reason || trust.reasons?.[0] || 'Validation pending'))}</em></span>`;
    if (live) {
      const closed=marketState==='closed';
      const cls=closed?'closed':admission?'live':'blocked';
      const headline=closed?'MARKET CLOSED':admission?'LIVE':'NOT ACTIONABLE';
      const detail=closed?'Verified close':admission?'Governed evidence':'Fail-closed';
      live.className=`live-truth-badge ${cls}`;
      live.innerHTML=`<i></i><b>${headline}</b><small>${detail}</small>`;
    }
    if ($('workspaceSystemNote')) $('workspaceSystemNote').textContent=admission?'Runtime trust allows governed decision admission.':text(trust.reason || trust.reasons?.[0] || 'Runtime trust is not ready; no actionable fallback is created.');
  }
  function r7ResearchTrackedCount(payload = state.workspace) { const persisted=number(state.modelPaper?.counts?.research); if(persisted!==null)return Math.max(0,persisted); return rows(payload?.candidates).filter(row=>row?.research_only!==false || /RESEARCH|WATCH|PREPARED|RERANK/.test(text(pick(row,'display_stage','candidate_stage','status')).toUpperCase())).length; }
  function r7EquityRecords(){ return settledLifecycleRows({performanceOnly:true}).filter(row=>number(row.net_pnl)!==null).sort((a,b)=>Date.parse(text(a.closed_at||a.settled_at||a.updated_at))-Date.parse(text(b.closed_at||b.settled_at||b.updated_at))).slice(-60); }
  function renderR7PerformanceCockpit(payload=state.workspace||{}){
    if(!$('r7PerformanceCockpit'))return; const proj=workspaceFinalRows(payload||{}); $('r7ActionableStat').textContent=formatNumber(proj.ranked.length,0); const rc=r7ResearchTrackedCount(payload); $('r7ResearchStat').textContent=formatNumber(rc,0); $('r7ResearchSub').textContent=state.modelPaper?.counts?.research!==undefined?'persisted Research history':'current Research candidates'; const metric=accuracyMetrics(); const acc=number(metric?.accuracy_pct); $('r7AccuracyStat').textContent=acc===null?'—':`${formatNumber(acc,1)}%`; $('r7AccuracySub').textContent=acc===null?'no eligible settled sample':`${formatNumber(number(pick(metric,'accuracy_denominator','accuracy_eligible','scored_trades'))||0,0)} settled scored`; const econ=state.performance?.model_paper_performance||state.performance?.performance_evidence?.model_paper_performance||{}; const all=econ.filters?.all?.all||null; const net=number(all?.net_pnl); $('r7PnlStat').textContent=net===null?'—':money(net); $('r7PnlStat').className=net===null?'':net>0?'positive-text':net<0?'negative-text':''; const rec=r7EquityRecords(),panel=$('r7EquityPanel'),line=$('r7EquityLine'),area=$('r7EquityArea'),dots=$('r7EquityDots'),empty=$('r7GraphEmpty'); let cum=0; const vals=rec.map(row=>{cum+=number(row.net_pnl)||0;return{v:cum,t:row.closed_at||row.settled_at||row.updated_at}}); const fv=vals.length?vals.at(-1).v:net; $('r7EquityValue').textContent=fv===null?'—':money(fv); $('r7EquityValue').className=fv===null?'':fv>0?'positive-text':fv<0?'negative-text':''; panel.dataset.trend=fv===null||fv===0?'neutral':fv>0?'positive':'negative'; if(!vals.length){line.setAttribute('d','');area.setAttribute('d','');dots.innerHTML='';empty.hidden=false;$('r7SettledRange').textContent='Research performance is measured separately';return} empty.hidden=true; const W=760,H=146,pad=8,raw=vals.map(x=>x.v),mn=Math.min(0,...raw),mx=Math.max(0,...raw),span=Math.max(1,mx-mn); const pts=vals.map((x,i)=>({x:vals.length===1?W/2:pad+i*(W-2*pad)/(vals.length-1),y:pad+(mx-x.v)*(H-2*pad)/span,...x})); const d=pts.map((p,i)=>`${i?'L':'M'}${p.x.toFixed(2)},${p.y.toFixed(2)}`).join(' '),zy=pad+(mx)*(H-2*pad)/span; line.setAttribute('d',d);area.setAttribute('d',`${d} L${pts.at(-1).x.toFixed(2)},${zy.toFixed(2)} L${pts[0].x.toFixed(2)},${zy.toFixed(2)} Z`);dots.innerHTML=pts.filter((p,i)=>i===pts.length-1||(pts.length<=12&&i%Math.max(1,Math.floor(pts.length/6))===0)).map((p,i,a)=>`<circle class="r7-graph-dot${i===a.length-1?' end':''}" cx="${p.x.toFixed(2)}" cy="${p.y.toFixed(2)}" r="3.2"></circle>`).join(''); $('r7SettledRange').textContent=`${vals.length} settled · Research performance separate`; }

  function scheduleWorkspaceOutcomeRefresh() {
    if (state.workspaceOutcomeBusy || Date.now()-state.workspaceOutcomeRefreshAt < 15000) { renderWorkspaceRecentOutcomes(); return; }
    state.workspaceOutcomeBusy=true; state.workspaceOutcomeRefreshAt=Date.now();
    loadPerformance(true).then(()=>{renderWorkspaceRecentOutcomes();renderR7PerformanceCockpit(state.workspace);}).finally(()=>{state.workspaceOutcomeBusy=false;});
  }

  function renderWorkspace(payload) {
    state.workspaceStale = false;
    state.lastWorkspaceSuccessAt = Date.now();
    const effective = {...payload, trust:customerTrust(payload.trust || {})};
    state.workspace = effective;
    renderTrustStrip(effective.trust || {});
    const marketState = text(effective.market_state || 'UNKNOWN').toLowerCase();
    setStatePill($('globalMarketState'), marketState === 'live' ? 'live' : marketState === 'closed' ? 'closed' : 'unavailable', marketState === 'live' ? 'Market live' : marketState === 'closed' ? 'Market closed' : label(marketState));
    if ($('workspaceAsOf')) $('workspaceAsOf').textContent = `${compactTime(effective.as_of)} · ${formatNumber(effective.route_elapsed_ms, 1)}ms`;
    renderWorkspaceSummary(effective);
    renderWorkspacePulse(effective, marketState);
    renderWorkspaceFinalSignals(effective);
    renderWorkspaceWatchNext(effective);
    renderMarketContextRail(effective, marketState);
    renderR7PerformanceCockpit(effective);
    scheduleWorkspaceOutcomeRefresh();
    if (!state.modelPaper) loadModelPaper(false).then(()=>renderR7PerformanceCockpit(state.workspace));
    activateNumberAnimations(document.querySelector('[data-page-panel="workspace"]') || document);
  }


  function renderOpportunities() {
    const payload = state.workspace || {};
    const deskRow = (payload.mode_status || {})[state.desk] || {};
    const candidates = dedupeDeskCandidates(payload.candidates, state.desk).sort((a,b) => {
      const aw=candidateStageWeight(a), bw=candidateStageWeight(b);
      if (aw !== bw) return bw-aw;
      const as=evidenceScoreValue(a), bs=evidenceScoreValue(b);
      if (as !== bs) return (bs ?? -1) - (as ?? -1);
      return candidateRecency(b) - candidateRecency(a);
    });
    state.opportunityCandidates = candidates;
    const progress = scannerProgress(deskRow, state.desk, payload.counts?.universe);
    const scanned = progress.shown;
    const expected = progress.expected;
    const promoted = progress.promoted;
    const analysis = deskRow.analysis || {};
    const lastCompleted = analysis.last_completed || deskRow.last_completed || {};
    const rejectedCount = number(pick(analysis,'cycle_rejected')) ?? number(pick(lastCompleted,'rejected')) ?? number(pick(deskRow,'rejected')) ?? 0;
    const gatedCount = number(pick(analysis,'cycle_blocked')) ?? number(pick(lastCompleted,'blocked')) ?? number(pick(deskRow,'blocked')) ?? 0;
    const rejectedGated = Math.max(0, rejectedCount + gatedCount);
    const scanPct = scanned !== null && expected ? Math.max(0, Math.min(100, scanned * 100 / expected)) : null;
    const partialScan = scanPct !== null && scanPct < 99.999;
    $('scanRunState').textContent = `${label(state.desk)} · ${partialScan ? `PARTIAL ${formatNumber(scanPct,1)}%` : label(progress.status)}`;
    $('scanRunMeta').textContent = `${formatNumber(scanned,0)} / ${expected ? formatNumber(expected,0) : 'universe warming'} · ${partialScan ? 'ranking provisional until sweep completes · ' : ''}${progress.asOf ? compactTime(progress.asOf) : compactTime(payload.as_of)}`;
    renderStats($('scannerStats'), [
      {label:'Universe', value:expected === null ? 'Warming' : formatNumber(expected,0)},
      {label:'Processed', value:scanned === null ? 'Unavailable' : formatNumber(scanned,0)},
      {label:'Published research', value:formatNumber(candidates.length,0), tone:'positive'},
      {label:'Current scan promoted', value:promoted === null ? 'Unavailable' : formatNumber(promoted,0), tone:'positive'},
      {label:'Rejected / gated', value:formatNumber(rejectedGated,0), tone:'warning'},
    ]);
    renderGatedDetails(deskRow);
    $('candidateRows').innerHTML = candidates.map((row, index) => {
      const symbol = rowSymbol(row);
      const geometryPending = text(row.trade_geometry_display_state).toUpperCase() === 'PENDING_LIVE_CONFIRMATION';
      const positiveGeometry = value => { const n = number(value); return n !== null && n > 0 ? n : null; };
      const entry = positiveGeometry(displayGeometry(row,'entry'));
      const target = positiveGeometry(displayGeometry(row,'target'));
      const stop = positiveGeometry(displayGeometry(row,'stop'));
      const rr = positiveGeometry(displayGeometry(row,'rr'));
      const evidenceScore = evidenceScoreValue(row);
      const priceSort = number(pick(row,'display_price','current_price','captured_price','ltp'));
      const candidateState = text(pick(row,'candidate_stage','opportunity_stage','status','decision') || 'WATCH').toUpperCase();
      const geometryComplete = !geometryPending && [entry,target,stop].every(value => value !== null && value > 0);
      const candidateDisplayState = researchStageLabel(candidateState, {geometryComplete});
      const candidateTone = researchStageTone(candidateDisplayState);
      return `<tr class="${candidateTone}"><td data-sort-value="${index + 1}">${index + 1}</td><td data-sort-value="${esc(symbol)}"><button type="button" class="stock-link" data-open-stock="${esc(symbol)}" data-instrument-key="${esc(text(row.instrument_key || row.provider_instrument_key))}" data-mode="${esc(rowMode(row))}">${esc(symbol)}</button></td><td>${esc(pick(row,'setup','setup_type','reason') || 'Evidence watch')}</td><td><span class="row-state ${candidateTone}">${esc(candidateDisplayState)}</span></td><td data-sort-value="${priceSort === null ? '' : priceSort}">${currentCapturedPriceHtml(row)}</td><td data-sort-value="${entry ?? ''}">${geometryPending || entry===null ? '<span class="pending-geometry">Pending</span>' : esc(formatNumber(entry,2))}</td><td data-sort-value="${target ?? ''}">${geometryPending || target===null ? '<span class="pending-geometry">Pending</span>' : esc(formatNumber(target,2))}</td><td data-sort-value="${stop ?? ''}">${geometryPending || stop===null ? '<span class="pending-geometry">Pending</span>' : esc(formatNumber(stop,2))}</td><td data-sort-value="${rr ?? ''}">${geometryPending || rr===null ? '<span class="pending-geometry">Pending</span>' : esc(formatNumber(rr,2))}</td><td data-sort-value="${evidenceScore ?? ''}">${evidenceScore === null ? '—' : esc(formatNumber(evidenceScore,1))}</td><td><button type="button" class="table-action why-action" data-inspect-candidate-key="${esc(candidateStableKey(row))}" aria-expanded="false">Why</button></td></tr>`;
    }).join('') || emptyRow(11, `No ${state.desk} candidate is currently published. This is a genuine no-result state, not a zero-filled shortlist.`);
    applyStoredSort($('candidateRows'));
    const inspectPanel = $('candidateInspectPanel');
    if (inspectPanel) {
      const selectedIndex = state.candidateInspectKey ? candidates.findIndex(row => candidateStableKey(row) === state.candidateInspectKey) : -1;
      if (selectedIndex >= 0) {
        const selected = candidates[selectedIndex];
        state.candidateInspectSnapshot = selected;
        inspectPanel.innerHTML = candidateExplanationHtml(selected, selectedIndex + 1);
        inspectPanel.hidden = false;
        all('[data-inspect-candidate-key]').forEach(node => node.setAttribute('aria-expanded', node.dataset.inspectCandidateKey === state.candidateInspectKey ? 'true' : 'false'));
      } else if (state.candidateInspectKey && state.candidateInspectSnapshot && rowMode(state.candidateInspectSnapshot) === state.desk) {
        inspectPanel.innerHTML = `<div class="candidate-inspect-stale"><b>Candidate is no longer in the latest projection.</b><span>The last explanation remains visible until you close it or select another candidate.</span></div>${candidateExplanationHtml(state.candidateInspectSnapshot, null)}`;
        inspectPanel.hidden = false;
        all('[data-inspect-candidate-key]').forEach(node => node.setAttribute('aria-expanded', 'false'));
      } else {
        inspectPanel.hidden = true; inspectPanel.innerHTML = '';
      }
    }
  }

  async function runScan() {
    const button = $('runScan');
    button.disabled = true;
    button.textContent = 'Requesting…';
    try {
      const path = state.desk === 'intraday' ? '/api/refresh' : '/api/deep-scan';
      const payload = await api(path, {method:'POST', body:{}, timeout:2500});
      toast(payload.message || `${label(state.desk)} scan requested`);
      setTimeout(() => loadWorkspace(), 1000);
    } catch (error) {
      toast(`Scan request failed: ${error.message}`);
    } finally {
      button.disabled = false;
      button.textContent = 'Start authorized scan';
    }
  }

  function chartTime(row) {
    const raw = pick(row, 'timestamp', 'time', 'date', 'bar_start_ts');
    if (typeof raw === 'number') return raw > 1e12 ? Math.floor(raw / 1000) : raw;
    const parsed = Date.parse(text(raw));
    return Number.isFinite(parsed) ? Math.floor(parsed / 1000) : null;
  }
  function normaliseCandles(payload) {
    const map = new Map();
    for (const raw of rows(payload?.candles || payload?.rows || payload?.bars)) {
      const item = {time:chartTime(raw), open:number(raw.open), high:number(raw.high), low:number(raw.low), close:number(raw.close), volume:number(raw.volume), raw};
      if (item.time && [item.open,item.high,item.low,item.close].every(Number.isFinite)) map.set(item.time, item);
    }
    return [...map.values()].sort((a,b) => a.time - b.time);
  }
  function colorAlpha(hex, alpha=.5) {
    const raw = text(hex).trim();
    const match = /^#([0-9a-f]{6})$/i.exec(raw);
    if (!match) return raw;
    const value = Number.parseInt(match[1], 16);
    return `rgba(${(value>>16)&255},${(value>>8)&255},${value&255},${alpha})`;
  }
  function chartBaseOptions({interactive=true} = {}) {
    const p = chartPalette();
    return {
      autoSize:true,
      layout:{background:{color:p.bg}, textColor:p.text},
      localization:{timeFormatter:chartTimeFormatter},
      grid:{vertLines:{color:p.grid},horzLines:{color:p.grid}},
      rightPriceScale:{visible:true,autoScale:true,borderVisible:true,borderColor:p.line,scaleMargins:{top:.08,bottom:.08}},
      timeScale:{visible:false,borderVisible:true,borderColor:p.line,timeVisible:/minute$/.test(state.interval),secondsVisible:false,rightOffset:6,minBarSpacing:1,tickMarkFormatter:axisTick},
      crosshair:{mode:1},
      handleScroll:interactive ? {mouseWheel:true,pressedMouseMove:true,horzTouchDrag:true,vertTouchDrag:false} : false,
      handleScale:interactive ? {axisPressedMouseMove:true,axisDoubleClickReset:true,mouseWheel:true,pinch:true} : false,
      kineticScroll:{mouse:interactive,touch:interactive},
    };
  }
  function syncPaneRange(range) {
    if (!range || state.rangeSyncing) return;
    state.rangeSyncing = true;
    try {
      for (const chart of [state.volumeChart,state.rsiChart,state.macdChart]) {
        if (!chart) continue;
        try { chart.timeScale().setVisibleLogicalRange(range); } catch {}
      }
    } finally { state.rangeSyncing = false; }
  }
  function resizeChartPane(chart, host) {
    if (!chart || !host || host.hidden) return;
    const width = Math.max(1, Math.floor(host.clientWidth || host.getBoundingClientRect().width || 1));
    const height = Math.max(1, Math.floor(host.clientHeight || host.getBoundingClientRect().height || 1));
    try { chart.resize(width, height); } catch {}
  }
  function updatePaneVisibility() {
    const showVolume = Boolean(state.overlayEnabled.volume);
    const showRsi = Boolean(state.overlayEnabled.rsi);
    const showMacd = Boolean(state.overlayEnabled.macd);
    if ($('volumePane')) $('volumePane').hidden = !showVolume;
    if ($('rsiPane')) $('rsiPane').hidden = !showRsi;
    if ($('macdPane')) $('macdPane').hidden = !showMacd;
    const bottom = showMacd ? state.macdChart : showRsi ? state.rsiChart : showVolume ? state.volumeChart : state.chart;
    for (const chart of [state.chart,state.volumeChart,state.rsiChart,state.macdChart]) {
      if (!chart) continue;
      try { chart.timeScale().applyOptions({visible:chart === state.chart || chart === bottom,timeVisible:/minute$/.test(state.interval),secondsVisible:false,tickMarkFormatter:axisTick}); } catch {}
    }
    requestAnimationFrame(() => {
      resizeChartPane(state.chart, $('chartHost'));
      if (showVolume) resizeChartPane(state.volumeChart, $('volumeChart'));
      if (showRsi) resizeChartPane(state.rsiChart, $('rsiChart'));
      if (showMacd) resizeChartPane(state.macdChart, $('macdChart'));
      if (state.chartUserRange) syncPaneRange(state.chartUserRange);
    });
  }
  function ensureChart() {
    if (!INTERNAL_CHART_ENABLED) return;
    if (state.page !== 'report' || state.chart || !window.LightweightCharts) return;
    const p = chartPalette();
    state.chart = LightweightCharts.createChart($('chartHost'), chartBaseOptions({interactive:true}));
    state.chart.applyOptions({timeScale:{visible:true,timeVisible:/minute$/.test(state.interval),secondsVisible:false}});
    state.candleSeries = state.chart.addCandlestickSeries({upColor:p.green,downColor:p.red,borderVisible:false,wickUpColor:p.green,wickDownColor:p.red});
    state.volumeChart = LightweightCharts.createChart($('volumeChart'), chartBaseOptions({interactive:false}));
    state.volumeSeries = state.volumeChart.addHistogramSeries({priceFormat:{type:'volume'},priceLineVisible:false,lastValueVisible:true,title:'Volume'});
    state.volumeAvgSeries = state.volumeChart.addLineSeries({color:p.blue,lineWidth:2,priceLineVisible:false,lastValueVisible:true,title:'Avg20',priceFormat:{type:'volume'}});
    try { state.volumeChart.priceScale('right').applyOptions({autoScale:true,scaleMargins:{top:.12,bottom:0}}); } catch {}
    state.rsiChart = LightweightCharts.createChart($('rsiChart'), chartBaseOptions({interactive:false}));
    state.rsiSeries = state.rsiChart.addLineSeries({color:p.blue,lineWidth:2,priceLineVisible:false,lastValueVisible:true,title:'RSI',autoscaleInfoProvider:()=>({priceRange:{minValue:0,maxValue:100}})});
    for (const [price,title,color] of [[20,'20',p.red],[50,'50',p.text],[80,'80',p.green]]) {
      try { state.rsiSeries.createPriceLine({price,title,color,lineWidth:1,lineStyle:2,axisLabelVisible:true}); } catch {}
    }
    state.macdChart = LightweightCharts.createChart($('macdChart'), chartBaseOptions({interactive:false}));
    state.macdLineSeries = state.macdChart.addLineSeries({color:p.blue,lineWidth:2,priceLineVisible:false,lastValueVisible:true,title:'MACD'});
    state.macdSignalSeries = state.macdChart.addLineSeries({color:p.amber,lineWidth:2,lineStyle:2,priceLineVisible:false,lastValueVisible:true,title:'Signal'});
    state.macdHistogramSeries = state.macdChart.addHistogramSeries({priceLineVisible:false,lastValueVisible:false,title:'Histogram'});
    try { state.macdLineSeries.createPriceLine({price:0,title:'0',color:p.text,lineWidth:1,lineStyle:0,axisLabelVisible:true}); } catch {}
    state.chart.timeScale().subscribeVisibleLogicalRangeChange(range => {
      syncPaneRange(range);
      if (!range) return;
      // Viewport authority belongs to the trader after any manual pan/zoom.
      // Programmatic moves (initial fit, explicit Fit, Follow Live, history prepend)
      // are guarded briefly so their own callbacks do not disable Follow Live.
      const programmatic = Date.now() <= Number(state.chartRangeProgrammaticUntil || 0);
      if (!programmatic && !state.chartLoadingOlder) {
        state.chartUserRange = {from:range.from, to:range.to};
        if (state.followLive) setFollowLive(false, {move:false});
      }
      if (state.chartLoadingOlder || !state.chartHasMore || range.from > 16) return;
      loadOlderChart();
    });
    updatePaneVisibility();
  }
  function parkStockChartSurface() {
    clearInterval(state.liveTimer); state.liveTimer = null;
    clearTimeout(state.chartWarmRetryTimer);
    clearTimeout(state.projectionRefreshTimer);
    state.chartWarmRetryTimer = null;
    state.projectionRefreshTimer = null;
    state.projectionRefreshEpoch += 1;
    state.chartLoadingOlder = false;
    const report = document.querySelector('[data-page-panel="report"]');
    report?.classList.add('chart-surface-parked');
    const maximized = document.querySelector('.chart-panel.chart-maximized');
    if (maximized) maximized.classList.remove('chart-maximized');
    document.body.classList.remove('analytics-maximized');
    if ($('maximizeChart')) $('maximizeChart').textContent = 'Maximize';
    for (const chart of [state.macdChart,state.rsiChart,state.volumeChart,state.chart]) {
      try { chart?.remove(); } catch {}
    }
    // Some Chromium/GPU combinations can retain a detached canvas compositor
    // after Lightweight Charts remove().  Zero and remove every residual canvas
    // and wrapper so inactive routes cannot paint a black surface below the page.
    const priceHost=$('chartHost'); const message=$('chartMessage');
    for (const host of [priceHost,$('volumeChart'),$('rsiChart'),$('macdChart')]) {
      if(!host) continue;
      host.querySelectorAll('canvas').forEach(canvas=>{ try{canvas.width=0;canvas.height=0;}catch{} canvas.remove(); });
      if(host===priceHost && message) host.replaceChildren(message); else host.replaceChildren();
    }
    state.chart = null; state.candleSeries = null;
    state.volumeChart = null; state.volumeSeries = null; state.volumeAvgSeries = null;
    state.rsiChart = null; state.rsiSeries = null;
    state.macdChart = null; state.macdLineSeries = null; state.macdSignalSeries = null; state.macdHistogramSeries = null;
    state.overlaySeries = {}; state.priceLines = [];
  }
  function restoreStockChartSurface() {
    const report = document.querySelector('[data-page-panel="report"]');
    report?.classList.remove('chart-surface-parked');
    if (!state.symbol) return;
    ensureChart();
    if (state.candles.length && state.candleSeries) {
      try { state.candleSeries.setData(state.candles.map(({time,open,high,low,close}) => ({time,open,high,low,close}))); } catch {}
      renderChartOverlays();
      updatePaneVisibility();
      if ($('chartMessage')) $('chartMessage').hidden = true;
      if (state.chartUserRange) {
        try { state.chart.timeScale().setVisibleLogicalRange(state.chartUserRange); } catch { setDefaultVisibleRange(); }
      } else setDefaultVisibleRange();
      const projectionState = text(state.chartProjection?.state || '').toUpperCase();
      const enabledMissing = ['vwap','ema','supertrend','rsi','macd'].some(name => state.overlayEnabled[name] && !projectionHas(name));
      if (projectionState !== 'READY' || enabledMissing) scheduleProjectionRefresh(220);
    } else if ($('chartMessage')) {
      $('chartMessage').hidden = false;
      $('chartMessage').textContent = `Search or reopen ${state.symbol} to load verified chart data.`;
    }
    scheduleLivePoll();
  }

  function clearPriceLines() {
    for (const line of state.priceLines) try { state.candleSeries?.removePriceLine(line); } catch {}
    state.priceLines = [];
  }
  function addPriceLine(raw, title, color, lineWidth = 1, lineStyle = 2) {
    const price = number(raw);
    if (price === null || price <= 0 || !state.candleSeries) return;
    try { state.priceLines.push(state.candleSeries.createPriceLine({price,title,color,lineWidth,lineStyle,axisLabelVisible:true})); } catch {}
  }
  function clearOverlaySeries() {
    if (!state.chart) return;
    for (const series of Object.values(state.overlaySeries)) {
      try { state.chart.removeSeries(series); } catch {}
    }
    state.overlaySeries = {};
  }
  function overlayPoints(name) {
    return rows(state.chartProjection?.series?.[name]).map(row => ({time:chartTimestamp(row.time),value:number(row.value)})).filter(row => row.time !== null && row.value !== null);
  }
  function alignedOverlayPoints(name, decorate=null) {
    const byTime = new Map(overlayPoints(name).map(row => [row.time,row]));
    return state.candles.map(candle => {
      const row = byTime.get(candle.time);
      if (!row) return {time:candle.time};
      return decorate ? decorate(row) : row;
    });
  }
  function addLineOverlay(key, points, options = {}) {
    if (!state.chart || !points.some(row => number(row.value) !== null)) return null;
    const series = state.chart.addLineSeries({lineWidth:2,priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false,...options});
    series.setData(points);
    state.overlaySeries[key] = series;
    return series;
  }
  function canonicalLevelPrice(row) { return number(typeof row === 'object' ? pick(row,'price','level','value') : row); }
  function structuralLevelCandidates(levels, side, latest) {
    const direct = side === 'support' ? rows(levels.support_levels) : rows(levels.resistance_levels);
    const ranked = rows(levels.major_levels).filter(row => {
      const value=canonicalLevelPrice(row); if (value===null || latest===null) return false;
      const sideText=text(pick(row,'side','kind','type','classification')).toLowerCase();
      return side==='support' ? (value<latest && (!sideText || /support|demand|low/.test(sideText))) : (value>latest && (!sideText || /resistance|supply|high/.test(sideText)));
    });
    const values=[...direct,...ranked].map(canonicalLevelPrice).filter(value => value!==null && value>0 && (latest===null || (side==='support'?value<latest:value>latest)));
    return [...new Set(values.map(value => Number(value.toFixed(6))))].sort((a,b)=>side==='support'?b-a:a-b);
  }
  function renderPriceOverlays() {
    clearPriceLines();
    const p = chartPalette();
    const map = activeDeskProjection(state.stock).tradeMap || {};
    const levels = state.stock?.market_levels || state.stock?.levels || {};
    const latest = number(state.candles.at(-1)?.close) ?? number(pick(state.stock?.selected_quote || state.stock?.quote || {},'ltp','close'));
    const atr = number(levels.atr14) ?? number(state.chartProjection?.metrics?.atr14) ?? 0;
    const tolerance = Math.max(0.01, (latest || 0) * 0.0015, atr * 0.12);
    const baseSupport = canonicalLevelPrice(levels.support ?? state.stock?.support);
    const baseResistance = canonicalLevelPrice(levels.resistance ?? state.stock?.resistance);
    const supportCandidates = structuralLevelCandidates(levels,'support',latest);
    const resistanceCandidates = structuralLevelCandidates(levels,'resistance',latest);
    let majorSupport = canonicalLevelPrice(pick(levels.major_support_evidence || {},'price') ?? levels.long_term_support);
    let majorResistance = canonicalLevelPrice(pick(levels.major_resistance_evidence || {},'price') ?? levels.long_term_resistance);
    if (majorSupport===null || Math.abs(majorSupport-(baseSupport||majorSupport))<=tolerance) majorSupport = supportCandidates.find(value => baseSupport===null || Math.abs(value-baseSupport)>tolerance) ?? majorSupport;
    if (majorResistance===null || Math.abs(majorResistance-(baseResistance||majorResistance))<=tolerance) majorResistance = resistanceCandidates.find(value => baseResistance===null || Math.abs(value-baseResistance)>tolerance) ?? majorResistance;
    const distinctFrom = (value, ...others) => value !== null && value > 0 && others.every(other => other === null || other <= 0 || Math.abs(value-other) > tolerance);
    if (state.overlayEnabled.trade) {
      addPriceLine(baseSupport, 'S · SUPPORT', p.green, 1, 0);
      addPriceLine(baseResistance, 'R · RESISTANCE', p.red, 1, 0);
      addPriceLine(map.entry, 'ENTRY', p.blue, 2, 0);
      addPriceLine(map.target_1 ?? map.target, 'T1', p.green, 2, 0);
      addPriceLine(map.stop, 'SL', p.red, 2, 0);
    }
    if (state.overlayEnabled.major_sr) {
      if (distinctFrom(majorSupport, baseSupport, baseResistance)) addPriceLine(majorSupport, 'MAJOR S', p.cyan || '#00c2ff', 2, 0);
      if (distinctFrom(majorResistance, baseResistance, baseSupport)) addPriceLine(majorResistance, 'MAJOR R', p.amber, 2, 0);
    }
    // Camarilla remains backend evidence only; hidden from the customer chart.
  }
  function renderIndicatorReadout() {
    const metrics = state.chartProjection?.metrics || state.stock?.indicator_metrics || {};
    const items = [];
    const add = (key,value,detail='') => {
      if (number(value) === null && !text(value)) return;
      items.push(`<span class="indicator-chip"><small>${esc(key)}</small><b>${esc(number(value) === null ? label(value) : formatNumber(value,2))}</b>${detail ? `<em>${esc(detail)}</em>` : ''}</span>`);
    };
    const projectionState = text(state.chartProjection?.state || 'MISSING').toUpperCase();
    const projectionFresh = text(state.chartProjection?.freshness || '').toUpperCase();
    if (!/READY/.test(projectionState) || /MISSING|STALE/.test(projectionFresh)) add('Indicators', /READY/.test(projectionState) ? `Refreshing ${projectionFresh || ''}` : 'Warming');
    add('ATR14',metrics.atr14,number(metrics.atr14_pct) === null ? '' : `${formatNumber(metrics.atr14_pct,2)}%`);
    add('ADX14',metrics.adx14);
    if (state.overlayEnabled.rsi) { const rsi=number(metrics.rsi14); add('RSI14',rsi,rsi===null?'':rsi>=80?'Overbought':rsi<=20?'Oversold':'Neutral'); }
    if (state.overlayEnabled.macd) { add('MACD',metrics.macd); add('Signal',metrics.macd_signal); add('Hist',metrics.macd_hist); }
    if (state.overlayEnabled.ema) add('EMA stack',state.chartProjection?.states?.ema_stack || 'Unavailable');
    if (state.overlayEnabled.supertrend) add('Supertrend',number(metrics.supertrend_direction)>0?'Bullish':number(metrics.supertrend_direction)<0?'Bearish':'Unavailable');
    $('indicatorReadout').innerHTML = items.join('') || '<span>Indicators use backend-owned completed-candle mathematics.</span>';
  }
  function renderIndicatorPanes() {
    if (!state.volumeSeries || !state.rsiSeries || !state.macdLineSeries) return;
    const p = chartPalette();
    if (state.overlayEnabled.volume) {
      const data = state.candles.map(row => number(row.volume) === null ? {time:row.time} : {time:row.time,value:row.volume,color:row.close>=row.open?colorAlpha(p.green,.55):colorAlpha(p.red,.55)});
      try { state.volumeSeries.setData(data); } catch {}
      const avg20 = state.candles.map((row,index,all) => {
        const window = all.slice(Math.max(0,index-19),index+1).map(x=>number(x.volume)).filter(v=>v!==null&&v>=0);
        return window.length >= 5 ? {time:row.time,value:window.reduce((a,b)=>a+b,0)/window.length} : {time:row.time};
      });
      try { state.volumeAvgSeries?.setData(avg20); } catch {}
      const usable = data.filter(row => number(row.value) !== null).length;
      const latestVol = usable ? number(data.filter(row=>number(row.value)!==null).at(-1)?.value) : null;
      const latestAvg = number(avg20.filter(row=>number(row.value)!==null).at(-1)?.value);
      const vi = volumeParticipationIntel(state.stock || {}, state.stock?.selected_quote || state.stock?.quote || {});
      const paneDetail = latestVol!==null ? `${formatCompactVolume(latestVol)}${latestAvg!==null ? ` · Avg20 ${formatCompactVolume(latestAvg)}` : ''}` : vi.text;
      $('volumeState').textContent = usable ? `${paneDetail} · ${vi.label} · ${usable} bars` : 'Volume unavailable';
      if (state.stock) renderQuoteStats(state.stock, state.stock.selected_quote || state.stock.quote || {});
    } else { try { state.volumeSeries.setData([]); state.volumeAvgSeries?.setData([]); } catch {} }
    if (state.overlayEnabled.rsi) {
      const data = alignedOverlayPoints('rsi14');
      try { state.rsiSeries.setData(data); } catch {}
      const value = number(state.chartProjection?.metrics?.rsi14);
      $('rsiState').textContent = value === null ? 'RSI projection warming' : `RSI ${formatNumber(value,1)} · completed candles`;
    } else { try { state.rsiSeries.setData([]); } catch {} }
    if (state.overlayEnabled.macd) {
      const line = alignedOverlayPoints('macd'), signal = alignedOverlayPoints('macd_signal');
      const hist = alignedOverlayPoints('macd_hist', row => ({...row,color:row.value>=0?colorAlpha(p.green,.6):colorAlpha(p.red,.6)}));
      try { state.macdLineSeries.setData(line); state.macdSignalSeries.setData(signal); state.macdHistogramSeries.setData(hist); } catch {}
      const mv=number(state.chartProjection?.metrics?.macd), sv=number(state.chartProjection?.metrics?.macd_signal), hv=number(state.chartProjection?.metrics?.macd_hist);
      $('macdState').textContent = mv === null ? 'MACD projection warming' : `MACD ${formatNumber(mv,2)} · Signal ${formatNumber(sv,2)} · Hist ${formatNumber(hv,2)}`;
    } else { try { state.macdLineSeries.setData([]); state.macdSignalSeries.setData([]); state.macdHistogramSeries.setData([]); } catch {} }
    updatePaneVisibility();
  }
  function renderChartOverlays() {
    if (!state.chart || !state.candleSeries) return;
    clearOverlaySeries();
    renderPriceOverlays();
    const p = chartPalette();
    if (state.overlayEnabled.vwap) addLineOverlay('vwap',alignedOverlayPoints('vwap'),{color:p.amber,lineWidth:2});
    if (state.overlayEnabled.ema) {
      addLineOverlay('ema20',alignedOverlayPoints('ema20'),{color:p.blue,lineWidth:2});
      addLineOverlay('ema50',alignedOverlayPoints('ema50'),{color:'#7c3aed',lineWidth:2});
    }
    if (state.overlayEnabled.supertrend) {
      const points = rows(state.chartProjection?.series?.supertrend).map(row => ({time:chartTimestamp(row.time),value:number(row.value),direction:number(row.direction)})).filter(row => row.time !== null && row.value !== null);
      const byTime = new Map(points.map(row => [row.time,row]));
      const bull = state.candles.map(candle => { const row=byTime.get(candle.time); return row?.direction>0?{time:candle.time,value:row.value}:{time:candle.time}; });
      const bear = state.candles.map(candle => { const row=byTime.get(candle.time); return row?.direction<0?{time:candle.time,value:row.value}:{time:candle.time}; });
      addLineOverlay('supertrendBull',bull,{color:p.green,lineWidth:3});
      addLineOverlay('supertrendBear',bear,{color:p.red,lineWidth:3});
    }
    renderIndicatorPanes();
    renderIndicatorReadout();
  }
  function syncOverlayButtons() {
    all('[data-overlay]').forEach(button => {
      const active = Boolean(state.overlayEnabled[button.dataset.overlay]);
      button.classList.toggle('active',active);
      button.setAttribute('aria-pressed',String(active));
    });
  }
  function projectionRequirement(name) {
    return ({vwap:['vwap'],ema:['ema20','ema50'],supertrend:['supertrend'],rsi:['rsi14'],macd:['macd','macd_signal','macd_hist']})[name] || [];
  }
  function projectionHas(name) { return projectionRequirement(name).every(key => rows(state.chartProjection?.series?.[key]).length > 0); }
  function setProjectionBusy(busy) {
    for (const name of ['vwap','ema','supertrend','rsi','macd']) {
      const button = document.querySelector(`[data-overlay="${name}"]`);
      if (button) button.setAttribute('aria-busy',String(Boolean(busy && state.overlayEnabled[name] && !projectionHas(name))));
    }
  }
  function scheduleProjectionRefresh(delay=350) {
    clearTimeout(state.projectionRefreshTimer);
    if (state.page !== 'report' || !state.symbol || !state.candles.length) return;
    const symbol=state.symbol, interval=state.interval, stockEpoch=state.stockEpoch, refreshEpoch=++state.projectionRefreshEpoch;
    state.projectionRefreshAttempts=0;
    const run = async () => {
      if (state.page !== 'report' || refreshEpoch !== state.projectionRefreshEpoch || stockEpoch !== state.stockEpoch || state.symbol !== symbol || state.interval !== interval) return;
      setProjectionBusy(true);
      try {
        const payload = await api(`/api/chart-data?symbol=${encodeURIComponent(symbol)}&interval=${encodeURIComponent(interval)}&limit=500`,{timeout:3200});
        if (state.page !== 'report' || refreshEpoch !== state.projectionRefreshEpoch || stockEpoch !== state.stockEpoch || state.symbol !== symbol || state.interval !== interval) return;
        if (!payloadIdentityMatches(payload,symbol)) return;
        const projection = payload.chart_projection || {};
        if (Object.keys(projection).length) { state.chartProjection=projection; renderChartOverlays(); }
        const ready = text(projection.state).toUpperCase()==='READY' && !/MISSING|STALE/.test(text(projection.freshness).toUpperCase());
        const enabledMissing = ['vwap','ema','supertrend','rsi','macd'].some(name => state.overlayEnabled[name] && !projectionHas(name));
        if (ready && !enabledMissing) { setProjectionBusy(false); return; }
      } catch {}
      state.projectionRefreshAttempts += 1;
      if (state.projectionRefreshAttempts < 5) state.projectionRefreshTimer=setTimeout(run,[450,800,1400,2200,3200][state.projectionRefreshAttempts-1] || 3200);
      else { setProjectionBusy(false); renderIndicatorReadout(); }
    };
    state.projectionRefreshTimer=setTimeout(run,delay);
  }
  function toggleOverlay(name) {
    if (!(name in state.overlayEnabled)) return;
    state.overlayEnabled[name] = !state.overlayEnabled[name];
    syncOverlayButtons();
    renderChartOverlays();
    if (state.overlayEnabled[name] && projectionRequirement(name).length && !projectionHas(name)) scheduleProjectionRefresh(40);
  }
  function defaultVisibleBars() {
    return ({'1minute':390,'3minute':130,'5minute':78,'15minute':52,'30minute':40,'60minute':35,'240minute':45,'day':120,'week':104,'month':60})[state.interval] || 120;
  }
  function markProgrammaticChartRange(ms = 320) { state.chartRangeProgrammaticUntil = Date.now() + ms; }
  function setDefaultVisibleRange() {
    if (!state.chart || !state.candles.length) return;
    const visible = Math.min(state.candles.length, defaultVisibleBars());
    markProgrammaticChartRange();
    try { state.chart.timeScale().setVisibleLogicalRange({from:Math.max(0,state.candles.length-visible),to:state.candles.length+3}); }
    catch { try { state.chart.timeScale().fitContent(); } catch {} }
  }
  function clearChartForSelection(message = 'Loading chart…') {
    ensureChart();
    clearTimeout(state.projectionRefreshTimer);
    state.projectionRefreshEpoch += 1;
    state.projectionRefreshAttempts = 0;
    setProjectionBusy(false);
    state.candles = []; state.chartProjection = {}; state.chartBefore = null; state.chartHasMore = true;
    clearOverlaySeries(); clearPriceLines();
    for (const series of [state.candleSeries,state.volumeSeries,state.volumeAvgSeries,state.rsiSeries,state.macdLineSeries,state.macdSignalSeries,state.macdHistogramSeries]) {
      try { series?.setData([]); } catch {}
    }
    if ($('volumeState')) $('volumeState').textContent = 'Volume awaiting verified bars';
    if ($('rsiState')) $('rsiState').textContent = 'RSI projection awaiting verified bars';
    if ($('macdState')) $('macdState').textContent = 'MACD projection awaiting verified bars';
    $('chartAsOf').textContent = 'No verified bars';
    $('chartContract').textContent = `${label(state.interval)} candles · identity pending · IST`;
    $('chartMessage').hidden = false; $('chartMessage').textContent = message;
    updatePaneVisibility();
  }
  function timeframeIdentityMatches(payload, expectedInterval) {
    const proof = payload?.timeframe_identity || {};
    const expected = ({'1minute':'1m','3minute':'3m','5minute':'5m','15minute':'15m','30minute':'30m','60minute':'60m','240minute':'240m','day':'1d','week':'1w','month':'1mo'})[expectedInterval] || expectedInterval;
    return proof?.passed === true && text(proof?.canonical_interval) === text(expected);
  }
  function payloadIdentityMatches(payload, expectedSymbol) {
    const returned = text(payload?.symbol || payload?.instrument?.symbol || payload?.instrument?.trading_symbol).trim().toUpperCase();
    const expected = text(expectedSymbol).trim().toUpperCase();
    // Every stock/chart/live payload is identity-bearing by contract. Missing
    // identity is therefore unsafe, not a compatibility success: fail closed so
    // another symbol can never remain visible after a timed-out selection.
    return Boolean(returned && expected && returned === expected);
  }
  function renderChart(payload, {fit = true} = {}) {
    ensureChart();
    if (!payloadIdentityMatches(payload, state.symbol)) {
      clearChartForSelection(`Chart identity mismatch: requested ${state.symbol}; received ${payload?.symbol || 'unknown'}. No stale chart retained.`);
      return false;
    }
    if (!timeframeIdentityMatches(payload, state.interval)) {
      clearChartForSelection(`${label(state.interval)} requested · timeframe identity FAILED CLOSED · IST`);
      return false;
    }
    // The backend chart service is the freshness/continuity authority. A payload
    // may contain retained candles for diagnostics or historical paging while
    // still being explicitly non-usable as a live chart. Never render those
    // retained rows as though they were current merely because candle data exists.
    if (payload?.ok !== true || payload?.chart_enabled === false) {
      const status = text(payload?.data_status || payload?.state).toLowerCase();
      const message = status === 'stale_disabled'
        ? 'Live chart disabled — candle data is stale.'
        : status === 'continuity_failed'
          ? 'Chart disabled — candle continuity is not proven.'
          : (payload?.message || 'Chart unavailable — verified live candle evidence is not ready.');
      clearChartForSelection(message);
      return false;
    }
    const candles = normaliseCandles(payload);
    if (!candles.length) {
      try { state.candleSeries?.setData([]); } catch {}
      state.candles = []; state.chartProjection = {};
      $('chartMessage').hidden = false;
      const warming = payload?.refreshing === true || /warming|materializ|missing_local/i.test(text(payload?.data_status || payload?.state));
      $('chartMessage').textContent = warming ? `Verified ${label(state.interval)} history is materializing locally…` : (payload?.message || 'No verified local bars are available for this identity.');
      $('chartAsOf').textContent = warming ? 'History warming' : 'No verified bars';
      return false;
    }
    clearTimeout(state.chartWarmRetryTimer); state.chartWarmRetryAttempts = 0;
    state.candles = candles;
    state.chartProjection = payload?.chart_projection || {};
    state.chartBefore = new Date(candles[0].time * 1000).toISOString();
    state.chartHasMore = payload?.paging?.has_more_older !== false;
    state.candleSeries.setData(candles.map(({time,open,high,low,close}) => ({time,open,high,low,close})));
    for (const chart of [state.chart,state.volumeChart,state.rsiChart,state.macdChart]) {
      try { chart?.timeScale().applyOptions({timeVisible:/minute$/.test(state.interval), secondsVisible:false, tickMarkFormatter:axisTick}); } catch {}
    }
    $('chartMessage').hidden = true;
    $('chartAsOf').textContent = chartRangeSummary(candles);
    if (state.stock) {
      const chosen = state.stock.display_quote || state.stock.selected_quote || state.stock.quote || {};
      if (positivePrice(pick(chosen,'ltp','last_price','close')) === null && candles.length) {
        const last = candles[candles.length - 1];
        state.stock.display_quote = {ltp:last.close,last_price:last.close,close:last.close,timestamp:new Date(last.time*1000).toISOString(),freshness_state:'completed_session_close',display_only:true,execution_price_authority:false,display_price_authority:'SERVED_VERIFIED_CHART_CANDLES'};
        renderQuoteStats(state.stock, state.stock.display_quote); renderPriceChangeStrip(state.stock, state.stock.display_quote);
        setStatePill($('quoteState'),'closed','Verified close');
      }
    }
    const projectionState = text(state.chartProjection?.state || '').toUpperCase();
    const indicatorContract = projectionState === 'READY' ? 'completed-candle indicators ready' : 'indicator projection warming';
    $('chartContract').textContent = `${label(state.interval)} candles · timeframe identity verified · ${payload?.data_status || payload?.state || 'local projection'} · ${indicatorContract} · IST · older history paged`;
    renderChartOverlays();
    updatePaneVisibility();
    const enabledProjectionMissing = ['vwap','ema','supertrend','rsi','macd'].some(name => state.overlayEnabled[name] && !projectionHas(name));
    if (projectionState !== 'READY' || enabledProjectionMissing) scheduleProjectionRefresh(220);
    if (fit) setDefaultVisibleRange();
    return true;
  }
  async function loadChartOnly({fit = true} = {}) {
    if (!INTERNAL_CHART_ENABLED) {
      clearInterval(state.liveTimer);
      clearTimeout(state.chartWarmRetryTimer);
      return false;
    }
    if (!state.symbol) return;
    const epoch = state.stockEpoch;
    $('chartMessage').hidden = false;
    $('chartMessage').textContent = 'Loading bounded local chart…';
    try {
      const payload = await api(`/api/chart-data?symbol=${encodeURIComponent(state.symbol)}&interval=${encodeURIComponent(state.interval)}&limit=500`, {timeout:4000});
      if (epoch !== state.stockEpoch) return;
      const rendered = renderChart(payload, {fit});
      if (rendered) scheduleLivePoll();
      else {
        clearInterval(state.liveTimer);
        // Retry the bounded verified chart endpoint rather than polling/forming
        // live bars onto a chart whose base history failed freshness/continuity.
        clearTimeout(state.chartWarmRetryTimer);
        state.chartWarmRetryTimer = setTimeout(() => {
          if (state.symbol && state.page === 'report') loadChartOnly({fit:false});
        }, 5000);
      }
    } catch (error) {
      if (epoch !== state.stockEpoch) return;
      clearChartForSelection(`Chart unavailable for ${state.symbol}: ${error.message}. No other symbol's chart is retained.`);
    }
  }
  async function loadOlderChart() {
    if (!INTERNAL_CHART_ENABLED) return;
    if (!state.symbol || !state.chartBefore || state.chartLoadingOlder || !state.chartHasMore) return;
    state.chartLoadingOlder = true;
    const oldRange = state.chart.timeScale().getVisibleLogicalRange();
    const oldCount = state.candles.length;
    try {
      const payload = await api(`/api/chart-data?symbol=${encodeURIComponent(state.symbol)}&interval=${encodeURIComponent(state.interval)}&before=${encodeURIComponent(state.chartBefore)}&limit=500`, {timeout:3500});
      const older = normaliseCandles(payload).filter(row => row.time < state.candles[0].time);
      if (!older.length) { state.chartHasMore = false; return; }
      const combined = new Map([...older, ...state.candles].map(row => [row.time, row]));
      state.candles = [...combined.values()].sort((a,b) => a.time - b.time);
      state.chartBefore = new Date(state.candles[0].time * 1000).toISOString();
      state.chartHasMore = payload?.paging?.has_more_older !== false;
      state.candleSeries.setData(state.candles.map(({time,open,high,low,close}) => ({time,open,high,low,close})));
      // Older pages are price/volume pagination only. Retain the current verified
      // indicator projection and align it against the expanded candle timeline.
      renderChartOverlays();
      if (oldRange) { markProgrammaticChartRange(); state.chart.timeScale().setVisibleLogicalRange({from:oldRange.from + state.candles.length - oldCount, to:oldRange.to + state.candles.length - oldCount}); }
    } catch (error) {
      toast(`Older chart page unavailable: ${error.message}`);
    } finally {
      state.chartLoadingOlder = false;
    }
  }
  function setFollowLive(active, {move=true} = {}) {
    state.followLive = Boolean(active);
    if (state.followLive) state.chartUserRange = null;
    $('followLive').classList.toggle('active', state.followLive);
    $('followLive').textContent = state.followLive ? 'Following live' : 'Follow live';
    if (state.followLive && move) {
      markProgrammaticChartRange();
      try { state.chart?.timeScale().scrollToRealTime(); } catch {}
    }
  }
  function scheduleLivePoll() {
    clearInterval(state.liveTimer);
    if (!INTERNAL_CHART_ENABLED) return;
    if (!state.symbol) return;
    loadLiveBar();
    if (state.workspace?.market_open === false) return;
    state.liveTimer = setInterval(loadLiveBar, 1000);
  }
  async function loadLiveBar() {
    if (!INTERNAL_CHART_ENABLED) return;
    const symbol = state.symbol, interval = state.interval;
    if (!symbol || state.page !== 'report') return;
    try {
      const payload = await api(`/api/live-chart-bar?symbol=${encodeURIComponent(symbol)}&interval=${encodeURIComponent(interval)}`, {timeout:1800});
      if (state.symbol !== symbol || state.interval !== interval) return;
      if (!payloadIdentityMatches(payload, symbol)) {
        clearChartForSelection(`Live-chart identity mismatch for ${symbol}. Stale data suppressed.`);
        return;
      }
      const quote = payload.quote || {};
      if (Object.keys(quote).length && state.stock) {
        const merged = {...(state.stock.selected_quote || state.stock.quote || {}), ...quote};
        state.stock.selected_quote = merged;
        state.stock.quote = merged;
        renderQuoteStats(state.stock, merged);
        renderPriceChangeStrip(state.stock, merged);
      }
      const bar = normaliseCandles({candles:[payload.forming_bar].filter(Boolean)})[0];
      const fresh = freshness(payload.quote_freshness || payload.state, sourceTime(quote) || sourceTime(payload.forming_bar || payload), state.workspace?.market_open);
      setStatePill($('quoteState'), fresh.state, fresh.label);
      if (!bar) return;
      const previous = state.candles.at(-1);
      if (!previous || bar.time > previous.time) state.candles.push(bar);
      else if (bar.time === previous.time) state.candles[state.candles.length - 1] = bar;
      state.candleSeries?.update({time:bar.time,open:bar.open,high:bar.high,low:bar.low,close:bar.close});
      if (state.volumeSeries && number(bar.volume) !== null) {
        const p = chartPalette();
        try { state.volumeSeries.update({time:bar.time,value:bar.volume,color:bar.close >= bar.open ? colorAlpha(p.green,.55) : colorAlpha(p.red,.55)}); } catch {}
      }
      $('chartAsOf').textContent = `${chartRangeSummary(state.candles)} · forming ${chartTimeFormatter(bar.time)}`;
      $('chartContract').textContent = `${label(state.interval)} candles · forming bar live · indicators from completed candles · IST`;
      if (state.followLive) { markProgrammaticChartRange(220); state.chart?.timeScale().scrollToRealTime(); }
    } catch {}
  }

  function stockSnapshotNeedsConvergence(payload) {
    if (!payload || payload.ok !== true) return true;
    const states = payload.component_states || {};
    const technical = text(states.technical_snapshot?.state || '').toUpperCase();
    const chart = text(states.chart?.state || '').toUpperCase();
    return text(payload.state).toUpperCase() !== 'READY' || technical !== 'READY' || chart !== 'READY';
  }
  function scheduleSnapshotConvergence(snapshotPath, symbol, epoch) {
    clearTimeout(state.snapshotWarmRetryTimer);
    if (epoch !== state.stockEpoch || state.symbol !== symbol || state.snapshotWarmRetryAttempts >= 12) return;
    const delay = Math.min(1800, 320 + state.snapshotWarmRetryAttempts * 140);
    state.snapshotWarmRetryTimer = setTimeout(async () => {
      if (epoch !== state.stockEpoch || state.symbol !== symbol) return;
      state.snapshotWarmRetryAttempts += 1;
      try {
        const retry = await api(snapshotPath, {timeout:3000});
        if (epoch !== state.stockEpoch || state.symbol !== symbol || !payloadIdentityMatches(retry, symbol)) return;
        state.stock = retry; renderStock(retry);
        if (stockSnapshotNeedsConvergence(retry)) scheduleSnapshotConvergence(snapshotPath, symbol, epoch);
      } catch { scheduleSnapshotConvergence(snapshotPath, symbol, epoch); }
    }, delay);
  }
  function scheduleChartConvergence(symbol, epoch, {fit=true} = {}) {
    clearTimeout(state.chartWarmRetryTimer);
    if (!INTERNAL_CHART_ENABLED) return;
    if (state.page !== 'report' || epoch !== state.stockEpoch || state.symbol !== symbol || state.chartWarmRetryAttempts >= 14 || state.candles.length) return;
    const delay = Math.min(1700, 260 + state.chartWarmRetryAttempts * 120);
    state.chartWarmRetryTimer = setTimeout(async () => {
      if (state.page !== 'report' || epoch !== state.stockEpoch || state.symbol !== symbol || state.candles.length) return;
      state.chartWarmRetryAttempts += 1;
      try {
        const payload = await api(`/api/chart-data?symbol=${encodeURIComponent(state.instrumentKey || symbol)}&interval=${encodeURIComponent(state.interval)}&limit=500`, {timeout:4000});
        if (epoch !== state.stockEpoch || state.symbol !== symbol) return;
        if (!renderChart(payload,{fit})) scheduleChartConvergence(symbol, epoch, {fit});
      } catch { scheduleChartConvergence(symbol, epoch, {fit}); }
    }, delay);
  }

  async function openStock(rawSymbol, mode = state.stockMode, rawInstrumentKey = '', decisionId = '', researchCandidate = '') {
    const symbol = text(rawSymbol).trim().toUpperCase();
    const instrumentKey = text(rawInstrumentKey).trim();
    if (!symbol) return;
    state.symbol = symbol;
    state.instrumentKey = instrumentKey;
    state.stockMode = mode === 'intraday' ? 'intraday' : 'delivery';
    state.interval = state.stockMode === 'intraday' ? '5minute' : 'day';
    api('/api/live-market/subscriptions', {method:'POST', body:{symbol, mode:'full', ttl_seconds:900}, timeout:1800}).catch(() => {});
    state.stock = null;
    state.candles = [];
    state.chartHasMore = true;
    clearTimeout(state.chartWarmRetryTimer); clearTimeout(state.snapshotWarmRetryTimer);
    state.chartWarmRetryAttempts = 0; state.snapshotWarmRetryAttempts = 0;
    const epoch = ++state.stockEpoch;
    showPage('report',{push:false});
    history.replaceState(null,'',routeHash('report',{symbol,mode:state.stockMode,instrument:instrumentKey,decision:decisionId,research:researchCandidate}));
    clearChartForSelection(`Loading ${symbol} chart…`);
    $('reportTitle').textContent = symbol;
    $('reportSubtitle').textContent = `${label(state.stockMode)} · resolving canonical local identity`;
    $('reportLoading').hidden = false;
    $('quoteStats').innerHTML = '';
    $('tradeMap').innerHTML = '<div class="empty">Decision projection loading…</div>';
    $('priceChangeStrip').innerHTML = '';
    $('deskDecisionStrip').innerHTML = '<div class="empty">Desk decisions loading…</div>';
    $('mtfStrip').innerHTML = '<span class="muted">Loading MTF evidence…</span>';
    $('mtfStatus').textContent = '0/10';
    $('indicatorReadout').innerHTML = '<span>Loading indicator projection…</span>';
    const identityLookup = instrumentKey || symbol;
    const snapshotPath = `/api/stock-snapshot?symbol=${encodeURIComponent(identityLookup)}&mode=${encodeURIComponent(state.stockMode)}`;
    const snapshotPromise = api(snapshotPath, {timeout:2800});
    // CUSTOM_CHART_DISABLED: do not fetch internal chart data while the reliability gate is closed.
    // The stock intelligence/decision path is independent of chart availability.
    const chartPromise = INTERNAL_CHART_ENABLED
      ? api(`/api/chart-data?symbol=${encodeURIComponent(identityLookup)}&interval=${encodeURIComponent(state.interval)}&limit=500`, {timeout:4000})
      : Promise.resolve({ok:false, chart_enabled:false, state:'DISABLED', message:'Internal live chart disabled — use broker chart.'});
    const [snapshotResult, chartResult] = await Promise.allSettled([snapshotPromise, chartPromise]);
    if (epoch !== state.stockEpoch) return;
    $('reportLoading').hidden = true;
    if (snapshotResult.status === 'fulfilled' && payloadIdentityMatches(snapshotResult.value, symbol)) {
      state.stock = snapshotResult.value;
      renderStock(snapshotResult.value);
      if (stockSnapshotNeedsConvergence(snapshotResult.value)) scheduleSnapshotConvergence(snapshotPath, symbol, epoch);
    } else {
      const snapshotReason = snapshotResult.status === 'fulfilled'
        ? new Error(`identity mismatch: requested ${symbol}; received ${snapshotResult.value?.symbol || 'unknown'}`)
        : snapshotResult.reason;
      $('reportSubtitle').textContent = `Stock snapshot warming: ${snapshotReason.message}`;
      setStatePill($('quoteState'), 'warming', 'Snapshot warming');
      // A browser deadline is not an authority failure. Keep the independent
      // chart visible and poll the bounded local read model while the selected-
      // stock pipeline converges. The operator can inspect exact progress in
      // Progress & Proof; we do not turn a transient 4.5s miss into permanent
      // UNAVAILABLE until the user leaves the stock.
      const pollSnapshot = async (attempt = 1) => {
        if (epoch !== state.stockEpoch || state.symbol !== symbol || state.stock) return;
        try {
          const retry = await api(snapshotPath, {timeout:3000});
          if (epoch !== state.stockEpoch || state.symbol !== symbol) return;
          if (!payloadIdentityMatches(retry, symbol)) throw new Error(`identity mismatch: requested ${symbol}; received ${retry?.symbol || 'unknown'}`);
          state.stock = retry; renderStock(retry); $('reportLoading').hidden = true;
          if (stockSnapshotNeedsConvergence(retry)) scheduleSnapshotConvergence(snapshotPath, symbol, epoch);
        } catch (retryError) {
          if (epoch !== state.stockEpoch || state.symbol !== symbol || state.stock) return;
          $('reportSubtitle').textContent = `Stock snapshot warming for ${symbol} · attempt ${attempt} · ${retryError.message} · see Progress & Proof`;
          setStatePill($('quoteState'), 'warming', 'Snapshot warming');
          if (attempt < 10) setTimeout(() => pollSnapshot(attempt + 1), Math.min(2500, 700 + attempt * 180));
          else {
            $('reportSubtitle').textContent = `Stock snapshot still warming for ${symbol} · use Progress & Proof / Rebuild selected stock`;
            setStatePill($('quoteState'), 'warming', 'Needs progress check');
          }
        }
      };
      setTimeout(() => pollSnapshot(1), 700);
    }
    if (chartResult.status === 'fulfilled') {
      if (!renderChart(chartResult.value)) scheduleChartConvergence(symbol, epoch);
    } else {
      clearChartForSelection(`Chart unavailable for ${symbol}: ${chartResult.reason.message}. No stale series retained.`);
      scheduleChartConvergence(symbol, epoch);
    }
    if (INTERNAL_CHART_ENABLED) scheduleLivePoll();
  }

  function volumeParticipationIntel(payload, quote) {
    const metricSources = [payload?.selected_stock_snapshot?.metrics || {}, payload?.technical_snapshot?.metrics || {}, state.chartProjection?.metrics || {}, payload?.selected_stock_truth || {}, quote || {}];
    const metric = (...keys) => { for (const src of metricSources) for (const key of keys) { const value=number(src?.[key]); if (value !== null) return value; } return null; };
    const current=metric('volume','traded_volume','session_volume','traded_qty');
    let rvol=metric('session_relative_volume','recent_volume_vs_base','relative_volume','rvol20','rvol');
    let dod=metric('volume_change_pct','volume_vs_previous_pct','volume_dod_pct');
    const previous=metric('previous_volume','prev_volume','previous_session_volume');
    if (dod===null && current!==null && previous!==null && previous>0) dod=(current/previous-1)*100;
    if (rvol===null && state.candles?.length) { const vols=state.candles.map(row=>number(row.volume)).filter(v=>v!==null&&v>=0); if (vols.length>=20) { const now=vols[vols.length-1], base=vols.slice(Math.max(0,vols.length-21),-1); const avg=base.length?base.reduce((a,b)=>a+b,0)/base.length:null; if(avg&&avg>0) rvol=now/avg; const dailyLike=/^(1D|1W|1M)$/i.test(text(state.interval)); if(dod===null&&dailyLike&&vols.length>=2&&vols[vols.length-2]>0) dod=(now/vols[vols.length-2]-1)*100; } }
    const deliveryPct=metric('nse_delivery_pct','delivery_pct','delivery_percentage');
    const parts=[]; if(current!==null) parts.push(formatCompactVolume(current)); if(dod!==null) parts.push(`${dod>=0?'▲':'▼'}${Math.abs(dod).toFixed(0)}% D/D`); if(rvol!==null&&rvol>0) parts.push(`RVOL ${rvol.toFixed(2)}×`); if(deliveryPct!==null&&deliveryPct>0) parts.push(`Del ${deliveryPct.toFixed(0)}%`);
    const labelValue=rvol!==null?(rvol>=1.5?'SURGING':rvol>=1.15?'STRONG':rvol>=.8?'NORMAL':'WEAK'):dod!==null?(dod>=35?'STRONG':dod<=-30?'WEAK':'NORMAL'):'WARMING'; parts.push(labelValue);
    return {text:parts.join(' · '), tone:labelValue==='SURGING'||labelValue==='STRONG'?'positive':labelValue==='WEAK'?'warning':'', label:labelValue};
  }
  function formatCompactVolume(value) { const v=number(value); if(v===null)return '—'; if(v>=10000000)return `${formatNumber(v/10000000,2)}Cr`; if(v>=100000)return `${formatNumber(v/100000,1)}L`; if(v>=1000)return `${formatNumber(v/1000,1)}K`; return formatNumber(v,0); }

  function selectedOperatingLevels(payload) {
    const tfByInterval = {
      '1minute':'1m','3minute':'3m','5minute':'5m','15minute':'15m','30minute':'30m',
      '60minute':'1H','240minute':'4H','day':'1D','week':'1W','month':'1M'
    };
    const timeframe = tfByInterval[state.interval] || '';
    const levels = payload?.levels_by_timeframe?.[timeframe] || {};
    return {
      timeframe,
      support: number(levels?.support),
      resistance: number(levels?.resistance),
      roleState: text(levels?.current_role_state),
      crossed: rows(levels?.crossed_levels_pending_confirmation),
    };
  }

  function renderQuoteStats(payload, quote) {
    const ltp = positivePrice(pick(quote,'ltp','last_price','close'));
    const previous = positivePrice(pick(quote,'previous_close','prev_close')) ?? positivePrice(pick(payload?.selected_quote,'previous_close','prev_close'));
    const change = number(pick(quote,'change_pct','pChange')) ?? (ltp !== null && previous ? (ltp / previous - 1) * 100 : null);
    const motion = livePriceMotion(`stock:${state.symbol}`, ltp);
    const hero = $('stockReportHeading');
    if (hero) {
      hero.classList.remove('positive','negative','neutral');
      hero.classList.add(change > 0 ? 'positive' : change < 0 ? 'negative' : 'neutral');
    }
    $('quoteStats').classList.remove('tick-up','tick-down');
    if (motion) { void $('quoteStats').offsetWidth; $('quoteStats').classList.add(motion); }
    const absChange = ltp !== null && previous !== null ? ltp - previous : null;
    const volumeIntel = volumeParticipationIntel(payload, quote);
    const operating = selectedOperatingLevels(payload);
    const srValue = operating.support === null && operating.resistance === null
      ? 'Unavailable'
      : `${operating.support === null ? '—' : formatNumber(operating.support,2)} / ${operating.resistance === null ? '—' : formatNumber(operating.resistance,2)}`;
    const crossedDetail = operating.crossed.length
      ? `${operating.crossed.length} crossed historical level${operating.crossed.length===1?'':'s'} pending completed-candle confirmation`
      : 'Completed-candle role authority · live-price reconciled';
    renderStats($('quoteStats'), [
      {label:quote?.display_only ? 'Verified close' : 'LTP', value:ltp === null ? 'Unavailable' : money(ltp), tone:change > 0 ? 'positive' : change < 0 ? 'negative' : '', detail:quote?.display_only ? 'Display only · not execution authority' : ''},
      {label:'Session', value:`${pct(change)}${absChange === null ? '' : ` (${absChange >= 0 ? '+' : ''}${money(absChange)})`}`, tone:change > 0 ? 'positive' : change < 0 ? 'negative' : ''},
      {label:`Operating S / R · ${operating.timeframe || 'TF'}`, value:srValue, detail:crossedDetail},
      {label:'Snapshot', value:label(payload.state || payload.selected_stock_snapshot?.quality_state), detail:compactTime(sourceTime(quote) || payload.selected_stock_snapshot?.as_of)},
      {label:'Volume intel', value:volumeIntel.text, tone:volumeIntel.tone},
    ]);
  }

  const mtfOrder = ['1m','3m','5m','15m','30m','1H','4H','1D','1W','1M'];
  function mtfRowMap(mtf) {
    const map = new Map();
    for (const row of rows(mtf)) {
      const key = text(pick(row,'tf','timeframe','label')).trim();
      if (key) map.set(key.toUpperCase(), row);
    }
    return map;
  }
  function mtfTone(row) {
    const direction = number(pick(row,'direction','directional_score'));
    const raw = text(pick(row,'state','trend','direction_label')).toLowerCase();
    if (/stale|missing|unavailable|pending/.test(raw)) return 'missing';
    if ((direction !== null && direction > 0) || /bull|up|positive/.test(raw)) return 'bull';
    if ((direction !== null && direction < 0) || /bear|down|negative/.test(raw)) return 'bear';
    return 'neutral';
  }
  function renderMtfSummary(mtf) {
    const map = mtfRowMap(mtf);
    const intervalByTf = Object.fromEntries(intervals);
    let verified = 0;
    $('mtfStrip').innerHTML = mtfOrder.map(tf => {
      const row = map.get(tf.toUpperCase()) || {};
      const tone = mtfTone(row);
      const score = number(pick(row,'composite_score','score','directional_score'));
      const stateLabel = pick(row,'state','trend') || 'Missing';
      const usable = tone !== 'missing' && score !== null;
      if (usable) verified += 1;
      const arrow = tone === 'bull' ? '▲' : tone === 'bear' ? '▼' : tone === 'missing' ? '—' : '•';
      const strength = score === null ? 'Unavailable' : Math.abs(score) >= 67 ? 'Strong' : Math.abs(score) >= 34 ? 'Moderate' : 'Weak';
      const interval = intervalByTf[tf];
      const selected = state.interval === interval ? ' selected' : '';
      return `<button type="button" class="mtf-cell ${tone}${selected}" data-mtf-interval="${esc(interval)}" title="${esc(`${tf} · ${label(stateLabel)} · ${strength} · ${score === null ? 'score unavailable' : `score ${formatNumber(score,1)}`} · ${compactTime(pick(row,'last_completed_at','last_candle','as_of'))}`)}"><b>${esc(tf)}</b><i>${arrow}</i><strong>${esc(strength)}</strong></button>`;
    }).join('');
    $('mtfStatus').textContent = `${verified}/10`;
  }


  function customerNextRequirement(proof = {}) {
    const gates = rows(proof.gates);
    const hard = proof.first_hard_blocker || {};
    if (Object.keys(hard).length) return hard;
    const setup = gates.find(g => /setup\s*\/|setup|trigger/i.test(text(g.gate)) && !['PASS','READY','QUALIFIED','COMPLETE','VERIFIED'].includes(text(g.status).toUpperCase()));
    return setup || proof.first_pending_gate || {};
  }
  function broadMarketContext() {
    const indices = rows(state.workspace?.indices);
    const nifty = indices.find(row => text(pick(row,'display_name','name','symbol')).toUpperCase()==='NIFTY 50');
    if (!nifty) return '';
    const change = number(pick(nifty,'change_pct','pChange'));
    return `NIFTY 50 ${change===null?'verified close':pct(change)}`;
  }
  function decisionProofHtml(proof = {}, payload = {}, quote = {}) {
    const gates = rows(proof.gates);
    const first = proof.first_hard_blocker || {};
    const pending = customerNextRequirement(proof);
    const score = number(proof.evidence_quality_score);
    const tier = text(proof.authority_tier || proof.state || 'EVIDENCE_PENDING').toUpperCase();
    const volumeIntel = volumeParticipationIntel(payload, quote);
    const marketContext = broadMarketContext();
    const statusClass = status => {
      const v = text(status).toUpperCase();
      if (['PASS','READY','QUALIFIED','COMPLETE','VERIFIED','RUNNING','ACTIVE','SELECTED'].includes(v)) return 'pass';
      if (['FAIL','FAILED','BLOCKED','REJECTED','INVALID','INVALIDATED','ERROR','STUCK'].includes(v)) return 'fail';
      if (v === 'CONTEXT') return 'context';
      if (['WAITING','WARN','WARNING','PARTIAL','DEFERRED','PENDING','RECOVERING','NO_PROGRESS','WARMING','UNAVAILABLE'].includes(v)) return 'warn';
      return 'neutral';
    };
    const tierClass = /FINAL_SELECTED|EVIDENCE_READY/.test(tier) ? 'pass' : /REJECT/.test(tier) ? 'fail' : /BUILDING|PENDING/.test(tier) ? 'warn' : 'neutral';
    if (!gates.length) return '<div class="decision-proof-empty">Decision evidence is still materialising.</div>';
    const hiddenDownstream = gates.filter(g => ['NOT_APPLICABLE','NOT_REQUIRED'].includes(text(g.status).toUpperCase()));
    const customerGates = gates.filter(g => !['NOT_APPLICABLE','NOT_REQUIRED'].includes(text(g.status).toUpperCase())).map(g => {
      const gateName = text(g.gate);
      const status = text(g.status).toUpperCase();
      let displayStatus = status;
      let detail = compactReason(g.actual ?? g.reason ?? 'Evidence pending');
      if (status === 'UNAVAILABLE' && /liquidity|participation/i.test(gateName) && volumeIntel.text) {
        displayStatus = 'CONTEXT'; detail = `${volumeIntel.text} · execution-liquidity qualification pending`;
      } else if (status === 'UNAVAILABLE' && /NSE delivery|delivery \/ volume/i.test(gateName) && volumeIntel.text) {
        displayStatus = 'CONTEXT'; detail = `${volumeIntel.text}${/Del /i.test(volumeIntel.text)?'':' · official delivery % pending'}`;
      } else if (status === 'UNAVAILABLE' && /market|index|sector/i.test(gateName) && marketContext) {
        displayStatus = 'CONTEXT'; detail = `${marketContext} · sector/regime qualification pending`;
      }
      return {...g, displayStatus, detail};
    });
    const chain = customerGates.map(g => `<div class="proof-gate ${statusClass(g.displayStatus)}" data-authority-status="${esc(text(g.status).toUpperCase())}"><span>${esc(g.gate || 'Gate')}</span><b>${esc(g.displayStatus === 'CONTEXT' ? 'Context' : label(g.displayStatus || 'WAITING'))}</b><small>${esc(g.detail)}</small></div>`).join('');
    const downstream = hiddenDownstream.length ? `<div class="proof-downstream"><b>Downstream after setup</b><span>${esc(`${hiddenDownstream.length} risk/admission checks activate only after valid trade geometry exists.`)}</span></div>` : '';
    let blocker = '';
    if (Object.keys(first).length) blocker = `<div class="proof-blocker fail"><b>Hard blocker</b><span>${esc(first.gate || 'Unknown')} · ${esc(compactReason(first.actual ?? first.reason ?? first.rule ?? 'Unavailable'))}</span></div>`;
    else if (Object.keys(pending).length) blocker = `<div class="proof-blocker warn"><b>Next requirement</b><span>${esc(pending.gate || 'Evidence')} · ${esc(compactReason(pending.actual ?? pending.reason ?? pending.rule ?? 'Pending'))}</span></div>`;
    else blocker = `<div class="proof-blocker pass"><b>Admission evidence</b><span>No unresolved hard blocker or evidence wait in this materialized proof.</span></div>`;
    const scoreText = score === null ? 'Evidence quality —' : `Evidence quality ${formatNumber(score,0)}%`;
    return `<div class="decision-proof ${tierClass}"><div class="proof-head"><b>Decision Proof</b><div class="proof-authority"><strong class="proof-tier ${tierClass}">${esc(label(tier))}</strong><span>${esc(scoreText)}</span><span>${esc(label(proof.final_action || 'NO-TRADE'))}</span></div></div><div class="proof-chain">${chain}</div>${downstream}${blocker}</div>`;
  }

  function renderStock(payload) {
    if (!payloadIdentityMatches(payload, state.symbol)) {
      state.stock = null;
      $('reportSubtitle').textContent = `Stock identity mismatch for ${state.symbol}; stale decision data suppressed.`;
      setStatePill($('quoteState'), 'unavailable', 'Identity mismatch');
      return false;
    }
    const instrument = payload.instrument || {};
    const trust = payload.trust || state.workspace?.trust || {};
    renderTrustStrip(trust);
    const trustBlocked = trustBlocksAdmission(trust);
    const quote = payload.display_quote || payload.selected_quote || payload.quote || {};
    const active = activeDeskProjection(payload);
    const map = active.tradeMap;
    const decision = active.decision;
    const fresh = freshness(pick(quote,'freshness_state','freshness'), sourceTime(quote), state.workspace?.market_open);
    $('reportTitle').textContent = `${payload.symbol || state.symbol}`;
    $('reportSubtitle').textContent = `${pick(instrument,'display_name','name') || payload.symbol || state.symbol} · ${pick(instrument,'exchange','segment') || 'Exchange unavailable'} · ${text(instrument.instrument_key) || 'identity warming'}`;
    setStatePill($('quoteState'), fresh.state, fresh.label);
    renderQuoteStats(payload, quote);
    renderPriceChangeStrip(payload, quote);
    renderDeskDecisions(payload);
    const truth = payload.selected_stock_truth || {};
    const hasDecision = Object.keys(decision || {}).length > 0;
    const mapState = text(map.state || decision.lifecycle_state || decision.canonical_state || decision.status || (hasDecision ? 'UNAVAILABLE' : truth.decision_status || truth.data_status || 'UNAVAILABLE')).toUpperCase();
    const noSetupReady = !hasDecision && /READY|PARTIAL|WARM|UNAVAILABLE/.test(mapState);
    const decisionTone = trustBlocked ? 'unavailable' : mapState === 'FINAL' || mapState === 'OPENED' ? 'ready' : noSetupReady || /RESEARCH|WATCH|VALIDAT|PARTIAL|QUOTE_PENDING|WARM/.test(mapState) ? 'warming' : /REJECT|INVALID|BLOCK/.test(mapState) ? 'unavailable' : 'neutral';
    setStatePill($('decisionState'), decisionTone, trustBlocked ? 'Do not trust · system blocked' : `${label(state.stockMode)} · ${hasDecision ? label(mapState) : 'No admitted setup'}`);
    const decisionPanel = $('decisionState')?.closest('.compact-decision-panel');
    if (decisionPanel) {
      decisionPanel.classList.remove('tone-ready','tone-warming','tone-blocked');
      decisionPanel.classList.add(decisionTone === 'ready' ? 'tone-ready' : decisionTone === 'warming' ? 'tone-warming' : 'tone-blocked');
    }
    const decisionRank = number(pick(decision,'rank_score','evidence_score')) ?? number(pick(decision?.trader_explanation || {},'score'));
    const positive = value => { const parsed=number(value); return parsed !== null && parsed > 0 ? parsed : null; };
    const side = pick(map,'side','direction') || pick(decision,'side','direction');
    const entry = positive(pick(map,'entry') ?? pick(decision,'entry'));
    const target = positive(pick(map,'target_1','target') ?? pick(decision,'target','t1'));
    const stop = positive(pick(map,'stop') ?? pick(decision,'stop','sl'));
    const rr = positive(pick(map,'room_rr','rr') ?? pick(decision,'reward_risk','rr'));
    const hasAuthorizedGeometry = entry !== null && target !== null && stop !== null;
    const geometryText = value => value === null ? (['FINAL','OPEN','OPENED'].includes(mapState) ? 'Unavailable' : hasDecision ? 'Pending' : 'Not authorized') : money(value);
    const rawAction = text(pick(decision,'display_action','current_action','management_action','decision','action')).toUpperCase();
    const explicitAction = trustBlocked ? 'NO-TRADE'
      : /REJECT|INVALID|BLOCK/.test(mapState) ? 'REJECT'
      : !hasDecision ? 'NO-TRADE'
      : /SELL|EXIT/.test(rawAction) ? 'SELL'
      : /BUY|ENTER/.test(rawAction) ? 'BUY'
      : /HOLD|CONTINUE/.test(rawAction) || ['OPEN','OPENED'].includes(mapState) ? 'HOLD'
      : mapState === 'FINAL' && text(side).toUpperCase() === 'LONG' ? 'BUY'
      : mapState === 'FINAL' && text(side).toUpperCase() === 'SHORT' ? 'SELL'
      : 'NO-TRADE';
    const proof = payload.decision_proof || active.node?.decision_proof || {};
    const nextGate = customerNextRequirement(proof);
    const nextRequirement = trustBlocked ? (trust.reason || 'Restore trusted customer read path') : Object.keys(nextGate).length ? `${nextGate.gate || 'Evidence'} · ${compactReason(nextGate.actual ?? nextGate.reason ?? nextGate.rule ?? 'Pending')}` : 'Wait for a valid setup/trigger';
    const actionReason = trustBlocked ? `System trust gate: ${trust.reason || 'runtime read path is not trusted'}` : map.block_reason || decision.reason || truth.reason || truth.coverage_message || (explicitAction === 'NO-TRADE' ? 'No setup has passed canonical admission.' : `Canonical ${label(mapState)} state`);
    const tokens = trustBlocked ? [
      ['Desk', label(state.stockMode)],
      ['Action', 'NO-TRADE'],
      ['Status', 'System blocked'],
      ['Why', trust.reason || 'Customer read path not trusted', 'note'],
    ] : hasDecision ? [
      ['Desk', label(state.stockMode)],
      ['Decision', side ? label(side) : label(mapState)],
      ['Entry', geometryText(entry)],
      ['T1', geometryText(target)],
      ['SL', geometryText(stop)],
      ['R:R', rr === null ? '—' : formatNumber(rr,2)],
      ['Rank', decisionRank === null || decisionRank <= 0 ? '—' : formatNumber(decisionRank,0)],
      ['Action', explicitAction],
    ] : [
      ['Desk', label(state.stockMode)],
      ['Action', 'NO-TRADE'],
      ['Status', 'No admitted setup'],
      ['Next', nextRequirement, 'note'],
    ];
    const contextTitle = $('decisionContextTitle');
    if (contextTitle) contextTitle.textContent = trustBlocked ? 'System blocked · no admission' : hasDecision ? `${label(state.stockMode)} · ${explicitAction}` : `${label(state.stockMode)} · Waiting for setup`;
    $('tradeMap').innerHTML = tokens.map(([key,value,klass]) => `<div class="decision-token${klass ? ` ${klass}` : ''}"><small>${esc(key)}</small><b>${esc(value)}</b></div>`).join('');
    if (!hasAuthorizedGeometry && ['FINAL','OPEN','OPENED'].includes(mapState)) {
      $('tradeMap').insertAdjacentHTML('beforeend','<div class="decision-token note"><small>Trust</small><b>Final state exists but complete positive geometry is unavailable; no zero values substituted.</b></div>');
    }
    $('decisionWhy').innerHTML = decisionProofHtml(proof, payload, quote) + decisionExplanationHtml(decision, map, active.node);
    const mtf = rows(payload.mtf_trend);
    renderMtfSummary(mtf);
    const fundamentals = payload.fundamentals || {};
    const secondary = [
      ['Major support', formatNumber(payload.support,2)], ['Major resistance', formatNumber(payload.resistance,2)],
      ['Fundamentals', label(fundamentals.state || (fundamentals.ok ? 'READY' : 'WARMING'))],
      ['Research', label(payload.research_snapshot?.state || 'WARMING')],
      ['Technical source', payload.selected_stock_snapshot?.technical_snapshot_source || 'Materialized technical snapshot'],
      ['Decision ID', pick(decision,'decision_id','signal_id') || 'No canonical decision'],
    ];
    $('secondaryEvidence').innerHTML = secondary.map(([key,value]) => `<div class="secondary-card"><span>${esc(key)}</span><b>${esc(value)}</b></div>`).join('');
    $('stockDiagnostics').textContent = JSON.stringify({instrument, trust, component_states:payload.component_states, selected_stock_truth:payload.selected_stock_truth, decision:{decision_id:decision.decision_id, version:decision.version, state:mapState}, service_version:payload.service_version}, null, 2);
    renderPriceOverlays();
    renderIndicatorReadout();
  }

  async function submitSearch(event) {
    event.preventDefault();
    let query = $('stockQuery').value.trim();
    if (!query) return;
    const mode = $('stockMode').value;
    clearTimeout(suggestTimer);
    state.searchEpoch += 1;
    $('suggestions').hidden = true;
    try {
      const result = await api('/api/search', {method:'POST', body:{q:query, mode}, timeout:1400});
      const match = rows(result.matches)[0];
      if (match) {
        query = rowSymbol(match) || query;
        return openStock(query, mode, pick(match,'instrument_key','provider_instrument_key'));
      }
    } catch {}
    openStock(query, mode);
  }

  let suggestTimer = null;
  function scheduleSuggest() {
    clearTimeout(suggestTimer);
    const query = $('stockQuery').value.trim();
    if (query.length < 2) { $('suggestions').hidden = true; return; }
    const epoch = ++state.searchEpoch;
    suggestTimer = setTimeout(async () => {
      try {
        const payload = await api(`/api/suggest?q=${encodeURIComponent(query)}`, {timeout:900});
        if (epoch !== state.searchEpoch) return;
        const matches = rows(payload.matches);
        $('suggestions').innerHTML = matches.map(row => `<button type="button" data-suggestion="${esc(rowSymbol(row))}" data-instrument-key="${esc(text(pick(row,'instrument_key','provider_instrument_key')))}"><span><b>${esc(rowSymbol(row))}</b> ${esc(pick(row,'display_name','name') || '')}</span><small>${esc(pick(row,'exchange','segment') || '')}</small></button>`).join('') || `<div class="empty">No exact local identity. Press Open report to try the entered symbol.</div>`;
        $('suggestions').hidden = false;
      } catch { if (epoch === state.searchEpoch) $('suggestions').hidden = true; }
    }, 220);
  }

  function modelPaperSectionReady(payload, key) {
    return text(payload?.sections?.[key]?.state).toUpperCase() === 'READY';
  }
  function mergeModelPaperPayload(previous, incoming) {
    if (!previous || !incoming) return incoming || previous;
    const next = {...incoming, sections:{...(incoming.sections || {})}};
    const preserve = (key, field) => {
      const stateName = text(next.sections?.[key]?.state).toUpperCase();
      if ((stateName === 'UNAVAILABLE' || !next.sections?.[key]) && rows(previous?.[field]).length) {
        next[field] = previous[field];
        next.sections[key] = {...(next.sections[key] || {}), state:'STALE_LAST_VERIFIED', retained:true};
      }
    };
    preserve('final_positions','final');
    preserve('research_publications','research');
    if (text(next.sections?.capital?.state).toUpperCase() === 'UNAVAILABLE' && previous.capital) {
      next.capital = previous.capital;
      next.sections.capital = {...next.sections.capital, state:'STALE_LAST_VERIFIED', retained:true};
    }
    const finalRows=rows(next.final), researchRows=rows(next.research);
    next.counts = {
      ...(next.counts || {}),
      final: finalRows.length,
      final_open: finalRows.filter(row=>text(row.status).toUpperCase()==='OPEN').length,
      final_closed: finalRows.filter(row=>text(row.status).toUpperCase()==='CLOSED').length,
      research: researchRows.length,
    };
    if ((next.state === 'UNAVAILABLE' || next.ok !== true) && (finalRows.length || researchRows.length)) {
      next.ok = true; next.state = 'PARTIAL'; next.retained_last_verified = true;
    }
    return next;
  }
  async function loadModelPaper(force = false) {
    if (state.modelPaper && !force) { renderModelPaper(); return state.modelPaper; }
    const previous = state.modelPaper;
    try {
      const incoming = await api('/api/model-portfolio?mode=all&detail=core', {timeout:5000});
      state.modelPaper = mergeModelPaperPayload(previous, incoming);
    } catch (error) {
      const incoming = error.payload || {ok:false,state:'UNAVAILABLE',error:error.message,sections:{}};
      state.modelPaper = mergeModelPaperPayload(previous, incoming);
    }
    renderModelPaper(); return state.modelPaper;
  }
  function modelPaperRowsForBook(payload, book) {
    return rows(book === 'research' ? payload?.research : payload?.final);
  }
  function modelPaperTimestamp(row) {
    return text(row.closed_at || row.opened_at || row.occurred_at || row.updated_at || row.generated_at);
  }
  function modelPaperIsToday(row) {
    const stamp=Date.parse(modelPaperTimestamp(row));
    if(!Number.isFinite(stamp)) return false;
    const day=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Kolkata',year:'numeric',month:'2-digit',day:'2-digit'});
    return day.format(new Date(stamp))===day.format(new Date());
  }
  function modelPaperFilteredRows(payload, book) {
    const data=modelPaperRowsForBook(payload,book);
    const scope=state.modelPaperScope || 'all';
    if(scope==='today') return data.filter(modelPaperIsToday);
    if(scope==='open') return data.filter(row => {
      const status=text(row.status || row.prediction_state || row.trade_map_state).toUpperCase();
      const action=text(row.action).toUpperCase();
      return status==='OPEN' || /OPEN|ACTIVE|MONITOR|RESEARCH_MAP_READY/.test(status) || /HOLD|MONITOR|AWAITING/.test(action);
    });
    return data;
  }
  function renderModelPaper() {
    const payload = state.modelPaper || {};
    const book = state.modelPaperBook === 'research' ? 'research' : 'final';
    const data = modelPaperFilteredRows(payload, book);
    const modelPaperPage=document.querySelector('[data-page-panel="model-paper"]');
    if (modelPaperPage) modelPaperPage.classList.toggle('is-empty-book', data.length===0);
    const counts = payload.counts || {};
    const capital = payload.capital || {};
    const diag = payload.entry_diagnostics || {};
    const overall = text(payload.state || (payload.ok === true ? 'READY' : 'UNAVAILABLE')).toUpperCase();
    const pillTone = overall === 'READY' ? 'ready' : overall === 'PARTIAL' ? 'warming' : 'unavailable';
    setStatePill($('modelPaperState'), pillTone, overall === 'READY' ? 'Canonical · Ready' : overall === 'PARTIAL' ? 'Canonical · Partial' : label(overall));
    const countValue = value => number(value)===null ? '—' : formatNumber(value,0);
    const rp=payload.research_performance || {};
    const researchAcc=number(rp.accuracy_pct);
    const statRows = book === 'research' ? [
      ['Published', countValue(rp.published ?? counts.research), 'warning'], ['Active', countValue(rp.active ?? counts.research_active), 'warning'],
      ['Success', countValue(rp.success ?? counts.research_success), 'positive'], ['Failure', countValue(rp.failure ?? counts.research_failure), 'negative'],
      ['Research acc.', researchAcc===null?'—':`${formatNumber(researchAcc,1)}%`, ''], ['Authority', 'Research only · not Final P&L', '']
    ] : [
      ['Final', countValue(counts.final), 'positive'], ['Open', countValue(counts.final_open), ''], ['Settled', countValue(counts.final_closed), ''],
      ['Research', countValue(counts.research), 'warning'], ['Wallet', number(capital.equity ?? capital.wallet ?? capital.model_wallet)===null ? '—' : money(capital.equity ?? capital.wallet ?? capital.model_wallet), ''],
      ['Authority', payload.model_paper_authority || '—', '']
    ];
    $('modelPaperStats').innerHTML = statRows.map(([k,v,t]) => `<span class="model-paper-stat ${t}"><small>${esc(k)}</small><b>${esc(v)}</b></span>`).join('');

    const finalState=text(payload.sections?.final_positions?.state || 'UNKNOWN').toUpperCase();
    const researchState=text(payload.sections?.research_publications?.state || 'UNKNOWN').toUpperCase();
    const retained=payload.retained_last_verified===true || /STALE_LAST_VERIFIED/.test(finalState+researchState);
    const sourceStrip=$('modelPaperSources');
    if(sourceStrip) sourceStrip.innerHTML = `<span><b>Final book</b> ${esc(label(finalState))}</span><i></i><span><b>Research history</b> ${esc(label(researchState))}</span><i></i><span><b>Scope</b> ${esc(state.modelPaperScope==='all'?'All persisted history':label(state.modelPaperScope))}</span>${retained?'<i></i><span class="warning-text"><b>Last verified rows retained</b></span>':''}`;

    const modes = diag.modes || {};
    const blockers = ['delivery','intraday'].map(mode => {
      const node=modes[mode] || {}; const first=rows(node.top_blockers)[0];
      return `<span><b>${esc(label(mode))}</b> ${esc(label(node.state || 'NO PERSISTED ROWS'))}${first ? ` · ${esc(first.reason)}` : node.explanation ? ` · ${esc(node.explanation)}` : ''}</span>`;
    });
    $('modelPaperBlocker').innerHTML = blockers.join('<i></i>');
    $('modelPaperRows').innerHTML = data.slice(0,250).map(row => {
      const symbol=text(row.symbol).toUpperCase(); const mode=text(row.mode).toLowerCase() || 'delivery';
      const action=pick(row,'action','decision','side') || (row.status==='OPEN'?'HOLD':'—');
      const researchPerf=book==='research'?number(row.research_performance_pct):null; const pnl=number(row.net_pnl); const shownPerf=book==='research'?researchPerf:pnl; const pnlTone=shownPerf===null?'':shownPerf>0?'positive':shownPerf<0?'negative':'';
      const key=text(row.instrument_key || row.provider_instrument_key || row.chart_binding?.instrument_key || '');
      const rowState = text(row.status || row.prediction_state || row.trade_map_state || book).toUpperCase();
      const rowTone = pnl !== null ? (pnl > 0 ? 'semantic-positive' : pnl < 0 ? 'semantic-negative' : 'semantic-neutral') : /OPEN|READY|ACTIVE/.test(rowState) ? 'semantic-positive' : /REJECT|FAIL|INVALID/.test(rowState) ? 'semantic-negative' : /PENDING|WAIT|RESEARCH/.test(rowState) ? 'semantic-warning' : 'semantic-neutral';
      return `<tr class="${rowTone}"><td><button class="stock-link" data-open-stock="${esc(symbol)}" data-instrument-key="${esc(key)}" data-mode="${esc(mode)}">${esc(symbol)}</button></td>`+
        `<td>${esc(label(mode))}</td><td>${esc(label(row.status || row.prediction_state || row.trade_map_state || book))}</td><td><span class="decision-word ${/BUY|HOLD|CONTINUE|OPEN/.test(text(action).toUpperCase())?'buy':/SELL|EXIT|LOSS|REJECT/.test(text(action).toUpperCase())?'sell':'watch'}">${esc(label(action))}</span></td>`+
        `<td>${number(row.entry)===null?'—':esc(formatNumber(row.entry,2))}</td><td>${number(row.target)===null?'—':esc(formatNumber(row.target,2))}</td><td>${number(row.active_stop ?? row.original_stop)===null?'—':esc(formatNumber(row.active_stop ?? row.original_stop,2))}</td>`+
        `<td>${number(row.quantity)===null?'—':esc(formatNumber(row.quantity,0))}</td><td><span class="change-cell ${pnlTone}">${shownPerf===null?'—':book==='research'?`${shownPerf>0?'+':''}${esc(formatNumber(shownPerf,2))}%`:esc(money(shownPerf))}</span></td><td>${esc(label(row.signal_outcome || row.economic_outcome || row.outcome || row.accuracy_state || 'PENDING'))}</td><td>${esc(book==='research'?(compactTime(row.first_seen_at || modelPaperTimestamp(row)) || '—'):(ageLabel(row) || compactTime(modelPaperTimestamp(row)) || '—'))}</td></tr>`;
    }).join('') || emptyRow(11, book === 'final' ? 'No governed Final Model Paper rows match this view. Persisted history is retained when available.' : 'No governed Research publications match this view. Persisted history is retained when available.');
    applyStoredSort($('modelPaperRows'));
  }
  async function advanceResearchLifecycle() {
    const button=$('advanceLifecycle'); const status=$('lifecycleAdvanceState'); if(!button || !status) return;
    button.disabled=true; status.hidden=false; status.className='lifecycle-advance-state'; status.textContent='Advancing existing governed lifecycle…';
    try {
      const result=await api('/api/operations/action',{method:'POST',body:{action:'advance_full_lifecycle',reason:'research_page_advance'},timeout:5000});
      status.classList.add('ready'); status.textContent=`${label(result.state || 'SCHEDULED')} · running in Progress & Proof; this page will not wait on WFA/settlement.`;
      setTimeout(()=>loadOperationsControl({silent:true}),500);
      setTimeout(()=>{ state.modelPaper=null; state.performance=null; loadResearch(); },1200);
    } catch(error) {
      status.classList.add('blocked'); status.textContent=`Lifecycle blocked: ${error.payload?.reason || error.payload?.error || error.message}`;
    } finally { button.disabled=false; }
  }
  async function loadPerformance(force = false) {
    if (state.performance && !force) return state.performance;
    try {
      state.performance = await api('/api/performance?mode=all', {timeout:2500});
    } catch (error) {
      state.performance = error.payload || {ok:false,state:'UNAVAILABLE',error:error.message};
    }
    return state.performance;
  }
  function accuracyMetrics() {
    const payload = state.performance || {};
    const economics = payload.model_paper_performance || payload.performance_evidence?.model_paper_performance || {};
    const period = economics.filters?.all?.[state.metricMode];
    const lifecycle = payload.canonical_lifecycle || payload.performance_evidence?.signal_accuracy || {};
    return period || (state.metricMode === 'all' ? lifecycle.overall : lifecycle.by_desk?.[state.metricMode]) || economics.accuracy || null;
  }
  function settledLifecycleRows({performanceOnly=false} = {}) {
    const payload = state.performance || {};
    const lifecycle = payload.canonical_lifecycle || payload.performance_evidence?.signal_accuracy || {};
    return rows(lifecycle.records).filter(row => performanceOnly ? row.performance_eligible === true : row.accuracy_eligible === true);
  }
  function numericSum(input, key) {
    const values=rows(input).map(row=>number(row?.[key])).filter(value=>value!==null);
    return values.length ? values.reduce((sum,value)=>sum+value,0) : null;
  }
  function finalDecisionSummary(metric) {
    const payload=state.performance || {};
    const lifecycle=payload.canonical_lifecycle || payload.performance_evidence?.signal_accuracy || {};
    const records=rows(lifecycle.records).filter(row=>state.metricMode==='all' || text(row.mode).toLowerCase()===state.metricMode);
    const pendingRecords=records.filter(row=>!/SUCCESS|FAILURE/.test(text(row.signal_outcome).toUpperCase()));
    return {
      total:number(pick(metric||{},'total_final','total','decisions')) ?? (records.length || number(pick(metric||{},'accuracy_denominator','accuracy_eligible','scored_trades'))),
      wins:number(pick(metric||{},'success','wins')), losses:number(pick(metric||{},'failure','losses')),
      pending:number(pick(metric||{},'pending','open')) ?? pendingRecords.length,
      gross:numericSum(records,'gross_pnl'), costs:numericSum(records,'costs'), net:numericSum(records,'net_pnl'),
    };
  }
  function settlementAction(row) {
    return text(pick(row,'side','action','decision','final_action') || '—').toUpperCase();
  }
  function settlementSymbolCell(row) {
    const symbol=text(row.symbol || row.trading_symbol || '—');
    const key=text(row.instrument_key || row.instrument_id || '');
    const mode=text(row.mode || 'delivery').toLowerCase()==='intraday'?'intraday':'delivery';
    return symbol==='—' ? '—' : `<button class="stock-link compact-stock-link" data-open-stock="${esc(symbol)}" data-mode="${esc(mode)}" data-instrument-key="${esc(key)}">${esc(symbol)}</button>`;
  }
  function settlementTone(row) {
    const signal=text(row.signal_outcome).toUpperCase();
    const pnl=number(row.net_pnl);
    return signal==='SUCCESS' || (pnl!==null && pnl>0) ? 'positive-text' : signal==='FAILURE' || (pnl!==null && pnl<0) ? 'negative-text' : 'warning-text';
  }
  function renderSettlementStrip(targetId, {performance=false}={}) {
    const payload=state.performance || {};
    const lifecycle=payload.canonical_lifecycle || payload.performance_evidence?.signal_accuracy || {};
    const overall=lifecycle.overall || {};
    const parity=payload.settlement_parity || payload.performance_evidence?.settlement_parity || {};
    const parityState=parity.ok===true ? 'PARITY PASS' : text(parity.state || 'PARITY PENDING').replaceAll('_',' ');
    const excluded=number(overall.excluded_incomplete);
    const items = performance ? [
      ['Eligible', pick(overall,'performance_eligible')], ['Settled', pick(overall,'settled')], ['Costs', payload.model_paper_performance?.filters?.all?.all?.costs], ['Parity', parityState]
    ] : [
      ['Eligible', pick(overall,'accuracy_eligible')], ['Wins', pick(overall,'wins')], ['Losses', pick(overall,'losses')], ['Excluded', excluded], ['Parity', parityState]
    ];
    const node=$(targetId); if(!node) return;
    node.innerHTML=items.map(([k,v])=>`<span><small>${esc(k)}</small><b>${typeof v==='number'?esc(formatNumber(v,k==='Costs'?2:0)):esc(v ?? '—')}</b></span>`).join('');
  }
  function renderAccuracyRows() {
    const node=$('accuracyRows'); if(!node) return;
    const filtered=settledLifecycleRows().filter(row => state.metricMode==='all' || text(row.mode).toLowerCase()===state.metricMode).slice(0,150);
    node.innerHTML=filtered.map(row=>`<tr class="${settlementTone(row).replace('-text','').replace('positive','semantic-positive').replace('negative','semantic-negative').replace('warning','semantic-warning')}"><td>${settlementSymbolCell(row)}</td><td>${esc(label(row.mode||'—'))}</td><td>${esc(label(settlementAction(row)))}</td><td><span class="${settlementTone(row)}">${esc(label(row.signal_outcome||'—'))}</span></td><td>${number(row.entry)===null?'—':esc(formatNumber(row.entry,2))}</td><td>${number(row.exit)===null?'—':esc(formatNumber(row.exit,2))}</td><td><span class="${settlementTone(row)}">${number(row.net_pnl)===null?'—':esc(money(row.net_pnl))}</span></td><td>${number(row.costs)===null?'—':esc(money(row.costs))}</td><td>${number(row.realized_r)===null?'—':esc(formatNumber(row.realized_r,2))}</td><td>${esc(compactTime(row.closed_at||row.settled_at||row.updated_at)||'—')}</td></tr>`).join('') || emptyRow(10,'No accuracy-eligible settled decisions for this desk yet.');
  }
  function renderPerformanceRows() {
    const node=$('performanceRows'); if(!node) return;
    const filtered=settledLifecycleRows({performanceOnly:true}).filter(row => state.performanceMode==='all' || text(row.mode).toLowerCase()===state.performanceMode).slice(0,150);
    node.innerHTML=filtered.map(row=>`<tr class="${settlementTone(row).replace('-text','').replace('positive','semantic-positive').replace('negative','semantic-negative').replace('warning','semantic-warning')}"><td>${settlementSymbolCell(row)}</td><td>${esc(label(row.mode||'—'))}</td><td>${esc(label(settlementAction(row)))}</td><td><span class="${settlementTone(row)}">${esc(label(row.economic_outcome||row.signal_outcome||'—'))}</span></td><td>${number(row.quantity)===null?'—':esc(formatNumber(row.quantity,0))}</td><td>${number(row.gross_pnl)===null?'—':esc(money(row.gross_pnl))}</td><td>${number(row.costs)===null?'—':esc(money(row.costs))}</td><td><span class="${settlementTone(row)}">${number(row.net_pnl)===null?'—':esc(money(row.net_pnl))}</span></td><td>${number(row.realized_r)===null?'—':esc(formatNumber(row.realized_r,2))}</td><td>${esc(compactTime(row.closed_at||row.settled_at||row.updated_at)||'—')}</td></tr>`).join('') || emptyRow(10,'No performance-eligible settled Model Paper positions for this desk yet.');
  }
  function renderAccuracy() {
    const payload = state.performance || {};
    const metric = accuracyMetrics();
    const ready = Boolean(metric);
    setStatePill($('accuracyState'), ready ? 'ready' : text(payload.state).toLowerCase() === 'warming' ? 'warming' : 'unavailable', ready ? `As of ${compactTime(payload.materialized_at || payload.model_paper_performance?.as_of)}` : label(payload.state || 'Unavailable'));
    const finalSummary=finalDecisionSummary(metric);
    const accuracyPage=document.querySelector('[data-page-panel="accuracy"]');
    if (accuracyPage) accuracyPage.classList.toggle('is-empty-accuracy', settledLifecycleRows().filter(row => state.metricMode==='all' || text(row.mode).toLowerCase()===state.metricMode).length===0);
    renderStats($('accuracyStats'), [
      {label:'Total Final', value:metric && finalSummary.total!==null ? formatNumber(finalSummary.total,0) : 'Unavailable'},
      {label:'Wins', value:metric && finalSummary.wins!==null ? formatNumber(finalSummary.wins,0) : 'Unavailable', tone:'positive'},
      {label:'Losses', value:metric && finalSummary.losses!==null ? formatNumber(finalSummary.losses,0) : 'Unavailable', tone:'negative'},
      {label:'Pending', value:metric ? formatNumber(finalSummary.pending,0) : 'Unavailable', tone:'warning'},
      {label:'Accuracy', value:metric && number(metric.accuracy_pct) !== null ? `${formatNumber(metric.accuracy_pct,2)}%` : 'Insufficient sample', tone:metric && number(metric.accuracy_pct) !== null ? 'positive' : 'warning'},
      {label:'Gross P&L', value:finalSummary.gross===null?'Unavailable':money(finalSummary.gross)},
      {label:'Costs', value:finalSummary.costs===null?'Unavailable':money(finalSummary.costs), tone:'warning'},
      {label:'Net P&L', value:finalSummary.net===null?'Unavailable':money(finalSummary.net), tone:finalSummary.net===null?'':finalSummary.net>0?'positive':finalSummary.net<0?'negative':''},
    ]);
    $('accuracyContract').innerHTML = `<p><b>Population:</b> settled, geometry-complete canonical decisions with Model Paper settlement lineage.</p><p><b>Formula:</b> success / (success + failure). Neutral and unscorable outcomes are visible but excluded.</p><p><b>Unavailable is not zero:</b> missing authority or no eligible sample remains explicit.</p>`;
    const lifecycle = payload.canonical_lifecycle || payload.performance_evidence?.signal_accuracy || {};
    const blockerRows = state.metricMode === 'all' ? [...rows(lifecycle.by_desk?.delivery?.blockers), ...rows(lifecycle.by_desk?.intraday?.blockers)] : rows(lifecycle.by_desk?.[state.metricMode]?.blockers);
    const reasonCounts = new Map(); for (const row of blockerRows) { const key=text(row.field||'incomplete'); reasonCounts.set(key,(reasonCounts.get(key)||0)+(number(row.count)||0)); }
    const exclusions = [...reasonCounts.entries()].sort((a,b)=>b[1]-a[1]).slice(0,6);
    const exclusionHtml = exclusions.length ? `<p><b>Excluded ${formatNumber(number(lifecycle.overall?.excluded_incomplete)||0,0)}:</b> ${exclusions.map(([k,v])=>`${esc(label(k))} ${formatNumber(v,0)}`).join(' · ')}</p>` : '<p><b>Exclusions:</b> no incomplete-reason breakdown in the current projection.</p>';
    $('accuracyReconciliation').innerHTML = ready ? `<p><b>Projection:</b> ${esc(payload.read_state || payload.state || 'READY')}</p><p><b>Age:</b> ${number(payload.snapshot_age_sec) === null ? 'Unavailable' : `${formatNumber(payload.snapshot_age_sec,1)} seconds`}</p><p><b>Authority:</b> ${esc(payload.accuracy_authority || payload.performance_evidence?.lanes?.signal_accuracy?.authority || 'Canonical lifecycle')}</p>${exclusionHtml}` : `<p>${esc(payload.error || payload.policy || 'Accuracy projection is warming. No compatibility outcome was substituted.')}</p>${exclusionHtml}`;
    renderSettlementStrip('accuracySettlementStrip');
    renderAccuracyRows();
  }
  function performanceMetric() {
    const payload = state.performance || {};
    const economics = payload.model_paper_performance || payload.performance_evidence?.model_paper_performance || {};
    return {economics, metric:economics.filters?.[state.performancePeriod]?.[state.performanceMode] || null};
  }
  function renderPerformance() {
    const payload = state.performance || {};
    const {economics, metric} = performanceMetric();
    const ready = Boolean(metric);
    setStatePill($('performanceState'), ready ? 'ready' : text(payload.state).toLowerCase() === 'warming' ? 'warming' : 'unavailable', ready ? `${economics.units || 'INR net of costs'} · ${compactTime(economics.as_of)}` : label(payload.state || 'Unavailable'));
    const open=economics.open_mtm || {};
    const modelRows=settledLifecycleRows({performanceOnly:true}).filter(row=>state.performanceMode==='all' || text(row.mode).toLowerCase()===state.performanceMode);
    const accuracyPage=document.querySelector('[data-page-panel="accuracy"]');
    if (accuracyPage) accuracyPage.classList.toggle('is-empty-performance', modelRows.length===0 && (number(open.positions)??0)===0);
    const wins=number(pick(metric||{},'wins','success')) ?? modelRows.filter(row=>text(row.economic_outcome||row.signal_outcome).toUpperCase()==='SUCCESS').length;
    const losses=number(pick(metric||{},'losses','failure')) ?? modelRows.filter(row=>text(row.economic_outcome||row.signal_outcome).toUpperCase()==='FAILURE').length;
    renderStats($('performanceStats'), [
      {label:'Open positions', value:number(open.positions)===null?'Unavailable':formatNumber(open.positions,0), tone:'warning'},
      {label:'Closed', value:metric ? formatNumber(pick(metric,'settled_trades','performance_eligible_trades'),0) : 'Unavailable'},
      {label:'Wins', value:metric ? formatNumber(wins,0) : 'Unavailable', tone:'positive'},
      {label:'Losses', value:metric ? formatNumber(losses,0) : 'Unavailable', tone:'negative'},
      {label:'Gross P&L', value:metric ? money(metric.gross_pnl) : 'Unavailable'},
      {label:'Charges', value:metric ? money(metric.costs) : 'Unavailable', tone:'warning'},
      {label:'Net P&L', value:metric ? money(metric.net_pnl) : 'Unavailable', tone:metric && number(metric.net_pnl) > 0 ? 'positive' : metric && number(metric.net_pnl) < 0 ? 'negative' : ''},
      {label:'Max drawdown', value:metric ? money(metric.max_drawdown) : 'Unavailable', tone:metric && number(metric.max_drawdown) < 0 ? 'negative' : ''},
    ]);
    $('performanceContract').innerHTML = ready ? `<p><b>Authority:</b> ${esc(economics.authority)}</p><p><b>Book:</b> Model Paper only. Gross − itemized India cash costs = net.</p><p><b>Scope:</b> settled governed positions only; research counterfactual and legacy price points excluded.</p>` : `<p>${esc(payload.error || payload.policy || 'Model Paper economics are warming. Price-point continuity is not shown as currency.')}</p>`;
    $('openPositionSummary').innerHTML = `<p><b>Open now:</b> ${number(open.positions) === null ? 'Unavailable' : formatNumber(open.positions,0)}</p><p><b>Open MTM:</b> ${number(open.net_pnl) === null ? 'Unavailable' : money(open.net_pnl)}</p><p>Open positions are excluded from settled Performance and Accuracy.</p>`;
    renderSettlementStrip('performanceSettlementStrip',{performance:true});
    renderPerformanceRows();
  }

  async function loadResearch() {
    const tasks = [
      api('/api/trader-research', {timeout:1800}),
      api('/api/quant-research-plane', {timeout:2500}),
      api('/api/forward-evidence-clock', {timeout:1800}),
      api('/api/model-portfolio?mode=all&detail=core', {timeout:5000}),
    ];
    const [registry, plane, forward, researchBook] = await Promise.allSettled(tasks);
    state.research = registry.status === 'fulfilled' ? registry.value : {ok:false,error:registry.reason?.message || 'Registry unavailable'};
    state.researchPlane = plane.status === 'fulfilled' ? plane.value : {ok:false,error:plane.reason?.message || 'Research continuity unavailable'};
    state.forwardEvidence = forward.status === 'fulfilled' ? forward.value : {ok:false,error:forward.reason?.message || 'Forward evidence unavailable'};
    if (researchBook.status === 'fulfilled') state.modelPaper = mergeModelPaperPayload(state.modelPaper, researchBook.value);
    renderResearch();
  }
  function researchLifecycleCard(desk, node = {}) {
    const folds = number(node.walk_forward_folds) ?? 0;
    const historicalFolds = number(node.historical_wfa_folds) ?? 0;
    const weight = number(node.production_weight) ?? 0;
    const lifecycle = text(node.lifecycle_state || node.state || 'NO PUBLICATION').toUpperCase();
    const tone = /ACTIVE_PRODUCTION|APPROVED/.test(lifecycle) ? 'positive' : /REJECT|BLOCK|MISSING/.test(lifecycle) ? 'negative' : 'warning';
    const model = node.model_id || 'No governed model publication';
    return `<article class="research-life-card ${tone}"><div><span>${esc(label(desk))}</span><b>${esc(label(lifecycle))}</b></div><strong>${esc(`Historical model WF ${formatNumber(folds,0)} · Forward selector WF ${formatNumber(historicalFolds,0)}`)}</strong><small>${esc(model)}</small><footer><span>Prod influence ${esc(formatNumber(weight * 100,1))}%</span><span>${node.historical_wfa_state ? esc(`Forward selector ${label(node.historical_wfa_state)}`) : node.created_at ? esc(`Historical publication ${compactTime(node.created_at)}`) : 'Historical PIT publication pending'}</span></footer></article>`;
  }
  // Legacy invariant terminology retained for inherited validation only: Published WF and Historical capital WFA distinguish governed publication from diagnostic selector replay.
  function renderResearch() {
    const payload = state.research || {};
    const plane = state.researchPlane || {};
    const persistent = state.modelPaper || {};
    const persistentRows = rows(persistent.research);
    const rp = persistent.research_performance || {};
    const rpAcc = number(pick(rp,'success_pct','accuracy_pct'));
    const rpReturn = number(rp.average_return_pct);
    const rpR = number(rp.average_r);
    const rstats = $('researchCandidateStats');
    if (rstats) rstats.innerHTML = [
      ['Total', formatNumber(rp.total ?? rp.published ?? persistentRows.length,0), 'warning'],
      ['Open', formatNumber(rp.open ?? rp.active ?? persistent.counts?.research_active ?? 0,0), 'warning'],
      ['Settled', formatNumber(rp.settled ?? 0,0), ''],
      ['Successful', formatNumber(rp.successful ?? rp.success ?? persistent.counts?.research_success ?? 0,0), 'positive'],
      ['Failed', formatNumber(rp.failed ?? rp.failure ?? persistent.counts?.research_failure ?? 0,0), 'negative'],
      ['Expired / rejected', formatNumber(rp.expired_rejected ?? ((number(rp.expired)||0)+(number(rp.rejected)||0)),0), 'warning'],
      ['Success %', rpAcc===null?'—':`${formatNumber(rpAcc,1)}%`, rpAcc===null?'warning':'positive'],
      ['Avg return', rpReturn===null?'—':`${rpReturn>0?'+':''}${formatNumber(rpReturn,2)}%`, rpReturn===null?'':rpReturn>0?'positive':rpReturn<0?'negative':''],
      ['Avg R', rpR===null?'—':`${rpR>0?'+':''}${formatNumber(rpR,2)}R`, rpR===null?'':rpR>0?'positive':rpR<0?'negative':''],
      ['Final / Paper impact', 'NONE', '']
    ].map(([k,v,t])=>`<span class="model-paper-stat ${t}"><small>${esc(k)}</small><b>${esc(v)}</b></span>`).join('');
    const filteredResearch = persistentRows.filter(row=>{
      const lifecycle=text(row.research_lifecycle || row.status).toUpperCase();
      const stage=text(row.research_stage || row.result || lifecycle).toUpperCase();
      const outcome=text(row.signal_outcome || row.outcome || 'PENDING').toUpperCase();
      if (state.researchScope==='active' && !/ACTIVE|WATCHING/.test(lifecycle)) return false;
      if (state.researchScope==='history' && /ACTIVE|WATCHING/.test(lifecycle)) return false;
      if (state.researchMode!=='all' && text(row.mode).toLowerCase()!==state.researchMode) return false;
      if (state.researchOutcome==='open' && outcome!=='PENDING') return false;
      if (state.researchOutcome==='success' && outcome!=='SUCCESS') return false;
      if (state.researchOutcome==='failure' && outcome!=='FAILURE') return false;
      if (state.researchOutcome==='promoted' && !/PROMOTED/.test(`${stage} ${lifecycle}`)) return false;
      return true;
    });
    if (state.researchFocusCandidate) {
      const focusIndex=filteredResearch.findIndex(row=>text(row.research_candidate_id || row.source_signal_id)===state.researchFocusCandidate);
      if (focusIndex>=0) state.researchPage=Math.floor(focusIndex/state.researchPageSize);
    }
    const pages=Math.max(1,Math.ceil(filteredResearch.length/state.researchPageSize));
    state.researchPage=Math.min(Math.max(0,state.researchPage),pages-1);
    const pageRows=filteredResearch.slice(state.researchPage*state.researchPageSize,(state.researchPage+1)*state.researchPageSize);
    if ($('researchPageState')) $('researchPageState').textContent=filteredResearch.length ? `${state.researchPage+1} / ${pages} · ${filteredResearch.length} candidates` : '0 candidates';
    if ($('researchPrev')) $('researchPrev').disabled=state.researchPage===0;
    if ($('researchNext')) $('researchNext').disabled=state.researchPage>=pages-1;
    const historyHost=$('researchCandidateHistoryRows');
    if(historyHost) historyHost.innerHTML = pageRows.map(row=>{
      const perf=number(row.research_performance_pct); const tone=perf===null?'':perf>0?'positive':perf<0?'negative':'';
      const currentR=number(row.research_current_r);
      const outcome=text(row.signal_outcome || row.outcome || 'PENDING').toUpperCase();
      const lifecycle=text(row.research_lifecycle || row.status || 'RESEARCH_ACTIVE').toUpperCase();
      const stage=text(row.research_stage || row.result || lifecycle).toUpperCase();
      const candidateId=text(row.research_candidate_id || row.source_signal_id);
      const focused=candidateId && candidateId===state.researchFocusCandidate;
      const setup=text(pick(row,'setup','setup_type','setup_name','pattern','strategy_name') || '—');
      const score=number(pick(row,'research_score','rank_score','evidence_score','score'));
      const holding=text(pick(row,'holding_period','target_window','horizon','expected_horizon') || '—');
      const reason=text(pick(row,'latest_reason','waiting_for','blocker','block_reason','reason') || (outcome==='PENDING'?'Monitoring Target / SL':'—'));
      const symbol=text(row.symbol).toUpperCase(), mode=text(row.mode).toLowerCase();
      return `<tr id="research-${esc(candidateId)}" data-research-candidate="${esc(candidateId)}" class="${focused?'research-focused ':''}${outcome==='SUCCESS'?'semantic-positive':outcome==='FAILURE'?'semantic-negative':'semantic-warning'}"><td><button class="stock-link" data-open-stock="${esc(symbol)}" data-mode="${esc(mode)}" data-research-candidate="${esc(candidateId)}">${esc(symbol)}</button></td><td>${esc(label(mode))}</td><td>${score===null?'—':esc(formatNumber(score,1))}</td><td title="${esc(setup)}">${esc(setup)}</td><td>${number(row.entry)===null?'—':esc(formatNumber(row.entry,2))}</td><td>${number(row.ltp)===null?'—':esc(formatNumber(row.ltp,2))}</td><td>${number(row.target)===null?'—':esc(formatNumber(row.target,2))}</td><td>${number(row.original_stop)===null?'—':esc(formatNumber(row.original_stop,2))}</td><td class="${tone}">${perf===null?'—':`${perf>0?'+':''}${esc(formatNumber(perf,2))}%`}</td><td class="${currentR===null?'':currentR>0?'positive':currentR<0?'negative':''}">${currentR===null?'—':`${currentR>0?'+':''}${esc(formatNumber(currentR,2))}R`}</td><td>${esc(compactTime(row.first_seen_at || row.occurred_at) || '—')}</td><td>${esc(candidateAgeLabel(row))}</td><td title="${esc(holding)}">${esc(holding)}</td><td>${esc(label(stage))}</td><td title="${esc(reason)}">${esc(reason)}</td><td>${esc(label(outcome))}</td></tr>`;
    }).join('') || emptyRow(16,'No published Research candidate matches this view. History is retained; change the filters to inspect it.');
    if (state.researchFocusCandidate) requestAnimationFrame(()=>document.querySelector(`[data-research-candidate="${CSS.escape(state.researchFocusCandidate)}"]`)?.scrollIntoView({block:'center'}));
    const lifecycle = plane.model_lifecycle || {};
    const registryOk = payload.ok === true;
    setStatePill($('researchState'), registryOk ? 'ready' : 'unavailable', registryOk ? `Evidence · ${compactTime(payload.as_of)}` : 'Partial');
    renderStats($('researchStats'), [
      {label:'Active methods', value:formatNumber(payload.counts?.active,0), tone:'positive'},
      {label:'Shadow methods', value:formatNumber(payload.counts?.shadow,0), tone:'warning'},
      {label:'Delivery historical WF', value:formatNumber(lifecycle.delivery?.walk_forward_folds ?? 0,0)},
      {label:'Intraday historical WF', value:formatNumber(lifecycle.intraday?.walk_forward_folds ?? 0,0)},
    ]);
    $('researchLifecycle').innerHTML = ['delivery','intraday'].map(desk => researchLifecycleCard(desk, lifecycle[desk] || {})).join('');
    $('researchContinuityNote').innerHTML = plane.ok
      ? `<b>Authority:</b> ${esc(plane.publication_authority?.authority || lifecycle.delivery?.authority || 'Governed research authority')} · <b>Production ML influence:</b> ${plane.production_influence ? 'ACTIVE' : '0% / shadow-only'} · earlier valid publications are displayed from persisted authority.`
      : `<b>Continuity:</b> ${esc(plane.error || plane.state || 'Persisted research authority is not currently readable.')} This preview does not fabricate missing folds.`;
    const pit = state.workspace?.historical_pit || {};
    const forward = state.forwardEvidence || {};
    const arms = forward.by_desk_arm || {};
    const armTruth = desk => {
      const deskArms = arms[desk] || {};
      return deskArms.hybrid || deskArms.quant || deskArms.heuristic || {};
    };
    const deliveryArm = armTruth('delivery'), intradayArm = armTruth('intraday');
    const alphaBlock = (desk, arm) => {
      const n = number(arm.settled_observation_count), bps = number(arm.mean_net_return_bps);
      const tone = bps === null ? 'neutral' : bps > 0 ? 'positive' : bps < 0 ? 'negative' : 'neutral';
      return `<article class="research-truth-card ${tone}"><span>${esc(label(desk))} settled net</span><b>${bps===null?'—':esc(`${bps>=0?'+':''}${formatNumber(bps,1)} bps`)}</b><small>${n===null?'No settled arm sample':`${esc(formatNumber(n,0))} settled · production influence 0%`}</small></article>`;
    };
    const truthHost = $('researchTruthStrip');
    if (truthHost) truthHost.innerHTML = `<article class="research-truth-card ${/RETRY|FAIL|BLOCK/.test(text(pit.state).toUpperCase())?'negative':/YIELD|WAIT/.test(text(pit.state).toUpperCase())?'warning':'info'}"><span>Historical PIT / WFA</span><b>${esc(label(pit.state || 'STARTING'))}</b><small>${esc(`P5 autonomous · min ${formatNumber(pit.min_dates ?? 504,0)} dates${pit.waiting_on?` · ${pit.waiting_on}`:''}`)}</small></article>${alphaBlock('delivery',deliveryArm)}${alphaBlock('intraday',intradayArm)}`;
    $('researchRows').innerHTML = rows(payload.methods).map(row => `<tr><td><strong>${esc(label(row.key))}</strong></td><td><span class="row-state">${esc(row.status)}</span></td><td>${esc(rows(row.modes).map(label).join(', '))}</td><td>${esc(row.responsibility)}</td><td>${esc(rows(row.outputs).join(', '))}</td><td>${esc(row.production_boundary)}</td></tr>`).join('') || emptyRow(6, 'No governed research method is registered.');
    applyStoredSort($('researchRows'));
  }
  function replayMetric(value, kind='pct') {
    const n = number(value);
    if (n === null) return '—';
    if (kind === 'ratio') return formatNumber(n,2);
    if (kind === 'count') return formatNumber(n,0);
    return `${(n * 100).toFixed(2)}%`;
  }
  function renderResearchReplay(payload, hostId = 'backtestResults') {
    const host = $(hostId);
    if (!host) return;
    if (!payload || payload.ok !== true) {
      host.innerHTML = `<div class="empty">${esc(payload?.error || payload?.state || 'Replay unavailable')}</div>`;
      return;
    }
    const cards = ['heuristic','quant','hybrid'].map(arm => {
      const node = payload.arms?.[arm] || {};
      const v = node.validation || {};
      const gates = v.gates || {};
      const failed = Object.entries(gates).filter(([,ok]) => ok !== true).map(([key]) => label(key));
      const tone = v.approved ? 'positive' : 'warning';
      return `<article class="backtest-arm ${tone}"><header><span>${esc(label(arm))}</span><b>${esc(v.status || 'UNAVAILABLE')}</b></header><div class="backtest-kpis"><span><small>Trades</small><b>${esc(replayMetric(v.n_test,'count'))}</b></span><span><small>Win</small><b>${esc(replayMetric(v.win_rate))}</b></span><span><small>Expectancy</small><b>${esc(replayMetric(v.expectancy_net_return ?? v.expectancy))}</b></span><span><small>Profit factor</small><b>${esc(replayMetric(v.profit_factor,'ratio'))}</b></span><span><small>Signal DD</small><b>${esc(replayMetric(v.precomputed_signal_equal_weight_drawdown))}</b></span><span><small>Folds</small><b>${esc(replayMetric(rows(v.folds).length,'count'))}</b></span></div><footer><span>Threshold ${node.selected_top_fraction == null ? 'not qualified' : esc(`${(node.selected_top_fraction*100).toFixed(0)}%`)}</span><span>${failed.length ? esc(`First blockers: ${failed.slice(0,3).join(' · ')}`) : 'All declared gates passed'}</span></footer></article>`;
    }).join('');
    host.innerHTML = `<div class="backtest-summary"><span>Desk <b>${esc(label(payload.mode))}</b></span><span>Horizon <b>${esc(payload.horizon)}</b></span><span>Populations <b>${esc(formatNumber(payload.population_count,0))}</b></span><span>Settled common <b>${esc(formatNumber(payload.settled_candidate_count,0))}</b></span><span>Purge/embargo <b>governed</b></span><span>Production authority <b>${esc(label(payload.production_authority))}</b></span></div><div class="backtest-arms">${cards}</div><div class="proof-note">Historical replay only. It never changes production authority or mixes into Forward/Model Paper Accuracy.</div>`;
  }

  function renderBacktestPageState() {
    const desk = $('backtestPageDesk')?.value || 'delivery';
    if ($('backtestState')) setStatePill($('backtestState'), 'neutral', `${label(desk)} · historical only`);
    if (state.researchReplay && $('backtestPageResults')) renderResearchReplay(state.researchReplay, 'backtestPageResults');
  }

  async function runBacktestPage() {
    const button = $('runBacktestPage');
    const desk = $('backtestPageDesk')?.value || 'delivery';
    const folds = $('backtestPageFolds')?.value || '8';
    const profile = $('backtestPageProfile')?.value || 'capital';
    const horizon = desk === 'intraday' ? '30m' : '10d';
    if (button) { button.disabled = true; button.textContent = 'Running…'; }
    if ($('backtestState')) setStatePill($('backtestState'), 'warming', 'Running persisted replay');
    if ($('backtestPageResults')) $('backtestPageResults').innerHTML = '<div class="empty">Reading persisted point-in-time populations and settled outcomes…</div>';
    try {
      const payload = await api(`/api/selection-walk-forward-replay?desk=${encodeURIComponent(desk)}&horizon=${encodeURIComponent(horizon)}&max_folds=${encodeURIComponent(folds)}&embargo_days=1&profile=${encodeURIComponent(profile)}`, {timeout:12000});
      state.researchReplay = payload;
      renderResearchReplay(payload, 'backtestPageResults');
      if ($('backtestState')) setStatePill($('backtestState'), payload.ok === true ? 'ready' : 'unavailable', payload.ok === true ? `${label(desk)} · replay complete` : label(payload.state || 'Unavailable'));
    } catch (error) {
      renderResearchReplay({ok:false,error:error.message}, 'backtestPageResults');
      if ($('backtestState')) setStatePill($('backtestState'), 'unavailable', 'Replay unavailable');
    } finally {
      if (button) { button.disabled = false; button.textContent = 'Run Backtest'; }
    }
  }

  async function runResearchReplay() {
    const button = $('runBacktest');
    const desk = $('backtestDesk')?.value || 'delivery';
    const folds = $('backtestFolds')?.value || '8';
    const horizon = desk === 'intraday' ? '30m' : '10d';
    button.disabled = true; button.textContent = 'Running replay…';
    $('backtestResults').innerHTML = '<div class="empty">Reading persisted point-in-time selector outcomes…</div>';
    try {
      const payload = await api(`/api/selection-walk-forward-replay?desk=${encodeURIComponent(desk)}&horizon=${encodeURIComponent(horizon)}&max_folds=${encodeURIComponent(folds)}&embargo_days=1&profile=capital`, {timeout:12000});
      state.researchReplay = payload;
      renderResearchReplay(payload);
    } catch (error) {
      renderResearchReplay({ok:false,error:error.message});
    } finally {
      button.disabled = false; button.textContent = 'Run governed replay';
    }
  }

  function downloadPlainText(content, filename = 'ProjectLaddu-Progress-Proof.json') {
    const blob = new Blob([content], {type:'application/json;charset=utf-8'});
    const href = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = href; link.download = filename; link.style.display = 'none';
    document.body.appendChild(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(href), 1500);
  }

  async function copyPlainText(value, successMessage = 'Copied') {
    const content = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
    let copied = false;
    let method = 'clipboard';
    if (navigator.clipboard?.writeText && window.isSecureContext) {
      try {
        // Invoke Clipboard API while the original click still owns user activation.
        await navigator.clipboard.writeText(content);
        copied = true;
      } catch {
        copied = false;
      }
    }
    if (!copied) {
      method = 'legacy';
      const area = document.createElement('textarea');
      area.value = content;
      area.style.position = 'fixed'; area.style.left = '-10000px'; area.style.top = '0';
      area.style.width = '1px'; area.style.height = '1px'; area.style.opacity = '0';
      document.body.appendChild(area);
      try {
        area.focus({preventScroll:true}); area.select(); area.setSelectionRange(0, content.length);
        copied = document.execCommand('copy') === true;
      } catch {
        copied = false;
      } finally {
        area.remove();
      }
    }
    const sizeKb = Math.max(1, Math.round(new Blob([content]).size / 1024));
    if (!copied) {
      downloadPlainText(content);
      toast(`Clipboard blocked · proof downloaded · ${sizeKb} KB`);
      return false;
    }
    toast(`${successMessage} · ${sizeKb} KB${method === 'legacy' ? ' · fallback' : ''}`);
    return true;
  }

  function currentOperationsProofPayload() {
    return {
      client_captured_at: new Date().toISOString(),
      operations: state.operations,
      forward_progress: state.operationsForward,
      forward_clock: state.operationsClock,
      logs: state.operationsLogs,
      console: state.operationsConsolePayload
    };
  }

  function operationTone(raw) {
    const value = text(raw).toUpperCase();
    if (/FAIL|ERROR|STUCK|DEAD|CIRCUIT|BLOCKED|WITHDRAW|INVALID/.test(value)) return 'negative';
    if (/COMPLETE|READY|HEALTHY|PASS|SETTLED|TRUSTED|VERIFIED/.test(value)) return 'positive';
    if (/RUNNING|ACTIVE|CONTINUING|PREPARING|COLLECTING/.test(value)) return 'info';
    if (/NO_PROGRESS|WAIT|WARM|RETRY|PAUS|PENDING|PARTIAL|WATCH|REVIEW|INCOMPLETE|RECOVER/.test(value)) return 'warning';
    if (/SLEEPING|EXPECTED_IDLE|MARKET_CLOSED|^IDLE$/.test(value)) return 'neutral';
    return 'neutral';
  }

  function operationDisplayState(row = {}) {
    const raw = text(row.display_state || row.state || row.stage || 'UNKNOWN').toUpperCase();
    if (raw === 'NO_PROGRESS' && !row.last_error) {
      const waiting = text(row.waiting_on).toUpperCase();
      if (/FEATURE|SNAPSHOT/.test(waiting)) return 'WAITING_FOR_FEATURES';
      return 'WAITING_DEPENDENCY';
    }
    return raw;
  }

  function operationJobMap(summary = state.operations) {
    const map = new Map();
    rows(summary?.jobs).forEach(row => map.set(text(row.job_id || row.component), row));
    return map;
  }

  function operationProgressLabel(row = {}) {
    const done = number(row.completed), total = number(row.total), pctValue = number(row.progress_pct);
    const lastFull = number(row.last_completed_sweep_count);
    if (done !== null && total !== null && lastFull !== null && lastFull >= total && done < total) {
      return `current ${formatNumber(done,0)}/${formatNumber(total,0)} · last full ${formatNumber(lastFull,0)}/${formatNumber(total,0)}`;
    }
    if (pctValue !== null) return `${formatNumber(pctValue,1)}%${done !== null && total !== null ? ` · ${formatNumber(done,0)}/${formatNumber(total,0)}` : ''}`;
    if (done !== null && total !== null) return `${formatNumber(done,0)}/${formatNumber(total,0)}`;
    return label(row.stage || row.state || 'Waiting');
  }

  function operationNextAction(row = {}) {
    const allowed = rows(row.allowed_actions);
    if (allowed.length) return `Next: ${label(allowed[0])}`;
    if (row.last_error) return 'Next: inspect copied error / evidence';
    if (row.waiting_on) return `Waiting: ${text(row.waiting_on)}`;
    const stateName = text(row.state).toUpperCase();
    if (/COMPLETE|READY|EXPECTED_IDLE|SLEEPING/.test(stateName)) return 'Next: continue / monitor';
    return 'Next: monitor progress';
  }

  function operationCadenceLabel(row = {}) {
    const last = row.last_cycle_at || row.last_progress_at || row.last_heartbeat_at;
    const next = row.next_cycle_at;
    const remaining = number(row.seconds_to_next);
    const parts=[];
    if(last) parts.push(`last ${compactTime(last)}`);
    if(next) parts.push(`next ${compactTime(next)}${remaining===null?'':` (${Math.max(0,Math.round(remaining))}s)`}`);
    return parts.join(' · ');
  }

  function updateQuickOperationCard(prefix, row, fallback = 'Unavailable') {
    const stateNode = $(`ops${prefix}State`), metaNode = $(`ops${prefix}Meta`);
    if (!stateNode || !metaNode) return;
    if (!row) { stateNode.textContent = fallback; stateNode.className = 'negative-text'; return; }
    const displayState = operationDisplayState(row);
    stateNode.textContent = label(displayState || fallback);
    stateNode.className = operationTone(displayState || fallback);
    const cadence=operationCadenceLabel(row);
    metaNode.textContent = `${operationProgressLabel(row)}${cadence ? ` · ${cadence}` : ''}${row.waiting_on ? ` · ${text(row.waiting_on)}` : ''}`;
  }

  function compactOperationDetail(value, limit = 420) {
    const clean = text(value).replace(/\s+/g, ' ').trim();
    if (clean.length <= limit) return {text:clean, truncated:false};
    return {text:clean.slice(0, Math.max(0, limit - 1)).trimEnd(), truncated:true};
  }
  function operationProblemRowHtml(row, index, copyAttribute) {
    const detail = compactOperationDetail(row.detail);
    const next = compactOperationDetail(row.next, 220);
    return `<div class="ops-error-row"><div><b>${esc(label(row.source))}</b><span class="${operationTone(row.state)}">${esc(label(row.state))}</span><p class="ops-problem-detail${detail.truncated?' is-truncated':''}">${esc(detail.text)}</p><small>${esc(next.text)}${next.truncated?' …':''}${detail.truncated?' · Copy for full evidence':''}</small></div><button type="button" class="ops-copy-button" ${copyAttribute}="${index}">Copy</button></div>`;
  }

  function renderOperationsControl(summary = {}, forward = {}, clock = {}, logs = {}) {
    state.operations = summary; state.operationsForward = forward; state.operationsClock = clock; state.operationsLogs = logs;
    const map = operationJobMap(summary);
    const intraday = map.get('loop:intraday_coverage') || rows(summary.jobs).find(r => r.component === 'intraday_coverage') || rows(summary.jobs).find(r => r.component === 'intraday_scanner');
    const delivery = map.get('loop:delivery_coverage') || map.get('scanner:delivery') || rows(summary.jobs).find(r => r.component === 'delivery_coverage') || rows(summary.jobs).find(r => r.component === 'delivery_scanner');
    const researchRows = rows(summary.jobs).filter(r => /^research:/.test(text(r.job_id)));
    const research = researchRows.sort((a,b) => (number(a.progress_pct) ?? -1) - (number(b.progress_pct) ?? -1))[0];
    const dataJob = map.get('data:deep-history') || rows(summary.jobs).find(r => /history|conveyor|delivery_data/i.test(text(r.component)));
    updateQuickOperationCard('Intraday', intraday);
    updateQuickOperationCard('Delivery', delivery);
    updateQuickOperationCard('Research', research, researchRows.length ? 'Monitoring' : 'No lifecycle job');
    updateQuickOperationCard('Data', dataJob, 'No backlog projected');
    const lifecycleClosure = map.get('lifecycle:closure') || rows(summary.jobs).find(r => r.component === 'lifecycle_closure');
    updateQuickOperationCard('Lifecycle', lifecycleClosure, 'Ready to run');

    // PL43 one-click end-to-end command center. The button only schedules the
    // existing governed lifecycle; the always-on controller/reconciliation
    // authorities own monitoring, bounded recovery and deterministic repair.
    const e2eState = text(lifecycleClosure?.state || 'READY').toUpperCase();
    const e2eStateNode = $('e2eRunState');
    if (e2eStateNode) {
      const tone = operationTone(e2eState);
      const toneClass = tone === 'negative' ? 'negative' : tone === 'warning' ? 'warning' : tone === 'info' ? 'running' : 'ready';
      e2eStateNode.className = `e2e-state ${toneClass}`;
      const b=e2eStateNode.querySelector('b'); if (b) b.textContent = e2eState === 'NOT_RUN' ? 'READY' : label(e2eState);
    }
    const e2eProgressNode = $('e2eRunProgress');
    if (e2eProgressNode) e2eProgressNode.textContent = lifecycleClosure
      ? `${operationProgressLabel(lifecycleClosure)} · ${label(lifecycleClosure.stage || 'processing')}`
      : 'One click · monitored automatically';
    const agents = lifecycleClosure?.agents || {};
    const renderAgent = (id,row,fallbackLabel) => {
      const node=$(id); if(!node) return;
      const status=text(row?.state || 'READY').toUpperCase();
      const tone=operationTone(status);
      node.classList.remove('warning','negative','info');
      if(tone==='warning') node.classList.add('warning'); else if(tone==='negative') node.classList.add('negative'); else if(tone==='info') node.classList.add('info');
      const small=node.querySelector('small'); if(small) small.textContent = row?.always_on === false ? label(status) : `${label(status || fallbackLabel)} · auto`;
    };
    renderAgent('e2eMonitorAgent',agents.monitoring_recovery,'READY');
    renderAgent('e2eReconcileAgent',agents.reconciliation,'READY');

    const rootHost = $('opsRootCause');
    if (rootHost) {
      const pressure = summary.workload_governor?.database_pressure || {};
      const gov = pressure.governance || {};
      const critical = rows(summary.controller?.active_blockers ?? summary.controller?.blockers).filter(row => /FAILED|STUCK|CIRCUIT_OPEN|DEAD|ERROR|BLOCKED|INVALID/.test(text(row.state).toUpperCase()));
      const affected = [...new Set(critical.map(row => text(row.component)).filter(Boolean))];
      const govNoCapacity = number(gov.pool_size) > 0 && number(gov.pool_available) === 0;
      const currentWait = number(gov.requests_waiting) ?? 0;
      const cumulativeQueued = number(gov.requests_queued);
      const lastAction = summary.controller?.last_action || {};
      const circuit = lastAction.circuit || {};
      const trust = summary.trust || state.workspace?.trust || {};
      const p95 = number(trust.latency?.customer_read_p95_ms);
      let title = 'No shared runtime root cause detected';
      let detail = 'Customer path is not currently linked to an active dependency failure.';
      let tone = 'ready';
      if (govNoCapacity || gov.pressured) {
        title = `Governance DB ${govNoCapacity ? 'capacity exhausted' : 'under pressure'}`;
        detail = `${formatNumber(gov.pool_available ?? 0,0)}/${formatNumber(gov.pool_size ?? 0,0)} immediately available · ${formatNumber(currentWait,0)} currently waiting${affected.length?` · affecting ${affected.slice(0,4).map(label).join(', ')}`:''}`;
        if (cumulativeQueued !== null) detail += ` · cumulative queued statistic ${formatNumber(cumulativeQueued,0)} (not current backlog)`;
        tone = currentWait > 0 || critical.length ? 'blocked' : 'warning';
      } else if (critical.length) {
        title = `${label(critical[0].component || 'Runtime')} ${label(critical[0].state || 'blocked')}`;
        detail = affected.length ? `Active runtime blockers: ${affected.map(label).join(', ')}` : text(critical[0].detail || critical[0].reason || 'Recovery required');
        tone = 'blocked';
      }
      if (p95 !== null && p95 >= 2000) { detail += ` · customer read p95 ${p95<1000?`${formatNumber(p95,0)}ms`:`${formatNumber(p95/1000,1)}s`}`; if (p95>=5000) tone='blocked'; }
      const failures = number(circuit.failures);
      const verified = lastAction.verified === true;
      const recovery = failures === null ? 'No active recovery circuit' : `Recovery: ${verified?'verified':'not verified'} · circuit failures ${formatNumber(failures,0)}`;
      rootHost.className = `ops-root-cause ${tone}`;
      rootHost.innerHTML = `<div><span>ROOT CAUSE</span><b>${esc(title)}</b><p>${esc(detail)}</p></div><aside><span>${esc(recovery)}</span><span>Trader trust: ${esc(label(trust.state || 'WARMING'))}</span></aside>`;
    }

    const forwardByDesk = forward.by_desk || clock.by_desk || {};
    const forwardStates = ['intraday','delivery'].map(desk => forwardByDesk[desk]?.state || forwardByDesk[desk]?.maturity_state).filter(Boolean);
    const forwardState = forward.state || clock.state || forwardStates.join(' / ') || 'Collecting';
    if ($('opsForwardState')) { $('opsForwardState').textContent = label(forwardState); $('opsForwardState').className = operationTone(forwardState); }
    if ($('opsForwardMeta')) {
      const parts = ['intraday','delivery'].map(desk => {
        const row = forwardByDesk[desk] || {};
        const days = number(pick(row,'trading_days','forward_trading_days','days'));
        const settled = number(pick(row,'settled_candidates','settled','sample_count'));
        return `${label(desk)} ${days===null?'—':formatNumber(days,0)}d / ${settled===null?'—':formatNumber(settled,0)} settled`;
      });
      $('opsForwardMeta').textContent = parts.join(' · ');
    }

    const jobsNode = $('operationsJobs');
    if (jobsNode) {
      const sourceJobs = rows(summary.jobs).map((row,index) => ({...row,_sourceIndex:index}));
      const rankState = value => { const v=text(value).toUpperCase(); if(/FAILED|STUCK|ERROR|BLOCKED|DEAD|CIRCUIT|INVALID/.test(v)) return 0; if(/NO_PROGRESS|RECOVER|WAIT|RETRY|PENDING|PARTIAL|WARM|INCOMPLETE/.test(v)) return 1; if(/RUNNING|ACTIVE|CONTINUING|PREPARING|COLLECTING/.test(v)) return 2; if(/COMPLETE|READY|HEALTHY|PASS|SETTLED/.test(v)) return 3; if(/EXPECTED_IDLE|IDLE|PAUSED|MARKET_CLOSED|SLEEPING/.test(v)) return 5; return 4; };
      sourceJobs.sort((a,b)=>rankState(a.state)-rankState(b.state) || text(a.title||a.component).localeCompare(text(b.title||b.component)));
      const expectedIdle = sourceJobs.filter(row => /EXPECTED_IDLE|MARKET_CLOSED|^IDLE$/.test(text(row.state).toUpperCase()));
      const primaryJobs = sourceJobs.filter(row => !expectedIdle.includes(row));
      const jobCard = row => {
        const index=row._sourceIndex; const stateName = operationDisplayState(row);
        const tone = operationTone(stateName); const pctValue = number(row.progress_pct);
        const actions = rows(row.allowed_actions).map(action => `<button type="button" class="ops-mini-action" data-ops-action="${esc(action)}" data-ops-job-index="${index}">${esc(label(action))}</button>`).join('');
        const problem = row.last_error || row.waiting_on || '';
        const cadence=operationCadenceLabel(row);
        return `<article class="ops-job-card ${tone}"><header><div><span>${esc(row.title || label(row.component || row.job_id))}</span><b>${esc(label(stateName))}</b></div><button type="button" class="ops-copy-button" data-copy-job="${index}">Copy</button></header><div class="ops-progress-track"><i style="width:${pctValue===null?0:Math.max(0,Math.min(100,pctValue))}%"></i></div><div class="ops-job-meta"><span>${esc(operationProgressLabel(row))}</span><span>${esc(cadence || (row.stage ? label(row.stage) : 'Stage unavailable'))}</span><span>${row.rate_per_min==null?'Rate —':`${esc(formatNumber(row.rate_per_min,1))}/min`}</span></div>${problem?`<div class="ops-blocker ${row.last_error?'negative-text':'warning-text'}">${esc(text(problem))}</div>`:''}<footer><small>${esc(operationNextAction(row))}</small><div>${actions}</div></footer></article>`;
      };
      const visiblePrimary = primaryJobs.slice(0,8);
      const overflowPrimary = primaryJobs.slice(8);
      const primaryHtml = visiblePrimary.map(jobCard).join('');
      const overflowHtml = overflowPrimary.length ? `<details class="ops-more-group"><summary><b>${overflowPrimary.length} more active components</b><span>Lower-attention running / waiting work</span></summary><div class="ops-more-grid">${overflowPrimary.map(jobCard).join('')}</div></details>` : '';
      const idleHtml = expectedIdle.length ? `<details class="ops-idle-group"><summary><b>${expectedIdle.length} healthy / sleeping jobs</b><span>Scheduled cadence is healthy; no trust penalty</span></summary><div class="ops-idle-list">${expectedIdle.map(row=>`<div><span>${esc(row.title || label(row.component || row.job_id))}</span><b>${esc(label(row.display_state || row.state || 'SLEEPING'))}</b><small>${esc([operationCadenceLabel(row),row.waiting_on || row.stage || 'No action required'].filter(Boolean).join(' · '))}</small><button type="button" class="ops-copy-button" data-copy-job="${row._sourceIndex}">Copy</button></div>`).join('')}</div></details>` : '';
      jobsNode.innerHTML = (primaryHtml || '<div class="empty">No active operational jobs are projected.</div>') + overflowHtml + idleHtml;
    }

    const rawProblemJobs = rows(summary.jobs).filter(row => row.last_error || /FAILED|STUCK|CIRCUIT_OPEN|UNINSTRUMENTED|DEAD|ERROR|BLOCKED|INVALID/.test(text(row.state).toUpperCase()));
    // Prefer the canonical virtual scanner/read-model contract over a stale
    // supervisor generation for the same component. This prevents one real job
    // from being counted twice as two independent blockers.
    const byComponent = new Map();
    for (const row of rawProblemJobs) {
      const key=text(row.component || row.job_id); const prior=byComponent.get(key);
      const canonical=/^(scanner:|data:|research:|selected:|lifecycle:)/.test(text(row.job_id));
      const priorCanonical=prior && /^(scanner:|data:|research:|selected:|lifecycle:)/.test(text(prior.job_id));
      if (!prior || (canonical && !priorCanonical)) byComponent.set(key,row);
    }
    const problemJobs = [...byComponent.values()];
    const blockers = rows(summary.controller?.active_blockers ?? summary.controller?.blockers).filter(row => row.actionable === true || /FAILED|STUCK|CIRCUIT_OPEN|DEAD|UNINSTRUMENTED|ERROR|BLOCKED|INVALID/.test(text(row.state).toUpperCase()));
    const evidencePending = rows(summary.controller?.evidence_pending ?? summary.controller?.blockers).filter(row => !blockers.includes(row));
    const activeRows = [
      ...problemJobs.map(row => ({source:row.title || row.component,state:row.state,detail:row.last_error || row.waiting_on || 'No useful progress',next:operationNextAction(row)})),
      ...blockers.map(row => ({source:row.component || 'controller',state:row.state || 'BLOCKER',detail:row.reason || row.detail || row.message || JSON.stringify(row),next:row.next_action || 'Use the allowed recovery action or inspect evidence'})),
    ];
    const evidenceRows = evidencePending.map(row => ({source:row.component || 'evidence',state:row.state || 'PENDING_EVIDENCE',detail:row.reason || row.detail || row.message || JSON.stringify(row),next:row.next_action || 'Evidence is still maturing; this is not an active runtime failure'}));
    state.operationsProblems = activeRows;
    state.operationsEvidence = evidenceRows;
    const errorsNode = $('operationsErrors');
    if (errorsNode) errorsNode.innerHTML = activeRows.map((row,index) => operationProblemRowHtml(row,index,'data-copy-error')).join('') || '<div class="ops-all-clear"><b>No active failure/blocker is projected.</b><span>Runtime problems will appear here only when action is required.</span></div>';
    const evidenceNode = $('operationsEvidence');
    if (evidenceNode) evidenceNode.innerHTML = evidenceRows.map((row,index) => operationProblemRowHtml(row,index,'data-copy-evidence')).join('') || '<div class="ops-all-clear"><b>No pending evidence gap is projected.</b><span>Forward and qualification proof is currently clear.</span></div>';

    const activeKeys=new Set();
    problemJobs.forEach(row=>activeKeys.add(`job:${text(row.component || row.job_id)}`));
    blockers.forEach(row=>activeKeys.add(`controller:${text(row.key || row.component || row.detail)}`));
    const blockedCount = activeKeys.size;
    const evidenceCount = number(summary.controller?.evidence_pending_count) ?? evidencePending.length;
    if ($('opsActiveProblemCount')) { $('opsActiveProblemCount').textContent = `${blockedCount} active`; $('opsActiveProblemCount').className = `state-pill ${blockedCount ? 'blocked' : 'ready'}`; }
    if ($('opsEvidencePendingCount')) { $('opsEvidencePendingCount').textContent = `${evidenceCount} pending`; $('opsEvidencePendingCount').className = `state-pill ${evidenceCount ? 'warning' : 'ready'}`; }
    if ($('progressQuick')) {
      $('progressQuick').textContent = 'Diagnostics';
      $('progressQuick').title = blockedCount ? `${blockedCount} active engineering issue(s) · ${evidenceCount} evidence item(s)` : `${evidenceCount} evidence item(s)`;
      $('progressQuick').classList.remove('has-blocker');
    }
    if ($('opsAutoState')) $('opsAutoState').textContent = `Auto refresh · 2.5s · age ${formatNumber(number(summary.projection_age_sec) ?? 0,1)}s`;
  }

  async function loadOperationsControl({silent=false, forceFresh=false} = {}) {
    if (state.operationsPollBusy) {
      if (!forceFresh) return state.operations;
      const deadline=Date.now()+2500;
      while (state.operationsPollBusy && Date.now()<deadline) await new Promise(resolve=>setTimeout(resolve,50));
      if (state.operationsPollBusy) {
        state.operationsPollBusy=false;
      }
    }
    state.operationsPollBusy = true;
    try {
      const nonce = Date.now();
      const calls = await Promise.allSettled([
        api(`/api/operations/summary${forceFresh ? `?refresh=true&_=${nonce}` : ''}`, {timeout:forceFresh?4000:1800}),
        api(`/api/forward-progress?_=${nonce}`, {timeout:forceFresh?12000:1800}),
        api(`/api/forward-evidence-clock?_=${nonce}`, {timeout:forceFresh?12000:1800}),
        api(`/api/operations/logs?limit=180${forceFresh ? `&refresh=true&_=${nonce}` : ''}`, {timeout:forceFresh?5000:1800}),
      ]);
      const value = (index, fallback={}) => calls[index].status === 'fulfilled' ? calls[index].value : {...fallback,ok:false,error:calls[index].reason?.message || 'request failed'};
      renderOperationsControl(value(0,{jobs:[]}), value(1,{by_desk:{}}), value(2,{by_desk:{}}), value(3,{lines:[]}));
      if (!silent && calls.some(row => row.status === 'rejected')) toast('Progress refreshed with one or more unavailable evidence sources.');
      return state.operations;
    } finally { state.operationsPollBusy = false; }
  }

  function setOperationsConsole(payload, title='Operation result') {
    state.operationsConsolePayload = {title, captured_at:new Date().toISOString(), payload};
    if ($('operationsConsole')) $('operationsConsole').textContent = JSON.stringify(state.operationsConsolePayload, null, 2);
  }

  async function runQuickOperation(command, button) {
    const original = button?.textContent || '';
    if (button) { button.disabled = true; button.textContent = 'Running…'; }
    try {
      let payload;
      if (command === 'intraday_scan') payload = await api('/api/refresh', {method:'POST', body:{}, timeout:4000});
      else if (command === 'delivery_scan') payload = await api('/api/deep-scan', {method:'POST', body:{}, timeout:4000});
      else if (command === 'research_advance') payload = await api('/api/operations/action', {method:'POST',body:{action:'advance_full_lifecycle',reason:'progress_research_advance'},timeout:5000});
      else if (command === 'delivery_sync') payload = await api('/api/delivery-sync?force=true', {timeout:15000});
      else if (command === 'safe_recovery') payload = await api('/api/operations/action', {method:'POST',body:{action:'recover_all_safe_stuck',reason:'progress_control_center'},timeout:10000});
      else if (command === 'full_lifecycle') payload = await api('/api/operations/action', {method:'POST',body:{action:'run_end_to_end',reason:'operator_one_click_end_to_end'},timeout:10000});
      else payload = {ok:true,state:'REFRESH_ONLY',message:'No mutation requested; evidence refreshed.'};
      setOperationsConsole(payload, label(command));
      toast(payload.message || `${label(command)} accepted`);
    } catch (error) {
      const payload = {ok:false,state:'ACTION_FAILED',command,error:error.message};
      setOperationsConsole(payload, `${label(command)} failed`);
      toast(`${label(command)} failed: ${error.message}`);
    } finally {
      if (button) { button.disabled = false; button.textContent = original; }
      setTimeout(() => loadOperationsControl({silent:true}), 500);
      setTimeout(() => loadWorkspace(), 900);
    }
  }

  async function runEndToEndProcess() {
    const button=$('runEndToEnd');
    if(button) button.disabled=true;
    const stateNode=$('e2eRunState');
    if(stateNode){stateNode.className='e2e-state running';const b=stateNode.querySelector('b');if(b)b.textContent='STARTING';}
    if($('e2eRunProgress')) $('e2eRunProgress').textContent='Scheduling governed end-to-end process…';
    try {
      const payload=await api('/api/operations/action',{method:'POST',body:{action:'run_end_to_end',reason:'workspace_one_click_end_to_end'},timeout:10000});
      setOperationsConsole(payload,'Run End-to-End');
      toast(payload.state ? label(payload.state) : 'End-to-end process scheduled');
      await loadOperationsControl({silent:true,forceFresh:true});
      showPage('workspace');
    } catch(error) {
      setOperationsConsole({ok:false,state:'ACTION_FAILED',action:'run_end_to_end',error:error.message},'Run End-to-End failed');
      if(stateNode){stateNode.className='e2e-state negative';const b=stateNode.querySelector('b');if(b)b.textContent='FAILED';}
      if($('e2eRunProgress')) $('e2eRunProgress').textContent=error.message;
      toast(`End-to-end start failed: ${error.message}`);
    } finally {
      if(button) button.disabled=false;
    }
  }

  async function runAllowedOperationAction(action, jobIndex, button) {
    const row = rows(state.operations?.jobs)[jobIndex] || {};
    const original = button?.textContent || '';
    if (button) { button.disabled = true; button.textContent = 'Running…'; }
    try {
      const payload = await api('/api/operations/action', {method:'POST', body:{action,component:row.action_component || row.component || '',symbol:row.symbol || '',mode:row.mode || 'delivery',interval:row.interval || '',reason:'operator_progress_control_center'}, timeout:12000});
      setOperationsConsole({request:{action,job:row},response:payload}, `${label(action)} · ${row.title || row.component}`);
      toast(payload.state ? label(payload.state) : `${label(action)} completed`);
    } catch (error) {
      setOperationsConsole({ok:false,action,job:row,error:error.message}, `${label(action)} failed`);
      toast(`${label(action)} failed: ${error.message}`);
    } finally {
      if (button) { button.disabled=false; button.textContent=original; }
      setTimeout(() => loadOperationsControl({silent:true}), 500);
    }
  }

  async function loadSystem() {
    try { state.ready = await api('/api/ready', {timeout:1500}); } catch (error) { state.ready = {ok:false,error:error.message}; }
    const ready = state.ready || {}, workspace = state.workspace || await ensureWorkspace() || {}, health = workspace.health || {};
    setStatePill($('systemState'), ready.ready ? 'ready' : 'unavailable', ready.ready ? 'Process ready' : 'Unavailable');
    renderStats($('systemStats'), [
      {label:'Runtime', value:ready.ready ? 'Ready' : 'Unavailable', tone:ready.ready ? 'positive' : 'negative'},
      {label:'Live plane', value:label(health.live_stream?.state || workspace.market_state || 'Unknown')},
      {label:'Scanner', value:label(health.scanner || 'Unknown')},
      {label:'Broker authority', value:'NONE'},
    ]);
    $('systemContracts').innerHTML = `<p><b>Actionable:</b> canonical decisions only; no research candidate or read-model fallback becomes a trade.</p><p><b>Chart:</b> internal recreated chart disabled from the critical path; broker chart is external and never a Laddu authority.</p><p><b>Metrics:</b> background-materialized Accuracy and settled Model Paper Performance.</p><p><b>Research:</b> WFA/ML/Alpha remain isolated until governed qualification permits influence.</p>`;
    $('systemDiagnostics').textContent = JSON.stringify({ready, workspace:{contract_version:workspace.contract_version, as_of:workspace.as_of, route_elapsed_ms:workspace.route_elapsed_ms, projection_state:workspace.projection_state, health}, product_mode:'AUTOMATIC_MODEL_PAPER_ONLY', broker_authority:'NONE'}, null, 2);
    await loadOperationsControl({silent:true});
  }

  function sectionStorageKey(panel, title) {
    const page = panel.closest('[data-page-panel]')?.dataset.pagePanel || 'page';
    const slug = text(title || panel.className || 'section').toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-|-$/g,'').slice(0,80) || 'section';
    return `laddu-ui3-section:${page}:${slug}`;
  }
  function initWorkspaceDetails() {
    all('[data-page-panel="workspace"] details.workspace-collapse').forEach(node => {
      node.open = false;
      const indicator=node.querySelector(':scope > summary i');
      const sync=()=>{ if(indicator) indicator.textContent=node.open?'Collapse':'Expand'; };
      sync();
      node.addEventListener('toggle',sync);
    });
  }

  function setSectionCollapsed(panel, collapsed, {persist=true} = {}) {
    if (!panel) return;
    panel.classList.toggle('is-collapsed', Boolean(collapsed));
    const button = panel.querySelector(':scope > .panel-heading > .section-collapse-toggle, :scope > .decision-inline-head > .section-collapse-toggle, :scope > .mtf-bar > .section-collapse-toggle');
    if (button) {
      button.textContent = collapsed ? '▸' : '▾';
      button.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
      button.title = collapsed ? 'Expand section' : 'Collapse section';
    }
    if (persist && panel.dataset.collapseKey) {
      safeStorageSet(panel.dataset.collapseKey, collapsed ? '1' : '0');
    }
    if (!collapsed && panel.classList.contains('chart-panel')) setTimeout(resizeCharts, 80);
  }
  function pageCollapsibleSections(page) {
    return all(`[data-page-panel="${page}"] .collapsible-section`);
  }
  function setPageSections(page, collapsed) {
    pageCollapsibleSections(page).forEach(panel => setSectionCollapsed(panel, collapsed));
  }
  function initCollapsibleSections() {
    const pages = new Set(['report','opportunities','model-paper','accuracy','research','system']);
    all('[data-page-panel] .panel').forEach(panel => {
      const page = panel.closest('[data-page-panel]')?.dataset.pagePanel;
      if (!pages.has(page) || panel.dataset.collapse === 'off' || panel.classList.contains('collapsible-section')) return;
      const head = panel.querySelector(':scope > .panel-heading, :scope > .decision-inline-head, :scope > .mtf-bar');
      if (!head) return;
      const title = text(head.querySelector('h2')?.textContent || head.querySelector('.eyebrow')?.textContent || head.querySelector('.mtf-title')?.textContent || 'Section').trim();
      const key = sectionStorageKey(panel, title);
      panel.dataset.collapseKey = key;
      panel.classList.add('collapsible-section');
      const toggle = document.createElement('button');
      toggle.type = 'button';
      toggle.className = 'section-collapse-toggle';
      toggle.setAttribute('aria-label', `Toggle ${title}`);
      toggle.addEventListener('click', event => {
        event.stopPropagation();
        setSectionCollapsed(panel, !panel.classList.contains('is-collapsed'));
      });
      head.appendChild(toggle);
      head.classList.add('collapsible-section-head');
      head.addEventListener('click', event => {
        if (event.target.closest('button,a,input,select,textarea,label,[role="button"]')) return;
        setSectionCollapsed(panel, !panel.classList.contains('is-collapsed'));
      });
      let stored = null;
      stored = safeStorageGet(key);
      const defaultCollapsed = /RUN, WATCH, PROVE|EVIDENCE STILL MATURING|ACTION\s*\/\s*EVIDENCE CONSOLE|PRODUCT CONTRACTS|SAFE OPERATOR GUIDANCE|HISTORICAL BACKTEST/i.test(title);
      setSectionCollapsed(panel, stored === null ? defaultCollapsed : stored === '1', {persist:false});
    });
    if ($('expandSystemSections')) $('expandSystemSections').addEventListener('click', () => setPageSections('system', false));
    if ($('collapseSystemSections')) $('collapseSystemSections').addEventListener('click', () => setPageSections('system', true));
  }

  function bindEvents() {
    all('[data-page]').forEach(button => button.addEventListener('click', () => showPage(button.dataset.page)));
    all('[data-open-page]').forEach(button => button.addEventListener('click', () => showPage(button.dataset.openPage)));
    all('[data-workspace-signal-mode] [data-mode]').forEach(button => button.addEventListener('click', () => {
      state.workspaceSignalMode = ['intraday','delivery'].includes(button.dataset.mode) ? button.dataset.mode : 'all';
      all('[data-workspace-signal-mode] [data-mode]').forEach(node => node.classList.toggle('active', node === button));
      if (state.workspace) renderWorkspaceFinalSignals(state.workspace);
    }));
    all('[data-workspace-signal-limit] [data-limit]').forEach(button => button.addEventListener('click', () => {
      state.workspaceSignalLimit = Number(button.dataset.limit) === 10 ? 10 : 5;
      all('[data-workspace-signal-limit] [data-limit]').forEach(node => node.classList.toggle('active', node === button));
      if (state.workspace) renderWorkspaceFinalSignals(state.workspace);
    }));
    $('menuButton').addEventListener('click', () => document.querySelector('.app-shell').classList.toggle('nav-open'));
    if ($('navCollapse')) {
      const shell = document.querySelector('.app-shell');
      const saved = safeStorageGet('laddu-nav-collapsed') === '1';
      shell.classList.toggle('nav-collapsed', saved);
      $('navCollapse').textContent = saved ? '▶' : '◀';
      $('navCollapse').addEventListener('click', () => {
        const collapsed = shell.classList.toggle('nav-collapsed');
        safeStorageSet('laddu-nav-collapsed', collapsed ? '1' : '0');
        $('navCollapse').textContent = collapsed ? '▶' : '◀';
        $('navCollapse').setAttribute('aria-label', collapsed ? 'Expand navigation' : 'Collapse navigation');
        setTimeout(resizeCharts, 180);
      });
    }
    if ($('maximizeChart')) $('maximizeChart').addEventListener('click', () => {
      const panel = document.querySelector('.chart-panel');
      const active = panel.classList.toggle('chart-maximized');
      document.body.classList.toggle('analytics-maximized', active);
      $('maximizeChart').textContent = active ? 'Restore' : 'Maximize';
      setTimeout(resizeCharts, 120);
    });
    $('themeToggle').addEventListener('click', () => applyTheme(state.theme === 'light' ? 'dark' : 'light'));
    if ($('progressQuick')) $('progressQuick').addEventListener('click', () => showPage('system'));
    if ($('runEndToEnd')) $('runEndToEnd').addEventListener('click', runEndToEndProcess);
    if ($('refreshOperations')) $('refreshOperations').addEventListener('click', () => loadOperationsControl({forceFresh:true}));
    if ($('copyOperations')) $('copyOperations').addEventListener('click', () => {
      // Copy the already-buffered 2.5s operations projection immediately.  Do not
      // await a network refresh here: Chromium/Electron may revoke clipboard user
      // activation after an await, which made Copy all intermittent.
      void copyPlainText(currentOperationsProofPayload(), 'Progress & proof copied');
      // Refresh independently so the next click sees the newest bounded projection.
      void loadOperationsControl({silent:true,forceFresh:true});
    });
    if ($('copyErrors')) $('copyErrors').addEventListener('click', () => copyPlainText(state.operationsProblems || [], 'Active problems copied'));
    if ($('copyEvidence')) $('copyEvidence').addEventListener('click', () => copyPlainText(state.operationsEvidence || [], 'Pending evidence copied'));
    if ($('copyOpsConsole')) $('copyOpsConsole').addEventListener('click', () => copyPlainText(state.operationsConsolePayload || $('operationsConsole')?.textContent || '', 'Console copied'));
    document.addEventListener('click', event => {
      const marketToggle = event.target.closest?.('[data-market-detail-toggle]');
      if (marketToggle) {
        const rail = marketToggle.closest('.market-decision-rail');
        const detail = rail?.querySelector('[data-market-detail]');
        if (detail) {
          const expanded = marketToggle.getAttribute('aria-expanded') === 'true';
          detail.hidden = expanded;
          marketToggle.setAttribute('aria-expanded', expanded ? 'false' : 'true');
          marketToggle.textContent = expanded ? '▾' : '▴';
          marketToggle.title = expanded ? 'Expand market breadth and movers' : 'Collapse market breadth and movers';
        }
        return;
      }
      const quick = event.target.closest?.('[data-quick-operation]');
      if (quick) { runQuickOperation(quick.dataset.quickOperation, quick); return; }
      const action = event.target.closest?.('[data-ops-action]');
      if (action) { runAllowedOperationAction(action.dataset.opsAction, Number(action.dataset.opsJobIndex), action); return; }
      const copyJob = event.target.closest?.('[data-copy-job]');
      if (copyJob) { const row = rows(state.operations?.jobs)[Number(copyJob.dataset.copyJob)] || {}; copyPlainText(row, `${row.title || row.component || 'Job'} copied`); return; }
      const copyError = event.target.closest?.('[data-copy-error]');
      if (copyError) { const row = rows(state.operationsProblems)[Number(copyError.dataset.copyError)] || {}; copyPlainText(row, 'Error/blocker copied'); return; }
      const copyEvidence = event.target.closest?.('[data-copy-evidence]');
      if (copyEvidence) { const row = rows(state.operationsEvidence)[Number(copyEvidence.dataset.copyEvidence)] || {}; copyPlainText(row, 'Pending evidence copied'); }
    });
    if ($('runBacktest')) $('runBacktest').addEventListener('click', runResearchReplay);
    if ($('runBacktestPage')) $('runBacktestPage').addEventListener('click', runBacktestPage);
    if ($('backtestPageDesk')) $('backtestPageDesk').addEventListener('change', renderBacktestPageState);
    if ($('advanceLifecycle')) $('advanceLifecycle').addEventListener('click', advanceResearchLifecycle);
    for (const [selector,key,allowed] of [
      ['[data-research-scope] [data-value]','researchScope',['all','active','history']],
      ['[data-research-mode] [data-value]','researchMode',['all','intraday','delivery']],
      ['[data-research-outcome] [data-value]','researchOutcome',['all','open','success','failure','promoted']],
    ]) all(selector).forEach(button=>button.addEventListener('click',()=>{
      state[key]=allowed.includes(button.dataset.value)?button.dataset.value:'all'; state.researchPage=0;
      all(selector).forEach(node=>node.classList.toggle('active',node===button)); renderResearch();
    }));
    if ($('researchPrev')) $('researchPrev').addEventListener('click',()=>{state.researchPage=Math.max(0,state.researchPage-1);renderResearch();});
    if ($('researchNext')) $('researchNext').addEventListener('click',()=>{state.researchPage+=1;renderResearch();});
    all('[data-model-paper-book] [data-book]').forEach(button => button.addEventListener('click', () => { all('[data-model-paper-book] [data-book]').forEach(b=>b.classList.toggle('active',b===button)); state.modelPaperBook=button.dataset.book==='research'?'research':'final'; renderModelPaper(); }));
    all('[data-model-paper-scope] [data-scope]').forEach(button => button.addEventListener('click', () => { all('[data-model-paper-scope] [data-scope]').forEach(b=>b.classList.toggle('active',b===button)); state.modelPaperScope=['open','today'].includes(button.dataset.scope)?button.dataset.scope:'all'; renderModelPaper(); }));
    if ($('backtestDesk')) $('backtestDesk').addEventListener('change', () => { if ($('backtestHorizonHint')) $('backtestHorizonHint').textContent = $('backtestDesk').value === 'intraday' ? '30m governed horizon' : '10d governed horizon'; });
    $('stockSearch').addEventListener('submit', submitSearch);
    $('stockQuery').addEventListener('input', scheduleSuggest);
    $('suggestions').addEventListener('click', event => {
      const button = event.target.closest('[data-suggestion]');
      if (!button) return;
      $('stockQuery').value = button.dataset.suggestion;
      $('suggestions').hidden = true;
      openStock(button.dataset.suggestion, $('stockMode').value, button.dataset.instrumentKey || '');
    });
    document.addEventListener('click', event => {
      const inspectButton = event.target.closest('[data-inspect-candidate-key]');
      if (inspectButton) {
        const key = inspectButton.dataset.inspectCandidateKey || '';
        const candidates = rows(state.opportunityCandidates);
        const rowIndex = candidates.findIndex(row => candidateStableKey(row) === key);
        const row = rowIndex >= 0 ? candidates[rowIndex] : null;
        const panel = $('candidateInspectPanel');
        if (row && panel) {
          state.candidateInspectKey = key; state.candidateInspectSnapshot = row;
          panel.innerHTML = candidateExplanationHtml(row, rowIndex + 1);
          panel.hidden = false;
          all('[data-inspect-candidate-key]').forEach(node => node.setAttribute('aria-expanded', node.dataset.inspectCandidateKey === key ? 'true' : 'false'));
          requestAnimationFrame(() => panel.scrollIntoView({block:'nearest',behavior:'smooth'}));
        }
      }
      const closeCandidateInspect = event.target.closest('[data-close-candidate-inspect]');
      if (closeCandidateInspect) {
        state.candidateInspectKey = ''; state.candidateInspectSnapshot = null;
        const panel = $('candidateInspectPanel');
        if (panel) { panel.hidden = true; panel.innerHTML = ''; }
        all('[data-inspect-candidate-key]').forEach(node => node.setAttribute('aria-expanded', 'false'));
      }
      const rowToggle = event.target.closest('[data-row-toggle]');
      if (rowToggle) {
        event.stopPropagation();
        const detail = document.getElementById(rowToggle.dataset.rowToggle);
        if (detail) {
          const nowOpen = detail.hidden;
          detail.hidden = !nowOpen;
          rowToggle.setAttribute('aria-expanded', String(nowOpen));
          rowToggle.classList.toggle('is-open', nowOpen);
        }
        return;
      }
      const stockButton = event.target.closest('[data-open-stock]');
      if (stockButton) {
        const lineage=stockButton.closest('[data-decision-id],[data-research-candidate]');
        openStock(stockButton.dataset.openStock, stockButton.dataset.mode, stockButton.dataset.instrumentKey || '', stockButton.dataset.decisionId || lineage?.dataset.decisionId || '', stockButton.dataset.researchCandidate || lineage?.dataset.researchCandidate || '');
      }
      const sortableHeader = event.target.closest('th.sortable');
      if (sortableHeader) sortTableFromHeader(sortableHeader);
      if (!event.target.closest('.stock-search')) $('suggestions').hidden = true;
    });
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape') {
        const panel = document.querySelector('.chart-panel.chart-maximized');
        if (panel) { panel.classList.remove('chart-maximized'); document.body.classList.remove('analytics-maximized'); if ($('maximizeChart')) $('maximizeChart').textContent='Maximize'; setTimeout(resizeCharts,120); }
      }
      const actionableRow = event.target.closest?.('tr.actionable-row[data-open-stock]');
      if (actionableRow && event.target === actionableRow && (event.key === 'Enter' || event.key === ' ')) {
        event.preventDefault();
        openStock(actionableRow.dataset.openStock, actionableRow.dataset.mode, actionableRow.dataset.instrumentKey || '', actionableRow.dataset.decisionId || '');
        return;
      }
      const sortableHeader = event.target.closest?.('th.sortable');
      if (sortableHeader && (event.key === 'Enter' || event.key === ' ')) {
        event.preventDefault();
        sortTableFromHeader(sortableHeader);
      }
    });
    all('[data-desk]').forEach(button => button.addEventListener('click', () => {
      state.desk = button.dataset.desk;
      all('[data-desk]').forEach(node => node.classList.toggle('active', node === button));
      renderOpportunities();
    }));
    $('runScan').addEventListener('click', runScan);
    $('mtfStrip').addEventListener('click', event => {
      const button = event.target.closest('[data-mtf-interval]');
      if (!button) return;
      state.interval = button.dataset.mtfInterval;
      renderMtfSummary(rows(state.stock?.mtf_trend));
      if (state.stock) renderQuoteStats(state.stock, state.stock.selected_quote || state.stock.quote || state.stock.display_quote || {});
      loadChartOnly();
    });
    $('deskDecisionStrip').addEventListener('click', event => {
      const button = event.target.closest('[data-stock-desk]');
      if (!button || !state.stock) return;
      state.stockMode = button.dataset.stockDesk === 'intraday' ? 'intraday' : 'delivery';
      renderDeskDecisions(state.stock);
      renderStock(state.stock);
      renderPriceOverlays();
    });
    $('chartOverlayButtons').addEventListener('click', event => {
      const button = event.target.closest('[data-overlay]');
      if (!button) return;
      toggleOverlay(button.dataset.overlay);
    });
    $('fitChart').addEventListener('click', () => { setFollowLive(false, {move:false}); markProgrammaticChartRange(); try { state.chart?.timeScale().fitContent(); syncPaneRange(state.chart?.timeScale().getVisibleLogicalRange()); } catch {} });
    $('followLive').addEventListener('click', () => setFollowLive(!state.followLive));
    all('[data-metric-mode] [data-mode]').forEach(button => button.addEventListener('click', () => {
      state.metricMode = button.dataset.mode;
      all('[data-metric-mode] [data-mode]').forEach(node => node.classList.toggle('active', node === button));
      renderAccuracy();
    }));
    $('refreshAccuracy').addEventListener('click', () => loadPerformance(true).then(() => { renderAccuracy(); toast('Accuracy projection refreshed or queued.'); }));
    all('[data-performance-period] [data-period]').forEach(button => button.addEventListener('click', () => {
      state.performancePeriod = button.dataset.period;
      all('[data-performance-period] [data-period]').forEach(node => node.classList.toggle('active', node === button));
      renderPerformance();
    }));
    all('[data-performance-mode] [data-mode]').forEach(button => button.addEventListener('click', () => {
      state.performanceMode = button.dataset.mode;
      all('[data-performance-mode] [data-mode]').forEach(node => node.classList.toggle('active', node === button));
      renderPerformance();
    }));
    window.addEventListener('hashchange', () => {
      const route=parseHashRoute();
      if (route.page==='report' && route.params.symbol) {
        if (state.symbol!==text(route.params.symbol).toUpperCase() || state.stockMode!==route.params.mode) openStock(route.params.symbol,route.params.mode,route.params.instrument||'',route.params.decision||'',route.params.research||'');
        else showPage('report',{push:false});
        return;
      }
      if (route.page==='research') state.researchFocusCandidate=text(route.params.candidate);
      showPage(route.page,{push:false});
    });
  }

  async function initialize() {
    initialiseTheme();
    enableSortableTables();
    bindEvents();
    initWorkspaceDetails();
    initCollapsibleSections();
    syncOverlayButtons();
    try {
      let identity=null, identityError=null;
      for (const delay of [0, 400, 1000]) {
        if (delay) await new Promise(resolve => setTimeout(resolve, delay));
        try {
          identity = await api('/api/frontend-identity', {timeout:6000});
          break;
        } catch (error) {
          identityError=error;
        }
      }
      if (!identity) throw identityError || new Error('Frontend identity unavailable after bounded retries');
      state.frontendIdentity=identity;
      const expectedVersion=text(document.documentElement.dataset.buildVersion);
      const expectedOwner=text(document.documentElement.dataset.frontendOwner);
      const expectedMarker=text(document.documentElement.dataset.buildMarker);
      const marker=text(identity.build_marker);
      const mismatches=rows(identity.mismatches);
      const valid=identity.ok === true && mismatches.length===0 && text(identity.version)===expectedVersion && text(identity.manifest_version)===expectedVersion && text(identity.frontend_owner)===expectedOwner && marker===expectedMarker;
      state.frontendIdentityValid=valid;
      state.frontendIdentityReason=valid ? '' : `Frontend identity mismatch · expected ${expectedMarker || 'declared build'} · received ${marker || 'unverified build'}`;
      $('versionPill').textContent=valid ? 'v131 · R8 · PL46 · 8086' : `${expectedVersion || 'v131.1.6'} · IDENTITY FAIL`;
      if (!valid) notice(state.frontendIdentityReason,'negative');
    } catch (error) {
      state.frontendIdentity=null;
      state.frontendIdentityValid=false;
      state.frontendIdentityReason=`Frontend identity unavailable · ${error.message}`;
      $('versionPill').textContent=`${document.documentElement.dataset.buildVersion || 'v131.1.6'} · IDENTITY FAIL`;
      notice(state.frontendIdentityReason,'negative');
    }
    const initialRoute=parseHashRoute();
    const initialPage=initialRoute.page;
    if (initialPage==='research') state.researchFocusCandidate=text(initialRoute.params.candidate);
    if (initialPage==='report' && initialRoute.params.symbol) await openStock(initialRoute.params.symbol,initialRoute.params.mode,initialRoute.params.instrument||'',initialRoute.params.decision||'',initialRoute.params.research||'');
    else showPage(initialPage, {push:false});
    loadLiveTruth();
    if (initialPage !== 'workspace') loadWorkspace();
    setInterval(() => {
      if (state.page === 'workspace' || state.page === 'opportunities') loadWorkspace();
    }, 3000);
    setInterval(() => { if (state.page === 'workspace' || state.page === 'opportunities' || state.page === 'report') loadLiveTruth(); }, 1200);
    setInterval(() => {
      if (state.page === 'system') loadOperationsControl({silent:true});
    }, 2500);
  }

  initialize();
})();
