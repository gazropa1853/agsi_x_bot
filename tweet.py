import os
import requests
from datetime import datetime, timedelta, timezone


# =========================
# SECRETS
# =========================

AGSI_API_KEY = os.environ["AGSI_API_KEY"]
BUFFER_TOKEN = os.environ["BUFFER_TOKEN"]
BUFFER_CHANNEL_ID = os.environ["BUFFER_CHANNEL_ID"]


# =========================
# AGSI
# =========================

AGSI_URL = "https://agsi.gie.eu/api"

datasets = {
    "EU": {"type": "eu"},
    "DE": {"country": "DE"},
    "IT": {"country": "IT"},
    "NL": {"country": "NL"},
    "FR": {"country": "FR"},
    "PL": {"country": "PL"},
}


def get_agsi_data(extra_params):
    """
    Pobiera ostatnie kilka dni danych AGSI.
    """
    today = datetime.now(timezone.utc).date()
    start_date = today - timedelta(days=10)

    params = {
        **extra_params,
        "from": start_date.isoformat(),
        "to": today.isoformat(),
        "size": 30,
    }

    response = requests.get(
        AGSI_URL,
        headers={
            "x-key": AGSI_API_KEY,
            "Accept": "application/json",
        },
        params=params,
        timeout=30,
    )

    response.raise_for_status()

    result = response.json()

    if "data" not in result:
        raise RuntimeError(
            f"Brak pola 'data' w odpowiedzi AGSI: {result}"
        )

    return result["data"]


def parse_full(value):
    """
    Zamienia wartość AGSI na float.
    Zwraca None dla brakujących danych.
    """
    if value in (None, "", "-", "N/A"):
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# =========================
# POBIERANIE DANYCH
# =========================

all_data = {}

for code, params in datasets.items():
    rows = get_agsi_data(params)

    daily = {}

    for row in rows:
        gas_day = row.get("gasDayStart")
        full = parse_full(row.get("full"))

        if gas_day and full is not None:
            daily[gas_day] = full

    if not daily:
        raise RuntimeError(
            f"Nie znaleziono poprawnych danych AGSI dla {code}"
        )

    all_data[code] = daily


# =========================
# NAJNOWSZA WSPÓLNA DATA
# =========================

common_dates = set(all_data["EU"].keys())

for code in ["DE", "IT", "NL", "FR", "PL"]:
    common_dates &= set(all_data[code].keys())

if not common_dates:
    raise RuntimeError(
        "Nie znaleziono wspólnej daty AGSI dla wszystkich krajów."
    )

latest_date = max(common_dates)

print(f"Najnowsza wspólna data AGSI: {latest_date}")


# =========================
# FORMATOWANIE
# =========================

def format_percent(value):
    return f"{value:.1f}".replace(".", ",") + "%"


eu = format_percent(all_data["EU"][latest_date])
de = format_percent(all_data["DE"][latest_date])
it = format_percent(all_data["IT"][latest_date])
nl = format_percent(all_data["NL"][latest_date])
fr = format_percent(all_data["FR"][latest_date])
pl = format_percent(all_data["PL"][latest_date])

date_display = datetime.strptime(
    latest_date,
    "%Y-%m-%d"
).strftime("%d.%m.%Y")


post_text = (
    f"Stan zatłoczenia magazynów gazu ({date_display}):\n"
    f"🇪🇺  {eu}  "
    f"🇩🇪  {de}  "
    f"🇮🇹  {it}  "
    f"🇳🇱  {nl}  "
    f"🇫🇷  {fr}  "
    f"🇵🇱  {pl}\n"
    f"Źródło: AGSI"
)

print("\nTreść posta:")
print(post_text)


# =========================
# BUFFER
# =========================

query = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {

    ... on PostActionSuccess {
      post {
        id
        text
        dueAt
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
        "text": post_text,
        "channelId": BUFFER_CHANNEL_ID,
        "schedulingType": "automatic",
        "mode": "addToQueue",
    }
}

response = requests.post(
    "https://api.buffer.com/graphql",
    headers={
        "Authorization": f"Bearer {BUFFER_TOKEN}",
        "Content-Type": "application/json",
    },
    json={
        "query": query,
        "variables": variables,
    },
    timeout=30,
)

response.raise_for_status()

result = response.json()

print("\nOdpowiedź Buffer:")
print(result)

if result.get("errors"):
    raise RuntimeError(
        f"Błąd GraphQL Buffer: {result['errors']}"
    )

create_result = result.get("data", {}).get("createPost", {})

if "message" in create_result:
    raise RuntimeError(
        f"Buffer odrzucił post: {create_result['message']}"
    )

if not create_result.get("post"):
    raise RuntimeError(
        f"Nieoczekiwana odpowiedź Buffer: {result}"
    )

print(
    f"\nPost poprawnie przekazany do Buffer. "
    f"ID: {create_result['post']['id']}"
)
