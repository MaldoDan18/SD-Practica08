# Practica 6 - Servicios

import argparse
import copy
import json
import random
import socket
import socketserver
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from mimetypes import guess_type
from pathlib import Path
from urllib.parse import urlparse

try:
    import tkinter as tk
except Exception:  # pragma: no cover - the web deployment runs headless
    tk = None

FILAS = 30
COLUMNAS = 50
TOTAL_ASIENTOS = FILAS * COLUMNAS
RESERVA_TTL_SEGUNDOS = 1.5
SECTION_GAP_ROWS = 2
SECTION_LABEL_ROWS = 1

ZONA_PLATINO = "PLATINO"
ZONA_PREFERENTE = "PREFERENTE"
ZONA_NORMAL = "NORMAL"

ZONE_ORDER = (ZONA_PLATINO, ZONA_PREFERENTE, ZONA_NORMAL)
ZONE_TO_BUYER_TYPE = {
    ZONA_PLATINO: "platino",
    ZONA_PREFERENTE: "preferente",
    ZONA_NORMAL: "normal",
}

TIPO_PLATINO = "platino"
TIPO_PREFERENTE = "preferente"
TIPO_NORMAL = "normal"

ALLOWED_ZONES_BY_TYPE = {
    TIPO_PLATINO: [ZONA_PLATINO, ZONA_PREFERENTE, ZONA_NORMAL],
    TIPO_PREFERENTE: [ZONA_PREFERENTE, ZONA_NORMAL],
    TIPO_NORMAL: [ZONA_NORMAL],
}

TICKET_SERVICE_TIMEOUT = 4.0
WEBAPP_DIR = Path(__file__).resolve().parent / "webapp"


def send_json_request(host, port, payload, timeout=TICKET_SERVICE_TIMEOUT):
    data = (json.dumps(payload) + "\n").encode("utf-8")
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(data)

        response_bytes = b""
        while not response_bytes.endswith(b"\n"):
            chunk = sock.recv(4096)
            if not chunk:
                break
            response_bytes += chunk

    if not response_bytes:
        raise ConnectionError("Respuesta vacía del servicio externo")

    return json.loads(response_bytes.decode("utf-8").strip())


def build_zone_seats():
    zones = {
        ZONA_PLATINO: set(),
        ZONA_PREFERENTE: set(),
        ZONA_NORMAL: set(),
    }

    for row in range(FILAS):
        for col in range(COLUMNAS):
            if row <= 2:
                zones[ZONA_PLATINO].add((row, col))
            elif row <= 6:
                zones[ZONA_PREFERENTE].add((row, col))
            else:
                zones[ZONA_NORMAL].add((row, col))

    return zones


class TicketingServiceClient:
    def __init__(self, host, port, timeout=TICKET_SERVICE_TIMEOUT):
        self.host = host
        self.port = port
        self.timeout = timeout

    def _send(self, payload, expected_type=None):
        try:
            response = send_json_request(self.host, self.port, payload, timeout=self.timeout)
        except (OSError, ConnectionError, TimeoutError, json.JSONDecodeError) as exc:
            return {
                "status": "error",
                "code": "ticketing_service_unavailable",
                "message": str(exc),
            }

        if expected_type and (response.get("type") or "").upper() != expected_type.upper():
            return {
                "status": "error",
                "code": "unexpected_ticketing_response",
                "message": f"Respuesta inesperada del ticketing service: {response}",
            }
        return response

    def create_ticket(self, payload):
        request_payload = dict(payload)
        request_payload.setdefault("type", "CREATE_TICKET")
        request_payload.setdefault("request_id", str(uuid.uuid4()))
        return self._send(request_payload, expected_type="CREATE_TICKET_RESPONSE")

    def set_context(self, sale_id, server_host, server_port):
        return self._send(
            {
                "type": "SET_CONTEXT",
                "sale_id": sale_id,
                "server_host": server_host,
                "server_port": server_port,
            },
            expected_type="SET_CONTEXT_RESPONSE",
        )

    def open_sales(self):
        return self._send({"type": "OPEN_SALES"}, expected_type="OPEN_SALES_RESPONSE")

    def close_sales(self, reason):
        return self._send({"type": "CLOSE_SALES", "reason": reason}, expected_type="CLOSE_SALES_RESPONSE")

    def reset_sale(self):
        return self._send({"type": "RESET_SALE"}, expected_type="RESET_SALE_RESPONSE")

    def request_ticket(self, buyer_id, buyer_type, request_id, row=None, col=None):
        payload = {
            "type": "REQUEST_TICKET",
            "buyer_id": buyer_id,
            "buyer_type": buyer_type,
            "request_id": request_id,
        }
        if row is not None and col is not None:
            payload["row"] = row
            payload["col"] = col
        return self._send(payload, expected_type="REQUEST_TICKET_RESPONSE")

    def purchase(self, buyer_id, reservation_id, request_id):
        return self._send(
            {
                "type": "PURCHASE",
                "buyer_id": buyer_id,
                "reservation_id": reservation_id,
                "request_id": request_id,
            },
            expected_type="PURCHASE_RESPONSE",
        )

    def release_reservation(self, buyer_id, reservation_id, request_id):
        return self._send(
            {
                "type": "RELEASE_TICKET",
                "buyer_id": buyer_id,
                "reservation_id": reservation_id,
                "request_id": request_id,
            },
            expected_type="RELEASE_TICKET_RESPONSE",
        )

    def availability(self):
        return self._send({"type": "AVAILABILITY"}, expected_type="AVAILABILITY_RESPONSE")

    def health(self):
        return self._send({"type": "HEALTH"}, expected_type="HEALTH_RESPONSE")


