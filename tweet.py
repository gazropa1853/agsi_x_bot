import os
import requests

API_KEY = os.environ["AGSI_API_KEY"]

headers = {
"x-key": API_KEY
}

response = requests.get(
"https://agsi.gie.eu/api",
headers=headers
)

print("STATUS:", response.status_code)
print(response.text[:500])
