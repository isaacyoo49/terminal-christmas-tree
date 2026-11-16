#!/usr/bin/env python3
"""A rotating ASCII Christmas tree with an animated build, snow, and a sleigh."""

import argparse
import base64
import ctypes
import subprocess
import tempfile
import zlib
from functools import lru_cache
import math
import os
import random
import select
import shutil
import signal
import sys
import termios
import time
import tty

ESC = "\x1b["
TAU = math.tau
FPS = 30
GOLD = (255, 184, 63)
CREAM = (255, 238, 175)
FIR = "xx%xxXx%/\\x"
TIPS = "%x#+/:X"
LEVELS = (0, 95, 135, 175, 215, 255)
CUBE = [min(range(6), key=lambda i: abs(v - LEVELS[i])) for v in range(256)]

SLEIGH = (
    "     .rrrr.                               ^  ^  ",
    "   .rrrrrrrw.                            ^ ^^ ^ ",
    "   wwwwwwwwww.           _           ..--b--bRR",
    "    wp.p.pww    b     __/r)   __    bbbbbbbbbb ",
    "  rrrwwwwwwwrrrbbbrrr/rrr/----==----bbbbbb     ",
    "  rrrrrrrrrrrrrrrrrrrrrr/          b  b  b    ",
    "    gg================g/           '  '  '    ",
    "     .ggggggggggggggg-'                        ",
)
SLEIGH_COLORS = {"r": (176, 25, 53), "R": (255, 57, 71), "w": (249, 232, 208),
                 "p": (226, 157, 121), "b": (134, 88, 62), "^": (162, 172, 159),
                 "g": (220, 151, 58), "=": (220, 151, 58), ".": (103, 112, 130)}


def clamp(x):
    return min(1.0, max(0.0, x))


def mix(a, b, fraction):
    return tuple(round(x + (y - x) * fraction) for x, y in zip(a, b))


NEEDLE_COLORS = tuple((10 + i * 6, 38 + i * 14, 16 + i * 4) for i in range(16))
FROST_COLORS = tuple(mix(rgb, (136, 198, 207), 0.39) for rgb in NEEDLE_COLORS)


def envelope(h):
    return (1 - h) ** 0.86 * (1 - 0.09 * math.sin(h * 37)) + 0.018


def build_assets():
    rng = random.Random(1225)
    needles, garland, baubles, bulbs = [], [], [], []
    for tier in range(34):
        h = 0.055 + tier * 0.0278
        count = 8 + round(6 * (1 - h))
        for branch in range(count):
            angle = branch * TAU / count + tier * 2.399 + rng.uniform(-0.12, 0.12)
            c, s = math.cos(angle), math.sin(angle)
            length = envelope(h) * rng.uniform(0.87, 1.06)
            for _ in range(22 + round(length * 122)):
                u = rng.random() ** 0.65
                lateral = rng.uniform(-1, 1) * length * (0.035 + 0.15 * (1 - u))
                r = length * u
                jitter = rng.uniform(-0.014, 0.019)
                height = h + length * (0.026 - 0.093 * u * u) + jitter
                # Upper needles catch light, lower ones sit in shadow: dappled tiers.
                shade = rng.uniform(0.38, 0.84) + u * 0.12 + jitter * 5.5
                frost = u > 0.73 and rng.random() < 0.13
                glyph = rng.choice(TIPS if u > 0.87 else FIR)
                needles.append((c * r - s * lateral, max(0, height), s * r + c * lateral,
                                shade, glyph, frost))

    rng = random.Random(867)
    for index in range(610):
        pos = index / 609
        h = 0.10 + 0.76 * pos
        a = pos * TAU * 5.0
        r = envelope(h) * 1.015
        garland.append((r * math.cos(a), h, r * math.sin(a)))

    ornament_colors = [(222, 22, 38)] * 6 + [(214, 150, 42), (176, 205, 214)]
    for index in range(54):
        h = rng.uniform(0.08, 0.84)
        a = index * 2.399 + rng.uniform(-0.23, 0.23)
        r = envelope(h) * rng.uniform(1.00, 1.10)
        baubles.append((r * math.cos(a), h, r * math.sin(a),
                        rng.choice(ornament_colors), rng.uniform(0.013, 0.024)))

    for index in range(224):
        h = 0.052 + 0.85 * ((index + 0.5) / 224) ** 1.18
        a = index * 2.399 + rng.uniform(-0.25, 0.25)
        r = envelope(h) * rng.uniform(0.84, 1.14)
        bulbs.append((r * math.cos(a), h, r * math.sin(a),
                      rng.uniform(0.9, 2.8), rng.random() * TAU, rng.random()))
    snow = [(rng.random(), rng.random(), rng.random(), rng.random() * TAU)
            for _ in range(1700)]
    return needles, garland, baubles, bulbs, snow


