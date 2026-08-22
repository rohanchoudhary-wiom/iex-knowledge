import math
from datetime import datetime, timedelta
from statistics import median

from .models import Device


EARTH_RADIUS_M = 6_371_000


def distance_m(left: Device | tuple[float, float], right: Device | tuple[float, float]) -> float:
    lat1, lon1 = (left.latitude, left.longitude) if isinstance(left, Device) else left
    lat2, lon2 = (right.latitude, right.longitude) if isinstance(right, Device) else right
    if None in {lat1, lon1, lat2, lon2}:
        return math.inf
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi, dlambda = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def variable_density_clusters(devices: list[Device], reach_scale: float = 1, max_reach: float = 1_000) -> list[list[Device]]:
    # ponytail: This deterministic third-neighbour graph is the pilot VDBSCAN approximation;
    # replace it only if labelled replay shows materially worse clusters than a vetted implementation.
    if len(devices) < 2:
        return [devices] if devices else []
    distances = [[distance_m(left, right) for right in devices] for left in devices]
    neighbor = min(3, len(devices) - 1)
    reaches = []
    for index in range(len(devices)):
        nearest = sorted(distance for other, distance in enumerate(distances[index]) if other != index)
        reaches.append(min(max_reach, max(100, nearest[neighbor - 1] * reach_scale)))
    edges: list[list[int]] = [[] for _ in devices]
    for left in range(len(devices)):
        for right in range(left + 1, len(devices)):
            if distances[left][right] <= max(reaches[left], reaches[right]):
                edges[left].append(right)
                edges[right].append(left)
    seen, groups = set(), []
    for start in range(len(devices)):
        if start in seen:
            continue
        stack, component = [start], []
        seen.add(start)
        while stack:
            current = stack.pop()
            component.append(devices[current])
            for neighbor_index in edges[current]:
                if neighbor_index not in seen:
                    seen.add(neighbor_index)
                    stack.append(neighbor_index)
        groups.append(sorted(component, key=lambda device: device.device_id))
    return sorted(groups, key=lambda group: group[0].device_id)


# Backwards-compatible name for callers and replay notebooks.
adaptive_clusters = variable_density_clusters


def anchored_time_groups(devices: list[Device], failure_times: dict[str, datetime], minutes: int = 30) -> list[list[Device]]:
    timed = sorted(devices, key=lambda device: (failure_times[device.device_id], device.device_id))
    groups: list[list[Device]] = []
    for device in timed:
        if not groups or failure_times[device.device_id] - failure_times[groups[-1][0].device_id] > timedelta(minutes=minutes):
            groups.append([device])
        else:
            groups[-1].append(device)
    return groups


def strongest_window(times: list[datetime], minutes: int = 10) -> int:
    times = sorted(times)
    best = left = 0
    for right, value in enumerate(times):
        while value - times[left] > timedelta(minutes=minutes):
            left += 1
        best = max(best, right - left + 1)
    return best


def directional_profile(devices: list[Device]) -> dict[str, float]:
    if len(devices) < 2:
        return {"length_m": 0.0, "perpendicular_p90_m": 0.0, "directionality_ratio": 0.0}
    latitude = sum(device.latitude for device in devices) / len(devices)
    longitude = sum(device.longitude for device in devices) / len(devices)
    points = [(
        math.radians(device.longitude - longitude) * EARTH_RADIUS_M * math.cos(math.radians(latitude)),
        math.radians(device.latitude - latitude) * EARTH_RADIUS_M,
    ) for device in devices]
    xx = sum(x * x for x, _ in points) / len(points)
    yy = sum(y * y for _, y in points) / len(points)
    xy = sum(x * y for x, y in points) / len(points)
    root = math.sqrt((xx - yy) ** 2 + 4 * xy ** 2)
    major, minor = (xx + yy + root) / 2, max(0.0, (xx + yy - root) / 2)
    angle = math.atan2(2 * xy, xx - yy) / 2
    along = [x * math.cos(angle) + y * math.sin(angle) for x, y in points]
    across = sorted(abs(-x * math.sin(angle) + y * math.cos(angle)) for x, y in points)
    return {
        "length_m": round(max(along) - min(along), 1),
        "perpendicular_p90_m": round(across[math.ceil(.9 * len(across)) - 1], 1),
        "directionality_ratio": round(min(999.0, math.sqrt(major / minor)) if minor > 1e-9 else 999.0 if major else 0.0, 2),
    }


