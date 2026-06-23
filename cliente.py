# Practica 7 - PWA

import argparse
import json
import random
import socket
import threading
import time
import uuid

SOCKET_TIMEOUT = 4.0
HTTP_TIMEOUT = 5.0
USE_HTTP = False
API_PORT = 5001

TIPO_PLATINO = "platino"
TIPO_PREFERENTE = "preferente"
TIPO_NORMAL = "normal"

TYPE_ALIAS = {
    "a": TIPO_PLATINO,
    "b": TIPO_NORMAL,
    "c": TIPO_PREFERENTE,
    TIPO_PLATINO: TIPO_PLATINO,
    TIPO_PREFERENTE: TIPO_PREFERENTE,
    TIPO_NORMAL: TIPO_NORMAL,
}


stats_lock = threading.Lock()
terminal_lock = threading.Lock()
sold_out_event = threading.Event()
threads = []

metrics = {
    "buyers_success": 0,
    "buyers_fail": 0,
    "buyer_total_success": 0.0,
    "buyer_search_success": 0.0,
    "buyer_purchase_success": 0.0,
    "buyer_wait_success": 0.0,
    "buyer_total_fail": 0.0,
    "buyer_wait_fail": 0.0,
    "request_time_total": 0.0,
    "purchase_time_total": 0.0,
    "request_count": 0,
    "purchase_count": 0,
    "attempts_success": 0,
    "attempts_fail": 0,
    "network_errors": 0,
}

sales_start_ts = None
sales_end_ts = None


def monitor_server_health(host, port):
    # Monitorea cierre/agotamiento para cortar todos los hilos clientes de forma limpia.
    while not sold_out_event.is_set():
        try:
            if USE_HTTP:
                # use availability endpoint to detect closure
                from urllib.request import urlopen, Request
                from urllib.error import URLError

                req = Request(f"http://{host}:{API_PORT}/api/availability", method="GET")
                try:
                    with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                        response = {"type": "AVAILABILITY_RESPONSE", **data}
                except Exception:
                    response = {}
            else:
                response = send_request(
                    host,
                    port,
                    {
                        "type": "HEALTH",
                        "request_id": str(uuid.uuid4()),
                    },
                )

            if response.get("type") == "HEALTH_RESPONSE":
                if response.get("sales_closed"):
                    sold_out_event.set()
                    return

                total_seats = response.get("total_seats")
                sold_count = response.get("sold_count", 0)
                if isinstance(total_seats, int) and total_seats > 0 and sold_count >= total_seats:
                    sold_out_event.set()
                    return
        except Exception:
            # Si falla la consulta puntual, se reintenta en el siguiente ciclo.
            pass

        time.sleep(0.35)


def avg(total, count):
    if count == 0:
        return 0.0
    return total / count


def send_request(host, port, payload):
    data = (json.dumps(payload) + "\n").encode("utf-8")
    with socket.create_connection((host, port), timeout=SOCKET_TIMEOUT) as sock:
        sock.settimeout(SOCKET_TIMEOUT)
        sock.sendall(data)
        response_bytes = b""
        while not response_bytes.endswith(b"\n"):
            chunk = sock.recv(4096)
            if not chunk:
                break
            response_bytes += chunk

    if not response_bytes:
        raise ConnectionError("Respuesta vacía del servidor")

    return json.loads(response_bytes.decode("utf-8").strip())


def http_post(host, port, path, payload):
    from urllib.request import Request, urlopen
    from urllib.error import URLError, HTTPError

    url = f"http://{host}:{port}{path}"
    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError) as e:
        raise


def http_get(host, port, path):
    from urllib.request import Request, urlopen
    from urllib.error import URLError, HTTPError

    url = f"http://{host}:{port}{path}"
    req = Request(url, method="GET")
    try:
        with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError) as e:
        raise



def send_control_message(sock_file, payload):
    sock_file.write((json.dumps(payload) + "\n").encode("utf-8"))
    sock_file.flush()


def read_control_message(sock_file):
    raw_line = sock_file.readline()
    if not raw_line:
        raise ConnectionError("Conexión de control cerrada por servidor")
    return json.loads(raw_line.decode("utf-8").strip())


