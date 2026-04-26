import requests

URL = "https://jeevan2717-incident-postmortem-writer.hf.space"

r = requests.get(f"{URL}/health", timeout=10)
print(f"Health: {r.status_code} {r.json()}")
print()

r = requests.post(f"{URL}/reset", json={"difficulty": "easy"}, timeout=30)
print(f"Reset: {r.status_code}")
obs_keys = list(r.json()["observation"].keys())
print(f"Observation fields ({len(obs_keys)}):")
for k in sorted(obs_keys):
    marker = " <-- NEW" if k in ("skeptic_critiques", "critiques_addressed", "reviews_requested") else ""
    print(f"  {k}{marker}")