@lru_cache(maxsize=32)
def halo_offsets(radius, k=4.8):
    result = []
    for dy in range(-radius, radius + 1):
        for dx in range(-2 * radius, 2 * radius + 1):
            d = ((dx * 0.52) ** 2 + dy * dy) / (radius * radius)
            if d < 1:
                weight = math.exp(-k * d) - 0.008
                if weight > 0:
                    result.append((dx, dy, weight))
    return result


class Canvas:
    def __init__(self, width, height, paper, ground):
        self.w, self.h = width, height
        self.paper = paper.copy()
        self.glyph = [" "] * (width * height)
        self.ink = [(0, 0, 0)] * (width * height)
        self.depth = [-9.0] * (width * height)
        for i, char, rgb in ground:
            self.glyph[i] = char
            self.ink[i] = rgb

    def put(self, x, y, z, char, rgb, slack=0):
        if not (0 <= x < self.w and 0 <= y < self.h):
            return False
        i = y * self.w + x
        if z < self.depth[i] - slack:
            return False
        self.depth[i] = max(z, self.depth[i])
        self.glyph[i], self.ink[i] = char, rgb
        return True

    def halo(self, x, y, rgb, radius, strength, k=4.8):
        for dx, dy, alpha in halo_offsets(radius, k):
            xx, yy = x + dx, y + dy
            if 0 <= xx < self.w and 0 <= yy < self.h:
                i = yy * self.w + xx
                k = alpha * strength
                r, g, b = self.paper[i]
                self.paper[i] = (min(255, r + round(rgb[0] * k)),
                                 min(255, g + round(rgb[1] * k)),
                                 min(255, b + round(rgb[2] * k)))
                if self.glyph[i] != " " and self.depth[i] > -2:
                    r, g, b = self.ink[i]
                    self.ink[i] = (min(255, r + round(rgb[0] * k * 0.84)),
                                   min(255, g + round(rgb[1] * k * 0.84)),
                                   min(255, b + round(rgb[2] * k * 0.84)))


