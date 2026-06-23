# Practica 8 - Microservicios

import argparse
import json
import socket
import socketserver
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from flask import Flask, request, jsonify
except Exception:
    Flask = None


TIPO_NORMAL = "normal"
TICKET_SERVICE_TIMEOUT = 4.0


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


class TicketingAuthorityClient:
    def __init__(self, host, port, timeout=TICKET_SERVICE_TIMEOUT):
        self.host = host
        self.port = port
        self.timeout = timeout

    def _send(self, payload, expected_type=None):
        response = send_json_request(self.host, self.port, payload, timeout=self.timeout)
        if expected_type and (response.get("type") or "").upper() != expected_type.upper():
            raise RuntimeError(f"Respuesta inesperada del ticketing service: {response}")
        return response

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


class TicketServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False

    def __init__(self, server_address, handler_class, authority_client, expected_clients, sale_id):
        super().__init__(server_address, handler_class)
        self.authority_client = authority_client
        self.expected_clients = expected_clients
        self.sale_id = sale_id

        self.registration_lock = threading.Lock()
        self.terminal_lock = threading.Lock()

        self.connected_clients = {}
        self.ready_clients = set()
        self.done_clients = set()

        self.start_event = threading.Event()
        self.all_ready_event = threading.Event()

        self.sale_open_event = threading.Event()
        self.sale_closed_event = threading.Event()
        self.close_reason = None
        self.shutdown_requested = threading.Event()

        self.countdown_started_at = None
        self.countdown_duration = 0.0

    def begin_countdown(self, duration_seconds=5.0):
        with self.registration_lock:
            self.countdown_started_at = time.perf_counter()
            self.countdown_duration = float(duration_seconds)

    def get_sale_status(self, remote_snapshot=None):
        with self.registration_lock:
            countdown_started_at = self.countdown_started_at
            countdown_duration = self.countdown_duration
            connected_clients = len(self.connected_clients)
            ready_clients = len(self.ready_clients)
            done_clients = len(self.done_clients)

        sales_open = self.sale_open_event.is_set()
        sales_closed = self.sale_closed_event.is_set()

        if remote_snapshot:
            sales_open = bool(remote_snapshot.get("sales_open", sales_open))
            sales_closed = bool(remote_snapshot.get("sales_closed", sales_closed))
            if remote_snapshot.get("close_reason"):
                self.close_reason = remote_snapshot.get("close_reason")

        if sales_closed:
            state = "closed"
        elif sales_open:
            state = "open"
        elif countdown_started_at is not None:
            state = "countdown"
        else:
            state = "waiting"

        countdown_remaining = None
        if state == "countdown":
            elapsed = time.perf_counter() - countdown_started_at
            countdown_remaining = max(0.0, countdown_duration - elapsed)

        return {
            "state": state,
            "sales_open": sales_open,
            "sales_closed": sales_closed,
            "close_reason": self.close_reason,
            "countdown_started": countdown_started_at is not None,
            "countdown_duration": countdown_duration,
            "countdown_remaining": countdown_remaining,
            "connected_clients": connected_clients,
            "ready_clients": ready_clients,
            "done_clients": done_clients,
            "expected_clients": self.expected_clients,
        }

    def register_client(self, client_id, client_type, buyers_count):
        with self.registration_lock:
            self.connected_clients[client_id] = {
                "client_type": client_type,
                "buyers": buyers_count,
                "connected_at": time.strftime("%H:%M:%S"),
            }
            connected = len(self.connected_clients)

        return connected

    def mark_ready(self, client_id):
        all_ready = False
        with self.registration_lock:
            self.ready_clients.add(client_id)
            ready_count = len(self.ready_clients)
            connected_count = len(self.connected_clients)

            if (
                not self.all_ready_event.is_set()
                and connected_count >= self.expected_clients
                and ready_count >= self.expected_clients
            ):
                all_ready = True
                self.all_ready_event.set()

        if all_ready:
            with self.terminal_lock:
                print("Todos los clientes esperados están listos localmente.")

        return ready_count

    def trigger_start(self):
        with self.registration_lock:
            if self.start_event.is_set():
                return
            self.start_event.set()
            self.countdown_started_at = None

        response = self.authority_client.open_sales()
        if (response.get("status") or "").lower() == "ok":
            self.sale_open_event.set()
            with self.terminal_lock:
                print("Señal START enviada. ¡Venta abierta!")
            return

        raise RuntimeError(f"No se pudo abrir la venta en ticketing service: {response}")

    def mark_client_done(self, client_id):
        with self.registration_lock:
            self.done_clients.add(client_id)
            done_count = len(self.done_clients)
        with self.terminal_lock:
            print(f"Cliente {client_id} reportó fin de ejecución ({done_count}/{self.expected_clients})")
        if done_count >= self.expected_clients:
            self.close_sale("all_clients_done")
        return done_count

    def disconnect_client(self, client_id):
        with self.registration_lock:
            was_connected = client_id in self.connected_clients
            self.connected_clients.pop(client_id, None)
            self.ready_clients.discard(client_id)
            connected_count = len(self.connected_clients)
        if was_connected:
            with self.terminal_lock:
                print(f"Cliente {client_id} cerró conexión ({connected_count}/{self.expected_clients} conectados)")
        self._maybe_shutdown_if_finished()
        return connected_count

    def _maybe_shutdown_if_finished(self):
        should_shutdown = False
        with self.registration_lock:
            if (
                self.sale_closed_event.is_set()
                and len(self.connected_clients) == 0
                and len(self.done_clients) >= self.expected_clients
                and not self.shutdown_requested.is_set()
            ):
                self.shutdown_requested.set()
                should_shutdown = True

        if should_shutdown:
            with self.terminal_lock:
                print("Todos los clientes cerraron conexión. El servidor se apagará automáticamente.")

            def _shutdown_later():
                time.sleep(0.5)
                try:
                    self.shutdown()
                except Exception:
                    pass

            threading.Thread(target=_shutdown_later, daemon=True).start()

    def close_sale(self, reason):
        if self.sale_closed_event.is_set():
            return {"status": "ok", "close_reason": self.close_reason or reason}
        try:
            self.authority_client.close_sales(reason)
        except Exception:
            pass
        self.sale_open_event.clear()
        self.sale_closed_event.set()
        self.close_reason = reason
        self._maybe_shutdown_if_finished()
        return {"status": "ok", "close_reason": reason}


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
            request_id = payload.get("request_id") or str(uuid.uuid4())

            if message_type == "REGISTER":
                client_id = payload.get("client_id")
                client_type = (payload.get("client_type") or "").lower()
                buyers_count = int(payload.get("buyers", 0))
                if not client_id:
                    self.send_json({"type": "ERROR", "code": "missing_client_id"})
                    continue
                connected = self.server.register_client(client_id, client_type, buyers_count)
                self.send_json(
                    {
                        "type": "REGISTERED",
                        "client_id": client_id,
                        "connected_clients": connected,
                        "expected_clients": self.server.expected_clients,
                    }
                )
                continue

            if message_type == "READY":
                client_id = payload.get("client_id")
                if not client_id:
                    self.send_json({"type": "ERROR", "code": "missing_client_id"})
                    continue
                ready_count = self.server.mark_ready(client_id)
                self.server.start_event.wait()
                self.send_json(
                    {
                        "type": "START",
                        "client_id": client_id,
                        "ready_clients": ready_count,
                        "expected_clients": self.server.expected_clients,
                    }
                )
                continue

            if message_type == "REQUEST_TICKET":
                try:
                    response = self.server.authority_client.request_ticket(
                        buyer_id=payload.get("buyer_id"),
                        buyer_type=payload.get("buyer_type", TIPO_NORMAL),
                        request_id=request_id,
                        row=payload.get("row"),
                        col=payload.get("col"),
                    )
                    self.send_json(response)
                except Exception as exc:
                    self.send_json({"type": "REQUEST_TICKET_RESPONSE", "status": "error", "code": "gateway_error", "message": str(exc)})
                continue

            if message_type == "PURCHASE":
                try:
                    response = self.server.authority_client.purchase(
                        buyer_id=payload.get("buyer_id"),
                        reservation_id=payload.get("reservation_id"),
                        request_id=request_id,
                    )
                    self.send_json(response)
                except Exception as exc:
                    self.send_json({"type": "PURCHASE_RESPONSE", "status": "error", "code": "gateway_error", "message": str(exc)})
                continue

            if message_type == "HEALTH":
                try:
                    remote = self.server.authority_client.health()
                    remote.update(
                        {
                            "connected_clients": len(self.server.connected_clients),
                            "ready_clients": len(self.server.ready_clients),
                            "done_clients": len(self.server.done_clients),
                            "expected_clients": self.server.expected_clients,
                        }
                    )
                    self.send_json(remote)
                except Exception as exc:
                    self.send_json({"type": "HEALTH_RESPONSE", "status": "error", "code": "gateway_error", "message": str(exc)})
                continue

            if message_type == "CLIENT_DONE":
                client_id = payload.get("client_id")
                if not client_id:
                    self.send_json({"type": "ERROR", "code": "missing_client_id"})
                    continue
                done_count = self.server.mark_client_done(client_id)
                self.send_json(
                    {
                        "type": "DONE_ACK",
                        "client_id": client_id,
                        "done_clients": done_count,
                        "expected_clients": self.server.expected_clients,
                        "sale_status": self.server.get_sale_status(),
                    }
                )
                continue

            if message_type == "CLIENT_DISCONNECT":
                client_id = payload.get("client_id")
                if not client_id:
                    self.send_json({"type": "ERROR", "code": "missing_client_id"})
                    continue
                connected_count = self.server.disconnect_client(client_id)
                self.send_json(
                    {
                        "type": "DISCONNECT_ACK",
                        "client_id": client_id,
                        "connected_clients": connected_count,
                        "expected_clients": self.server.expected_clients,
                        "sale_status": self.server.get_sale_status(),
                    }
                )
                continue

            self.send_json({"type": "ERROR", "code": "unknown_message_type"})


