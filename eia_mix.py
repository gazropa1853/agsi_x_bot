
import os
import json
import requests

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR


# =========================
# SECRETS
# =========================

EIA_API_KEY = os.environ["EIA_API_KEY"]
BUFFER_TOKEN = os.environ["BUFFER_TOKEN"]
BUFFER_CHANNEL_ID = os.environ["BUFFER_CHANNEL_ID"]


# =========================
# CONFIGURATION
# =========================

EIA_URL = (
    "https://api.eia.gov/v2/electricity/"
    "rto/daily-fuel-type-data/data/"
)

BUFFER_URL = "https://api.buffer.com/graphql"


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

# Map additional EIA categories
FUEL_MAPPING = {
    "NG": "NG",
    "NUC": "NUC",
    "COL": "COL",
    "WND": "WND",
    "WNB": "WND",
    "SUN": "SUN",
    "SNB": "SUN",
    "WAT": "WAT",
    "OIL": "OIL",
    "OTH": "OTH",
    "GEO": "OTH",
    "UNK": "OTH",
}

# Energy storage excluded from fuel mix
STORAGE_CODES = {"BAT", "PS", "OES"}

# Core categories required for publication
REQUIRED = {
    "NG", "NUC", "COL",
    "WND", "SUN", "WAT"
}


# =========================
# EIA DATA
# =========================

def fetch_latest():

    today = datetime.now(
        timezone.utc
    ).date()

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
        "length": 5000,
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
    }

    print("Connecting to EIA API...")

    response = requests.get(
        EIA_URL,
        params=params,
        timeout=40
    )

    print(
        "EIA HTTP status:",
        response.status_code
    )

    if response.status_code != 200:
        print(response.text[:1000])

    response.raise_for_status()

    payload = response.json()

    if payload.get("error"):
        raise RuntimeError(
            str(payload["error"])
        )

    rows = payload.get(
        "response", {}
    ).get("data", [])

    print("Rows received:", len(rows))

    if not rows:
        raise RuntimeError(
            "No EIA data returned"
        )

    by_date = {}
    seen = set()

    for row in rows:

        if row.get("respondent") != "US48":
            continue

        date = row["period"][:10]
        fuel = row.get("fueltype")

        # Storage is excluded
        if fuel in STORAGE_CODES:
            continue

        if fuel not in FUEL_MAPPING:
            raise RuntimeError(
                f"Unknown EIA fuel type: {fuel}"
            )

        raw_value = row.get("value")

        if raw_value is None:
            continue

        value = Decimal(str(raw_value))

        if not value.is_finite():
            raise RuntimeError(
                "Invalid EIA value"
            )

        if value < 0:
            raise RuntimeError(
                f"Negative generation: "
                f"{fuel}, {date}"
            )

        key = (date, fuel)

        if key in seen:
            raise RuntimeError(
                f"Duplicate EIA record: {key}"
            )

        seen.add(key)

        category = FUEL_MAPPING[fuel]

        daily = by_date.setdefault(
            date, {}
        )

        daily[category] = (
            daily.get(category, Decimal(0))
            + value
        )

    # Select most recent complete date
    for date in sorted(
        by_date,
        reverse=True
    ):

        values = by_date[date]

        if not REQUIRED.issubset(
            values.keys()
        ):
            print(
                "Skipping incomplete date:",
                date
            )
            continue

        total = sum(values.values())

        if total <= 0:
            continue

        age = (
            today
            - datetime.fromisoformat(
                date
            ).date()
        ).days

        if age > 4:
            raise RuntimeError(
                f"EIA data too old: {date}"
            )

        print("Selected date:", date)
        print("Generation:", values)

        return date, values

    raise RuntimeError(
        "No complete recent EIA data"
    )


# =========================
# PERCENTAGES
# =========================

def calculate_percentages(values):

    total = sum(values.values())

    exact = {
        fuel: value * 100 / total
        for fuel, value in values.items()
    }

    rounded = {
        fuel: int(
            share.to_integral_value(
                rounding=ROUND_FLOOR
            )
        )
        for fuel, share in exact.items()
    }

    missing = 100 - sum(
        rounded.values()
    )

    ranking = sorted(
        exact,
        key=lambda fuel: (
            exact[fuel] - rounded[fuel],
            exact[fuel]
        ),
        reverse=True
    )

    for fuel in ranking[:missing]:
        rounded[fuel] += 1

    return rounded


# =========================
# POST FORMAT
# =========================

def create_post(date, values):

    shares = calculate_percentages(
        values
    )

    ordered = sorted(
        shares,
        key=lambda fuel: (
            -shares[fuel],
            FUEL_NAMES[fuel]
        )
    )

    formatted_date = (
        datetime.fromisoformat(date)
        .strftime("%d.%m.%Y")
    )

    lines = [
        "Miks produkcji energii "
        "elektrycznej w USA "
        f"({formatted_date})",
        ""
    ]

    for fuel in ordered:

        share = shares[fuel]

        # Hide zero-percent categories
        if share == 0:
            continue

        lines.append(
            f"{FUEL_NAMES[fuel]}: {share}%"
        )

    lines.extend([
        "",
        "Źródło: U.S. EIA (EIA-930)",
        "Obliczenia własne."
    ])

    post = "\n".join(lines)

    if len(post) > 280:
        raise RuntimeError(
            "Post exceeds 280 characters"
        )

    return post


# =========================
# BUFFER
# =========================

def send_to_buffer(post):

    # Encode text safely for GraphQL
    encoded_text = json.dumps(
        post,
        ensure_ascii=False
    )

    encoded_channel = json.dumps(
        BUFFER_CHANNEL_ID
    )

    query = f"""
    mutation {{
      createPost(
        input: {{
          text: {encoded_text}
          channelId: {encoded_channel}
          schedulingType: automatic
          mode: addToQueue
        }}
      ) {{
        ... on PostActionSuccess {{
          post {{
            id
          }}
        }}
        ... on MutationError {{
          message
        }}
      }}
    }}
    """

    response = requests.post(
        BUFFER_URL,
        headers={
            "Authorization": (
                f"Bearer {BUFFER_TOKEN}"
            ),
            "Content-Type": "application/json"
        },
        json={"query": query},
        timeout=40
    )

    print(
        "Buffer HTTP status:",
        response.status_code
    )

    response.raise_for_status()

    result = response.json()

    if result.get("errors"):
        raise RuntimeError(
            str(result["errors"])
        )

    action = (
        result.get("data", {})
        .get("createPost") or {}
    )

    post_data = action.get("post") or {}

    post_id = post_data.get("id")

    if not post_id:
        raise RuntimeError(
            f"Buffer rejected post: {action}"
        )

    print("Buffer post ID:", post_id)

    return post_id


# =========================
# MAIN
# =========================

def main():

    print("Starting EIA US Mix")

    date, values = fetch_latest()

    post = create_post(
        date,
        values
    )

    print("\n======================")
    print("POST")
    print("======================\n")

    print(post)

    print("\n======================")
    print("Sending to Buffer...")

    post_id = send_to_buffer(post)

    print(
        "Successfully added to Buffer queue"
    )

    print("Post ID:", post_id)


if __name__ == "__main__":
    main()
