# SD-Practica06

Versión adaptada para despliegue en nube de la práctica de ventas concurrentes.

## Objetivo del dashboard

Mostrar en tiempo real el estado de la venta: asientos libres, reservados y vendidos, tickets emitidos, eventos recientes y avance de la carga. El dashboard no vende boletos; solo monitorea la simulación y permite lanzar cargas de prueba.

## Cambios frente a la práctica original

- Se reemplazó la interfaz de escritorio por un dashboard web.
- Se agregó un backend HTTP para exponer métricas y control de carga.
- Se dockerizaron frontend, API y ticketing service.
- Se añadió un botón para generar carga y otro para reiniciar la venta.
- Se eliminó la dependencia de WebSockets, service workers e IndexedDB.

## Componentes

- `webapp/`: dashboard visual de monitoreo.
- `PWA/`: la aplicación que funge como cliente.
- `servidor.py`: API principal de la venta.
- `ticketing_service.py`: servicio externo que persiste tickets.

## Arquitectura

Usuario -> Dashboard web -> Servidor API -> Ticketing Service

El frontend consulta `GET /api/stats` y dispara la simulación con `POST /api/generate-load`.

## Ejecución local

1. Inicia el ticketing service:

```bash
python ticketing_service.py --host 127.0.0.1 --port 7000 --store-file tickets/tickets.txt
```

2. Inicia el servidor API:

```bash
python servidor.py --host 127.0.0.1 --port 8080 --ticket-service-host 127.0.0.1 --ticket-service-port 7000
```

3. Iniciar la ejecución de la PWA:

```bash
python -m http.server 8000 --directory webapp  
```

4. Acceder al http del dashboard y pwa para comprobar el funcionamiento del proyecto:

http://127.0.0.1:5001/dashboard
http://127.0.0.1:8000/index.html


## Docker

La VM no necesita instalar Python ni Java. Todo corre dentro de Docker.

Prerrequisitos en cualquier VM Linux de Azure, GCP o AWS:

- `git`
- `docker engine`
- `docker compose plugin`

Instalación rápida en Ubuntu:

```bash
sudo apt update
sudo apt install -y git docker.io docker-compose-v2
sudo usermod -aG docker $USER
```

Después cierra sesión y vuelve a entrar para usar Docker sin `sudo`.

Despliegue básico:

1. Clona el repositorio en la VM.
2. Entra a la carpeta `SD-Practica06`.
3. Verifica la configuración con `docker compose config`.
4. Levanta los contenedores con `docker compose up -d --build`.
5. Abre el puerto `80` en el NSG o firewall de la VM.
6. Entra al dashboard desde `http://IP_PUBLICA/`.

Para depuración puedes exponer `8080` y `7000`, pero para uso normal basta con `80`.

Servicios:

- Frontend: `http://localhost/`
- API interna: `http://server:8080/` dentro de la red Docker
- Ticketing Service interno: `http://ticketing_service:7000/` dentro de la red Docker

## Operación

- `Generar carga` inicia una simulación concurrente de compradores.
- `Reiniciar venta` limpia la venta, las reservas y los registros visibles.
- La vista muestra asientos vendidos, reservados, libres, tickets emitidos, métricas y eventos recientes.