class TicketState:
    def __init__(self):
        self.meta_lock = threading.Lock()
        self.events_lock = threading.Lock()
        self.terminal_lock = threading.Lock()
        self.ticketing_client = None
        self.sale_id = None
        self.server_host = None
        self.server_port = None

        self.zone_locks = {
            ZONA_PLATINO: threading.Lock(),
            ZONA_PREFERENTE: threading.Lock(),
            ZONA_NORMAL: threading.Lock(),
        }

        self.zone_free_seats = build_zone_seats()
        self.seat_status = [["FREE" for _ in range(COLUMNAS)] for _ in range(FILAS)]

        self.reservations_by_zone = {
            ZONA_PLATINO: {},
            ZONA_PREFERENTE: {},
            ZONA_NORMAL: {},
        }
        self.reservation_to_zone = {}
        self._availability_snapshot_cache = None
        self._availability_snapshot_cache_at = 0.0
        self._availability_snapshot_ttl = 0.25

        self.unique_buyers = set()
        self.registered_buyers_by_type = {
            TIPO_PLATINO: 0,
            TIPO_PREFERENTE: 0,
            TIPO_NORMAL: 0,
        }
        self.purchased_by_type = {
            TIPO_PLATINO: 0,
            TIPO_PREFERENTE: 0,
            TIPO_NORMAL: 0,
        }
        self.sold_count = 0
        self.sales_started_at = None
        self.sales_finished_at = None
        self.sales_open_event = threading.Event()
        self.sales_closed_event = threading.Event()
        self.sold_out_event = threading.Event()
        self.summary_printed = False
        self.hitos_reportados = set()
        self.close_reason = None
        self.recent_events = []

        self.metrics = {
            "request_ticket_count": 0,
            "purchase_count": 0,
            "request_ticket_time_total": 0.0,
            "purchase_time_total": 0.0,
            "ticket_request_count": 0,
            "ticket_request_time_total": 0.0,
            "ticket_request_ok": 0,
            "ticket_request_fail": 0,
            "request_ticket_ok": 0,
            "request_ticket_fail": 0,
            "purchase_ok": 0,
            "purchase_rejected": 0,
            "expired_releases": 0,
            "not_started_count": 0,
        }

    def set_ticketing_client(self, ticketing_client):
        self.ticketing_client = ticketing_client

    def set_sale_context(self, sale_id, server_host=None, server_port=None):
        self.sale_id = sale_id
        self.server_host = server_host
        self.server_port = server_port

    def _invalidate_availability_cache(self):
        with self.meta_lock:
            self._availability_snapshot_cache = None
            self._availability_snapshot_cache_at = 0.0

    def _build_local_snapshot(self):
        seat_status_copy = [row[:] for row in self.seat_status]
        reserved_count = sum(len(self.reservations_by_zone[z]) for z in self.reservations_by_zone)
        seats_by_type = {}
        with self.events_lock:
            recent_events = list(self.recent_events)
        seat_counts_by_zone = {
            ZONA_PLATINO: {"free": 0, "reserved": 0, "sold": 0},
            ZONA_PREFERENTE: {"free": 0, "reserved": 0, "sold": 0},
            ZONA_NORMAL: {"free": 0, "reserved": 0, "sold": 0},
        }

        for row_index, row in enumerate(seat_status_copy):
            zone = self._zone_for_row(row_index)
            zone_counts = seat_counts_by_zone[zone]
            for state in row:
                if state == "SOLD":
                    zone_counts["sold"] += 1
                elif state == "RESERVED":
                    zone_counts["reserved"] += 1
                else:
                    zone_counts["free"] += 1

        for zone in ZONE_ORDER:
            buyer_type = ZONE_TO_BUYER_TYPE[zone]
            zone_counts = seat_counts_by_zone[zone]
            seats_by_type[buyer_type] = {
                "free": zone_counts["free"],
                "sold": zone_counts["sold"],
                "reserved": zone_counts["reserved"],
                "total": zone_counts["free"] + zone_counts["sold"] + zone_counts["reserved"],
            }

        return {
            "sold_count": self.sold_count,
            "reserved_count": reserved_count,
            "free_count": TOTAL_ASIENTOS - self.sold_count - reserved_count,
            "buyers_created": len(self.unique_buyers),
            "seat_status": seat_status_copy,
            "sales_open": self.sales_open_event.is_set(),
            "sales_closed": self.sales_closed_event.is_set(),
            "close_reason": self.close_reason,
            "sale_id": self.sale_id,
            "metrics": dict(self.metrics),
            "recent_events": recent_events,
            "seats_by_type": seats_by_type,
        }

    def _record_event(self, event_type, **details):
        event = {
            "type": event_type,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        event.update(details)
        with self.events_lock:
            self.recent_events.append(event)
            if len(self.recent_events) > 60:
                self.recent_events = self.recent_events[-60:]

    def open_sales(self):
        if self.ticketing_client is not None:
            response = self.ticketing_client.open_sales()
            if (response.get("status") or "").lower() == "ok":
                self.sales_open_event.set()
                self._invalidate_availability_cache()
            return response
        with self.meta_lock:
            if self.sales_open_event.is_set():
                return
            self.sales_started_at = time.perf_counter()
            self.sales_open_event.set()

        self._record_event("sale_opened", sale_id=self.sale_id)

        with self.terminal_lock:
            print("Actualizaciones de venta:")

    def sales_open(self):
        return self.sales_open_event.is_set()

    def sales_closed(self):
        return self.sales_closed_event.is_set()

    def close_sales(self, reason):
        if self.ticketing_client is not None:
            response = self.ticketing_client.close_sales(reason)
            self.sales_closed_event.set()
            self.sales_open_event.clear()
            self.close_reason = reason
            return response
        lock_order = [self.zone_locks[ZONA_PLATINO], self.zone_locks[ZONA_PREFERENTE], self.zone_locks[ZONA_NORMAL]]
        for lock in lock_order:
            lock.acquire()

        try:
            with self.meta_lock:
                if self.sales_closed_event.is_set():
                    return

            released = 0
            for zone in (ZONA_PLATINO, ZONA_PREFERENTE, ZONA_NORMAL):
                zone_reservations = self.reservations_by_zone[zone]
                for reservation_id, info in list(zone_reservations.items()):
                    row, col = info["seat"]
                    self.seat_status[row][col] = "FREE"
                    self.zone_free_seats[zone].add((row, col))
                    zone_reservations.pop(reservation_id, None)
                    released += 1

            with self.meta_lock:
                self.reservation_to_zone.clear()
                if self.sales_finished_at is None:
                    self.sales_finished_at = time.perf_counter()
                self.close_reason = reason
                self.sales_closed_event.set()
                self.sold_out_event.set()

            self._record_event(
                "sale_closed",
                sale_id=self.sale_id,
                reason=reason,
                released_reservations=released,
            )

            with self.terminal_lock:
                if released > 0:
                    print(f"Actualización: cierre de venta liberó {released} reservas activas.")
        finally:
            for lock in reversed(lock_order):
                lock.release()

    def release_reservations_for_buyer_prefix(self, buyer_prefix, reason):
        lock_order = [self.zone_locks[ZONA_PLATINO], self.zone_locks[ZONA_PREFERENTE], self.zone_locks[ZONA_NORMAL]]
        for lock in lock_order:
            lock.acquire()

        released = 0
        try:
            for zone in (ZONA_PLATINO, ZONA_PREFERENTE, ZONA_NORMAL):
                zone_reservations = self.reservations_by_zone[zone]
                for reservation_id, info in list(zone_reservations.items()):
                    if not str(info.get("buyer_id", "")).startswith(buyer_prefix):
                        continue

                    row, col = info["seat"]
                    self.seat_status[row][col] = "FREE"
                    self.zone_free_seats[zone].add((row, col))
                    zone_reservations.pop(reservation_id, None)
                    released += 1

                    with self.meta_lock:
                        self.reservation_to_zone.pop(reservation_id, None)

                    self._record_event(
                        "reservation_released",
                        buyer_id=info.get("buyer_id"),
                        zone=zone,
                        seat={"row": row, "col": col},
                        reservation_id=reservation_id,
                        reason=reason,
                    )

            if released > 0:
                self._invalidate_availability_cache()

            return released
        finally:
            for lock in reversed(lock_order):
                lock.release()

    def release_reservation(self, reservation_id, reason):
        if self.ticketing_client is not None:
            with self.meta_lock:
                zone = self.reservation_to_zone.get(reservation_id)
            if zone is None:
                return False

            info = self.reservations_by_zone.get(zone, {}).get(reservation_id)

            buyer_id = info.get("buyer_id") if info else None
            response = self.ticketing_client.release_reservation(buyer_id, reservation_id, str(uuid.uuid4()))
            if (response.get("status") or "").lower() == "ok":
                with self.meta_lock:
                    self.reservation_to_zone.pop(reservation_id, None)
                    self.metrics["expired_releases"] += 1
                if info:
                    row, col = info["seat"]
                    self.seat_status[row][col] = "FREE"
                    self.zone_free_seats[zone].add((row, col))
                self._record_event(
                    "reservation_released",
                    buyer_id=buyer_id,
                    zone=zone,
                    seat={"row": info["seat"][0], "col": info["seat"][1]} if info else None,
                    reservation_id=reservation_id,
                    reason=reason,
                )
                self._invalidate_availability_cache()
                return True
            return False
        with self.meta_lock:
            zone = self.reservation_to_zone.get(reservation_id)

        if zone is None:
            return False

        zone_lock = self.zone_locks[zone]
        if not zone_lock.acquire(timeout=0.05):
            return False

        try:
            info = self.reservations_by_zone[zone].pop(reservation_id, None)
            if info is None:
                return False

            row, col = info["seat"]
            self.seat_status[row][col] = "FREE"
            self.zone_free_seats[zone].add((row, col))

            with self.meta_lock:
                self.reservation_to_zone.pop(reservation_id, None)
                self.metrics["expired_releases"] += 1

            self._record_event(
                "reservation_released",
                buyer_id=info.get("buyer_id"),
                zone=zone,
                seat={"row": row, "col": col},
                reservation_id=reservation_id,
                reason=reason,
            )
            self._invalidate_availability_cache()
            return True
        finally:
            zone_lock.release()

    def reset_sale(self):
        if self.ticketing_client is not None:
            response = self.ticketing_client.reset_sale()
            with self.meta_lock:
                self.zone_free_seats = build_zone_seats()
                self.seat_status = [["FREE" for _ in range(COLUMNAS)] for _ in range(FILAS)]
                for zone in self.reservations_by_zone:
                    self.reservations_by_zone[zone].clear()
                self.reservation_to_zone.clear()
                self.unique_buyers.clear()
                for key in self.registered_buyers_by_type:
                    self.registered_buyers_by_type[key] = 0
                for key in self.purchased_by_type:
                    self.purchased_by_type[key] = 0
                self.sold_count = 0
                for key in list(self.metrics.keys()):
                    self.metrics[key] = 0 if isinstance(self.metrics[key], int) else 0.0
                self.sales_started_at = None
                self.sales_finished_at = None
                self.sales_open_event.clear()
                self.sales_closed_event.clear()
                self.sold_out_event.clear()
                self.summary_printed = False
                self.hitos_reportados.clear()
                self.close_reason = None
                self.recent_events = []
                self.sale_id = response.get("sale_id") or f"venta-{uuid.uuid4().hex[:8]}"
            self._invalidate_availability_cache()
            return
        # Fully reset sale state: seats, reservations, metrics, events.
        lock_order = [self.zone_locks[ZONA_PLATINO], self.zone_locks[ZONA_PREFERENTE], self.zone_locks[ZONA_NORMAL]]
        for lock in lock_order:
            lock.acquire()

        try:
            with self.meta_lock:
                # Close current sale to make running workers stop promptly
                self.sales_closed_event.set()
                self.sold_out_event.set()

                # Reset seat pools and statuses
                self.zone_free_seats = build_zone_seats()
                self.seat_status = [["FREE" for _ in range(COLUMNAS)] for _ in range(FILAS)]

                # Clear reservations
                for zone in (ZONA_PLATINO, ZONA_PREFERENTE, ZONA_NORMAL):
                    self.reservations_by_zone[zone].clear()
                self.reservation_to_zone.clear()

                # Reset buyers/metrics
                self.unique_buyers.clear()
                for k in self.registered_buyers_by_type:
                    self.registered_buyers_by_type[k] = 0
                for k in self.purchased_by_type:
                    self.purchased_by_type[k] = 0
                self.sold_count = 0

                for key in list(self.metrics.keys()):
                    self.metrics[key] = 0 if isinstance(self.metrics[key], int) else 0.0

                # Reset timing/state
                self.sales_started_at = None
                self.sales_finished_at = None
                self.sales_open_event.clear()
                self.sales_closed_event.clear()
                self.sold_out_event.clear()
                self.summary_printed = False
                self.hitos_reportados.clear()
                self.close_reason = None

                # Clear recent events and set a new sale id
                self.recent_events = []
                self.sale_id = f"venta-{uuid.uuid4().hex[:8]}"

                # Record restart event
                self._record_event("sale_restarted", sale_id=self.sale_id)
                self._invalidate_availability_cache()
        finally:
            for lock in reversed(lock_order):
                lock.release()

    def can_buyer_type_keep_trying(self, buyer_type):
        normalized = normalize_buyer_type(buyer_type)
        zones = ALLOWED_ZONES_BY_TYPE.get(normalized, ALLOWED_ZONES_BY_TYPE[TIPO_NORMAL])
        lock_order = [self.zone_locks[zone] for zone in zones]

        for lock in lock_order:
            lock.acquire()

        try:
            for zone in zones:
                available_or_reserved = len(self.zone_free_seats[zone]) + len(self.reservations_by_zone[zone])
                if available_or_reserved > 0:
                    return True
            return False
        finally:
            for lock in reversed(lock_order):
                lock.release()

    def register_buyer(self, buyer_id):
        if not buyer_id:
            return
        with self.meta_lock:
            self.unique_buyers.add(str(buyer_id))

    def register_client_buyers(self, client_type, buyers_count):
        normalized = (client_type or "").lower()
        if normalized not in self.registered_buyers_by_type:
            normalized = TIPO_NORMAL
        with self.meta_lock:
            self.registered_buyers_by_type[normalized] += max(0, int(buyers_count))

    def _remaining_buyers_locked(self, buyer_type):
        return max(
            0,
            self.registered_buyers_by_type[buyer_type] - self.purchased_by_type[buyer_type],
        )

    def _eligible_remaining_for_zone_locked(self, zone):
        remaining_platino = self._remaining_buyers_locked(TIPO_PLATINO)
        remaining_preferente = self._remaining_buyers_locked(TIPO_PREFERENTE)
        remaining_normal = self._remaining_buyers_locked(TIPO_NORMAL)

        if zone == ZONA_PLATINO:
            return remaining_platino
        if zone == ZONA_PREFERENTE:
            return remaining_platino + remaining_preferente
        return remaining_platino + remaining_preferente + remaining_normal

    def _is_sale_still_possible_locked(self):
        for zone in (ZONA_PLATINO, ZONA_PREFERENTE, ZONA_NORMAL):
            available_or_reserved = len(self.zone_free_seats[zone]) + len(self.reservations_by_zone[zone])
            if available_or_reserved <= 0:
                continue
            if self._eligible_remaining_for_zone_locked(zone) > 0:
                return True
        return False

    def _close_if_unsellable_locked(self):
        if self.sales_closed_event.is_set() or not self.sales_open_event.is_set():
            return False
        if self.sold_count >= TOTAL_ASIENTOS:
            return False
        if self._is_sale_still_possible_locked():
            return False

        if self.sales_finished_at is None:
            self.sales_finished_at = time.perf_counter()
        self.close_reason = "unsellable_remaining"
        self.sales_closed_event.set()
        self.sold_out_event.set()
        return True

    def _cleanup_expired_zone_locked(self, zone):
        now = time.monotonic()
        zone_reservations = self.reservations_by_zone[zone]
        expired_ids = [
            reservation_id
            for reservation_id, info in zone_reservations.items()
            if info["expires_at"] <= now
        ]

        for reservation_id in expired_ids:
            info = zone_reservations.pop(reservation_id)
            row, col = info["seat"]
            self.seat_status[row][col] = "FREE"
            self.zone_free_seats[zone].add((row, col))
            with self.meta_lock:
                self.reservation_to_zone.pop(reservation_id, None)
                self.metrics["expired_releases"] += 1

    def _mark_sold_out_locked(self):
        if self.sales_finished_at is None:
            self.sales_finished_at = time.perf_counter()
        self.close_reason = "sold_out"
        self.sales_closed_event.set()
        self.sold_out_event.set()

    def _report_progress_milestones_locked(self):
        for porcentaje, umbral in (
            (10, int(TOTAL_ASIENTOS * 0.10)),
            (20, int(TOTAL_ASIENTOS * 0.20)),
            (30, int(TOTAL_ASIENTOS * 0.30)),
            (40, int(TOTAL_ASIENTOS * 0.40)),
            (50, int(TOTAL_ASIENTOS * 0.50)),
            (60, int(TOTAL_ASIENTOS * 0.60)),
            (70, int(TOTAL_ASIENTOS * 0.70)),
            (80, int(TOTAL_ASIENTOS * 0.80)),
            (90, int(TOTAL_ASIENTOS * 0.90)),
            (100, TOTAL_ASIENTOS),
        ):
            if self.sold_count >= umbral and porcentaje not in self.hitos_reportados:
                self.hitos_reportados.add(porcentaje)
                with self.terminal_lock:
                    print(f"Actualización: {porcentaje}% de boletos vendidos ({self.sold_count}/{TOTAL_ASIENTOS}).")

    def request_ticket(self, buyer_id, buyer_type, request_id, row=None, col=None):
        if self.ticketing_client is not None:
            started = time.perf_counter()
            with self.meta_lock:
                self.metrics["request_ticket_count"] += 1

            self.register_buyer(buyer_id)
            response = self.ticketing_client.request_ticket(buyer_id, buyer_type, request_id, row=row, col=col)
            elapsed = time.perf_counter() - started
            status = (response.get("status") or "").lower()

            with self.meta_lock:
                self.metrics["request_ticket_time_total"] += elapsed
                if status == "ok":
                    self.metrics["request_ticket_ok"] += 1
                else:
                    self.metrics["request_ticket_fail"] += 1
                    if status == "not_started":
                        self.metrics["not_started_count"] += 1

            if status == "ok":
                seat = response.get("seat") or {}
                reservation_id = response.get("reservation_id")
                zone = response.get("zone")
                if reservation_id and zone and "row" in seat and "col" in seat:
                    row = int(seat["row"])
                    col = int(seat["col"])
                    self.seat_status[row][col] = "RESERVED"
                    self.reservations_by_zone[zone][reservation_id] = {
                        "reservation_id": reservation_id,
                        "buyer_id": str(buyer_id),
                        "buyer_type": (buyer_type or TIPO_NORMAL).lower(),
                        "seat": (row, col),
                        "zone": zone,
                        "expires_at": time.monotonic() + RESERVA_TTL_SEGUNDOS,
                        "request_id": request_id,
                    }
                    with self.meta_lock:
                        self.reservation_to_zone[reservation_id] = zone
                    self._record_event(
                        "reservation_created",
                        buyer_id=str(buyer_id),
                        buyer_type=(buyer_type or TIPO_NORMAL).lower(),
                        zone=zone,
                        seat={"row": row, "col": col},
                        reservation_id=reservation_id,
                    )
                    self._invalidate_availability_cache()
            return response
        started = time.perf_counter()

        if self.sales_closed_event.is_set():
            return {"status": "closed", "message": "La venta fue cerrada."}

        if not self.sales_open_event.is_set():
            with self.meta_lock:
                self.metrics["not_started_count"] += 1
                self.metrics["request_ticket_time_total"] += time.perf_counter() - started
            return {"status": "not_started", "message": "La venta aún no inicia."}

        self.register_buyer(buyer_id)
        zones = ALLOWED_ZONES_BY_TYPE.get((buyer_type or "").lower(), ALLOWED_ZONES_BY_TYPE[TIPO_NORMAL])

        with self.meta_lock:
            self.metrics["request_ticket_count"] += 1

        for zone in zones:
            zone_lock = self.zone_locks[zone]
            acquired = zone_lock.acquire(timeout=0.02)
            if not acquired:
                continue

            try:
                self._cleanup_expired_zone_locked(zone)
                if not self.zone_free_seats[zone]:
                    continue

                seat = random.choice(tuple(self.zone_free_seats[zone]))
                row, col = seat
                reservation_id = str(uuid.uuid4())

                self.zone_free_seats[zone].remove(seat)
                self.seat_status[row][col] = "RESERVED"
                self.reservations_by_zone[zone][reservation_id] = {
                    "buyer_id": str(buyer_id),
                    "buyer_type": (buyer_type or TIPO_NORMAL).lower(),
                    "seat": seat,
                    "zone": zone,
                    "expires_at": time.monotonic() + RESERVA_TTL_SEGUNDOS,
                    "request_id": request_id,
                }

                with self.meta_lock:
                    self.reservation_to_zone[reservation_id] = zone
                    self.metrics["request_ticket_ok"] += 1
                    self.metrics["request_ticket_time_total"] += time.perf_counter() - started

                self._record_event(
                    "reservation_created",
                    buyer_id=str(buyer_id),
                    buyer_type=(buyer_type or TIPO_NORMAL).lower(),
                    zone=zone,
                    seat={"row": row, "col": col},
                    reservation_id=reservation_id,
                )

                return {
                    "status": "ok",
                    "reservation_id": reservation_id,
                    "zone": zone,
                    "seat": {"row": row, "col": col},
                    "ttl_seconds": RESERVA_TTL_SEGUNDOS,
                }
            finally:
                zone_lock.release()

        # No cerrar la venta por demanda insuficiente; solo termina cuando se agotan los boletos.

        with self.meta_lock:
            self.metrics["request_ticket_time_total"] += time.perf_counter() - started
            sold_count = self.sold_count

        if sold_count >= TOTAL_ASIENTOS:
            with self.meta_lock:
                self._mark_sold_out_locked()
            self._record_event("sold_out", sold_count=sold_count)
            return {"status": "sold_out", "message": "No hay asientos disponibles."}

        return {
            "status": "error",
            "code": "no_zone_available",
            "message": "No hay asientos disponibles para el tipo de comprador en este momento.",
        }

    def purchase(self, buyer_id, reservation_id, request_id):
        if self.ticketing_client is not None:
            started = time.perf_counter()
            with self.meta_lock:
                self.metrics["purchase_count"] += 1
                zone = self.reservation_to_zone.get(reservation_id)
            response = self.ticketing_client.purchase(buyer_id, reservation_id, request_id)
            elapsed = time.perf_counter() - started
            status = (response.get("status") or "").lower()
            with self.meta_lock:
                self.metrics["purchase_time_total"] += elapsed
                if status == "ok":
                    self.metrics["purchase_ok"] += 1
                    self.metrics["ticket_request_count"] += 1
                    self.metrics["ticket_request_ok"] += 1
                else:
                    self.metrics["purchase_rejected"] += 1
                    if status == "not_started":
                        self.metrics["not_started_count"] += 1
            if status == "ok":
                seat = response.get("seat") or {}
                row = int(seat.get("row", 0))
                col = int(seat.get("col", 0))
                if zone and 0 <= row < FILAS and 0 <= col < COLUMNAS:
                    self.reservations_by_zone[zone].pop(reservation_id, None)
                    with self.meta_lock:
                        self.reservation_to_zone.pop(reservation_id, None)
                    self.seat_status[row][col] = "SOLD"
                    self.sold_count += 1
                    buyer_type = normalize_buyer_type(response.get("ticket", {}).get("buyer_type") or TIPO_NORMAL)
                    self.purchased_by_type[buyer_type] += 1
                    self._record_event(
                        "ticket_sold",
                        buyer_id=str(buyer_id),
                        reservation_id=reservation_id,
                        zone=zone,
                        seat={"row": row, "col": col},
                        ticket_id=response.get("ticket_id"),
                    )
            return response
        started = time.perf_counter()

        if self.sales_closed_event.is_set():
            return {"status": "closed", "message": "La venta fue cerrada."}

        if not self.sales_open_event.is_set():
            with self.meta_lock:
                self.metrics["not_started_count"] += 1
                self.metrics["purchase_time_total"] += time.perf_counter() - started
            return {"status": "not_started", "message": "La venta aún no inicia."}

        if not reservation_id:
            return {"status": "error", "code": "missing_reservation_id"}

        with self.meta_lock:
            self.metrics["purchase_count"] += 1
            zone = self.reservation_to_zone.get(reservation_id)

        if zone is None:
            with self.meta_lock:
                self.metrics["purchase_rejected"] += 1
                self.metrics["purchase_time_total"] += time.perf_counter() - started
            return {"status": "error", "code": "invalid_or_expired_reservation"}

        zone_lock = self.zone_locks[zone]
        acquired = zone_lock.acquire(timeout=0.05)
        if not acquired:
            with self.meta_lock:
                self.metrics["purchase_rejected"] += 1
                self.metrics["purchase_time_total"] += time.perf_counter() - started
            return {"status": "error", "code": "zone_busy_retry"}

        try:
            self._cleanup_expired_zone_locked(zone)
            info = self.reservations_by_zone[zone].get(reservation_id)
            if info is None:
                with self.meta_lock:
                    self.metrics["purchase_rejected"] += 1
                    self.metrics["purchase_time_total"] += time.perf_counter() - started
                return {"status": "error", "code": "invalid_or_expired_reservation"}

            if info["buyer_id"] != str(buyer_id):
                with self.meta_lock:
                    self.metrics["purchase_rejected"] += 1
                    self.metrics["purchase_time_total"] += time.perf_counter() - started
                return {"status": "error", "code": "reservation_owner_mismatch"}

            row, col = info["seat"]

            if self.ticketing_client is None:
                with self.meta_lock:
                    self.metrics["purchase_rejected"] += 1
                    self.metrics["purchase_time_total"] += time.perf_counter() - started
                return {"status": "error", "code": "ticket_service_not_configured"}

            ticket_request = {
                "sale_id": self.sale_id,
                "buyer_id": str(buyer_id),
                "buyer_type": (info.get("buyer_type") or TIPO_NORMAL).lower(),
                "zone": zone,
                "seat": {"row": row, "col": col},
                "reservation_id": reservation_id,
                "request_id": request_id,
                "server_host": self.server_host,
                "server_port": self.server_port,
            }

            with self.meta_lock:
                self.metrics["ticket_request_count"] += 1

            ticket_started = time.perf_counter()
            try:
                ticket_response = self.ticketing_client.create_ticket(ticket_request)
            except Exception as exc:
                with self.meta_lock:
                    self.metrics["ticket_request_fail"] += 1
                    self.metrics["purchase_rejected"] += 1
                    self.metrics["ticket_request_time_total"] += time.perf_counter() - ticket_started
                    self.metrics["purchase_time_total"] += time.perf_counter() - started
                with self.terminal_lock:
                    print(f"Actualización: no fue posible emitir el ticket para {buyer_id}: {exc}")
                return {"status": "error", "code": "ticket_service_unavailable", "message": "No fue posible emitir el ticket."}

            ticket_elapsed = time.perf_counter() - ticket_started
            ticket_status = (ticket_response.get("status") or "").lower()
            ticket_id = ticket_response.get("ticket_id")
            ticket_data = ticket_response.get("ticket")

            with self.meta_lock:
                self.metrics["ticket_request_time_total"] += ticket_elapsed

            if ticket_status != "ok" or not ticket_id:
                with self.meta_lock:
                    self.metrics["ticket_request_fail"] += 1
                    self.metrics["purchase_rejected"] += 1
                    self.metrics["purchase_time_total"] += time.perf_counter() - started
                return {
                    "status": "error",
                    "code": "ticket_generation_failed",
                    "message": "El servicio de tickets no confirmó la emisión.",
                    "ticket_response": ticket_response,
                }

            with self.meta_lock:
                self.metrics["ticket_request_ok"] += 1

            self.reservations_by_zone[zone].pop(reservation_id, None)
            with self.meta_lock:
                self.reservation_to_zone.pop(reservation_id, None)
            self.seat_status[row][col] = "SOLD"

            with self.meta_lock:
                self.sold_count += 1
                self.metrics["purchase_ok"] += 1
                buyer_type = (info.get("buyer_type") or TIPO_NORMAL).lower()
                if buyer_type not in self.purchased_by_type:
                    buyer_type = TIPO_NORMAL
                self.purchased_by_type[buyer_type] += 1
                self._report_progress_milestones_locked()

                if self.sold_count >= TOTAL_ASIENTOS:
                    self._mark_sold_out_locked()

                remaining = TOTAL_ASIENTOS - self.sold_count
                sold_now = self.sold_count
                self.metrics["purchase_time_total"] += time.perf_counter() - started

            self._record_event(
                "ticket_sold",
                buyer_id=str(buyer_id),
                reservation_id=reservation_id,
                zone=zone,
                seat={"row": row, "col": col},
                ticket_id=ticket_id,
            )

            self._invalidate_availability_cache()

            return {
                "status": "ok",
                "reservation_id": reservation_id,
                "zone": zone,
                "seat": {"row": row, "col": col},
                "ticket_id": ticket_id,
                "ticket": ticket_data,
                "sold_count": sold_now,
                "remaining": remaining,
            }
        finally:
            zone_lock.release()

    def get_snapshot(self):
        if self.ticketing_client is not None:
            now = time.monotonic()
            with self.meta_lock:
                cached_snapshot = copy.deepcopy(self._availability_snapshot_cache) if self._availability_snapshot_cache else None
                cached_at = self._availability_snapshot_cache_at

            if cached_snapshot is not None and (now - cached_at) <= self._availability_snapshot_ttl:
                return cached_snapshot

            try:
                remote_snapshot = self.ticketing_client.availability()
            except Exception:
                if cached_snapshot is not None:
                    return cached_snapshot
                return self._build_local_snapshot()

            if (remote_snapshot.get("type") or "").upper() != "AVAILABILITY_RESPONSE":
                if cached_snapshot is not None:
                    return cached_snapshot
                return self._build_local_snapshot()

            seat_status = remote_snapshot.get("seat_status") or []
            reserved_count = int(remote_snapshot.get("reserved_count", 0))
            seats_by_type = {}
            if seat_status:
                seat_counts_by_zone = {
                    ZONA_PLATINO: {"free": 0, "reserved": 0, "sold": 0},
                    ZONA_PREFERENTE: {"free": 0, "reserved": 0, "sold": 0},
                    ZONA_NORMAL: {"free": 0, "reserved": 0, "sold": 0},
                }
                for row_index, row in enumerate(seat_status):
                    zone = self._zone_for_row(row_index)
                    zone_counts = seat_counts_by_zone[zone]
                    for state in row:
                        if state == "SOLD":
                            zone_counts["sold"] += 1
                        elif state == "RESERVED":
                            zone_counts["reserved"] += 1
                        else:
                            zone_counts["free"] += 1
                for zone in (ZONA_PLATINO, ZONA_PREFERENTE, ZONA_NORMAL):
                    zone_counts = seat_counts_by_zone[zone]
                    buyer_type = {ZONA_PLATINO: TIPO_PLATINO, ZONA_PREFERENTE: TIPO_PREFERENTE, ZONA_NORMAL: TIPO_NORMAL}[zone]
                    seats_by_type[buyer_type] = {
                        "free": zone_counts["free"],
                        "sold": zone_counts["sold"],
                        "reserved": zone_counts["reserved"],
                        "total": zone_counts["free"] + zone_counts["sold"] + zone_counts["reserved"],
                    }

            with self.meta_lock:
                recent_events = list(self.recent_events)
                snapshot = {
                    "sold_count": int(remote_snapshot.get("sold_count", 0)),
                    "reserved_count": reserved_count,
                    "free_count": int(remote_snapshot.get("free_count", 0)),
                    "buyers_created": len(self.unique_buyers),
                    "seat_status": seat_status,
                    "sales_open": bool(remote_snapshot.get("sales_open", False)),
                    "sales_closed": bool(remote_snapshot.get("sales_closed", False)),
                    "close_reason": remote_snapshot.get("close_reason"),
                    "sale_id": remote_snapshot.get("sale_id", self.sale_id),
                    "metrics": dict(self.metrics),
                    "recent_events": recent_events,
                    "seats_by_type": seats_by_type,
                }
                self._availability_snapshot_cache = copy.deepcopy(snapshot)
                self._availability_snapshot_cache_at = now
                return snapshot
        lock_order = [self.zone_locks[ZONA_PLATINO], self.zone_locks[ZONA_PREFERENTE], self.zone_locks[ZONA_NORMAL]]
        for lock in lock_order:
            lock.acquire()

        try:
            seat_status_copy = [row[:] for row in self.seat_status]
            reserved_count = sum(len(self.reservations_by_zone[z]) for z in self.reservations_by_zone)
            seats_by_type = {}
            with self.events_lock:
                recent_events = list(self.recent_events)
            seat_counts_by_zone = {
                ZONA_PLATINO: {"free": 0, "reserved": 0, "sold": 0},
                ZONA_PREFERENTE: {"free": 0, "reserved": 0, "sold": 0},
                ZONA_NORMAL: {"free": 0, "reserved": 0, "sold": 0},
            }

            for row_index, row in enumerate(seat_status_copy):
                zone = self._zone_for_row(row_index)
                zone_counts = seat_counts_by_zone[zone]
                for state in row:
                    if state == "SOLD":
                        zone_counts["sold"] += 1
                    elif state == "RESERVED":
                        zone_counts["reserved"] += 1
                    else:
                        zone_counts["free"] += 1

            for zone in ZONE_ORDER:
                buyer_type = ZONE_TO_BUYER_TYPE[zone]
                zone_counts = seat_counts_by_zone[zone]
                seats_by_type[buyer_type] = {
                    "free": zone_counts["free"],
                    "sold": zone_counts["sold"],
                    "reserved": zone_counts["reserved"],
                    "total": zone_counts["free"] + zone_counts["sold"] + zone_counts["reserved"],
                }

            with self.meta_lock:
                return {
                    "sold_count": self.sold_count,
                    "reserved_count": reserved_count,
                    "free_count": TOTAL_ASIENTOS - self.sold_count - reserved_count,
                    "buyers_created": len(self.unique_buyers),
                    "seat_status": seat_status_copy,
                    "sales_open": self.sales_open_event.is_set(),
                    "sales_closed": self.sales_closed_event.is_set(),
                    "close_reason": self.close_reason,
                    "sale_id": self.sale_id,
                    "metrics": dict(self.metrics),
                    "recent_events": recent_events,
                    "seats_by_type": seats_by_type,
                }
        finally:
            for lock in reversed(lock_order):
                lock.release()

    @staticmethod
    def _zone_for_row(row_index):
        if row_index <= 2:
            return ZONA_PLATINO
        if row_index <= 6:
            return ZONA_PREFERENTE
        return ZONA_NORMAL

    def print_summary_once(self):
        with self.meta_lock:
            if self.summary_printed:
                return
            self.summary_printed = True
        self.print_summary()

    def print_summary(self):
        with self.meta_lock:
            if self.sales_started_at is None:
                total_elapsed = 0.0
            elif self.sales_finished_at is None:
                total_elapsed = time.perf_counter() - self.sales_started_at
            else:
                total_elapsed = self.sales_finished_at - self.sales_started_at

            request_count = self.metrics["request_ticket_count"]
            purchase_count = self.metrics["purchase_count"]
            ticket_count = self.metrics["ticket_request_count"]
            request_avg = self.metrics["request_ticket_time_total"] / request_count if request_count else 0.0
            purchase_avg = self.metrics["purchase_time_total"] / purchase_count if purchase_count else 0.0
            ticket_avg = self.metrics["ticket_request_time_total"] / ticket_count if ticket_count else 0.0

            print("\n========== Resumen del Servidor ==========")
            print(f"Asientos totales: {TOTAL_ASIENTOS}")
            print(f"Asientos vendidos: {self.sold_count}")
            print(f"Compradores únicos detectados: {len(self.unique_buyers)}")
            print(f"Reservas activas: {sum(len(self.reservations_by_zone[z]) for z in self.reservations_by_zone)}")
            print(f"Reservas expiradas liberadas: {self.metrics['expired_releases']}")
            print(f"Solicitudes antes del inicio: {self.metrics['not_started_count']}")
            print(f"Tiempo total de ejecución de venta: {total_elapsed:.4f} s")
            print(f"Request_ticket procesados: {request_count}")
            print(f"Compras procesadas: {purchase_count}")
            print(f"Tickets solicitados: {ticket_count}")
            print(f"Request_ticket exitosos: {self.metrics['request_ticket_ok']}")
            print(f"Compras exitosas: {self.metrics['purchase_ok']}")
            print(f"Compras rechazadas: {self.metrics['purchase_rejected']}")
            print(f"Tiempo promedio request_ticket: {request_avg:.6f} s")
            print(f"Tiempo promedio purchase: {purchase_avg:.6f} s")
            print(f"Tickets emitidos exitosamente: {self.metrics['ticket_request_ok']}")
            print(f"Tickets rechazados: {self.metrics['ticket_request_fail']}")
            print(f"Tiempo promedio ticketing: {ticket_avg:.6f} s")
            print("==========================================\n")


def normalize_buyer_type(raw_type):
    normalized = (raw_type or TIPO_NORMAL).strip().lower()
    if normalized not in ALLOWED_ZONES_BY_TYPE:
        return TIPO_NORMAL
    return normalized


def run_internal_load(ticket_state, buyers=50, client_type=TIPO_NORMAL, worker_delay=(0.01, 0.06)):
    buyers = max(1, int(buyers))
    client_type = normalize_buyer_type(client_type)
    client_id = f"LOAD-{uuid.uuid4().hex[:8].upper()}"

    ticket_state.register_client_buyers(client_type, buyers)
    ticket_state.open_sales()
    ticket_state._record_event("load_started", client_id=client_id, buyers=buyers, client_type=client_type)

    stats = {
        "client_id": client_id,
        "client_type": client_type,
        "buyers": buyers,
        "success": 0,
        "fail": 0,
        "network_errors": 0,
        "started_at": time.perf_counter(),
        "finished_at": None,
    }
    stats_lock = threading.Lock()

    def buyer_worker(buyer_number):
        buyer_id = f"{client_id}-B{buyer_number}"
        purchased = False
        local_network_errors = 0

        while not ticket_state.sales_closed() and not ticket_state.sold_out_event.is_set():
            time.sleep(random.uniform(*worker_delay))

            if not ticket_state.can_buyer_type_keep_trying(client_type):
                break

            reserve_response = ticket_state.request_ticket(buyer_id, client_type, str(uuid.uuid4()))
            reserve_status = reserve_response.get("status")

            if reserve_status == "ok":
                reservation_id = reserve_response.get("reservation_id")
                if not reservation_id:
                    continue

                time.sleep(random.uniform(0.04, 0.12))
                purchase_response = ticket_state.purchase(buyer_id, reservation_id, str(uuid.uuid4()))
                purchase_status = purchase_response.get("status")

                if purchase_status == "ok":
                    purchased = True
                    if int(purchase_response.get("remaining") or 1) <= 0:
                        ticket_state.sold_out_event.set()
                    break

                if purchase_status in {"closed", "sold_out"}:
                    ticket_state.release_reservation(reservation_id, f"purchase_{purchase_status}")
                    break

                ticket_state.release_reservation(reservation_id, f"purchase_{purchase_status or 'failed'}")
                continue

            if reserve_status in {"closed", "sold_out"}:
                break

            if reserve_status == "not_started":
                continue

            if reserve_status == "no_zone_available":
                if not ticket_state.can_buyer_type_keep_trying(client_type):
                    break
                continue

            continue

        with stats_lock:
            if purchased:
                stats["success"] += 1
            else:
                stats["fail"] += 1
            stats["network_errors"] += local_network_errors

    threads = []
    for buyer_number in range(1, buyers + 1):
        thread = threading.Thread(target=buyer_worker, args=(buyer_number,), daemon=True)
        threads.append(thread)
        thread.start()
        time.sleep(0.001)

    for thread in threads:
        thread.join()

    stats["finished_at"] = time.perf_counter()
    stats["elapsed"] = stats["finished_at"] - stats["started_at"]

    released = ticket_state.release_reservations_for_buyer_prefix(client_id, "load_finished_cleanup")
    if released:
        ticket_state._record_event(
            "load_reservations_released",
            client_id=client_id,
            buyers=buyers,
            released=released,
        )

    ticket_state._record_event(
        "load_finished",
        client_id=client_id,
        buyers=buyers,
        success=stats["success"],
        fail=stats["fail"],
    )

    return stats

class LoadJobManager:
    def __init__(self, ticket_state):
        self.ticket_state = ticket_state
        self.lock = threading.Lock()
        self.jobs = {}

    def start_job(self, buyers=50, client_type=TIPO_NORMAL):
        job_id = uuid.uuid4().hex[:12]
        job = {
            "job_id": job_id,
            "status": "running",
            "buyers": max(1, int(buyers)),
            "client_type": normalize_buyer_type(client_type),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "started_at": time.perf_counter(),
            "finished_at": None,
            "result": None,
            "error": None,
        }

        with self.lock:
            self.jobs[job_id] = job

        def runner():
            try:
                result = run_internal_load(self.ticket_state, buyers=job["buyers"], client_type=job["client_type"])
                with self.lock:
                    job["status"] = "finished"
                    job["finished_at"] = time.perf_counter()
                    job["result"] = result
            except Exception as exc:
                with self.lock:
                    job["status"] = "failed"
                    job["finished_at"] = time.perf_counter()
                    job["error"] = str(exc)

        threading.Thread(target=runner, daemon=True).start()
        return dict(job)

    def snapshot(self):
        with self.lock:
            jobs = []
            for job in self.jobs.values():
                item = dict(job)
                if item.get("finished_at") is not None and item.get("started_at") is not None:
                    item["elapsed"] = item["finished_at"] - item["started_at"]
                jobs.append(item)
            jobs.sort(key=lambda item: item.get("created_at", ""), reverse=True)
            return jobs

    def clear_jobs(self):
        with self.lock:
            # Mark running jobs as cancelled and drop history
            for jid, j in list(self.jobs.items()):
                if j.get("status") == "running":
                    j["status"] = "cancelled"
                    j["finished_at"] = time.perf_counter()
            self.jobs.clear()


class CloudSaleHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address, handler_class, ticket_state):
        super().__init__(server_address, handler_class)
        self.ticket_state = ticket_state
        self.load_jobs = LoadJobManager(ticket_state)
        self.client_lock = threading.Lock()
        self.connected_clients = {}
        self.ready_clients = set()
        self.done_clients = set()
        self.expected_clients = 1


