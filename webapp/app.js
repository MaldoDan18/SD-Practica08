// API runs on port 5001 (socket server continues using 5000 for legacy clients)
const API_BASE = location.hostname === 'localhost' ? 'http://127.0.0.1:5001' : `${location.protocol}//${location.hostname}:5001`;

const seatMap = document.getElementById('seatMap');
const buyerTypeEl = document.getElementById('buyerType');
const refreshBtn = document.getElementById('refreshBtn');
const openCartBtn = document.getElementById('openCartBtn');
const cartCount = document.getElementById('cartCount');
const cartPanel = document.getElementById('cartPanel');
const cartList = document.getElementById('cartList');
const buyAllBtn = document.getElementById('buyAllBtn');
const clearCartBtn = document.getElementById('clearCartBtn');
const finishPurchaseBtn = document.getElementById('finishPurchaseBtn');
const logEl = document.getElementById('log');
const saleOverlay = document.getElementById('saleOverlay');
const saleOverlayTitle = document.getElementById('saleOverlayTitle');
const saleOverlayText = document.getElementById('saleOverlayText');
const saleOverlayTimer = document.getElementById('saleOverlayTimer');
const saleOverlayAction = document.getElementById('saleOverlayAction');
const closeSessionBtn = document.getElementById('closeSessionBtn');
const purchaseList = document.getElementById('purchaseList');
const findSeatBtn = document.getElementById('findSeatBtn');
const findPurchasesBtn = document.getElementById('findPurchasesBtn');
const sideDock = document.getElementById('sideDock');
const mapSectionEl = document.getElementById('mapSection');

const STATE_VERSION = '8';

function resetLegacyLocalStateIfNeeded(){
  const storedVersion = localStorage.getItem('pwa_state_version');
  if(storedVersion === STATE_VERSION) return;
  localStorage.removeItem('pwa_cart');
  localStorage.removeItem('pwa_purchases');
  localStorage.setItem('pwa_state_version', STATE_VERSION);
}

let availability = [];
let saleStatus = { state: 'loading', sales_open: false, sales_closed: false };
let sessionClosed = false;
let clientExecutionFinished = false;
resetLegacyLocalStateIfNeeded();
let cart = JSON.parse(localStorage.getItem('pwa_cart') || '[]');
let purchases = JSON.parse(localStorage.getItem('pwa_purchases') || '[]');
let localBuyerId = localStorage.getItem('pwa_buyer_id') || `PWA-${Math.random().toString(36).slice(2,9)}`;
if (!localStorage.getItem('pwa_buyer_id')) localStorage.setItem('pwa_buyer_id', localBuyerId);
let localClientId = localStorage.getItem('pwa_client_id') || `PWA-CLIENT-${Math.random().toString(36).slice(2,9)}`;
if (!localStorage.getItem('pwa_client_id')) localStorage.setItem('pwa_client_id', localClientId);
let pollingHandle = null;
let highlightMySeats = false;

const SECTION_RULES = [
  { name: 'SECCIÓN PLATINO', rowStart: 0, rowEnd: 2, className: 'section-platino' },
  { name: 'SECCIÓN PREFERENTE', rowStart: 3, rowEnd: 6, className: 'section-preferente' },
  { name: 'SECCIÓN NORMAL', rowStart: 7, rowEnd: 29, className: 'section-normal' },
];

function log(msg){
  const p = document.createElement('div');
  p.textContent = `[${new Date().toLocaleTimeString()}] ${msg}`;
  logEl.prepend(p);
}

function loadCartState(){
  cart = Array.isArray(cart) ? cart : [];
  purchases = Array.isArray(purchases) ? purchases : [];
  cart = cart.filter(entry => {
    if (!entry || !entry.seat) {
      return false;
    }
    entry.selectedForPurchase = entry.selectedForPurchase !== false;
    if (entry.status === 'sold') {
      return false;
    }
    return true;
  });
  saveLocalState();
}

// Ensure the UI starts clean on every PWA execution to avoid stale tickets or
// purchases from previous runs during local demos or integration tests.
function clearPurchasesOnStartup(){
  cart = [];
  purchases = [];
  saveLocalState();
  log('Estado inicial limpiado para esta ejecución');
}

