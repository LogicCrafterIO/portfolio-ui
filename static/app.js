/* 4Pillar options book — vanilla SPA: hash of state in memory, all writes via /api. */
(() => {
  'use strict';

  // ------------------------------------------------------------------ state
  let S = null;                 // server read model (calc.build_view)
  const UI = {
    tab: 'presession',
    asOf: '',
    store: null,
    live: null,
    filters: { field: 'exit_date', from: '', to: '', ticker: '' },
    sort: { key: 'exit_date', dir: -1 },
    showLegs: true,
    mergeSel: new Set(),
    wlNotes: localStorage.getItem('ui.wlNotes') === '1',
    wlFocus: null,
  };

  // ------------------------------------------------------------------ utils
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));

  const isNum = (v) => v !== null && v !== undefined && v !== '' && !Number.isNaN(Number(v));
  const n = (v) => (isNum(v) ? Number(v) : 0);

  function num(v, dp = 2) {
    if (!isNum(v)) return '—';
    const x = Number(v);
    const s = Math.abs(x).toLocaleString('en-US', { minimumFractionDigits: dp, maximumFractionDigits: dp });
    return (x < 0 ? '-' : '') + s;
  }
  function money(v, dp = 2) {
    if (!isNum(v)) return '—';
    const x = Number(v);
    return (x < 0 ? '-' : '') + '$' + Math.abs(x).toLocaleString('en-US',
      { minimumFractionDigits: dp, maximumFractionDigits: dp });
  }
  function pct(v, dp = 1) {
    if (!isNum(v)) return '—';
    return (Number(v) * 100).toFixed(dp) + '%';
  }
  const cls = (v) => (!isNum(v) ? 'muted' : Number(v) > 0 ? 'pos' : Number(v) < 0 ? 'neg' : 'muted');
  const cell = (v, dp = 2, extra = '') => `<td class="${cls(v)} ${extra}">${num(v, dp)}</td>`;
  const moneyCell = (v, dp = 2, extra = '') => `<td class="${cls(v)} ${extra}">${money(v, dp)}</td>`;

  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  function monthLabel(key, withYear = true) {
    if (!key || key.length < 7) return key || '—';
    const y = key.slice(0, 4), m = Number(key.slice(5, 7));
    return withYear ? `${MONTHS[m - 1]} ${y}` : MONTHS[m - 1];
  }
  function stamp(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    return d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  }
  const todayISO = () => {
    const d = new Date();
    const p = (x) => String(x).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  };

  // ------------------------------------------------------------------ api
  async function api(path, { method = 'GET', body = null } = {}) {
    const opts = { method, headers: {} };
    if (body !== null) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(path, opts);
    const text = await res.text();
    let data = null;
    if (text) { try { data = JSON.parse(text); } catch { data = { detail: text }; } }
    if (!res.ok) {
      let detail = data && (data.detail || data.message);
      if (Array.isArray(detail)) detail = detail.map((d) => `${(d.loc || []).slice(1).join('.')}: ${d.msg}`).join('; ');
      throw new Error(detail || `${res.status} ${res.statusText}`);
    }
    return data;
  }

  function absorb(data) {
    if (data && data.view) {
      S = data.view;
      UI.asOf = S.meta.as_of;
      render();
    }
  }

  async function reload(showError = true) {
    try {
      const q = UI.asOf ? `?as_of=${encodeURIComponent(UI.asOf)}` : '';
      const data = await api('/api/state' + q);
      S = data.view;
      UI.asOf = S.meta.as_of;
      if (UI.asOf) $('#asof-input').value = UI.asOf;
      render();
    } catch (err) {
      if (showError) toast('Could not load book: ' + err.message, 'bad');
    }
  }

  // ------------------------------------------------------------------ toast
  // Short by design: a toast gets ~2.5s, an Undo offer 4s (still clickable the
  // whole time), a failure 5s because it has to be read. Any toast also
  // disappears the moment you click it.
  const TOAST_MS = { plain: 2500, action: 4000, bad: 5000 };

  function toast(message, kind = '', action = null) {
    const root = $('#toast-root');
    const div = document.createElement('div');
    div.className = 'toast ' + kind;
    const text = document.createElement('span');
    text.textContent = message;
    div.appendChild(text);
    const life = action ? TOAST_MS.action : kind === 'bad' ? TOAST_MS.bad : TOAST_MS.plain;
    const timer = setTimeout(() => div.remove(), life);
    div.addEventListener('click', (e) => {
      if (e.target.closest('.toast-act')) return;
      clearTimeout(timer);
      div.remove();
    });
    if (action) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'toast-act';
      btn.textContent = action.label;
      btn.addEventListener('click', () => {
        btn.disabled = true;
        clearTimeout(timer);
        div.remove();
        action.run();
      });
      div.appendChild(btn);
    }
    root.appendChild(div);
  }

  // ------------------------------------------------------------------ modal
  const MODAL = { onSubmit: null, open: false };

  function fieldHTML(f) {
    if (f.type === 'heading') {
      return `<div class="field-heading full">${esc(f.label)}</div>`;
    }
    if (f.type === 'note') {
      const id = f.key ? ` id="f-${esc(f.key)}"` : '';
      return `<div class="field-note full"${id}>${f.label}</div>`;
    }
    const id = 'f-' + f.key;
    const value = f.value === null || f.value === undefined ? '' : String(f.value);
    const common = `name="${esc(f.key)}" id="${id}" ${f.required ? 'required' : ''}`;
    let control = '';
    if (f.type === 'select') {
      const opts = (f.options || []).map((o) => {
        const val = typeof o === 'string' ? o : o.value;
        const label = typeof o === 'string' ? o : o.label;
        return `<option value="${esc(val)}" ${String(val) === value ? 'selected' : ''}>${esc(label)}</option>`;
      }).join('');
      control = `<select ${common}>${opts}</select>`;
    } else if (f.type === 'textarea') {
      control = `<textarea ${common} rows="${f.rows || 3}" placeholder="${esc(f.placeholder || '')}">${esc(value)}</textarea>`;
    } else if (f.type === 'checkbox') {
      control = `<input type="checkbox" ${common} ${value === 'true' ? 'checked' : ''}>`;
    } else if (f.type === 'datalist') {
      const listId = `${id}-list`;
      const opts = (f.options || []).map((o) => `<option value="${esc(o)}"></option>`).join('');
      control = `<input ${common} type="text" value="${esc(value)}" list="${listId}" placeholder="${esc(f.placeholder || '')}"><datalist id="${listId}">${opts}</datalist>`;
    } else {
      const type = f.type || 'text';
      const step = f.step ? `step="${f.step}"` : (type === 'number' ? 'step="any"' : '');
      control = `<input ${common} type="${type}" value="${esc(value)}" ${step} placeholder="${esc(f.placeholder || '')}">`;
    }
    return `<label class="field ${f.full ? 'full' : ''}"><span>${esc(f.label)}${f.required ? ' *' : ''}</span>
      ${control}${f.hint ? `<div class="hint">${esc(f.hint)}</div>` : ''}</label>`;
  }

  function openModal({ title, fields, submitLabel = 'Save', onSubmit, wide = false, onMount = null }) {
    MODAL.onSubmit = onSubmit;
    MODAL.open = true;
    $('#modal-title').textContent = title;
    $('#modal-form').innerHTML = fields.map(fieldHTML).join('');
    $('#modal-submit').textContent = submitLabel;
    $('#modal-error').textContent = '';
    $('#modal-root').hidden = false;
    $('.modal').style.width = wide ? 'min(880px, calc(100vw - 32px))' : '';
    if (onMount) onMount();
    const first = $('#modal-form input, #modal-form select, #modal-form textarea');
    if (first) setTimeout(() => first.focus(), 30);
  }

  function closeModal() {
    MODAL.open = false;
    MODAL.onSubmit = null;
    $('#modal-root').hidden = true;
    $('#modal-form').innerHTML = '';
  }

  function modalError(msg) { $('#modal-error').textContent = msg; }

  // ------------------------------------------------------------------ chrome
  function renderChrome() {
    if (!S) return;
    const meta = S.meta;
    $('#asof-input').value = meta.as_of || todayISO();
    const live = UI.live;
    const pill = $('#quote-status');
    if (live) {
      const errors = (live.errors || []).length;
      pill.className = 'pill ' + (errors ? 'warn' : 'ok');
      pill.textContent = `Quotes ${stamp(live.ts)}${errors ? ` · ${errors} issue${errors > 1 ? 's' : ''}` : ' · live'}`;
      pill.title = [live.source || '', ...(live.errors || []).map((e) => `${e.ticker}: ${e.error}`)].join('\n');
    } else {
      const last = (S.positions.flatMap((p) => p.legs.map((l) => l.quote_ts)).filter(Boolean)).sort().pop();
      pill.className = 'pill ' + (last ? 'ok' : 'subtle');
      pill.textContent = last ? `Quotes ${stamp(last)}` : 'No live quotes yet';
      pill.title = 'Pull live data (yfinance, ~15 min delay)';
    }
    const store = $('#store-status');
    if (UI.store) {
      store.textContent = `Saved ${stamp(meta.updated_at)} · ${UI.store.store ? UI.store.store.split('/').slice(-2).join('/') : 'json'}`;
      store.title = UI.store.store || '';
    }
  }

  // ------------------------------------------------------------------ KPI strip
  const kpi = (label, value, sub = '', kind = '') =>
    `<div class="kpi ${kind}"><div class="kpi-label">${esc(label)}</div>
      <div class="kpi-value">${value}</div>${sub ? `<div class="kpi-sub">${sub}</div>` : ''}</div>`;

  function renderPresessionKPIs() {
    const sum = S.summary;
    const months = S.months.months;
    const current = months[0] || {};
    const longShort = `L ${money(sum.long_market_value, 0)} · S ${money(sum.short_market_value, 0)}`;
    $('#ps-kpis').innerHTML = [
      kpi('Opening balance', money(sum.opening_balance), 'O/P balance'),
      kpi('Realised', money(sum.realised_all_time), `${monthLabel(current.key, false) || 'this month'}: ${money(sum.realised_this_month)}`, cls(sum.realised_all_time)),
      kpi('Booked this month', money(sum.booked_this_month),
        `known/worse case · incl. realised ${money(sum.realised_this_month, 0)}`),
      kpi('Unrealised', money(sum.unrealised), `exposure ${money(sum.exposure, 0)} · ${longShort}`, cls(sum.unrealised)),
      kpi('Commissions', money(sum.commission), 'all-time, feeds account balance'),
      kpi('Account balance', money(sum.account_balance), 'opening + realised − commission + open P/L', 'hero'),
    ].join('');
  }

  // ------------------------------------------------------------------ month grid
  function renderMonthGrid() {
    const M = S.months;
    const months = M.months || [];
    const currentKey = months[0] ? months[0].key : '';
    const perPos = {};
    months.forEach((m) => (m.companies || []).forEach((c) => {
      perPos[c.position_id] = perPos[c.position_id] || {};
      perPos[c.position_id][m.key] = c;
    }));

    const colHeads = months.map((m) => `
      <th class="monthhead monthcell ${m.key === currentKey ? 'is-current' : ''}" colspan="3">
        ${esc(m.label)}${m.key === currentKey ? ' <span class="tag this">now</span>' : ''}
        <span class="thirdfri">3rd Fri ${new Date(m.third_friday + 'T00:00:00').toLocaleDateString([], { day: 'numeric', month: 'short' })}</span>
      </th>`).join('');

    const subHeads = months.map((m) => `
      <th class="monthcell ${m.key === currentKey ? 'is-current' : ''}">Known</th>
      <th class="${m.key === currentKey ? 'is-current' : ''}">Actual</th>
      <th class="${m.key === currentKey ? 'is-current' : ''}">Swing</th>`).join('');

    const posRows = (S.positions || []).map((pos) => {
      const cells = months.map((m) => {
        const c = perPos[pos.id] ? perPos[pos.id][m.key] : null;
        const booked = c ? c.booked : 0;
        const actual = c ? c.actual : pos.position_pl;
        const swing = c ? c.swing : (isNum(actual) ? n(actual) - n(booked) : null);
        const overridden = c && (c.legs || []).some((l) => l.overridden);
        const tip = overridden ? ' title="one or more legs use a manual booking month"' : '';
        return `<td class="monthcell ${cls(booked)} ${booked ? 'flag' : ''} ${m.key === currentKey ? 'is-current' : ''}"${tip}>${num(booked, 0)}</td>
                ${cell(actual, 0, m.key === currentKey ? 'is-current' : '')}
                ${cell(swing, 0, m.key === currentKey ? 'is-current' : '')}`;
      }).join('');
      return `<tr>
        <td class="sticky name"><b>${esc(pos.ticker)}</b>
          <span class="tag ${pos.bias === 'Short' ? 'short' : 'long'}">${esc(pos.bias)}</span>
          <span class="muted">${esc(pos.category)}</span></td>
        ${cells}</tr>`;
    }).join('');

    const totalRows = `
      <tr class="total">
        <td class="sticky name">Total known / worse case</td>
        ${months.map((m) => `
          <td class="monthcell ${cls(m.known)} ${m.key === currentKey ? 'is-current' : ''}">${num(m.known, 0)}</td>
          ${cell(m.actual, 0, m.key === currentKey ? 'is-current' : '')}
          ${cell(m.swing, 0, m.key === currentKey ? 'is-current' : '')}`).join('')}
      </tr>
      <tr class="sub">
        <td class="sticky name">Cum. known (all realised + booked legs)</td>
        ${months.map((m) => `
          <td class="monthcell ${cls(m.cumulative_booked)} ${m.key === currentKey ? 'is-current' : ''}">${num(m.cumulative_booked, 0)}</td>
          <td class="${m.key === currentKey ? 'is-current' : ''}"></td>
          <td class="${m.key === currentKey ? 'is-current' : ''}"></td>`).join('')}
      </tr>
      <tr class="sub">
        <td class="sticky name">Cum. balance after commissions</td>
        ${months.map((m) => `
          <td class="monthcell ${cls(m.cumulative_balance)} ${m.key === currentKey ? 'is-current' : ''}">${num(m.cumulative_balance, 0)}</td>
          <td class="${m.key === currentKey ? 'is-current' : ''}"></td>
          <td class="${m.key === currentKey ? 'is-current' : ''}"></td>`).join('')}
      </tr>`;

    $('#ps-months').innerHTML = `
      <div class="card-head">
        <div>
          <h2>Monthly booking &amp; swing factor</h2>
          <p class="subtle">Known/worse case = &minus;total cost for every leg whose <b>booking month</b> is that
            month; actual = live P/L; swing = actual &minus; known.</p>
        </div>
        <div class="row-gap">
          <button class="btn ghost small" data-act="pin-month">+ month</button>
        </div>
      </div>
      <div class="card-note">
        <b>Booking month rule:</b> the 3rd Friday closes the month &mdash; a 4th or 5th Friday expiry books into the
        following month. Month columns generate themselves from live leg expiries and drop off as time passes
        (as of <b>${esc(S.meta.as_of)}</b>). ${M.unpriced ? '<span class="neg">Some legs have no price, so totals are incomplete.</span>' : ''}
      </div>
      <div class="table-wrap"><table class="grid">
        <thead><tr><th class="sticky">Position</th>${colHeads}</tr>
          <tr><th class="sticky"></th>${subHeads}</tr></thead>
        <tbody>
          ${posRows || '<tr><td class="empty" colspan="2">No open positions yet.</td></tr>'}
          <tr class="sub">
            <td class="sticky name">Realised booked this month</td>
            ${months.map((m) => `
              <td class="monthcell ${cls(m.realised_pl)} ${m.key === currentKey ? 'is-current' : ''}">${num(m.realised_pl, 0)}</td>
              <td class="${m.key === currentKey ? 'is-current' : ''}"></td>
              <td class="${m.key === currentKey ? 'is-current' : ''}"></td>`).join('')}
          </tr>
          ${totalRows}
        </tbody>
      </table></div>
      <p class="footnote">A dot on a Known cell means at least one leg has a manual booking month.
        Hover any header for the exact 3rd-Friday cutoff.</p>`;
  }

  // ------------------------------------------------------------------ positions
  function quoteBadge(leg) {
    if (leg.mid_source === 'override') {
      return '<span class="quote-dot manual"></span><span class="muted">manual mid</span>';
    }
    const q = leg.quote || {};
    const status = leg.quote_status || (q.status || '');
    if (!leg.mid && !status) return '<span class="muted">manual</span>';
    const label = status ? status.replace('-', ' ') : 'manual';
    // a carried book is a spread that was really there, taken earlier: name when
    const title = q.book_ts
      ? `book from ${stamp(q.book_ts)} · pulled ${stamp(q.ts)} · ${q.book_source || q.source || ''}`
      : q.ts ? `${q.source || ''} ${stamp(q.ts)}` : 'no live quote';
    // a typed mid sitting on top of a real book is not used for pricing: say so rather
    // than letting it disappear silently
    const ignored = leg.mid_override_ignored
      ? ` · manual mid ${num(leg.mid_override, 3)} ignored (live book used)` : '';
    return `<span class="quote-dot ${esc(status || 'ok')}" title="${esc(title + ignored)}"></span><span class="muted">${esc(label)}</span>`;
  }

  function legRow(pos, leg) {
    const bookLabel = leg.effective_book_month ? monthLabel(leg.effective_book_month) : '—';
    const overridden = leg.book_month_override ? ' <span class="tag this" title="manual booking month">manual</span>' : '';
    const expired = leg.status === 'expired' ? ' <span class="tag this">expired</span>' : '';
    const opened = n(leg.contracts_initial);
    const contracts = opened > n(leg.contracts)
      ? `${num(leg.contracts, 0)}<span class="muted" title="${num(opened, 0)} opened, ${num(opened - n(leg.contracts), 0)} closed"> /${num(opened, 0)}</span>`
      : num(leg.contracts, 0);
    return `<tr>
      <td class="left side ${leg.side === 'Short' ? 'neg' : 'pos'}">${esc(leg.side)}</td>
      <td class="left">${esc(leg.type)}${expired}</td>
      <td>${num(leg.strike, 2)}</td>
      <td>${esc(leg.expiry || '—')}</td>
      <td class="left">${esc(bookLabel)}${overridden}</td>
      <td>${contracts}</td>
      <td>${num(leg.multiplier, 0)}</td>
      <td>${num(leg.cost_premium, 2)}</td>
      <td>${num(leg.bid, 2)}</td>
      <td><b>${leg.mid == null ? '—' : num(leg.mid, 3)}</b></td>
      <td>${num(leg.ask, 2)}</td>
      ${moneyCell(leg.total_cost)}
      ${moneyCell(leg.market_value)}
      ${cell(leg.option_pl_ps, 3)}
      ${moneyCell(leg.position_pl)}
      <td class="${cls(leg.pl_pct)}">${pct(leg.pl_pct)}</td>
      <td class="left">${quoteBadge(leg)}</td>
      <td class="actions">
        <button class="btn ghost small" data-act="edit-leg" data-pid="${esc(pos.id)}" data-lid="${esc(leg.id)}">edit</button>
        <button class="btn ghost small" data-act="close-leg" data-pid="${esc(pos.id)}" data-lid="${esc(leg.id)}">close</button>
        <button class="btn ghost small danger" data-act="delete-leg" data-pid="${esc(pos.id)}" data-lid="${esc(leg.id)}"
          title="Remove this leg only">del</button>
      </td>
    </tr>`;
  }

  function positionCard(pos) {
    const legs = pos.legs || [];
    const earnings = pos.earnings_date
      ? `<span title="${esc(pos.earnings_date)}">${esc(pos.next_earnings || pos.earnings_date)}</span>`
      : esc(pos.next_earnings || '—');
    const uq = pos.underlying_quote;
    const uqAge = uq && uq.ts ? ` <span class="muted" title="${esc(uq.source || '')}">${stamp(uq.ts)}</span>` : '';
    const warnings = (pos.warnings || []).length
      ? `<div class="warnline">${pos.warnings.map(esc).join(' &middot; ')}</div>` : '';
    return `<div class="pos-card">
      <div class="pos-head">
        <div>
          <span class="pos-ticker">${esc(pos.ticker)}</span>
          <span class="tag ${pos.bias === 'Short' ? 'short' : 'long'}" ${pos.bias_auto
            ? 'title="bias follows the legs"' : 'title="bias is set by hand"'}
            >${esc(pos.bias)}${pos.bias_auto ? ' · auto' : ''}</span>
          <span class="tag">${esc(pos.category)}</span>
          ${pos.bias_mismatch ? `<button class="bias-fix" data-act="fix-bias" data-pid="${esc(pos.id)}"
            title="The label and the legs disagree — switch this position to Auto (${esc(pos.bias_derived)})"
            >labelled ${esc(pos.bias_labelled)} · legs say ${esc(pos.bias_derived)} — fix</button>` : ''}
        </div>
        <div class="pos-meta">
          <span>${esc(pos.company)}</span>
          <span>&middot;</span>
          <span>last <b>${num(pos.underlying_price, 2)}</b>${uqAge}</span>
          <span>&middot;</span>
          <span>earnings ${earnings}</span>
          <span>&middot;</span>
          <span>${legs.length} leg${legs.length === 1 ? '' : 's'}</span>
        </div>
        <div class="pos-pl">
          <div class="big ${cls(pos.position_pl)}">${money(pos.position_pl)}</div>
          <div class="muted">${pct(pos.pl_pct)} on ${money(Math.abs(n(pos.total_cost)), 0)}</div>
        </div>
        <div class="pos-actions">
          <button class="btn ghost small" data-act="refresh-position" data-pid="${esc(pos.id)}">refresh</button>
          <button class="btn ghost small" data-act="add-leg" data-pid="${esc(pos.id)}">+ leg</button>
          <button class="btn ghost small" data-act="edit-position" data-pid="${esc(pos.id)}">edit</button>
          <button class="btn ghost small danger" data-act="delete-position" data-pid="${esc(pos.id)}"
            title="Remove ${esc(pos.ticker)} and its ${pos.legs.length} leg(s) from the book">delete</button>
        </div>
      </div>
      ${warnings}
      <div class="legs-wrap"><table class="legs">
        <thead><tr>
          <th class="left">Side</th><th class="left">Type</th><th>Strike</th><th>Expiry</th>
          <th class="left">Books in</th><th>Cont</th><th>&times;</th><th>Cost</th><th>Bid</th><th>Mid</th><th>Ask</th>
          <th>Cost $</th><th>Mkt val</th><th>Opt P/L</th><th>Pos P/L</th><th>%</th>
          <th class="left">Quote</th><th></th>
        </tr></thead>
        <tbody>
          ${legs.length ? legs.map((l) => legRow(pos, l)).join('')
            : '<tr><td colspan="18" class="empty">No legs yet — use “+ leg”.</td></tr>'}
        </tbody>
      </table></div>
    </div>`;
  }

  function renderPositions() {
    const positions = S.positions || [];
    $('#ps-pos-hint').textContent = `${positions.length} position${positions.length === 1 ? '' : 's'} · ${positions.reduce((a, p) => a + (p.leg_count || 0), 0)} live legs`;
    $('#ps-positions').innerHTML = positions.length
      ? positions.map(positionCard).join('')
      : `<div class="card"><div class="empty">No open positions. Use “+ Position” to add one.</div></div>`;
  }

  // ------------------------------------------------------------------ realised
  const STRUCTURES = [
    { value: 'single', label: 'Single option' },
    { value: 'calendar', label: 'Calendar (same strike, two expiries)' },
    { value: 'vertical', label: 'Vertical (same expiry, two strikes)' },
    { value: 'diagonal', label: 'Diagonal (strike + expiry differ)' },
    { value: 'roll', label: 'Roll' },
    { value: 'custom', label: 'Custom / multi-leg' },
  ];
  const STRUCTURE_CLASS = {
    single: 'chip-single', calendar: 'chip-calendar', vertical: 'chip-vertical',
    diagonal: 'chip-diagonal', roll: 'chip-roll', custom: 'chip-custom',
  };

  function filteredTrades() {
    const f = UI.filters;
    const from = f.from || '';
    const to = f.to || '';
    const tick = (f.ticker || '').trim().toUpperCase();
    let rows = (S.realised || []).filter((t) => {
      if (tick && !(t.ticker || '').includes(tick)) return false;
      if (f.field === 'book_month') {
        const k = t.book_month || '';
        if (from && (!k || k < from.slice(0, 7))) return false;
        if (to && (!k || k > to.slice(0, 7))) return false;
        return true;
      }
      const d = t[f.field] || '';
      if (from && (!d || d < from)) return false;
      if (to && (!d || d > to)) return false;
      return true;
    });
    const { key, dir } = UI.sort;
    rows.sort((a, b) => {
      const av = a[key], bv = b[key];
      if (isNum(av) && isNum(bv)) return (Number(av) - Number(bv)) * dir;
      const as = String(av ?? ''), bs = String(bv ?? '');
      return as.localeCompare(bs) * dir;
    });
    return rows;
  }

  // The sheet's M column: a leg's own span, falling back to the trade's dates.
  // The server sends leg.days; older payloads do not, so derive it from dates.
  function daysBetween(a, b) {
    if (!a || !b) return null;
    const d1 = new Date(`${a}T00:00:00`);
    const d2 = new Date(`${b}T00:00:00`);
    if (Number.isNaN(d1.getTime()) || Number.isNaN(d2.getTime())) return null;
    return Math.round((d2 - d1) / 86400000);
  }
  function legDays(t, l) {
    if (isNum(l.days)) return Number(l.days);
    const d = daysBetween(l.entry_date || t.entry_date, l.exit_date || t.exit_date);
    return d == null ? 0 : d;
  }

  function kpisOf(trades) {
    // One closed leg is one trade -- the same unit as a row on the sheet's
    // Realised tab, so gross wins/losses, win %, R and Kelly agree with it.
    const units = trades.flatMap((t) => (t.legs || []).map((l) => ({ t, l })));
    const count = units.length;
    const pls = units.map((u) => n(u.l.pl));
    const wins = pls.filter((v) => v > 0);
    const losses = pls.filter((v) => v < 0);
    const winDollars = wins.reduce((a, b) => a + b, 0);
    const lossDollars = losses.reduce((a, b) => a + b, 0);
    const winRate = count ? wins.length / count : null;
    const r = lossDollars ? winDollars / Math.abs(lossDollars) : null;
    const kelly = r && winRate != null ? winRate - (1 - winRate) / r : null;
    const days = units.map((u) => legDays(u.t, u.l));
    return {
      count, legs: count, winRate, winDollars, lossDollars, r, kelly,
      closedPl: winDollars + lossDollars,
      commission: trades.reduce((a, t) => a + n(t.commission), 0),
      avgDays: days.length ? days.reduce((a, b) => a + b, 0) / days.length : null,
      wins: wins.length, losses: losses.length,
    };
  }

  const SORTS = {
    trade_no: 'Trade', ticker: 'Ticker', structure: 'Structure', leg_count: 'Legs',
    entry_date: 'Entry', exit_date: 'Exit', days: 'Days', pl_pct: 'Profit %', pl: '$ P/L',
    commission: 'Commission',
  };

  function legRowMarkup(trade, leg) {
    const long = leg.side === 'Long';
    return `<tr class="legrow">
        <td class="pick"></td>
        <td colspan="5" class="left">
          <span class="chip ${long ? 'chip-long' : 'chip-short'}">${esc(leg.side[0])}</span>
          <b>${num(leg.strike, 2)} ${esc(leg.type)}</b>
          <span class="muted">${esc(leg.expiry || '—')} · ${num(leg.contracts, 0)}× ${num(leg.multiplier, 0)}</span>
        </td>
        <td class="left muted mono">${num(leg.price_in, 2)} → ${num(leg.price_out, 2)}</td>
        <td class="muted">${esc(leg.expiry || '—')}</td>
        <td class="${cls(leg.pl_pct)}">${pct(leg.pl_pct)}</td>
        ${moneyCell(leg.pl)}
        <td colspan="2" class="left muted">cost ${money(leg.cost_basis, 0)}${leg.notes ? ` · ${esc(leg.notes)}` : ''}</td>
        <td class="actions">
          <button class="btn ghost small" data-act="edit-trade-leg" data-id="${esc(trade.id)}"
            data-lid="${esc(leg.id)}">edit</button>
          <button class="btn ghost small danger" data-act="delete-trade-leg" data-id="${esc(trade.id)}"
            data-lid="${esc(leg.id)}" title="Remove this leg from the trade">−</button>
        </td>
      </tr>`;
  }

  function renderRealised() {
    const trades = filteredTrades();
    const k = kpisOf(trades);
    const all = S.kpis || {};

    $('#rl-kpis').innerHTML = [
      kpi('Closed P/L', money(k.closedPl), `net of ${money(k.commission)} commission`, cls(k.closedPl)),
      kpi('Trades', String(k.count), 'closed legs — one leg = one trade'),
      kpi('Win %', k.winRate == null ? '—' : pct(k.winRate, 2), `${k.wins}W / ${k.losses}L by leg`),
      kpi('Wins', money(k.winDollars), 'sum of winning legs', 'pos'),
      kpi('Losses', money(k.lossDollars), 'sum of losing legs', 'neg'),
      kpi('R multiple', k.r == null ? '—' : k.r.toFixed(2), 'wins ÷ |losses|'),
      kpi('Kelly %', k.kelly == null ? '—' : pct(k.kelly, 2), 'win% − (loss% ÷ R)'),
      kpi('Avg days', k.avgDays == null ? '—' : k.avgDays.toFixed(2), 'per closed leg'),
      kpi('Commission', money(k.commission), 'all-time ' + money(all.commission)),
    ].join('');

    const toggle = $('#rl-legs-toggle');
    if (toggle) toggle.textContent = `Legs: ${UI.showLegs ? 'shown' : 'hidden'}`;

    const sortMark = (key) => (UI.sort.key === key ? (UI.sort.dir > 0 ? ' ▲' : ' ▼') : '');
    const th = (key, label = SORTS[key], left = true) =>
      `<th class="${left ? 'left' : ''}" data-sort="${key}" style="cursor:pointer"
        title="Sort by ${esc(label)}">${esc(label)}${sortMark(key)}</th>`;

    const body = [];
    trades.forEach((t) => {
      const structure = t.structure || 'single';
      const legs = t.legs || [];
      const picked = UI.mergeSel.has(t.id);
      body.push(`<tr class="traderow ${picked ? 'picked' : ''}">
        <td class="pick"><input type="checkbox" data-act="pick-trade" data-id="${esc(t.id)}"
          ${picked ? 'checked' : ''} title="Select trade #${esc(t.trade_no ?? '')} for merging"></td>
        <td>${esc(t.trade_no ?? '')}</td>
        <td class="left"><b>${esc(t.ticker)}</b></td>
        <td class="left muted">${esc(t.company)}</td>
        <td class="left"><span class="chip ${STRUCTURE_CLASS[structure] || 'chip-custom'}">${esc(t.structure_label || structure)}</span></td>
        <td>${legs.length}</td>
        <td>${esc(t.entry_date || '—')}</td>
        <td>${esc(t.exit_date || '—')}</td>
        <td>${num(t.days, 0)}</td>
        <td class="${cls(t.pl_pct)}">${pct(t.pl_pct)}</td>
        ${moneyCell(t.pl)}
        <td class="left muted">${monthLabel(t.book_month)}</td>
        <td>${num(t.commission, 2)}</td>
        <td class="actions">
          <button class="btn ghost small" data-act="add-trade-leg" data-id="${esc(t.id)}"
            title="Add a second (or further) leg — a calendar is one trade, not two">+ leg</button>
          <button class="btn ghost small" data-act="edit-trade" data-id="${esc(t.id)}">edit</button>
          <button class="btn ghost small danger" data-act="delete-trade" data-id="${esc(t.id)}"
            title="Remove this closed trade and its legs">del</button>
        </td>
      </tr>`);
      if (UI.showLegs) legs.forEach((leg) => body.push(legRowMarkup(t, leg)));
    });

    UI.mergeSel.forEach((id) => { if (!(S.realised || []).some((t) => t.id === id)) UI.mergeSel.delete(id); });
    const allPicked = trades.length > 0 && trades.every((t) => UI.mergeSel.has(t.id));
    syncMergeButton(trades);

    const colCount = 14;
    $('#rl-table').innerHTML = `<table>
      <thead><tr>
        <th class="pick"><input type="checkbox" data-act="pick-all" ${allPicked ? 'checked' : ''}
          title="Select every trade in view"></th>
        <th class="left">#</th>${th('ticker')}<th class="left">Company</th>
        ${th('structure')}${th('leg_count')}${th('entry_date')}${th('exit_date')}${th('days')}
        ${th('pl_pct', 'Profit %')}${th('pl', '$ P/L')}<th class="left">Books in</th>${th('commission', 'Comm')}
        <th></th>
      </tr></thead>
      <tbody>${body.join('') ||
        `<tr><td colspan="${colCount}" class="empty">No trades in this date range.</td></tr>`}</tbody>
      <tfoot><tr class="total">
        <td></td>
        <td colspan="8" class="left">${trades.length} position${trades.length === 1 ? '' : 's'}
          · ${k.count} closed leg${k.count === 1 ? '' : 's'}</td>
        <td class="${cls(k.closedPl)}">${money(k.closedPl)}</td>
        <td></td>
        <td></td>
        <td>${num(k.commission, 2)}</td>
        <td></td>
      </tr></tfoot>
    </table>`;

    const byMonth = {};
    (S.realised || []).forEach((t) => {
      if (!t.book_month) return;
      byMonth[t.book_month] = (byMonth[t.book_month] || 0) + n(t.pl);
    });
    const monthBits = Object.keys(byMonth).sort()
      .map((key) => `${monthLabel(key, false)} ${money(byMonth[key], 0)}`).join(' · ');
    const allK = kpisOf(S.realised || []);
    $('#rl-footnote').innerHTML = `All-time closed P/L <b>${money(allK.closedPl)}</b> across
      <b>${allK.count} closed leg${allK.count === 1 ? '' : 's'}</b> in ${(S.realised || []).length} position${(S.realised || []).length === 1 ? '' : 's'}.
      Win % is counted per closed leg — one leg is one trade — so a calendar that wins on one leg and
      loses on the other is one win and one loss, exactly as the sheet counts it. Realised books into
      the pre-session monthly block by the third-Friday cutoff: ${monthBits || '—'}.`;
  }

  // ------------------------------------------------------------------ watchlist
  // One fixed row per sector block of the workbook, LONG and SHORT side by
  // side. Cells are the editor: type to add, × on a chip to remove. Every
  // sector row is always drawn, empty or not, so a blank sector is a visible
  // gap rather than a missing tab.
  const WL_SECTORS = [
    'Consumer Staples', 'Consumer Discretionary', 'Tech Hardware', 'Tech Software',
    'Comm Services', 'Industrials', 'Materials', 'Financials', 'Energy', 'Healthcare',
    'Utilities /Real Estate',
  ];
  const TICKER_RE = /^[A-Z][A-Z0-9.\-]{0,9}$/;

  const wlSameSector = (a, b) => String(a || '').trim().toLowerCase()
    === String(b || '').trim().toLowerCase();

  function wlSectorRows() {
    const known = new Set(WL_SECTORS.map((s) => s.toLowerCase()));
    const rows = WL_SECTORS.slice();
    (S.watchlist || []).forEach((w) => {
      const sector = w.sector || 'Unclassified';
      if (!known.has(sector.toLowerCase())) { known.add(sector.toLowerCase()); rows.push(sector); }
    });
    return rows;
  }

  function wlChip(w) {
    const detail = [w.sub_sector, w.conviction, w.comment, w.catalyst,
      w.source === 'deepseek' ? 'written by the DeepSeek harness' : '']
      .filter(Boolean).join(' · ');
    const dot = w.source === 'deepseek'
      ? '<span class="wl-ds" title="written by the DeepSeek harness"></span>' : '';
    const note = UI.wlNotes
      ? `<span class="wl-note">${esc(w.sub_sector || w.comment || '—')}</span>` : '';
    return `<span class="wl-chip" title="${esc(`${w.ticker} ${w.side}${detail ? ' — ' + detail : ''}`)}">
        <span class="wl-chip-top">${dot}<b>${esc(w.ticker)}</b>
          <button class="wl-x" data-act="drop-watch" data-id="${esc(w.id)}" tabindex="-1"
            title="Remove ${esc(w.ticker)}">&times;</button></span>${note}</span>`;
  }

  function wlCell(sector, side, items) {
    const placeholder = items.length ? '+ ticker' : 'add ticker';
    return `<td class="wl-cell" data-sector="${esc(sector)}" data-side="${side}">
        <div class="wl-chips">${items.map(wlChip).join('')}</div>
        <input class="wl-add" type="text" autocomplete="off" spellcheck="false"
          data-wl-cell="1" data-sector="${esc(sector)}" data-side="${side}"
          placeholder="${placeholder}" aria-label="Add a ${side} candidate to ${esc(sector)}">
      </td>`;
  }

  function wlInput(sector, side) {
    return $$('.wl-add').find((el) => el.dataset.sector === sector && el.dataset.side === side) || null;
  }

  function focusWlCell(sector, side) {
    const input = wlInput(sector, side);
    if (!input) return;
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
    const cell = input.closest('td');
    if (cell && cell.scrollIntoView) cell.scrollIntoView({ block: 'nearest' });
  }

  function renderWatchlist() {
    const items = S.watchlist || [];
    const dsh = items.filter((i) => i.source === 'deepseek').length;
    const last = items.map((i) => i.updated_at).filter(Boolean).sort().pop();
    $('#wl-status').textContent = `${items.length} candidates · ${dsh} from harness · updated ${stamp(last)}`;
    const toggle = $('#wl-notes-toggle');
    if (toggle) toggle.textContent = `Notes: ${UI.wlNotes ? 'on' : 'off'}`;

    const body = wlSectorRows().map((sector) => {
      const longs = items.filter((i) => wlSameSector(i.sector, sector) && i.side === 'LONG');
      const shorts = items.filter((i) => wlSameSector(i.sector, sector) && i.side === 'SHORT');
      const count = longs.length + shorts.length;
      return `<tr class="wl-row">
          <th class="wl-sec">${esc(sector)}<span class="wl-count">${count
            ? `${longs.length}L · ${shorts.length}S` : 'empty'}</span></th>
          ${wlCell(sector, 'LONG', longs)}${wlCell(sector, 'SHORT', shorts)}
        </tr>`;
    }).join('');

    $('#wl-groups').innerHTML = `<table class="wl-sheet">
        <thead><tr><th class="wl-sec">Sector</th>
          <th class="wl-side long">Long candidates</th>
          <th class="wl-side short">Short candidates</th></tr></thead>
        <tbody>${body}</tbody></table>`;

    if (UI.wlFocus) {
      const target = UI.wlFocus;
      UI.wlFocus = null;
      focusWlCell(target.sector, target.side);
    }
  }

  // ------------------------------------------------------------------ render
  function render() {
    if (!S) return;
    const scroll = window.scrollY;
    renderChrome();
    $$('.tab').forEach((t) => t.classList.toggle('active', t.dataset.tab === UI.tab));
    $$('.panel').forEach((p) => { p.hidden = p.id !== `panel-${UI.tab}`; });
    if (UI.tab === 'presession') {
      renderPresessionKPIs();
      renderMonthGrid();
      renderPositions();
    } else if (UI.tab === 'realised') {
      renderRealised();
    } else {
      renderWatchlist();
    }
    requestAnimationFrame(() => window.scrollTo(0, scroll));
  }

  // ------------------------------------------------------------------ forms
  function positionFields(pos = {}) {
    const cats = S.categories || [];
    return [
      { key: 'ticker', label: 'Ticker', value: pos.ticker || '', required: true, placeholder: 'BB' },
      { key: 'company', label: 'Company', value: pos.company || '', placeholder: 'Blackberry' },
      { key: 'category', label: 'Category', type: 'datalist', options: cats, value: pos.category || cats[0] || '',
        hint: 'Drives the Long/Short gross market value matrix.' },
      { key: 'bias', label: 'Bucket bias', type: 'select',
        options: [{ value: 'auto', label: 'Auto — follow the legs' }, 'Long', 'Short'],
        value: pos.bias_labelled || pos.bias || 'auto',
        hint: 'A put position is bearish (Short) even though the leg is Long; Auto follows the legs.' },
      { key: 'next_earnings', label: 'Next earnings (text)', value: pos.next_earnings || '',
        placeholder: '24th Dec' },
      { key: 'underlying_price', label: 'Underlying price', type: 'number', value: pos.underlying_price ?? '' },
      { key: 'notes', label: 'Notes', type: 'textarea', full: true, value: pos.notes || '' },
    ];
  }

  function legFields(leg = {}) {
    return [
      { key: 'side', label: 'Side', type: 'select', options: ['Long', 'Short'], value: leg.side || 'Long' },
      { key: 'type', label: 'Type', type: 'select', options: ['CALL', 'PUT'], value: leg.type || 'CALL' },
      { key: 'strike', label: 'Strike', type: 'number', value: leg.strike ?? '', required: true },
      { key: 'expiry', label: 'Expiration', type: 'date', value: leg.expiry || '', required: true },
      { key: 'contracts', label: 'Contracts', type: 'number', value: leg.contracts ?? 1, required: true },
      { key: 'multiplier', label: 'Multiplier', type: 'number', value: leg.multiplier ?? 100 },
      { key: 'cost_premium', label: 'Premium paid/received', type: 'number', value: leg.cost_premium ?? '',
        hint: 'Positive number; a short leg books it as a credit.' },
      { key: 'bid', label: 'Bid', type: 'number', value: leg.bid ?? '' },
      { key: 'ask', label: 'Ask', type: 'number', value: leg.ask ?? '' },
      { key: 'mid_override', label: 'Mid override', type: 'number', value: leg.mid_override ?? '',
        hint: 'Only prices a leg with no live bid/ask; a real book always wins.' },
      { key: 'book_month_override', label: 'Book in month', type: 'month', value: leg.book_month_override || '',
        hint: 'Blank = expiry month, rolled by the 3rd-Friday rule.' },
      { key: 'opened', label: 'Opened', type: 'date', value: leg.opened || '' },
      { key: 'notes', label: 'Notes', type: 'textarea', full: true, value: leg.notes || '' },
    ];
  }

  function tradeHeadFields(t = {}, { withNumber = true, autoStructure = false } = {}) {
    const structureOpts = autoStructure
      ? [{ value: 'auto', label: 'Auto — derive from the legs' }, ...STRUCTURES]
      : STRUCTURES;
    const fields = [];
    if (withNumber) fields.push({ key: 'trade_no', label: 'Trade #', type: 'number', value: t.trade_no ?? '' });
    fields.push(
      { key: 'ticker', label: 'Ticker', value: t.ticker || '', required: true },
      { key: 'company', label: 'Company', value: t.company || '' },
      { key: 'structure', label: 'Structure', type: 'select', options: structureOpts,
        value: t.structure || (autoStructure ? 'auto' : 'single') },
      { key: 'entry_date', label: 'Entry date', type: 'date', value: t.entry_date || '' },
      { key: 'exit_date', label: 'Exit date', type: 'date', value: t.exit_date || todayISO() },
      { key: 'commission', label: 'Commission (whole trade)', type: 'number', value: t.commission ?? 0 },
      { key: 'notes', label: 'Notes', type: 'textarea', full: true, value: t.notes || '' },
    );
    return fields;
  }

  function realisedLegFields(leg = {}, prefix = '') {
    const p = (key) => `${prefix}${key}`;
    return [
      { key: p('side'), label: 'Long / Short', type: 'select', options: ['Long', 'Short'],
        value: leg.side || 'Long' },
      { key: p('type'), label: 'Type', type: 'select', options: ['CALL', 'PUT'],
        value: leg.type || 'CALL' },
      { key: p('strike'), label: 'Strike', type: 'number', value: leg.strike ?? '' },
      { key: p('expiry'), label: 'Expiration', type: 'date', value: leg.expiry || '' },
      { key: p('contracts'), label: 'Contracts', type: 'number', value: leg.contracts ?? 1 },
      { key: p('multiplier'), label: 'Multiplier', type: 'number',
        value: leg.multiplier ?? (S.meta.default_multiplier || 100) },
      { key: p('price_in'), label: 'Price in', type: 'number', value: leg.price_in ?? '' },
      { key: p('price_out'), label: 'Price out', type: 'number', value: leg.price_out ?? '' },
      { key: p('notes'), label: 'Leg notes', value: leg.notes || '', full: true },
    ];
  }

  function pickLeg(values, prefix) {
    const leg = {};
    Object.keys(values).forEach((key) => {
      if (!key.startsWith(prefix)) return;
      leg[key.slice(prefix.length)] = values[key];
    });
    return leg;
  }

  function settingsFields() {
    const meta = S.meta;
    return [
      { key: 'opening_balance', label: 'Opening balance (O/P)', type: 'number', value: meta.opening_balance ?? '' },
      { key: 'default_multiplier', label: 'Default contract multiplier', type: 'number',
        value: meta.default_multiplier ?? 100 },
      { key: 'booking_rule', label: 'Booking rule', type: 'select',
        options: [{ value: 'third_friday', label: 'Third Friday closes the month' }, { value: 'calendar', label: 'Plain calendar month' }],
        value: meta.booking_rule || 'third_friday' },
      { key: 'as_of', label: 'Default as-of date', type: 'date', value: meta.as_of || '',
        hint: 'Book valuations and month columns are computed against this date.' },
    ];
  }

  // ------------------------------------------------------------------ actions
  async function submitModal(fn) {
    try {
      modalError('');
      await fn();
      closeModal();
    } catch (err) {
      modalError(err.message);
    }
  }

  function actAddPosition() {
    openModal({
      title: 'New position',
      fields: positionFields(),
      submitLabel: 'Create position',
      onSubmit: (v) => submitModal(async () => {
        absorb(await api('/api/positions', { method: 'POST', body: v }));
        toast('Position created', 'good');
      }),
    });
  }

  async function actFixBias(pid) {
    const pos = S.positions.find((p) => p.id === pid);
    if (!pos) return;
    absorb(await api(`/api/positions/${pid}`, { method: 'PATCH', body: { bias: 'auto' } }));
    toast(`${pos.ticker} bias now follows the legs (${pos.bias_derived})`, 'good');
  }

  function actEditPosition(pid) {
    const pos = S.positions.find((p) => p.id === pid);
    if (!pos) return;
    openModal({
      title: `Edit ${pos.ticker}`,
      fields: positionFields(pos),
      onSubmit: (v) => submitModal(async () => {
        absorb(await api(`/api/positions/${pid}`, { method: 'PATCH', body: v }));
        toast('Position saved', 'good');
      }),
    });
  }

  function actAddLeg(pid) {
    const pos = S.positions.find((p) => p.id === pid);
    if (!pos) return;
    openModal({
      title: `Add leg — ${pos.ticker}`,
      fields: legFields({ side: 'Long', type: 'CALL', contracts: 1, multiplier: 100 }),
      submitLabel: 'Add leg',
      onSubmit: (v) => submitModal(async () => {
        absorb(await api(`/api/positions/${pid}/legs`, { method: 'POST', body: v }));
        toast('Leg added', 'good');
      }),
    });
  }

  function actEditLeg(pid, lid) {
    const pos = S.positions.find((p) => p.id === pid);
    const leg = pos && pos.legs.find((l) => l.id === lid);
    if (!leg) return;
    openModal({
      title: `Edit leg — ${pos.ticker} ${leg.side} ${leg.type} ${leg.strike ?? ''}`,
      fields: legFields(leg),
      onSubmit: (v) => submitModal(async () => {
        absorb(await api(`/api/positions/${pid}/legs/${lid}`, { method: 'PATCH', body: v }));
        toast('Leg saved', 'good');
      }),
    });
  }

  function actCloseLeg(pid, lid) {
    const pos = S.positions.find((p) => p.id === pid);
    const leg = pos && pos.legs.find((l) => l.id === lid);
    if (!leg) return;
    const held = Math.round(n(leg.contracts));
    const opened = Math.round(n(leg.contracts_initial));
    const sign = leg.side === 'Short' ? -1 : 1;
    const mult = n(leg.multiplier) || 100;
    const premiumIn = n(leg.cost_premium);

    // Live read of what this exit books: the slice's P/L, and what stays behind.
    const preview = () => {
      const qtyField = $('#f-contracts');
      const pxField = $('#f-price_out');
      const box = $('#f-close-preview');
      const submit = $('#modal-submit');
      const raw = qtyField ? qtyField.value : '';
      const qty = raw === '' ? held : Math.round(Number(raw));
      const px = pxField && pxField.value !== '' ? Number(pxField.value) : NaN;
      const valid = Number.isFinite(qty) && qty >= 1 && qty <= held;
      const whole = valid && qty >= held;
      if (submit) {
        submit.textContent = whole ? 'Book to Realised' : (valid ? `Book ${qty} of ${held}` : 'Book to Realised');
        submit.disabled = !valid;
      }
      if (!box) return;
      if (!valid) {
        box.innerHTML = Number.isFinite(qty) && qty > held
          ? `<b>${qty}</b> is more than the ${held} contract${held === 1 ? '' : 's'} still open on this leg.`
          : `Enter 1 to ${held} contracts (the leg holds ${held}).`;
        return;
      }
      const rest = held - qty;
      if (!Number.isFinite(px)) {
        box.innerHTML = `Closing <b>${qty} of ${held}</b> contract${held === 1 ? '' : 's'} — `
          + `${rest ? `${rest} left open` : 'the leg leaves the book'}. Enter an exit premium to see the P/L.`;
        return;
      }
      const pl = sign * (px - premiumIn) * qty * mult;
      box.innerHTML = `Books <b class="${cls(pl)}">${money(pl)}</b> on <b>${qty} of ${held}</b> `
        + `contract${held === 1 ? '' : 's'} (cost basis ${money(premiumIn * qty * mult, 0)}) — `
        + (rest
          ? `<b>${rest}</b> contract${rest === 1 ? '' : 's'} stay${rest === 1 ? 's' : ''} open at `
            + `${num(premiumIn, 2)} in, ${money(premiumIn * rest * mult, 0)} of basis left.`
          : 'the leg leaves the book.');
    };

    openModal({
      title: `Close ${pos.ticker} ${leg.side} ${leg.type} ${num(leg.strike, 2)} ${leg.expiry}`,
      submitLabel: 'Book to Realised',
      onMount: () => {
        ['#f-contracts', '#f-price_out'].forEach((sel) => {
          const el = $(sel);
          if (!el) return;
          el.addEventListener('input', preview);
          el.addEventListener('change', preview);
        });
        preview();
      },
      fields: [
        { key: 'price_out', label: 'Exit premium', type: 'number', required: true,
          value: leg.mid ?? '', hint: 'Per share, as traded — same units as price in.' },
        { key: 'contracts', label: 'Contracts to close', type: 'number', required: true,
          value: held, step: 1,
          hint: opened && opened > held
            ? `of ${held} still open (${opened} opened, ${opened - held} already booked) — close fewer to leave the rest running.`
            : `of ${held} on the leg — leave it at ${held} to close the whole thing.` },
        { key: 'exit_date', label: 'Exit date', type: 'date', value: todayISO(), required: true },
        { key: 'commission', label: 'Commission', type: 'number', value: 0 },
        { key: 'keep_leg', label: 'Keep the leg row with 0 contracts (full close only)', type: 'checkbox', value: 'false' },
        { type: 'note', key: 'close-preview', label: '' },
        { key: 'notes', label: 'Notes', type: 'textarea', full: true, value: '' },
      ],
      onSubmit: (v) => submitModal(async () => {
        const qty = v.contracts === null ? held : Math.round(Number(v.contracts));
        if (!(qty >= 1 && qty <= held)) throw new Error(`Close between 1 and ${held} contracts.`);
        absorb(await api(`/api/positions/${pid}/legs/${lid}/close`, { method: 'POST', body: v }));
        toast(qty < held ? `Closed ${qty} of ${held} — ${held - qty} still open` : 'Moved to Realised', 'good');
      }),
    });
  }

  function actAddTrade() {
    openModal({
      title: 'New closed trade',
      wide: true,
      submitLabel: 'Create trade',
      fields: [
        { type: 'heading', label: 'Trade', key: 'h-trade' },
        ...tradeHeadFields({}, { withNumber: true, autoStructure: true }),
        { type: 'heading', label: 'Leg 1', key: 'h-leg1' },
        ...realisedLegFields({}, 'l1_'),
        { type: 'heading', label: 'Leg 2 — optional (a calendar is one trade, not two)', key: 'h-leg2' },
        ...realisedLegFields({ price_in: null }, 'l2_'),
      ],
      onSubmit: (v) => submitModal(async () => {
        const leg1 = pickLeg(v, 'l1_');
        const leg2 = pickLeg(v, 'l2_');
        const legs = [leg1, leg2].filter((leg) => leg.price_in !== null && leg.price_in !== '');
        if (!legs.length) throw new Error('Fill in the first leg (price in at minimum).');
        const body = {
          ticker: v.ticker,
          company: v.company,
          trade_no: v.trade_no,
          entry_date: v.entry_date,
          exit_date: v.exit_date,
          commission: v.commission,
          notes: v.notes,
          legs,
        };
        if (v.structure && v.structure !== 'auto') body.structure = v.structure;
        absorb(await api('/api/realised', { method: 'POST', body }));
        toast(`Trade created with ${legs.length} leg${legs.length === 1 ? '' : 's'}`, 'good');
      }),
    });
  }

  function actEditTrade(id) {
    const t = S.realised.find((x) => x.id === id);
    if (!t) return;
    openModal({
      title: `Edit trade #${t.trade_no ?? ''} ${t.ticker}`,
      wide: true,
      fields: tradeHeadFields(t, { withNumber: true }),
      onSubmit: (v) => submitModal(async () => {
        absorb(await api(`/api/realised/${id}`, { method: 'PATCH', body: v }));
        toast('Trade saved', 'good');
      }),
    });
  }

  function syncMergeButton(visible = filteredTrades()) {
    const picked = visible.filter((t) => UI.mergeSel.has(t.id));
    const btn = $('#rl-merge');
    if (btn) {
      btn.disabled = picked.length < 2;
      btn.textContent = picked.length ? `Merge selected (${picked.length})` : 'Merge selected';
      const tickers = Array.from(new Set(picked.map((t) => t.ticker)));
      btn.title = tickers.length > 1
        ? `Pick trades of one ticker — you have ${tickers.join(', ')}`
        : 'Fold the selected trades into one trade (a calendar is one trade, not two)';
    }
    const all = $('[data-act="pick-all"]');
    if (all) all.checked = visible.length > 0 && picked.length === visible.length;
    return picked;
  }

  function toggleMergePick(input, id) {
    if (!id) return;
    const on = !UI.mergeSel.has(id);
    if (on) UI.mergeSel.add(id);
    else UI.mergeSel.delete(id);
    if (input) input.checked = on;
    const row = input && input.closest('tr');
    if (row) row.classList.toggle('picked', on);
    syncMergeButton();
  }

  function pickAllTrades() {
    const visible = filteredTrades();
    const allPicked = visible.length > 0 && visible.every((t) => UI.mergeSel.has(t.id));
    visible.forEach((t) => (allPicked ? UI.mergeSel.delete(t.id) : UI.mergeSel.add(t.id)));
    render();
  }

  function actMergeTrades() {
    const picked = (S.realised || []).filter((t) => UI.mergeSel.has(t.id));
    if (picked.length < 2) {
      toast('Tick at least two trades first — they merge into one trade.', '');
      return;
    }
    const tickers = Array.from(new Set(picked.map((t) => t.ticker)));
    if (tickers.length > 1) {
      toast(`Merging needs a single ticker — you picked ${tickers.join(', ')}.`, '');
      return;
    }
    const legs = picked.reduce((a, t) => a + (t.legs || []).length, 0);
    const totalPl = picked.reduce((a, t) => a + n(t.pl), 0);
    const earliest = picked.map((t) => t.entry_date).filter(Boolean).sort()[0];
    const latest = picked.map((t) => t.exit_date).filter(Boolean).sort().pop();
    const detail = picked
      .slice()
      .sort((a, b) => (a.trade_no || 0) - (b.trade_no || 0))
      .map((t) => `<div class="merge-line"><b>#${esc(t.trade_no ?? '')} ${esc(t.ticker)}</b>
        <span class="muted">${esc(t.structure_label || t.structure)} · ${money(t.pl)}</span>
        <div class="muted small">${(t.legs || []).map((l) => esc(l.label)).join(' &nbsp;·&nbsp; ')}</div></div>`)
      .join('');

    openModal({
      title: `Merge ${picked.length} ${tickers[0]} trades into one`,
      wide: true,
      submitLabel: `Merge into one ${legs}-leg trade`,
      fields: [
        { type: 'heading', label: 'What goes in', key: 'h-in' },
        { type: 'note', label: detail, key: 'n-in' },
        { type: 'heading', label: 'Result', key: 'h-out' },
        { type: 'note', key: 'n-out', label:
          `<b>${legs} legs</b> · P/L <b>${money(totalPl)}</b> · entry ${esc(earliest || '—')} → `
          + `exit ${esc(latest || '—')} · commission ${money(picked.reduce((a, t) => a + n(t.commission), 0))}`
          + `<div class="muted small">Structure is re-derived from the legs. Dates widen to the outer `
          + `entry/exit; commission and notes combine.</div>` },
        { key: 'keep_id', label: 'Keep this trade number', type: 'select',
          options: picked.slice().sort((a, b) => (a.trade_no || 0) - (b.trade_no || 0))
            .map((t) => ({ value: t.id, label: `#${t.trade_no ?? ''} (${money(t.pl)})` })),
          value: picked.slice().sort((a, b) => (a.trade_no || 0) - (b.trade_no || 0))[0].id },
      ],
      onSubmit: (v) => submitModal(async () => {
        absorb(await api('/api/realised/merge', {
          method: 'POST', body: { ids: picked.map((t) => t.id), keep_id: v.keep_id },
        }));
        UI.mergeSel.clear();
        render();
        syncMergeButton();
        toast(`Merged ${picked.length} trades into one ${legs}-leg trade`, 'good');
      }),
    });
  }

  function actAddTradeLeg(tid) {
    const t = S.realised.find((x) => x.id === tid);
    if (!t) return;
    const last = (t.legs || [])[t.legs.length - 1] || {};
    openModal({
      title: `Add leg — #${t.trade_no ?? ''} ${t.ticker}`,
      wide: true,
      submitLabel: 'Add leg',
      fields: [
        { type: 'heading', label: `Second leg of a ${String(t.structure || 'single')} trade`, key: 'h' },
        ...realisedLegFields({ side: last.side === 'Long' ? 'Short' : 'Long', type: last.type || 'CALL',
          strike: last.strike, multiplier: last.multiplier }),
      ],
      onSubmit: (v) => submitModal(async () => {
        if (v.price_in === null || v.price_in === '') throw new Error('Price in is required.');
        absorb(await api(`/api/realised/${tid}/legs`, { method: 'POST', body: v }));
        toast('Leg added to the trade', 'good');
      }),
    });
  }

  function actEditTradeLeg(tid, lid) {
    const t = S.realised.find((x) => x.id === tid);
    const leg = t && (t.legs || []).find((l) => l.id === lid);
    if (!leg) return;
    openModal({
      title: `Edit leg — ${t.ticker} ${leg.side} ${leg.type} ${num(leg.strike, 2)}`,
      wide: true,
      fields: realisedLegFields(leg),
      onSubmit: (v) => submitModal(async () => {
        absorb(await api(`/api/realised/${tid}/legs/${lid}`, { method: 'PATCH', body: v }));
        toast('Leg saved', 'good');
      }),
    });
  }

  async function actDeleteTradeLeg(tid, lid) {
    const t = S.realised.find((x) => x.id === tid);
    const leg = t && (t.legs || []).find((l) => l.id === lid);
    if (!leg) return;
    if ((t.legs || []).length === 1) {
      toast('That is the only leg — delete the trade instead.', '');
      return;
    }
    if (!(await confirmDelete(`Remove the ${leg.side} ${leg.type} ${num(leg.strike, 2)} leg from trade #${t.trade_no}?`))) return;
    absorb(await api(`/api/realised/${tid}/legs/${lid}`, { method: 'DELETE' }));
    toast('Leg removed', 'good');
  }

  // watchlist: add straight from the cell you typed in, remove with the chip's ×
  async function wlAdd(sector, side, text) {
    const parsed = String(text || '').split(/[\s,;]+/)
      .map((t) => t.trim().toUpperCase()).filter(Boolean);
    const invalid = parsed.filter((t) => !TICKER_RE.test(t));
    const wanted = parsed.filter((t) => TICKER_RE.test(t));
    if (!wanted.length) {
      toast(`“${String(text || '').trim()}” is not a ticker — try something like AAPL`, 'bad');
      return false;
    }
    const all = S.watchlist || [];
    const sameCell = new Set(all.filter((w) => wlSameSector(w.sector, sector) && w.side === side)
      .map((w) => w.ticker));
    const elsewhere = wanted.filter((t) => !sameCell.has(t)
      && all.some((w) => w.ticker === t && w.side === side));
    const items = wanted.filter((t) => !sameCell.has(t) && !elsewhere.includes(t))
      .map((t) => ({ ticker: t, side, sector, source: 'manual', updated_by: 'manual' }));
    const notes = [];
    if (invalid.length) notes.push(`skipped ${invalid.join(', ')}`);
    if (elsewhere.length) {
      const where = elsewhere.map((t) => {
        const hit = all.find((w) => w.ticker === t && w.side === side);
        return `${t} (already ${side} under ${hit.sector})`;
      });
      notes.push(where.join(', '));
    }
    if (!items.length) {
      toast(notes.length ? notes.join(' · ') : `Already ${side} in ${sector}`, notes.length ? 'bad' : '');
      return false;
    }

    UI.wlFocus = { sector, side };
    absorb(await api('/api/watchlist/bulk', {
      method: 'POST',
      body: { items, mode: 'merge', updated_by: 'manual' },
    }));
    const added = items.map((i) => i.ticker).join(', ');
    toast(`Added ${added} — ${side} · ${sector}${notes.length ? ' · ' + notes.join(' · ') : ''}`, 'good');
    return true;
  }

  async function commitWlCell(input) {
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    try {
      await wlAdd(input.dataset.sector, input.dataset.side, text);
    } catch (err) {
      toast('Could not add: ' + err.message, 'bad');
    }
  }

  async function actDropWatch(id) {
    const w = (S.watchlist || []).find((x) => x.id === id);
    if (!w) return;
    const restore = { ...w };
    delete restore.id;
    try {
      absorb(await api(`/api/watchlist/${id}`, { method: 'DELETE' }));
    } catch (err) {
      toast('Could not remove: ' + err.message, 'bad');
      return;
    }
    toast(`Removed ${w.ticker} (${w.side} · ${w.sector})`, '', {
      label: 'Undo',
      run: async () => {
        try {
          absorb(await api('/api/watchlist', { method: 'POST', body: { ...restore, id: w.id } }));
          toast(`${w.ticker} put back`, 'good');
        } catch (err) { toast('Undo failed: ' + err.message, 'bad'); }
      },
    });
  }

  function actToggleWlNotes() {
    UI.wlNotes = !UI.wlNotes;
    localStorage.setItem('ui.wlNotes', UI.wlNotes ? '1' : '0');
    renderWatchlist();
  }

  function actSettings() {
    openModal({
      title: 'Book settings',
      fields: settingsFields(),
      onSubmit: (v) => submitModal(async () => {
        absorb(await api('/api/meta', { method: 'PATCH', body: v }));
        toast('Settings saved', 'good');
      }),
    });
  }

  function actPinMonth() {
    openModal({
      title: 'Pin a month column',
      submitLabel: 'Add month',
      fields: [{ key: 'month', label: 'Month', type: 'month', required: true, value: (S.meta.as_of || todayISO()).slice(0, 7),
        hint: 'Extends the grid past the last month the book reaches — every month in between is already drawn. Months before the as-of date are ignored.' }],
      onSubmit: (v) => submitModal(async () => {
        absorb(await api('/api/months', { method: 'POST', body: v }));
        toast(`Pinned ${monthLabel(v.month)}`, 'good');
      }),
    });
  }

  async function confirmDelete(message) {
    return window.confirm(message);
  }

  async function actDeletePosition(pid) {
    const pos = S.positions.find((p) => p.id === pid);
    if (!pos) return;
    if (!(await confirmDelete(`Delete ${pos.ticker} and its ${pos.legs.length} leg(s)?`))) return;
    try {
      absorb(await api(`/api/positions/${pid}`, { method: 'DELETE' }));
      toast('Position deleted', 'good');
    } catch (err) { toast(err.message, 'bad'); }
  }

  async function actDeleteLeg(pid, lid) {
    if (!(await confirmDelete('Delete this leg?'))) return;
    try {
      absorb(await api(`/api/positions/${pid}/legs/${lid}`, { method: 'DELETE' }));
      toast('Leg deleted', 'good');
    } catch (err) { toast(err.message, 'bad'); }
  }

  async function actDeleteTrade(id) {
    if (!(await confirmDelete('Delete this closed trade?'))) return;
    try {
      absorb(await api(`/api/realised/${id}`, { method: 'DELETE' }));
      toast('Trade deleted', 'good');
    } catch (err) { toast(err.message, 'bad'); }
  }

  async function doRefresh(tickers = null) {
    const buttons = [$('#btn-refresh')];
    buttons.forEach((b) => { b.disabled = true; const s = b.querySelector('.spin'); if (s) s.hidden = false; });
    const target = $('#btn-refresh .label');
    const original = target.textContent;
    target.textContent = 'Pulling…';
    try {
      const data = await api('/api/quotes/refresh', {
        method: 'POST',
        body: { tickers, include_earnings: true },
      });
      UI.live = data.report || null;
      if (data.view) { S = data.view; UI.asOf = S.meta.as_of; render(); }
      const errors = (UI.live && UI.live.errors) || [];
      if (errors.length) {
        toast(`Quotes updated with ${errors.length} issue${errors.length > 1 ? 's' : ''} — ${errors[0].ticker}: ${errors[0].error}`, 'bad');
      } else {
        toast('Live quotes updated (yfinance, ~15 min delay)', 'good');
      }
    } catch (err) {
      toast('Quote pull failed: ' + err.message, 'bad');
    } finally {
      buttons.forEach((b) => { b.disabled = false; const s = b.querySelector('.spin'); if (s) s.hidden = true; });
      target.textContent = original;
    }
  }

  // ------------------------------------------------------------------ events
  function bindEvents() {
    $('#tabbar').addEventListener('click', (e) => {
      const tab = e.target.closest('.tab');
      if (!tab) return;
      UI.tab = tab.dataset.tab;
      localStorage.setItem('ui.tab', UI.tab);
      render();
    });

    $('#asof-input').addEventListener('change', (e) => {
      UI.asOf = e.target.value;
      localStorage.setItem('ui.asOf', UI.asOf);
      reload();
    });
    $('#btn-today').addEventListener('click', () => {
      UI.asOf = '';
      localStorage.removeItem('ui.asOf');
      reload();
    });
    $('#btn-refresh').addEventListener('click', () => doRefresh());
    $('#btn-export').addEventListener('click', async () => {
      const q = S && S.meta.as_of ? `?as_of=${encodeURIComponent(S.meta.as_of)}` : '';
      // fetched, not navigated to: the builder can refuse (the tab has room for
      // three category buckets), and that answer used to replace the page with
      // a bare "Internal Server Error" and no workbook. Now the reason arrives
      // as a toast and the book stays where it was.
      let res;
      try {
        res = await fetch('/api/export/xlsx' + q);
      } catch (err) {
        toast('Could not reach the exporter: ' + err.message, 'bad');
        return;
      }
      if (!res.ok) {
        let why = `Export failed (HTTP ${res.status})`;
        try {
          const body = await res.json();
          why = body.detail || body.error || why;
        } catch { /* not JSON -- keep the status line */ }
        toast(why, 'bad');
        return;
      }
      const blob = await res.blob();
      const found = (res.headers.get('Content-Disposition') || '').match(/filename="?([^";]+)/);
      const href = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = href;
      link.download = found ? found[1] : 'options-book.xlsx';
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(href), 4000);

      // the export pulls fresh yfinance quotes first; say what that pull found, so a
      // pre-market sheet (no live book anywhere) is never mistaken for a broken one
      let summary = null;
      try { summary = JSON.parse(res.headers.get('X-Quote-Summary') || 'null'); } catch { /* older server */ }
      if (summary && summary.error) {
        toast('Exported, but the quote pull failed: ' + summary.error, 'bad');
      } else if (summary && summary.legs) {
        const bits = [`${summary.live} live`];
        if (summary.carried) bits.push(`${summary.carried} carried`);
        if (summary.no_book) bits.push(`${summary.no_book} with no book`);
        const kind = summary.no_book && !summary.live ? 'bad' : summary.live ? '' : 'plain';
        toast(`Exported · quotes: ${bits.join(' / ')}`, kind);
      } else {
        toast('Exported');
      }
      reload();
    });
    $('#btn-settings').addEventListener('click', actSettings);

    // date filters
    const filterInputs = { '#rl-datefield': 'field', '#rl-from': 'from', '#rl-to': 'to', '#rl-ticker': 'ticker' };
    Object.entries(filterInputs).forEach(([sel, key]) => {
      const el = $(sel);
      el.addEventListener('change', () => { UI.filters[key] = el.value; render(); });
      el.addEventListener('input', () => { UI.filters[key] = el.value; render(); });
    });
    $$('[data-preset]').forEach((btn) => btn.addEventListener('click', () => {
      const preset = btn.dataset.preset;
      const now = new Date();
      const iso = (d) => d.toISOString().slice(0, 10);
      if (preset === 'all') { UI.filters.from = ''; UI.filters.to = ''; }
      if (preset === 'month') {
        UI.filters.field = 'exit_date';
        UI.filters.from = iso(new Date(now.getFullYear(), now.getMonth(), 1));
        UI.filters.to = iso(new Date(now.getFullYear(), now.getMonth() + 1, 0));
      }
      if (preset === '90') {
        UI.filters.field = 'exit_date';
        UI.filters.from = iso(new Date(now.getTime() - 90 * 864e5));
        UI.filters.to = iso(now);
      }
      if (preset === 'ytd') {
        UI.filters.field = 'exit_date';
        UI.filters.from = `${now.getFullYear()}-01-01`;
        UI.filters.to = iso(now);
      }
      $('#rl-datefield').value = UI.filters.field;
      $('#rl-from').value = UI.filters.from;
      $('#rl-to').value = UI.filters.to;
      render();
    }));

    // watchlist cells: Enter saves, Escape clears, clicking away saves
    document.addEventListener('keydown', (e) => {
      const input = e.target.closest && e.target.closest('.wl-add');
      if (!input) return;
      if (e.key === 'Enter') {
        e.preventDefault();
        commitWlCell(input);
      } else if (e.key === 'Escape') {
        e.preventDefault();
        input.value = '';
        input.blur();
      }
    });

    document.addEventListener('focusout', (e) => {
      const input = e.target.closest && e.target.closest('.wl-add');
      if (input) commitWlCell(input);
    });

    // delegated clicks
    document.addEventListener('click', (e) => {
      const sortHeader = e.target.closest('[data-sort]');
      if (sortHeader) {
        const key = sortHeader.dataset.sort;
        if (UI.sort.key === key) UI.sort.dir *= -1;
        else { UI.sort.key = key; UI.sort.dir = key === 'ticker' ? 1 : -1; }
        render();
        return;
      }
      // click anywhere in a watchlist cell and you are typing in it
      const cellEl = e.target.closest('td.wl-cell');
      if (cellEl && !e.target.closest('.wl-x') && !e.target.closest('input')) {
        focusWlCell(cellEl.dataset.sector, cellEl.dataset.side);
        return;
      }
      const el = e.target.closest('[data-act]');
      if (!el) return;
      const { act, pid, lid, id } = el.dataset;
      switch (act) {
        case 'modal-close': closeModal(); break;
        case 'add-position': actAddPosition(); break;
        case 'edit-position': actEditPosition(pid); break;
        case 'fix-bias': actFixBias(pid); break;
        case 'delete-position': actDeletePosition(pid); break;
        case 'refresh-position': doRefresh([(S.positions.find((p) => p.id === pid) || {}).ticker].filter(Boolean)); break;
        case 'add-leg': actAddLeg(pid); break;
        case 'edit-leg': actEditLeg(pid, lid); break;
        case 'delete-leg': actDeleteLeg(pid, lid); break;
        case 'close-leg': actCloseLeg(pid, lid); break;
        case 'add-trade': actAddTrade(); break;
        case 'edit-trade': actEditTrade(id); break;
        case 'delete-trade': actDeleteTrade(id); break;
        case 'pick-trade': toggleMergePick(el, id); break;
        case 'pick-all': pickAllTrades(); break;
        case 'merge-trades': actMergeTrades(); break;
        case 'add-trade-leg': actAddTradeLeg(id); break;
        case 'edit-trade-leg': actEditTradeLeg(id, lid); break;
        case 'delete-trade-leg': actDeleteTradeLeg(id, lid); break;
        case 'toggle-legs':
          UI.showLegs = !UI.showLegs;
          render();
          break;
        case 'drop-watch': actDropWatch(id); break;
        case 'toggle-wl-notes': actToggleWlNotes(); break;
        case 'pin-month': actPinMonth(); break;
        default: break;
      }
    });

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && MODAL.open) closeModal();
    });

    $('#modal-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      if (!MODAL.onSubmit) return;
      const form = e.target;
      const values = {};
      Array.from(form.elements).forEach((input) => {
        if (!input.name) return;
        if (input.type === 'checkbox') values[input.name] = input.checked;
        else values[input.name] = input.value === '' ? null : input.value;
      });
      const submit = $('#modal-submit');
      submit.disabled = true;
      try {
        await MODAL.onSubmit(values);
      } catch (err) {
        modalError(err.message);
      } finally {
        submit.disabled = false;
      }
    });

  }

  // ------------------------------------------------------------------ boot
  async function boot() {
    UI.tab = localStorage.getItem('ui.tab') || 'presession';
    UI.asOf = localStorage.getItem('ui.asOf') || '';
    bindEvents();
    try {
      UI.store = await api('/api/health');
    } catch { /* non-fatal */ }
    await reload();
    if (!S) return;
    const hasQuotes = S.positions.some((p) => p.legs.some((l) => l.quote_ts));
    if (!hasQuotes && S.positions.length) {
      toast('No live quotes stored yet — use “Pull live data”.', '');
    }
  }

  boot();
})();
