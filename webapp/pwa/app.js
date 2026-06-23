// Use the local server for localhost development, otherwise go through Nginx proxy on the same origin.
const API_BASE = (location.protocol === 'file:' || location.hostname === 'localhost' || location.hostname === '127.0.0.1')
  ? 'http://127.0.0.1:8080'
  : '';

const seatMap = document.getElementById('seatMap');
const buyerTypeEl = document.getElementById('buyerType');
const refreshBtn = document.getElementById('refreshBtn');
const openCartBtn = document.getElementById('openCartBtn');
const cartCount = document.getElementById('cartCount');
const cartPanel = document.getElementById('cartPanel');
const cartList = document.getElementById('cartList');
const buyAllBtn = document.getElementById('buyAllBtn');
const clearCartBtn = document.getElementById('clearCartBtn');
const purchaseList = document.getElementById('purchaseList');
const findSeatBtn = document.getElementById('findSeatBtn');
const findPurchasesBtn = document.getElementById('findPurchasesBtn');
const logEl = document.getElementById('log');
const soldOutBanner = document.getElementById('soldOutBanner');

const STATE_VERSION = '9';

let availability = [];
let saleStatus = { state: 'loading', sales_open: false, sales_closed: false };
let cart = [];
let purchases = [];
let lastSaleId = null;
let localBuyerId = localStorage.getItem('pwa_buyer_id') || `PWA-${Math.random().toString(36).slice(2,9)}`;
if (!localStorage.getItem('pwa_buyer_id')) localStorage.setItem('pwa_buyer_id', localBuyerId);
let localClientId = localStorage.getItem('pwa_client_id') || `PWA-CLIENT-${Math.random().toString(36).slice(2,9)}`;
if (!localStorage.getItem('pwa_client_id')) localStorage.setItem('pwa_client_id', localClientId);
let pollingHandle = null;
let highlightMySeats = false;
let availabilityInFlight = false;

function resetLegacyLocalStateIfNeeded() {
  const storedVersion = localStorage.getItem('pwa_state_version');
  if (storedVersion === STATE_VERSION) return;
  localStorage.removeItem('pwa_cart');
  localStorage.removeItem('pwa_purchases');
  localStorage.setItem('pwa_state_version', STATE_VERSION);
}

resetLegacyLocalStateIfNeeded();
cart = JSON.parse(localStorage.getItem('pwa_cart') || '[]');
purchases = JSON.parse(localStorage.getItem('pwa_purchases') || '[]');

function saveLocalState() {
  localStorage.setItem('pwa_cart', JSON.stringify(cart));
  localStorage.setItem('pwa_purchases', JSON.stringify(purchases));
}

function persistCart() {
  saveLocalState();
}

function seatLabel(entry) {
  return `${entry.seat.row + 1}-${entry.seat.col + 1}`;
}

function cartBadge(entry) {
  if (entry.status === 'sold') return 'Comprada';
  if (entry.status === 'released') return 'Liberada';
  if (entry.status === 'expired') return 'Expirada';
  return 'Reservada';
}

function seatSectionName(row) {
  if (row <= 2) return 'PLATINO';
  if (row <= 6) return 'PREFERENTE';
  return 'NORMAL';
}

function formatSeatLabel(entry) {
  return `${seatSectionName(entry.seat.row)} - Asiento ${entry.seat.row}-${entry.seat.col}`;
}

function isPurchasedSeat(row, col) {
  return purchases.some((entry) => entry.seat && entry.seat.row === row && entry.seat.col === col && entry.status === 'sold');
}

function cartSelectedCount() {
  return cart.filter((entry) => entry.selectedForPurchase !== false && entry.status !== 'sold').length;
}

function refreshPanels() {
  updateCartUI();
  renderPurchasesUI();
}

function removeCartEntry(reservationId, reason = 'Liberada') {
  const index = cart.findIndex((item) => item.reservation_id === reservationId);
  if (index === -1) return false;

  const [entry] = cart.splice(index, 1);
  persistCart();
  updateCartUI();
  renderSeats();
  log(`${reason} ${seatLabel(entry)} (${entry.zone})`);
  return true;
}

function loadCartState() {
  cart = Array.isArray(cart) ? cart : [];
  purchases = Array.isArray(purchases) ? purchases : [];
  cart = cart.filter((entry) => entry && entry.seat && entry.status !== 'sold');
  purchases = purchases.filter((entry) => entry && entry.seat && entry.status === 'sold');
  saveLocalState();
}

function clearPurchasesOnStartup() {
  cart = [];
  purchases = [];
  saveLocalState();
  log('Estado inicial limpio para esta ejecución');
}

