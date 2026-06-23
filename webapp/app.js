const API_BASE = window.location.protocol === 'file:' ? 'http://127.0.0.1:8080' : '/api';

const elements = {
  statusBadge: document.getElementById('statusBadge'),
  statusText: document.getElementById('statusText'),
  saleId: document.getElementById('saleId'),
  sold: document.getElementById('soldCount'),
  reserved: document.getElementById('reservedCount'),
  free: document.getElementById('freeCount'),
  tickets: document.getElementById('ticketsCount'),
  buyersCreated: document.getElementById('buyersCreated'),
  loadStatus: document.getElementById('loadStatus'),
  seatTypeGrid: document.getElementById('seatTypeGrid'),
  miniSeatMap: document.getElementById('miniSeatMap'),
  metricsList: document.getElementById('metricsList'),
  eventsList: document.getElementById('eventsList'),
  generateBtn: document.getElementById('generateLoadBtn'),
  buyersInput: document.getElementById('buyersInput'),
  buyerType: document.getElementById('buyerType'),
  refreshBtn: document.getElementById('refreshBtn'),
  restartBtn: document.getElementById('restartBtn'),
  openPwaBtn: document.getElementById('openPwaBtn'),
  lastRefresh: document.getElementById('lastRefresh'),
  toastArea: document.getElementById('toastArea'),
};

let pollingHandle = null;
let inFlight = false;
let lastStats = null;
let lastSoldPercentNotified = 0;
let lastMilestoneShown = new Set();
const progressMilestones = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100];

function escapeText(value) {
  return String(value ?? '');
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) {
    return 'n/a';
  }
  const value = Math.max(0, Number(seconds));
  if (value < 60) {
    return `${value.toFixed(2)} s`;
  }
  const minutes = Math.floor(value / 60);
  const remaining = value - minutes * 60;
  return `${minutes}m ${remaining.toFixed(1)}s`;
}

function formatMetricRows(metrics) {
  const rows = [
    ['Reservas solicitadas', metrics.request_ticket_count],
    ['Reservas aceptadas', metrics.request_ticket_ok],
    ['Compras procesadas', metrics.purchase_count],
    ['Compras exitosas', metrics.purchase_ok],
    ['Compras rechazadas', metrics.purchase_rejected],
    ['Tickets solicitados', metrics.ticket_request_count],
    ['Tickets emitidos', metrics.ticket_request_ok],
    ['Tickets fallidos', metrics.ticket_request_fail],
    ['Liberaciones por expiración', metrics.expired_releases],
    ['Solicitudes antes del inicio', metrics.not_started_count],
  ];

  return rows.map(([label, value]) => `
    <div class="metric-row">
      <span>${escapeText(label)}</span>
      <strong>${escapeText(value ?? 0)}</strong>
    </div>
  `).join('');
}

function renderSeatTypes(seatsByType) {
  const order = [
    ['platino', 'Platino'],
    ['preferente', 'Preferente'],
    ['normal', 'Normal'],
  ];

  elements.seatTypeGrid.innerHTML = order.map(([key, label]) => {
    const data = seatsByType?.[key] || {};
    const free = Number(data.free ?? 0);
    const sold = Number(data.sold ?? 0);
    const reserved = Number(data.reserved ?? 0);
    const total = Number(data.total ?? free + sold + reserved);

    return `
      <article class="seat-type-card">
        <div class="seat-type-head">
          <h3>${escapeText(label)}</h3>
          <span>Total ${escapeText(total)}</span>
        </div>
        <div class="seat-type-split">
          <div class="seat-type-block free">
            <span>Libres</span>
            <strong>${escapeText(free)}</strong>
          </div>
          <div class="seat-type-block sold">
            <span>Comprados</span>
            <strong>${escapeText(sold)}</strong>
          </div>
        </div>
        <p class="seat-type-foot">Reservados en tránsito: ${escapeText(reserved)}</p>
      </article>
    `;
  }).join('');
}

