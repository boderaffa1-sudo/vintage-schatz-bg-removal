import json, sys

f = open(sys.argv[1], 'r', encoding='utf-8')
data = json.loads(f.read())
f.close()

print(f"{len(data)} log entries")
print(f"First: {data[0]['timestamp']}")
print(f"Last:  {data[-1]['timestamp']}")

uploads = [d for d in data if 'Uploaded:' in d['message']]
skips = [d for d in data if 'SKIP' in d['message'] or 'FEHLER' in d['message']]
oks = [d for d in data if 'OK' in d['message'] and len(d['message']) < 60]
folders = [d for d in data if 'images to process' in d['message']]
rate_limits = [d for d in data if '429' in d['message']]

print(f"\nFolders with images: {len(folders)}")
print(f"OK processed: {len(oks)}")
print(f"Uploaded: {len(uploads)}")
print(f"Skipped/Error: {len(skips)}")
print(f"Rate limit retries: {len(rate_limits)}")

# Time calculation
from datetime import datetime
t1 = datetime.fromisoformat(data[0]['timestamp'].replace('Z', '+00:00'))
t2 = datetime.fromisoformat(data[-1]['timestamp'].replace('Z', '+00:00'))
elapsed = (t2 - t1).total_seconds()
print(f"\nElapsed: {elapsed/60:.1f} minutes")

if uploads:
    avg_per_image = elapsed / len(uploads) if len(uploads) > 0 else 0
    print(f"Avg per uploaded image: {avg_per_image:.0f}s")

# Show folders
print("\nFolders:")
for d in folders:
    msg = d['message']
    print(f"  {msg.strip()}")
