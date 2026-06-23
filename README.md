## Práctica 08 - Microservicios

# Hipótesis

Comprendiendo que los microservicios son funciones separadas al funcionamiento y vida del sistema, que involucran acciones o llamadas, como procesos siempre activos o dormidos que se llaman ocacionalmente.

Como tratamos un entorno de alta afluencia con resultados inmediatos, lo mejor es que el microservicio se mantenga activo en todo momento.

# Planteamiento de la práctica.

Siguiendo prácticas anteriores se plantea migrar el Ticketing Service ya desarrollado en un microservicio en regla.

Lo que implica que el servicio de tickets se convierta en la autoridad que maneja el mapa de memoria de asientos, los cambios y actualizaciones. Reduciendo la autoridad del servidor, modificandolo para que actue como un API Gateway (punto de conexión).

## Requisitos de funcionamiento

Flask>=2.0

# Dockerización

La Práctica 08 se despliega con tres contenedores:

- `server`: API/gateway en `8080`
- `ticketing_service`: autoridad de asientos en `7000`
- `frontend`: Nginx estático para dashboard y PWA en `80`

Arranque local:

```bash
docker compose up -d --build
```

URLs resultantes:

- Dashboard: `http://localhost/`
- PWA: `http://localhost/pwa/`
- API del gateway: `http://localhost/api/...`

Si quieres acceso directo sin Nginx, también quedan expuestos:

- Gateway: `http://localhost:8080`
- Ticketing service: `http://localhost:7000`

La imagen del frontend sirve el dashboard en la raíz y la PWA en `/pwa/`, de forma que ambas convivan sin tocar la lógica de negocio.

# Compilación 

Linux

Windows
