# Practica 8 - Microservicios

import argparse
import json
import random
import socketserver
import threading
import time
import uuid
from pathlib import Path


FILAS = 30
COLUMNAS = 50
TOTAL_ASIENTOS = FILAS * COLUMNAS
RESERVA_TTL_SEGUNDOS = 10.0

ZONA_PLATINO = "PLATINO"
ZONA_PREFERENTE = "PREFERENTE"
ZONA_NORMAL = "NORMAL"

TIPO_PLATINO = "platino"
TIPO_PREFERENTE = "preferente"
TIPO_NORMAL = "normal"

ALLOWED_ZONES_BY_TYPE = {
    TIPO_PLATINO: [ZONA_PLATINO, ZONA_PREFERENTE, ZONA_NORMAL],
    TIPO_PREFERENTE: [ZONA_PREFERENTE, ZONA_NORMAL],
    TIPO_NORMAL: [ZONA_NORMAL],
}


def zone_for_row(row):
    if row <= 2:
        return ZONA_PLATINO
    if row <= 6:
        return ZONA_PREFERENTE
    return ZONA_NORMAL


def build_zone_seats():
    zones = {
        ZONA_PLATINO: set(),
        ZONA_PREFERENTE: set(),
        ZONA_NORMAL: set(),
    }
    for row in range(FILAS):
        for col in range(COLUMNAS):
            zones[zone_for_row(row)].add((row, col))
    return zones


class TicketStore:
    def __init__(self, store_file):
        self.store_file = Path(store_file)
        self.lock = threading.Lock()
        self.ticket_count = 0
        self.store_file.parent.mkdir(parents=True, exist_ok=True)

    def create_ticket(self, payload):
        required_fields = ["sale_id", "buyer_id", "buyer_type", "zone", "seat", "reservation_id", "request_id"]
        missing_fields = [field for field in required_fields if not payload.get(field)]
        seat = payload.get("seat")
        if not isinstance(seat, dict):
            missing_fields.append("seat")
        elif "row" not in seat or "col" not in seat:
            missing_fields.append("seat.row/seat.col")

        if missing_fields:
            raise ValueError("Faltan campos requeridos: " + ", ".join(missing_fields))

        ticket_id = f"TKT-{uuid.uuid4().hex[:12].upper()}"
        created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        record = {
            "ticket_id": ticket_id,
            "created_at": created_at,
            "sale_id": payload["sale_id"],
            "buyer_id": payload["buyer_id"],
            "buyer_type": payload["buyer_type"],
            "zone": payload["zone"],
            "seat": {
                "row": int(seat["row"]),
                "col": int(seat["col"]),
            },
            "reservation_id": payload["reservation_id"],
            "request_id": payload["request_id"],
            "server_host": payload.get("server_host"),
            "server_port": payload.get("server_port"),
        }

        line = json.dumps(record, ensure_ascii=False)
        with self.lock:
            with self.store_file.open("a", encoding="utf-8") as file_handle:
                file_handle.write(line + "\n")
            self.ticket_count += 1
            stored_count = self.ticket_count

        return record, stored_count


