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
# sounds only a mech makes (not crates, drones, deaths, drops or blast points)
HUNTER_SOUNDS = {"gun", "mortar", "laser", "laser_charge", "lightning", "jet", "step", "boost", "reload"}


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
        self.solid = {}  # cell -> list of (base, top): every prism, for 3D line of sight
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
                    self.solid.setdefault(j * self.nx + i, []).append((p["base"], p["top"]))
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

    def los(self, a, b, skip=3.0):
        """3D line of sight between world points a and b (buildings and terrain block; trees don't).
        Ignores the first and last `skip` meters, where the 4 m raster is too coarse to trust."""
        dx, dy, dz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        d = math.hypot(dx, dy)
        if d <= 2 * skip:
            return True
        steps = int(d / 2.0) + 1
        s0 = skip / d
        solid = self.solid
        nx, minx, miny = self.nx, self.minx, self.miny
        for s in range(1, steps):
            f = s / steps
            if f < s0 or f > 1 - s0:
                continue
            x, y, z = a[0] + dx * f, a[1] + dy * f, a[2] + dz * f
            i, j = int((x - minx) / CELL), int((y - miny) / CELL)
            iv = solid.get(j * nx + i)
            if iv:
                for lo, hi in iv:
                    if lo < z < hi:
                        return False
            if z < self.ground(x, y) - 0.3:
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
        self.strafe_len = 0.7
        self.threat = None  # (x, y, t) last known attacker position estimate
        self.last_hit_t = -99
        self.track = {}  # id -> (pos, vel, t, hp)
        self.charging = False
        self.prev_t = 0
        self.home = None
        self.home_t = 0
        self.mortar_lock = -99  # committed to a mortar lob until this time
        self.cover = None  # (x, y) hiding spot
        self.cover_t = -99
        self.peek = False  # at cover with exactly one enemy in sight: hold and shoot

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

    def can_hit(self, me, tgt):
        """Line of fire from our muzzle to the target's center or head (fix #1: don't shoot walls)."""
        if self.nav is None:
            return True
        mx, my, mz = me["pos"]
        m = (mx, my, mz + me["stats"].get("muzzle_height", 7))
        tx, ty, tz = tgt["pos"]
        h = 9.0 if tgt.get("kind") == "mech" else 3.0
        return self.nav.los(m, (tx, ty, tz + h * 0.5)) or self.nav.los(m, (tx, ty, tz + h * 0.9))

    def seeing(self, enemies, p, stop=1):
        """How many enemies' eyes see a mech standing at ground point p (x, y, z), counting up to `stop`."""
        n = 0
        for e in enemies:
            ex, ey, ez = e["pos"]
            eye = (ex, ey, ez + 8.0)
            if self.nav.los(eye, (p[0], p[1], p[2] + 4.5)) or self.nav.los(eye, (p[0], p[1], p[2] + 8.5)):
                n += 1
                if n >= stop:
                    break
        return n

    def seen_by(self, enemies, p):
        return self.seeing(enemies, p) > 0

    def mortar_solve(self, me, tgt, w, lob):
        """Pitch to land a shell on the target's feet, leading it (fix #2). None if out of reach."""
        v, g = w["stats"]["speed"], w["stats"]["gravity"]
        ttl = w["stats"].get("ttl", 7)
        mx, my, mz = me["pos"]
        mz += me["stats"].get("muzzle_height", 7)
        tx, ty, tz = tgt["pos"]
        vx, vy = (tgt.get("vel") or (0, 0, 0))[:2]
        tof, yaw, pitch = 0.0, 0.0, None
        for _ in range(3):
            lead = tof * 0.9
            px, py = tx + vx * lead, ty + vy * lead
            d = math.hypot(px - mx, py - my)
            dz = tz + 1.0 - mz
            disc = v ** 4 - g * (g * d * d + 2 * dz * v * v)
            if disc < 0 or d < 1:
                return None
            r = math.sqrt(disc)
            pitch = math.atan((v * v + r) / (g * d)) if lob else math.atan((v * v - r) / (g * d))
            yaw = math.atan2(py - my, px - mx)
            tof = d / (v * math.cos(pitch))
        if tof > ttl or pitch > 1.28 or pitch < -0.69:
            return None
        return yaw, pitch

    def find_cover(self, me, enemies, zc, zr, t, hp_frac=1.0):
        """A nearby reachable spot no visible enemy can see (fix #3). Cached; time-boxed.
        Sets self.peek when we should hold our corner and fight the one enemy that sees us."""
        if self.nav is None or not enemies:
            return None
        pos = me["pos"]
        if self.cover and t - self.cover_t < 1.0:
            return self.cover
        t0 = time.perf_counter()
        near = sorted(enemies, key=lambda e: dist2(e["pos"], pos))[:4]
        n_see = self.seeing(near, pos, stop=2)
        at_cover = self.cover is not None and dist2(self.cover, pos) < 6
        # hidden, or holding our corner against a single peeker: stay and shoot it
        if n_see == 0 or (n_see == 1 and hp_frac >= 0.25):
            self.peek = n_see == 1
            self.cover, self.cover_t = (pos[0], pos[1]) if not at_cover else self.cover, t
            return self.cover
        self.peek = False
        near = near[:3]
        cx = sum(e["pos"][0] for e in near) / len(near)
        cy = sum(e["pos"][1] for e in near) / len(near)
        nav = self.nav
        cands = []
        for r in (6, 12, 18, 26, 36, 48, 60):
            for k in range(16):
                a = k * math.pi / 8 + r * 0.1
                x, y = pos[0] + r * math.cos(a), pos[1] + r * math.sin(a)
                if not nav.free(nav.ci(x), nav.cj(y)) or dist2((x, y), zc) > zr - 6:
                    continue
                # prefer close spots that are away from the enemies
                cands.append((r - 0.4 * math.hypot(x - cx, y - cy), x, y))
        cands.sort()
        best = None
        for _, x, y in cands:
            if time.perf_counter() - t0 > 0.008:
                break
            p = (x, y, nav.ground(x, y))
            if not self.seen_by(near, p) and nav.line_free(pos, p):
                best = (x, y)
                break
        self.cover, self.cover_t = best, t
        return best

    def pick_drop(self, obs, me, enemies):
        """Nearest supply drop worth going for: (drop, urgent). Urgent = landed and close, worth
        leaving a fight for. Skips drops someone else is capturing or an enemy stands next to."""
        pos = me["pos"]
        best = None
        for d in obs.get("drops") or []:
            holder = d.get("holder")
            if holder is not None and holder != me["id"]:
                continue
            dd = dist2(d["pos"], pos)
            if dd > 320:
                continue
            if any(dist2(e["pos"], d["pos"]) < d["radius"] + 6 for e in enemies):
                continue
            if best is None or dd < best[0]:
                best = (dd, d)
        if best is None:
            return None
        return best[1], best[1].get("lands_in", 1) <= 0 and best[0] < 45

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
                    self.threat = (pos[0] + 60 * math.cos(b), pos[1] + 60 * math.sin(b), t, b, 8.0)
        # fix #5: hunters make noise before they shoot. Turn to face fresh nearby mech sounds
        # (front armor, and the 120 deg vision wedge), unless a hit just told us where they are.
        if t - self.last_hit_t > 2.0:
            best = None
            for s in obs.get("sounds", []):
                if s.get("kind") not in HUNTER_SOUNDS or s.get("dist") == "far" or s.get("age", 9) > 1.0:
                    continue
                rank = (0 if s["dist"] == "near" else 1, s.get("age", 0))
                if best is None or rank < best[0]:
                    best = (rank, s)
            if best:
                s = best[1]
                r = 40 if s["dist"] == "near" else 110
                b = s["bearing"]
                self.threat = (pos[0] + r * math.cos(b), pos[1] + r * math.sin(b), t, b, 4.0)
        if self.threat and t - self.threat[2] > self.threat[4]:
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
                # an enemy we can actually shoot beats a closer one behind a wall
                blocked = 0.0 if self.can_hit(me, m) else 4.0
                return d / 40 + (m.get("hp", 1400) / 1400) * 2.5 + blocked
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
        # fix #3: outnumbered or hurt -> break line of sight and let them come round the corner one at a time
        threats = [m for m in enemies if dist2(m["pos"], pos) < 160]
        want_cover = False  # TEST v3: never hide
        # cover must stay inside the circle the zone is heading to, not just the current one
        cz_c, cz_r = (zc, zr) if zr < cur_r else (cur_c, cur_r)
        cover = self.find_cover(me, threats, cz_c, cz_r, t, hp_frac) if want_cover and not zone_danger else None
        if not want_cover:
            self.cover, self.peek = None, False
        # fix #4: supply drops give full HP, full energy and 3 cubes
        drop = self.pick_drop(obs, me, enemies)
        retreat = cover is None and target is not None and hp_frac < 0.35 and target.get("hp", 1400) / max(target.get("max_hp", 1400), 1) > hp_frac + 0.1
        if zone_danger:
            mode = "zone"
            move = self.go(me, t, zc)
            boost = not self.in_circle(pos, cur_c, cur_r, margin=0) or me["energy"] > 60
        elif drop is not None and (drop[1] or (tgt is None and not want_cover)):
            mode = "drop"
            d0 = drop[0]
            if dist2(d0["pos"], pos) > d0["radius"] * 0.5:
                move = self.go(me, t, d0["pos"])
                boost = me["energy"] > 50 and dist2(d0["pos"], pos) > 40
        elif cover is not None:
            mode = "cover"
            if self.peek and target is not None:
                # one enemy round the corner: shoot it, sidestepping a little but staying at the corner
                if t - self.strafe_t > 0.8 + self.rng.random() * 0.6:
                    self.strafe, self.strafe_t = -self.strafe, t
                to = math.atan2(target["pos"][1] - pos[1], target["pos"][0] - pos[0])
                perp = to + self.strafe * math.pi / 2
                sx, sy = math.cos(perp), math.sin(perp)
                ox, oy = pos[0] - cover[0], pos[1] - cover[1]
                if ox * sx + oy * sy > 3.0:  # far enough this way: come back
                    self.strafe, self.strafe_t = -self.strafe, t
                    sx, sy = -sx, -sy
                move = [sx * 0.7, sy * 0.7]
            elif dist2(cover, pos) > 2.0:
                move = self.go(me, t, cover)
                boost = me["energy"] > 40
        elif retreat:
            mode = "retreat"
            ex, ey = target["pos"][0], target["pos"][1]
            away = math.atan2(pos[1] - ey, pos[0] - ex)
            # retreat away from the enemy but toward the zone center
            to_c = math.atan2(zc[1] - pos[1], zc[0] - pos[0])
            dirn = math.atan2(math.sin(away) * 2 + math.sin(to_c), math.cos(away) * 2 + math.cos(to_c))
            goal = (pos[0] + 40 * math.cos(dirn), pos[1] + 40 * math.sin(dirn))
            hide = self.find_cover(me, threats or [target], cz_c, cz_r, t, 0.0)  # IDEA k1: break line of sight
            if hide is not None and dist2(hide, pos) > 2.0:
                goal = hide
            move = self.go(me, t, goal)
            boost = me["energy"] > 20
        elif tgt is not None:
            mode = "fight"
            d = dist2(tgt["pos"], pos)
            want = 25 if "lightning" in held else 35  # TEST v12 brawl: close range, where our hits land and lightning works
            if tgt.get("kind") == "npc":
                want = 25 if "lightning" in held else 40
            to = math.atan2(tgt["pos"][1] - pos[1], tgt["pos"][0] - pos[0])
            # TEST v9 dodge: zig-zag every 0.45-0.95 s so lead-aiming shooters miss
            if t - self.strafe_t > self.strafe_len:
                self.strafe = -self.strafe
                self.strafe_t = t
                self.strafe_len = 0.45 + self.rng.random() * 0.5
            perp = to + self.strafe * math.pi / 2
            radial = 0.0
            if d > want + 15:
                radial = 1.0
            elif d < want - 10:
                radial = -0.8
            mx = math.cos(perp) * 0.8 + math.cos(to) * radial
            my = math.sin(perp) * 0.8 + math.sin(to) * radial
            if d > want + 40 or (tgt.get("kind") == "mech" and not self.can_hit(me, tgt)):
                # TEST v12 brawl: close in fast, and chase round corners instead of waiting
                move = self.go(me, t, tgt["pos"])
                boost = me["energy"] > 50 and tgt.get("kind") == "mech"
            else:
                if self.nav is not None and not self.nav.line_free(pos, (pos[0] + mx * 6, pos[1] + my * 6)):
                    # strafing into a wall: switch sides
                    self.strafe, self.strafe_t = -self.strafe, t
                    perp = to + self.strafe * math.pi / 2
                    mx = math.cos(perp) * 0.8 + math.cos(to) * radial
                    my = math.sin(perp) * 0.8 + math.sin(to) * radial
                n = math.hypot(mx, my) or 1
                move = [mx / n, my / n]
                # TEST v9 dodge: boost the sidestep when energy allows (keep some for lightning)
                boost = me["energy"] > (60 if "lightning" in held else 35) and tgt.get("kind") == "mech"
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
            if goal is None and self.threat is not None and hp_frac > 0.6 and self.in_circle(self.threat[:2], zc, zr):
                # TEST v14 hunt: a heard or shooting hunter is nearby -> go meet it while it is alone
                goal = self.threat[:2]
                mode = "hunt"
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
            d = math.dist(tgt["pos"], pos)
            clear = self.can_hit(me, tgt)  # fix #1: never spend shots on a wall
            laser_charging = any(w and w["kind"] == "laser" and w.get("charge", 0) > 0 for w in weapons)
            # fix #2: one aim per tick. Lob the mortar only when the direct weapons can't work
            # (no line of fire, gun empty/reloading) or we already committed to a lob.
            mortar_aim = None
            mi = next((i for i, w in enumerate(weapons) if w and w["kind"] == "mortar"), None)
            if mi is not None and not laser_charging and tgt.get("kind") == "mech":
                mw = weapons[mi]
                gun_busy = gun is None or gun.get("ammo", 1) <= 0 or gun.get("reload", 0) > 0
                if mw.get("cooldown", 0) <= 0 and 25 < d < 230 and (not clear or gun_busy or t < self.mortar_lock):
                    mortar_aim = self.mortar_solve(me, tgt, mw, lob=not clear) or (self.mortar_solve(me, tgt, mw, lob=False) if not clear else None)
            if mortar_aim is not None:
                if t >= self.mortar_lock:
                    self.mortar_lock = t + 1.0
                aim = list(mortar_aim)
                if abs(me["aim"][1] - mortar_aim[1]) < 0.04 and abs(wrap(me["aim"][0] - mortar_aim[0])) < 0.04:
                    fire[mi] = True
                    self.mortar_lock = -99
            err = abs(wrap(me["aim"][0] - yaw)) + abs(me["aim"][1] - pitch) * 0.5
            for i, w in enumerate(weapons):
                if not w:
                    continue
                k = w["kind"]
                if k == "gun":
                    if clear and mortar_aim is None and d < w["stats"]["range"] and err < max(0.04, 3.5 / max(d, 1)) and w.get("ammo", 1) > 0:
                        fire[i] = True
                elif k == "lightning":
                    if clear and d < w["stats"]["range"] and abs(wrap(me["aim"][0] - math.atan2(tgt["pos"][1] - pos[1], tgt["pos"][0] - pos[0]))) < math.radians(w["stats"]["cone_deg"]) * 0.9 and me["energy"] > w["stats"].get("energy", 3):
                        fire[i] = True
                elif k == "laser":
                    ch = w.get("charge", 0)
                    if ch > 0:
                        # charging: hold until full, on target and with a clear line, then let go
                        fire[i] = not (clear and ch >= w["stats"]["charge"] and err <= 0.03 and d < w["stats"]["range"])
                    elif clear and mortar_aim is None and d < w["stats"]["range"] and w.get("cooldown", 0) <= 0:
                        fire[i] = True  # start charging
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
