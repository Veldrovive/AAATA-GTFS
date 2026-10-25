"""Google's encoded polyline format (used by GTFS-RT `Shape.encoded_polyline`)."""


def decode_polyline(s: str, precision: int = 5) -> list[tuple[float, float]]:
    """Decode an encoded polyline into (latitude, longitude) pairs."""
    factor = 10**precision
    coords: list[tuple[float, float]] = []
    index = lat = lon = 0
    while index < len(s):
        deltas = []
        for _ in range(2):
            result = shift = 0
            while True:
                if index >= len(s):
                    raise ValueError("truncated polyline")
                b = ord(s[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        lat += deltas[0]
        lon += deltas[1]
        coords.append((lat / factor, lon / factor))
    return coords