class TicketAuthority:
    def __init__(self, ticket_store):
        self.ticket_store = ticket_store
        self.lock = threading.RLock()
        self.zone_free_seats = build_zone_seats()
        self.seat_status = [["FREE" for _ in range(COLUMNAS)] for _ in range(FILAS)]
        self.reservations = {}
        self.sales_open = False
        self.sales_closed = False
        self.close_reason = None
        self.sale_id = "default-sale"
        self.server_host = None
        self.server_port = None
        self.sold_count = 0
        self.expired_releases = 0

    def set_context(self, sale_id=None, server_host=None, server_port=None):
        with self.lock:
            if sale_id:
                self.sale_id = str(sale_id)
            self.server_host = server_host
            self.server_port = server_port

    def open_sales(self):
        with self.lock:
            if self.sales_closed:
                return {"status": "error", "code": "sale_closed"}
            self.sales_open = True
            return {"status": "ok", "sales_open": True}

    def close_sales(self, reason="manual_close"):
        with self.lock:
            for reservation in list(self.reservations.values()):
                row, col = reservation["seat"]
                zone = reservation["zone"]
                self.seat_status[row][col] = "FREE"
                self.zone_free_seats[zone].add((row, col))
            self.reservations.clear()
            self.sales_closed = True
            self.sales_open = False
            self.close_reason = reason
            return {"status": "ok", "sales_closed": True, "close_reason": reason}

    def _cleanup_expired_locked(self):
        now = time.monotonic()
        expired = [rid for rid, info in self.reservations.items() if info["expires_at"] <= now]
        for reservation_id in expired:
            info = self.reservations.pop(reservation_id)
            row, col = info["seat"]
            zone = info["zone"]
            self.seat_status[row][col] = "FREE"
            self.zone_free_seats[zone].add((row, col))
            self.expired_releases += 1

    def request_ticket(self, buyer_id, buyer_type, request_id, specific_row=None, specific_col=None):
        with self.lock:
            self._cleanup_expired_locked()

            if self.sales_closed:
                return {"status": "closed", "message": "La venta fue cerrada."}
            if not self.sales_open:
                return {"status": "not_started", "message": "La venta aún no inicia."}

            normalized_type = (buyer_type or TIPO_NORMAL).lower()
            if normalized_type not in ALLOWED_ZONES_BY_TYPE:
                normalized_type = TIPO_NORMAL
            zones = ALLOWED_ZONES_BY_TYPE[normalized_type]

            if specific_row is not None and specific_col is not None:
                try:
                    row = int(specific_row)
                    col = int(specific_col)
                except (TypeError, ValueError):
                    row = None
                    col = None

                if row is not None and col is not None and 0 <= row < FILAS and 0 <= col < COLUMNAS:
                    zone = zone_for_row(row)
                    if zone not in zones:
                        return {
                            "status": "error",
                            "code": "zone_not_allowed",
                            "message": f"Tu tipo de comprador no puede acceder a la zona {zone}.",
                        }
                    if self.seat_status[row][col] != "FREE":
                        return {
                            "status": "error",
                            "code": "seat_not_available",
                            "message": f"El asiento {row}-{col} no está disponible.",
                        }

                    reservation_id = str(uuid.uuid4())
                    self.seat_status[row][col] = "RESERVED"
                    self.zone_free_seats[zone].discard((row, col))
                    self.reservations[reservation_id] = {
                        "buyer_id": str(buyer_id),
                        "buyer_type": normalized_type,
                        "seat": (row, col),
                        "zone": zone,
                        "expires_at": time.monotonic() + RESERVA_TTL_SEGUNDOS,
                        "request_id": request_id,
                    }
                    return {
                        "status": "ok",
                        "reservation_id": reservation_id,
                        "zone": zone,
                        "seat": {"row": row, "col": col},
                        "ttl_seconds": RESERVA_TTL_SEGUNDOS,
                    }

            for zone in zones:
                if not self.zone_free_seats[zone]:
                    continue
                row, col = random.choice(tuple(self.zone_free_seats[zone]))
                reservation_id = str(uuid.uuid4())
                self.zone_free_seats[zone].remove((row, col))
                self.seat_status[row][col] = "RESERVED"
                self.reservations[reservation_id] = {
                    "buyer_id": str(buyer_id),
                    "buyer_type": normalized_type,
                    "seat": (row, col),
                    "zone": zone,
                    "expires_at": time.monotonic() + RESERVA_TTL_SEGUNDOS,
                    "request_id": request_id,
                }
                return {
                    "status": "ok",
                    "reservation_id": reservation_id,
                    "zone": zone,
                    "seat": {"row": row, "col": col},
                    "ttl_seconds": RESERVA_TTL_SEGUNDOS,
                }

            if self.sold_count >= TOTAL_ASIENTOS:
                self.sales_closed = True
                self.sales_open = False
                self.close_reason = "all_sold"
                return {"status": "sold_out", "message": "No hay asientos disponibles."}

            return {
                "status": "error",
                "code": "no_zone_available",
                "message": "No hay asientos disponibles para el tipo de comprador en este momento.",
            }

    def purchase(self, buyer_id, reservation_id, request_id):
        with self.lock:
            self._cleanup_expired_locked()

            if self.sales_closed:
                return {"status": "closed", "message": "La venta fue cerrada."}
            if not self.sales_open:
                return {"status": "not_started", "message": "La venta aún no inicia."}
            if not reservation_id:
                return {"status": "error", "code": "missing_reservation_id"}

            reservation = self.reservations.get(reservation_id)
            if reservation is None:
                return {"status": "error", "code": "invalid_or_expired_reservation"}
            if reservation["buyer_id"] != str(buyer_id):
                return {"status": "error", "code": "reservation_owner_mismatch"}

            row, col = reservation["seat"]
            zone = reservation["zone"]
            payload = {
                "sale_id": self.sale_id,
                "buyer_id": str(buyer_id),
                "buyer_type": reservation.get("buyer_type", TIPO_NORMAL),
                "zone": zone,
                "seat": {"row": row, "col": col},
                "reservation_id": reservation_id,
                "request_id": request_id,
                "server_host": self.server_host,
                "server_port": self.server_port,
            }

            try:
                ticket, stored_count = self.ticket_store.create_ticket(payload)
            except Exception as exc:
                return {
                    "status": "error",
                    "code": "ticket_generation_failed",
                    "message": f"No fue posible emitir el ticket: {exc}",
                }

            self.reservations.pop(reservation_id, None)
            self.seat_status[row][col] = "SOLD"
            self.sold_count += 1
            remaining = TOTAL_ASIENTOS - self.sold_count

            if self.sold_count >= TOTAL_ASIENTOS:
                self.sales_closed = True
                self.sales_open = False
                self.close_reason = "all_sold"

            return {
                "status": "ok",
                "reservation_id": reservation_id,
                "zone": zone,
                "seat": {"row": row, "col": col},
                "ticket_id": ticket["ticket_id"],
                "ticket": ticket,
                "stored_count": stored_count,
                "sold_count": self.sold_count,
                "remaining": remaining,
            }

    def release_reservation(self, buyer_id, reservation_id, request_id=None):
        with self.lock:
            self._cleanup_expired_locked()

            if self.sales_closed:
                return {"status": "closed", "message": "La venta fue cerrada."}
            if not reservation_id:
                return {"status": "error", "code": "missing_reservation_id"}

            reservation = self.reservations.get(reservation_id)
            if reservation is None:
                return {"status": "error", "code": "invalid_or_expired_reservation"}
            if buyer_id is not None and reservation["buyer_id"] != str(buyer_id):
                return {"status": "error", "code": "reservation_owner_mismatch"}

            row, col = reservation["seat"]
            zone = reservation["zone"]
            self.reservations.pop(reservation_id, None)
            self.seat_status[row][col] = "FREE"
            self.zone_free_seats[zone].add((row, col))

            return {
                "status": "ok",
                "reservation_id": reservation_id,
                "zone": zone,
                "seat": {"row": row, "col": col},
                "request_id": request_id,
            }

    def get_snapshot(self):
        with self.lock:
            self._cleanup_expired_locked()
            reserved_count = len(self.reservations)
            return {
                "sold_count": self.sold_count,
                "reserved_count": reserved_count,
                "free_count": TOTAL_ASIENTOS - self.sold_count - reserved_count,
                "seat_status": [row[:] for row in self.seat_status],
                "sales_open": self.sales_open,
                "sales_closed": self.sales_closed,
                "close_reason": self.close_reason,
                "ttl_seconds": RESERVA_TTL_SEGUNDOS,
            }

    def get_health(self):
        with self.lock:
            self._cleanup_expired_locked()
            return {
                "status": "ok",
                "total_seats": TOTAL_ASIENTOS,
                "sold_count": self.sold_count,
                "reserved_count": len(self.reservations),
                "sales_open": self.sales_open,
                "sales_closed": self.sales_closed,
                "close_reason": self.close_reason,
                "expired_releases": self.expired_releases,
            }


