const $ = (selector) => document.querySelector(selector);
const state = { mode: 'demo', applyEnabled: false, configLoaded: false };

function money(value) {
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 }).format(Number(value || 0));
}

function showToast(message, error = false) {
  const toast = document.createElement('div');
  toast.className = `toast${error ? ' error' : ''}`;
  toast.textContent = message;
  document.body.appendChild(toast);
  window.setTimeout(() => toast.remove(), 4200);
}

function setView(name) {
  document.querySelectorAll('.view').forEach((view) => view.classList.toggle('active', view.id === name));
  document.querySelectorAll('.nav-item').forEach((item) => item.classList.toggle('active', item.dataset.view === name));
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `Request failed (${response.status})`);
  return payload;
}

function renderDemo(data) {
  const monthly = data.monthly;
  const daily = data.daily;
  const monthlyPct = monthly.cap_usd ? Math.min(100, monthly.spend_usd / monthly.cap_usd * 100) : 0;
  const dailyPct = daily.cap_usd ? Math.min(100, daily.spend_usd / daily.cap_usd * 100) : 0;
  $('#monthly-spend').textContent = money(monthly.spend_usd);
  $('#monthly-cap-label').textContent = `of ${money(monthly.cap_usd)}`;
  $('#monthly-remaining').textContent = `${money(Math.max(0, monthly.cap_usd - monthly.spend_usd))} remaining`;
  $('#monthly-progress').style.width = `${monthlyPct}%`;
  $('#monthly-period').textContent = monthly.period;
  $('#daily-spend').textContent = money(daily.spend_usd);
  $('#daily-cap-label').textContent = `of ${money(daily.cap_usd)}`;
  $('#daily-remaining').textContent = `${money(Math.max(0, daily.cap_usd - daily.spend_usd))} remaining`;
  $('#daily-progress').style.width = `${dailyPct}%`;
  $('#daily-period').textContent = daily.period;
  $('#raise-floor').textContent = `${money(daily.spend_usd)} + ${money(monthly.cap_usd)} / ${data.daily_raise.monthly_to_daily_divisor} = ${money(data.daily_raise.minimum_next_tier_usd)}`;
  $('#move-preview').hidden = false;
  $('#move-label').textContent = 'SIMULATED GROUP MOVE · NO ACTION TAKEN';
  $('#from-group').textContent = data.tier_move_example.current_group;
  $('#to-group').textContent = data.tier_move_example.target_group;
  $('#move-detail').textContent = 'The real helper adds the target tier group first, then removes the old configured tier group. Demo mode does not change group membership.';

  const tiers = data.daily_raise.tiers;
  const currentIndex = tiers.reduce((found, tier, index) => tier <= daily.cap_usd ? index : found, -1);
  const nextIndex = tiers.findIndex((tier) => tier >= data.daily_raise.minimum_next_tier_usd);
  const track = $('#tier-track');
  track.replaceChildren();
  tiers.forEach((tier, index) => {
    const step = document.createElement('div');
    step.className = `tier-step${index === currentIndex ? ' current' : ''}${index === nextIndex ? ' next' : ''}`;
    const bar = document.createElement('span');
    bar.className = 'tier-bar';
    bar.style.height = `${18 + index * 7}px`;
    const amount = document.createElement('span');
    amount.textContent = money(tier);
    step.append(bar, amount);
    track.append(step);
  });
}