function clearPurchasesForNewSale(newSaleId) {
  if (!newSaleId || newSaleId === lastSaleId) return;
  purchases = [];
  lastSaleId = newSaleId;
  saveLocalState();
  renderPurchasesUI();
  renderSeats();
  log('Mis compras se limpiaron por reinicio de venta');
}

async function releaseReservation(entry) {
  const payload = {
    type: 'RELEASE_RESERVATION',
    buyer_id: localBuyerId,
    reservation_id: entry.reservation_id,
    request_id: cryptoRandomId(),
  };

  try {
    const res = await fetch(API_BASE + '/api/release_reservation', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await readJsonResponse(res);
    if (!res.ok || data.status !== 'ok') {
      throw new Error(data.message || data.code || `HTTP ${res.status}`);
    }
    removeCartEntry(entry.reservation_id, 'Reserva liberada');
    await fetchAvailability();
  } catch (err) {
    if (err.message.includes('invalid_or_expired_reservation')) {
      removeCartEntry(entry.reservation_id, 'Reserva expirada');
      await fetchAvailability();
      return;
    }
    log('No se pudo liberar la reserva: ' + err.message);
  }
}

function toggleCartSelection(reservationId, selected) {
  const entry = cart.find((item) => item.reservation_id === reservationId);
  if (!entry) return;
  entry.selectedForPurchase = Boolean(selected);
  persistCart();
  updateCartUI();
}

function syncCartWithAvailability(seatStatus) {
  if (!Array.isArray(seatStatus) || seatStatus.length === 0) return;

  const expired = [];
  cart = cart.filter((entry) => {
    if (!entry || !entry.seat || entry.status === 'sold') return true;
    const row = seatStatus[entry.seat.row];
    const serverState = row ? row[entry.seat.col] : 'FREE';
    if (serverState === 'RESERVED') return true;
    expired.push(entry);
    return false;
  });

  if (expired.length > 0) {
    persistCart();
    updateCartUI();
    renderSeats();
    expired.forEach((entry) => log(`Reserva liberada por el servidor: ${seatLabel(entry)} (${entry.zone})`));
  }
}

function renderPurchasesUI() {
  purchaseList.innerHTML = '';

  if (purchases.length === 0) {
    const empty = document.createElement('li');
    empty.className = 'empty-state';
    empty.textContent = 'Aún no hay boletos comprados en esta fecha';
    purchaseList.appendChild(empty);
    return;
  }

  purchases.forEach((entry) => {
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

function buildCartItem(entry) {
  const li = document.createElement('li');
  li.className = `cart-item ${entry.status || 'reserved'}`;

  const header = document.createElement('div');
  header.className = 'cart-item-header';

  const left = document.createElement('div');
  left.className = 'cart-item-main';

  const title = document.createElement('div');
  title.className = 'cart-item-title';
  title.textContent = formatSeatLabel(entry);

  const meta = document.createElement('div');
  meta.className = 'cart-item-meta';
  const parts = [`Reserva ${entry.reservation_id || 'n/a'}`];
  if (entry.ttl_seconds) parts.push(`TTL ${entry.ttl_seconds}s`);
  meta.textContent = parts.join(' · ');

  left.appendChild(title);
  left.appendChild(meta);

  const badge = document.createElement('span');
  badge.className = `cart-badge ${entry.status || 'reserved'}`;
  badge.textContent = cartBadge(entry);

  const checkboxWrap = document.createElement('label');
  checkboxWrap.className = 'cart-select';
  const checkbox = document.createElement('input');
  checkbox.type = 'checkbox';
  checkbox.checked = entry.selectedForPurchase !== false && entry.status !== 'sold';
  checkbox.disabled = entry.status === 'sold';
  checkbox.addEventListener('change', (event) => toggleCartSelection(entry.reservation_id, event.target.checked));
  const checkboxText = document.createElement('span');
  checkboxText.textContent = 'Seleccionar';
  checkboxWrap.appendChild(checkbox);
  checkboxWrap.appendChild(checkboxText);

  const actions = document.createElement('div');
  actions.className = 'cart-item-actions';

  const buyButton = document.createElement('button');
  buyButton.type = 'button';
  buyButton.className = 'secondary';
  buyButton.textContent = 'Comprar';
  buyButton.disabled = entry.status === 'sold';
  buyButton.addEventListener('click', () => buySingle(entry.reservation_id));

  const releaseButton = document.createElement('button');
  releaseButton.type = 'button';
  releaseButton.className = 'danger';
  releaseButton.textContent = 'Liberar';
  releaseButton.disabled = entry.status === 'sold';
  releaseButton.addEventListener('click', () => releaseReservation(entry));

  actions.appendChild(buyButton);
  actions.appendChild(releaseButton);

  header.appendChild(left);
  header.appendChild(badge);

  li.appendChild(header);
  li.appendChild(checkboxWrap);
  li.appendChild(actions);
  return li;
}

function log(msg){
  const p = document.createElement('div');
  p.textContent = `[${new Date().toLocaleTimeString()}] ${msg}`;
  logEl.prepend(p);
}

function updateCartUI(){
  cartCount.textContent = cart.length;
  cartList.innerHTML = '';
  if (cart.length === 0) {
    const empty = document.createElement('li');
    empty.className = 'cart-empty';
    empty.textContent = 'No hay reservas en el carrito.';
    cartList.appendChild(empty);
    return;
  }

  cart.forEach((entry) => {
    cartList.appendChild(buildCartItem(entry));
  });

  buyAllBtn.textContent = `Comprar seleccionados (${cartSelectedCount()})`;
  buyAllBtn.disabled = cartSelectedCount() === 0;
}

function formatCountdown(seconds){
  const value = Math.max(0, Math.ceil(Number(seconds) || 0));
  const minutes = Math.floor(value / 60);
  const remainingSeconds = value % 60;
  return minutes > 0 ? `${minutes}:${String(remainingSeconds).padStart(2, '0')}` : `${remainingSeconds}s`;
}

function updateSoldOutBanner(){
  if (!soldOutBanner) return;

  const isSoldOut = saleStatus.sales_closed || saleStatus.state === 'closed';
  const isAllSold = saleStatus.close_reason === 'all_sold';

  if (isSoldOut && isAllSold) {
    soldOutBanner.classList.remove('hidden');
  } else {
    soldOutBanner.classList.add('hidden');
  }
}

function adjustSideDock() {
  const sideDock = document.getElementById('sideDock');
  if (!sideDock) return;
  if (window.innerWidth <= 1100) {
    sideDock.style.position = '';
    sideDock.style.top = '';
    sideDock.style.width = '';
    sideDock.style.maxHeight = '';
    return;
  }
  sideDock.style.position = '';
  sideDock.style.top = '';
  sideDock.style.width = '';
  sideDock.style.maxHeight = '';
}

async function readJsonResponse(res) {
  const raw = await res.text();
  const contentType = (res.headers.get('content-type') || '').toLowerCase();

  if (contentType.includes('application/json')) {
    return JSON.parse(raw || '{}');
  }

  try {
    return JSON.parse(raw || '{}');
  } catch {
    throw new Error((raw || '').trim().slice(0, 180) || `HTTP ${res.status}`);
  }
}

async function fetchAvailability(){
  if (availabilityInFlight) return;
  availabilityInFlight = true;
  try{
    const res = await fetch(API_BASE + '/api/availability');
    if(!res.ok) throw new Error('No availability');
    const data = await readJsonResponse(res);
    saleStatus = data.sale_status || saleStatus;
    clearPurchasesForNewSale(data.sale_id || saleStatus.sale_id);
    availability = data.seat_status || [];
    syncCartWithAvailability(availability);
    renderSeats();
    updateSoldOutBanner();
  }catch(err){
    log('No se pudo obtener disponibilidad: '+err.message);
    saleStatus = { state: 'offline', sales_open: false, sales_closed: false };
    updateSoldOutBanner();
  } finally {
    availabilityInFlight = false;
  }
}

async function registerPWA(){
  try{
    const payload = { client_id: localClientId, client_type: buyerTypeEl.value || 'normal', buyers: 1, buyer_ids: [localBuyerId] };
    const res = await fetch(API_BASE + '/api/register_client', { method: 'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload) });
    if(!res.ok) {
      const txt = await res.text();
      log('Registro PWA falló: '+txt);
      return;
    }
    const data = await readJsonResponse(res);
    log('PWA registrada como cliente: ' + data.client_id + ` (${data.connected_clients}/${data.expected_clients})`);

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
  const cols = 50;
  seatMap.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;
  const sectionNames = ['SECCIÓN PLATINO', 'SECCIÓN PREFERENTE', 'SECCIÓN NORMAL'];
  const sectionStartRows = [0, 3, 7];
  let currentSectionIdx = 0;

  for(let r=0;r<availability.length;r++){
    if(currentSectionIdx < sectionNames.length && r === sectionStartRows[currentSectionIdx]){
      const label = document.createElement('div');
      label.classList.add('section-label');
      label.textContent = sectionNames[currentSectionIdx];
      seatMap.appendChild(label);
      if(currentSectionIdx < sectionNames.length - 1){
        currentSectionIdx++;
      }
    }
    for(let c=0;c<Math.min(availability[r].length, cols);c++){
      const cell = document.createElement('div');
      cell.classList.add('seat');
      const sectionClass = r <= 2 ? 'section-platino' : r <= 6 ? 'section-preferente' : 'section-normal';
      cell.classList.add(sectionClass);
      const state = availability[r][c];
      const localReservation = cart.find(x => x.seat && x.seat.row===r && x.seat.col===c);

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
    const data = await readJsonResponse(res);
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

async function purchaseReservation(entry) {
  if (!entry || entry.status === 'sold') return false;

  try{
    const payload = { type:'PURCHASE', buyer_id: localBuyerId, reservation_id: entry.reservation_id, request_id: cryptoRandomId() };
    const res = await fetch(API_BASE + '/api/purchase', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload) });
    const data = await readJsonResponse(res);
    if(data.status === 'ok'){
      const purchaseRecord = {
        ...entry,
        status: 'sold',
        ticket_id: data.ticket_id || data.ticket?.ticket_id,
        purchased_at: new Date().toISOString(),
      };
      purchases.unshift(purchaseRecord);
      cart = cart.filter((item) => item.reservation_id !== entry.reservation_id);
      entry.status = 'sold';
      entry.ticket_id = purchaseRecord.ticket_id;
      log(`Compra OK asiento ${entry.seat.row}-${entry.seat.col} ticket=${entry.ticket_id||'n/a'}`);
      return true;
    }

    entry.status = 'failed';
    log(`Compra fallida para ${entry.seat.row}-${entry.seat.col}: ${JSON.stringify(data)}`);
    return false;
  }catch(err){
    entry.status = 'failed';
    log('Error en compra: '+err.message);
    return false;
  } finally {
    saveLocalState();
    refreshPanels();
  }
}

async function buyAll(){
  const selectedItems = cart.filter((entry) => entry.status !== 'sold' && entry.selectedForPurchase !== false);
  if(selectedItems.length === 0) return log('No hay reservas seleccionadas');
  for(let i=0;i<selectedItems.length;i++){
    const entry = selectedItems[i];
    await purchaseReservation(entry);
  }
  await fetchAvailability();
}

async function buySingle(reservationId) {
  const entry = cart.find((item) => item.reservation_id === reservationId);
  if (!entry || entry.status === 'sold') return;
  entry.selectedForPurchase = true;
  await purchaseReservation(entry);
  await fetchAvailability();
}

async function releaseAllReservations() {
  if (cart.length === 0) return;
  const snapshot = [...cart];
  for (let index = 0; index < snapshot.length; index += 1) {
    const entry = snapshot[index];
    const currentIndex = cart.findIndex((item) => item.reservation_id === entry.reservation_id);
    if (currentIndex >= 0) {
      await releaseReservation(cart[currentIndex]);
    }
  }
}

function toggleSeatHighlight() {
  highlightMySeats = !highlightMySeats;
  if (findSeatBtn) findSeatBtn.textContent = highlightMySeats ? 'Ocultar mis asientos' : 'Encontrar mi asiento';
  if (findPurchasesBtn) findPurchasesBtn.textContent = highlightMySeats ? 'Ocultar mis asientos' : 'Encontrar mi asiento';
  renderSeats();
}

refreshBtn.addEventListener('click', fetchAvailability);
openCartBtn.addEventListener('click', ()=>{ cartPanel.classList.toggle('hidden'); refreshPanels(); });
buyAllBtn.addEventListener('click', buyAll);
clearCartBtn.addEventListener('click', async ()=>{ await releaseAllReservations(); refreshPanels(); renderSeats(); log('Carrito vaciado'); });
if(findSeatBtn) findSeatBtn.addEventListener('click', toggleSeatHighlight);
if(findPurchasesBtn) findPurchasesBtn.addEventListener('click', toggleSeatHighlight);

function startPolling(){
  if(pollingHandle) clearInterval(pollingHandle);
  pollingHandle = setInterval(fetchAvailability, 1000);
}

if('serviceWorker' in navigator){
  navigator.serviceWorker.register('sw.js').then(()=>console.log('sw registered')).catch(()=>console.log('sw failed'));
}

loadCartState();
clearPurchasesOnStartup();
refreshPanels();
updateSoldOutBanner();
registerPWA();
fetchAvailability();
startPolling();

window.addEventListener('load', adjustSideDock);
window.addEventListener('resize', adjustSideDock);
