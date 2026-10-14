import argparse
from collections import defaultdict
from datetime import date, datetime, time, timedelta
import json
import math
import os
from pathlib import Path
import re
from zoneinfo import ZoneInfo

import requests


WARSAW = ZoneInfo("Europe/Warsaw")

GROUPS = {
    "Solar": "PV",
    "Fossil hard coal": "Węgiel kamienny",
    "Fossil brown coal / lignite": "Węgiel brunatny",
    "Fossil gas": "Gaz",
    "Wind onshore": "Wiatr lądowy",
    "Wind offshore": "Wiatr morski",
    "Biomass": "Biomasa",
    "Hydro Run-of-River": "Woda",
    "Hydro water reservoir": "Woda",
    "Hydro pumped storage": "Woda",
    "Battery": "Pozostałe",
    "Fossil oil": "Pozostałe",
    "Fossil coal-derived gas": "Pozostałe",
    "Others": "Pozostałe",
    "Other renewables": "Pozostałe",
}

EXCLUDED = {
    "Hydro pumped storage consumption",
    "Battery Consumption",
    "Cross border electricity trading",
    "Load",
    "Residual load",
    "Renewable share of load",
    "Renewable share of generation",
}

REQUIRED = {
    "Solar",
    "Fossil hard coal",
    "Fossil brown coal / lignite",
    "Fossil gas",
    "Wind onshore",
}


def bounds(day):
    start = datetime.combine(day, time.min, WARSAW)
    end = datetime.combine(
        day + timedelta(days=1),
        time.min,
        WARSAW,
    )
    return start, end


def fetch(day):
    start, end = bounds(day)

    response = requests.get(
        "https://api.energy-charts.info/public_power",
        params={
            "country": "pl",
            "start": start.isoformat(),
            "end": end.isoformat(),
        },
        timeout=90,
    )
    response.raise_for_status()

    return response.json()