function renderMiniMap(seatStatus) {
  if (!seatStatus || seatStatus.length === 0) {
    elements.miniSeatMap.innerHTML = '<div class="empty-state">Sin mapa disponible.</div>';
    return;
  }

  const fragment = document.createDocumentFragment();
  const sectionBreaks = new Set([2, 6]);
  for (let row = 0; row < seatStatus.length; row += 1) {
    for (let col = 0; col < seatStatus[row].length; col += 1) {
      const cell = document.createElement('div');
      const state = seatStatus[row][col];
      cell.className = `mini-seat-cell ${state === 'SOLD' ? 'sold' : state === 'RESERVED' ? 'reserved' : 'free'}`;
      cell.title = `Fila ${row + 1}, Asiento ${col + 1}: ${state}`;
      fragment.appendChild(cell);
    }

    if (sectionBreaks.has(row)) {
      const spacer = document.createElement('div');
      spacer.className = 'mini-seat-gap';
      fragment.appendChild(spacer);
    }
  }

  elements.miniSeatMap.innerHTML = '';
  elements.miniSeatMap.appendChild(fragment);
}

function renderEvents(events) {
  if (!events || events.length === 0) {
    elements.eventsList.innerHTML = '<li class="empty-state">Sin eventos recientes.</li>';
    return;
  }

  const recent = events.slice(-20).reverse();
  elements.eventsList.innerHTML = recent.map((event) => {
    const parts = [event.type || 'evento'];
    if (event.reason) parts.push(`motivo=${event.reason}`);
    if (event.zone) parts.push(`zona=${event.zone}`);
    if (event.ticket_id) parts.push(`ticket=${event.ticket_id}`);
    if (event.reservation_id) parts.push(`reserva=${event.reservation_id}`);
    if (event.buyer_id) parts.push(`comprador=${event.buyer_id}`);
    if (event.sold_count !== undefined) parts.push(`vendidos=${event.sold_count}`);

    return `
      <li class="event-item">
        <div class="event-ts">${escapeText(event.ts || '')}</div>
        <div class="event-body">${escapeText(parts.join(' · '))}</div>
      </li>
    `;
  }).join('');
}

function renderLoadJobs(loadJobs) {
  if (!loadJobs || loadJobs.length === 0) {
    elements.loadStatus.textContent = 'Sin cargas internas activas.';
    return;
  }

  const activeJob = loadJobs.find((job) => job.status === 'running') || loadJobs[0];
  const elapsed = activeJob.elapsed ? formatDuration(activeJob.elapsed) : 'en curso';
  const result = activeJob.result
    ? `success=${activeJob.result.success} fail=${activeJob.result.fail}`
    : activeJob.error || '';

  elements.loadStatus.textContent = `Carga ${activeJob.job_id} · ${activeJob.status} · ${elapsed}${result ? ` · ${result}` : ''}`;
}

function showToast(title, message, variant = 'info') {
  if (!elements.toastArea) return;

  const toast = document.createElement('div');
  toast.className = `toast-notice ${variant}`;
  toast.innerHTML = `
    <div class="toast-title">${escapeText(title)}</div>
    <div class="toast-body">${escapeText(message)}</div>
  `;

  elements.toastArea.appendChild(toast);
  window.setTimeout(() => {
    toast.classList.add('fade-out');
    window.setTimeout(() => toast.remove(), 280);
  }, 4200);
}

function maybeShowProgressToast(stats) {
  const totalSeats = 1500;
  const soldCount = Number(stats.sold_count || 0);
  const percent = Math.floor((soldCount / totalSeats) * 100);

  for (const milestone of progressMilestones) {
    if (percent >= milestone && !lastMilestoneShown.has(milestone)) {
      lastMilestoneShown.add(milestone);
      showToast('Avance de venta', `${milestone}% de los asientos vendidos (${soldCount}/${totalSeats}).`, milestone === 100 ? 'success' : 'info');
    }
  }

  if (percent > lastSoldPercentNotified) {
    lastSoldPercentNotified = percent;
  }
}

function showOverlay(message) {
  const overlay = document.getElementById('overlay');
  if (!overlay) return;
  overlay.querySelector('.overlay-dialog').textContent = message;
  overlay.classList.remove('hidden');
  overlay.setAttribute('aria-hidden', 'false');
}

function hideOverlay() {
  const overlay = document.getElementById('overlay');
  if (!overlay) return;
  overlay.classList.add('hidden');
  overlay.setAttribute('aria-hidden', 'true');
}