class Scene:
    def __init__(self, width, height):
        self.w, self.h = width, height
        self.cx = (width - 1) / 2
        self.floor = round(height * 0.80)
        self.base = round(height * 0.855)
        self.scale = max(1, min(height * 0.72, height - 11, width * 0.72))
        self.spread = min(width * 0.35, self.scale * 0.72)
        self.paper, self.ground = self.backdrop()

    def backdrop(self):
        paper, ground = [], []
        rng = random.Random(403)
        distant_firs = [
            (self.w * offset, self.h * rng.uniform(0.037, 0.15), rng.uniform(0.52, 1.10))
            for offset in (0.015, 0.056, 0.109, 0.168, 0.216, 0.278,
                           0.724, 0.78, 0.825, 0.878, 0.925, 0.979)
        ]
        horizon = []
        for x in range(self.w):
            ridge = self.floor + self.h * (0.012 * math.sin(x * 0.093)
                                            + 0.007 * math.sin(x * 0.032))
            pine = max(max(0, height - abs(x - pos) / spread)
                       for pos, height, spread in distant_firs)
            horizon.append((ridge, ridge - pine))
        for y in range(self.h):
            for x in range(self.w):
                nx = (x - self.cx) / max(1, self.w * 0.39)
                ny = (y - self.base * 0.56) / max(1, self.h * 0.64)
                haze = math.exp(-2.9 * nx * nx - 2.3 * ny * ny)
                ridge, pine = horizon[x]
                sky = (3 + round(10 * haze), 7 + round(14 * haze), 17 + round(27 * haze))
                if y >= ridge:
                    distance = (y - ridge) / max(1, self.h - ridge)
                    stride = max(1, round(3 * (1 - distance)))
                    wave = 0.5 + 0.5 * math.cos(x * 0.12 + y * 1.16 + math.sin(x * 0.04))
                    sky = mix((6, 13, 24), (20, 38, 58), clamp(0.27 + distance * 0.22 + wave * 0.35))
                    if x % stride == 0 or rng.random() > 0.46:
                        brush = clamp(0.13 + wave * 0.34 + 0.26 * rng.random() + 0.20 * haze)
                        char = rng.choice("...--:+ " if distance > 0.46 else "...-:")
                        ground.append((y * self.w + x, char,
                                       mix((26, 42, 61), (82, 108, 142), brush)))
                elif pine <= y:
                    amount = clamp(0.32 + 0.40 * haze)
                    sky = mix(sky, (3, 10, 18), amount)
                    if (x * 11 + y * 97) % 13 < 6:
                        ground.append((y * self.w + x, "/" if x % 2 else "\\",
                                       mix((15, 26, 37), (40, 61, 86), amount)))
                paper.append(sky)
        return paper, ground

    def frame(self, elapsed, angle, assets, intro=True):
        canvas = Canvas(self.w, self.h, self.paper, self.ground)
        w, h, base = self.w, self.h, self.base
        t = elapsed if intro else elapsed + 18
        needles, garland, baubles, bulbs, snow = assets
        c, s = math.cos(angle), math.sin(angle)

        def project(x, y, z):
            side, front = x * c - z * s, x * s + z * c
            perspective = 1 + front * 0.105
            return (round(self.cx + side * self.spread * perspective),
                    round(base - y * self.scale + front * self.scale * 0.066), front)

        snow_count = min(len(snow), max(75, w * h // 15))
        for ox, oy, layer, phase in snow[:snow_count]:
            if layer >= 0.83:
                continue
            x = int(w * ((ox + elapsed * (0.007 + layer * 0.015)
                          + 0.008 * math.sin(elapsed * 0.8 + phase)) % 1))
            y = int(h * ((oy + elapsed * (0.019 + layer * 0.061)) % 1))
            dim = 0.18 + 0.65 * layer + 0.07 * math.sin(elapsed * 3 + phase)
            canvas.put(x, y, -5, "." if layer < 0.73 else "+",
                       mix((44, 58, 77), (191, 203, 225), dim))

        stem = clamp(t / 1.12)
        for row in range(round(base - stem * self.scale), base + 2):
            fraction = clamp((base - row) / max(1, self.scale))
            radius = max(1, round(self.scale * (0.047 - 0.032 * fraction)))
            for dx in range(-radius, radius + 1):
                char = "xX%/|"[(row * 13 + dx * 7) % 5]
                canvas.put(round(self.cx) + dx, row, -0.19, char,
                           mix((67, 37, 20), (153, 92, 43), 0.66 - 0.37 * dx / radius))

        growth = clamp((t - 1.12) / 4.20) * 1.04
        center, spread, scale = self.cx, self.spread, self.scale
        ground_light = clamp((t - 9.7) / 3)
        if ground_light:
            for offset, strength in ((-0.55, 0.11), (0, 0.18), (0.58, 0.11)):
                xx = round(center + offset * spread)
                canvas.halo(xx, round(base + h * 0.042), GOLD, max(2, round(h * 0.09)),
                            ground_light * strength)

        # This is the busiest draw loop, so write directly to the depth buffer.
        for x, yy, z, shade, char, frost in needles:
            if yy > growth or t < 1.12:
                continue
            side, front = x * c - z * s, x * s + z * c
            col = round(center + side * spread * (1 + front * 0.105))
            row = round(base - yy * scale + front * scale * 0.066)
            if 0 <= col < w and 0 <= row < h:
                cell = row * w + col
                if front >= canvas.depth[cell]:
                    light = int(clamp(shade + 0.25 * front - 0.13 * side) ** 0.8 * 15)
                    canvas.depth[cell] = front
                    canvas.glyph[cell] = char
                    canvas.ink[cell] = (FROST_COLORS if frost else NEEDLE_COLORS)[light]

        ribbon = round(len(garland) * clamp((t - 5.4) / 2.9))
        for index, (x, yy, z) in enumerate(garland[:ribbon]):
            col, row, front = project(x, yy, z)
            if front < 0 or (index % 7 == 0 and index % 2):
                continue
            char = "-=~="[index % 4]
            color = mix((82, 49, 23), (207, 157, 58), clamp(0.40 + front * 0.87))
            canvas.put(col, row, front + 0.012, char, color, slack=0.033)

        visible_baubles = []
        for x, yy, z, color, size in baubles[:round(len(baubles) * clamp((t - 7.1) / 3.0))]:
            col, row, front = project(x, yy, z)
            visible_baubles.append((front, col, row, color, size))
        for front, col, row, color, size in sorted(visible_baubles):
            if front < -0.11:
                continue
            yr = max(0.72, self.scale * size)
            xr = yr * 1.85
            highlight_x = round(-0.30 * xr)
            highlight_y = round(-0.42 * yr)
            visible = False
            for dy in range(-math.ceil(yr), math.ceil(yr) + 1):
                for dx in range(-math.ceil(xr), math.ceil(xr) + 1):
                    nx, ny = dx / xr, dy / yr
                    rr = nx * nx + ny * ny
                    if rr > 1:
                        continue
                    nz = math.sqrt(1 - rr)  # sphere normal, light from the upper left
                    diffuse = max(0.0, -0.45 * nx - 0.55 * ny + 0.70 * nz)
                    spec = max(0.0, -0.30 * nx - 0.42 * ny + 0.86 * nz) ** 20
                    # Give small spheres a visible highlight on the character grid.
                    if dx == highlight_x and dy == highlight_y:
                        spec = max(0.82, spec)
                    bounce = max(0.0, 0.5 * nx + 0.6 * ny) ** 3 * 0.30
                    value = (0.20 + 0.95 * diffuse + bounce) * (0.62 + 0.38 * nz)
                    ink = tuple(min(255, round(v * value)) for v in color)
                    ink = mix(ink, (255, 238, 222), clamp(spec * 1.25))
                    char = "*" if spec > 0.60 else "@" if rr < 0.35 else "o" if rr < 0.78 else "."
                    if canvas.put(col + dx, row + dy, front + 0.045 * (1 - rr),
                                  char, ink, slack=0.047):
                        visible = True
                        # Tint the cell behind the glyph so the ball reads as solid.
                        cell = (row + dy) * w + col + dx
                        backdrop = tuple(round(v * (0.17 + 0.43 * value)) for v in color)
                        canvas.paper[cell] = mix(canvas.paper[cell], backdrop, 0.34 + 0.62 * nz)
            if visible:
                canvas.halo(col, row, color, 2, 0.10, 7.0)

        halos = []
        light_cells = set()
        for x, yy, z, rate, phase, strength in bulbs[:round(len(bulbs) * clamp((t - 9.7) / 3.0))]:
            col, row, front = project(x, yy, z)
            if front < -0.07:
                continue
            if (col, row) in light_cells:
                continue
            pulse = (0.5 + 0.5 * math.sin(rate * elapsed + phase)) ** 15
            weight = clamp(0.55 + 0.40 * pulse + strength * 0.15)
            color = mix(GOLD, (255, 255, 230), clamp(pulse + strength * 0.35))
            if canvas.put(col, row, front + 0.025, "@" if pulse > 0.70 else "*" if pulse > 0.2 else "o",
                          mix((205, 148, 52), color, weight), slack=0.064):
                light_cells.add((col, row))
                halos.append((col, row, color, weight, pulse))

        for col, row, color, weight, pulse in halos:
            canvas.halo(col, row, GOLD, max(2, round(h * 0.035)), 0.025 + pulse * 0.15, 6.6)
            canvas.halo(col, row, CREAM, 1, 0.03 + pulse * 0.09, 9.0)
            if pulse > 0.62:
                flare = mix(GOLD, CREAM, 0.63 + pulse * 0.28)
                rays = [(-2, 0, "."), (-1, 0, "-"), (1, 0, "-"),
                        (2, 0, "."), (0, -1, "|"), (0, 1, "|")]
                if pulse > 0.87 and h > 30:
                    rays += [(-3, 0, "."), (3, 0, "."), (-2, -1, "\\"),
                             (2, -1, "/"), (-2, 1, "/"), (2, 1, "\\")]
                    canvas.put(col, row, 1.19, "*", (255, 251, 225))
                for dx, dy, char in rays:
                    reach = math.hypot(dx * 0.53, dy)
                    ink = mix((120, 80, 36), flare, max(0.45, pulse) * max(0.30, 0.98 - 0.30 * reach))
                    canvas.put(col + dx, row + dy, 1.18, char, ink)

        # Flare rays cannot overwrite neighboring bulb cores.
        for col, row, color, weight, pulse in halos:
            canvas.put(col, row, 1.20, "*" if pulse > 0.62 else "o",
                       mix(GOLD, (255, 255, 238), clamp(0.17 + pulse)))

        if t >= 12.8:
            self.star(canvas, elapsed, clamp((t - 12.8) / 0.85))

        if t > 15.0:
            self.presents(canvas, angle, t)
        stage = (t - 17.4) % 36 if t >= 17.4 else -1
        if 0 <= stage < 6.1:
            xx = round(-len(SLEIGH[0]) + (w + len(SLEIGH[0])) * stage / 6.1)
            self.sleigh(canvas, xx, round(h * 0.29 + 1.2 * math.sin(stage * 0.9)), elapsed)

        for ox, oy, layer, phase in snow[:snow_count]:
            if layer < 0.83:
                continue
            x = int(w * ((ox + elapsed * (0.022 + layer * 0.024)
                          + 0.012 * math.sin(elapsed + phase)) % 1))
            y = int(h * ((oy + elapsed * (0.066 + 0.11 * layer)) % 1))
            glint = (0.5 + 0.5 * math.sin(elapsed * 3.2 + phase)) ** 7
            canvas.put(x, y, 1.40, "*" if glint > 0.46 else ".",
                       mix((118, 141, 167), (231, 241, 255), 0.21 + glint * 0.79))
        return canvas

    def star(self, canvas, t, scale):
        x, y = round(self.cx), round(self.base - self.scale) - 2
        pulse = 0.5 + 0.5 * math.sin(t * 2.5)
        core = mix((237, 172, 70), (255, 247, 199), scale)
        canvas.halo(x, y, GOLD, max(4, round(self.h * 0.12)), scale * (0.24 + 0.13 * pulse))
        canvas.halo(x, y, CREAM, 3, scale * (0.45 + 0.16 * pulse))
        radius = max(1.5, min(4.0, self.h * 0.038))
        vertices = []
        for n in range(10):
            a = n * math.pi / 5 - math.pi / 2
            r = radius * (1 if n % 2 == 0 else 0.44)
            vertices.append((math.cos(a) * r, math.sin(a) * r))
        for dy in range(-math.ceil(radius), math.ceil(radius) + 1):
            for dx in range(-math.ceil(radius * 2), math.ceil(radius * 2) + 1):
                px = dx * 0.52
                crossings = sum(
                    (ay > dy) != (by > dy) and px < ax + (dy - ay) * (bx - ax) / (by - ay)
                    for (ax, ay), (bx, by) in zip(vertices, vertices[1:] + vertices[:1])
                )
                if crossings % 2:
                    value = clamp(0.75 - 0.12 * px + 0.09 * pulse)
                    char = "*" if abs(px) + abs(dy) < 1.6 else "+"
                    canvas.put(x + dx, y + dy, 1.6, char,
                               mix((174, 109, 28), core, value * scale))
        for reach in range(1, max(4, round(radius * 2.5)) + 1):
            value = scale * (0.65 + 0.2 * pulse) / reach ** 0.64
            for dx, dy, char in ((reach * 2, 0, "-"), (-reach * 2, 0, "-"),
                                 (0, reach, "|"), (0, -reach, "|")):
                canvas.put(x + dx, y + dy, 1.45, char,
                           mix((29, 27, 27), CREAM, value))
        canvas.put(x, y, 1.7, "*", (255, 253, 220))

    def presents(self, canvas, angle, t):
        colors = ((159, 27, 51), (37, 74, 162), (141, 105, 37),
                  (33, 107, 76), (164, 44, 47))
        boxes = []
        for index, color in enumerate(colors):
            stage = clamp((t - 15 - index * 0.55) / 0.42)
            if not stage:
                continue
            a = angle + index * TAU / 5 + 0.21
            front = math.sin(a)
            x = round(self.cx + math.cos(a) * self.spread * 1.05)
            y = round(self.base + front * self.h * 0.068 - (1 - stage) * 4)
            boxes.append((front, x, y, color, index))
        for front, x, y, color, index in sorted(boxes):
            rows = max(2, round(self.scale * (0.055 + (index % 3) * 0.011)))
            cols = max(2, round(rows * 1.22))
            depth = front * 0.82
            lid = mix(color, (248, 220, 166), 0.30)
            foil = (226, 165, 62)
            fold = max(2, cols // 2)
            for col in range(-cols + 2, cols + 1):
                ribbon = abs(col) < 2
                canvas.put(x + col + 1, y - rows - 1, depth, "X" if ribbon else "/",
                           foil if ribbon else lid)
            for row in range(-rows, 1):
                for col in range(-cols, cols + 1):
                    ribbon = abs(col + 1) < 2 or row == -rows + 1
                    rgb = foil if ribbon else lid if row == -rows else color
                    if col > fold:
                        rgb = mix((16, 14, 22), rgb, 0.54)
                    pattern = "%#==:#*:"[(index + (col * 3 + row * 7) % 8) % 8]
                    char = "X" if ribbon else "|" if col in (-cols, fold, cols) else pattern
                    canvas.put(x + col, y + row, depth, char, rgb)
            canvas.put(x - 3, y - rows - 2, depth, "o", foil)
            canvas.put(x - 2, y - rows - 2, depth, "o", foil)

    def sleigh(self, canvas, x, y, t):
        for dy, row in enumerate(SLEIGH):
            for dx, mark in enumerate(row):
                if mark == " ":
                    continue
                color = SLEIGH_COLORS.get(mark, (135, 107, 79))
                char = {"r": "=", "w": "#", "p": "o", "b": "-", "R": "*", "g": "="}.get(mark, mark)
                if canvas.put(x + dx, y + dy, -0.10, char, color) and mark == "R":
                    canvas.halo(x + dx, y + dy, (255, 15, 33), 2, 0.21 + 0.1 * math.sin(t * 6))


@lru_cache(maxsize=8192)
def color_code(rgb, truecolor):
    r, g, b = rgb
    if truecolor:
        return f"2;{r};{g};{b}"
    ri, gi, bi = CUBE[r], CUBE[g], CUBE[b]
    cr, cg, cb = LEVELS[ri], LEVELS[gi], LEVELS[bi]
    cube_error = 2 * (r - cr) ** 2 + 4 * (g - cg) ** 2 + 2 * (b - cb) ** 2
    gray_i = min(23, max(0, round(((r + 2 * g + b) / 4 - 8) / 10)))
    v = 8 + gray_i * 10
    gray_error = 2 * (r - v) ** 2 + 4 * (g - v) ** 2 + 2 * (b - v) ** 2
    return f"5;{232 + gray_i if gray_error < cube_error else 16 + 36 * ri + 6 * gi + bi}"


def ansi_frame(canvas, left, top, truecolor):
    output = []
    last_fg = last_bg = None
    for row in range(canvas.h):
        output.append(f"{ESC}{top + row + 1};{left + 1}H")
        start = row * canvas.w
        for i in range(start, start + canvas.w):
            char = canvas.glyph[i]
            fr, fg, fb = canvas.ink[i]
            br, bg, bb = canvas.paper[i]
            ink = color_code((fr & 248, fg & 248, fb & 248), truecolor) if char != " " else last_fg
            paper = color_code((br & 248, bg & 248, bb & 248), truecolor)
            if ink != last_fg and paper != last_bg:
                output.append(f"{ESC}38;{ink};48;{paper}m")
            elif ink != last_fg:
                output.append(f"{ESC}38;{ink}m")
            elif paper != last_bg:
                output.append(f"{ESC}48;{paper}m")
            output.append(char)
            last_fg, last_bg = ink, paper
    # Cursor positioning clears the pending wrap after the bottom-right cell.
    return "".join(output) + ESC + "0m" + ESC + "H"


NATIVE_RENDERER = r'''
#include <CoreGraphics/CoreGraphics.h>
#include <CoreText/CoreText.h>
#include <stdlib.h>
#include <math.h>
#include <stdint.h>

#define CW 6
#define CH 12
static unsigned char atlas[128][CW * CH];
static int initialized = 0;

static void make_atlas(void) {
    CGColorSpaceRef gray = CGColorSpaceCreateDeviceGray();
    CTFontRef font = CTFontCreateWithName(CFSTR("Menlo"), 10.0, NULL);
    for (int c = 32; c < 127; c++) {
        CGContextRef ctx = CGBitmapContextCreate(atlas[c], CW, CH, 8, CW, gray, kCGImageAlphaNone);
        if (!ctx) continue;
        CGContextSetGrayFillColor(ctx, 1, 1);
        CGContextSetShouldAntialias(ctx, true);
        UniChar character = (UniChar)c;
        CGGlyph glyph;
        if (CTFontGetGlyphsForCharacters(font, &character, &glyph, 1)) {
            CGPoint point = CGPointMake(0, 2);
            CTFontDrawGlyphs(font, &glyph, &point, 1, ctx);
        }
        CGContextRelease(ctx);
    }
    CFRelease(font);
    CGColorSpaceRelease(gray);
    initialized = 1;
}

/* Separable box blur with a running sum; cost is independent of radius. */
static void blur(float *src, float *tmp, int w, int h, int radius) {
    float norm = 1.0f / (radius * 2 + 1);
    for (int y = 0; y < h; y++) {
        for (int c = 0; c < 3; c++) {
            float sum = 0;
            for (int k = -radius; k <= radius; k++) {
                int x = k < 0 ? 0 : k >= w ? w - 1 : k;
                sum += src[(y * w + x) * 3 + c];
            }
            for (int x = 0; x < w; x++) {
                tmp[(y * w + x) * 3 + c] = sum * norm;
                int left = x - radius;
                int right = x + radius + 1;
                if (left < 0) left = 0;
                if (right >= w) right = w - 1;
                sum += src[(y * w + right) * 3 + c] - src[(y * w + left) * 3 + c];
            }
        }
    }
    for (int x = 0; x < w; x++) {
        for (int c = 0; c < 3; c++) {
            float sum = 0;
            for (int k = -radius; k <= radius; k++) {
                int y = k < 0 ? 0 : k >= h ? h - 1 : k;
                sum += tmp[(y * w + x) * 3 + c];
            }
            for (int y = 0; y < h; y++) {
                src[(y * w + x) * 3 + c] = sum * norm;
                int top = y - radius;
                int bottom = y + radius + 1;
                if (top < 0) top = 0;
                if (bottom >= h) bottom = h - 1;
                sum += tmp[(bottom * w + x) * 3 + c] - tmp[(top * w + x) * 3 + c];
            }
        }
    }
}

int render(int cols, int rows, const unsigned char *glyphs, const unsigned char *ink,
           const unsigned char *paper, unsigned char *out) {
    if (cols < 1 || rows < 1 || cols > 500 || rows > 300) return 1;
    if (!initialized) make_atlas();
    int w = cols * CW, h = rows * CH, count = w * h;
    float *base = calloc((size_t)count * 3, sizeof(float));
    float *glow = calloc((size_t)count * 3, sizeof(float));
    float *tmp = calloc((size_t)count * 3, sizeof(float));
    if (!base || !glow || !tmp) { free(base); free(glow); free(tmp); return 2; }

    /* Smooth the illumination behind the glyphs at pixel resolution. */
    for (int y = 0; y < h; y++) {
        float gy = ((float)y + 0.5f) / CH - 0.5f;
        int y0 = (int)floorf(gy);
        float fy = gy - y0;
        int y1 = y0 + 1;
        if (y0 < 0) y0 = 0;
        if (y1 >= rows) y1 = rows - 1;
        for (int x = 0; x < w; x++) {
            float gx = ((float)x + 0.5f) / CW - 0.5f;
            int x0 = (int)floorf(gx);
            float fx = gx - x0;
            int x1 = x0 + 1;
            if (x0 < 0) x0 = 0;
            if (x1 >= cols) x1 = cols - 1;
            int cell = (y / CH) * cols + x / CW;
            unsigned char ch = glyphs[cell];
            float coverage = ch < 128 ? atlas[ch][(y % CH) * CW + x % CW] / 255.0f : 0;
            float red = ink[cell * 3], green = ink[cell * 3 + 1], blue = ink[cell * 3 + 2];
            float energy = (red > 180 && green > 110) ? 1.65f : (red > 160 && red > green * 2) ? 0.65f : 0.05f;
            if (fabsf(red - blue) < 25 && fabsf(green - blue) < 25) energy *= 0.24f;
            if (ch != '*' && ch != '@' && ch != 'o' && ch != '.') energy *= 0.18f;
            if (ch == '*' || ch == '@') energy *= 1.25f;
            for (int c = 0; c < 3; c++) {
                float upper = paper[(y0 * cols + x0) * 3 + c] * (1 - fx)
                            + paper[(y0 * cols + x1) * 3 + c] * fx;
                float lower = paper[(y1 * cols + x0) * 3 + c] * (1 - fx)
                            + paper[(y1 * cols + x1) * 3 + c] * fx;
                float bg = upper * (1 - fy) + lower * fy;
                int i = (y * w + x) * 3 + c;
                base[i] = bg * (1 - coverage) + ink[cell * 3 + c] * coverage;
                glow[i] = ink[cell * 3 + c] * coverage * energy;
            }
        }
    }
    blur(glow, tmp, w, h, 2);
    blur(glow, tmp, w, h, 2);
    for (int i = 0; i < count * 3; i++) base[i] += glow[i] * 0.60f;
    blur(glow, tmp, w, h, 7);
    blur(glow, tmp, w, h, 7);
    for (int i = 0; i < count; i++) {
        for (int c = 0; c < 3; c++) {
            float value = base[i * 3 + c] + glow[i * 3 + c] * 1.25f;
            if (value > 230) value = 230 + 25 * (1 - expf(-(value - 230) / 25));
            out[i * 4 + c] = (unsigned char)fminf(255, fmaxf(0, value));
        }
        out[i * 4 + 3] = 255;
    }
    free(base); free(glow); free(tmp);
    return 0;
}
'''


class CinematicRenderer:
    """Draw antialiased ASCII glyphs with pixel-level bloom in Ghostty on macOS."""

    def __init__(self):
        self.directory = tempfile.TemporaryDirectory(prefix="christmas-bloom-")
        self.previous_id = None
        self.ids = (25000000 + os.getpid() * 2, 25000001 + os.getpid() * 2)
        self.sequence = 0
        try:
            source_path = os.path.join(self.directory.name, "bloom.c")
            library_path = os.path.join(self.directory.name, "bloom.dylib")
            with open(source_path, "w") as source:
                source.write(NATIVE_RENDERER)
            result = subprocess.run(
                ["/usr/bin/clang", "-O3", "-dynamiclib", source_path,
                 "-framework", "CoreGraphics", "-framework", "CoreText",
                 "-framework", "CoreFoundation", "-o", library_path],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode:
                raise RuntimeError("The macOS renderer could not compile. " + result.stderr[-1200:])
            self.library = ctypes.CDLL(library_path)
            self.library.render.argtypes = [
                ctypes.c_int, ctypes.c_int, ctypes.c_char_p,
                ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
            ]
            self.library.render.restype = ctypes.c_int
        except BaseException:
            self.directory.cleanup()
            raise

    def pixels(self, canvas):
        glyphs = "".join(canvas.glyph).encode("ascii")
        ink = bytes(channel for rgb in canvas.ink for channel in rgb)
        paper = bytes(channel for rgb in canvas.paper for channel in rgb)
        output = ctypes.create_string_buffer(canvas.w * 6 * canvas.h * 12 * 4)
        status = self.library.render(canvas.w, canvas.h, glyphs, ink, paper, output)
        if status:
            raise RuntimeError("The cinematic frame could not be rendered.")
        return output.raw

    def frame(self, canvas, cols, rows):
        payload = base64.b64encode(zlib.compress(self.pixels(canvas), 1)).decode("ascii")
        image_id = self.ids[self.sequence % 2]
        self.sequence += 1
        chunks = [ESC + "H"]
        for start in range(0, len(payload), 4096):
            data = payload[start:start + 4096]
            more = int(start + len(data) < len(payload))
            header = f"m={more},q=2"
            if start == 0:
                header = (f"a=T,f=32,s={canvas.w * 6},v={canvas.h * 12},o=z,"
                          f"i={image_id},p=1,c={cols},r={rows},C=1,q=2,m={more}")
            chunks.append("\x1b_G" + header + ";" + data + "\x1b\\")
        if self.previous_id is not None:
            chunks.append(f"\x1b_Ga=d,d=I,i={self.previous_id},q=2;\x1b\\")
        self.previous_id = image_id
        return "".join(chunks)

    def close(self):
        self.directory.cleanup()
        return "".join(f"\x1b_Ga=d,d=I,i={image_id},q=2;\x1b\\" for image_id in self.ids)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--color", choices=("auto", "256", "truecolor"), default="auto")
    parser.add_argument("--speed", type=float, default=0.314, help="rotation speed in radians per second")
    parser.add_argument("--no-intro", action="store_true", help="start with the tree fully decorated")
    parser.add_argument("--ascii", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--cinematic", action="store_true", help="smooth pixel bloom for Ghostty on macOS (requires Apple command-line tools)")
    args = parser.parse_args()
    if not math.isfinite(args.speed):
        parser.error("--speed must be finite")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.exit(1, "Run this script directly in a terminal window.\n")

    truecolor = args.color == "truecolor" or (args.color == "auto" and (
        os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")
        or os.environ.get("TERM_PROGRAM", "").lower() == "ghostty"))
    cinematic = None
    if args.cinematic:
        if sys.platform != "darwin":
            parser.exit(1, "--cinematic requires macOS and a terminal with Kitty graphics support.\n")
        try:
            cinematic = CinematicRenderer()
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            parser.exit(1, str(error) + "\nRun again without --cinematic for text mode.\n")
    assets = build_assets()
    fd = sys.stdin.fileno()
    original = termios.tcgetattr(fd)
    terminal_size = None
    paused = False
    elapsed = 0.0

    def terminate(signum, frame):
        raise KeyboardInterrupt

    previous_term = signal.signal(signal.SIGTERM, terminate)
    try:
        tty.setcbreak(fd)
        sys.stdout.write(ESC + "?1049h" + ESC + "?25l" + ESC + "2J")
        last = time.monotonic()
        while True:
            start = time.monotonic()
            if not paused:
                elapsed += start - last
            last = start
            cols, rows = shutil.get_terminal_size()
            if terminal_size != (cols, rows):
                terminal_size = cols, rows
                sys.stdout.write(ESC + "0m" + ESC + "2J")
                factor = min(2.0, math.sqrt(10000 / (cols * rows)), 180 / cols, 110 / rows) if cinematic else 1.0
                scene = Scene(max(1, round(cols * factor)), max(1, round(rows * factor)))
            if cols < 30 or rows < 14:
                sys.stdout.write(ESC + "H" + "Enlarge the terminal"[:max(0, cols - 1)])
            else:
                angle = (3.54 + elapsed * args.speed) % TAU
                frame = scene.frame(elapsed, angle, assets, intro=not args.no_intro)
                if cinematic:
                    sys.stdout.write(cinematic.frame(frame, cols, rows))
                else:
                    sys.stdout.write(ansi_frame(frame, 0, 0, truecolor))
            sys.stdout.flush()
            wait = max(0, 1 / FPS - (time.monotonic() - start))
            if select.select([fd], [], [], wait)[0]:
                for key in os.read(fd, 32):
                    if key in b"qQ":
                        return
                    if key in b"rR":
                        elapsed = 0.0
                    if key == ord(" "):
                        paused = not paused
    except KeyboardInterrupt:
        pass
    finally:
        if cinematic:
            sys.stdout.write(cinematic.close())
        termios.tcsetattr(fd, termios.TCSADRAIN, original)
        signal.signal(signal.SIGTERM, previous_term)
        sys.stdout.write(ESC + "0m" + ESC + "?25h" + ESC + "?1049l")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