class TicketingServiceHandler(socketserver.StreamRequestHandler):
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

            if message_type == "CREATE_TICKET":
                try:
                    ticket, stored_count = self.server.ticket_store.create_ticket(payload)
                    self.send_json(
                        {
                            "type": "CREATE_TICKET_RESPONSE",
                            "status": "ok",
                            "ticket_id": ticket["ticket_id"],
                            "ticket": ticket,
                            "store_file": str(self.server.ticket_store.store_file),
                            "stored_count": stored_count,
                        }
                    )
                except ValueError as exc:
                    self.send_json(
                        {
                            "type": "CREATE_TICKET_RESPONSE",
                            "status": "error",
                            "code": "invalid_payload",
                            "message": str(exc),
                        }
                    )
                except Exception as exc:
                    self.send_json(
                        {
                            "type": "CREATE_TICKET_RESPONSE",
                            "status": "error",
                            "code": "storage_failed",
                            "message": f"No fue posible almacenar el ticket: {exc}",
                        }
                    )
                continue

            if message_type == "SET_CONTEXT":
                response = self.server.authority.set_context(
                    sale_id=payload.get("sale_id"),
                    server_host=payload.get("server_host"),
                    server_port=payload.get("server_port"),
                )
                self.send_json({"type": "SET_CONTEXT_RESPONSE", **(response or {"status": "ok"})})
                continue

            if message_type == "OPEN_SALES":
                response = self.server.authority.open_sales()
                self.send_json({"type": "OPEN_SALES_RESPONSE", **response})
                continue

            if message_type == "CLOSE_SALES":
                reason = payload.get("reason") or "manual_close"
                response = self.server.authority.close_sales(reason=reason)
                self.send_json({"type": "CLOSE_SALES_RESPONSE", **response})
                continue

            if message_type == "REQUEST_TICKET":
                response = self.server.authority.request_ticket(
                    buyer_id=payload.get("buyer_id"),
                    buyer_type=payload.get("buyer_type"),
                    request_id=payload.get("request_id") or str(uuid.uuid4()),
                    specific_row=payload.get("row"),
                    specific_col=payload.get("col"),
                )
                self.send_json({"type": "REQUEST_TICKET_RESPONSE", **response})
                continue

            if message_type == "PURCHASE":
                response = self.server.authority.purchase(
                    buyer_id=payload.get("buyer_id"),
                    reservation_id=payload.get("reservation_id"),
                    request_id=payload.get("request_id") or str(uuid.uuid4()),
                )
                self.send_json({"type": "PURCHASE_RESPONSE", **response})
                continue

            if message_type == "RELEASE_TICKET":
                response = self.server.authority.release_reservation(
                    buyer_id=payload.get("buyer_id"),
                    reservation_id=payload.get("reservation_id"),
                    request_id=payload.get("request_id") or str(uuid.uuid4()),
                )
                self.send_json({"type": "RELEASE_TICKET_RESPONSE", **response})
                continue

            if message_type == "AVAILABILITY":
                snapshot = self.server.authority.get_snapshot()
                self.send_json({"type": "AVAILABILITY_RESPONSE", "status": "ok", **snapshot})
                continue

            if message_type == "HEALTH":
                health = self.server.authority.get_health()
                self.send_json({"type": "HEALTH_RESPONSE", **health})
                continue

            self.send_json({"type": "ERROR", "code": "unknown_message_type"})


class TicketingServiceServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False

    def __init__(self, server_address, handler_class, ticket_store, authority):
        super().__init__(server_address, handler_class)
        self.ticket_store = ticket_store
        self.authority = authority


def cleanup_expired_loop(authority, stop_event):
    while not stop_event.is_set():
        with authority.lock:
            authority._cleanup_expired_locked()
        stop_event.wait(0.25)


def parse_args():
    parser = argparse.ArgumentParser(description="Ticketing Service para microservicios")
    parser.add_argument("--host", default="127.0.0.1", help="Host para escuchar conexiones")
    parser.add_argument("--port", type=int, default=7000, help="Puerto para escuchar conexiones")
    parser.add_argument("--store-file", default="tickets/tickets.txt", help="Archivo de almacenamiento de tickets")
    return parser.parse_args()


def main():
    args = parse_args()
    ticket_store = TicketStore(args.store_file)
    authority = TicketAuthority(ticket_store)
    server = TicketingServiceServer((args.host, args.port), TicketingServiceHandler, ticket_store, authority)

    stop_event = threading.Event()
    cleanup_thread = threading.Thread(target=cleanup_expired_loop, args=(authority, stop_event), daemon=True)
    cleanup_thread.start()

    print("Ticketing Service iniciado (autoridad de asientos)")
    print(f"Escuchando en {args.host}:{args.port}")
    print(f"Archivo de tickets: {ticket_store.store_file}")
    print(f"TTL de reserva: {RESERVA_TTL_SEGUNDOS:.0f} segundos")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Ticketing Service] Interrupción recibida. Cerrando servicio...")
    finally:
        stop_event.set()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()