class CloudSaleHTTPRequestHandler(BaseHTTPRequestHandler):
    server_version = "SaleCloudAPI/1.0"

    def log_message(self, format, *args):  # noqa: A003
        return

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        super().end_headers()

    def _send_json(self, status_code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return

    def _read_json(self):
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length) if content_length > 0 else b"{}"
        if not raw_body:
            return {}
        payload = json.loads(raw_body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("invalid_json")
        return payload

    @property
    def state(self):
        return self.server.ticket_state  # type: ignore[attr-defined]

    @property
    def load_jobs(self):
        return self.server.load_jobs  # type: ignore[attr-defined]

    @property
    def client_lock(self):
        return self.server.client_lock  # type: ignore[attr-defined]

    @property
    def connected_clients(self):
        return self.server.connected_clients  # type: ignore[attr-defined]

    @property
    def ready_clients(self):
        return self.server.ready_clients  # type: ignore[attr-defined]

    @property
    def done_clients(self):
        return self.server.done_clients  # type: ignore[attr-defined]

    @property
    def expected_clients(self):
        return self.server.expected_clients  # type: ignore[attr-defined]

    def _api_path(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/"):
            path = path[4:]
        elif path == "/api":
            path = "/"
        return path

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self):  # noqa: N802
        path = self._api_path()

        if path in {"/", "/index.html", ""}:
            index_file = WEBAPP_DIR / "index.html"
            if not index_file.exists():
                self._send_json(HTTPStatus.NOT_FOUND, {"status": "error", "code": "not_found"})
                return

            body = index_file.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        static_file = WEBAPP_DIR / path.lstrip("/")
        if static_file.is_file() and static_file.suffix.lower() in {".html", ".css", ".js", ".json", ".png", ".svg", ".ico", ".webmanifest"}:
            body = static_file.read_bytes()
            content_type, _ = guess_type(static_file.name)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/health":
            self._send_json(HTTPStatus.OK, {"status": "ok", "service": "sale-api"})
            return

        if path == "/availability":
            snapshot = self.state.get_snapshot()
            snapshot["sale_status"] = {
                "state": "closed" if snapshot.get("sales_closed") else ("open" if snapshot.get("sales_open") else "waiting"),
                "sales_open": snapshot.get("sales_open", False),
                "sales_closed": snapshot.get("sales_closed", False),
                "close_reason": snapshot.get("close_reason"),
                "connected_clients": len(self.connected_clients),
                "ready_clients": len(self.ready_clients),
                "done_clients": len(self.done_clients),
                "expected_clients": self.expected_clients,
            }
            self._send_json(HTTPStatus.OK, snapshot)
            return

        if path == "/stats":
            snapshot = self.state.get_snapshot()
            snapshot["status"] = "ok"
            snapshot["load_jobs"] = self.load_jobs.snapshot()
            snapshot["summary"] = {
                "sold_count": snapshot.get("sold_count", 0),
                "reserved_count": snapshot.get("reserved_count", 0),
                "free_count": snapshot.get("free_count", 0),
                "tickets_emitted": snapshot.get("metrics", {}).get("ticket_request_ok", 0),
            }
            self._send_json(HTTPStatus.OK, snapshot)
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"status": "error", "code": "not_found"})

    def do_POST(self):  # noqa: N802
        path = self._api_path()

        try:
            payload = self._read_json()
        except (json.JSONDecodeError, ValueError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"status": "error", "code": "invalid_json"})
            return

        if path in {"/generate-load", "/load"}:
            buyers = int(payload.get("buyers") or 50)
            client_type = payload.get("client_type") or TIPO_NORMAL
            job = self.load_jobs.start_job(buyers=buyers, client_type=client_type)
            self._send_json(HTTPStatus.ACCEPTED, {"status": "ok", "job": job})
            return

        if path == "/register_client":
            client_id = payload.get("client_id")
            client_type = payload.get("client_type") or TIPO_NORMAL
            buyers = int(payload.get("buyers") or 1)
            buyer_ids = payload.get("buyer_ids") or []
            if not client_id:
                self._send_json(HTTPStatus.BAD_REQUEST, {"type": "ERROR", "code": "missing_client_id"})
                return
            with self.client_lock:
                self.connected_clients[client_id] = {
                    "client_type": client_type,
                    "buyers": buyers,
                    "connected_at": time.strftime("%H:%M:%S"),
                }
                connected = len(self.connected_clients)
            self.state.register_client_buyers(client_type, buyers)
            if buyer_ids:
                for buyer_id in buyer_ids:
                    self.state.register_buyer(buyer_id)
            elif buyers == 1:
                self.state.register_buyer(client_id)

            if not self.state.sales_open_event.is_set():
                self.state.open_sales()

            self._send_json(
                HTTPStatus.OK,
                {
                    "type": "REGISTERED",
                    "client_id": client_id,
                    "connected_clients": connected,
                    "expected_clients": self.expected_clients,
                },
            )
            return

        if path == "/ready":
            client_id = payload.get("client_id")
            if not client_id:
                self._send_json(HTTPStatus.BAD_REQUEST, {"type": "ERROR", "code": "missing_client_id"})
                return
            with self.client_lock:
                self.ready_clients.add(client_id)
                ready_count = len(self.ready_clients)
            self._send_json(
                HTTPStatus.OK,
                {
                    "type": "START_ACK",
                    "client_id": client_id,
                    "ready_clients": ready_count,
                    "expected_clients": self.expected_clients,
                },
            )
            return

        if path == "/request_ticket":
            response = self.state.request_ticket(
                payload.get("buyer_id"),
                payload.get("buyer_type", TIPO_NORMAL),
                payload.get("request_id") or str(uuid.uuid4()),
                payload.get("row"),
                payload.get("col"),
            )
            self._send_json(HTTPStatus.OK, response)
            return

        if path == "/purchase":
            response = self.state.purchase(
                payload.get("buyer_id"),
                payload.get("reservation_id"),
                payload.get("request_id") or str(uuid.uuid4()),
            )
            self._send_json(HTTPStatus.OK, response)
            return

        if path == "/release_reservation":
            success = self.state.release_reservation(payload.get("reservation_id"), "pwa_release")
            if success:
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "type": "RELEASE_TICKET_RESPONSE",
                        "status": "ok",
                        "reservation_id": payload.get("reservation_id"),
                    },
                )
            else:
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "type": "RELEASE_TICKET_RESPONSE",
                        "status": "error",
                        "code": "invalid_or_expired_reservation",
                        "message": "La reserva no existe o expiró.",
                    },
                )
            return

        if path in {"/restart-sale", "/restart-sale"}:
            # Reset server-side sale state and clear persisted tickets file if present
            try:
                self.state.reset_sale()
                try:
                    # Clear any internal load job records so the UI reflects a clean state
                    self.load_jobs.clear_jobs()
                except Exception:
                    pass
                # Attempt to clear tickets storage if available on disk
                tickets_path = Path(__file__).resolve().parent / "tickets" / "tickets.txt"
                try:
                    if tickets_path.exists():
                        tickets_path.write_text("")
                except Exception:
                    # Non-fatal: continue even if file can't be cleared
                    pass

                with self.state.terminal_lock:
                    print("Actualización: venta reiniciada por petición REST.")

                self._send_json(HTTPStatus.ACCEPTED, {"status": "ok", "message": "sale_restarted"})
            except Exception as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"status": "error", "message": str(exc)})
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"status": "error", "code": "not_found"})


class TicketServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False

    def __init__(self, server_address, handler_class, ticket_state, expected_clients, sale_id, use_global_sync=False):
        super().__init__(server_address, handler_class)
        self.ticket_state = ticket_state
        self.expected_clients = expected_clients
        self.sale_id = sale_id
        self.use_global_sync = use_global_sync
        self.registration_lock = threading.Lock()
        self.connected_clients = {}
        self.ready_clients = set()
        self.done_clients = set()
        self.start_event = threading.Event()
        self.all_ready_event = threading.Event()
        self.global_start_event = threading.Event()
        if not self.use_global_sync:
            self.global_start_event.set()
    

    def register_client(self, client_id, client_type, buyers_count):
        with self.registration_lock:
            self.connected_clients[client_id] = {
                "client_type": client_type,
                "buyers": buyers_count,
                "connected_at": time.strftime("%H:%M:%S"),
            }
            self.ticket_state.register_client_buyers(client_type, buyers_count)
            connected = len(self.connected_clients)
            return connected

    def mark_ready(self, client_id):
        all_ready = False
        with self.registration_lock:
            self.ready_clients.add(client_id)
            ready_count = len(self.ready_clients)
            connected_count = len(self.connected_clients)

            if (not self.all_ready_event.is_set()
                    and connected_count >= self.expected_clients
                    and ready_count >= self.expected_clients):
                all_ready = True
                self.all_ready_event.set()

        if all_ready:
            with self.ticket_state.terminal_lock:
                print("Todos los clientes esperados están listos localmente.")
            if not self.use_global_sync:
                self.global_start_event.set()

        return ready_count

    def trigger_start(self):
        with self.registration_lock:
            if self.start_event.is_set():
                return
            self.start_event.set()
        self.ticket_state.open_sales()
        with self.ticket_state.terminal_lock:
            print("Señal START enviada. ¡Venta abierta!")

    def mark_client_done(self, client_id):
        with self.registration_lock:
            self.done_clients.add(client_id)
            done_count = len(self.done_clients)

        with self.ticket_state.terminal_lock:
            print("Cliente reportó fin de ejecución. La venta continúa abierta hasta sold-out.")

        return done_count


