# Interactive Commercial WiFi Course — Generation Prompt

Attach these files with the prompt:

- `IX Tables.docx`
- `ACS Router Parameters - categorized.xlsx`

```text
Create a complete beginner-to-practitioner interactive course explaining the networking, fiber, WiFi, router-management, telemetry, and anomaly-detection concepts used by a commercial WiFi/ISP company.

LEARNER

Assume I am a product/data/business person with almost no networking knowledge. Explain everything in plain English before introducing technical terminology. Never use an acronym without expanding and explaining it. Use relatable analogies, but also provide the technically correct explanation.

INPUT FILES

Study the attached files carefully:

1. “IX Tables.docx”
2. “ACS Router Parameters - categorized.xlsx”

Map the course directly to the tables, columns, ACS parameters, examples, and terminology found in these files. Do not expose credentials, customer PII, phone numbers, IP addresses, or sensitive identifiers. If the files conflict with generic networking theory, clearly explain the difference.

DELIVERABLE

Create one completely self-contained `index.html` file using only vanilla HTML, CSS, and JavaScript.

Requirements:

- Works offline by double-clicking `index.html`
- No frameworks, packages, build step, CDN, or external assets
- Responsive on desktop and mobile
- Accessible keyboard navigation, visible focus states, readable contrast, semantic HTML and ARIA where appropriate
- Store course progress and quiz scores in `localStorage`
- Include a progress dashboard and reset-progress button
- Include searchable glossary and acronym explorer
- Use inline SVG/CSS diagrams
- All interactions and calculators must genuinely work
- No placeholder buttons or unfinished sections
- No excessive animation
- Verify that it opens without console errors

CORE VISUAL MODEL

Build an interactive diagram around this complete journey:

Customer device
→ SSID/WiFi
→ access point/router
→ ONT/ONU
→ drop fiber
→ passive splitter
→ OLT
→ aggregation/backhaul/core network
→ NAS/BNG
→ Internet

Show ACS managing the router from the side, RADIUS communicating with the NAS/BNG, and telemetry flowing into Snowflake, monitoring, and incident systems.

Make every component clickable. Clicking it should explain:

- What it is
- Where it physically exists
- What it does
- What can fail
- Which data detects that failure
- Which attached table or ACS parameter represents it
- What the customer experiences when it fails

Explicitly warn that NAS here means Network Access Server, not Network Attached Storage.

COURSE STRUCTURE

Module 1: The complete commercial WiFi journey

Explain how data travels from a customer’s phone to the Internet and back. Distinguish Internet service, fiber connectivity, the home router, and WiFi quality.

Module 2: Networking foundations

Cover:

- Packets and traffic
- Bandwidth versus actual throughput
- Latency, jitter and packet loss
- Ethernet and MAC addresses
- IPv4, private/public IPs, subnets and default gateways
- ARP
- DHCP
- DNS
- NAT and CGNAT
- TCP, UDP and ICMP/ping
- Routers versus switches
- VLANs and trunking
- QoS
- Practical TCP/IP and OSI models without certification-style memorization

Module 3: WiFi inside the customer premises

Cover:

- Router, modem, ONT and access-point differences
- SSID and BSSID
- 2.4 GHz, 5 GHz and 6 GHz
- Channels and channel width
- Co-channel and adjacent-channel interference
- RSSI, SNR and noise floor
- Airtime and channel utilization
- MCS and retransmissions
- Band steering
- Connected clients
- Roaming and mesh
- WPA2/WPA3
- Captive portals
- Why good fiber does not guarantee good WiFi
- Why connected-device count alone does not prove quality

Module 4: FTTH, PON and GPON

Cover:

- FTTH
- PON, GPON, EPON and XGS-PON at a useful level
- OLT, ONU, ONT and ODN
- Feeder, distribution and drop fiber
- Passive splitters and split ratios
- OLT cards, ports and PON ports
- Shared-medium behavior
- Upstream and downstream communication
- Optical wavelengths
- Optical loss and optical budget
- dBm and why negative values are normal
- RX power, TX power, bias current, voltage and transceiver temperature
- Healthy, weak, impossible, clipped and sentinel readings
- LOS and fiber cuts
- Dirty connectors, bends, bad splicing, splitters and failing optics

Include an interactive optical-power calculator and visual scale.

Module 5: ISP access and subscriber sessions

Cover:

- Access, aggregation, backhaul and core networks
- NAS and BNG/BRAS
- PPP and PPPoE
- RADIUS
- AAA: authentication, authorization and accounting
- NAS_ID
- Sessions, assigned IPs and accounting records
- Primary and secondary connections
- Public versus private addressing
- What customers experience during authentication, DNS, routing or backhaul failures

Include an interactive PPPoE/RADIUS login simulation.

Module 6: Router remote management

Cover:

- CPE
- ACS
- TR-069/CWMP
- Inform events
- Parameter trees
- Fixed attributes
- Configuration values
- Dynamic telemetry
- On-demand diagnostics
- Read-only versus remotely writable parameters
- Refresh cadence
- Firmware and configuration changes
- Reboots and uptime
- Ping, download, upload and WiFi-scan diagnostics
- Privacy and security risks of remote management

Map examples directly to the ACS workbook, especially its P1 health parameters.

Module 7: Understanding the IX data

Explain the purpose and grain of every documented IX table:

- PROD_DB.S3.ROUTER_PING_HOURLY_DATA
- PROD_DB.PUBLIC.HOURLY_DEVICE_PING_INFLUX
- PROD_DB.DBT.HOURLY_USAGE_PRORATED
- PROD_DB.DBT.DAILY_DATA_USAGE
- incidents
- incident_impacted_device
- CLEVERTAP_CUSTOMER

Also explain the DBT version of HOURLY_DEVICE_PING_INFLUX if present.

For every important column explain:

- Plain-English meaning
- Unit
- Expected range
- Whether null is allowed
- Whether absence of a row has meaning
- Join keys
- Common data-quality failures
- What business question it answers

Module 8: Ping and uptime analysis

Cover:

- Expected 12 five-minute pings per hour
- PING_COUNT
- PING_BITMAP/PING_BIT
- Received versus missed pings
- Fragmented versus continuous misses
- Uptime calculations
- Missing telemetry versus an actual outage
- Why a table containing only routers with at least one ping cannot directly show fully offline routers
- Expected active-router/device-hour scaffolds
- Device, partner and network-level outage clustering

Include an interactive 12-bit ping-bitmap decoder.

Module 9: Usage and customer-impact analysis

Cover:

- Bytes, GiB and TiB
- Download versus upload
- Sessions and session duration
- Hourly versus daily aggregation
- Duplicate aggregation and inflated usage
- Missing dimensions
- Heavy-user detection
- Customer-minutes impacted
- Why usage dropping can support outage diagnosis but cannot prove it alone

Include a byte/unit converter.

Module 10: Incidents and commercial operations

Cover:

- Incident lifecycle
- Detection, opening, escalation, recovery and closure
- Parent incident versus impacted-device rows
- Severity and size
- MTTD and MTTR
- SLA
- Stale/zombie incidents
- Reopened incidents and flapping
- Duplicate alerts and alert fatigue
- Partner reliability
- Customer impact
- NOC triage and escalation

Module 11: Anomaly detection

Teach the difference between:

- Real network anomaly
- Customer-specific problem
- Router/WiFi problem
- Data-pipeline failure
- Expected seasonality
- Bad or sentinel data

Cover simple, practical detectors first:

- Valid-range checks
- Cross-field invariants
- Missing-hour/partition checks
- Duplicate checks
- Freshness checks
- Device-specific baselines
- Median and MAD
- Rolling windows
- Rate-of-change detection
- Peer-group comparison by model, firmware or partner
- Simultaneous clustering by partner, OLT/PON or geography
- Persistent versus transient optical degradation
- Incident flapping
- False positives and threshold calibration

Do not jump directly to machine learning. Explain when simple SQL rules are sufficient and when ML might become useful.

Include an interactive anomaly lab with editable sample data and explain why each row is normal, suspicious, invalid, or inconclusive.

Module 12: Troubleshooting scenarios

Create guided decision-tree exercises for:

- Fiber cut
- Weak optical signal
- OLT/PON outage
- NAS/RADIUS authentication failure
- DNS failure
- Backhaul congestion
- WiFi interference
- Too many clients
- Bad router or power adapter
- Reboot loop
- Firmware/configuration regression
- Telemetry pipeline outage
- Stale incident that never closed

For each scenario show:

- Customer symptom
- First metric to inspect
- Next checks
- Likely root cause
- Tables/fields involved
- Correct operational response
- Common incorrect conclusion

Module 13: Business perspective

Explain how this technical data supports:

- Proactive support before customers complain
- Partner scorecards
- SLA compliance
- Reduced support cost
- Technician dispatch
- Customer communication
- Churn reduction
- Refund/service-credit decisions
- Capacity planning
- Firmware rollout monitoring

Module 14: Capstone

Provide an interactive end-to-end case where a partner has increased missed pings, weak optical readings, repeated incidents and unusual usage. Make the learner determine:

- Whether it is a network fault or data problem
- Scope of affected customers
- Most likely root cause
- Confidence level
- Immediate action
- Longer-term preventive action

INTERACTIVE FEATURES

Include:

- Clickable network journey map
- Acronym explorer
- Searchable glossary
- “Explain like I’m five” toggle
- Optical dBm calculator
- Ping-bitmap decoder
- Uptime calculator
- Bytes/GiB/TiB converter
- Incident-duration calculator
- Troubleshooting decision trees
- Data-quality checker
- Anomaly-classification exercise
- Module quizzes with explanations
- Final assessment
- Progress tracking
- Bookmarks
- Dark/light mode
- Printable cheat sheet

Every module must include:

- Learning objectives
- Plain-language explanation
- Correct technical explanation
- Diagram
- Commercial WiFi example
- Relevant table/field mapping
- Common failure modes
- “What the customer experiences”
- Three-question knowledge check
- Short summary

GLOSSARY

Include at least:

ACS, AP, ARP, BNG, BRAS, CPE, CWMP, DHCP, DNS, dB, dBm, Ethernet, FTTH, GPON, IP, jitter, latency, MAC, MCS, MTTR, MTTD, NAS, NAT, ODN, OLT, ONT, ONU, packet loss, PON, PPPoE, QoS, RADIUS, RSSI, SLA, SNR, SSID, TCP, telemetry, throughput, TR-069, UDP, VLAN, WAN, WiFi and XGS-PON.

QUALITY RULES

- Prefer operational understanding over exam preparation.
- Explain concepts in the order needed to understand the next concept.
- Clearly separate facts, heuristics and company-specific assumptions.
- Never treat missing data as proof of a network failure.
- Never treat correlation as confirmed root cause.
- Mark potentially sensitive fields.
- Cite authoritative sources in a Sources section, prioritizing Broadband Forum, ITU-T, IETF, IEEE/Wi-Fi Alliance and official vendor documentation.
- Do not copy long passages from sources.
- Do not omit difficult concepts; simplify them progressively.
- Avoid decorative complexity that does not improve learning.

Before finishing, test:

- Every navigation link
- Every interactive control
- Quiz scoring
- Progress persistence
- Reset behavior
- Calculators using known examples
- Mobile layout
- Keyboard-only navigation
- Browser console for errors

Produce the finished `index.html`, not merely an outline or mockup.
```