function renderLive(report) {
  const totals = report.totals || {};
  const monthlySpend = Number(totals.month_to_date_spend_usd || 0);
  const dailySpend = Number(totals.daily_spend_usd || 0);
  $('#monthly-spend').textContent = money(monthlySpend);
  $('#monthly-cap-label').textContent = 'estimated · month to date';
  $('#monthly-remaining').textContent = 'System-table estimate; no cap comparison';
  $('#monthly-progress').style.width = '0%';
  $('#monthly-period').textContent = 'telemetry estimate';
  $('#daily-spend').textContent = money(dailySpend);
  $('#daily-cap-label').textContent = 'estimated · today UTC';
  $('#daily-remaining').textContent = 'System-table estimate; no cap comparison';
  $('#daily-progress').style.width = '0%';
  $('#daily-period').textContent = 'telemetry estimate';
  $('#raise-floor').textContent = 'daily spend + configured monthly cap / 15';
  $('#live-spend-panel').hidden = false;
  $('#freshness').textContent = `Queried ${new Date(report.queried_at).toLocaleString()} · sources may lag enforcement`;
  const body = $('#spend-rows');
  body.replaceChildren();
  (report.rows || []).slice(0, 30).forEach((row) => {
    const tr = document.createElement('tr');
    [row.principal, money(row.daily_spend_usd), money(row.month_to_date_spend_usd), row.latest_event_at || '—'].forEach((value) => {
      const td = document.createElement('td');
      td.textContent = String(value);
      tr.append(td);
    });
    body.append(tr);
  });
  if (!body.children.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 4;
    td.textContent = 'No matching usage records for the current month.';
    tr.append(td);
    body.append(tr);
  }
}

async function refreshLiveSpend() {
  try {
    const report = await api('/api/spend-estimates');
    renderLive(report);
    showToast('Spend estimates refreshed.');
  } catch (error) {
    showToast(error.message, true);
  }
}

