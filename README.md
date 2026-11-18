# eBay laptop watcher

Checks eBay UK for newly listed laptops that match my criteria, and sends a phone alert through [ntfy](https://ntfy.sh).
Runs on GitHub Actions every 5 minutes, so my PC doesn't need to be on.

- `watcher.py`: searches the eBay Browse API, applies the criteria and sends alerts. Standard library only.
- `.github/workflows/watch.yml`: the schedule and steps GitHub runs.

## Alerts

- ⭐ priority 5: all-metal laptop with RTX 3080 16GB / 3080 Ti
- ❔⭐ priority 4: same GPU, but build or VRAM needs checking
- Other matches: priority 2 (quiet)
- Daily "watcher running" heartbeat at priority 1

## Run locally

```bash
DRY_RUN=1 SHOW_ALL=1 EBAY_CLIENT_ID=... EBAY_CLIENT_SECRET=... python watcher.py
```
