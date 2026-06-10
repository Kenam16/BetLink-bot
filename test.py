import requests
import time

CHAPA_SECRET_KEY = "PASTE_YOUR_CHAPA_SECRET_KEY_HERE"  # <-- paste your key here

url = "https://api.chapa.co/v1/transaction/initialize"

headers = {
    "Authorization": f"Bearer {CHAPA_SECRET_KEY}",
    "Content-Type": "application/json",
}

payload = {
    "amount": "100",
    "currency": "ETB",
    "email": "customer@betlink.com",
    "first_name": "BetLink",
    "last_name": "Customer",
    "tx_ref": f"test_{int(time.time())}",
    "return_url": "https://t.me/BetLinkBot",
    "customization": {
        "title": "Test Payment",
        "description": "BetLink test",
    },
}

print("Sending request to Chapa...")
print(f"URL     : {url}")
print(f"Payload : {payload}\n")

try:
    response = requests.post(url, json=payload, headers=headers, timeout=10)
    print(f"HTTP Status : {response.status_code}")
    print(f"Raw Response: {response.text}\n")

    data = response.json()
    if data.get("status") == "success":
        print("✅ SUCCESS! Checkout URL:")
        print(data["data"]["checkout_url"])
    else:
        print("❌ FAILED — Chapa returned an error:")
        print(data)

except requests.exceptions.ConnectionError:
    print("❌ Connection error — check your internet connection.")
except requests.exceptions.Timeout:
    print("❌ Request timed out.")
except Exception as e:
    print(f"❌ Unexpected error: {e}")