def radius_profile(devices: list[Device]) -> tuple[tuple[float, float], dict[str, float]]:
    center = median(device.latitude for device in devices), median(device.longitude for device in devices)
    distances = sorted(distance_m(center, device) for device in devices)

    def quantile(percent: float) -> float:
        return round(distances[max(0, math.ceil(percent * len(distances)) - 1)], 1)

    return center, {"r70": quantile(.7), "r80": quantile(.8), "r90": quantile(.9), "r100": quantile(1)}


def radius_core(devices: list[Device], center: tuple[float, float], percent: float = .9) -> tuple[list[Device], list[Device]]:
    ordered = sorted(devices, key=lambda device: (distance_m(center, device), device.device_id))
    cutoff = math.ceil(percent * len(ordered))
    return ordered[:cutoff], ordered[cutoff:]


def cluster_stability(devices: list[Device]) -> float:
    if len(devices) < 2:
        return 1
    scores = []
    ids = {device.device_id for device in devices}
    for scale in (.85, 1.15):
        variants = variable_density_clusters(devices, reach_scale=scale)
        scores.append(max(len(ids & {device.device_id for device in group}) / len(ids) for group in variants))
    return round(min(scores), 3)


def convex_hull(devices: list[Device]) -> list[tuple[float, float]]:
    points = sorted({(device.latitude, device.longitude) for device in devices if device.latitude is not None})
    if len(points) < 3:
        return points

    def cross(origin: tuple[float, float], left: tuple[float, float], right: tuple[float, float]) -> float:
        return (left[0] - origin[0]) * (right[1] - origin[1]) - (left[1] - origin[1]) * (right[0] - origin[0])

    lower, upper = [], []
    for point in points:
        while len(lower) > 1 and cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)
    for point in reversed(points):
        while len(upper) > 1 and cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)
    return lower[:-1] + upper[:-1]


def comparison_polygon(
    devices: list[Device], center: tuple[float, float], radius_m: float
) -> list[tuple[float, float]]:
    hull = convex_hull(devices)
    if len(hull) >= 3:
        return hull
    latitude, longitude = map(math.radians, center)
    angular_radius = max(radius_m, 20) / EARTH_RADIUS_M
    points = []
    for degrees in range(0, 360, 15):
        bearing = math.radians(degrees)
        point_latitude = math.asin(
            math.sin(latitude) * math.cos(angular_radius)
            + math.cos(latitude) * math.sin(angular_radius) * math.cos(bearing)
        )
        point_longitude = longitude + math.atan2(
            math.sin(bearing) * math.sin(angular_radius) * math.cos(latitude),
            math.cos(angular_radius) - math.sin(latitude) * math.sin(point_latitude),
        )
        points.append((math.degrees(point_latitude), math.degrees(point_longitude)))
    return points


def inside_polygon(device: Device, polygon: list[tuple[float, float]]) -> bool:
    if device.latitude is None or len(polygon) < 3:
        return False
    point, inside = (device.latitude, device.longitude), False
    for index, left in enumerate(polygon):
        right = polygon[index - 1]
        cross = (point[0] - left[0]) * (right[1] - left[1]) - (point[1] - left[1]) * (right[0] - left[0])
        if abs(cross) < 1e-12 and min(left[0], right[0]) <= point[0] <= max(left[0], right[0]) and min(left[1], right[1]) <= point[1] <= max(left[1], right[1]):
            return True
        if (left[1] > point[1]) != (right[1] > point[1]):
            intersection = (right[0] - left[0]) * (point[1] - left[1]) / (right[1] - left[1]) + left[0]
            if point[0] < intersection:
                inside = not inside
    return inside