# Coordinator client removed for simplified deployment.



class TicketRequestHandler(socketserver.StreamRequestHandler):
    def send_json(self, payload):
        try:
            self.wfile.write((json.dumps(payload) + "\n").encode("utf-8"))
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return False

    def handle(self):
        while True:
            try:
                raw_line = self.rfile.readline()
            except (ConnectionResetError, ConnectionAbortedError, OSError):
                return
            if not raw_line:
                return

            try:
                payload = json.loads(raw_line.decode("utf-8").strip())
            except json.JSONDecodeError:
                self.send_json({"type": "ERROR", "code": "invalid_json"})
                continue

            message_type = (payload.get("type") or "").upper()
            request_id = payload.get("request_id", str(uuid.uuid4()))

            if message_type == "REGISTER":
                client_id = payload.get("client_id")
                client_type = (payload.get("client_type") or "").lower()
                buyers_count = int(payload.get("buyers", 0))

                if not client_id:
                    self.send_json({"type": "ERROR", "code": "missing_client_id"})
                    continue

                connected = self.server.register_client(client_id, client_type, buyers_count)
                self.send_json({
                    "type": "REGISTERED",
                    "client_id": client_id,
                    "connected_clients": connected,
                    "expected_clients": self.server.expected_clients,
                })
                continue

            if message_type == "READY":
                client_id = payload.get("client_id")
                if not client_id:
                    self.send_json({"type": "ERROR", "code": "missing_client_id"})
                    continue

                ready_count = self.server.mark_ready(client_id)
                self.server.start_event.wait()
                self.send_json({
                    "type": "START",
                    "client_id": client_id,
                    "ready_clients": ready_count,
                    "expected_clients": self.server.expected_clients,
                })
                continue

            if message_type == "REQUEST_TICKET":
                buyer_id = payload.get("buyer_id")
                buyer_type = payload.get("buyer_type", TIPO_NORMAL)
                response = self.server.ticket_state.request_ticket(buyer_id, buyer_type, request_id)
                response["type"] = "REQUEST_TICKET_RESPONSE"
                self.send_json(response)
                continue

            if message_type == "PURCHASE":
                buyer_id = payload.get("buyer_id")
                reservation_id = payload.get("reservation_id")
                response = self.server.ticket_state.purchase(buyer_id, reservation_id, request_id)
                response["type"] = "PURCHASE_RESPONSE"
                self.send_json(response)
                continue

            if message_type == "HEALTH":
                snapshot = self.server.ticket_state.get_snapshot()
                self.send_json({
                    "type": "HEALTH_RESPONSE",
                    "status": "ok",
                    "total_seats": TOTAL_ASIENTOS,
                    "sales_open": self.server.ticket_state.sales_open(),
                    "sales_closed": self.server.ticket_state.sales_closed(),
                    "connected_clients": len(self.server.connected_clients),
                    "ready_clients": len(self.server.ready_clients),
                    "done_clients": len(self.server.done_clients),
                    "expected_clients": self.server.expected_clients,
                    "sold_count": snapshot["sold_count"],
                })
                continue

            if message_type == "CLIENT_DONE":
                client_id = payload.get("client_id")
                if not client_id:
                    self.send_json({"type": "ERROR", "code": "missing_client_id"})
                    continue

                done_count = self.server.mark_client_done(client_id)
                self.send_json({
                    "type": "DONE_ACK",
                    "client_id": client_id,
                    "done_clients": done_count,
                    "expected_clients": self.server.expected_clients,
                })
                continue

            self.send_json({"type": "ERROR", "code": "unknown_message_type"})


