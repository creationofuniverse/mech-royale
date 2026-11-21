# MOON — Mech Royale bot

Python bot for the Buildathon 2026 Mech Royale track (Builders).

- `bot.py` — the bot: grid navigation built from the map at start (buildings rasterized, A* with
  string pulling), zone-first movement, looting (lightning > laser > mortar), cube farming from
  crates and drones when calm, strafing fights with lead aiming for the gun, laser charge/release,
  lightning within its cone, and retreating to regenerate when losing.
- Fires only with a clear 3D line of fire (buildings and terrain from the map), lobs the mortar with a
  ballistic solve (height and lead) only when the gun can't shoot, breaks line of sight when outnumbered
  or hurt, captures supply drops, and turns to face nearby mech sounds.
- `mechfight.py` — the kit's protocol wrapper, unchanged.

MIT licensed. No outside code besides the kit's wrapper.
