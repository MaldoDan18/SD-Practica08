## Práctica 08 - Microservicios y PWA

Esta práctica separa la autoridad de asientos en un `ticketing_service` y deja al servidor como `API Gateway`. La PWA sigue siendo el cliente visual, y el dashboard convive con ella sin tocar la lógica de venta.

## Cambios frente a la Práctica 07

- El servidor ya no decide los asientos: sólo enruta y expone API.
- El `ticketing_service` mantiene el estado real de reservas, compras y disponibilidad.
- La PWA se sirve en `/pwa/` y el dashboard en `/`.

## Funcionamiento

- `server` en `8080`: dashboard, PWA y API.
- `ticketing_service` en `7000`: autoridad de asientos.
- `frontend` en `80`: Nginx para archivos estáticos y proxy a `/api/`.

## Ejecución local

```powershell
docker compose up -d --build
```

URLs:

- Dashboard: `http://localhost/`
- PWA: `http://localhost/pwa/`
- API: `http://localhost/api/...`

Acceso directo:

- Gateway: `http://localhost:8080`
- Ticketing service: `http://localhost:7000`