def parse_args():
    parser = argparse.ArgumentParser(description="Servidor gateway de boletos")
    parser.add_argument("expected_clients", type=int, help="Cantidad de clientes que deben conectarse antes de iniciar")
    parser.add_argument("--host", default="127.0.0.1", help="Host para escuchar conexiones")
    parser.add_argument("--port", type=int, default=5000, help="Puerto para escuchar conexiones")
    parser.add_argument("--no-gui", action="store_true", help="Compatibilidad: el gateway opera sin GUI")
    parser.add_argument("--sale-id", default=None, help="Identificador de esta venta/servidor")
    parser.add_argument("--ticket-service-host", default="127.0.0.1", help="Host del Ticketing Service")
    parser.add_argument("--ticket-service-port", type=int, default=7000, help="Puerto del Ticketing Service")
    return parser.parse_args()


def create_api(server):
    if Flask is None:
        return None
    app = Flask(__name__)

    @app.after_request
    def add_cors(resp):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
        return resp

    @app.route("/api/availability", methods=["GET"])
    def api_availability():
        try:
            snap = server.authority_client.availability()
            snap["sale_status"] = server.get_sale_status(remote_snapshot=snap)
            return jsonify(snap)
        except Exception as exc:
            return jsonify({"status": "error", "code": "gateway_error", "message": str(exc), "sale_status": server.get_sale_status()}), 503

    @app.route("/api/register_client", methods=["POST"])
    def api_register_client():
        data = request.get_json() or {}
        client_id = data.get("client_id")
        client_type = data.get("client_type", TIPO_NORMAL)
        buyers = int(data.get("buyers", 1))
        if not client_id:
            return jsonify({"type": "ERROR", "code": "missing_client_id"}), 400
        connected = server.register_client(client_id, client_type, buyers)
        return jsonify(
            {
                "type": "REGISTERED",
                "client_id": client_id,
                "connected_clients": connected,
                "expected_clients": server.expected_clients,
            }
        )

    @app.route("/api/ready", methods=["POST"])
    def api_ready():
        data = request.get_json() or {}
        client_id = data.get("client_id")
        if not client_id:
            return jsonify({"type": "ERROR", "code": "missing_client_id"}), 400
        ready_count = server.mark_ready(client_id)
        return jsonify(
            {
                "type": "START_ACK",
                "client_id": client_id,
                "ready_clients": ready_count,
                "expected_clients": server.expected_clients,
            }
        )

    @app.route("/api/request_ticket", methods=["POST"])
    def api_request_ticket():
        data = request.get_json() or {}
        request_id = data.get("request_id") or str(uuid.uuid4())
        try:
            response = server.authority_client.request_ticket(
                buyer_id=data.get("buyer_id"),
                buyer_type=data.get("buyer_type", TIPO_NORMAL),
                request_id=request_id,
                row=data.get("row"),
                col=data.get("col"),
            )
            return jsonify(response)
        except Exception as exc:
            return jsonify({"type": "REQUEST_TICKET_RESPONSE", "status": "error", "code": "gateway_error", "message": str(exc)}), 503

    @app.route("/api/purchase", methods=["POST"])
    def api_purchase():
        data = request.get_json() or {}
        request_id = data.get("request_id") or str(uuid.uuid4())
        try:
            response = server.authority_client.purchase(
                buyer_id=data.get("buyer_id"),
                reservation_id=data.get("reservation_id"),
                request_id=request_id,
            )
            return jsonify(response)
        except Exception as exc:
            return jsonify({"type": "PURCHASE_RESPONSE", "status": "error", "code": "gateway_error", "message": str(exc)}), 503

    @app.route("/api/release_ticket", methods=["POST"])
    def api_release_ticket():
        data = request.get_json() or {}
        request_id = data.get("request_id") or str(uuid.uuid4())
        try:
            response = server.authority_client.release_reservation(
                buyer_id=data.get("buyer_id"),
                reservation_id=data.get("reservation_id"),
                request_id=request_id,
            )
            return jsonify(response)
        except Exception as exc:
            return jsonify({"type": "RELEASE_TICKET_RESPONSE", "status": "error", "code": "gateway_error", "message": str(exc)}), 503

    @app.route("/api/client_done", methods=["POST"])
    def api_client_done():
        data = request.get_json() or {}
        client_id = data.get("client_id")
        if not client_id:
            return jsonify({"type": "ERROR", "code": "missing_client_id"}), 400
        done_count = server.mark_client_done(client_id)
        return jsonify(
            {
                "type": "DONE_ACK",
                "client_id": client_id,
                "done_clients": done_count,
                "expected_clients": server.expected_clients,
                "sale_status": server.get_sale_status(),
            }
        )

    @app.route("/api/client_disconnect", methods=["POST"])
    def api_client_disconnect():
        data = request.get_json() or {}
        client_id = data.get("client_id")
        if not client_id:
            return jsonify({"type": "ERROR", "code": "missing_client_id"}), 400
        connected_count = server.disconnect_client(client_id)
        return jsonify(
            {
                "type": "DISCONNECT_ACK",
                "client_id": client_id,
                "connected_clients": connected_count,
                "expected_clients": server.expected_clients,
                "sale_status": server.get_sale_status(),
            }
        )

    @app.route("/api/close_sale", methods=["POST"])
    def api_close_sale():
        data = request.get_json() or {}
        reason = data.get("reason") or "manual_close"
        try:
            response = server.close_sale(reason)
            return jsonify({"type": "CLOSE_SALE_RESPONSE", "status": "ok", "sale_status": server.get_sale_status(), **response})
        except Exception as exc:
            return jsonify({"type": "CLOSE_SALE_RESPONSE", "status": "error", "code": "gateway_error", "message": str(exc)}), 503

    return app