def aggregate(payload, day):
    if payload.get("deprecated"):
        raise ValueError(
            "Endpoint oznaczony jako deprecated. "
            "Sprawdź dokumentację API."
        )

    start, end = bounds(day)
    lo = int(start.timestamp())
    hi = int(end.timestamp())

    stamps = payload["unix_seconds"]

    if stamps != sorted(set(stamps)):
        raise ValueError(
            "Powtórzone lub nieuporządkowane znaczniki czasu."
        )

    indices = [
        i
        for i, timestamp in enumerate(stamps)
        if lo <= timestamp < hi
    ]

    # 96 kwadransów zwykle, 92 lub 100 przy zmianie czasu.
    expected = list(range(lo, hi, 900))

    if [stamps[i] for i in indices] != expected:
        raise ValueError(
            "Niekompletna doba lub zmieniona rozdzielczość API. "
            "Post pominięty."
        )

    rows = payload["production_types"]
    names = [row["name"] for row in rows]

    if len(names) != len(set(names)):
        raise ValueError("Powtórzona kategoria w API.")

    unknown = set(names) - GROUPS.keys() - EXCLUDED

    if unknown:
        raise ValueError(
            f"Nowe kategorie wymagają sprawdzenia: {sorted(unknown)}"
        )

    missing = REQUIRED - set(names)

    if missing:
        raise ValueError(
            f"Brak podstawowych źródeł: {sorted(missing)}"
        )

    energy = defaultdict(float)

    for row in rows:
        name = row["name"]

        if name in EXCLUDED:
            continue

        values = row["data"]

        if len(values) != len(stamps):
            raise ValueError(
                f"Nieprawidłowa długość serii: {name}"
            )

        for i in indices:
            value = values[i]

            if (
                not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(
                    f"Brak lub błędna wartość: {name}, {stamps[i]}"
                )

            # Średnia moc MW × 0,25 h = energia MWh.
            energy[GROUPS[name]] += value * 0.25

    if sum(energy.values()) <= 0:
        raise ValueError("Brak produkcji.")

    return dict(energy)


def x_length(text):
    # X liczy każdy link jako 23 znaki.
    # Pozostały tekst w tym formacie używa znaków łacińskich.
    return len(
        re.sub(r"https?://[^\s]+", "x" * 23, text)
    )


def make_post(energy, day):
    total = sum(energy.values())

    exact = {
        name: 100 * value / total
        for name, value in energy.items()
        if value > 0
    }

    # Zaokrąglenie metodą największych reszt:
    # suma prezentowanych udziałów wynosi dokładnie 100%.
    rounded = {
        name: math.floor(value)
        for name, value in exact.items()
    }

    remainder_order = sorted(
        exact,
        key=lambda name: (
            -(exact[name] - rounded[name]),
            name,
        ),
    )

    points_to_add = 100 - sum(rounded.values())

    for name in remainder_order[:points_to_add]:
        rounded[name] += 1

    lines = [
        (
            "Miks produkcji energii elektrycznej w Polsce "
            f"({day:%d.%m.%Y})"
        ),
        "",
    ]

    # Malejąco według produkcji przed zaokrągleniem.
    production_order = sorted(
        exact,
        key=lambda name: (-energy[name], name),
    )

    for name in production_order:
        lines.append(f"{name}: {rounded[name]}%")

    lines.extend([
        "",
        "Źródło: https://energy-charts.info/",
        "https://creativecommons.org/licenses/by/4.0/",
        "Obliczenia własne.",
    ])

    text = "\n".join(lines)
    length = x_length(text)

    if length > 280:
        raise ValueError(
            f"Post ma {length} znaków i przekracza limit 280."
        )

    return text


def publish(text):
    token = os.environ["BUFFER_TOKEN"]
    channel_id = os.environ["BUFFER_CHANNEL_ID"]

    # json.dumps zabezpiecza tekst i znaki nowej linii
    # przy wstawianiu do zapytania GraphQL.
    query = """
    mutation {
      createPost(
        input: {
          text: %s
          channelId: %s
          schedulingType: automatic
          mode: addToQueue
        }
      ) {
        ... on PostActionSuccess {
          post {
            id
          }
        }
        ... on MutationError {
          message
        }
      }
    }
    """ % (
        json.dumps(text),
        json.dumps(channel_id),
    )

    response = requests.post(
        "https://api.buffer.com/graphql",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={"query": query},
        timeout=60,
    )
    response.raise_for_status()

    data = response.json()

    if data.get("errors"):
        raise RuntimeError(
            "Buffer GraphQL: "
            + json.dumps(data["errors"], ensure_ascii=False)
        )

    result = (
        (data.get("data") or {}).get("createPost") or {}
    )
    post_id = (result.get("post") or {}).get("id")

    if not post_id:
        message = result.get("message", "brak ID posta")
        raise RuntimeError(
            f"Buffer nie potwierdził dodania posta: {message}"
        )

    return post_id


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        help="Opcjonalna data danych, np. 2026-10-13",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Dodaj post do kolejki Buffera",
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="Lokalny plik JSON zamiast pobierania z API",
    )

    args = parser.parse_args()

    today = datetime.now(WARSAW).date()
    day = args.date or today - timedelta(days=1)

    if day >= today:
        raise ValueError("Wybierz zakończoną dobę.")

    marker = Path(".mix-state") / f"{day}.json"

    if args.publish and marker.exists():
        print("Ten dzień został już dodany do Buffera.")
        return

    if args.input:
        payload = json.loads(
            args.input.read_text(encoding="utf-8")
        )
    else:
        payload = fetch(day)

    energy = aggregate(payload, day)
    text = make_post(energy, day)

    print(text)
    print(
        f"\nKontrola: {sum(energy.values()) / 1000:.3f} GWh; "
        f"{x_length(text)} znaków według limitu X."
    )

    if not args.publish:
        print(
            "PODGLĄD — nic nie wysłano. "
            "Użyj --publish, aby dodać do kolejki."
        )
        return

    post_id = publish(text)

    marker.parent.mkdir(exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "date": str(day),
                "post_id": post_id,
                "text": text,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print("Dodano do kolejki Buffera. ID:", post_id)


if __name__ == "__main__":
    main()