function saveLocalState(){
  localStorage.setItem('pwa_cart', JSON.stringify(cart));
  localStorage.setItem('pwa_purchases', JSON.stringify(purchases));
}

function seatSectionName(row){
  return getSectionForRow(row).name.replace('SECCIÓN ', '');
}

function formatSeatLabel(entry){
  return `${seatSectionName(entry.seat.row)} - Asiento ${entry.seat.row}-${entry.seat.col}`;
}

function isPurchasedSeat(row, col){
  return purchases.some(entry => entry.seat && entry.seat.row === row && entry.seat.col === col && entry.status === 'sold');
}

function cartSelectedCount(){
  return cart.filter(entry => entry.selectedForPurchase !== false).length;
}

function renderCartUI(){
  cartCount.textContent = cart.length;
  cartList.innerHTML = '';

  cart.forEach((entry, index) => {
    const item = document.createElement('li');
    item.className = 'cart-entry';
    if (entry.selectedForPurchase !== false) {
      item.classList.add('selected');
    }
    if(entry.status === 'expired'){
      item.classList.add('expired');
    }

    const controls = document.createElement('div');
    controls.className = 'cart-entry-controls';

    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.checked = entry.selectedForPurchase !== false;
    checkbox.addEventListener('change', () => {
      cart[index].selectedForPurchase = checkbox.checked;
      saveLocalState();
      renderCartUI();
    });
    if(entry.status === 'expired') checkbox.disabled = true;

    const body = document.createElement('div');
    body.className = 'cart-entry-main';

    const title = document.createElement('div');
    title.className = 'cart-entry-title';
    title.textContent = formatSeatLabel(entry);

    const meta = document.createElement('div');
    meta.className = 'cart-entry-meta';
    meta.textContent = `Reserva ${entry.reservation_id.slice(0, 8)} · Zona ${entry.zone}`;

    body.appendChild(title);
    body.appendChild(meta);

    const releaseBtn = document.createElement('button');
    releaseBtn.type = 'button';
    releaseBtn.className = 'icon-button';
    releaseBtn.textContent = 'Liberar';
    releaseBtn.addEventListener('click', () => releaseReservation(index));
    if(entry.status === 'expired'){
      releaseBtn.disabled = true;
      releaseBtn.style.opacity = '0.6';
    }

    controls.appendChild(checkbox);
    controls.appendChild(releaseBtn);

    item.appendChild(controls);
    item.appendChild(body);
    cartList.appendChild(item);
  });

  buyAllBtn.textContent = `Comprar seleccionados (${cartSelectedCount()})`;
  buyAllBtn.disabled = cartSelectedCount() === 0;
}

function renderPurchasesUI(){
  purchaseList.innerHTML = '';

  if(purchases.length === 0){
    const empty = document.createElement('li');
    empty.className = 'empty-state';
    empty.textContent = 'Aún no hay boletos comprados en esta fecha';
    purchaseList.appendChild(empty);
    return;
  }

  purchases.forEach(entry => {
    const item = document.createElement('li');
    item.className = 'purchase-entry';

    const body = document.createElement('div');
    body.className = 'purchase-entry-main';

    const title = document.createElement('div');
    title.className = 'purchase-entry-title';
    title.textContent = formatSeatLabel(entry);

    const meta = document.createElement('div');
    meta.className = 'purchase-entry-meta';
    meta.textContent = `Ticket ${entry.ticket_id || 'n/a'} · ${entry.zone || 'sin zona'}`;

    body.appendChild(title);
    body.appendChild(meta);
    item.appendChild(body);
    purchaseList.appendChild(item);
  });
}

function refreshPanels(){
  renderCartUI();
  renderPurchasesUI();
  // ensure dock aligns with map and panels have correct width
  try{ adjustSideDock(); }catch(e){}
}

function stopPolling(){
  if(pollingHandle){
    clearInterval(pollingHandle);
    pollingHandle = null;
  }
}