def create_builtin_http_api(server, host="127.0.0.1", port=5001):
    class GatewayHttpHandler(BaseHTTPRequestHandler):
        def _send_json(self, payload, status=200):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            if not raw:
                return {}
            try:
                return json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                return {}

        def do_OPTIONS(self):
            self._send_json({}, status=204)

        def do_GET(self):
            if self.path != "/api/availability":
                self._send_json({"status": "error", "code": "not_found"}, status=404)
                return
            try:
                snap = server.authority_client.availability()
                snap["sale_status"] = server.get_sale_status(remote_snapshot=snap)
                self._send_json(snap)
            except Exception as exc:
                self._send_json(
                    {
                        "status": "error",
                        "code": "gateway_error",
                        "message": str(exc),
                        "sale_status": server.get_sale_status(),
                    },
                    status=503,
                )

        def do_POST(self):
            data = self._read_json()
            if self.path == "/api/register_client":
                client_id = data.get("client_id")
                client_type = data.get("client_type", TIPO_NORMAL)
                buyers = int(data.get("buyers", 1))
                if not client_id:
                    self._send_json({"type": "ERROR", "code": "missing_client_id"}, status=400)
                    return
                connected = server.register_client(client_id, client_type, buyers)
                self._send_json(
                    {
                        "type": "REGISTERED",
                        "client_id": client_id,
                        "connected_clients": connected,
                        "expected_clients": server.expected_clients,
                    }
                )
                return

            if self.path == "/api/ready":
                client_id = data.get("client_id")
                if not client_id:
                    self._send_json({"type": "ERROR", "code": "missing_client_id"}, status=400)
                    return
                ready_count = server.mark_ready(client_id)
                self._send_json(
                    {
                        "type": "START_ACK",
                        "client_id": client_id,
                        "ready_clients": ready_count,
                        "expected_clients": server.expected_clients,
                    }
                )
                return

            if self.path == "/api/request_ticket":
                request_id = data.get("request_id") or str(uuid.uuid4())
                try:
                    response = server.authority_client.request_ticket(
                        buyer_id=data.get("buyer_id"),
                        buyer_type=data.get("buyer_type", TIPO_NORMAL),
                        request_id=request_id,
                        row=data.get("row"),
                        col=data.get("col"),
                    )
                    self._send_json(response)
                except Exception as exc:
                    self._send_json(
                        {
                            "type": "REQUEST_TICKET_RESPONSE",
                            "status": "error",
                            "code": "gateway_error",
                            "message": str(exc),
                        },
                        status=503,
                    )
                return

            if self.path == "/api/purchase":
                request_id = data.get("request_id") or str(uuid.uuid4())
                try:
                    response = server.authority_client.purchase(
                        buyer_id=data.get("buyer_id"),
                        reservation_id=data.get("reservation_id"),
                        request_id=request_id,
                    )
                    self._send_json(response)
                except Exception as exc:
                    self._send_json(
                        {
                            "type": "PURCHASE_RESPONSE",
                            "status": "error",
                            "code": "gateway_error",
                            "message": str(exc),
                        },
                        status=503,
                    )
                return

            if self.path == "/api/release_ticket":
                request_id = data.get("request_id") or str(uuid.uuid4())
                try:
                    response = server.authority_client.release_reservation(
                        buyer_id=data.get("buyer_id"),
                        reservation_id=data.get("reservation_id"),
                        request_id=request_id,
                    )
                    self._send_json(response)
                except Exception as exc:
                    self._send_json(
                        {
                            "type": "RELEASE_TICKET_RESPONSE",
                            "status": "error",
                            "code": "gateway_error",
                            "message": str(exc),
                        },
                        status=503,
                    )
                return

            if self.path == "/api/client_done":
                client_id = data.get("client_id")
                if not client_id:
                    self._send_json({"type": "ERROR", "code": "missing_client_id"}, status=400)
                    return
                done_count = server.mark_client_done(client_id)
                self._send_json(
                    {
                        "type": "DONE_ACK",
                        "client_id": client_id,
                        "done_clients": done_count,
                        "expected_clients": server.expected_clients,
                        "sale_status": server.get_sale_status(),
                    }
                )
                return

            if self.path == "/api/client_disconnect":
                client_id = data.get("client_id")
                if not client_id:
                    self._send_json({"type": "ERROR", "code": "missing_client_id"}, status=400)
                    return
                connected_count = server.disconnect_client(client_id)
                self._send_json(
                    {
                        "type": "DISCONNECT_ACK",
                        "client_id": client_id,
                        "connected_clients": connected_count,
                        "expected_clients": server.expected_clients,
                        "sale_status": server.get_sale_status(),
                    }
                )
                return

            if self.path == "/api/close_sale":
                reason = data.get("reason") or "manual_close"
                try:
                    response = server.close_sale(reason)
                    self._send_json({"type": "CLOSE_SALE_RESPONSE", "status": "ok", "sale_status": server.get_sale_status(), **response})
                except Exception as exc:
                    self._send_json(
                        {
                            "type": "CLOSE_SALE_RESPONSE",
                            "status": "error",
                            "code": "gateway_error",
                            "message": str(exc),
                        },
                        status=503,
                    )
                return

            self._send_json({"status": "error", "code": "not_found"}, status=404)

        def log_message(self, fmt, *args):
            return

    http_server = ThreadingHTTPServer((host, port), GatewayHttpHandler)

    def _run():
        http_server.serve_forever()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    print(f"[API] HTTP API nativa running on {host}:{port}")
    return thread, http_server