async function restartSale() {
  if (!confirm('¿Seguro que desea reiniciar la venta? Esto limpiará tickets y reservas.')) return;
  elements.restartBtn.disabled = true;
  showOverlay('Reiniciando venta...');
  try {
    const resp = await fetch(`${API_BASE}/restart-sale`, { method: 'POST' });
    if (!resp.ok) throw new Error('no_ok');
    // keep overlay for 3.5s to show the message
    await new Promise((r) => setTimeout(r, 3500));
    await fetchStats();
  } catch (err) {
    showToast('Error', 'No fue posible reiniciar la venta.', 'danger');
  } finally {
    hideOverlay();
    elements.restartBtn.disabled = false;
  }
}

function updateSummary(stats) {
  elements.statusBadge.textContent = stats.sales_closed ? 'Cerrada' : (stats.sales_open ? 'Abierta' : 'Esperando');
  elements.statusBadge.dataset.state = stats.sales_closed ? 'closed' : (stats.sales_open ? 'open' : 'waiting');
  elements.statusText.textContent = stats.sales_closed
    ? `Cierre: ${stats.close_reason || 'n/a'}`
    : (stats.sales_open ? 'La simulación está corriendo.' : 'La venta aún no se inicia.');

  elements.saleId.textContent = stats.sale_id || 'n/a';
  elements.sold.textContent = stats.sold_count ?? 0;
  elements.reserved.textContent = stats.reserved_count ?? 0;
  elements.free.textContent = stats.free_count ?? 0;
  elements.tickets.textContent = stats.metrics?.ticket_request_ok ?? 0;
  elements.buyersCreated.textContent = stats.buyers_created ?? 0;

  elements.metricsList.innerHTML = formatMetricRows(stats.metrics || {});
  renderSeatTypes(stats.seats_by_type || {});
  renderMiniMap(stats.seat_status || []);
  renderEvents(stats.recent_events || []);
  renderLoadJobs(stats.load_jobs || []);

  maybeShowProgressToast(stats);

  const now = new Date();
  elements.lastRefresh.textContent = `Actualizado ${now.toLocaleTimeString()}`;
  lastStats = stats;
}

async function fetchStats() {
  try {
    const response = await fetch(`${API_BASE}/stats`, { cache: 'no-store' });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const stats = await response.json();
    updateSummary(stats);
  } catch (error) {
    elements.statusBadge.textContent = 'Offline';
    elements.statusBadge.dataset.state = 'offline';
    elements.statusText.textContent = `No se pudo conectar con el servidor: ${error.message}`;
    elements.lastRefresh.textContent = `Error ${new Date().toLocaleTimeString()}`;
  }
}

async function generateLoad() {
  if (inFlight) return;
  inFlight = true;
  elements.generateBtn.disabled = true;
  elements.generateBtn.textContent = 'Generando...';

  const buyers = Math.max(1, parseInt(elements.buyersInput.value || '50', 10));
  const clientType = elements.buyerType.value || 'normal';

  try {
    const response = await fetch(`${API_BASE}/generate-load`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ buyers, client_type: clientType }),
    });

    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.message || payload.code || `HTTP ${response.status}`);
    }

    const job = payload.job || {};
    elements.loadStatus.textContent = `Carga iniciada: ${job.job_id || 'n/a'} · ${job.buyers || buyers} compradores`;
    await fetchStats();
  } catch (error) {
    elements.loadStatus.textContent = `No fue posible iniciar la carga: ${error.message}`;
  } finally {
    elements.generateBtn.disabled = false;
    elements.generateBtn.textContent = 'Generar carga';
    inFlight = false;
  }
}

function startPolling() {
  if (pollingHandle) {
    clearInterval(pollingHandle);
  }
  pollingHandle = setInterval(fetchStats, 150);
}

elements.generateBtn.addEventListener('click', generateLoad);
elements.restartBtn.addEventListener('click', restartSale);
if (elements.openPwaBtn) {
  elements.openPwaBtn.addEventListener('click', () => window.open('/pwa/index.html', '_blank', 'noopener'));
}

document.addEventListener('DOMContentLoaded', async () => {
  await fetchStats();
  startPolling();
});