function setControlsEnabled(enabled){
  refreshBtn.disabled = !enabled;
  openCartBtn.disabled = !enabled;
  buyAllBtn.disabled = !enabled || cartSelectedCount() === 0;
  clearCartBtn.disabled = !enabled;
  buyerTypeEl.disabled = !enabled;
  findSeatBtn.disabled = !enabled;
  if(findPurchasesBtn) findPurchasesBtn.disabled = !enabled;
  if(finishPurchaseBtn) finishPurchaseBtn.disabled = !enabled || clientExecutionFinished;
}

function showSaleClosedOverlay(reason){
  saleOverlay.classList.remove('hidden');
  saleOverlayTitle.textContent = '✓ La venta ha concluido';

  if(reason === 'all_sold'){
    saleOverlayText.textContent = 'Todos los asientos se vendieron exitosamente.';
  } else if(reason === 'all_clients_done'){
    saleOverlayText.textContent = 'La venta terminó cuando los clientes completaron su operación.';
  } else if(reason === 'test_finished'){
    saleOverlayText.textContent = 'La simulación de pruebas finalizó correctamente.';
  } else {
    saleOverlayText.textContent = 'La simulación ha finalizado.';
  }

  saleOverlayTimer.textContent = '';
  if(saleOverlayAction) saleOverlayAction.classList.remove('hidden');
  setControlsEnabled(false);
  stopPolling();
}

async function closeSession(){
  if(sessionClosed) return;
  sessionClosed = true;
  stopPolling();
  clientExecutionFinished = true;
  if(finishPurchaseBtn) finishPurchaseBtn.disabled = true;

  try{
    await fetch(API_BASE + '/api/client_disconnect', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ client_id: localClientId }),
    });
  }catch(err){
    log('No se pudo notificar la desconexión: ' + err.message);
  }

  localStorage.removeItem('pwa_cart');
  localStorage.removeItem('pwa_purchases');
  localStorage.removeItem('pwa_state_version');
  localStorage.removeItem('pwa_buyer_id');
  localStorage.removeItem('pwa_client_id');
  log('Sesión cerrada por el usuario');

  if('serviceWorker' in navigator){
    navigator.serviceWorker.getRegistration().then(reg => {
      if(reg) return reg.unregister();
      return null;
    }).catch(() => {});
  }

  setTimeout(() => {
    try{
      window.close();
    }catch(e){}
    window.location.replace('about:blank');
  }, 150);
}

async function finishPurchaseExecution(){
  if(sessionClosed) return;
  try{
    const res = await fetch(API_BASE + '/api/client_done', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ client_id: localClientId }),
    });
    const data = await res.json();
    if(data.status === 'ok' || data.type === 'DONE_ACK'){
      log(`Cliente marcado como terminado (${data.done_clients}/${data.expected_clients})`);
      clientExecutionFinished = true;
      if(data.sale_status && data.sale_status.state === 'closed'){
        showSaleClosedOverlay(data.sale_status.close_reason || 'all_clients_done');
      }
      if(finishPurchaseBtn) finishPurchaseBtn.disabled = true;
      return;
    }
    log('No se pudo terminar la compra: ' + JSON.stringify(data));
  }catch(err){
    log('Error notificando fin de ejecución: ' + err.message);
  }
}

function reconcileCartWithAvailability(){
  if(!Array.isArray(cart) || !Array.isArray(availability)) return;
  for(const entry of cart){
    if(!entry || !entry.seat) continue;
    const r = entry.seat.row; const c = entry.seat.col;
    const state = (availability[r] && availability[r][c]) || 'FREE';
    if(state === 'RESERVED'){
      continue;
    }

    // If seat is no longer RESERVED, mark as expired and schedule removal in 5s
    if(entry.status !== 'expired'){
      entry.status = 'expired';
      // prevent scheduling multiple timeouts
      if(!entry._expirationScheduled){
        entry._expirationScheduled = true;
        (function(resId){
          setTimeout(async ()=>{
            const idx = cart.findIndex(e=>e && e.reservation_id === resId);
            if(idx >= 0){
              const removed = cart.splice(idx,1)[0];
              saveLocalState();
              refreshPanels();
              log(`Reserva ${resId.slice(0,8)} eliminada tras expiración`);
              try{ await fetchAvailability(); }catch(e){}
            }
          }, 5000);
        })(entry.reservation_id);
      }
      log(`Reserva ${entry.reservation_id.slice(0,8)} marcada como expirada`);
      saveLocalState();
      refreshPanels();
    }
  }
}

