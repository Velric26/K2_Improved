"""Motion bounds shared by the K2 Adaptive PA companion and Klipper adapter."""

import math


COMMAND = "STOCK_PA_DIRECT_WALL_STROKE"
CAPABILITY = "bounded wall-flow and acceleration stroke v1"
PRINT_COMMAND = "STOCK_PA_DIRECT_PRINT_WALL"
PRINT_CAPABILITY = "printed PA tower wall and corner flow v1"
START_X = 20.
END_X = 240.
CENTER_Y = 130.
MIN_ACCEL = 500.
MAX_ACCEL = 12000.
MAX_SPEED = 400.
MAX_PA = .12
COUPON_CENTER = 130.
MIN_SPAN = 70.
MAX_SPAN = 170.
MAX_LAYER_Z = 55.
# Outward offset allowed for first-layer brim and skirt rings.
MAX_BRIM_OFFSET = 30.
# XY section of OrcaSlicer's calib/pressure_advance/tower_with_seam.drc,
# translated to center the 70 mm footprint and starting at the rear seam.
TOWER_OUTLINE = ((0., 35.), (-35., 34.75), (-35., 0.), (0., -35.), (35., 0.), (35., 34.75))
SHORTEST_SIDE_FRACTION = 34.75 / 70.


def coupon_span(points):
    """Allow acceleration, deceleration and at least 0.14 s at the requested speed."""
    required = max(point["speed"] ** 2 / point["acceleration"] + .14 * point["speed"]
                   for point in points)
    span = max(MIN_SPAN, math.ceil((required + 4.) / (5. * SHORTEST_SIDE_FRACTION)) * 5.)
    if span > MAX_SPAN:
        raise ValueError("The wall speed and acceleration need a longer straight than the coupon can fit.")
    return span


def wall_ring(span, inset, flow, speed, diameter):
    """Return the rear seam and moves for a uniformly scaled PA tower perimeter."""
    if (not all(math.isfinite(v) for v in (span, inset, flow, speed, diameter))
            or not MIN_SPAN <= span <= MAX_SPAN or not -MAX_BRIM_OFFSET <= inset <= 1.2
            or not 0 < speed <= MAX_SPEED or diameter <= 0 or not .01 <= flow / speed <= 1.):
        raise ValueError("The requested tower wall or extrusion width is outside the calibration bounds.")
    outline = [(COUPON_CENTER + x * span / 70, COUPON_CENTER + y * span / 70) for x, y in TOWER_OUTLINE]
    normals = []
    for a, b in zip(outline, outline[1:] + outline[:1]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        normals.append((-dy / length, dx / length))
    vertices = []
    for (x, y), (ax, ay), (bx, by) in zip(outline, normals[-1:] + normals[:-1], normals):
        distance = inset / (1 + ax * bx + ay * by)
        vertices.append((x + distance * (ax + bx), y + distance * (ay + by)))
    start = vertices[0]
    corners = vertices[1:] + vertices[:1]
    extrusion_ratio = flow / (speed * math.pi * (diameter / 2) ** 2)
    previous = start
    moves = []
    for point in corners:
        travel = math.hypot(point[0] - previous[0], point[1] - previous[1])
        moves.append((*point, travel * extrusion_ratio))
        previous = point
    return start, moves


def ring_gcode(moves, speed):
    return [f"G1 X{x:.5f} Y{y:.5f} E{e:.7f} F{speed * 60:.5f}" for x, y, e in moves]


def stroke_segments(flow, speed, acceleration, diameter):
    """Return (distance, XY speed, filament length) for a collinear stroke."""
    if (not all(math.isfinite(value) for value in (flow, speed, acceleration, diameter))
            or flow <= 0 or not 0 < speed <= MAX_SPEED
            or not MIN_ACCEL <= acceleration <= MAX_ACCEL or diameter <= 0):
        raise ValueError("Use a wall speed up to 400 mm/s and acceleration from 500 to 12000 mm/s².")
    slow = speed * .35
    stop_distance = slow ** 2 / (2 * acceleration)
    ramp_distance = (speed ** 2 - slow ** 2) / (2 * acceleration)
    slow_leg = stop_distance + slow * .26
    fast_leg = 2 * ramp_distance + speed * .24
    distances = (slow_leg, fast_leg, slow_leg)
    if START_X + sum(distances) > END_X:
        raise ValueError(f"{speed:g} mm/s at {acceleration:g} mm/s² needs more than the 220 mm test stroke.")
    line_area = flow / speed
    if not .01 <= line_area <= 1.0:
        raise ValueError("The test line area must be between 0.01 and 1.0 mm².")
    filament_area = math.pi * (diameter / 2) ** 2
    return tuple((length, velocity, length * line_area / filament_area)
                 for length, velocity in zip(distances, (slow, speed, slow)))
