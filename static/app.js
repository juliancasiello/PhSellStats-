const state = {
  platform: 'all',
  event_name: 'all',
  days: 7,
  month: 'all',
  date_mode: 'sale_date',
  granularity: 'hour',
  metric: 'revenue',
  seriesMode: 'normal',
  rollingWindow: 24,
  currency: 'ARS',
  lastAlertId: 0,
  timeseriesChart: null,
  eventsChart: null,
};

function fmtMoney(val) {
  const num = Number(val || 0);
  return new Intl.NumberFormat('es-AR', {
    style: 'currency',
    currency: state.currency || 'ARS',
    maximumFractionDigits: 0,
  }).format(num);
}

function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString('es-AR', {
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function platformBadge(plat) {
  if (plat === 'lumepic') {
    return '<span class="px-2 py-0.5 rounded-full text-[11px] font-medium bg-indigo-500/15 text-indigo-300 border border-indigo-500/30">Lumepic</span>';
  }
  if (plat === 'momentia') {
    return '<span class="px-2 py-0.5 rounded-full text-[11px] font-medium bg-emerald-500/15 text-emerald-300 border border-emerald-500/30">Momentia</span>';
  }
  if (plat === 'canon') {
    return '<span class="px-2 py-0.5 rounded-full text-[11px] font-medium bg-amber-500/15 text-amber-300 border border-amber-500/30">Canon Fijo</span>';
  }
  return `<span class="px-2 py-0.5 rounded-full text-[11px] font-medium bg-slate-700 text-slate-300">${plat}</span>`;
}

async function apiFetch(url, options = {}) {
  const res = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (res.status === 401) {
    window.location.href = '/login';
    throw new Error('No autenticado');
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Error HTTP ${res.status}`);
  }
  return res.json();
}

function fmtMonthLabel(ym) {
  const [y, m] = ym.split('-').map(Number);
  const d = new Date(y, (m || 1) - 1, 1);
  const name = d.toLocaleDateString('es-AR', { month: 'long', year: 'numeric' });
  return name.charAt(0).toUpperCase() + name.slice(1);
}

async function loadMonths() {
  const data = await apiFetch('/api/months');
  const sel = document.getElementById('filterMonth');
  if (!sel) return;
  const cur = state.month;
  sel.innerHTML = '<option value="all">Por período</option>';
  (data.months || []).forEach((ym) => {
    const opt = document.createElement('option');
    opt.value = ym;
    opt.textContent = fmtMonthLabel(ym);
    if (ym === cur) opt.selected = true;
    sel.appendChild(opt);
  });
}

async function loadAll() {
  await Promise.all([
    loadMonths(),
    loadSummary(),
    loadTimeseries(),
    loadEvents(),
    loadSales(),
    loadSyncStatus(),
  ]);
}

async function loadSummary() {
  const q = new URLSearchParams({
    platform: state.platform,
    event_name: state.event_name,
    days: state.days,
    month: state.month,
    date_mode: state.date_mode,
  });
  const data = await apiFetch(`/api/summary?${q}`);

  document.getElementById('kpiRevenue').textContent = fmtMoney(data.total_revenue);
  document.getElementById('kpiItems').textContent = data.total_items.toLocaleString('es-AR');
  document.getElementById('kpiSales').textContent = `${data.total_sales.toLocaleString('es-AR')} operaciones`;
  document.getElementById('kpiAvgTicket').textContent = fmtMoney(data.avg_ticket);
  document.getElementById('kpiVelocity').textContent = `${data.velocity_24h_per_hour} / h`;
  document.getElementById('kpiVelocitySub').textContent = `Última hora: ${data.sales_last_1h} ventas · 24h: ${data.sales_last_24h}`;

  const lume = data.by_platform.find((p) => p.platform === 'lumepic');
  const mom = data.by_platform.find((p) => p.platform === 'momentia');
  const canon = data.by_platform.find((p) => p.platform === 'canon');
  document.getElementById('kpiRevenueSplit').innerHTML = `
    <span class="text-indigo-300">Lumepic: ${fmtMoney(lume ? lume.revenue : 0)}</span>
    <span class="text-emerald-300">Momentia: ${fmtMoney(mom ? mom.revenue : 0)}</span>
    ${canon && canon.revenue > 0 ? `<span class="text-amber-300">Canon: ${fmtMoney(canon.revenue)}</span>` : ''}
  `;

  const demoBadge = document.getElementById('demoBadge');
  const clearQuick = document.getElementById('btnClearDemoQuick');
  if (data.has_demo_data) {
    demoBadge.classList.remove('hidden');
    clearQuick.classList.remove('hidden');
  } else {
    demoBadge.classList.add('hidden');
    clearQuick.classList.add('hidden');
  }
}

async function loadTimeseries() {
  const q = new URLSearchParams({
    granularity: state.granularity,
    platform: state.platform,
    event_name: state.event_name,
    days: state.days,
    month: state.month,
    date_mode: state.date_mode,
  });
  const rows = await apiFetch(`/api/timeseries?${q}`);

  const labels = rows.map((r) => {
    if (state.granularity === 'hour') return r.bucket.slice(5);
    if (state.granularity === 'month') return fmtMonthLabel(r.bucket);
    return r.bucket;
  });
  let lumeSeries = rows.map((r) =>
    state.metric === 'revenue' ? r.lumepic_revenue : (r.lumepic_items ?? r.lumepic_sales)
  );
  let momSeries = rows.map((r) =>
    state.metric === 'revenue' ? r.momentia_revenue : (r.momentia_items ?? r.momentia_sales)
  );

  if (state.seriesMode === 'cumulative') {
    let accL = 0;
    let accM = 0;
    lumeSeries = lumeSeries.map((v) => (accL += v));
    momSeries = momSeries.map((v) => (accM += v));
  } else if (state.seriesMode === 'rolling') {
    const win = Math.max(Number(state.rollingWindow) || 2, 2);
    const rollingSum = (arr) =>
      arr.map((_, i) => {
        let s = 0;
        const start = Math.max(0, i - win + 1);
        for (let j = start; j <= i; j += 1) s += arr[j];
        return s;
      });
    lumeSeries = rollingSum(lumeSeries);
    momSeries = rollingSum(momSeries);
  }

  const isLine = state.seriesMode !== 'normal';
  const ctx = document.getElementById('timeseriesChart').getContext('2d');
  if (state.timeseriesChart) state.timeseriesChart.destroy();

  state.timeseriesChart = new Chart(ctx, {
    type: isLine ? 'line' : 'bar',
    data: {
      labels,
      datasets: [
        {
          label: 'Lumepic',
          data: lumeSeries,
          borderColor: '#818cf8',
          backgroundColor: isLine ? 'rgba(129, 140, 248, 0.15)' : 'rgba(129, 140, 248, 0.75)',
          fill: isLine,
          tension: 0.3,
          pointRadius: isLine ? 0 : undefined,
          pointHitRadius: 8,
          borderRadius: 4,
        },
        {
          label: 'Momentia',
          data: momSeries,
          borderColor: '#34d399',
          backgroundColor: isLine ? 'rgba(52, 211, 153, 0.15)' : 'rgba(52, 211, 153, 0.75)',
          fill: isLine,
          tension: 0.3,
          pointRadius: isLine ? 0 : undefined,
          pointHitRadius: 8,
          borderRadius: 4,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { labels: { color: '#cbd5e1', font: { size: 11 } } },
        tooltip: {
          callbacks: {
            label: (ctxItem) => {
              const v = ctxItem.parsed.y;
              return `${ctxItem.dataset.label}: ${state.metric === 'revenue' ? fmtMoney(v) : v + ' fotos'}`;
            },
          },
        },
      },
      scales: {
        x: {
          stacked: !isLine,
          ticks: { color: '#94a3b8', maxTicksLimit: 12, font: { size: 10 } },
          grid: { color: 'rgba(148, 163, 184, 0.08)' },
        },
        y: {
          stacked: !isLine,
          beginAtZero: true,
          min: 0,
          ticks: {
            color: '#94a3b8',
            font: { size: 10 },
            callback: (val) => (state.metric === 'revenue' ? fmtMoney(val) : val),
          },
          grid: { color: 'rgba(148, 163, 184, 0.08)' },
        },
      },
    },
  });
}

async function loadEvents() {
  const q = new URLSearchParams({ days: state.days, month: state.month, platform: state.platform, date_mode: state.date_mode });
  const data = await apiFetch(`/api/events?${q}`);

  // Update event dropdown preserving current selection
  const select = document.getElementById('filterEvent');
  const current = state.event_name;
  select.innerHTML = '<option value="all">Todos los eventos</option>';
  data.event_names.forEach((name) => {
    const opt = document.createElement('option');
    opt.value = name;
    opt.textContent = name;
    if (name === current) opt.selected = true;
    select.appendChild(opt);
  });

  // Table: 1 single row per event (combining paired platforms), sorted by total revenue DESC
  const tbody = document.getElementById('eventsTableBody');
  if (!data.events.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="py-4 text-center text-slate-500">Sin eventos en este período</td></tr>';
  } else {
    const byEvent = {};
    data.events.forEach((ev) => {
      if (!byEvent[ev.event_name]) {
        byEvent[ev.event_name] = {
          name: ev.event_name,
          totalRev: 0,
          totalItems: 0,
          totalSales: 0,
          maxVel: 0,
          platforms: new Set(),
          paired: false,
        };
      }
      const g = byEvent[ev.event_name];
      g.totalRev += ev.revenue;
      g.totalItems += ev.items_count;
      g.totalSales += ev.sales_count;
      g.maxVel = Math.max(g.maxVel, ev.velocity_per_hour || 0);
      g.platforms.add(ev.platform);
      if (ev.paired) g.paired = true;
    });
    const sortedGroups = Object.values(byEvent).sort((a, b) => b.totalRev - a.totalRev);

    tbody.innerHTML = sortedGroups
      .map((g) => {
        const avgTicket = g.totalSales > 0 ? g.totalRev / g.totalSales : 0;
        const badges = Array.from(g.platforms).map((p) => platformBadge(p)).join(' ');
        return `
          <tr class="hover:bg-slate-800/40">
            <td class="py-2.5 pr-3 font-medium text-slate-200">
              ${g.name}
              ${g.paired ? '<span class="ml-1.5 text-[10px] px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-300 border border-amber-500/30">🔗 Hermanado</span>' : ''}
            </td>
            <td class="py-2.5 pr-3 whitespace-nowrap">${badges}</td>
            <td class="py-2.5 pr-3 text-right font-semibold text-slate-200">${g.totalItems} <span class="text-[10px] font-normal text-slate-500">(${g.totalSales} op.)</span></td>
            <td class="py-2.5 pr-3 text-right font-semibold text-white">${fmtMoney(g.totalRev)}</td>
            <td class="py-2.5 pr-3 text-right text-slate-300">${fmtMoney(avgTicket)}</td>
            <td class="py-2.5 text-right text-emerald-400">${g.maxVel}</td>
          </tr>`;
      })
      .join('');
  }

  // Paired events summary cards (events published on both Lumepic & Momentia)
  const pairedMap = {};
  data.events.forEach((ev) => {
    if (!ev.paired) return;
    if (!pairedMap[ev.event_name]) {
      pairedMap[ev.event_name] = {
        name: ev.event_name,
        lumepic_rev: 0,
        lumepic_cnt: 0,
        momentia_rev: 0,
        momentia_cnt: 0,
      };
    }
    if (ev.platform === 'lumepic') {
      pairedMap[ev.event_name].lumepic_rev += ev.revenue;
      pairedMap[ev.event_name].lumepic_cnt += ev.items_count;
    } else if (ev.platform === 'momentia') {
      pairedMap[ev.event_name].momentia_rev += ev.revenue;
      pairedMap[ev.event_name].momentia_cnt += ev.items_count;
    }
  });
  const pairedList = Object.values(pairedMap);
  const pairedSec = document.getElementById('pairedEventsSection');
  const pairedGrid = document.getElementById('pairedEventsGrid');
  if (pairedSec && pairedGrid) {
    if (pairedList.length > 0) {
      pairedSec.classList.remove('hidden');
      pairedGrid.innerHTML = pairedList
        .map((p) => {
          const total = p.lumepic_rev + p.momentia_rev;
          return `
          <div class="p-3.5 rounded-xl bg-slatebg border border-amber-500/30 space-y-1.5">
            <div class="text-xs font-semibold text-slate-200 truncate" title="${p.name}">${p.name}</div>
            <div class="text-lg font-bold text-white">${fmtMoney(total)} <span class="text-xs font-normal text-slate-400">· ${p.lumepic_cnt + p.momentia_cnt} fotos</span></div>
            <div class="flex items-center justify-between text-[11px] text-slate-400">
              <span class="text-indigo-300">Lumepic: ${fmtMoney(p.lumepic_rev)} (${p.lumepic_cnt} f.)</span>
              <span class="text-emerald-300">Momentia: ${fmtMoney(p.momentia_rev)} (${p.momentia_cnt} f.)</span>
            </div>
          </div>`;
        })
        .join('');
    } else {
      pairedSec.classList.add('hidden');
    }
  }

  // Horizontal bar chart comparing events
  const grouped = {};
  data.events.forEach((ev) => {
    if (!grouped[ev.event_name]) grouped[ev.event_name] = { lumepic: 0, momentia: 0, canon: 0 };
    grouped[ev.event_name][ev.platform] = (grouped[ev.event_name][ev.platform] || 0) + ev.revenue;
  });
  const evLabels = Object.keys(grouped).sort((a, b) => {
    const totA = grouped[a].lumepic + grouped[a].momentia + grouped[a].canon;
    const totB = grouped[b].lumepic + grouped[b].momentia + grouped[b].canon;
    return totB - totA;
  });
  const container = document.getElementById('eventsChartContainer');
  if (container) {
    container.style.height = `${Math.max(280, evLabels.length * 32)}px`;
  }
  const ctx = document.getElementById('eventsChart').getContext('2d');
  if (state.eventsChart) state.eventsChart.destroy();

  state.eventsChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: evLabels,
      datasets: [
        {
          label: 'Lumepic',
          data: evLabels.map((l) => grouped[l].lumepic),
          backgroundColor: 'rgba(129, 140, 248, 0.85)',
          borderRadius: 4,
        },
        {
          label: 'Momentia',
          data: evLabels.map((l) => grouped[l].momentia),
          backgroundColor: 'rgba(52, 211, 153, 0.85)',
          borderRadius: 4,
        },
        {
          label: 'Canon Fijo',
          data: evLabels.map((l) => grouped[l].canon),
          backgroundColor: 'rgba(251, 191, 36, 0.85)',
          borderRadius: 4,
        },
      ],
    },
    options: {
      indexAxis: 'y',
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: '#cbd5e1', font: { size: 11 } } },
        tooltip: {
          callbacks: {
            title: (items) => (items.length ? evLabels[items[0].dataIndex] : ''),
            label: (c) => `${c.dataset.label}: ${fmtMoney(c.parsed.x)}`,
          },
        },
      },
      scales: {
        x: {
          stacked: true,
          ticks: { color: '#94a3b8', font: { size: 10 }, callback: (v) => fmtMoney(v) },
          grid: { color: 'rgba(148, 163, 184, 0.08)' },
        },
        y: {
          stacked: true,
          ticks: {
            color: '#cbd5e1',
            autoSkip: false,
            font: { size: 11 },
            callback: function (_, idx) {
              const lbl = evLabels[idx] || '';
              return lbl.length > 26 ? lbl.slice(0, 25) + '…' : lbl;
            },
          },
          grid: { display: false },
        },
      },
    },
  });
}

async function loadSales() {
  const q = new URLSearchParams({
    platform: state.platform,
    event_name: state.event_name,
    limit: 40,
  });
  const rows = await apiFetch(`/api/sales?${q}`);
  document.getElementById('salesCountLabel').textContent = `Mostrando ${rows.length} ventas recientes`;
  const tbody = document.getElementById('salesTableBody');
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="py-4 text-center text-slate-500">No hay ventas registradas</td></tr>';
    return;
  }
  tbody.innerHTML = rows
    .map(
      (s) => `
    <tr class="hover:bg-slate-800/40">
      <td class="py-2 pr-3 text-slate-400 whitespace-nowrap">${fmtDate(s.sold_at)}</td>
      <td class="py-2 pr-3">${platformBadge(s.platform)}</td>
      <td class="py-2 pr-3 text-slate-200">${s.event_name}</td>
      <td class="py-2 pr-3 text-slate-400">${s.buyer_name || '—'}</td>
      <td class="py-2 pr-3 text-right">${s.items_count}</td>
      <td class="py-2 text-right font-semibold text-white">${fmtMoney(s.amount)}</td>
    </tr>`
    )
    .join('');
}

// Goals & Alerts removed from UI

async function loadSyncStatus() {
  const data = await apiFetch('/api/sync/status');
  const cfg = data.config;
  state.currency = cfg.currency || 'ARS';

  document.getElementById('cfgActiveInterval').value = cfg.active_sync_interval_minutes ?? 10;
  document.getElementById('cfgIdleInterval').value = cfg.idle_sync_interval_minutes ?? 120;
  document.getElementById('cfgCurrency').value = cfg.currency;
  document.getElementById('cfgLumeLogin').value = cfg.lumepic_login_url;
  document.getElementById('cfgLumeDash').value = cfg.lumepic_dashboard_url;
  document.getElementById('cfgMomLogin').value = cfg.momentia_login_url;
  document.getElementById('cfgMomDash').value = cfg.momentia_dashboard_url;

  const sched = data.scheduler;
  const pill = document.getElementById('schedulerStatusPill');
  if (sched && pill) {
    const modeLabel = sched.mode === 'active' ? 'Activo (~10m)' : 'Reposo (~2h)';
    const nextTime = sched.next_sync_at ? fmtDate(sched.next_sync_at) : '—';
    pill.textContent = `Auto: ${modeLabel} · Próx: ${nextTime}`;
  }

  const setBadge = (elId, connected) => {
    const el = document.getElementById(elId);
    if (connected) {
      el.textContent = 'Conectado';
      el.className = 'text-xs px-2 py-0.5 rounded-full bg-emerald-500/20 text-emerald-300 border border-emerald-500/30';
    } else {
      el.textContent = 'Desconectado';
      el.className = 'text-xs px-2 py-0.5 rounded-full bg-slate-800 text-slate-400';
    }
  };
  setBadge('statusLumepic', data.sessions.lumepic);
  setBadge('statusMomentia', data.sessions.momentia);
  const btnLume = document.getElementById('btnConnectLumepic');
  const btnMom = document.getElementById('btnConnectMomentia');
  if (btnLume) btnLume.textContent = data.sessions.lumepic ? 'Reconectar' : 'Conectar cuenta';
  if (btnMom) btnMom.textContent = data.sessions.momentia ? 'Reconectar' : 'Conectar cuenta';

  const banner = document.getElementById('connectBanner');
  if (!data.sessions.lumepic && !data.sessions.momentia) {
    banner.classList.remove('hidden');
  } else {
    banner.classList.add('hidden');
  }

  const logsEl = document.getElementById('syncLogsList');
  if (!data.logs.length) {
    logsEl.innerHTML = '<p class="text-slate-500">Sin sincronizaciones ejecutadas aún.</p>';
  } else {
    logsEl.innerHTML = data.logs
      .map(
        (l) => `
      <div class="flex items-center justify-between gap-2 p-2 rounded-lg bg-slatebg border border-borderdark">
        <div>
          <span class="font-semibold uppercase text-[10px] ${l.status === 'success' ? 'text-emerald-400' : 'text-rose-400'}">[${l.platform} · ${l.status}]</span>
          <span class="text-slate-300 ml-1">${l.message}</span>
        </div>
        <span class="text-[10px] text-slate-500 whitespace-nowrap">${fmtDate(l.created_at)}</span>
      </div>`
      )
      .join('');
  }
}

function showModalStatus(message, type = 'info') {
  const el = document.getElementById('modalStatus');
  if (!el) return;
  const styles = {
    info: 'bg-indigo-500/15 border-indigo-500/30 text-indigo-200',
    success: 'bg-emerald-500/15 border-emerald-500/30 text-emerald-200',
    error: 'bg-rose-500/15 border-rose-500/30 text-rose-200',
  };
  el.className = `rounded-xl px-4 py-2.5 text-xs border ${styles[type] || styles.info}`;
  el.textContent = message;
  el.classList.remove('hidden');
}

async function connectPlatform(platform) {
  showModalStatus(
    `Abriendo Brave para iniciar sesión en ${platform.toUpperCase()}... Iniciá sesión ahí y, cuando veas tu tablero, cerrá esa ventana para guardar la sesión.`,
    'info'
  );
  try {
    const res = await apiFetch(`/api/connect/${platform}`, { method: 'POST' });
    showModalStatus(res.message, 'info');
    // Poll session status every 3s for up to 3 minutes
    let attempts = 0;
    const poller = setInterval(async () => {
      attempts += 1;
      try {
        const status = await apiFetch('/api/sync/status');
        if (status.sessions && status.sessions[platform]) {
          clearInterval(poller);
          showModalStatus(`¡${platform.toUpperCase()} conectado correctamente!`, 'success');
          await loadSyncStatus();
        } else if (attempts >= 60) {
          clearInterval(poller);
        }
      } catch (_) {}
    }, 3000);
  } catch (err) {
    showModalStatus(`Error al conectar ${platform}: ${err.message}`, 'error');
  }
}

async function disconnectPlatform(platform) {
  await apiFetch(`/api/disconnect/${platform}`, { method: 'POST' });
  await loadSyncStatus();
}

// Event listeners
document.getElementById('filterPlatform').addEventListener('change', (e) => {
  state.platform = e.target.value;
  loadAll();
});
document.getElementById('filterEvent').addEventListener('change', (e) => {
  state.event_name = e.target.value;
  loadAll();
});
document.getElementById('filterDays').addEventListener('change', (e) => {
  state.days = Number(e.target.value);
  state.month = 'all';
  const mSel = document.getElementById('filterMonth');
  if (mSel) mSel.value = 'all';
  loadAll();
});
document.getElementById('filterMonth')?.addEventListener('change', (e) => {
  state.month = e.target.value;
  loadAll();
});
document.getElementById('filterDateMode')?.addEventListener('change', (e) => {
  state.date_mode = e.target.value;
  loadAll();
});

document.querySelectorAll('.gran-btn').forEach((btn) => {
  btn.addEventListener('click', () => {
    state.granularity = btn.dataset.gran;
    const unitEl = document.getElementById('rollingWindowUnit');
    const winInput = document.getElementById('rollingWindowInput');
    if (unitEl && winInput) {
      if (state.granularity === 'day') {
        unitEl.textContent = 'd';
        if (Number(winInput.value) === 24) {
          winInput.value = 7;
          state.rollingWindow = 7;
        }
      } else {
        unitEl.textContent = 'h';
        if (Number(winInput.value) === 7) {
          winInput.value = 24;
          state.rollingWindow = 24;
        }
      }
    }
    document.querySelectorAll('.gran-btn').forEach((b) => {
      b.className = b === btn
        ? 'gran-btn px-2.5 py-1 bg-indigo-600 text-white font-medium cursor-pointer'
        : 'gran-btn px-2.5 py-1 bg-slatebg text-slate-300 hover:bg-slate-800 cursor-pointer';
    });
    loadTimeseries();
  });
});

document.querySelectorAll('.metric-btn').forEach((btn) => {
  btn.addEventListener('click', () => {
    state.metric = btn.dataset.metric;
    document.querySelectorAll('.metric-btn').forEach((b) => {
      b.className = b === btn
        ? 'metric-btn px-2.5 py-1 bg-indigo-600 text-white font-medium cursor-pointer'
        : 'metric-btn px-2.5 py-1 bg-slatebg text-slate-300 hover:bg-slate-800 cursor-pointer';
    });
    loadTimeseries();
  });
});

document.querySelectorAll('.mode-btn').forEach((btn) => {
  btn.addEventListener('click', () => {
    state.seriesMode = btn.dataset.mode;
    document.querySelectorAll('.mode-btn').forEach((b) => {
      b.className = b === btn
        ? 'mode-btn px-2.5 py-1 bg-indigo-600 text-white font-medium cursor-pointer'
        : 'mode-btn px-2.5 py-1 bg-slatebg text-slate-300 hover:bg-slate-800 cursor-pointer';
    });
    const wrap = document.getElementById('rollingWindowWrap');
    if (wrap) {
      if (state.seriesMode === 'rolling') wrap.classList.remove('hidden');
      else wrap.classList.add('hidden');
    }
    loadTimeseries();
  });
});

document.getElementById('rollingWindowInput')?.addEventListener('input', (e) => {
  state.rollingWindow = Math.max(Number(e.target.value) || 2, 2);
  if (state.seriesMode === 'rolling') loadTimeseries();
});

document.getElementById('btnSyncNow').addEventListener('click', async () => {
  const spinner = document.getElementById('syncSpinner');
  spinner.classList.remove('hidden');
  try {
    await apiFetch('/api/sync/now', { method: 'POST' });
    await loadAll();
  } finally {
    spinner.classList.add('hidden');
  }
});

document.getElementById('btnToggleCanonForm')?.addEventListener('click', () => {
  document.getElementById('canonForm').classList.toggle('hidden');
});

document.getElementById('canonForm')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const dateVal = document.getElementById('canonDate').value;
  await apiFetch('/api/sales/manual', {
    method: 'POST',
    body: JSON.stringify({
      event_name: document.getElementById('canonEventName').value,
      amount: Number(document.getElementById('canonAmount').value),
      sold_at: dateVal ? `${dateVal}T20:00:00` : null,
      notes: 'Canon fijo',
    }),
  });
  e.target.reset();
  e.target.classList.add('hidden');
  await loadAll();
});

// Goals & Alerts listeners removed

// Modal controls
const modal = document.getElementById('settingsModal');
document.getElementById('btnSettings').addEventListener('click', () => modal.classList.remove('hidden'));
document.getElementById('btnBannerConfig').addEventListener('click', () => modal.classList.remove('hidden'));
document.getElementById('btnCloseModal').addEventListener('click', () => modal.classList.add('hidden'));

document.getElementById('configForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  await apiFetch('/api/config', {
    method: 'POST',
    body: JSON.stringify({
      active_sync_interval_minutes: Number(document.getElementById('cfgActiveInterval').value),
      idle_sync_interval_minutes: Number(document.getElementById('cfgIdleInterval').value),
      currency: document.getElementById('cfgCurrency').value,
      lumepic_login_url: document.getElementById('cfgLumeLogin').value,
      lumepic_dashboard_url: document.getElementById('cfgLumeDash').value,
      momentia_login_url: document.getElementById('cfgMomLogin').value,
      momentia_dashboard_url: document.getElementById('cfgMomDash').value,
    }),
  });
  modal.classList.add('hidden');
  await loadAll();
});

document.getElementById('btnSeedDemo').addEventListener('click', async () => {
  await apiFetch('/api/demo/seed', { method: 'POST' });
  await loadAll();
});

const clearDemoHandler = async () => {
  await apiFetch('/api/demo/clear', { method: 'POST' });
  await loadAll();
};
document.getElementById('btnClearDemo').addEventListener('click', clearDemoHandler);
document.getElementById('btnClearDemoQuick').addEventListener('click', clearDemoHandler);

// Initial load & auto-refresh UI every 60s
loadAll();
setInterval(loadAll, 60000);