function adjustSideDock(){
  if(!sideDock || !mapSectionEl) return;
  if(window.innerWidth <= 1100){
    sideDock.style.position = '';
    sideDock.style.top = '';
    sideDock.style.width = '';
    sideDock.style.right = '';
    sideDock.style.maxHeight = '';
    if(mapSectionEl) mapSectionEl.style.marginRight = '';
    return;
  }

  // let CSS grid control the width; only clear old inline layout state
  sideDock.style.position = '';
  sideDock.style.top = '';
  sideDock.style.width = '';
  sideDock.style.right = '';
  sideDock.style.maxHeight = '';
  if(mapSectionEl) mapSectionEl.style.marginRight = '';
}

function updateCartUI(){
  refreshPanels();
}

function formatCountdown(seconds){
  const value = Math.max(0, Math.ceil(Number(seconds) || 0));
  const minutes = Math.floor(value / 60);
  const remainingSeconds = value % 60;
  return minutes > 0 ? `${minutes}:${String(remainingSeconds).padStart(2, '0')}` : `${remainingSeconds}s`;
}

function updateSaleOverlay(){
  const state = saleStatus.state || 'loading';
  if(state === 'open'){
    saleOverlay.classList.add('hidden');
    if(saleOverlayAction) saleOverlayAction.classList.add('hidden');
    saleOverlayTitle.textContent = 'Venta abierta';
    saleOverlayText.textContent = 'Ya puedes seleccionar asientos.';
    saleOverlayTimer.textContent = '';
    setControlsEnabled(true);
    return;
  }

  saleOverlay.classList.remove('hidden');
  if(saleOverlayAction) saleOverlayAction.classList.add('hidden');

  if(state === 'countdown'){
    saleOverlayTitle.textContent = 'La venta está por iniciar';
    saleOverlayText.textContent = 'El servidor ya autorizó el inicio y está en cuenta regresiva.';
    saleOverlayTimer.textContent = formatCountdown(saleStatus.countdown_remaining);
    return;
  }

  if(state === 'closed'){
    showSaleClosedOverlay(saleStatus.close_reason || 'unknown');
    return;
  }

  if(state === 'waiting'){
    saleOverlayTitle.textContent = 'La venta no ha iniciado';
    saleOverlayText.textContent = 'Esperando a que el servidor autorice el inicio.';
    saleOverlayTimer.textContent = '';
    setControlsEnabled(false);
    return;
  }

  saleOverlayTitle.textContent = 'Cargando estado...';
  saleOverlayText.textContent = 'Esperando respuesta del servidor.';
  saleOverlayTimer.textContent = '';
  setControlsEnabled(false);
}

function getSectionForRow(row){
  return SECTION_RULES.find(section => row >= section.rowStart && row <= section.rowEnd) || SECTION_RULES[SECTION_RULES.length - 1];
}

function getLocalReservationForSeat(row, col){
  return cart.find(entry => entry.seat && entry.seat.row === row && entry.seat.col === col);
}

async function fetchAvailability(){
  try{
    const res = await fetch(API_BASE + '/api/availability');
    if(!res.ok) throw new Error('No availability');
    const data = await res.json();
    saleStatus = data.sale_status || saleStatus;
    availability = data.seat_status || [];
    renderSeats();
    reconcileCartWithAvailability();
    updateSaleOverlay();
    if(saleStatus.sales_closed){
      showSaleClosedOverlay(saleStatus.close_reason || 'unknown');
    }
  }catch(err){
    log('No se pudo obtener disponibilidad: '+err.message);
    saleStatus = { state: 'offline', sales_open: false, sales_closed: false };
    updateSaleOverlay();
  }
}

