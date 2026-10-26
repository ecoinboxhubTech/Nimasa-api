"""Where a location sits (coastal, inland waterway or offshore) and which alert rules apply to which zone.

Kept in code, not in the database, so existing deployments need no schema change. Locations and rules that are not
listed here are treated as coastal / apply everywhere, which is how the original rules behave."""

COASTAL, WATERWAY, OFFSHORE = "coastal", "waterway", "offshore"

ZONE_OF = {
    "badagry-creek": WATERWAY,
    "lagos-lagoon": WATERWAY,
    "lagos-offshore": OFFSHORE,
}

# rule id -> (category shown on the alert, zones the rule is evaluated for)
RULE_META = {
    "wave-high": ("coastal", (COASTAL, OFFSHORE)),
    "nav-ocean-waves": ("ocean_navigation", (COASTAL, OFFSHORE)),
    "nav-ocean-wind": ("ocean_navigation", (COASTAL, OFFSHORE)),
    "nav-ocean-visibility": ("ocean_navigation", (COASTAL, OFFSHORE)),
    "nav-ocean-current": ("ocean_navigation", (COASTAL, OFFSHORE)),
    "nav-waterway-wind": ("waterway_navigation", (WATERWAY,)),
    "nav-waterway-visibility": ("waterway_navigation", (WATERWAY,)),
    "nav-waterway-current": ("waterway_navigation", (WATERWAY,)),
}
ALL_ZONES = (COASTAL, WATERWAY, OFFSHORE)


def zone_of(location_id: str) -> str:
    return ZONE_OF.get(location_id, COASTAL)


def category_of(rule_id: str) -> str:
    return RULE_META.get(rule_id, ("coastal", ALL_ZONES))[0]


def rule_applies(rule_id: str, location_id: str) -> bool:
    return zone_of(location_id) in RULE_META.get(rule_id, ("coastal", ALL_ZONES))[1]
