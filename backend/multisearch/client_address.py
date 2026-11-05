from ipaddress import IPv4Address, IPv6Address, ip_address

type IPAddress = IPv4Address | IPv6Address


def parse_client_address(value: str | None) -> IPAddress | None:
    if not value or "%" in value:
        return None
    try:
        address = ip_address(value)
    except ValueError:
        return None
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address
