"""
Visible integration runner for the microservices flow described in BASIC.txt.

What it does:
- Starts the Ticketing Service authority.
- Starts the gateway server for 1 client.
- Starts the static server for the PWA.
- Opens the PWA in the default browser.
- Waits until the sale is open, then performs a small smoke test through the API.

The script keeps the processes alive until you press Ctrl+C so you can watch
the PWA update in the browser.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT_DIR = Path(__file__).resolve().parent
PYTHON = sys.executable or "py"

TICKETING_HOST = "127.0.0.1"
TICKETING_PORT = 7000
GATEWAY_HOST = "127.0.0.1"
GATEWAY_SOCKET_PORT = 5000
GATEWAY_API_PORT = 5001
STATIC_HOST = "127.0.0.1"
STATIC_PORT = 8000

PWA_URL = f"http://{STATIC_HOST}:{STATIC_PORT}/index.html?v=4"
API_BASE = f"http://{GATEWAY_HOST}:{GATEWAY_API_PORT}"

CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def post_json(path, payload, timeout=5):
    data = json.dumps(payload).encode("utf-8")
    request = Request(
        API_BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def get_json(path, timeout=5):
    request = Request(API_BASE + path, method="GET")
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for_http(path, timeout_seconds=30, interval=0.5):
    deadline = time.time() + timeout_seconds
    last_error = None
    while time.time() < deadline:
        try:
            return get_json(path)
        except Exception as exc:
            last_error = exc
            time.sleep(interval)
    raise RuntimeError(f"No se pudo alcanzar {path} en {timeout_seconds}s: {last_error}")


def wait_for_static_site(url, timeout_seconds=30, interval=0.5):
    deadline = time.time() + timeout_seconds
    last_error = None
    while time.time() < deadline:
        try:
            request = Request(url, method="GET")
            with urlopen(request, timeout=5) as response:
                if response.status < 400:
                    return True
        except Exception as exc:
            last_error = exc
            time.sleep(interval)
    raise RuntimeError(f"No se pudo abrir la PWA en {timeout_seconds}s: {last_error}")


def start_process(command, title):
    print(f"Iniciando {title}...")
    return subprocess.Popen(
        command,
        cwd=str(ROOT_DIR),
        creationflags=CREATE_NEW_CONSOLE | CREATE_NEW_PROCESS_GROUP,
    )


def open_browser(url):
    print(f"Abriendo navegador en: {url}")
    try:
        webbrowser.open(url, new=2)
    except Exception as exc:
        print(f"No se pudo abrir el navegador automáticamente: {exc}")


def wait_for_sale_open(timeout_seconds=60):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            snapshot = get_json("/api/availability")
            sale_status = snapshot.get("sale_status") or {}
            if sale_status.get("state") == "open":
                return snapshot
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("La venta no abrió a tiempo")


def smoke_test_flow():
    buyer_id = "TEST-BUYER-1"
    buyer_type = "normal"
    print("Ejecutando smoke test sobre la API del gateway...")

    availability = get_json("/api/availability")
    if not availability.get("seat_status"):
        raise RuntimeError("La disponibilidad no contiene mapa de asientos")

    seat = None
    seat_status = availability["seat_status"]
    allowed_row_start = 7 if buyer_type == "normal" else 3 if buyer_type == "preferente" else 0
    for row_index, row in enumerate(seat_status):
        if row_index < allowed_row_start:
            continue
        for col_index, state in enumerate(row):
            if state == "FREE":
                seat = {"row": row_index, "col": col_index}
                break
        if seat:
            break

    if not seat:
        raise RuntimeError("No se encontró un asiento libre para la prueba")

    request_payload = {
        "type": "REQUEST_TICKET",
        "buyer_id": buyer_id,
        "buyer_type": buyer_type,
        "request_id": f"TEST-{int(time.time() * 1000)}",
        "row": seat["row"],
        "col": seat["col"],
    }
    reservation = post_json("/api/request_ticket", request_payload)
    print("request_ticket ->", reservation)
    if reservation.get("status") != "ok":
        raise RuntimeError(f"No se pudo reservar el asiento de prueba: {reservation}")

    purchase_payload = {
        "type": "PURCHASE",
        "buyer_id": buyer_id,
        "reservation_id": reservation.get("reservation_id"),
        "request_id": f"TEST-PURCHASE-{int(time.time() * 1000)}",
    }
    purchase = post_json("/api/purchase", purchase_payload)
    print("purchase ->", purchase)
    if purchase.get("status") != "ok":
        raise RuntimeError(f"No se pudo completar la compra de prueba: {purchase}")

    refreshed = get_json("/api/availability")
    current_state = refreshed["seat_status"][seat["row"]][seat["col"]]
    print(f"Asiento {seat['row']}-{seat['col']} estado final: {current_state}")
    if current_state != "SOLD":
        raise RuntimeError(f"Se esperaba SOLD y se obtuvo {current_state}")


def terminate_processes(processes):
    for process in reversed(processes):
        if process.poll() is None:
            try:
                process.terminate()
            except Exception:
                pass

    deadline = time.time() + 5
    for process in reversed(processes):
        while process.poll() is None and time.time() < deadline:
            time.sleep(0.1)
        if process.poll() is None:
            try:
                process.kill()
            except Exception:
                pass


def main():
    processes = []
    try:
        ticketing = start_process(
            [
                PYTHON,
                "ticketing_service.py",
                "--host",
                TICKETING_HOST,
                "--port",
                str(TICKETING_PORT),
            ],
            "Ticketing Service",
        )
        processes.append(ticketing)

        gateway = start_process(
            [
                PYTHON,
                "servidor.py",
                "1",
                "--host",
                GATEWAY_HOST,
                "--port",
                str(GATEWAY_SOCKET_PORT),
                "--no-gui",
                "--ticket-service-host",
                TICKETING_HOST,
                "--ticket-service-port",
                str(TICKETING_PORT),
            ],
            "Gateway",
        )
        processes.append(gateway)

        static_server = start_process(
            [
                PYTHON,
                "-m",
                "http.server",
                str(STATIC_PORT),
                "--directory",
                "webapp",
            ],
            "Servidor estático PWA",
        )
        processes.append(static_server)

        print("Esperando a que el gateway y la PWA estén disponibles...")
        wait_for_http("/api/availability", timeout_seconds=45)
        wait_for_static_site(PWA_URL, timeout_seconds=45)

        open_browser(PWA_URL)

        print("Esperando a que la PWA se registre y abra la venta...")
        snapshot = wait_for_sale_open(timeout_seconds=90)
        sale_status = snapshot.get("sale_status") or {}
        print("Venta abierta ->", sale_status)

        smoke_test_flow()

        print()
        print("Flujo visible listo.")
        print("Deja el navegador abierto para revisar el mapa de asientos.")
        print("Pulsa Ctrl+C en esta terminal cuando quieras cerrar servicios.")

        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nInterrumpido por el usuario.")
    finally:
        terminate_processes(processes)


if __name__ == "__main__":
    main()