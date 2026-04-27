#!/usr/bin/env python3
"""
Tacerca monitor + auto-booker.

Objetivo:
- Mantener el comportamiento base del monitor original:
  pregunta la fecha por consola, consulta disponibilidad y notifica cambios.
- Agregar, sin romper ese flujo, la capacidad de reservar automáticamente
  cuando aparezca un horario objetivo.

Flujo observado en HAR:
  GET  /api/planning-trip/get-booking
  GET  /api/trips/check-trip-exists/{activeTrip}
  GET  /api/user/get-customer/{rut}
  GET  /api/wallet/payment-methods
  POST /api/trips/create-trip-auth/
  POST /api/trips/create-trip-massive/
  GET  /api/trips/get-trip/{code}

Notas operativas:
- Si NO defines regla horaria, el script funciona sólo como monitor, igual que el original.
- Si defines regla horaria, además de monitorear intentará reservar cuando aparezca un viaje candidato.
- Si el asiento preferido está ocupado, tomará el primer asiento libre disponible.
- Si ya reservó una vez para la fecha, no intentará reservar de nuevo.
- El modo Compra Masiva mantiene su propio archivo de estado para evitar duplicados.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()


BASE_URL = "https://backend.tacerca.cl/api"
LOGIN_URL = f"{BASE_URL}/auth/login"
BOOKING_URL = f"{BASE_URL}/planning-trip/get-booking"
CHECK_TRIP_EXISTS_URL = f"{BASE_URL}/trips/check-trip-exists"
GET_CUSTOMER_URL = f"{BASE_URL}/user/get-customer"
PAYMENT_METHODS_URL = f"{BASE_URL}/wallet/payment-methods"
CREATE_TRIP_AUTH_URL = f"{BASE_URL}/trips/create-trip-auth/"
CREATE_TRIP_MASSIVE_URL = f"{BASE_URL}/trips/create-trip-massive/"
GET_TRIP_URL = f"{BASE_URL}/trips/get-trip"

# Ruta fija observada en el script original.
DEFAULT_ORIGIN_ID = "64f5268fc82f4c1fbbbccba2"
DEFAULT_DESTINATION_ID = "654285ab8ef632cd5d0632dc"

DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://tacerca.cl",
    "Referer": "https://tacerca.cl/",
    "User-Agent": "Mozilla/5.0",
}

REQUEST_TIMEOUT = 30
STATE_DIR = Path(".")
MASSIVE_DEFAULT_START_DATE = "04-05-2026"
MASSIVE_DEFAULT_HOUR = "06:54"
MASSIVE_DEFAULT_SEAT = 7
MASSIVE_DEFAULT_DAYS = 7
MAY_PLAN_DEFAULT_POLL_SECONDS = 30
MAY_PLAN_DEFAULT_SEAT = 7
MAY_PLAN_OUTBOUND_HOUR = "06:54"
MAY_PLAN_RETURN_HOUR = "19:00"
MAY_PLAN_DATE_BLOCKS = [
    [
        "04-05-2026",
        "05-05-2026",
        "06-05-2026",
        "07-05-2026",
        "08-05-2026",
        "12-05-2026",
        "13-05-2026",
    ],
    [
        "14-05-2026",
        "15-05-2026",
        "18-05-2026",
        "19-05-2026",
        "20-05-2026",
        "22-05-2026",
        "26-05-2026",
    ],
    [
        "27-05-2026",
        "28-05-2026",
        "29-05-2026",
    ],
]


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on", "si", "sí"}


def env_int(name: str, default: int | None = None) -> int | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return int(value.strip())


def env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def normalize_hhmm(value: Any) -> str | None:
    if value is None:
        return None

    raw = str(value).strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::\d{2})?", raw)
    if not match:
        return None

    hh_i, mm_i = int(match.group(1)), int(match.group(2))
    if not (0 <= hh_i <= 23 and 0 <= mm_i <= 59):
        return None

    return f"{hh_i:02d}:{mm_i:02d}"


def parse_hhmm(value: str) -> tuple[int, int]:
    normalized = normalize_hhmm(value)
    if normalized is None:
        raise ValueError(f"Hora inválida: {value!r}. Usa HH:MM.")

    hh, mm = normalized.split(":")
    return int(hh), int(mm)


def require_hhmm(value: str) -> str:
    normalized = normalize_hhmm(value)
    if normalized is None:
        raise ValueError(f"Hora inválida: {value!r}. Usa HH:MM.")
    return normalized


def hhmm_to_minutes(value: str) -> int:
    hh, mm = parse_hhmm(value)
    return hh * 60 + mm


def minutes_in_window(value: int, start: int, end: int) -> bool:
    if start <= end:
        return start <= value <= end
    return value >= start or value <= end


def ask_run_mode() -> str:
    print("Elige modo de ejecución.")
    print("1) Monitor / reserva puntual actual")
    print("2) Compra masiva Piedra Roja 04-05-2026 06:54 asiento 7")
    print("3) Plan Mayo 2026 ida/vuelta Piedra Roja")
    option = input("Elige una opción [1/2/3]: ").strip()
    if option not in {"1", "2", "3"}:
        raise ValueError("Opción inválida. Elige 1, 2 o 3.")
    return option


def ask_trip_date() -> str:
    date_str = input("Ingresa la fecha del viaje (DD-MM-AAAA): ").strip()
    validate_trip_date(date_str)
    return date_str


def ask_target_rule() -> "ReservationRule":
    print("Configura el horario objetivo.")
    print("1) Hora exacta")
    print("2) Ventana horaria")
    print("3) Solo monitoreo (sin reserva)")
    option = input("Elige una opción [1/2/3]: ").strip()

    if option == "1":
        hours_raw = input("Ingresa horario(s) objetivo (HH:MM, separados por coma): ").strip()
        exact_hours = [require_hhmm(hour.strip()) for hour in hours_raw.split(",") if hour.strip()]
        if not exact_hours:
            raise ValueError("Debes ingresar al menos un horario objetivo.")

        max_price_raw = input("Precio máximo opcional (Enter para omitir): ").strip()
        max_price = int(max_price_raw) if max_price_raw else None

        auto_book_raw = input("Activar auto reserva? [s/n]: ").strip().lower()
        auto_book = auto_book_raw in {"s", "si", "sí", "y", "yes"}

        poll_raw = input("Frecuencia en segundos [15]: ").strip()
        poll_seconds = int(poll_raw) if poll_raw else 15

        return ReservationRule(
            exact_hours=exact_hours,
            hour_from=None,
            hour_to=None,
            max_price=max_price,
            auto_book=auto_book,
            poll_seconds=poll_seconds,
        )

    if option == "2":
        hour_from = require_hhmm(input("Hora inicio ventana (HH:MM): ").strip())
        hour_to = require_hhmm(input("Hora fin ventana (HH:MM): ").strip())

        max_price_raw = input("Precio máximo opcional (Enter para omitir): ").strip()
        max_price = int(max_price_raw) if max_price_raw else None

        auto_book_raw = input("Activar auto reserva? [s/n]: ").strip().lower()
        auto_book = auto_book_raw in {"s", "si", "sí", "y", "yes"}

        poll_raw = input("Frecuencia en segundos [15]: ").strip()
        poll_seconds = int(poll_raw) if poll_raw else 15

        return ReservationRule(
            exact_hours=[],
            hour_from=hour_from,
            hour_to=hour_to,
            max_price=max_price,
            auto_book=auto_book,
            poll_seconds=poll_seconds,
        )

    poll_raw = input("Frecuencia en segundos [60]: ").strip()
    poll_seconds = int(poll_raw) if poll_raw else 60

    return ReservationRule(
        exact_hours=[],
        hour_from=None,
        hour_to=None,
        max_price=None,
        auto_book=False,
        poll_seconds=poll_seconds,
    )

def validate_trip_date(date_str: str) -> None:
    try:
        datetime.strptime(date_str, "%d-%m-%Y")
    except ValueError as exc:
        raise RuntimeError(
            "Fecha inválida. Usa formato DD-MM-AAAA, por ejemplo 23-04-2026."
        ) from exc


def date_to_unix_ms(date_str: str) -> int:
    dt = datetime.strptime(date_str, "%d-%m-%Y")
    return int(dt.timestamp() * 1000)


def date_to_unix_ms_noon(date_str: str) -> int:
    dt = datetime.strptime(date_str, "%d-%m-%Y").replace(
        hour=12,
        minute=0,
        second=0,
        microsecond=0,
    )
    return int(dt.timestamp() * 1000)


def date_range_strings(start_date: str, days: int) -> list[str]:
    validate_trip_date(start_date)
    start = datetime.strptime(start_date, "%d-%m-%Y")
    return [(start + timedelta(days=offset)).strftime("%d-%m-%Y") for offset in range(days)]


def state_file_for_date(trip_date: str) -> Path:
    safe_date = trip_date.replace("-", "")
    return STATE_DIR / f"tacerca_state_{safe_date}.json"


def state_file_for_massive(config: "MassiveBookingConfig") -> Path:
    safe_date = config.start_date.replace("-", "")
    return STATE_DIR / f"tacerca_massive_state_{safe_date}_{config.days}d.json"


def state_file_for_may2026_plan() -> Path:
    return STATE_DIR / "tacerca_plan_mayo_2026_state.json"


def load_state(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def send_telegram(message: str) -> None:
    token = env_str("TELEGRAM_BOT_TOKEN")
    chat_id = env_str("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    response = requests.post(
        url,
        json={"chat_id": chat_id, "text": message},
        timeout=20,
    )
    response.raise_for_status()


def now_ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def first_non_empty(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def deep_find_first(obj: Any, keys: set[str]) -> Any:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in keys and value not in (None, "", [], {}):
                return value
        for value in obj.values():
            found = deep_find_first(value, keys)
            if found not in (None, "", [], {}):
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = deep_find_first(item, keys)
            if found not in (None, "", [], {}):
                return found
    return None


def deep_collect_values(obj: Any, keys: set[str]) -> list[Any]:
    values: list[Any] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in keys and value not in (None, "", [], {}):
                values.append(value)
            values.extend(deep_collect_values(value, keys))
    elif isinstance(obj, list):
        for item in obj:
            values.extend(deep_collect_values(item, keys))
    return values


def normalize_trip_id(trip: dict[str, Any]) -> str:
    active_trip = trip.get("activeTrip")
    if isinstance(active_trip, dict):
        return str(first_non_empty(active_trip.get("_id"), trip.get("_id"), "")) or ""
    return str(first_non_empty(active_trip, trip.get("_id"), "")) or ""


def normalize_planning_trip_id(trip: dict[str, Any]) -> str:
    active_trip = trip.get("activeTrip")
    if isinstance(active_trip, dict):
        found = first_non_empty(
            trip.get("planningTrip"),
            trip.get("planingTrip"),
            active_trip.get("planningTrip"),
            active_trip.get("planingTrip"),
            trip.get("_id"),
        )
        return str(found or "")
    found = first_non_empty(trip.get("planningTrip"), trip.get("planingTrip"), trip.get("_id"))
    return str(found or "")


def trip_departure_hour(trip: dict[str, Any]) -> str:
    return normalize_hhmm(first_non_empty(trip.get("hourInit"), trip.get("hourDeparture"))) or ""


def trip_arrival_hour(trip: dict[str, Any]) -> str:
    return normalize_hhmm(first_non_empty(trip.get("hourFinish"), trip.get("hourArrival"))) or ""


def trip_execution_date(trip: dict[str, Any]) -> str:
    raw = first_non_empty(trip.get("dateExecuting"), trip.get("dateExecution"), trip.get("date"))
    if raw is None:
        return ""

    if isinstance(raw, (int, float)):
        value = float(raw)
        if value > 10_000_000_000:
            value /= 1000
        return datetime.fromtimestamp(value).strftime("%d-%m-%Y")

    text = str(raw).strip()
    if not text:
        return ""

    date_part = text.split("T", 1)[0]
    for fmt in ("%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(date_part, fmt).strftime("%d-%m-%Y")
        except ValueError:
            pass

    return ""


def trip_station_origin_name(trip: dict[str, Any], fallback: str = "Cond. Los Montes") -> str:
    return str(
        first_non_empty(
            trip.get("stationOrigin"),
            deep_find_first(trip, {"stationOriginName", "originName"}),
            fallback,
        )
    )


def trip_station_destination_name(
    trip: dict[str, Any],
    fallback: str = "Metro Escuela Militar",
) -> str:
    return str(
        first_non_empty(
            trip.get("stationDestination"),
            deep_find_first(trip, {"stationDestinationName", "destinationName"}),
            fallback,
        )
    )


def infer_station_id(trip: dict[str, Any]) -> str:
    if env_bool("TACERCA_USE_FIXED_STATION_ID", False):
        explicit = env_str("TACERCA_STATION_ID")
        if explicit:
            return explicit

    candidates = []
    for key in ("station", "stationId", "originStation", "boardingStation"):
        value = deep_find_first(trip, {key})
        if value:
            candidates.append(value)

    for value in candidates:
        if isinstance(value, dict):
            nested_id = first_non_empty(value.get("_id"), value.get("id"))
            if nested_id:
                return str(nested_id)
        elif isinstance(value, str):
            return value

    station_id = trip.get("_id")
    if station_id:
        return str(station_id)

    raise RuntimeError("No se pudo inferir station desde get-booking.")


def available_seats(trip: dict[str, Any]) -> int:
    total = extract_total_seat_count(trip)

    raw_seatings = trip.get("seatings", [])
    if isinstance(raw_seatings, list):
        occupied_count = len(raw_seatings)
    else:
        occupied_count = len(extract_reserved_seat_numbers(trip))

    if total <= 0:
        return 0

    return max(total - occupied_count, 0)


def extract_total_seat_count(trip: dict[str, Any]) -> int:
    vehicle = trip.get("vehicle", {})
    if isinstance(vehicle, dict):
        count = first_non_empty(vehicle.get("seatings"), vehicle.get("totalSeats"))
        if isinstance(count, int):
            return count
        if isinstance(count, str) and count.strip().isdigit():
            return int(count.strip())
        if isinstance(count, list):
            return len(count)

    count = deep_find_first(trip, {"totalSeats"})
    if isinstance(count, int):
        return count
    if isinstance(count, str) and count.strip().isdigit():
        return int(count.strip())
    if isinstance(count, list):
        return len(count)

    return 0

def extract_reserved_seat_numbers(trip: dict[str, Any]) -> set[int]:
    reserved: set[int] = set()
    raw_values = deep_collect_values(trip.get("seatings", []), {"seating", "seat", "number", "seatNumber"})
    for value in raw_values:
        try:
            reserved.add(int(str(value)))
        except Exception:
            pass
    return reserved


def specific_seat_available(trip: dict[str, Any], seat: int) -> tuple[bool, str]:
    total = extract_total_seat_count(trip)
    if total and seat > total:
        return False, f"asiento {seat} excede el total ({total})"

    if available_seats(trip) <= 0:
        return False, "sin asientos disponibles"

    reserved = extract_reserved_seat_numbers(trip)
    if seat in reserved:
        return False, f"asiento {seat} ocupado"

    return True, ""


def choose_seat_number(trip: dict[str, Any]) -> str:
    """
    Regla pedida:
    - intenta el asiento preferido, por ejemplo 8
    - si está ocupado, toma cualquier asiento libre
    - si el backend no devuelve bien el mapa, cae al preferido
    """
    preferred = env_int("TACERCA_PREFERRED_SEAT")
    reserved = extract_reserved_seat_numbers(trip)
    total = extract_total_seat_count(trip)

    if preferred is not None:
        if total and not (1 <= preferred <= total):
            raise RuntimeError(
                f"TACERCA_PREFERRED_SEAT={preferred} excede el total de asientos ({total})."
            )
        if preferred not in reserved:
            return str(preferred)

    if total > 0:
        for seat in range(1, total + 1):
            if seat not in reserved:
                return str(seat)

    if preferred is not None:
        return str(preferred)

    return "8"


def build_snapshot(trips: list[dict[str, Any]]) -> list[dict[str, Any]]:
    snapshot: list[dict[str, Any]] = []
    for trip in trips:
        snapshot.append(
            {
                "trip_id": normalize_trip_id(trip),
                "planning_trip_id": normalize_planning_trip_id(trip),
                "hourInit": trip_departure_hour(trip),
                "hourFinish": trip_arrival_hour(trip),
                "price": int(first_non_empty(trip.get("price"), trip.get("totalPrice"), 0) or 0),
                "available": available_seats(trip),
            }
        )

    snapshot.sort(key=lambda x: (x["hourInit"], x["trip_id"]))
    return snapshot


def build_massive_snapshot(trips: list[dict[str, Any]]) -> list[dict[str, Any]]:
    snapshot: list[dict[str, Any]] = []
    for trip in trips:
        snapshot.append(
            {
                "date": trip_execution_date(trip),
                "trip_id": normalize_trip_id(trip),
                "planning_trip_id": normalize_planning_trip_id(trip),
                "hourInit": trip_departure_hour(trip),
                "hourFinish": trip_arrival_hour(trip),
                "price": int(first_non_empty(trip.get("price"), trip.get("totalPrice"), 0) or 0),
                "available": available_seats(trip),
            }
        )

    snapshot.sort(key=lambda x: (x["date"], x["hourInit"], x["trip_id"]))
    return snapshot


def make_signature(snapshot: list[dict[str, Any]]) -> str:
    compact = [
        {
            "trip_id": item["trip_id"],
            "hourInit": item["hourInit"],
            "available": item["available"],
        }
        for item in snapshot
    ]
    return json.dumps(compact, sort_keys=True, ensure_ascii=False)


def make_massive_signature(snapshot: list[dict[str, Any]]) -> str:
    compact = [
        {
            "date": item["date"],
            "trip_id": item["trip_id"],
            "hourInit": item["hourInit"],
            "available": item["available"],
        }
        for item in snapshot
    ]
    return json.dumps(compact, sort_keys=True, ensure_ascii=False)


def build_full_status_message(snapshot: list[dict[str, Any]], trip_date: str) -> str:
    lines = [
        f"Tacerca {trip_date} - Los Montes -> Escuela Militar",
        "Estado actual por horario:",
    ]

    if not snapshot:
        lines.append("- Sin horarios devueltos por la consulta")
        return "\n".join(lines)

    for item in snapshot:
        status = (
            f"{item['available']} asiento(s) disponibles"
            if item["available"] > 0
            else "Sin asientos disponibles"
        )
        lines.append(
            f"- {item['hourInit']} -> {item['hourFinish']}: {status}, tarifa ${item['price']}"
        )

    return "\n".join(lines)


def build_changes_message(
    old_snapshot: list[dict[str, Any]],
    new_snapshot: list[dict[str, Any]],
    trip_date: str,
) -> str:
    old_map = {item["trip_id"]: item for item in old_snapshot}
    changes: list[str] = []

    for item in new_snapshot:
        old_item = old_map.get(item["trip_id"])
        old_available = old_item["available"] if old_item else None
        new_available = item["available"]

        if old_available != new_available:
            if old_available is None:
                changes.append(
                    f"- {item['hourInit']} -> {item['hourFinish']}: nuevo horario, "
                    f"{new_available} asiento(s) disponibles, tarifa ${item['price']}"
                    if new_available > 0
                    else f"- {item['hourInit']} -> {item['hourFinish']}: nuevo horario, sin asientos disponibles, tarifa ${item['price']}"
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


@dataclass
class MassiveBookingConfig:
    start_date: str
    hour: str
    seat: int
    days: int
    poll_seconds: int
    require_all_days: bool = True

    def __post_init__(self) -> None:
        validate_trip_date(self.start_date)
        self.hour = require_hhmm(self.hour)
        if self.days < 1 or self.days > 7:
            raise ValueError("Compra Masiva permite entre 1 y 7 días.")
        if self.seat < 1:
            raise ValueError("El asiento objetivo debe ser mayor o igual a 1.")
        if self.poll_seconds < 1:
            raise ValueError("La frecuencia debe ser mayor o igual a 1 segundo.")

    @property
    def dates(self) -> list[str]:
        return date_range_strings(self.start_date, self.days)


def build_default_massive_config() -> MassiveBookingConfig:
    poll_default = env_int("TACERCA_POLL_SECONDS", 15) or 15
    return MassiveBookingConfig(
        start_date=env_str("TACERCA_MASSIVE_START_DATE", MASSIVE_DEFAULT_START_DATE),
        hour=env_str("TACERCA_MASSIVE_HOUR", MASSIVE_DEFAULT_HOUR),
        seat=env_int("TACERCA_MASSIVE_SEAT", MASSIVE_DEFAULT_SEAT) or MASSIVE_DEFAULT_SEAT,
        days=env_int("TACERCA_MASSIVE_DAYS", MASSIVE_DEFAULT_DAYS) or MASSIVE_DEFAULT_DAYS,
        poll_seconds=env_int("TACERCA_MASSIVE_POLL_SECONDS", poll_default) or poll_default,
        require_all_days=env_bool("TACERCA_MASSIVE_REQUIRE_ALL_DAYS", True),
    )


@dataclass
class MayBookingBlock:
    key: str
    route_key: str
    route_label: str
    origin_id: str
    destination_id: str
    hour: str
    dates: list[str]

    def __post_init__(self) -> None:
        if not self.origin_id or not self.destination_id:
            raise ValueError(f"Faltan ids de ruta para {self.route_label}.")
        self.hour = require_hhmm(self.hour)
        if not 1 <= len(self.dates) <= 7:
            raise ValueError("Cada bloque debe tener entre 1 y 7 fechas.")
        for trip_date in self.dates:
            validate_trip_date(trip_date)


@dataclass
class May2026PlanConfig:
    seat: int
    poll_seconds: int
    type_payment: str
    blocks: list[MayBookingBlock]

    def __post_init__(self) -> None:
        if self.seat < 1:
            raise ValueError("El asiento objetivo debe ser mayor o igual a 1.")
        if self.poll_seconds < 1:
            raise ValueError("La frecuencia debe ser mayor o igual a 1 segundo.")
        if self.type_payment != "wallet":
            raise ValueError("El plan de mayo debe pagarse contra billetera.")
        if not self.blocks:
            raise ValueError("El plan de mayo no tiene bloques configurados.")

        keys = [block.key for block in self.blocks]
        if len(keys) != len(set(keys)):
            raise ValueError("Hay bloques duplicados en el plan de mayo.")

    @property
    def trigger_block(self) -> MayBookingBlock:
        return self.blocks[0]

    @property
    def total_reservations(self) -> int:
        return sum(len(block.dates) for block in self.blocks)


def build_may2026_plan_config(client: "TacercaClient") -> May2026PlanConfig:
    seat = env_int("TACERCA_MAY_PLAN_SEAT", MAY_PLAN_DEFAULT_SEAT) or MAY_PLAN_DEFAULT_SEAT
    poll_seconds = (
        env_int("TACERCA_MAY_PLAN_POLL_SECONDS", MAY_PLAN_DEFAULT_POLL_SECONDS)
        or MAY_PLAN_DEFAULT_POLL_SECONDS
    )

    outbound_origin_id = client.origin_id
    outbound_destination_id = client.destination_id
    return_origin_id = outbound_destination_id
    return_destination_id = outbound_origin_id

    blocks: list[MayBookingBlock] = []
    for index, dates in enumerate(MAY_PLAN_DATE_BLOCKS, start=1):
        blocks.append(
            MayBookingBlock(
                key=f"ida_bloque_{index}",
                route_key="ida",
                route_label="IDA Los Montes -> Escuela Militar",
                origin_id=outbound_origin_id,
                destination_id=outbound_destination_id,
                hour=MAY_PLAN_OUTBOUND_HOUR,
                dates=list(dates),
            )
        )

    for index, dates in enumerate(MAY_PLAN_DATE_BLOCKS, start=1):
        blocks.append(
            MayBookingBlock(
                key=f"regreso_bloque_{index}",
                route_key="regreso",
                route_label="REGRESO Escuela Militar -> Los Montes",
                origin_id=return_origin_id,
                destination_id=return_destination_id,
                hour=MAY_PLAN_RETURN_HOUR,
                dates=list(dates),
            )
        )

    return May2026PlanConfig(
        seat=seat,
        poll_seconds=poll_seconds,
        type_payment="wallet",
        blocks=blocks,
    )


@dataclass
class ReservationRule:
    exact_hours: list[str]
    hour_from: str | None
    hour_to: str | None
    max_price: int | None
    auto_book: bool
    poll_seconds: int

    def __post_init__(self) -> None:
        self.exact_hours = [require_hhmm(hour) for hour in self.exact_hours]
        self.hour_from = require_hhmm(self.hour_from) if self.hour_from else None
        self.hour_to = require_hhmm(self.hour_to) if self.hour_to else None

    def is_booking_enabled(self) -> bool:
        return bool(self.exact_hours or (self.hour_from and self.hour_to))

    def matches(self, trip: dict[str, Any], require_available: bool = True) -> bool:
        if not self.is_booking_enabled():
            return False

        hour = trip_departure_hour(trip)
        if not hour:
            return False

        if self.max_price is not None:
            price = int(first_non_empty(trip.get("price"), trip.get("totalPrice"), 0) or 0)
            if price > self.max_price:
                return False

        if require_available and available_seats(trip) <= 0:
            return False

        if self.exact_hours:
            return hour in self.exact_hours

        if self.hour_from and self.hour_to:
            value = hhmm_to_minutes(hour)
            return minutes_in_window(value, hhmm_to_minutes(self.hour_from), hhmm_to_minutes(self.hour_to))

        return False

    def score(self, trip: dict[str, Any]) -> tuple[int, int]:
        hour = trip_departure_hour(trip)
        price = int(first_non_empty(trip.get("price"), trip.get("totalPrice"), 0) or 0)

        if self.exact_hours:
            try:
                idx = self.exact_hours.index(hour)
            except ValueError:
                idx = 9999
            return (idx, price)

        if self.hour_from and self.hour_to:
            start = hhmm_to_minutes(self.hour_from)
            end = hhmm_to_minutes(self.hour_to)
            value = hhmm_to_minutes(hour)
            if start <= end:
                center = (start + end) // 2
                distance = abs(value - center)
            else:
                window_length = (24 * 60 - start) + end
                offset = (value - start) % (24 * 60)
                distance = abs(offset - (window_length // 2))
            return (distance, price)

        return (9999, price)


class TacercaClient:
    def __init__(self) -> None:
        self.username = env_str("TACERCA_USERNAME")
        self.password = env_str("TACERCA_PASSWORD")
        if not self.username or not self.password:
            raise RuntimeError("Faltan TACERCA_USERNAME o TACERCA_PASSWORD.")

        self.customer_first_name = env_str("TACERCA_CUSTOMER_FIRST_NAME")
        self.customer_last_name = env_str("TACERCA_CUSTOMER_LAST_NAME")
        self.customer_email = env_str("TACERCA_CUSTOMER_EMAIL", self.username)
        self.customer_phone = env_str("TACERCA_CUSTOMER_PHONE")
        self.customer_rut = env_str("TACERCA_CUSTOMER_RUT")

        self.origin_id = env_str("TACERCA_ORIGIN_ID", DEFAULT_ORIGIN_ID)
        self.destination_id = env_str("TACERCA_DESTINATION_ID", DEFAULT_DESTINATION_ID)
        self.type_payment = env_str("TACERCA_TYPE_PAYMENT", "wallet")
        self.type_trip = env_str("TACERCA_TYPE_TRIP", "justReturn")
        self.massive = env_bool("TACERCA_IS_MASSIVE", False)

        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)

    def login(self) -> str:
        response = self.session.post(
            LOGIN_URL,
            json={
                "username": self.username,
                "password": self.password,
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()

        payload = response.json()
        token = payload.get("data", {}).get("accessToken")
        if not token:
            raise RuntimeError(f"No se encontró accessToken en la respuesta: {payload}")

        self.session.headers["Authorization"] = f"Bearer {token}"
        return token

    def fetch_trips(
        self,
        trip_date: str,
        origin_id: str | None = None,
        destination_id: str | None = None,
    ) -> list[dict[str, Any]]:
        response = self.session.get(
            BOOKING_URL,
            params={
                "origin": origin_id or self.origin_id,
                "destination": destination_id or self.destination_id,
                "date": str(date_to_unix_ms(trip_date)),
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()

        payload = response.json()
        trips = payload.get("data")
        if not isinstance(trips, list):
            raise RuntimeError(f"Respuesta inesperada del backend en get-booking: {payload}")
        return trips

    def fetch_trips_for_dates(
        self,
        trip_dates: list[str],
        origin_id: str | None = None,
        destination_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if not trip_dates:
            return []

        response = self.session.get(
            BOOKING_URL,
            params={
                "origin": origin_id or self.origin_id,
                "destination": destination_id or self.destination_id,
                "date": ",".join(str(date_to_unix_ms_noon(date)) for date in trip_dates),
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()

        payload = response.json()
        trips = payload.get("data")
        if not isinstance(trips, list):
            raise RuntimeError(f"Respuesta inesperada del backend en get-booking masivo: {payload}")
        return trips

    def check_trip_exists(self, active_trip_id: str) -> dict[str, Any]:
        response = self.session.get(
            f"{CHECK_TRIP_EXISTS_URL}/{active_trip_id}",
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return response.json() if response.text.strip() else {}

    def get_customer(self) -> dict[str, Any]:
        if not self.customer_rut:
            return {}
        response = self.session.get(
            f"{GET_CUSTOMER_URL}/{self.customer_rut}",
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return response.json() if response.text.strip() else {}

    def get_payment_methods(self) -> dict[str, Any]:
        response = self.session.get(
            PAYMENT_METHODS_URL,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return response.json() if response.text.strip() else {}

    def build_guest(self, seat_number: str) -> dict[str, Any]:
        missing = [
            name
            for name, value in {
                "TACERCA_CUSTOMER_FIRST_NAME": self.customer_first_name,
                "TACERCA_CUSTOMER_LAST_NAME": self.customer_last_name,
                "TACERCA_CUSTOMER_EMAIL": self.customer_email,
                "TACERCA_CUSTOMER_PHONE": self.customer_phone,
                "TACERCA_CUSTOMER_RUT": self.customer_rut,
            }.items()
            if not value
        ]
        if missing:
            raise RuntimeError("Faltan datos del pasajero para reservar: " + ", ".join(missing))

        return {
            "email": self.customer_email,
            "firstName": self.customer_first_name,
            "lastName": self.customer_last_name,
            "phone": self.customer_phone,
            "rut": self.customer_rut,
            "seating": seat_number,
        }

    def build_booking_payload(self, trip: dict[str, Any]) -> dict[str, Any]:
        active_trip_id = normalize_trip_id(trip)
        planning_trip_id = normalize_planning_trip_id(trip)
        if not active_trip_id:
            raise RuntimeError("No se pudo inferir activeTrip desde get-booking.")
        if not planning_trip_id:
            raise RuntimeError("No se pudo inferir planningTrip desde get-booking.")

        seat_number = choose_seat_number(trip)
        hour_departure = trip_departure_hour(trip)
        hour_arrival = trip_arrival_hour(trip)
        total_price = int(first_non_empty(trip.get("price"), trip.get("totalPrice"), 0) or 0)

        station_origin_name = trip_station_origin_name(trip)
        station_destination_name = trip_station_destination_name(trip)

        return {
            "activeTrip": active_trip_id,
            "hourArrival": hour_arrival,
            "hourDeparture": hour_departure,
            "seating": seat_number,
            "totalPrice": total_price,
            "stationOrigin": station_origin_name,
            "stationDestination": station_destination_name,
            "passengers": [],
            "planningTrip": planning_trip_id,
            "guest": self.build_guest(seat_number),
            "station": infer_station_id(trip),
            "typePayment": self.type_payment,
            "typeTrip": self.type_trip,
            "isMassive": self.massive,
        }

    def build_massive_booking_payload(
        self,
        trips: list[dict[str, Any]],
        seat_number: int,
        station_origin_fallback: str = "Cond. Los Montes",
        station_destination_fallback: str = "Metro Escuela Militar",
    ) -> dict[str, Any]:
        if not trips:
            raise RuntimeError("No hay viajes para armar Compra Masiva.")

        seat = str(seat_number)
        payload_trips: list[dict[str, Any]] = []
        for trip in trips:
            active_trip_id = normalize_trip_id(trip)
            planning_trip_id = normalize_planning_trip_id(trip)
            if not active_trip_id:
                raise RuntimeError("No se pudo inferir activeTrip para Compra Masiva.")
            if not planning_trip_id:
                raise RuntimeError("No se pudo inferir planningTrip para Compra Masiva.")

            payload_trips.append(
                {
                    "activeTrip": active_trip_id,
                    "passengers": [],
                    "planningTrip": planning_trip_id,
                    "seating": seat,
                    "station": infer_station_id(trip),
                    "stationOrigin": trip_station_origin_name(trip, station_origin_fallback),
                    "stationDestination": trip_station_destination_name(
                        trip,
                        station_destination_fallback,
                    ),
                }
            )

        return {
            "trips": payload_trips,
            "paymentEntry": {
                "guest": self.build_guest(seat),
                "typePayment": self.type_payment,
            },
        }

    def create_trip_auth(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.session.post(
            CREATE_TRIP_AUTH_URL,
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        if not response.text.strip():
            return {}
        try:
            return response.json()
        except Exception:
            return {"raw": response.text}

    def create_trip_massive(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.session.post(
            CREATE_TRIP_MASSIVE_URL,
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        if not response.text.strip():
            return {}
        try:
            return response.json()
        except Exception:
            return {"raw": response.text}

    def get_trip(self, booking_code: str) -> dict[str, Any]:
        response = self.session.get(
            f"{GET_TRIP_URL}/{booking_code}",
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()

    def get_massive_trip(self, massive_id: str) -> dict[str, Any]:
        response = self.session.get(
            f"{GET_TRIP_URL}/{massive_id}",
            params={"isMassive": "true"},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()


def extract_booking_code_from_response(data: Any) -> str | None:
    if data is None:
        return None

    if isinstance(data, dict):
        if isinstance(data.get("data"), dict):
            candidate = first_non_empty(
                data["data"].get("code"),
                data["data"].get("bookingCode"),
                data["data"].get("tripCode"),
            )
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()

        candidate = first_non_empty(
            data.get("code") if isinstance(data.get("code"), str) else None,
            data.get("bookingCode"),
            data.get("tripCode"),
        )
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()

        for value in data.values():
            nested = extract_booking_code_from_response(value)
            if nested:
                return nested

    if isinstance(data, list):
        for item in data:
            nested = extract_booking_code_from_response(item)
            if nested:
                return nested

    if isinstance(data, str):
        match = re.search(r"\b[A-Z0-9]{8,16}\b", data)
        if match:
            return match.group(0)

    return None


def extract_massive_id_from_response(data: Any) -> str | None:
    if data is None:
        return None

    if isinstance(data, dict):
        if isinstance(data.get("data"), dict):
            candidate = first_non_empty(
                data["data"].get("massiveId"),
                data["data"].get("_id"),
                data["data"].get("id"),
            )
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()

        candidate = first_non_empty(
            data.get("massiveId"),
            data.get("_id"),
            data.get("id"),
        )
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()

        for value in data.values():
            nested = extract_massive_id_from_response(value)
            if nested:
                return nested

    if isinstance(data, list):
        for item in data:
            nested = extract_massive_id_from_response(item)
            if nested:
                return nested

    return None


def choose_target_trip(
    trips: list[dict[str, Any]],
    rule: ReservationRule,
    require_available: bool = True,
) -> dict[str, Any] | None:
    candidates = [trip for trip in trips if rule.matches(trip, require_available=require_available)]
    if not candidates:
        return None
    candidates.sort(key=rule.score)
    return candidates[0]


def build_booking_success_message(
    trip_date: str,
    booking_code: str | None,
    trip: dict[str, Any],
    verification: dict[str, Any] | None = None,
    already_existing: bool = False,
) -> str:
    hour_departure = trip_departure_hour(trip)
    hour_arrival = trip_arrival_hour(trip)
    price = int(first_non_empty(trip.get("price"), trip.get("totalPrice"), 0) or 0)
    trip_id = normalize_trip_id(trip)
    title = (
        f"Reserva Tacerca existente confirmada para {trip_date}"
        if already_existing
        else f"Reserva Tacerca exitosa para {trip_date}"
    )

    lines = [
        title,
        f"- Horario: {hour_departure} -> {hour_arrival}",
        f"- Trip ID: {trip_id}",
        f"- Tarifa: ${price}",
    ]

    if booking_code:
        lines.append(f"- Código reserva: {booking_code}")

    if verification and isinstance(verification.get("data"), dict):
        status = verification["data"].get("status")
        seat = verification["data"].get("seating")
        if status:
            lines.append(f"- Estado: {status}")
        if seat:
            lines.append(f"- Asiento: {seat}")

    return "\n".join(lines)


def build_booking_failed_message(trip_date: str, trip: dict[str, Any], exc: Exception) -> str:
    hour_departure = trip_departure_hour(trip)
    trip_id = normalize_trip_id(trip)
    return "\n".join(
        [
            f"Reserva Tacerca fallida para {trip_date}",
            f"- Horario candidato: {hour_departure}",
            f"- Trip ID: {trip_id}",
            f"- Error: {exc}",
        ]
    )


def find_trip_for_date_hour(
    trips: list[dict[str, Any]],
    trip_date: str,
    hour: str,
) -> dict[str, Any] | None:
    normalized_hour = require_hhmm(hour)
    candidates = [
        trip
        for trip in trips
        if trip_execution_date(trip) == trip_date and trip_departure_hour(trip) == normalized_hour
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda trip: (available_seats(trip) <= 0, normalize_trip_id(trip)))
    return candidates[0]


def choose_massive_target_trips(
    trips: list[dict[str, Any]],
    config: MassiveBookingConfig,
) -> tuple[list[dict[str, Any]], list[str]]:
    selected: list[dict[str, Any]] = []
    problems: list[str] = []

    for trip_date in config.dates:
        candidates = [
            trip
            for trip in trips
            if trip_execution_date(trip) == trip_date and trip_departure_hour(trip) == config.hour
        ]
        if not candidates:
            problems.append(f"{trip_date}: no existe viaje a las {config.hour}")
            continue

        usable: list[dict[str, Any]] = []
        blocked_reasons: list[str] = []
        for trip in candidates:
            ok, reason = specific_seat_available(trip, config.seat)
            if ok:
                usable.append(trip)
            else:
                blocked_reasons.append(reason)

        if not usable:
            reason = ", ".join(sorted(set(blocked_reasons))) or "sin disponibilidad"
            problems.append(f"{trip_date}: {reason}")
            continue

        usable.sort(key=lambda trip: normalize_trip_id(trip))
        selected.append(usable[0])

    return selected, problems


def build_massive_available_message(config: MassiveBookingConfig, trip: dict[str, Any]) -> str:
    return "\n".join(
        [
            "Tacerca Compra Masiva - asientos disponibles",
            "Ruta: Piedra Roja / Los Montes -> Escuela Militar",
            f"- Fecha gatillo: {config.start_date}",
            f"- Horario: {trip_departure_hour(trip)} -> {trip_arrival_hour(trip)}",
            f"- Disponibles: {available_seats(trip)}",
            f"- Asiento objetivo: {config.seat}",
            f"- Acción: intentar Compra Masiva por {config.days} días",
        ]
    )


def build_massive_blocked_message(config: MassiveBookingConfig, problems: list[str]) -> str:
    lines = [
        "Tacerca Compra Masiva bloqueada",
        f"Ruta: Piedra Roja / Los Montes -> Escuela Militar, {config.hour}, asiento {config.seat}",
        "No se ejecutó la compra porque no están listos todos los días:",
    ]
    lines.extend(f"- {problem}" for problem in problems)
    return "\n".join(lines)


def build_massive_success_message(
    config: MassiveBookingConfig,
    massive_id: str,
    trips: list[dict[str, Any]],
) -> str:
    lines = [
        "Tacerca Compra Masiva exitosa",
        f"- ID masivo: {massive_id}",
        f"- Horario: {config.hour}",
        f"- Asiento: {config.seat}",
        "- Días reservados:",
    ]
    for trip in trips:
        lines.append(f"  {trip_execution_date(trip)} - trip {normalize_trip_id(trip)}")
    return "\n".join(lines)


def build_massive_failed_message(config: MassiveBookingConfig, exc: Exception) -> str:
    return "\n".join(
        [
            "Tacerca Compra Masiva fallida",
            f"- Inicio: {config.start_date}",
            f"- Horario: {config.hour}",
            f"- Asiento: {config.seat}",
            f"- Error: {exc}",
        ]
    )


def format_trip_date_es(date_str: str) -> str:
    weekdays = [
        "lunes",
        "martes",
        "miércoles",
        "jueves",
        "viernes",
        "sábado",
        "domingo",
    ]
    months = {
        1: "enero",
        2: "febrero",
        3: "marzo",
        4: "abril",
        5: "mayo",
        6: "junio",
        7: "julio",
        8: "agosto",
        9: "septiembre",
        10: "octubre",
        11: "noviembre",
        12: "diciembre",
    }
    dt = datetime.strptime(date_str, "%d-%m-%Y")
    return f"{weekdays[dt.weekday()]} {dt.day:02d} de {months[dt.month]} de {dt.year}"


def block_dates_text(block: MayBookingBlock) -> str:
    return ", ".join(format_trip_date_es(date) for date in block.dates)


def may_plan_blocks_state(state: dict[str, Any]) -> dict[str, Any]:
    blocks = state.setdefault("may_plan_blocks", {})
    if not isinstance(blocks, dict):
        state["may_plan_blocks"] = {}
        return state["may_plan_blocks"]
    return blocks


def may_plan_block_state(state: dict[str, Any], block: MayBookingBlock) -> dict[str, Any]:
    blocks = may_plan_blocks_state(state)
    item = blocks.setdefault(block.key, {})
    if not isinstance(item, dict):
        blocks[block.key] = {}
        return blocks[block.key]
    return item


def may_plan_block_done(state: dict[str, Any], block: MayBookingBlock) -> bool:
    blocks = state.get("may_plan_blocks")
    if not isinstance(blocks, dict):
        return False
    item = blocks.get(block.key)
    return isinstance(item, dict) and bool(item.get("done"))


def any_may_plan_block_done(state: dict[str, Any], config: May2026PlanConfig) -> bool:
    return any(may_plan_block_done(state, block) for block in config.blocks)


def all_may_plan_blocks_done(state: dict[str, Any], config: May2026PlanConfig) -> bool:
    return all(may_plan_block_done(state, block) for block in config.blocks)


def choose_plan_target_trips(
    client: TacercaClient,
    trips: list[dict[str, Any]],
    block: MayBookingBlock,
    seat: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    selected: list[dict[str, Any]] = []
    problems: list[str] = []

    for trip_date in block.dates:
        candidates = [
            trip
            for trip in trips
            if trip_execution_date(trip) == trip_date and trip_departure_hour(trip) == block.hour
        ]
        if not candidates:
            problems.append(f"{trip_date}: no existe viaje a las {block.hour}")
            continue

        usable: list[dict[str, Any]] = []
        blocked_reasons: list[str] = []
        for trip in candidates:
            ok, reason = specific_seat_available(trip, seat)
            if ok:
                usable.append(trip)
                continue

            existing_response = client.check_trip_exists(normalize_trip_id(trip))
            if extract_booking_code_from_response(existing_response):
                usable.append(trip)
                continue

            blocked_reasons.append(reason)

        if not usable:
            reason = ", ".join(sorted(set(blocked_reasons))) or "sin disponibilidad"
            problems.append(f"{trip_date}: {reason}")
            continue

        usable.sort(key=lambda trip: normalize_trip_id(trip))
        selected.append(usable[0])

    return selected, problems


def build_may_plan_trigger_available_message(
    config: May2026PlanConfig,
    trip: dict[str, Any],
) -> str:
    block = config.trigger_block
    return "\n".join(
        [
            "Tacerca Plan Mayo - gatillo disponible",
            f"- Ruta: {block.route_label}",
            f"- Fecha gatillo: {format_trip_date_es(block.dates[0])}",
            f"- Horario: {trip_departure_hour(trip)} -> {trip_arrival_hour(trip)}",
            f"- Asiento objetivo: {config.seat}",
            f"- Pago: {config.type_payment}",
            "- Acción: intentar reservas por bloques.",
        ]
    )


def build_may_plan_block_blocked_message(
    config: May2026PlanConfig,
    block: MayBookingBlock,
    problems: list[str],
) -> str:
    lines = [
        "Tacerca Plan Mayo - bloque pendiente",
        f"- Bloque: {block.key}",
        f"- Ruta: {block.route_label}",
        f"- Horario: {block.hour}",
        f"- Asiento: {config.seat}",
        "No se ejecutó este bloque porque:",
    ]
    lines.extend(f"- {problem}" for problem in problems)
    return "\n".join(lines)


def build_may_plan_block_success_message(
    config: May2026PlanConfig,
    block: MayBookingBlock,
    massive_id: str | None,
    trips: list[dict[str, Any]],
    existing_bookings: dict[str, str],
) -> str:
    lines = [
        "Tacerca Plan Mayo - bloque reservado",
        f"- Bloque: {block.key}",
        f"- Ruta: {block.route_label}",
        f"- Horario: {block.hour}",
        f"- Asiento: {config.seat}",
        f"- Pago: {config.type_payment}",
    ]
    if massive_id:
        lines.append(f"- ID masivo: {massive_id}")
    if existing_bookings:
        lines.append(f"- Reservas existentes detectadas: {len(existing_bookings)}")
    lines.append("- Días:")
    for trip in trips:
        trip_id = normalize_trip_id(trip)
        suffix = " existente" if trip_id in existing_bookings else ""
        lines.append(f"  {trip_execution_date(trip)} - trip {trip_id}{suffix}")
    return "\n".join(lines)


def build_may_plan_done_message(config: May2026PlanConfig) -> str:
    return "\n".join(
        [
            "Tacerca Plan Mayo completado",
            f"- Reservas objetivo: {config.total_reservations}",
            f"- Asiento: {config.seat}",
            f"- Pago: {config.type_payment}",
            "- Estado: todos los bloques quedaron registrados.",
        ]
    )


def build_may_plan_failed_message(
    config: May2026PlanConfig,
    block: MayBookingBlock,
    exc: Exception,
) -> str:
    return "\n".join(
        [
            "Tacerca Plan Mayo - reserva fallida",
            f"- Bloque: {block.key}",
            f"- Ruta: {block.route_label}",
            f"- Horario: {block.hour}",
            f"- Asiento: {config.seat}",
            f"- Error: {exc}",
        ]
    )


def attempt_may_plan_block(
    client: TacercaClient,
    config: May2026PlanConfig,
    block: MayBookingBlock,
    trips: list[dict[str, Any]],
    state: dict[str, Any],
    state_file: Path,
) -> None:
    block_state = may_plan_block_state(state, block)
    if block_state.get("done"):
        return

    client.type_payment = config.type_payment
    print(f"[{now_ts()}] Intentando reservar {block.key} ({len(trips)} viaje(s))...")

    existing_bookings: dict[str, str] = {}
    trips_to_book: list[dict[str, Any]] = []
    for trip in trips:
        active_trip_id = normalize_trip_id(trip)
        existing_response = client.check_trip_exists(active_trip_id)
        existing_code = extract_booking_code_from_response(existing_response)
        if existing_code:
            existing_bookings[active_trip_id] = existing_code
        else:
            trips_to_book.append(trip)

    client.get_customer()
    client.get_payment_methods()

    massive_id: str | None = None
    create_response: dict[str, Any] = {}
    verification: dict[str, Any] = {}
    debug_path: Path | None = None

    if trips_to_book:
        station_origin_fallback = (
            "Cond. Los Montes" if block.route_key == "ida" else "Metro Escuela Militar"
        )
        station_destination_fallback = (
            "Metro Escuela Militar" if block.route_key == "ida" else "Cond. Los Montes"
        )
        payload = client.build_massive_booking_payload(
            trips_to_book,
            config.seat,
            station_origin_fallback=station_origin_fallback,
            station_destination_fallback=station_destination_fallback,
        )
        debug_path = Path(f"tacerca_may2026_{block.key}_payload.json")
        debug_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        create_response = client.create_trip_massive(payload)
        massive_id = extract_massive_id_from_response(create_response)
        if not massive_id:
            payment_url = create_response.get("payment_url") if isinstance(create_response, dict) else None
            if payment_url:
                raise RuntimeError(
                    "Tacerca devolvió una URL de pago; la automatización no puede completar ese pago."
                )
            raise RuntimeError(f"No se pudo extraer massiveId desde la respuesta: {create_response}")

        try:
            verification = client.get_massive_trip(massive_id)
        except Exception as exc:
            verification = {"verification_error": str(exc)}

    block_state["done"] = True
    block_state["route"] = block.route_key
    block_state["route_label"] = block.route_label
    block_state["dates"] = block.dates
    block_state["hour"] = block.hour
    block_state["seat"] = config.seat
    block_state["type_payment"] = config.type_payment
    block_state["trip_ids"] = [normalize_trip_id(trip) for trip in trips]
    block_state["existing_bookings"] = existing_bookings
    if massive_id:
        block_state["massive_id"] = massive_id
    if debug_path:
        block_state["payload_path"] = str(debug_path)
    if create_response:
        block_state["response"] = create_response
    if verification:
        block_state["verification"] = verification
    save_state(state_file, state)

    message = build_may_plan_block_success_message(
        config,
        block,
        massive_id,
        trips,
        existing_bookings,
    )
    print(message)
    send_telegram(message)


def attempt_massive_booking(
    client: TacercaClient,
    config: MassiveBookingConfig,
    trips: list[dict[str, Any]],
    state: dict[str, Any],
    state_file: Path,
) -> None:
    if state.get("massive_booking_done"):
        return

    print(f"[{now_ts()}] Intentando Compra Masiva para {len(trips)} día(s)...")

    existing: list[str] = []
    for trip in trips:
        active_trip_id = normalize_trip_id(trip)
        existing_response = client.check_trip_exists(active_trip_id)
        existing_code = extract_booking_code_from_response(existing_response)
        if existing_code:
            existing.append(f"{trip_execution_date(trip)} {config.hour}: {existing_code}")

    if existing:
        raise RuntimeError(
            "Tacerca ya reporta reservas existentes para: " + "; ".join(existing)
        )

    client.get_customer()
    client.get_payment_methods()

    payload = client.build_massive_booking_payload(trips, config.seat)
    debug_path = Path(
        f"tacerca_massive_payload_{config.start_date.replace('-', '')}_{config.days}d.json"
    )
    debug_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    create_response = client.create_trip_massive(payload)
    massive_id = extract_massive_id_from_response(create_response)
    if not massive_id:
        payment_url = create_response.get("payment_url") if isinstance(create_response, dict) else None
        if payment_url:
            raise RuntimeError(
                "Tacerca devolvió una URL de pago; la automatización no puede completar ese pago."
            )
        raise RuntimeError(f"No se pudo extraer massiveId desde la respuesta: {create_response}")

    try:
        verification = client.get_massive_trip(massive_id)
    except Exception as exc:
        verification = {"verification_error": str(exc)}

    state["massive_booking_done"] = True
    state["massive_id"] = massive_id
    state["massive_start_date"] = config.start_date
    state["massive_hour"] = config.hour
    state["massive_seat"] = config.seat
    state["massive_dates"] = config.dates
    state["massive_trip_ids"] = [normalize_trip_id(trip) for trip in trips]
    state["massive_payload_path"] = str(debug_path)
    state["massive_response"] = create_response
    state["massive_verification"] = verification
    save_state(state_file, state)

    message = build_massive_success_message(config, massive_id, trips)
    print(message)
    send_telegram(message)


def booking_data(response: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {}
    data = response.get("data")
    if isinstance(data, dict):
        return data
    return response


def booking_active_trip_id(response: dict[str, Any] | None) -> str:
    data = booking_data(response)
    active_trip = data.get("activeTrip")
    if isinstance(active_trip, dict):
        return str(first_non_empty(active_trip.get("_id"), active_trip.get("id"), ""))
    return str(active_trip or "")


def booking_departure_hour(response: dict[str, Any] | None) -> str:
    data = booking_data(response)
    return normalize_hhmm(first_non_empty(data.get("hourDeparture"), data.get("hourInit"))) or ""


def validate_booking_matches_trip(verification: dict[str, Any] | None, trip: dict[str, Any]) -> None:
    expected_active_trip_id = normalize_trip_id(trip)
    expected_hour = trip_departure_hour(trip)
    actual_active_trip_id = booking_active_trip_id(verification)
    actual_hour = booking_departure_hour(verification)

    mismatches: list[str] = []
    if actual_active_trip_id and expected_active_trip_id and actual_active_trip_id != expected_active_trip_id:
        mismatches.append(
            f"activeTrip confirmado {actual_active_trip_id}, esperado {expected_active_trip_id}"
        )
    if expected_hour and not actual_hour:
        mismatches.append(f"Tacerca no devolvió hora confirmada; esperada {expected_hour}")
    if actual_hour and expected_hour and actual_hour != expected_hour:
        mismatches.append(f"hora confirmada {actual_hour}, esperada {expected_hour}")

    if mismatches:
        raise RuntimeError(
            "La reserva confirmada por Tacerca no coincide con el horario candidato: "
            + "; ".join(mismatches)
            + ". No se marcará booking_done."
        )


def rule_matches_hour(rule: ReservationRule, hour: str) -> bool:
    normalized_hour = normalize_hhmm(hour)
    if not normalized_hour:
        return False

    if rule.exact_hours:
        return normalized_hour in rule.exact_hours

    if rule.hour_from and rule.hour_to:
        value = hhmm_to_minutes(normalized_hour)
        return minutes_in_window(value, hhmm_to_minutes(rule.hour_from), hhmm_to_minutes(rule.hour_to))

    return False


def state_booking_hour(state: dict[str, Any]) -> str:
    verification = state.get("booking_verification")
    verified_hour = booking_departure_hour(verification if isinstance(verification, dict) else None)
    stored_hour = normalize_hhmm(state.get("booking_hour")) or ""
    return verified_hour or stored_hour


def state_booking_matches_rule(state: dict[str, Any], rule: ReservationRule) -> bool:
    if not state.get("booking_done"):
        return False
    if not rule.is_booking_enabled():
        return True
    return rule_matches_hour(rule, state_booking_hour(state))


def attempt_booking(
    client: TacercaClient,
    trip_date: str,
    trip: dict[str, Any],
    state: dict[str, Any],
    state_file: Path,
) -> None:
    if state.get("booking_done"):
        return

    active_trip_id = normalize_trip_id(trip)
    print(f"[{now_ts()}] Intentando reservar trip {active_trip_id}...")

    existing_response = client.check_trip_exists(active_trip_id)
    existing_code = extract_booking_code_from_response(existing_response)
    if existing_code:
        verification = client.get_trip(existing_code)
        validate_booking_matches_trip(verification, trip)

        state["booking_done"] = True
        state["booking_existing"] = True
        state.pop("booking_done_ignored", None)
        state["booking_trip_id"] = active_trip_id
        state["booking_hour"] = trip_departure_hour(trip)
        state["booking_code"] = existing_code
        state["booking_response"] = existing_response
        state["booking_verification"] = verification
        save_state(state_file, state)

        message = build_booking_success_message(
            trip_date,
            existing_code,
            trip,
            verification,
            already_existing=True,
        )
        print(message)
        send_telegram(message)
        return

    if available_seats(trip) <= 0:
        print(
            f"[{now_ts()}] No hay asientos disponibles para {trip_departure_hour(trip)} "
            "y Tacerca no reportó una reserva existente para ese activeTrip."
        )
        return

    client.get_customer()
    client.get_payment_methods()

    payload = client.build_booking_payload(trip)
    debug_path = Path(f"tacerca_booking_payload_{trip_date.replace('-', '')}.json")
    debug_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    create_response = client.create_trip_auth(payload)
    booking_code = extract_booking_code_from_response(create_response)
    if not booking_code:
        raise RuntimeError(f"No se pudo extraer código de reserva desde la respuesta: {create_response}")

    verification = client.get_trip(booking_code)
    validate_booking_matches_trip(verification, trip)

    state["booking_done"] = True
    state.pop("booking_existing", None)
    state.pop("booking_done_ignored", None)
    state["booking_trip_id"] = active_trip_id
    state["booking_hour"] = trip_departure_hour(trip)
    state["booking_code"] = booking_code
    state["booking_payload_path"] = str(debug_path)
    state["booking_response"] = create_response
    state["booking_verification"] = verification
    save_state(state_file, state)

    message = build_booking_success_message(trip_date, booking_code, trip, verification)
    print(message)
    send_telegram(message)


def run_check(
    client: TacercaClient,
    trip_date: str,
    state_file: Path,
    rule: ReservationRule,
) -> None:
    state = load_state(state_file)
    old_signature = state.get("last_signature")
    old_snapshot = state.get("last_snapshot", [])
    booking_state_was_ignored = False

    trips = client.fetch_trips(trip_date)
    snapshot = build_snapshot(trips)
    new_signature = make_signature(snapshot)

    if old_signature is None:
        message = build_full_status_message(snapshot, trip_date)
        print(f"[{now_ts()}] Primera ejecución")
        print(message)
        send_telegram(message)
        state["last_signature"] = new_signature
        state["last_snapshot"] = snapshot
        save_state(state_file, state)
    elif new_signature != old_signature:
        message = build_changes_message(old_snapshot, snapshot, trip_date)
        if message:
            print(f"[{now_ts()}] Cambio detectado")
            print(message)
            send_telegram(message)
        state["last_signature"] = new_signature
        state["last_snapshot"] = snapshot
        save_state(state_file, state)
    else:
        print(f"[{now_ts()}] Sin cambios. No se envió notificación.")

    if state.get("booking_done"):
        if state_booking_matches_rule(state, rule):
            print(f"[{now_ts()}] Ya existe booking_done=true. No se intentará reservar de nuevo.")
            return

        previous_hour = state_booking_hour(state) or "sin hora verificada"
        print(
            f"[{now_ts()}] booking_done previo no coincide con la regla actual "
            f"(hora guardada/verificada: {previous_hour}). Se intentará reservar de nuevo."
        )
        state["booking_done"] = False
        state["booking_done_ignored"] = {
            "timestamp": now_ts(),
            "reason": "booking_done no coincide con la regla horaria actual",
            "hour": previous_hour,
        }
        save_state(state_file, state)
        booking_state_was_ignored = True

    if not rule.is_booking_enabled():
        return

    target_trip = choose_target_trip(trips, rule)
    if not target_trip:
        existing_target_trip = choose_target_trip(trips, rule, require_available=False)
        if booking_state_was_ignored and rule.auto_book and existing_target_trip:
            try:
                attempt_booking(client, trip_date, existing_target_trip, state, state_file)
            except Exception as exc:
                message = build_booking_failed_message(trip_date, existing_target_trip, exc)
                print(message, file=sys.stderr)
                send_telegram(message)
                state["last_booking_error"] = {
                    "timestamp": now_ts(),
                    "trip_id": normalize_trip_id(existing_target_trip),
                    "error": str(exc),
                }
                save_state(state_file, state)
                raise
        return

    dep = trip_departure_hour(target_trip)
    avail = available_seats(target_trip)
    price = int(first_non_empty(target_trip.get("price"), target_trip.get("totalPrice"), 0) or 0)
    print(f"[{now_ts()}] Candidato encontrado: salida {dep}, disponibles={avail}, tarifa=${price}")

    if not rule.auto_book:
        msg = (
            f"Tacerca {trip_date} - candidato encontrado pero auto-book desactivado\n"
            f"- Horario: {dep}\n"
            f"- Disponibles: {avail}\n"
            f"- Tarifa: ${price}"
        )
        send_telegram(msg)
        return

    try:
        attempt_booking(client, trip_date, target_trip, state, state_file)
    except Exception as exc:
        message = build_booking_failed_message(trip_date, target_trip, exc)
        print(message, file=sys.stderr)
        send_telegram(message)
        state["last_booking_error"] = {
            "timestamp": now_ts(),
            "trip_id": normalize_trip_id(target_trip),
            "error": str(exc),
        }
        save_state(state_file, state)
        raise


def run_massive_check(
    client: TacercaClient,
    config: MassiveBookingConfig,
    state_file: Path,
) -> None:
    state = load_state(state_file)
    if state.get("massive_booking_done"):
        print(f"[{now_ts()}] Compra Masiva ya registrada. No se intentará de nuevo.")
        return

    dates = config.dates
    trips = client.fetch_trips_for_dates(dates)
    snapshot = build_massive_snapshot(trips)
    new_signature = make_massive_signature(snapshot)

    if state.get("last_massive_signature") != new_signature:
        print(f"[{now_ts()}] Estado masivo actualizado para {dates[0]} -> {dates[-1]}")
        state["last_massive_signature"] = new_signature
        state["last_massive_snapshot"] = snapshot
        save_state(state_file, state)
    else:
        print(f"[{now_ts()}] Sin cambios en ventana masiva.")

    trigger_trip = find_trip_for_date_hour(trips, config.start_date, config.hour)
    if not trigger_trip or available_seats(trigger_trip) <= 0:
        print(
            f"[{now_ts()}] Sin asientos disponibles el {config.start_date} "
            f"a las {config.hour}."
        )
        return

    alert_signature = json.dumps(
        {
            "trip_id": normalize_trip_id(trigger_trip),
            "available": available_seats(trigger_trip),
            "seat": config.seat,
        },
        sort_keys=True,
    )
    if state.get("last_massive_available_alert") != alert_signature:
        message = build_massive_available_message(config, trigger_trip)
        print(message)
        send_telegram(message)
        state["last_massive_available_alert"] = alert_signature
        save_state(state_file, state)

    selected_trips, problems = choose_massive_target_trips(trips, config)
    if problems and config.require_all_days:
        problem_signature = json.dumps(problems, ensure_ascii=False, sort_keys=True)
        if state.get("last_massive_problem_signature") != problem_signature:
            message = build_massive_blocked_message(config, problems)
            print(message)
            send_telegram(message)
            state["last_massive_problem_signature"] = problem_signature
            save_state(state_file, state)
        return

    if len(selected_trips) != config.days:
        message = build_massive_blocked_message(
            config,
            [f"se seleccionaron {len(selected_trips)} de {config.days} días requeridos"],
        )
        print(message)
        send_telegram(message)
        state["last_massive_problem_signature"] = message
        save_state(state_file, state)
        return

    try:
        attempt_massive_booking(client, config, selected_trips, state, state_file)
    except Exception as exc:
        message = build_massive_failed_message(config, exc)
        print(message, file=sys.stderr)
        send_telegram(message)
        state["last_massive_booking_error"] = {
            "timestamp": now_ts(),
            "error": str(exc),
            "trip_ids": [normalize_trip_id(trip) for trip in selected_trips],
        }
        save_state(state_file, state)
        raise


def run_may2026_plan_check(
    client: TacercaClient,
    config: May2026PlanConfig,
    state_file: Path,
) -> None:
    state = load_state(state_file)
    client.type_payment = config.type_payment

    if state.get("may_plan_done"):
        print(f"[{now_ts()}] Plan Mayo ya completado. No se intentará de nuevo.")
        return

    if not any_may_plan_block_done(state, config):
        trigger_block = config.trigger_block
        trigger_date = trigger_block.dates[0]
        trigger_trips = client.fetch_trips_for_dates(
            [trigger_date],
            trigger_block.origin_id,
            trigger_block.destination_id,
        )
        trigger_trip = find_trip_for_date_hour(trigger_trips, trigger_date, trigger_block.hour)
        if not trigger_trip:
            print(
                f"[{now_ts()}] Gatillo no disponible: {trigger_date} "
                f"{trigger_block.hour} ({trigger_block.route_label})."
            )
            return

        ok, reason = specific_seat_available(trigger_trip, config.seat)
        if not ok:
            existing_response = client.check_trip_exists(normalize_trip_id(trigger_trip))
            if not extract_booking_code_from_response(existing_response):
                print(
                    f"[{now_ts()}] Gatillo encontrado, pero no utilizable: "
                    f"{trigger_date} {trigger_block.hour}, {reason}."
                )
                return

        alert_signature = json.dumps(
            {
                "trip_id": normalize_trip_id(trigger_trip),
                "available": available_seats(trigger_trip),
                "seat": config.seat,
            },
            sort_keys=True,
        )
        if state.get("may_plan_trigger_alert_signature") != alert_signature:
            message = build_may_plan_trigger_available_message(config, trigger_trip)
            print(message)
            send_telegram(message)
            state["may_plan_trigger_alert_signature"] = alert_signature
            save_state(state_file, state)

    for block in config.blocks:
        if may_plan_block_done(state, block):
            continue

        trips = client.fetch_trips_for_dates(
            block.dates,
            block.origin_id,
            block.destination_id,
        )
        snapshot = build_massive_snapshot(trips)
        signature = make_massive_signature(snapshot)
        block_state = may_plan_block_state(state, block)
        if block_state.get("last_signature") != signature:
            print(f"[{now_ts()}] Estado actualizado para {block.key}.")
            block_state["last_signature"] = signature
            block_state["last_snapshot"] = snapshot
            save_state(state_file, state)
        else:
            print(f"[{now_ts()}] Sin cambios para {block.key}.")

        selected_trips, problems = choose_plan_target_trips(client, trips, block, config.seat)
        if problems:
            problem_signature = json.dumps(problems, ensure_ascii=False, sort_keys=True)
            if block_state.get("last_problem_signature") != problem_signature:
                message = build_may_plan_block_blocked_message(config, block, problems)
                print(message)
                send_telegram(message)
                block_state["last_problem_signature"] = problem_signature
                save_state(state_file, state)
            return

        if len(selected_trips) != len(block.dates):
            message = build_may_plan_block_blocked_message(
                config,
                block,
                [f"se seleccionaron {len(selected_trips)} de {len(block.dates)} fechas requeridas"],
            )
            print(message)
            send_telegram(message)
            block_state["last_problem_signature"] = message
            save_state(state_file, state)
            return

        try:
            attempt_may_plan_block(client, config, block, selected_trips, state, state_file)
        except Exception as exc:
            message = build_may_plan_failed_message(config, block, exc)
            print(message, file=sys.stderr)
            send_telegram(message)
            block_state["last_error"] = {
                "timestamp": now_ts(),
                "error": str(exc),
                "trip_ids": [normalize_trip_id(trip) for trip in selected_trips],
            }
            save_state(state_file, state)
            raise

    if all_may_plan_blocks_done(state, config):
        state["may_plan_done"] = True
        state["may_plan_completed_at"] = now_ts()
        save_state(state_file, state)
        message = build_may_plan_done_message(config)
        print(message)
        send_telegram(message)


def print_may2026_plan_config_summary(config: May2026PlanConfig) -> None:
    print()
    print("Validación inicial - Plan Mayo 2026")
    print("Este flujo puede crear reservas reales en Tacerca.")
    print(f"Frecuencia: cada {config.poll_seconds} segundo(s)")
    print(f"Pago: {config.type_payment}")
    print(f"Asiento objetivo: {config.seat}")
    print(f"Total de reservas objetivo: {config.total_reservations}")
    print("Ruta ida: Los Montes, Piedra Roja -> Escuela Militar, 06:54")
    print("Ruta regreso: Escuela Militar -> Los Montes, Piedra Roja, 19:00")
    print("Bloques:")
    for block in config.blocks:
        print(f"- {block.key}: {block.route_label}, {block.hour}, {len(block.dates)} fecha(s)")
        print(f"  {block_dates_text(block)}")
    print()


def ask_may2026_plan_confirmation(
    client: TacercaClient,
    config: May2026PlanConfig,
) -> bool:
    client.build_guest(str(config.seat))
    print_may2026_plan_config_summary(config)
    answer = input("Escribe SI para aceptar estas condiciones y ejecutar el plan: ").strip().lower()
    return answer in {"si", "sí", "s", "yes", "y"}


def print_config_summary(trip_date: str, rule: ReservationRule) -> None:
    print()
    print(f"Monitoreando Tacerca para la fecha {trip_date}")
    print("Ruta fija: Los Montes -> Escuela Militar")
    print(f"Frecuencia: cada {rule.poll_seconds} segundo(s)")

    if rule.exact_hours:
        print(f"Horarios objetivo: {', '.join(rule.exact_hours)}")
    elif rule.hour_from and rule.hour_to:
        print(f"Ventana objetivo: {rule.hour_from} -> {rule.hour_to}")
    else:
        print("Modo reserva: OFF (sin regla horaria). Se mantendrá sólo el monitoreo.")

    if rule.max_price is not None:
        print(f"Precio máximo: ${rule.max_price}")

    print(f"Auto reserva: {'ON' if rule.auto_book else 'OFF'}")
    print("Presiona Ctrl+C para detener.")
    print()


def print_massive_config_summary(config: MassiveBookingConfig) -> None:
    dates = config.dates
    print()
    print("Monitoreando Tacerca Compra Masiva")
    print("Ruta fija: Piedra Roja / Los Montes -> Escuela Militar")
    print(f"Fecha gatillo: {config.start_date}")
    print(f"Rango Compra Masiva: {dates[0]} -> {dates[-1]} ({config.days} días)")
    print(f"Horario objetivo: {config.hour}")
    print(f"Asiento objetivo: {config.seat}")
    print(f"Frecuencia: cada {config.poll_seconds} segundo(s)")
    print("Presiona Ctrl+C para detener.")
    print()


def main() -> int:
    try:
        mode = ask_run_mode()
        client = TacercaClient()

        if mode == "3":
            config = build_may2026_plan_config(client)
            if not ask_may2026_plan_confirmation(client, config):
                print("Plan Mayo cancelado por el usuario.")
                return 0

            client.login()
            state_file = state_file_for_may2026_plan()
            print("Plan Mayo aceptado. Presiona Ctrl+C para detener.")
            print()

            while True:
                try:
                    run_may2026_plan_check(client, config, state_file)
                except requests.HTTPError as exc:
                    print(f"HTTP error: {exc}", file=sys.stderr)
                    if exc.response is not None:
                        print(exc.response.text[:2000], file=sys.stderr)
                    print()
                except Exception as exc:
                    print(f"Error: {exc}", file=sys.stderr)
                    print()

                time.sleep(config.poll_seconds)

        client.login()

        if mode == "2":
            config = build_default_massive_config()
            state_file = state_file_for_massive(config)
            print_massive_config_summary(config)

            while True:
                try:
                    run_massive_check(client, config, state_file)
                except requests.HTTPError as exc:
                    print(f"HTTP error: {exc}", file=sys.stderr)
                    if exc.response is not None:
                        print(exc.response.text[:2000], file=sys.stderr)
                    print()
                except Exception as exc:
                    print(f"Error: {exc}", file=sys.stderr)
                    print()

                time.sleep(config.poll_seconds)

        trip_date = ask_trip_date()
        state_file = state_file_for_date(trip_date)
        rule = ask_target_rule()
        print_config_summary(trip_date, rule)

        while True:
            try:
                run_check(client, trip_date, state_file, rule)
            except requests.HTTPError as exc:
                print(f"HTTP error: {exc}", file=sys.stderr)
                if exc.response is not None:
                    print(exc.response.text[:2000], file=sys.stderr)
                print()
            except Exception as exc:
                print(f"Error: {exc}", file=sys.stderr)
                print()

            time.sleep(rule.poll_seconds)

    except KeyboardInterrupt:
        print("\nMonitoreo detenido por el usuario.")
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