def run_api_thread(app, server, host="127.0.0.1", port=5001):
    if app is None:
        return create_builtin_http_api(server, host=host, port=port)

    def _run():
        app.run(host=host, port=port, threaded=True)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    print(f"[API] HTTP API running on {host}:{port}")
    return thread, None


def main():
    args = parse_args()
    if args.expected_clients <= 0:
        raise ValueError("expected_clients debe ser mayor que 0")

    expected_clients = args.expected_clients
    sale_id = args.sale_id or f"{args.host}:{args.port}"

    authority_client = TicketingAuthorityClient(args.ticket_service_host, args.ticket_service_port)
    authority_client.set_context(sale_id=sale_id, server_host=args.host, server_port=args.port)

    server = TicketServer(
        (args.host, args.port),
        TicketRequestHandler,
        authority_client,
        expected_clients,
        sale_id,
    )

    api_app = create_api(server)
    api_thread, builtin_http_server = run_api_thread(api_app, server, host=args.host, port=5001)

    def wait_and_start():
        server.all_ready_event.wait()
        server.begin_countdown(5.0)
        for i in range(5, 0, -1):
            print(f"  Iniciando en {i}...")
            time.sleep(1)
        server.trigger_start()

    countdown_thread = threading.Thread(target=wait_and_start, daemon=True)
    countdown_thread.start()

    print("Servidor gateway iniciado")
    print(f"Escuchando en {args.host}:{args.port}")
    print(f"Sale ID: {sale_id}")
    print(f"Clientes esperados para iniciar: {expected_clients}")
    print(f"Ticketing Service: {args.ticket_service_host}:{args.ticket_service_port}")
    if not args.no_gui:
        print("[Gateway] Modo GUI eliminado en microservicios; ejecutando en modo headless.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Servidor] Interrupción recibida. Cerrando servidor...")
    finally:
        server.close_sale("interrupted")
        server.shutdown()
        server.server_close()
        if builtin_http_server is not None:
            builtin_http_server.shutdown()
            builtin_http_server.server_close()


if __name__ == "__main__":
    main()