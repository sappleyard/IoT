# Aircraft heard overhead

The published copy of a small static dashboard: aircraft detected by two home microphones,
compared with every ADS-B pass within 5 km, plus a 10-day forecast of daily passes from the
Heathrow weather forecast. It's rebuilt and pushed here once a day; the code that builds it lives
in github.com/sappleyard/IoT (`Aircraft-Noise/website/`). Only flight details that are public
anyway are published: callsign, airline, route, aircraft, type, whether it was heard, and the
date and hour.