def register_and_wait_start(host, port, client_id, client_type, buyers_count):
    if USE_HTTP:
        # Register via HTTP API and wait for sale to open
        try:
            reg_payload = {"client_id": client_id, "client_type": client_type, "buyers": buyers_count}
            resp = http_post(host, API_PORT, "/api/register_client", reg_payload)
            with terminal_lock:
                print(f"Registrado via HTTP como {client_id}. {resp.get('connected_clients')}/{resp.get('expected_clients')}")
        except Exception as exc:
            raise RuntimeError(f"Registro HTTP falló: {exc}")

        try:
            ready_payload = {"client_id": client_id}
            resp = http_post(host, API_PORT, "/api/ready", ready_payload)
            with terminal_lock:
                print(f"READY enviado via HTTP. {resp.get('ready_clients')}/{resp.get('expected_clients')}")
        except Exception as exc:
            raise RuntimeError(f"READY HTTP falló: {exc}")

        # Poll availability until sale opens
        for _ in range(120):
            try:
                av = http_get(host, API_PORT, "/api/availability")
                sale_status = av.get("sale_status") or {}
                state = sale_status.get("state")
                if state == "open" or sale_status.get("sales_open"):
                    with terminal_lock:
                        print("START recibido (HTTP): venta abierta")
                    return
            except Exception:
                pass
            time.sleep(0.5)
        raise RuntimeError("Timeout esperando START (HTTP)")

    # socket mode (original behavior)
    with socket.create_connection((host, port), timeout=SOCKET_TIMEOUT) as sock:
        sock.settimeout(None)
        with sock.makefile("rwb") as sock_file:

            send_control_message(
                sock_file,
                {
                    "type": "REGISTER",
                    "request_id": str(uuid.uuid4()),
                    "client_id": client_id,
                    "client_type": client_type,
                    "buyers": buyers_count,
                },
            )
            response = read_control_message(sock_file)
            if response.get("type") != "REGISTERED":
                raise RuntimeError(f"Registro rechazado: {response}")

            with terminal_lock:
                print(
                    f"Registrado como {client_id}. "
                    f"Conectados: {response.get('connected_clients')}/{response.get('expected_clients')}"
                )

            send_control_message(
                sock_file,
                {
                    "type": "READY",
                    "request_id": str(uuid.uuid4()),
                    "client_id": client_id,
                },
            )

            with terminal_lock:
                print("Esperando señal START del servidor...")

            start_response = read_control_message(sock_file)
            if start_response.get("type") != "START":
                raise RuntimeError(f"Se esperaba START y llegó: {start_response}")

            with terminal_lock:
                print(
                    f"START recibido. "
                    f"Clientes listos: {start_response.get('ready_clients')}/{start_response.get('expected_clients')}"
                )


def notify_client_done(host, port, client_id):
    try:
        if USE_HTTP:
            # no HTTP endpoint for CLIENT_DONE; log and return
            with terminal_lock:
                print("CLIENT_DONE (HTTP mode): skipping socket notification")
            return

        response = send_request(
            host,
            port,
            {
                "type": "CLIENT_DONE",
                "request_id": str(uuid.uuid4()),
                "client_id": client_id,
            },
        )

        with terminal_lock:
            print(
                f"CLIENT_DONE enviado. "
                f"Clientes finalizados: {response.get('done_clients')}/{response.get('expected_clients')}"
            )
    except Exception:
        with stats_lock:
            metrics["network_errors"] += 1