async function registerPWA(){
  try{
    const payload = { client_id: localClientId, client_type: buyerTypeEl.value || 'normal', buyers: 1 };
    const res = await fetch(API_BASE + '/api/register_client', { method: 'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload) });
    if(!res.ok) {
      const txt = await res.text();
      log('Registro PWA falló: '+txt);
      return;
    }
    const data = await res.json();
    log('PWA registrada como cliente: ' + data.client_id + ` (${data.connected_clients}/${data.expected_clients})`);

    // signal ready so server can count this client
    const readyRes = await fetch(API_BASE + '/api/ready', { method: 'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ client_id: localClientId }) });
    if(readyRes.ok){
      const rd = await readyRes.json();
      log('PWA READY: ' + JSON.stringify(rd));
    }
  }catch(err){
    log('Error registrando PWA: '+err.message);
  }
}

function renderSeats(){
  seatMap.innerHTML = '';
  const cols = 50; // COLUMNAS from server
  seatMap.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;

  let currentSectionName = null;

  for(let r=0;r<availability.length;r++){
    const section = getSectionForRow(r);

    if(section.name !== currentSectionName){
      const label = document.createElement('div');
      label.classList.add('section-label');
      label.classList.add(section.className);
      label.textContent = section.name;
      seatMap.appendChild(label);
      currentSectionName = section.name;
    }
    
    for(let c=0;c<Math.min(availability[r].length, cols);c++){
      const cell = document.createElement('div');
      cell.classList.add('seat');
      cell.classList.add(section.className);
      const state = availability[r][c];
      const localReservation = getLocalReservationForSeat(r, c);

      if(state === 'FREE'){
        cell.classList.add('free');
      } else if(state === 'RESERVED'){
        if(localReservation && localReservation.status !== 'sold'){
          cell.classList.add('reserved-mine');
        } else {
          cell.classList.add('reserved-other');
        }
      } else {
        cell.classList.add('sold');
      }

      if (highlightMySeats && isPurchasedSeat(r, c)) {
        cell.classList.add('my-purchase');
      }

      cell.textContent = `${r}-${c}`;
      cell.dataset.row = r;
      cell.dataset.col = c;
      if(state !== 'SOLD'){
        cell.addEventListener('click', onSeatClick);
      }
      seatMap.appendChild(cell);
    }
  }
}

async function onSeatClick(evt){
  const row = parseInt(evt.currentTarget.dataset.row, 10);
  const col = parseInt(evt.currentTarget.dataset.col, 10);
  const buyerType = buyerTypeEl.value;

  // Send request_ticket to server with specific seat coordinates
  const payload = {
    type: 'REQUEST_TICKET',
    buyer_id: localBuyerId,
    buyer_type: buyerType,
    request_id: cryptoRandomId(),
    row: row,
    col: col,
  };

  try{
    const res = await fetch(API_BASE + '/api/request_ticket', {
      method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload)
    });
    const data = await res.json();
    if(data.status === 'ok' && data.reservation_id){
      const entry = { reservation_id: data.reservation_id, seat: data.seat, zone: data.zone, status: 'reserved', ttl_seconds: data.ttl_seconds, selectedForPurchase: true };
      cart.push(entry);
      saveLocalState();
      log(`Reserva OK asiento ${entry.seat.row}-${entry.seat.col} id=${entry.reservation_id}`);
      refreshPanels();
      renderSeats();
    } else {
      log('Reserva rechazada: ' + (data.message || JSON.stringify(data)));
    }
  }catch(err){
    log('Error en reserva: '+err.message);
  }
}

function cryptoRandomId(){
  return Math.random().toString(36).slice(2)+Date.now().toString(36);
}

async function buyAll(){
  const selectedEntries = cart.filter(entry => entry.selectedForPurchase !== false && entry.status !== 'sold');
  if(selectedEntries.length === 0) return log('No hay selecciones activas para comprar');

  for(let i=0;i<selectedEntries.length;i++){
    const entry = selectedEntries[i];
    try{
      const payload = { type:'PURCHASE', buyer_id: localBuyerId, reservation_id: entry.reservation_id, request_id: cryptoRandomId() };
      const res = await fetch(API_BASE + '/api/purchase', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload) });
      const data = await res.json();
      if(data.status === 'ok'){
        const purchaseRecord = {
          ...entry,
          status: 'sold',
          ticket_id: data.ticket_id || data.ticket?.ticket_id,
          purchased_at: new Date().toISOString(),
        };
        purchases.unshift(purchaseRecord);
        cart = cart.filter(item => item.reservation_id !== entry.reservation_id);
        saveLocalState();
        log(`Compra OK asiento ${entry.seat.row}-${entry.seat.col} ticket=${purchaseRecord.ticket_id||'n/a'}`);
      } else {
        entry.status = 'failed';
        log(`Compra fallida para ${entry.seat.row}-${entry.seat.col}: ${JSON.stringify(data)}`);
      }
    }catch(err){
      entry.status = 'failed';
      log('Error en compra: '+err.message);
    }
    refreshPanels();
  }
  // refresh availability after purchases
  await fetchAvailability();
}

async function releaseReservation(index){
  const entry = cart[index];
  if(!entry) return;

  try{
    const payload = { type: 'RELEASE_TICKET', buyer_id: localBuyerId, reservation_id: entry.reservation_id, request_id: cryptoRandomId() };
    const res = await fetch(API_BASE + '/api/release_ticket', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
    const data = await res.json();
    if(data.status === 'ok'){
      cart.splice(index, 1);
      saveLocalState();
      log(`Reserva liberada asiento ${entry.seat.row}-${entry.seat.col}`);
      refreshPanels();
      await fetchAvailability();
      return;
    }
    log(`No se pudo liberar ${entry.seat.row}-${entry.seat.col}: ${JSON.stringify(data)}`);
  }catch(err){
    log('Error liberando reserva: '+err.message);
  }
}

async function releaseAllReservations(){
  if(cart.length === 0) return;
  const snapshot = [...cart];
  for(let i = 0; i < snapshot.length; i++){
    const entry = snapshot[i];
    const currentIndex = cart.findIndex(item => item.reservation_id === entry.reservation_id);
    if(currentIndex >= 0){
      await releaseReservation(currentIndex);
    }
  }
}

if(closeSessionBtn){
  closeSessionBtn.addEventListener('click', closeSession);
}

if(finishPurchaseBtn){
  finishPurchaseBtn.addEventListener('click', finishPurchaseExecution);
}

refreshBtn.addEventListener('click', fetchAvailability);
openCartBtn.addEventListener('click', ()=>{ cartPanel.classList.toggle('hidden'); refreshPanels(); });
buyAllBtn.addEventListener('click', buyAll);
clearCartBtn.addEventListener('click', async ()=>{ await releaseAllReservations(); refreshPanels(); renderSeats(); log('Carrito vaciado'); });
findSeatBtn.addEventListener('click', ()=>{
  highlightMySeats = !highlightMySeats;
  findSeatBtn.textContent = highlightMySeats ? 'Ocultar mis asientos' : 'Encontrar mi asiento';
  renderSeats();
});

if(findPurchasesBtn){
  findPurchasesBtn.addEventListener('click', ()=>{
    highlightMySeats = !highlightMySeats;
    findPurchasesBtn.textContent = highlightMySeats ? 'Ocultar mis asientos' : 'Encontrar mi asiento';
    // sync label in header if present
    if(findSeatBtn) findSeatBtn.textContent = highlightMySeats ? 'Ocultar mis asientos' : 'Encontrar mi asiento';
    renderSeats();
  });
}

// polling
function startPolling(){
  if(pollingHandle) clearInterval(pollingHandle);
  pollingHandle = setInterval(fetchAvailability, 1000);
}

// service worker registration
if('serviceWorker' in navigator){
  navigator.serviceWorker.register('sw.js?v=8').then(()=>console.log('sw registered')).catch(()=>console.log('sw failed'));
}

// init
loadCartState();
// Force purchases list to be empty on each start so "Mis compras" always
// appears empty for a fresh run / integration test. This avoids stale visual
// state across runs while preserving cart behavior during the session.
clearPurchasesOnStartup();
refreshPanels();
updateSaleOverlay();
registerPWA();  // Register as client BEFORE fetching availability
fetchAvailability();
startPolling();

// adjust dock on load/resize/scroll
window.addEventListener('load', adjustSideDock);
window.addEventListener('resize', adjustSideDock);
window.addEventListener('scroll', () => { if(sideDock) adjustSideDock(); });
