import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

LOGIN_URL = "https://backend.tacerca.cl/api/auth/login"
BOOKING_URL = "https://backend.tacerca.cl/api/planning-trip/get-booking"

# Ruta fija: Los Montes -> Escuela Militar
ORIGIN_ID = "64f5268fc82f4c1fbbbccba2"
DESTINATION_ID = "654285ab8ef632cd5d0632dc"

USERNAME = os.environ.get("TACERCA_USERNAME", "").strip()
PASSWORD = os.environ.get("TACERCA_PASSWORD", "").strip()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()



def ask_trip_date() -> str:
    date_str = input("Ingresa la fecha del viaje (DD-MM-AAAA): ").strip()

    try:
        datetime.strptime(date_str, "%d-%m-%Y")
    except ValueError as exc:
        raise RuntimeError(
            "Fecha inválida. Usa formato DD-MM-AAAA, por ejemplo 15-04-2026."
        ) from exc

    return date_str


def date_to_unix_ms(date_str: str) -> int:
    dt = datetime.strptime(date_str, "%d-%m-%Y")
    return int(dt.timestamp() * 1000)


def state_file_for_date(trip_date: str) -> Path:
    safe_date = trip_date.replace("-", "")
    return Path(f"tacerca_state_{safe_date}.json")


def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(path: Path, state: dict) -> None:
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def send_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID.")

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    response = requests.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
        },
        timeout=20,
    )
    response.raise_for_status()


def login() -> str:
    if not USERNAME or not PASSWORD:
        raise RuntimeError("Faltan TACERCA_USERNAME o TACERCA_PASSWORD.")

    response = requests.post(
        LOGIN_URL,
        json={
            "username": USERNAME,
            "password": PASSWORD,
        },
        headers={
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://tacerca.cl",
            "Referer": "https://tacerca.cl/",
            "User-Agent": "Mozilla/5.0",
        },
        timeout=30,
    )
    response.raise_for_status()

    payload = response.json()
    token = payload.get("data", {}).get("accessToken")

    if not token:
        raise RuntimeError(f"No se encontró accessToken en la respuesta: {payload}")

    return token


def fetch_trips(access_token: str, trip_date: str) -> list:
    params = {
        "origin": ORIGIN_ID,
        "destination": DESTINATION_ID,
        "date": str(date_to_unix_ms(trip_date)),
    }

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Authorization": f"Bearer {access_token}",
        "Origin": "https://tacerca.cl",
        "Referer": "https://tacerca.cl/",
        "User-Agent": "Mozilla/5.0",
    }

    response = requests.get(
        BOOKING_URL,
        params=params,
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()

    payload = response.json()

    if not isinstance(payload, dict) or "data" not in payload:
        raise RuntimeError(f"Respuesta inesperada del backend: {payload}")

    trips = payload["data"]
    if not isinstance(trips, list):
        raise RuntimeError(f"'data' no es una lista: {payload}")

    return trips


def available_seats(trip: dict) -> int:
    total = int(trip.get("vehicle", {}).get("seatings", 0))
    occupied = len(trip.get("seatings", []))
    return max(total - occupied, 0)


def build_snapshot(trips: list) -> list:
    snapshot = []

    for trip in trips:
        seats = available_seats(trip)
        snapshot.append(
            {
                "trip_id": trip.get("activeTrip") or trip.get("_id") or "",
                "hourInit": trip.get("hourInit", ""),
                "hourFinish": trip.get("hourFinish", ""),
                "price": trip.get("price", 0),
                "available": seats,
            }
        )

    snapshot.sort(key=lambda x: (x["hourInit"], x["trip_id"]))
    return snapshot


def make_signature(snapshot: list) -> str:
    compact = [
        {
            "trip_id": item["trip_id"],
            "hourInit": item["hourInit"],
            "available": item["available"],
        }
        for item in snapshot
    ]
    return json.dumps(compact, sort_keys=True, ensure_ascii=False)


def build_full_status_message(snapshot: list, trip_date: str) -> str:
    lines = [
        f"Tacerca {trip_date} - Los Montes -> Escuela Militar",
        "Estado actual por horario:",
    ]

    if not snapshot:
        lines.append("- Sin horarios devueltos por la consulta")
        return "\n".join(lines)

    for item in snapshot:
        if item["available"] > 0:
            status = f"{item['available']} asiento(s) disponibles"
        else:
            status = "Sin asientos disponibles"

        lines.append(
            f"- {item['hourInit']} -> {item['hourFinish']}: {status}, tarifa ${item['price']}"
        )

    return "\n".join(lines)


def build_changes_message(old_snapshot: list, new_snapshot: list, trip_date: str) -> str:
    old_map = {item["trip_id"]: item for item in old_snapshot}
    changes = []

    for item in new_snapshot:
        old_item = old_map.get(item["trip_id"])
        old_available = old_item["available"] if old_item else None
        new_available = item["available"]

        if old_available != new_available:
            if new_available > 0:
                status = f"{new_available} asiento(s) disponibles"
            else:
                status = "Sin asientos disponibles"

            if old_available is None:
                changes.append(
                    f"- {item['hourInit']} -> {item['hourFinish']}: nuevo horario, {status}, tarifa ${item['price']}"
                )
            else:
                changes.append(
                    f"- {item['hourInit']} -> {item['hourFinish']}: "
                    f"{old_available} -> {new_available} asiento(s), tarifa ${item['price']}"
                )

    if not changes:
        return ""

    lines = [
        f"Tacerca {trip_date} - Los Montes -> Escuela Militar",
        "Cambios detectados:",
    ]
    lines.extend(changes)
    return "\n".join(lines)


def run_check(trip_date: str, state_file: Path) -> None:
    state = load_state(state_file)
    old_signature = state.get("last_signature")
    old_snapshot = state.get("last_snapshot", [])

    access_token = login()
    trips = fetch_trips(access_token, trip_date)
    snapshot = build_snapshot(trips)
    new_signature = make_signature(snapshot)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if old_signature is None:
        message = build_full_status_message(snapshot, trip_date)
        print(f"[{timestamp}] Primera ejecución")
        print(message)
        send_telegram(message)
        state["last_signature"] = new_signature
        state["last_snapshot"] = snapshot
        save_state(state_file, state)
        print("Notificación inicial enviada a Telegram.")
        print()
        return

    if new_signature != old_signature:
        message = build_changes_message(old_snapshot, snapshot, trip_date)
        if message:
            print(f"[{timestamp}] Cambio detectado")
            print(message)
            send_telegram(message)

        state["last_signature"] = new_signature
        state["last_snapshot"] = snapshot
        save_state(state_file, state)
        print("Cambio registrado y notificado.")
        print()
    else:
        print(f"[{timestamp}] Sin cambios. No se envió notificación.")
        print()


def main() -> int:
    try:
        trip_date = ask_trip_date()
        state_file = state_file_for_date(trip_date)

        print()
        print(f"Monitoreando Tacerca para la fecha {trip_date}")
        print("Ruta fija: Los Montes -> Escuela Militar")
        print("Frecuencia: cada 1 minuto")
        print("Presiona Ctrl+C para detener.")
        print()

        while True:
            try:
                run_check(trip_date, state_file)
            except requests.HTTPError as exc:
                print(f"HTTP error: {exc}", file=sys.stderr)
                if exc.response is not None:
                    print(exc.response.text[:1000], file=sys.stderr)
                print()
            except Exception as exc:
                print(f"Error: {exc}", file=sys.stderr)
                print()

            time.sleep(60)

    except KeyboardInterrupt:
        print("\nMonitoreo detenido por el usuario.")
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())