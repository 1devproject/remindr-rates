import requests
import json
import os

API_KEY = os.environ["EXCHANGE_API_KEY"]
url = f"https://v6.exchangerate-api.com/v6/{API_KEY}/latest/USD"

response = requests.get(url)
data = response.json()

output = {
    "base": data["base_code"],
    "rates": data["conversion_rates"],
    "updated": data["time_last_update_utc"],
    "next_update": data["time_next_update_utc"]
}

with open("rates.json", "w") as f:
    json.dump(output, f, indent=2)