function showResult(title, text, showApply = false) {
  $('#result-panel').hidden = false;
  $('#result-title').textContent = title;
  $('#result-output').textContent = text;
  $('#apply-controls').hidden = !showApply;
  $('#result-panel').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function loadExample() {
  const payload = await api('/api/example-config');
  $('#config-editor').value = payload.config_yaml;
  state.configLoaded = true;
}

async function runConfigAction(action) {
  const configYaml = $('#config-editor').value;
  try {
    const result = await api(`/api/${action}`, {
      method: 'POST',
      body: JSON.stringify({ config_yaml: configYaml }),
    });
    const title = action === 'plan' ? 'Reconciliation plan' : action === 'verify' ? 'Verification' : 'Configuration validation';
    const display = result.plan || result.verification || result.message || JSON.stringify(result, null, 2);
    showResult(title, typeof display === 'string' ? display : JSON.stringify(display, null, 2), action === 'plan' && state.applyEnabled);
    showToast(action === 'plan' ? 'Plan generated.' : `${title} complete.`);
  } catch (error) {
    showResult(`${action} failed`, error.message);
    showToast(error.message, true);
  }
}

async function applyChanges() {
  if (!state.applyEnabled) return;
  const confirmation = $('#apply-confirmation').value.trim();
  const configYaml = $('#config-editor').value;
  try {
    const result = await api('/api/apply', {
      method: 'POST',
      body: JSON.stringify({
        config_yaml: configYaml,
        confirmation,
        apply_budgets: $('#apply-budgets').checked,
      }),
    });
    showResult('Apply completed', `${result.message}\n\nDurable audit records:\n${result.audit_records.join('\n')}`);
    $('#apply-confirmation').value = '';
    showToast('Changes applied and audit records persisted.');
  } catch (error) {
    showResult('Apply failed', error.message);
    showToast(error.message, true);
  }
}

async function initialize() {
  $('#today-label').textContent = new Intl.DateTimeFormat('en', { dateStyle: 'medium', timeZone: 'UTC' }).format(new Date()).toUpperCase();
  try {
    const status = await api('/api/status');
    state.mode = status.mode;
    state.applyEnabled = status.apply_enabled && status.apply_audit_ready;
    $('#environment').textContent = status.mode.toUpperCase();
    $('#connection-label').textContent = status.mode === 'demo' ? 'Synthetic preview' : 'Live workspace mode';
    $('#freshness').textContent = status.mode === 'demo'
      ? 'Synthetic sample data · no workspace connected'
      : 'Live mode · telemetry may lag native budget enforcement';
    if (state.mode === 'demo') {
      const data = await api('/api/demo-data');
      renderDemo(data);
    } else {
      $('#live-spend-panel').hidden = false;
      $('#monthly-spend').textContent = '—';
      $('#daily-spend').textContent = '—';
      $('#monthly-cap-label').textContent = 'estimated · month to date';
      $('#daily-cap-label').textContent = 'estimated · today UTC';
      $('#monthly-remaining').textContent = 'Load live telemetry to view estimates';
      $('#daily-remaining').textContent = 'Load live telemetry to view estimates';
      $('#raise-floor').textContent = 'daily spend + configured monthly cap / 15';
      $('#move-preview').hidden = false;
      $('#move-label').textContent = 'OPERATOR TIER MOVES · TIERS VIEW';
      $('#from-group').textContent = 'current configured tier';
      $('#to-group').textContent = 'next eligible tier';
      $('#move-detail').textContent = 'Admins preview and apply tier moves from the Tiers view. Engineer self-service raises are a later phase.';
      await refreshLiveSpend();
    }
    await loadExample();
  } catch (error) {
    showToast(`Initialization failed: ${error.message}`, true);
  }
}

document.querySelectorAll('.nav-item').forEach((item) => item.addEventListener('click', () => setView(item.dataset.view)));
document.querySelectorAll('[data-go]').forEach((item) => item.addEventListener('click', () => setView(item.dataset.go)));
document.querySelectorAll('[data-action]').forEach((item) => item.addEventListener('click', () => {
  setView('configuration');
  runConfigAction(item.dataset.action);
}));
$('#load-example').addEventListener('click', () => loadExample().then(() => showToast('Example loaded.')).catch((error) => showToast(error.message, true)));
$('#validate').addEventListener('click', () => runConfigAction('config/validate'));
$('#make-plan').addEventListener('click', () => runConfigAction('plan'));
$('#apply-button').addEventListener('click', applyChanges);
$('#clear-result').addEventListener('click', () => { $('#result-panel').hidden = true; });
$('#refresh-spend').addEventListener('click', refreshLiveSpend);


function tierBody() {
  const spend = $('#tier-spend').value.trim();
  return {
    config_yaml: $('#config-editor').value,
    email: $('#tier-email').value.trim(),
    kind: $('#tier-kind').value,
    target: $('#tier-target').value.trim() || null,
    spend_usd: spend === '' ? null : Number(spend),
  };
}

function showTier(title, payload, controls = null) {
  $('#tier-result-panel').hidden = false;
  $('#tier-result-title').textContent = title;
  $('#tier-result-output').textContent = typeof payload === 'string' ? payload : JSON.stringify(payload, null, 2);
  $('#tier-move-controls').hidden = controls !== 'move' || !state.applyEnabled;
  $('#tier-reset-controls').hidden = controls !== 'reset' || !state.applyEnabled;
}

async function tierAction(path, body, title, controls = null) {
  if (state.mode !== 'live') {
    showTier(title, 'Tier operations need live mode. Demo mode never reads or changes group membership.');
    return;
  }
  try {
    const result = await api(path, { method: 'POST', body: JSON.stringify(body) });
    const actionable = controls === 'move' ? (result.operations || []).length > 0 && result.eligible !== false
      : controls === 'reset' ? result.memberships_removed > 0 : false;
    showTier(title, result, actionable ? controls : null);
  } catch (error) {
    showTier(`${title} failed`, error.message);
    showToast(error.message, true);
  }
}

$('#tier-preview').addEventListener('click', () => tierAction('/api/tiers/preview', tierBody(), 'Tier move preview', 'move'));
$('#tier-move').addEventListener('click', () => tierAction('/api/tiers/move', { ...tierBody(), confirmation: $('#tier-confirmation').value.trim() }, 'Tier move applied'));
$('#tier-reset-preview').addEventListener('click', () => tierAction('/api/tiers/reset-runaway', { config_yaml: $('#config-editor').value, dry_run: true }, 'Runaway reset preview', 'reset'));
$('#tier-reset').addEventListener('click', () => tierAction('/api/tiers/reset-runaway', { config_yaml: $('#config-editor').value, dry_run: false, confirmation: $('#tier-reset-confirmation').value.trim() }, 'Runaway reset applied'));

initialize();
