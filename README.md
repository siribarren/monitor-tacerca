# Tacerca automation

## Archivos
- `monitor_tacerca.py`: script principal, con monitoreo + auto-reserva.
- `monitor_tacerca_backup.py`: respaldo del monitor original, sanitizado.
- `.env.example`: plantilla de variables.
- `requirements.txt`: dependencias Python.
- `.gitignore`: excluye secretos y estado local.

## Instalación
```bash
python3 -m pip install -r requirements.txt
cp .env.example .env
```

## Ejecución
```bash
python3 monitor_tacerca.py
```

El script primero preguntará el modo:
- `1`: flujo actual de monitor / reserva puntual.
- `2`: monitor de Compra Masiva para Piedra Roja, Los Montes -> Escuela Militar, desde el lunes `04-05-2026`, horario `06:54`, asiento `7`, por `7` días.

En el modo `1`, el script preguntará la fecha por consola y luego la regla horaria:
- Hora exacta: acepta uno o más horarios, por ejemplo `06:54` o `06:54, 08:25, 17:54`.
- Ventana horaria: acepta cualquier rango `HH:MM -> HH:MM`, por ejemplo `08:00 -> 10:00`.

La reserva usa los ids del viaje seleccionado para que cualquier horario o franja funcione de forma independiente.

El modo `2` envía una alerta a Telegram cuando el viaje gatillo del `04-05-2026` a las `06:54` tiene asientos disponibles. Si los 7 días tienen el mismo horario y el asiento `7` disponible, intenta `trips/create-trip-massive/` con pago según `TACERCA_TYPE_PAYMENT`.

Variables opcionales para ajustar el modo `2` sin cambiar código:
- `TACERCA_MASSIVE_START_DATE`
- `TACERCA_MASSIVE_HOUR`
- `TACERCA_MASSIVE_SEAT`
- `TACERCA_MASSIVE_DAYS`
- `TACERCA_MASSIVE_POLL_SECONDS`
