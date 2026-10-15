
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path

import requests


# =========================
# CONFIGURATION
# =========================

EIA_API_KEY = os.environ["EIA_API_KEY"]
BUFFER_TOKEN = os.environ["BUFFER_TOKEN"]
BUFFER_CHANNEL_ID = os.environ["BUFFER_CHANNEL_ID"]

API_URL = (
    "https://api.eia.gov/v2/electricity/"
    "rto/daily-fuel-type-data/data/"
)

BUFFER_URL = "https://api.buffer.com/graphql"

STATE_FILE = Path("eia_last_published.json")

# True = test without publishing
# False = send post to Buffer
DRY_RUN = os.environ.get(
    "DRY_RUN", "true"
).lower() != "false"


# =========================
# FUEL CATEGORIES
# =========================

FUEL_NAMES = {
    "NG": "Gaz ziemny",
    "NUC": "Energia jądrowa",
    "COL": "Węgiel",
    "WND": "Wiatr",
    "SUN": "PV",
    "WAT": "Woda",
    "OIL": "Ropa i produkty naftowe",
    "OTH": "Pozostałe",
}

EXPECTED = set(FUEL_NAMES)


# =========================
# EIA API
# =========================

def fetch_latest():

    today = datetime.now(timezone.utc).date()

    params = {
        "api_key": EIA_API_KEY,
        "frequency": "daily",
        "data[0]": "value",
        "facets[respondent][]": "US48",
        "start": (
            today - timedelta(days=10)
        ).isoformat(),
        "end": (
            today - timedelta(days=1)
        ).isoformat(),
        "length": "5000",
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
    }

    print("Connecting to EIA API...")

    response = requests.get(
        API_URL,
        params=params,
        timeout=40,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
    )

    print("EIA HTTP status:", response.status_code)
    print(
        "Content-Type:",
        response.headers.get("Content-Type")
    )

    if response.status_code != 200:

        print(
            "EIA response:",
            response.text[:1500]
        )

        response.raise_for_status()

    payload = response.json()

    if payload.get("error"):
        raise RuntimeError(
            f"EIA API error: {payload['error']}"
        )

    rows = payload.get(
        "response", {}
    ).get("data", [])

    print("Rows received:", len(rows))

    if not rows:
        raise RuntimeError(
            "EIA returned no data"
        )

    by_date = {}

    for row in rows:

        date = row["period"][:10]

        if row.get("respondent") != "US48":
            continue

        fuel = row.get("fueltype")

        if fuel not in EXPECTED:
            raise RuntimeError(
                f"Unexpected fuel type: {fuel}. "
                "Check EIA classification."
            )

        raw_value = row.get("value")

        if raw_value is None:
            continue

        value = Decimal(str(raw_value))

        if not value.is_finite():
            raise RuntimeError(
                f"Invalid value for {fuel}"
            )

        if value < 0:
            raise RuntimeError(
                f"Negative generation: {fuel}. "
                "Manual verification required."
            )

        daily = by_date.setdefault(date, {})

        if fuel in daily:
            raise RuntimeError(
                f"Duplicate fuel {fuel} on {date}"
            )

        daily[fuel] = value

    print("Available dates:", sorted(by_date))

    # Select latest complete reporting day
    for date in sorted(by_date, reverse=True):

        values = by_date[date]

        if set(values) != EXPECTED:
            print(
                f"Skipping incomplete date {date}: "
                f"{sorted(values)}"
            )
            continue

        total = sum(values.values())

        if total <= 0:
            continue

        age = (
            today
            - datetime.fromisoformat(date).date()
        ).days

        if age > 4:
            raise RuntimeError(
                f"EIA data too old: {date}"
            )

        print("Selected date:", date)
        print("Generation values:", values)

        return date, values

    raise RuntimeError(
        "No complete recent EIA dataset found. "
        "Check fuel categories and available data."
    )


# =========================
# PERCENTAGES
# =========================

def percentages(values):

    total = sum(values.values())

    if total <= 0:
        raise RuntimeError(
            "Total generation must be positive"
        )

    exact = {
        fuel: value * 100 / total
        for fuel, value in values.items()
    }

    rounded = {
        fuel: int(
            percentage.to_integral_value(
                rounding=ROUND_FLOOR
            )
        )
        for fuel, percentage in exact.items()
    }

    missing = 100 - sum(rounded.values())

    ranking = sorted(
        values,
        key=lambda fuel: (
            exact[fuel] - rounded[fuel],
            exact[fuel],
            fuel
        ),
        reverse=True
    )

    for fuel in ranking[:missing]:
        rounded[fuel] += 1

    return rounded


# =========================
# POST FORMAT
# =========================

def format_post(date, values):

    shares = percentages(values)

    ordered = sorted(
        shares,
        key=lambda fuel: (
            -shares[fuel],
            FUEL_NAMES[fuel]
        )
    )

    nice_date = datetime.fromisoformat(
        date
    ).strftime("%d.%m.%Y")

    lines = [
        "Miks produkcji energii elektrycznej "
        f"w USA ({nice_date})",
        ""
    ]

    for fuel in ordered:

        name = FUEL_NAMES[fuel]
        share = shares[fuel]

        lines.append(
            f"{name}: {share}%"
        )

    lines.extend([
        "",
        "Źródło: U.S. EIA (EIA-930)",
        "Obliczenia własne."
    ])

    post = "\n".join(lines)

    if len(post) > 280:
        raise RuntimeError(
            f"Post too long: {len(post)} characters"
        )

    return post


# =========================
# BUFFER
# =========================

def send_to_buffer(text):

    mutation = """
    mutation CreatePost($input: CreatePostInput!) {
        createPost(input: $input) {
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
    """

    variables = {
        "input": {
            "text": text,
            "channelId": BUFFER_CHANNEL_ID,
            "schedulingType": "automatic",
            "mode": "addToQueue"
        }
    }

    response = requests.post(
        BUFFER_URL,
        headers={
            "Authorization": f"Bearer {BUFFER_TOKEN}",
            "Content-Type": "application/json"
        },
        json={
            "query": mutation,
            "variables": variables
        },
        timeout=40
    )

    response.raise_for_status()

    result = response.json()

    if result.get("errors"):
        raise RuntimeError(
            f"Buffer error: {result['errors']}"
        )

    action = result.get(
        "data", {}
    ).get("createPost", {})

    post_data = action.get("post") or {}

    post_id = post_data.get("id")

    if not post_id:
        raise RuntimeError(
            f"Buffer did not confirm post: {result}"
        )

    return post_id


# =========================
# MAIN
# =========================

def main():

    print("Starting EIA US Mix automation")

    date, values = fetch_latest()

    previous = {}

    if STATE_FILE.exists():
        previous = json.loads(
            STATE_FILE.read_text(
                encoding="utf-8"
            )
        )

    if previous.get("date", "") >= date:
        print(
            f"Date {date} already queued. "
            "Skipping publication."
        )
        return

    post = format_post(date, values)

    print("\n=========================")
    print("POST PREVIEW")
    print("=========================\n")

    print(post)

    print("\n=========================")

    if DRY_RUN:
        print(
            "DRY_RUN enabled. "
            "Post was NOT published."
        )
        return

    post_id = send_to_buffer(post)

    STATE_FILE.write_text(
        json.dumps(
            {
                "date": date,
                "buffer_post_id": post_id
            },
            indent=2
        ) + "\n",
        encoding="utf-8"
    )

    print(
        "Post successfully queued in Buffer"
    )
    print("Buffer post ID:", post_id)


if __name__ == "__main__":
    main()
