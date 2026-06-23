## Práctica 08 - Microservicios

# Hipótesis

Comprendiendo que los microservicios son funciones separadas al funcionamiento y vida del sistema, que involucran acciones o llamadas, como procesos siempre activos o dormidos que se llaman ocacionalmente.

Como tratamos un entorno de alta afluencia con resultados inmediatos, lo mejor es que el microservicio se mantenga activo en todo momento.

# Planteamiento de la práctica.

Siguiendo prácticas anteriores se plantea migrar el Ticketing Service ya desarrollado en un microservicio en regla.

Lo que implica que el servicio de tickets se convierta en la autoridad que maneja el mapa de memoria de asientos, los cambios y actualizaciones. Reduciendo la autoridad del servidor, modificandolo para que actue como un API Gateway (punto de conexión).

## Requisitos de funcionamiento

Flask>=2.0

# Compilación 

Linux

Windows
