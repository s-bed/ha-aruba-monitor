# Parser fixtures

These are **synthetic, fixed-width examples**, not captures from tested hardware.
Their headers and value formats follow the official
[Instant REST API guide](https://www.arubanetworks.com/techdocs/Aruba-Instant-8.x-Books/8112/Aruba-Instant-8.11.2.0-REST-API-Guide.pdf)
and [show network reference](https://arubanetworking.hpe.com/techdocs/CLI-Bank/Content/instant/shownwkprinter.htm).
All addresses, MACs, serials, names and SSIDs are invented.

The `instant_8_10_0_16/` subdirectory contains **sanitized real controller output**
captured on 2026-10-14 from Instant 8.10.0.16: 50 clients, four APs, and five SSIDs.
Original header positions, metric values, blank-name placement and table layouts
are preserved. Client/AP names, IP/MAC/IPv6 addresses, serials, SSIDs and profile
names are replaced; unrelated configuration columns are replaced with `-`.
The connection used only login, show commands and logout. Neither credentials nor
session IDs are in these fixtures.

For firmware support verification, collect read-only `show version`, `show clients`,
`show aps` and `show network` responses from the real virtual controller using the
official REST API. Review the results and add sanitized captures
with their exact firmware version and provenance. Preserve column offsets when
anonymizing; changing field widths without realigning the entire table invalidates
a fixed-width fixture. Do not commit credentials, session IDs or unreviewed captures.
