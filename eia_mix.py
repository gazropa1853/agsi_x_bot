import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path

import requests

API_URL = "https://api.eia.gov/v2/electricity/rto/daily-fuel-type-data/data/"
BUFFER_URL = "https://api.buffer.com/graphql"
STATE_FILE = Path("eia_last_published.json")
FUEL_NAMES = {
    "NG": "Gaz ziemny", "NUC": "Energia jądrowa", "COL": "Węgiel",
    "WND": "Wiatr", "SUN": "PV", "WAT": "Woda",
    "OIL": "Ropa i produkty naftowe", "OTH": "Pozostałe",
}
EXPECTED = set(FUEL_NAMES)


def fetch_latest():
    # US48 is EIA's existing aggregate for contiguous USA; no double counting regions.
    today = datetime.now(timezone.utc).date()
    params = {
        "api_key": os.environ["EIA_API_KEY"],
        "frequency": "daily", "data[0]": "value",
        "facets[respondent][]": "US48",
        "start": (today - timedelta(days=8)).isoformat(),
        "end": (today - timedelta(days=1)).isoformat(),
        "length": "5000", "sort[0][column]": "period",
        "sort[0][direction]": "desc",
    }
    resp = requests.get(
    API_URL,
    params=params,
    timeout=40,
    headers={
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    },
)

print("EIA HTTP status:", resp.status_code)
print("Content-Type:", resp.headers.get("Content-Type"))
print("EIA response:", resp.text[:1500])

resp.raise_for_status()
payload = resp.json()
    if payload.get("error"):
        raise RuntimeError(f"EIA error: {payload['error']}")
    rows = payload.get("response", {}).get("data", [])
    if not rows:
        raise RuntimeError("No EIA data returned")
    by_date = {}
    for row in rows:
        date = row["period"][:10]
        if row.get("respondent") != "US48":
            continue
        code = row["fueltype"]
        if code not in EXPECTED:
            raise RuntimeError(f"Unknown fuel type {code}; verify before publishing")
        if row.get("value") is None:
            continue
        value = Decimal(str(row["value"]))
        if value < 0:
            raise RuntimeError("Negative generation value: manual review needed")
        if code in by_date.setdefault(date, {}):
            raise RuntimeError(f"Duplicate fuel {code} for {date}")
        by_date[date][code] = value
    # Most recent date with all expected categories; prevent stale releases.
    for date in sorted(by_date, reverse=True):
        values = by_date[date]
        if set(values) == EXPECTED and sum(values.values()) > 0:
            if (today - datetime.fromisoformat(date).date()).days > 4:
                raise RuntimeError(f"Latest complete report is stale ({date})")
            return date, values
    raise RuntimeError("No recent date with all eight EIA fuel types")


def percentages(values):
    # Largest-remainder rounding, ensuring the displayed integers sum to 100.
    total = sum(values.values())
    exact = {k: (v * 100 / total) for k, v in values.items()}
    whole = {k: int(p.to_integral_value(rounding=ROUND_FLOOR)) for k, p in exact.items()}
    remainder = 100 - sum(whole.values())
    ranking = sorted(values, key=lambda k: (exact[k] - whole[k], exact[k], k), reverse=True)
    for k in ranking[:remainder]:
        whole[k] += 1
    return whole


def format_post(date, values):
    pct = percentages(values)
    ordered = sorted(pct, key=lambda k: (-pct[k], FUEL_NAMES[k]))
    nice_date = datetime.fromisoformat(date).strftime("%d.%m.%Y")
    lines = [f"Miks produkcji energii elektrycznej w USA ({nice_date})", ""]
    lines += [f"{FUEL_NAMES[k]}: {pct[k]}%" for k in ordered]
    lines += ["", "Źródło: U.S. EIA (EIA-930)", "Obliczenia własne."]
    text = "\n".join(lines)
    if len(text) > 280:
        raise RuntimeError(f"Post exceeds X limit: {len(text)} chars")
    return text


def send_to_buffer(text):
    mutation = """
    mutation CreatePost($input: CreatePostInput!) {
      createPost(input: $input) {
        ... on PostActionSuccess { post { id } }
        ... on MutationError { message }
      }
    }
    """
    variables = {"input": {"text": text, "channelId": os.environ["BUFFER_CHANNEL_ID"],
                           "schedulingType": "automatic", "mode": "addToQueue"}}
    resp = requests.post(BUFFER_URL,
                         headers={"Authorization": f"Bearer {os.environ['BUFFER_TOKEN']}",
                                  "Content-Type": "application/json"},
                         json={"query": mutation, "variables": variables}, timeout=40)
    resp.raise_for_status()
    body = resp.json()
    if body.get("errors"):
        raise RuntimeError(f"Buffer GraphQL errors: {body['errors']}")
    result = body.get("data", {}).get("createPost", {})
    post_id = result.get("post", {}).get("id") if isinstance(result, dict) else None
    if not post_id:
        raise RuntimeError(f"Buffer did not confirm success: {body}")
    return post_id


def main():
    date, values = fetch_latest()
    previous = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    if previous.get("date", "") >= date:
        print(f"Already queued date {date} or a newer report; skipping")
        return
    post = format_post(date, values)
    print("Preview:\n" + post)
    if os.environ.get("DRY_RUN", "true").lower() != "false":
        print("DRY_RUN enabled: post not sent")
        return
    post_id = send_to_buffer(post)
    STATE_FILE.write_text(json.dumps({"date": date, "buffer_post_id": post_id}, indent=2) + "\n")
    print(f"Queued Buffer post ID: {post_id}")


if __name__ == "__main__":
    main()
