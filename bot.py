"""MOON mech bot: survive first, take the fights it can win, one hunter at a time.

Plan per decision (10 per second):
  1. Read the world: visible mechs, threats from damage events and sounds, the zone.
  2. Pick a goal: stay inside the safe circle, loot a better weapon or cubes when calm,
     fight the most killable enemy in sight, retreat to regenerate when losing.
  3. Move along a grid path (buildings rasterized at start), strafing while fighting.
  4. Aim with lead for the gun, fire every weapon whose shot would land.
"""

import heapq
import json
import math
import os
import random
import time

import mechfight as mf

CELL = 4.0  # navigation grid, meters
PICK_PREF = ["armor", "plating", "nanites", "lightning_efficiency", "lightning_range", "lightning_chain",
             "gun_damage", "gun_rate", "laser_damage", "laser_charge", "reactor", "capacitor",
             "gun_velocity", "gun_mag", "servos", "jets", "gyros", "sensors",
             "mortar_reload", "mortar_splash", "mortar_velocity", "laser_pierce"]
WEAPON_RANK = {"lightning": 3, "laser": 2, "mortar": 1, "gun": 0}


def wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


def dist2(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def point_in_ring(x, y, ring):
    inside = False
    n = len(ring) // 2
    j = n - 1
    for i in range(n):
        xi, yi = ring[2 * i], ring[2 * i + 1]
        xj, yj = ring[2 * j], ring[2 * j + 1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi:
            inside = not inside
        j = i
    return inside


class Nav:
    def __init__(self, map_dir):
        w = json.load(open(os.path.join(map_dir, "v2/world.json")))
        t = w["terrain"]
        self.tminx, self.tminy, self.tnx, self.tny = t["minX"], t["minY"], t["nx"], t["ny"]
        with open(os.path.join(map_dir, "v2/" + os.path.basename(t.get("file", "terrain.i16"))), "rb") as fh:
            raw = fh.read()
        import array

        self.h = array.array("h")
        self.h.frombytes(raw)
        b = w["bounds"]
        self.minx, self.miny = b["minX"], b["minY"]
        self.nx = int((b["maxX"] - b["minX"]) / CELL) + 1
        self.ny = int((b["maxY"] - b["minY"]) / CELL) + 1
        self.blocked = bytearray(self.nx * self.ny)
        for p in w["prisms"]:
            ring = p["ring"]
            xs, ys = ring[0::2], ring[1::2]
            i0, i1 = self.ci(min(xs)), self.ci(max(xs))
            j0, j1 = self.cj(min(ys)), self.cj(max(ys))
            for j in range(j0, j1 + 1):
                cy = self.miny + (j + 0.5) * CELL
                for i in range(i0, i1 + 1):
                    cx = self.minx + (i + 0.5) * CELL
                    if not point_in_ring(cx, cy, ring):
                        continue
                    if any(point_in_ring(cx, cy, hole) for hole in p.get("holes") or []):
                        continue
                    g = self.ground(cx, cy)
                    if p["base"] - g >= 9.5:  # bridge high enough to walk under
                        continue
                    if p["top"] - g <= 1.2:
                        continue
                    self.blocked[j * self.nx + i] = 1
        # steep terrain
        for j in range(self.ny):
            for i in range(self.nx):
                k = j * self.nx + i
                if self.blocked[k]:
                    continue
                cx = self.minx + (i + 0.5) * CELL
                cy = self.miny + (j + 0.5) * CELL
                g = self.ground(cx, cy)
                if abs(self.ground(cx + CELL, cy) - g) > CELL * 1.19 or abs(self.ground(cx, cy + CELL) - g) > CELL * 1.19:
                    self.blocked[k] = 1
        # the map edge is a wall
        for i in range(self.nx):
            self.blocked[i] = self.blocked[(self.ny - 1) * self.nx + i] = 1
        for j in range(self.ny):
            self.blocked[j * self.nx] = self.blocked[j * self.nx + self.nx - 1] = 1

    def ci(self, x):
        return min(max(int((x - self.minx) / CELL), 0), self.nx - 1)

    def cj(self, y):
        return min(max(int((y - self.miny) / CELL), 0), self.ny - 1)

    def center(self, i, j):
        return (self.minx + (i + 0.5) * CELL, self.miny + (j + 0.5) * CELL)

    def ground(self, x, y):
        fx = min(max(x - self.tminx, 0), self.tnx - 1.001)
        fy = min(max(y - self.tminy, 0), self.tny - 1.001)
        i, j = int(fx), int(fy)
        dx, dy = fx - i, fy - j
        n = self.tnx
        h = self.h
        a = h[j * n + i]
        b = h[j * n + i + 1]
        c = h[(j + 1) * n + i]
        d = h[(j + 1) * n + i + 1]
        return ((a * (1 - dx) + b * dx) * (1 - dy) + (c * (1 - dx) + d * dx) * dy) * 0.01

    def free(self, i, j):
        return 0 <= i < self.nx and 0 <= j < self.ny and not self.blocked[j * self.nx + i]

    def nearest_free(self, i, j):
        if self.free(i, j):
            return i, j
        for r in range(1, 8):
            for di in range(-r, r + 1):
                for dj in (-r, r):
                    if self.free(i + di, j + dj):
                        return i + di, j + dj
                    if self.free(i + dj, j + di):
                        return i + dj, j + di
        return i, j

    def line_free(self, a, b):
        d = dist2(a, b)
        steps = int(d / (CELL * 0.5)) + 1
        for s in range(1, steps + 1):
            x = a[0] + (b[0] - a[0]) * s / steps
            y = a[1] + (b[1] - a[1]) * s / steps
            if not self.free(self.ci(x), self.cj(y)):
                return False
        return True

    def path(self, a, b, budget_s=0.006):
        """A* from a to b; returns list of waypoints (world xy) or None."""
        t0 = time.perf_counter()
        si, sj = self.nearest_free(self.ci(a[0]), self.cj(a[1]))
        gi, gj = self.nearest_free(self.ci(b[0]), self.cj(b[1]))
        start, goal = (si, sj), (gi, gj)
        if start == goal:
            return [b]
        openh = [(0, 0, start)]
        came = {start: None}
        gcost = {start: 0}
        best, besth = start, 1e18
        n = 0
        while openh:
            _, g, cur = heapq.heappop(openh)
            if cur == goal:
                best = cur
                break
            n += 1
            if n % 64 == 0 and time.perf_counter() - t0 > budget_s:
                break
            ci_, cj_ = cur
            for di, dj, c in ((1, 0, 1), (-1, 0, 1), (0, 1, 1), (0, -1, 1), (1, 1, 1.414), (1, -1, 1.414), (-1, 1, 1.414), (-1, -1, 1.414)):
                ni, nj = ci_ + di, cj_ + dj
                if not self.free(ni, nj):
                    continue
                if di and dj and (not self.free(ci_ + di, cj_) or not self.free(ci_, cj_ + dj)):
                    continue
                ng = g + c
                nb = (ni, nj)
                if ng < gcost.get(nb, 1e18):
                    gcost[nb] = ng
                    came[nb] = cur
                    h = math.hypot(gi - ni, gj - nj)
                    if h < besth:
                        best, besth = nb, h
                    heapq.heappush(openh, (ng + h * 1.2, ng, nb))
        node = best
        cells = []
        while node is not None:
            cells.append(node)
            node = came[node]
        cells.reverse()
        pts = [self.center(i, j) for i, j in cells]
        if best == goal:
            pts.append(tuple(b[:2]))
        # string pulling
        out = []
        anchor = tuple(a[:2])
        k = 0
        while k < len(pts):
            far = k
            for m in range(min(len(pts) - 1, k + 24), k - 1, -4 if k + 24 - k > 8 else -1):
                if self.line_free(anchor, pts[m]):
                    far = m
                    break
            out.append(pts[far])
            anchor = pts[far]
            k = far + 1
        return out


class Bot:
    name = "MOON"

    def __init__(self, hello):
        self.cfg = hello["config"]
        self.slot = hello["slot"]
        self.rng = random.Random(hello.get("seed", 0))
        try:
            self.nav = Nav(hello["map"]["dir"])
        except Exception as e:  # never die on map trouble
            mf.log("nav failed", repr(e))
            self.nav = None
        self.path = []
        self.path_goal = None
        self.path_t = -99
        self.last_pos = None
        self.stuck_t = 0
        self.strafe = 1
        self.strafe_t = 0
        self.threat = None  # (x, y, t) last known attacker position estimate
        self.last_hit_t = -99
        self.track = {}  # id -> (pos, vel, t, hp)
        self.charging = False
        self.prev_t = 0
        self.home = None

    # ------------------------------------------------------------------ helpers

    def aim_at(self, me, tgt_pos, tgt_vel, speed):
        """yaw, pitch to hit a target center, leading it for a projectile of `speed`."""
        st = me["stats"]
        mx, my, mz = me["pos"]
        mz += st.get("muzzle_height", 7)
        tx, ty, tz = tgt_pos
        tz += 4.5
        vx, vy, vz = tgt_vel if tgt_vel else (0, 0, 0)
        tt = 0.0
        if speed:
            for _ in range(3):
                d = math.dist((mx, my, mz), (tx + vx * tt, ty + vy * tt, tz + vz * tt * 0.5))
                tt = d / speed
        px, py, pz = tx + vx * tt, ty + vy * tt, tz + vz * tt * 0.5
        yaw = math.atan2(py - my, px - mx)
        pitch = math.atan2(pz - mz, math.hypot(px - mx, py - my))
        return yaw, pitch

    def go(self, me, t, goal, boost=False):
        """movement vector toward goal following a grid path."""
        pos = me["pos"]
        if self.nav is None:
            yaw = math.atan2(goal[1] - pos[1], goal[0] - pos[0])
            return [math.cos(yaw), math.sin(yaw)]
        if (self.path_goal is None or dist2(self.path_goal, goal) > 6 or t - self.path_t > 2.0 or not self.path):
            p = self.nav.path(pos, goal)
            self.path = p or [tuple(goal[:2])]
            self.path_goal = tuple(goal[:2])
            self.path_t = t
        while len(self.path) > 1 and dist2(self.path[0], pos) < CELL * 1.2:
            self.path.pop(0)
        wp = self.path[0]
        dx, dy = wp[0] - pos[0], wp[1] - pos[1]
        d = math.hypot(dx, dy)
        if d < 0.5:
            return [0.0, 0.0]
        return [dx / d, dy / d]

    def safe_point(self, obs):
        z = obs["zone"]
        t = obs["t"]
        # aim for the next circle once its shrink is near
        if t > z["shrink_start"] - 25:
            c, r = z["next_center"], z["next_radius"]
        else:
            c, r = z["center"], z["radius"]
        return c, r

    def in_circle(self, p, c, r, margin=0):
        return dist2(p, c) < r - margin

    # ------------------------------------------------------------------ main

    def act(self, obs):
        t = obs["t"]
        me = obs["self"]
        pos = me["pos"]
        act = {"tick": obs["tick"]}
        dt = max(t - self.prev_t, 0.05)
        self.prev_t = t

        if obs.get("offer"):
            choices = obs["offer"]["choices"]
            ids = [c["id"] for c in choices]
            pick = 0
            for pref in PICK_PREF:
                if pref in ids:
                    pick = ids.index(pref)
                    break
            act["pick"] = pick

        # ---- events: who is shooting us
        for ev in obs.get("events", []):
            if ev.get("type") == "damaged" and ev.get("by") is not None:
                self.last_hit_t = t
                b = ev.get("bearing")
                if b is not None:
                    self.threat = (pos[0] + 60 * math.cos(b), pos[1] + 60 * math.sin(b), t, b)
        if self.threat and t - self.threat[2] > 8:
            self.threat = None

        enemies = [m for m in obs.get("mechs", []) if m.get("kind") == "mech"]
        npcs = [m for m in obs.get("mechs", []) if m.get("kind") == "npc"]

        weapons = me["weapons"]
        held = {w["kind"]: i for i, w in enumerate(weapons) if w}
        hp_frac = me["hp"] / max(me["max_hp"], 1)

        # ---- zone
        zc, zr = self.safe_point(obs)
        zone = obs["zone"]
        cur_c, cur_r = zone["center"], zone["radius"]
        # where the zone is right now (it moves during the shrink)
        if zone["shrink_start"] <= t <= zone["shrink_end"] and zone["shrink_end"] > zone["shrink_start"]:
            f = (t - zone["shrink_start"]) / (zone["shrink_end"] - zone["shrink_start"])
            cur_c = [cur_c[0] + (zone["next_center"][0] - cur_c[0]) * f, cur_c[1] + (zone["next_center"][1] - cur_c[1]) * f]
            cur_r = cur_r + (zone["next_radius"] - cur_r) * f
        zone_danger = not self.in_circle(pos, cur_c, cur_r, margin=8) or not self.in_circle(pos, zc, zr, margin=max(10, zr * 0.25))

        # ---- choose a target
        target = None
        if enemies:
            def score(m):
                d = math.dist(m["pos"], pos)
                return d / 40 + (m.get("hp", 1400) / 1400) * 2.5
            target = min(enemies, key=score)
        npc_target = None
        if not target and npcs:
            npc_target = min(npcs, key=lambda m: math.dist(m["pos"], pos))
            if math.dist(npc_target["pos"], pos) > 70:
                npc_target = None

        # ---- movement goal
        move = [0.0, 0.0]
        boost = False
        jump = False
        mode = "hold"
        tgt = target or npc_target
        retreat = target is not None and hp_frac < 0.35 and target.get("hp", 1400) / max(target.get("max_hp", 1400), 1) > hp_frac + 0.1
        if zone_danger:
            mode = "zone"
            move = self.go(me, t, zc)
            boost = not self.in_circle(pos, cur_c, cur_r, margin=0) or me["energy"] > 60
        elif retreat:
            mode = "retreat"
            ex, ey = target["pos"][0], target["pos"][1]
            away = math.atan2(pos[1] - ey, pos[0] - ex)
            # retreat away from the enemy but toward the zone center
            to_c = math.atan2(zc[1] - pos[1], zc[0] - pos[0])
            dirn = math.atan2(math.sin(away) * 2 + math.sin(to_c), math.cos(away) * 2 + math.cos(to_c))
            goal = (pos[0] + 40 * math.cos(dirn), pos[1] + 40 * math.sin(dirn))
            move = self.go(me, t, goal)
            boost = me["energy"] > 20
        elif tgt is not None:
            mode = "fight"
            d = dist2(tgt["pos"], pos)
            want = 30 if "lightning" in held else 70
            if tgt.get("kind") == "npc":
                want = 25 if "lightning" in held else 40
            to = math.atan2(tgt["pos"][1] - pos[1], tgt["pos"][0] - pos[0])
            if t - self.strafe_t > 1.2 + self.rng.random():
                self.strafe = -self.strafe
                self.strafe_t = t
            perp = to + self.strafe * math.pi / 2
            radial = 0.0
            if d > want + 15:
                radial = 1.0
            elif d < want - 10:
                radial = -0.8
            mx = math.cos(perp) * 0.8 + math.cos(to) * radial
            my = math.sin(perp) * 0.8 + math.sin(to) * radial
            if d > want + 40:
                move = self.go(me, t, tgt["pos"])
            else:
                n = math.hypot(mx, my) or 1
                move = [mx / n, my / n]
        else:
            # calm: loot nearby weapons, cubes, crates; otherwise hold a spot inside the zone
            goal = None
            want_weapon = weapons[1] is None or WEAPON_RANK.get(weapons[1]["kind"], 0) < 3
            if want_weapon:
                best = None
                for p in obs.get("pickups", []):
                    if p["kind"] in held or p["kind"] == "gun":
                        continue
                    if weapons[1] is not None and WEAPON_RANK.get(p["kind"], 0) <= WEAPON_RANK.get(weapons[1]["kind"], 0):
                        continue
                    d = dist2(p["pos"], pos)
                    if d < 120 and self.in_circle(p["pos"], zc, zr) and (best is None or d < best[0]):
                        best = (d, p)
                if best:
                    goal = best[1]["pos"]
                    mode = "loot"
            if goal is None and obs.get("cubes"):
                c = min(obs["cubes"], key=lambda c: dist2(c["pos"], pos))
                if dist2(c["pos"], pos) < 80:
                    goal = c["pos"]
                    mode = "cube"
            if goal is None and obs.get("crates") and me["cubes"] < 10:
                c = min(obs["crates"], key=lambda c: dist2(c["pos"], pos))
                if dist2(c["pos"], pos) < 60 and abs(c["pos"][2] - pos[2]) < 4:
                    tgt = {"pos": c["pos"], "vel": [0, 0, 0], "kind": "crate", "id": c["id"]}
                    if dist2(c["pos"], pos) > 20:
                        goal = c["pos"]
                    mode = "crate"
            if goal is None and mode != "crate":
                # drift to a holding point inside the safe circle, then stand still (silent)
                if self.home is None or not self.in_circle(self.home, zc, zr * 0.6) or self.home_t < zone["shrink_start"] - 200:
                    ang = self.rng.random() * 2 * math.pi
                    rad = self.rng.random() * zr * 0.35
                    self.home = (zc[0] + rad * math.cos(ang), zc[1] + rad * math.sin(ang))
                    self.home_t = t
                if dist2(self.home, pos) > 8:
                    goal = self.home
                    mode = "travel"
            if goal is not None:
                move = self.go(me, t, goal)

        # ---- stuck handling
        speed = math.hypot(me["vel"][0], me["vel"][1])
        if (move[0] or move[1]) and speed < 1.0 and me["grounded"]:
            self.stuck_t += dt
        else:
            self.stuck_t = 0
        if self.stuck_t > 0.6:
            jump = True
            boost = True
            self.path_t = -99  # replan
            if self.stuck_t > 2.0:
                ang = math.atan2(move[1], move[0]) + self.rng.choice([-1.3, 1.3])
                move = [math.cos(ang), math.sin(ang)]
        if not me["grounded"] and me["energy"] > 10 and self.stuck_t > 0:
            boost = True

        # ---- take weapons we are standing on
        reach = self.cfg["loot"]["pickup_radius"]
        for p in obs.get("pickups", []):
            if dist2(p["pos"], pos) < reach - 0.3 and abs(p["pos"][2] - pos[2]) < 3.5 and p["kind"] not in held and p["kind"] != "gun":
                if weapons[1] is None:
                    act["take"] = p["id"]
                    break
                cur = weapons[1]
                if WEAPON_RANK.get(p["kind"], 0) > WEAPON_RANK.get(cur["kind"], 0) and not (cur["kind"] == "laser" and cur.get("charge", 0) > 0):
                    act["take"] = p["id"]
                    act["drop"] = 1
                    break

        # ---- aim and fire
        fire = [False, False]
        aim = None
        if tgt is not None:
            gun = weapons[0] if weapons[0] and weapons[0]["kind"] == "gun" else None
            speed_p = gun["stats"]["speed"] if gun else 252
            yaw, pitch = self.aim_at(me, tgt["pos"], tgt.get("vel"), speed_p)
            aim = [yaw, pitch]
            err = abs(wrap(me["aim"][0] - yaw)) + abs(me["aim"][1] - pitch) * 0.5
            d = math.dist(tgt["pos"], pos)
            for i, w in enumerate(weapons):
                if not w:
                    continue
                k = w["kind"]
                if k == "gun":
                    if d < w["stats"]["range"] and err < max(0.04, 3.5 / max(d, 1)) and w.get("ammo", 1) > 0:
                        fire[i] = True
                elif k == "lightning":
                    if d < w["stats"]["range"] and abs(wrap(me["aim"][0] - math.atan2(tgt["pos"][1] - pos[1], tgt["pos"][0] - pos[0]))) < math.radians(w["stats"]["cone_deg"]) * 0.9 and me["energy"] > w["stats"].get("energy", 3):
                        fire[i] = True
                elif k == "laser":
                    if d < w["stats"]["range"] and w.get("cooldown", 0) <= 0:
                        ch = w.get("charge", 0)
                        if ch < w["stats"]["charge"] or err > 0.03:
                            fire[i] = True  # keep charging until full and on target
                        else:
                            fire[i] = False  # let go: fire
                    elif w.get("charge", 0) > 0:
                        fire[i] = False
                elif k == "mortar":
                    if 25 < d < 200 and w.get("cooldown", 0) <= 0:
                        # lob: pitch for range on flat ground
                        v = w["stats"]["speed"]
                        g = w["stats"]["gravity"]
                        s = min(1.0, g * d / (v * v))
                        aim_m = 0.5 * math.asin(s)
                        aim = [yaw, aim_m] if not any(fire) else aim
                        if abs(me["aim"][1] - aim_m) < 0.05 and abs(wrap(me["aim"][0] - yaw)) < 0.05:
                            fire[i] = True
        elif self.threat is not None:
            aim = [self.threat[3], 0.0]
        elif mode in ("zone", "travel", "loot", "cube") and (move[0] or move[1]):
            aim = [math.atan2(move[1], move[0]), 0.0]
        else:
            # scan slowly while holding still
            aim = [wrap(me["aim"][0] + 0.25), 0.0]

        # reload when calm
        g0 = weapons[0]
        if g0 and g0["kind"] == "gun" and tgt is None and g0.get("ammo", 12) < g0["stats"]["mag"] // 2 and g0.get("reload", 0) == 0:
            act["reload"] = True

        act["move"] = [round(move[0], 3), round(move[1], 3)]
        act["boost"] = bool(boost)
        act["jump"] = bool(jump)
        if aim is not None:
            act["aim"] = [round(aim[0], 4), round(max(-0.69, min(1.29, aim[1])), 4)]
        act["fire"] = fire
        self.last_pos = pos
        return act


if __name__ == "__main__":
    mf.run(Bot)
