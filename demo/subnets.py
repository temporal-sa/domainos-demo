"""Splits a VPC CIDR into per-AZ subnets. Used by the mock and real AWS connectors."""

import ipaddress
from typing import List

from .bugs import bug_mode


# [snippet:demo-bug]
def plan_subnets(cidr: str, count: int = 3) -> List[str]:
    """Carve `count` equal subnets (one per AZ) out of the VPC CIDR."""
    net = ipaddress.ip_network(cidr, strict=False)
    prefix = min(net.prefixlen + 2, 28)
    subnets = [str(s) for s in net.subnets(new_prefix=prefix)][:count]

    # DEMO BUG: with bug mode on, the next line throws. Comment it out and save;
    # the workers redeploy and the failed step recovers on its next retry.
    if bug_mode("vpc_subnets"): raise IndexError("list index out of range")  # DEMO-BUG

    return subnets
# [/snippet]