def buyer_worker(local_buyer_number, host, port, client_id, client_type):
    buyer_id = f"{client_id}-B{local_buyer_number}"
    buyer_started = time.perf_counter()
    wait_random_accum = 0.0
    request_time_accum = 0.0
    purchase_time_accum = 0.0
    attempts = 0
    purchased = False
    consecutive_network_errors = 0

    while not sold_out_event.is_set():
        pause = random.uniform(0.1, 2.0)
        time.sleep(pause)
        wait_random_accum += pause

        request_payload = {
            "type": "REQUEST_TICKET",
            "request_id": str(uuid.uuid4()),
            "buyer_id": buyer_id,
            "buyer_type": client_type,
        }

        attempts += 1
        request_started = time.perf_counter()
        try:
            if USE_HTTP:
                request_response = http_post(host, API_PORT, '/api/request_ticket', request_payload)
            else:
                request_response = send_request(host, port, request_payload)
            consecutive_network_errors = 0
        except Exception:
            with stats_lock:
                metrics["network_errors"] += 1
            consecutive_network_errors += 1
            if consecutive_network_errors >= 25:
                break
            continue

        request_elapsed = time.perf_counter() - request_started
        request_time_accum += request_elapsed

        with stats_lock:
            metrics["request_count"] += 1
            metrics["request_time_total"] += request_elapsed

        request_status = request_response.get("status")
        if request_status == "closed":
            sold_out_event.set()
            break
        if request_status == "not_started":
            time.sleep(0.15)
            continue
        if request_status == "sold_out":
            sold_out_event.set()
            break
        if request_status != "ok":
            if request_status == "error" and request_response.get("code") == "no_zone_available":
                time.sleep(0.12)
            continue

        reservation_id = request_response.get("reservation_id")
        if not reservation_id:
            continue

        payment_pause = random.uniform(1.0, 5.0)
        time.sleep(payment_pause)
        wait_random_accum += payment_pause

        purchase_payload = {
            "type": "PURCHASE",
            "request_id": str(uuid.uuid4()),
            "buyer_id": buyer_id,
            "reservation_id": reservation_id,
        }

        purchase_started = time.perf_counter()
        try:
            if USE_HTTP:
                purchase_response = http_post(host, API_PORT, '/api/purchase', purchase_payload)
            else:
                purchase_response = send_request(host, port, purchase_payload)
            consecutive_network_errors = 0
        except Exception:
            with stats_lock:
                metrics["network_errors"] += 1
            consecutive_network_errors += 1
            if consecutive_network_errors >= 25:
                break
            continue

        purchase_elapsed = time.perf_counter() - purchase_started
        purchase_time_accum += purchase_elapsed

        with stats_lock:
            metrics["purchase_count"] += 1
            metrics["purchase_time_total"] += purchase_elapsed

        purchase_status = purchase_response.get("status")
        if purchase_status == "closed":
            sold_out_event.set()
            break
        if purchase_status == "not_started":
            time.sleep(0.15)
            continue
        if purchase_status == "sold_out":
            sold_out_event.set()
            break
        if purchase_status == "ok":
            purchased = True
            if purchase_response.get("remaining", 1) <= 0:
                sold_out_event.set()
            break

    buyer_total_elapsed = time.perf_counter() - buyer_started
    buyer_search_elapsed = wait_random_accum + request_time_accum

    with stats_lock:
        if purchased:
            metrics["buyers_success"] += 1
            metrics["buyer_total_success"] += buyer_total_elapsed
            metrics["buyer_search_success"] += buyer_search_elapsed
            metrics["buyer_purchase_success"] += purchase_time_accum
            metrics["buyer_wait_success"] += wait_random_accum
            metrics["attempts_success"] += attempts
        else:
            metrics["buyers_fail"] += 1
            metrics["buyer_total_fail"] += buyer_total_elapsed
            metrics["buyer_wait_fail"] += wait_random_accum
            metrics["attempts_fail"] += attempts


