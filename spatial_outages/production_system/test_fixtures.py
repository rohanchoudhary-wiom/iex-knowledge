import math

from attribution import Device, Inventory


TEST_OUTAGES = [
    {"outage_id": "TEST-FIBRE", "devices": [f"FA{index:02d}" for index in range(11)], "ongoing_time": 1_800},
    {"outage_id": "TEST-POWER", "devices": [f"PA{index:02d}" for index in range(10)], "ongoing_time": 2_400},
    {"outage_id": "TEST-ISP", "devices": [f"IA{index:02d}" for index in range(10)], "ongoing_time": 3_600},
    {"outage_id": "TEST-UNKNOWN", "devices": [f"UA{index:02d}" for index in range(10)], "ongoing_time": 900},
]


def _points(count: int, latitude: float, longitude: float, radius: float = .0002):
    for index in range(count):
        angle = 2 * math.pi * index / count
        distance = radius * (.45 + .45 * (index % 3) / 2)
        yield latitude + math.sin(angle) * distance, longitude + math.cos(angle) * distance


def test_data() -> tuple[Inventory, dict[str, int | None]]:
    devices: dict[str, Device] = {}
    ages: dict[str, int | None] = {}

    def add(prefix: str, csp: str, count: int, center: tuple[float, float], age: int | None, radius: float = .0002) -> None:
        for index, (latitude, longitude) in enumerate(_points(count, *center, radius)):
            device_id = f"{prefix}{index:02d}"
            devices[device_id] = Device(device_id, csp, latitude, longitude)
            ages[device_id] = age

    def add_far(prefix: str, csp: str, start: int, count: int, center: tuple[float, float]) -> None:
        for offset, (latitude, longitude) in enumerate(_points(count, center[0] + .06, center[1] + .06, .002)):
            device_id = f"{prefix}{start + offset:02d}"
            devices[device_id] = Device(device_id, csp, latitude, longitude)
            ages[device_id] = 60

    fibre = 28.6139, 77.2090
    add("FA", "CSP-A", 10, fibre, 900)
    for device_id in ("FA00", "FA01"):
        device = devices[device_id]
        devices[device_id] = Device(device.device_id, device.csp_id, device.latitude, device.longitude, "H-1 Gali 2 Delhi")
    for index, (latitude, longitude) in enumerate(_points(5, *fibre, .00012), 10):
        devices[f"FA{index:02d}"] = Device(f"FA{index:02d}", "CSP-A", latitude, longitude)
        ages[f"FA{index:02d}"] = 60
    add_far("FA", "CSP-A", 15, 13, fibre)
    add("FB", "CSP-B", 5, fibre, 60, .00012)

    power = 19.0760, 72.8777
    add("PA", "CSP-C", 10, power, 900)
    for prefix, csp, age in (("PA", "CSP-C", 60), ("PB", "CSP-D", 900)):
        if prefix == "PB":
            add(prefix, csp, 10, power, age)
        for index, (latitude, longitude) in enumerate(_points(2, *power, .00012), 10):
            devices[f"{prefix}{index:02d}"] = Device(f"{prefix}{index:02d}", csp, latitude, longitude)
            ages[f"{prefix}{index:02d}"] = 60
        add_far(prefix, csp, 12, 13, power)

    add("IA", "CSP-E", 10, (12.9716, 77.5946), 900)

    unknown = 20.0, 80.0
    add("UA", "CSP-U", 10, unknown, 900)
    for index, (latitude, longitude) in enumerate(_points(5, *unknown, .00012), 10):
        devices[f"UA{index:02d}"] = Device(f"UA{index:02d}", "CSP-U", latitude, longitude)
        ages[f"UA{index:02d}"] = 60
    add_far("UA", "CSP-U", 15, 13, unknown)
    add("UB", "CSP-V", 5, unknown, None, .00012)

    return Inventory(devices), ages