class ServerDashboard:
    def __init__(self, ticket_state, server, host, port):
        self.ticket_state = ticket_state
        self.server = server
        self.host = host
        self.port = port

        self.root = tk.Tk()
        self.root.title("Servidor de boletos - Múltiples clientes")
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self.label_margin = 30
        self.cell_size = 18
        self.grid_width = COLUMNAS * self.cell_size
        self.visual_rows = FILAS + (SECTION_GAP_ROWS * 2) + (SECTION_LABEL_ROWS * 3)
        self.grid_height = self.visual_rows * self.cell_size
        self.canvas_width = self.label_margin + self.grid_width + 10
        self.canvas_height = self.label_margin + self.grid_height + 10
        self.waiting_mode = True
        self.simulation_started = False
        self.countdown_active = False

        self.root.configure(bg="#0a0e27")

        self.waiting_frame = tk.Frame(self.root, bg="#0a0e27")
        self.waiting_frame.pack(fill="both", expand=True)
        self.main_frame = tk.Frame(self.root)

        self.waiting_title = tk.Label(
            self.waiting_frame,
            text="VENTA DE BOLETOS",
            font=("Arial", 28, "bold"),
            fg="#276ef1",
            bg="#0a0e27",
        )
        self.waiting_title.pack(pady=(90, 18))

        self.waiting_label = tk.Label(
            self.waiting_frame,
            text=f"Esperando {self.server.expected_clients} cliente(s)...",
            font=("Arial", 18),
            fg="white",
            bg="#0a0e27",
        )
        self.waiting_label.pack(pady=(0, 10))

        self.waiting_sublabel = tk.Label(
            self.waiting_frame,
            text="0 conectados · 0 listos",
            font=("Arial", 14),
            fg="#a0a8c0",
            bg="#0a0e27",
        )
        self.waiting_sublabel.pack(pady=(0, 25))

        bar_width = min(460, int(self.canvas_width * 0.6))
        bar_height = 34
        self.waiting_bar_canvas = tk.Canvas(
            self.waiting_frame,
            width=bar_width,
            height=bar_height,
            bg="#1a1a3e",
            highlightthickness=1,
            highlightbackground="#333366",
        )
        self.waiting_bar_canvas.pack()
        self.waiting_bar_fill = self.waiting_bar_canvas.create_rectangle(
            0,
            0,
            0,
            bar_height,
            fill="#276ef1",
            outline="",
        )
        self.waiting_bar_text = self.waiting_bar_canvas.create_text(
            bar_width // 2,
            bar_height // 2,
            text="0%",
            fill="white",
            font=("Arial", 11, "bold"),
        )
        self.waiting_bar_width = bar_width
        self.waiting_bar_height = bar_height

        self.main_frame.configure(bg="white")
        self.main_frame.pack(padx=10, pady=10)
        self.main_frame.pack_forget()

        self.canvas = tk.Canvas(self.main_frame, width=self.canvas_width, height=self.canvas_height, bg="white")
        self.canvas.pack(side="left")

        info_frame = tk.Frame(self.main_frame, padx=16)
        info_frame.pack(side="left", fill="y")

        tk.Label(info_frame, text=f"Servidor: {self.host}:{self.port}", font=("Arial", 11), anchor="w").pack(fill="x", pady=(4, 8))

        self.buyers_label = tk.Label(info_frame, text="Compradores detectados: 0", font=("Arial", 11), anchor="w")
        self.buyers_label.pack(fill="x", pady=4)

        self.sold_label = tk.Label(info_frame, text="Asientos vendidos: 0", font=("Arial", 11), anchor="w")
        self.sold_label.pack(fill="x", pady=4)

        self.reserved_label = tk.Label(info_frame, text="Asientos apartados: 0", font=("Arial", 11), anchor="w")
        self.reserved_label.pack(fill="x", pady=4)

        self.free_label = tk.Label(info_frame, text=f"Asientos libres: {TOTAL_ASIENTOS}", font=("Arial", 11), anchor="w")
        self.free_label.pack(fill="x", pady=4)

        tk.Label(info_frame, text="Leyenda", font=("Arial", 11, "bold"), anchor="w").pack(fill="x", pady=(14, 4))
        tk.Label(info_frame, text="Gris: Libre", font=("Arial", 10), anchor="w").pack(fill="x")
        tk.Label(info_frame, text="Naranja: Apartado", font=("Arial", 10), anchor="w").pack(fill="x")
        tk.Label(info_frame, text="Azul: Vendido", font=("Arial", 10), anchor="w").pack(fill="x")

        tk.Label(info_frame, text="Secciones", font=("Arial", 11, "bold"), anchor="w").pack(fill="x", pady=(14, 4))
        tk.Label(info_frame, text="Platino: filas 1-3", font=("Arial", 10), anchor="w").pack(fill="x")
        tk.Label(info_frame, text="Preferente: filas 4-7", font=("Arial", 10), anchor="w").pack(fill="x")
        tk.Label(info_frame, text="Normal: filas 8-30", font=("Arial", 10), anchor="w").pack(fill="x")

        self.radius = int(self.cell_size * 0.35)
        self.seat_items = [[None for _ in range(COLUMNAS)] for _ in range(FILAS)]
        self.last_status = [[None for _ in range(COLUMNAS)] for _ in range(FILAS)]
        self.final_popup_shown = False

        for c in range(COLUMNAS):
            label_x = self.label_margin + c * self.cell_size + self.cell_size // 2
            self.canvas.create_text(label_x, 12, text=str(c + 1), fill="black", font=("Arial", 8))

        for r in range(FILAS):
            visual_row = self._to_visual_row(r)
            label_y = self.label_margin + visual_row * self.cell_size + self.cell_size // 2
            self.canvas.create_text(12, label_y, text=str(r + 1), fill="black", font=("Arial", 8))

        self._draw_zone_guides()
        self._draw_section_labels()

        for r in range(FILAS):
            for c in range(COLUMNAS):
                center_x = self.label_margin + c * self.cell_size + self.cell_size // 2
                visual_row = self._to_visual_row(r)
                center_y = self.label_margin + visual_row * self.cell_size + self.cell_size // 2
                seat = self.canvas.create_oval(
                    center_x - self.radius,
                    center_y - self.radius,
                    center_x + self.radius,
                    center_y + self.radius,
                    fill=self.free_color_for_row(r),
                    outline="black",
                )
                self.seat_items[r][c] = seat

    @staticmethod
    def zone_for_row(row):
        if row <= 2:
            return ZONA_PLATINO
        if row <= 6:
            return ZONA_PREFERENTE
        return ZONA_NORMAL

    @staticmethod
    def free_color_for_row(row):
        zone = ServerDashboard.zone_for_row(row)
        if zone == ZONA_PLATINO:
            return "#B87474"
        if zone == ZONA_PREFERENTE:
            return "#73B27D"
        return "gray"

    def _to_visual_row(self, row):
        offset = SECTION_LABEL_ROWS
        if row >= 3:
            offset += SECTION_GAP_ROWS + SECTION_LABEL_ROWS
        if row >= 7:
            offset += SECTION_GAP_ROWS + SECTION_LABEL_ROWS
        return row + offset

    def _draw_zone_guides(self):
        pref_end_visual_row = self._to_visual_row(2) + 1
        plat_end_visual_row = self._to_visual_row(6) + 1

        y_pref_end = self.label_margin + (pref_end_visual_row * self.cell_size)
        y_plat_end = self.label_margin + (plat_end_visual_row * self.cell_size)

        self.canvas.create_line(self.label_margin, y_pref_end, self.label_margin + self.grid_width, y_pref_end, fill="#9A9A9A", dash=(3, 2))
        self.canvas.create_line(self.label_margin, y_plat_end, self.label_margin + self.grid_width, y_plat_end, fill="#9A9A9A", dash=(3, 2))

    def _draw_section_labels(self):
        center_x = self.label_margin + self.grid_width // 2
        pref_label_row = 0
        plat_label_row = self._to_visual_row(3) - SECTION_LABEL_ROWS
        norm_label_row = self._to_visual_row(7) - SECTION_LABEL_ROWS

        y_pref = self.label_margin + (pref_label_row * self.cell_size) + (self.cell_size // 2)
        y_plat = self.label_margin + (plat_label_row * self.cell_size) + (self.cell_size // 2)
        y_norm = self.label_margin + (norm_label_row * self.cell_size) + (self.cell_size // 2)

        self.canvas.create_text(center_x, y_pref, text="SECCIÓN PLATINO", fill="#7A3F3F", font=("Arial", 9, "bold"))
        self.canvas.create_text(center_x, y_plat, text="SECCIÓN PREFERENTE", fill="#56795E", font=("Arial", 9, "bold"))
        self.canvas.create_text(center_x, y_norm, text="SECCIÓN NORMAL", fill="#555555", font=("Arial", 9, "bold"))

    @staticmethod
    def seat_color(status, row):
        if status == "SOLD":
            return "blue"
        if status == "RESERVED":
            return "orange"
        return ServerDashboard.free_color_for_row(row)

    def refresh(self):
        snapshot = self.ticket_state.get_snapshot()
        status_matrix = snapshot["seat_status"]

        for r in range(FILAS):
            for c in range(COLUMNAS):
                current = status_matrix[r][c]
                if self.last_status[r][c] != current:
                    self.last_status[r][c] = current
                    self.canvas.itemconfig(self.seat_items[r][c], fill=self.seat_color(current, r))

        self.buyers_label.config(text=f"Compradores detectados: {snapshot['buyers_created']}")
        self.sold_label.config(text=f"Asientos vendidos: {snapshot['sold_count']}")
        self.reserved_label.config(text=f"Asientos apartados: {snapshot['reserved_count']}")
        self.free_label.config(text=f"Asientos libres: {snapshot['free_count']}")

        if self.ticket_state.sales_closed() and not self.final_popup_shown:
            self.final_popup_shown = True
            self.show_final_popup(snapshot["sold_count"])
            return

        self.root.after(50, self.refresh)

    def show_final_popup(self, sold_count):
        popup = tk.Toplevel(self.root)
        popup.title("Fin de venta")
        popup.transient(self.root)
        popup.resizable(False, False)

        if sold_count >= TOTAL_ASIENTOS:
            message = "La venta ha concluido"
        else:
            message = f"La venta cerró con {sold_count}/{TOTAL_ASIENTOS} vendidos"

        tk.Label(popup, text=message, font=("Arial", 12), padx=24, pady=20).pack()

        self.root.update_idletasks()
        popup.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - popup.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - popup.winfo_height()) // 2
        popup.geometry(f"+{x}+{y}")

        self.root.after(3000, self.close)

    def _check_ready_phase(self):
        if not self.waiting_mode:
            return

        if self.server.use_global_sync:
            if not self.server.global_start_event.is_set():
                with self.server.registration_lock:
                    connected = len(self.server.connected_clients)
                    ready = len(self.server.ready_clients)
                expected = self.server.expected_clients
                self.waiting_label.config(text="Esperando señal global del coordinador...")
                self.waiting_sublabel.config(
                    text=f"Local: {connected}/{expected} conectados · {ready}/{expected} READY"
                )
                self.root.after(200, self._check_ready_phase)
                return

            self._start_countdown()
            return

        if self.server.all_ready_event.is_set():
            self._start_countdown()
            return

        with self.server.registration_lock:
            connected = len(self.server.connected_clients)
            ready = len(self.server.ready_clients)

        expected = self.server.expected_clients
        self.waiting_label.config(text=f"Esperando {expected} cliente(s)...")
        self.waiting_sublabel.config(text=f"{connected} conectados \u00b7 {ready} listos")
        self.root.after(200, self._check_ready_phase)

    def _start_countdown(self):
        if self.countdown_active:
            return
        self.countdown_active = True
        self.waiting_label.config(text="\u00a1Todos los clientes listos!")
        self.waiting_sublabel.config(text="La venta inicia en 5 segundos...")
        self.countdown_start = time.time()
        self.countdown_duration = 5.0
        self._update_countdown()

    def _update_countdown(self):
        if not self.waiting_mode:
            return

        elapsed = time.time() - self.countdown_start
        progress = min(elapsed / self.countdown_duration, 1.0)
        fill_width = int(self.waiting_bar_width * progress)

        self.waiting_bar_canvas.coords(
            self.waiting_bar_fill, 0, 0, fill_width, self.waiting_bar_height
        )
        percent = int(progress * 100)
        self.waiting_bar_canvas.itemconfig(self.waiting_bar_text, text=f"{percent}%")

        remaining = max(0.0, self.countdown_duration - elapsed)
        if remaining > 0:
            secs_left = int(remaining) + (1 if remaining > int(remaining) else 0)
            self.waiting_sublabel.config(text=f"La venta inicia en {secs_left} segundos...")

        if progress >= 1.0:
            self.waiting_sublabel.config(text="\u00a1Venta abierta!")
            self.root.after(250, self._show_simulation_view)
            return

        self.root.after(50, self._update_countdown)

    def _show_simulation_view(self):
        if not self.waiting_mode:
            return
        self.waiting_mode = False
        self.waiting_frame.pack_forget()
        self.root.configure(bg="white")
        self.main_frame.pack(padx=10, pady=10)

        if not self.simulation_started:
            self.simulation_started = True
            self.server.trigger_start()
            self.refresh()

    def run(self):
        self._check_ready_phase()
        self.root.mainloop()

    def close(self):
        try:
            self.server.shutdown()
        except Exception:
            pass
        try:
            self.server.server_close()
        except Exception:
            pass

        self.ticket_state.print_summary_once()

        if self.root.winfo_exists():
            self.root.destroy()


def cleanup_expired_reservations(ticket_state):
    while not ticket_state.sold_out_event.is_set():
        if ticket_state.sales_open_event.is_set():
            for zone in (ZONA_PLATINO, ZONA_PREFERENTE, ZONA_NORMAL):
                zone_lock = ticket_state.zone_locks[zone]
                acquired = zone_lock.acquire(timeout=0.05)
                if acquired:
                    try:
                        ticket_state._cleanup_expired_zone_locked(zone)
                    finally:
                        zone_lock.release()
        time.sleep(0.05)


def monitor_sold_out(ticket_state):
    ticket_state.sold_out_event.wait()
    with ticket_state.meta_lock:
        sold_count = ticket_state.sold_count
        close_reason = ticket_state.close_reason
        if ticket_state.sales_started_at is None:
            sale_elapsed = 0.0
        elif ticket_state.sales_finished_at is None:
            sale_elapsed = time.perf_counter() - ticket_state.sales_started_at
        else:
            sale_elapsed = ticket_state.sales_finished_at - ticket_state.sales_started_at
        need_100 = 100 not in ticket_state.hitos_reportados
        if sold_count >= TOTAL_ASIENTOS and need_100:
            ticket_state.hitos_reportados.add(100)

        finish_summary = {
            "sold_count": sold_count,
            "total_seats": TOTAL_ASIENTOS,
            "empty_seats": TOTAL_ASIENTOS - sold_count,
            "sale_elapsed_seconds": float(sale_elapsed),
            "close_reason": close_reason,
            "unique_buyers": len(ticket_state.unique_buyers),
            "request_ticket_count": ticket_state.metrics["request_ticket_count"],
            "purchase_count": ticket_state.metrics["purchase_count"],
        }

    with ticket_state.terminal_lock:
        if sold_count >= TOTAL_ASIENTOS and need_100:
            print(f"Actualización: 100% de boletos vendidos ({sold_count}/{TOTAL_ASIENTOS}).")
        if close_reason == "all_clients_done" and sold_count < TOTAL_ASIENTOS:
            print(f"Actualización: venta cerrada por fin de clientes ({sold_count}/{TOTAL_ASIENTOS} vendidos).")
        if close_reason == "unsellable_remaining":
            print(f"Actualización: venta cerrada porque los asientos restantes no tienen compradores elegibles ({sold_count}/{TOTAL_ASIENTOS} vendidos).")
        print("Actualización: la venta ha concluido.")

    # Coordinator notifications removed.

    ticket_state.print_summary_once()


def parse_args():
    parser = argparse.ArgumentParser(description="Servidor de boletos para despliegue en nube")
    parser.add_argument("--host", default="0.0.0.0", help="Host para escuchar conexiones HTTP")
    parser.add_argument("--port", type=int, default=8080, help="Puerto para escuchar conexiones HTTP")
    parser.add_argument("--sale-id", default="venta-cloud", help="Identificador de esta venta")
    parser.add_argument("--ticket-service-host", default="127.0.0.1", help="Host del Ticketing Service externo")
    parser.add_argument("--ticket-service-port", type=int, default=7000, help="Puerto del Ticketing Service externo")
    return parser.parse_args()


def main():
    args = parse_args()
    sale_id = args.sale_id or f"{args.host}:{args.port}"

    ticket_state = TicketState()
    ticket_state.set_sale_context(sale_id, args.host, args.port)

    ticketing_client = TicketingServiceClient(args.ticket_service_host, args.ticket_service_port)
    ticket_state.set_ticketing_client(ticketing_client)
    ticketing_client.set_context(sale_id, args.host, args.port)

    server = CloudSaleHTTPServer((args.host, args.port), CloudSaleHTTPRequestHandler, ticket_state)

    print("Servidor HTTP de boletos iniciado")
    print(f"Escuchando en http://{args.host}:{args.port}")
    print(f"Sale ID: {sale_id}")
    print(f"Ticketing Service: {args.ticket_service_host}:{args.ticket_service_port}")
    print("Endpoints: /health, /stats, /generate-load")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Servidor] Interrupción recibida. Cerrando servidor...")
    finally:
        with ticket_state.meta_lock:
            if not ticket_state.sales_closed_event.is_set():
                ticket_state.close_sales("shutdown")
        ticket_state.print_summary_once()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