def print_summary(client_id, client_type, buyers_count):
    success = metrics["buyers_success"]
    fail = metrics["buyers_fail"]

    with terminal_lock:
        print("\n========== Resumen del Punto de Acceso ==========")
        print(f"Cliente: {client_id}")
        print(f"Tipo de compradores: {client_type}")
        print(f"Compradores creados en este cliente: {buyers_count}")
        print(f"Compradores con compra: {success}")
        print(f"Compradores sin compra: {fail}")
        print(f"Tiempo total local: {sales_end_ts - sales_start_ts:.4f} s")
        print(f"Promedio total por comprador exitoso: {avg(metrics['buyer_total_success'], success):.4f} s")
        print(f"Promedio de búsqueda por comprador exitoso: {avg(metrics['buyer_search_success'], success):.4f} s")
        print(f"Promedio de compra por comprador exitoso: {avg(metrics['buyer_purchase_success'], success):.6f} s")
        print(f"Promedio de espera aleatoria por comprador exitoso: {avg(metrics['buyer_wait_success'], success):.4f} s")
        print(f"Promedio total por comprador sin compra: {avg(metrics['buyer_total_fail'], fail):.4f} s")
        print(f"Promedio de espera aleatoria por comprador sin compra: {avg(metrics['buyer_wait_fail'], fail):.4f} s")
        print(f"Promedio de intentos por comprador exitoso: {avg(metrics['attempts_success'], success):.2f}")
        print(f"Promedio de intentos por comprador sin compra: {avg(metrics['attempts_fail'], fail):.2f}")
        print(f"Tiempo promedio request_ticket (red + servidor): {avg(metrics['request_time_total'], metrics['request_count']):.6f} s")
        print(f"Tiempo promedio purchase (red + servidor): {avg(metrics['purchase_time_total'], metrics['purchase_count']):.6f} s")
        print(f"Errores de red detectados: {metrics['network_errors']}")
        print("===============================================\n")


def parse_args():
    parser = argparse.ArgumentParser(description="Cliente - Punto de acceso múltiple")
    parser.add_argument("client_type", help="Tipo de cliente: A/B/C o normal/preferente/platino")
    parser.add_argument("buyers", type=int, help="Cantidad de compradores (hilos) para este cliente")
    parser.add_argument("--host", default="127.0.0.1", help="Host del servidor")
    parser.add_argument("--port", type=int, default=5000, help="Puerto del servidor")
    parser.add_argument("--use-http", action="store_true", help="Usar la API HTTP del gateway en vez del protocolo socket")
    parser.add_argument("--api-port", type=int, default=5001, help="Puerto del API HTTP del gateway (cuando --use-http)")
    parser.add_argument("--client-id", default=None, help="Identificador único del punto de acceso")
    parser.add_argument("--sale-id", default=None, help="ID de venta para sincronización con el servidor")
    return parser.parse_args()


def normalize_client_type(raw_type):
    normalized = TYPE_ALIAS.get((raw_type or "").strip().lower())
    if normalized is None:
        raise ValueError("Tipo inválido. Usa A/B/C o normal/preferente/platino")
    return normalized


def main():
    global sales_start_ts, sales_end_ts

    args = parse_args()
    global USE_HTTP, API_PORT
    USE_HTTP = bool(args.use_http)
    API_PORT = int(args.api_port)
    normalized_type = normalize_client_type(args.client_type)

    if args.buyers <= 0:
        raise ValueError("buyers debe ser mayor que 0")

    client_id = args.client_id or f"{args.client_type.upper()}-{str(uuid.uuid4())[:6]}"
    sale_id = args.sale_id or f"{args.host}:{args.port}"

    with terminal_lock:
        print("Punto de acceso cliente iniciado")
        print(f"ID cliente: {client_id}")
        print(f"Servidor objetivo: {sale_id} ({args.host}:{args.port})")
        print(f"Sale ID: {sale_id}")
        print(f"Tipo de compradores: {normalized_type}")
        print(f"Compradores a crear en este cliente: {args.buyers}")

    register_and_wait_start(args.host, args.port, client_id, normalized_type, args.buyers)

    health_thread = threading.Thread(target=monitor_server_health, args=(args.host, args.port), daemon=True)
    health_thread.start()

    sales_start_ts = time.perf_counter()

    for buyer_number in range(1, args.buyers + 1):
        thread = threading.Thread(
            target=buyer_worker,
            args=(buyer_number, args.host, args.port, client_id, normalized_type),
            daemon=False,
        )
        threads.append(thread)
        thread.start()
        time.sleep(0.0005)

    for thread in threads:
        thread.join()

    notify_client_done(args.host, args.port, client_id)

    sales_end_ts = time.perf_counter()
    print_summary(client_id, normalized_type, args.buyers)


if __name__ == "__main__":
    main()
