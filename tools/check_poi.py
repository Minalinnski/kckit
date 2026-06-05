#!/usr/bin/env python3
"""Quick connectivity check: verify the poi bridge is reachable and print fleet state."""
import asyncio, json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


async def main():
    try:
        import websockets
    except ImportError:
        print("ERROR: websockets not installed. Run: pip install -r requirements.txt")
        sys.exit(1)

    url = "ws://127.0.0.1:23456"
    print(f"Connecting to {url} …")
    try:
        async with websockets.connect(url, open_timeout=8, max_size=8*1024*1024) as ws:
            raw = await asyncio.wait_for(ws.recv(), timeout=8)
    except ConnectionRefusedError:
        print("FAILED: Connection refused.")
        print()
        print("Fix:")
        print("  1. Make sure poi is running (ARM build: poi-11.1.0-arm64)")
        print("  2. Restart poi to load the kckit-bridge plugin")
        print("  3. In poi: Settings → Plugins → enable 'kckit Bridge'")
        sys.exit(1)
    except asyncio.TimeoutError:
        print("FAILED: Connected but no state received within 8s.")
        print("The bridge plugin may be loaded but the game isn't logged in yet.")
        sys.exit(1)

    data = json.loads(raw)
    if data.get("type") != "state":
        print(f"Unexpected message type: {data.get('type')}")
        sys.exit(1)

    # Plugin sends { type: "state", payload: { ships, equips, fleets, resources, hq_level } }
    state  = data["payload"]
    ships  = state.get("ships", {})
    equips = state.get("equips", {})
    fleets = state.get("fleets", {})
    res    = state.get("resources", {})

    print("OK — poi bridge connected!\n")
    print(f"  Ships:     {len(ships)}")
    print(f"  Equips:    {len(equips)}")
    print(f"  Fleets:    {len(fleets)}")
    print(f"  HQ Level:  {state.get('hq_level', '?')}")
    if res:
        print(f"  Fuel:      {res.get('fuel', '?')}")
        print(f"  Ammo:      {res.get('ammo', '?')}")

    print()
    for fid, fleet in sorted(fleets.items(), key=lambda x: x[0]):
        # poi fleet: { api_ship: [id, id, ...], api_mission: [...], ... }
        ship_ids = fleet.get("api_ship", [])
        names = []
        for sid in ship_ids:
            if sid == -1:
                continue
            s = ships.get(str(sid)) or ships.get(sid)
            if s:
                name = s.get("$master", {}).get("api_name") or s.get("api_name") or f"#{sid}"
                names.append(name)
        print(f"  Fleet {fid}: {', '.join(names) or '(empty)'}")


if __name__ == "__main__":
    asyncio.run(main())
