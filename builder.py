"""
cisco-ai-builder: type what you want in plain English, get the lab configs typed into SecureCRT.

    prompt -> Ollama (fills a JSON plan) + rule checks -> Jinja2 templates -> config files
           -> SecureCRT script that types it over the console cable or telnet

The AI never writes IOS commands. It only fills in the plan (pod, devices, IPs...).
The commands come from templates/ (the Day 1 lab notes), so they are always the commands you teach.

Examples:
    python builder.py "I want to configure my cisco switches. coretaas will have an ip of 10.61.1.2 and corebaba is 10.61.1.4"
    python builder.py "..." --securecrt               (open SecureCRT on the console cable and type it)
    python builder.py "..." --securecrt --via telnet  (type it over telnet instead)
    python builder.py                                 (interactive: keeps asking for prompts)
    python builder.py --plan out/pod61/plan.json      (skip the AI, re-render a saved plan)
"""

import argparse
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

HERE = Path(__file__).resolve().parent
DEFAULTS = json.loads((HERE / "lab_defaults.json").read_text(encoding="utf-8"))
ROLES = ["coretaas", "corebaba", "cucm", "aironet"]          # build order
HOSTNAMES = {"corebaba": "COREbaba-{pod}", "coretaas": "COREtaas-{pod}",
             "cucm": "CUCM-{pod}", "aironet": "aironet-{pod}"}
SWITCHES = ("coretaas", "corebaba")
LAST_COM = HERE / "out" / "last_com_port.txt"
USB_SERIAL_RE = r"usb|ch340|ch341|prolific|pl2303|ftdi|cp210|silicon labs|uart|serial converter"

# How people actually type the device names in class.
NAME_RE = {
    "coretaas": r"core\s*-?\s*taas|\btaas\b",
    "corebaba": r"core\s*-?\s*baba|\bbaba\b|leaf\s*switch",
    "cucm":     r"\bcucm\b|call\s*-?\s*manager|\bcme\b",
    "aironet":  r"aironet|access\s*-?\s*points?|\bwap\b",
}
KEYWORD_RE = {  # words that justify building a device even without its name
    "coretaas": r"\bswitch(es)?\b|backbone",
    "corebaba": r"\bswitch(es)?\b|backbone|cameras?|cctv|\bvlans?\b|dhcp",
    "cucm":     r"ip\s*phones?|telephony|voip|ephone",
    "aironet":  r"\bwi-?fi\b|wireless|\bssid\b",
}
IP_RE = r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])"
MAC_RE = r"\b[0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\b|\b(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}\b"
OTHER_THING_RE = r"camera|cctv|phone|access\s*point|\bap\b|wi-?fi|gateway|dns|server|\bpc\b|edge|router"

# ---------------------------------------------------------------------------
# 1. What we ask the AI to fill in. Ollama forces its answer to match this schema.
# ---------------------------------------------------------------------------
NULLABLE_STR = {"type": ["string", "null"]}
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "pod": {"type": ["integer", "null"]},
        "devices": {"type": "array", "items": {"type": "string", "enum": ROLES}},
        "device_ips": {"type": "array", "items": {"type": "object", "properties": {
            "device": {"type": "string", "enum": ROLES}, "ip": {"type": "string"}},
            "required": ["device", "ip"]}},
        "cameras": {"type": "array", "items": {"type": "object", "properties": {
            "ip": {"type": "string"}, "mac": NULLABLE_STR, "port": NULLABLE_STR},
            "required": ["ip", "mac", "port"]}},
        "phones": {"type": "array", "items": {"type": "object", "properties": {
            "number": NULLABLE_STR, "mac": NULLABLE_STR, "port": NULLABLE_STR},
            "required": ["number", "mac", "port"]}},
        "access_points": {"type": "array", "items": {"type": "object", "properties": {
            "ip": NULLABLE_STR, "port": NULLABLE_STR},
            "required": ["ip", "port"]}},
        "wifi_ssid": NULLABLE_STR,
        "wifi_password": NULLABLE_STR,
        "block_cameras_from_wifi": {"type": "boolean"},
        "ssh": {"type": "boolean"},
        "ospf": {"type": "boolean"},
        "default_route": {"type": "boolean"},
    },
    "required": ["pod", "devices", "device_ips", "cameras", "phones", "access_points",
                 "wifi_ssid", "wifi_password", "block_cameras_from_wifi", "ssh", "ospf",
                 "default_route"],
}

SYSTEM_PROMPT = """You turn a network student's request into a JSON plan for a Cisco lab.
You do NOT write Cisco commands. You only fill in the JSON.

Lab facts:
- Every pod uses 10.P.x.x where P is the pod number (e.g. 10.61.1.2 -> pod 61).
- devices can be: "coretaas" (the upper core switch, usually 10.P.1.2),
  "corebaba" (the core/leaf L3 switch where cameras, phones and APs plug in, usually 10.P.1.4),
  "cucm" (CallManager Express router for IP phones, 10.P.100.8),
  "aironet" (the wireless access point).
- "my switches" / "both switches" means coretaas and corebaba.
- device_ips = the IP the user gives a device, e.g. "coretaas will have an ip of 10.61.1.2"
  -> {"device": "coretaas", "ip": "10.61.1.2"}.
- cameras = IP cameras / CCTV. phones = IP phones. access_points = wifi APs.
- port = a switch port like "Fa0/6" or "Gi0/1" only if the user said one.
- block_cameras_from_wifi = true only if the user asks to block/hide/protect the cameras from wifi users.
- ssh = true only if the user asks for SSH / secure remote access.
- ospf = true only if the user asks for OSPF.
- default_route = true only if the user asks for a default route / internet / going out to the edge.

RULES:
- Copy IP addresses, MAC addresses and phone numbers EXACTLY as the user typed them.
- NEVER invent an IP, MAC, port or number. If the user did not give it, use null or leave the list empty.
- If the user did not say the pod number, use null.

Example: "configure my cisco switches. coretaas will have an ip of 10.61.1.2 and corebaba is 10.61.1.4"
-> {"pod": 61, "devices": ["coretaas", "corebaba"],
    "device_ips": [{"device": "coretaas", "ip": "10.61.1.2"}, {"device": "corebaba", "ip": "10.61.1.4"}],
    "cameras": [], "phones": [], "access_points": [], "wifi_ssid": null, "wifi_password": null,
    "block_cameras_from_wifi": false, "ssh": false, "ospf": false, "default_route": false}
"""


def ollama_get(url, path, body=None, timeout=300):
    req = urllib.request.Request(url.rstrip("/") + path,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def pick_model(wanted, url):
    """Use the configured model if it's pulled, otherwise the best one that is."""
    names = [m["name"] for m in ollama_get(url, "/api/tags", timeout=10).get("models", [])]
    if not names:
        raise RuntimeError(f"Ollama has no models. Run: ollama pull {wanted}")
    if wanted in names or f"{wanted}:latest" in names:
        return wanted
    for family in ("qwen2.5-coder", "qwen2.5", "qwen3", "llama3.1", "llama3.2", "mistral", "gemma"):
        for n in names:
            if n.startswith(family) and not n.endswith(":1b"):
                print(f"  ({wanted} isn't pulled, using {n}. Better results: ollama pull {wanted})")
                return n
    print(f"  ({wanted} isn't pulled, using {names[0]}. Better results: ollama pull {wanted})")
    return names[0]


def ask_ollama(prompt, model, url):
    """Returns the AI's plan, or None if Ollama can't be used (the rules still work)."""
    try:
        model = pick_model(model, url)
        print(f"Asking {model} ... (first run can take a minute while it loads)")
        reply = ollama_get(url, "/api/chat", {
            "model": model,
            "stream": False,
            "format": PLAN_SCHEMA,
            "options": {"temperature": 0},
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": prompt}],
        })
        return json.loads(reply["message"]["content"])
    except (urllib.error.URLError, OSError) as e:
        print(f"  ! Can't reach Ollama at {url} ({getattr(e, 'reason', e)}). "
              "Building from the rules only. Start it with: ollama serve")
    except (RuntimeError, KeyError, ValueError) as e:
        print(f"  ! Ollama problem: {e}. Building from the rules only.")
    return None


# ---------------------------------------------------------------------------
# 2. Rules: read the prompt with plain regex. They catch what a small model misses
#    and are the only thing used with --no-ai or when Ollama is down.
# ---------------------------------------------------------------------------
def fill_placeholder(prompt, pod):
    """'10.m.1.2' + 'pod 61'  ->  '10.61.1.2'."""
    if re.search(r"\b10\.[mpk]\.", prompt, re.I):
        if not pod:
            sys.exit("You typed 10.m.x.x. Put your number in place of m (e.g. 10.61.1.2) or add 'pod 61'.")
        prompt = re.sub(r"\b10\.[mpk]\.([mpk]\.)?",
                        lambda m: f"10.{pod}." + (f"{pod}." if m.group(1) else ""), prompt, flags=re.I)
    return prompt


def read_rules(prompt):
    low = prompt.lower()
    r = {"devices": set(), "keywords": set(), "device_ips": {}, "pod": None}
    m = re.search(r"\bpod\s*#?\s*(\d{1,3})\b", low)
    if m:
        r["pod"] = int(m.group(1))
    for role, pat in NAME_RE.items():
        if re.search(pat, low):
            r["devices"].add(role)
    for role, pat in KEYWORD_RE.items():
        if re.search(pat, low):
            r["keywords"].add(role)
    if re.search(r"\b(switches|both\s+switch|backbone)\b", low):
        r["devices"].update(SWITCHES)

    # Pair each IP with the device named right before it ("coretaas will have an ip of 10.61.1.2"),
    # or right after it ("10.61.1.2 for coretaas"), as long as nothing else is named in between.
    mentions = sorted((m.start(), m.end(), role) for role, pat in NAME_RE.items()
                      for m in re.finditer(pat, low))
    ips = [(m.start(), m.end(), m.group()) for m in re.finditer(IP_RE, low)]
    taken = set()
    for i, (s, e, ip) in enumerate(ips):
        prev_end = ips[i - 1][1] if i else 0
        next_start = ips[i + 1][0] if i + 1 < len(ips) else len(low)
        before = [mm for mm in mentions if prev_end <= mm[0] and mm[1] <= s and mm[2] not in taken]
        after = [mm for mm in mentions if e <= mm[0] < next_start and mm[2] not in taken]
        for cand, gap in ([(before[-1], low[before[-1][1]:s])] if before else []) + \
                         ([(after[0], low[e:after[0][0]])] if after else []):
            if len(gap) <= 45 and not re.search(OTHER_THING_RE, gap):
                r["device_ips"][cand[2]] = ip
                taken.add(cand[2])
                break

    r["ssh"] = bool(re.search(r"\bssh\b|secure\s*shell", low))
    r["ospf"] = bool(re.search(r"\bospf\b|open\s*shortest", low))
    r["default_route"] = bool(re.search(r"default\s*route|0\.0\.0\.0|internet", low))
    r["no_uplink"] = bool(re.search(r"\b(no|without|skip)\s+(the\s+)?(trunk|ether\s*channel|lacp)", low))
    r["block"] = bool(re.search(r"(block|hide|protect|deny).{0,60}(wi-?fi|wireless)", low))
    return r


def rules_items(prompt, pod, device_ips):
    """--no-ai / Ollama-down fallback: cameras = 10.P.50.x IPs, APs = 10.P.10.x IPs."""
    used = set(device_ips.values())
    items = {"cameras": [], "access_points": [], "phones": []}
    ips = list(re.finditer(IP_RE, prompt))
    for i, m in enumerate(ips):
        ip = m.group()
        if ip in used:
            continue
        end = ips[i + 1].start() if i + 1 < len(ips) else len(prompt)
        mac = re.search(MAC_RE, prompt[m.end():end])
        if ip.startswith(f"10.{pod}.50."):
            items["cameras"].append({"ip": ip, "mac": mac.group() if mac else None, "port": None})
        elif ip.startswith(f"10.{pod}.10."):
            items["access_points"].append({"ip": ip, "port": None})
    return items


# ---------------------------------------------------------------------------
# 3. Check the AI's plan against the prompt. Anything it made up gets thrown out.
# ---------------------------------------------------------------------------
def hex_only(s):
    return re.sub(r"[^0-9a-f]", "", (s or "").lower())


def clean_mac(mac, prompt, warn, what):
    if not mac:
        return None
    h = hex_only(mac)
    if len(h) != 12:
        warn(f"{what}: '{mac}' is not a valid MAC, ignored")
        return None
    if h not in hex_only(prompt):
        warn(f"{what}: AI made up MAC '{mac}' (not in your prompt), ignored")
        return None
    return f"{h[0:4]}.{h[4:8]}.{h[8:12]}"


def clean_ip(ip, prompt, warn, what):
    if not ip:
        return None
    ip = ip.strip()
    try:
        ipaddress.IPv4Address(ip)
    except ValueError:
        warn(f"{what}: '{ip}' is not a valid IPv4 address, ignored")
        return None
    if not re.search(rf"(?<![\d.]){re.escape(ip)}(?![\d])", prompt):
        warn(f"{what}: AI made up IP '{ip}' (not in your prompt), ignored")
        return None
    return ip


def clean_port(port, warn, what):
    if not port:
        return None
    m = re.fullmatch(r"\s*(fa|fastethernet|gi|gigabitethernet)\s*(\d+/\d+(?:/\d+)?)\s*", port, re.I)
    if not m:
        warn(f"{what}: '{port}' doesn't look like a switch port, using the default port")
        return None
    return ("Fa" if m.group(1).lower().startswith("f") else "Gi") + m.group(2)


def find_pod(plan, prompt, rules, pod_override, warn):
    """--pod wins, then 'pod 61' in the prompt, then the 2nd octet of the IPs, then the AI."""
    if pod_override:
        return pod_override
    if rules["pod"]:
        return rules["pod"]
    octets = {int(o) for o in re.findall(r"(?<![\d.])10\.(\d{1,3})\.\d{1,3}\.\d{1,3}", prompt)}
    if len(octets) == 1:
        return octets.pop()
    if len(octets) > 1:
        sys.exit(f"Your IPs point at different pods {sorted(octets)}. Say which pod, e.g. 'pod 61'.")
    ai_pod = plan.get("pod")
    if ai_pod and re.search(rf"(?<!\d){ai_pod}(?!\d)", prompt):
        return ai_pod
    if ai_pod:
        warn(f"AI guessed pod {ai_pod} but it's not in your prompt, ignored")
    sys.exit("I couldn't tell your pod number. Give an IP like 10.61.1.2, add 'pod 61', or use --pod 61.")


def check_plan(plan, prompt, pod_override=None):
    """Merge the AI plan (may be {}) with the rules. Returns (clean plan, warnings)."""
    warnings = []
    warn = warnings.append
    rules = read_rules(prompt)
    pod = int(find_pod(plan, prompt, rules, pod_override, warn))
    if not 1 <= pod <= 254:
        sys.exit(f"Pod {pod} is out of range (1-254).")
    p = {"pod": pod, "cameras": [], "phones": [], "access_points": []}

    if not plan:  # no AI: rules only
        plan = {**rules_items(prompt, pod, rules["device_ips"]),
                "ssh": rules["ssh"], "ospf": rules["ospf"], "default_route": rules["default_route"],
                "block_cameras_from_wifi": rules["block"]}

    # --- device management IPs: the rules read them exactly; the AI can only add, never override
    device_ips = dict(rules["device_ips"])
    ai_ips = plan.get("device_ips") or []
    if isinstance(ai_ips, dict):  # a saved plan.json stores {device: ip}
        ai_ips = [{"device": k, "ip": v} for k, v in ai_ips.items()]
    for d in ai_ips:
        role, ip = d.get("device"), clean_ip(d.get("ip"), prompt, warn, f"{d.get('device')} IP")
        if role in ROLES and ip and role not in device_ips and ip not in device_ips.values():
            device_ips[role] = ip
    hosts = dict(DEFAULTS["host_octet"])
    for role, ip in device_ips.items():
        o = [int(x) for x in ip.split(".")]
        want_subnet = 100 if role == "cucm" else 1 if role in SWITCHES else None
        if o[0] != 10 or o[1] != pod:
            warn(f"{role} {ip} is not in pod {pod} (10.{pod}.x.x), using the default .{hosts.get(role)}")
            continue
        if want_subnet is not None and o[2] != want_subnet:
            warn(f"{role} {ip}: management IP should be 10.{pod}.{want_subnet}.x, using .{o[3]} in that subnet")
        if not 2 <= o[3] <= 254:
            warn(f"{role} {ip}: .{o[3]} can't be used, keeping .{hosts.get(role)}")
            continue
        if role in hosts:
            hosts[role] = o[3]
        if role in SWITCHES and o[3] > DEFAULTS["dhcp_exclude_up_to"]:
            warn(f"{role} .{o[3]} is inside the DHCP range (.{DEFAULTS['dhcp_exclude_up_to'] + 1}+), "
                 "a PC could get the same IP")
    if hosts["coretaas"] == hosts["corebaba"]:
        sys.exit(f"coretaas and corebaba can't both be .{hosts['coretaas']}. Give them different IPs.")
    p["device_ips"] = device_ips
    p["hosts"] = hosts

    # --- cameras, phones, APs
    for i, c in enumerate(plan.get("cameras") or [], 1):
        ip = clean_ip(c.get("ip"), prompt, warn, f"camera {i}")
        if ip and ip not in device_ips.values():
            p["cameras"].append({"ip": ip, "mac": clean_mac(c.get("mac"), prompt, warn, f"camera {ip}"),
                                 "port": clean_port(c.get("port"), warn, f"camera {ip}")})
    for i, ph in enumerate(plan.get("phones") or [], 1):
        num = (ph.get("number") or "").strip() or None
        if num and (not num.isdigit() or num not in prompt):
            warn(f"phone {i}: AI made up number '{num}', using the default")
            num = None
        p["phones"].append({"number": num, "mac": clean_mac(ph.get("mac"), prompt, warn, f"phone {i}"),
                            "port": clean_port(ph.get("port"), warn, f"phone {i}")})
    for i, ap in enumerate(plan.get("access_points") or [], 1):
        ip = clean_ip(ap.get("ip"), prompt, warn, f"AP {i}")
        if ip in device_ips.values():
            continue
        p["access_points"].append({"ip": ip, "port": clean_port(ap.get("port"), warn, f"AP {i}")})
    for c in p["cameras"]:
        if not c["ip"].startswith(f"10.{pod}.50."):
            warn(f"camera {c['ip']} is not in the camera VLAN 10.{pod}.50.0/24")
    for ap in p["access_points"]:
        if ap["ip"] and not ap["ip"].startswith(f"10.{pod}.10."):
            warn(f"AP {ap['ip']} is not in the wifi VLAN 10.{pod}.10.0/24")

    for key in ("cameras", "phones", "access_points"):
        spare = [x for x in DEFAULTS["default_ports"][key] if x not in {i["port"] for i in p[key]}]
        for item in p[key]:
            if not item["port"]:
                if not spare:
                    sys.exit(f"Too many {key} without a port. Say which port each one is on, e.g. 'on Fa0/9'.")
                item["port"] = spare.pop(0)
    used = {ph["number"] for ph in p["phones"]}
    for ph in p["phones"]:
        if not ph["number"]:
            ph["number"] = next(n for n in (f"{pod}{k}{k}" for k in range(1, 10)) if n not in used)
            used.add(ph["number"])

    p["wifi_ssid"] = plan.get("wifi_ssid") if plan.get("wifi_ssid") and plan["wifi_ssid"] in prompt else None
    p["wifi_password"] = plan.get("wifi_password") if plan.get("wifi_password") and plan["wifi_password"] in prompt else None

    # --- which devices. Small models add devices nobody asked for, so the AI's pick only
    #     counts if the prompt names the device or a word that goes with it.
    wanted = set(rules["devices"]) | set(device_ips)
    for d in plan.get("devices") or []:
        if d in ROLES and d not in wanted:
            if d in rules["keywords"]:
                wanted.add(d)
            else:
                warn(f"AI wanted to build {d} but your prompt doesn't mention it, skipped")
    if p["cameras"] or p["phones"] or p["access_points"]:
        wanted.add("corebaba")
    if p["phones"]:
        wanted.add("cucm")
    if p["access_points"] or p["wifi_ssid"]:
        wanted.add("aironet")
    p["devices"] = [d for d in ROLES if d in wanted] or list(SWITCHES)

    p["uplink"] = not rules["no_uplink"]
    p["block_cameras_from_wifi"] = bool(plan.get("block_cameras_from_wifi") or rules["block"])
    for flag in ("ssh", "ospf", "default_route"):
        p[flag] = bool(rules[flag] or plan.get(flag))
        if plan.get(flag) and not rules[flag]:
            warn(f"AI turned on {flag} though you didn't say it by name - check that's what you meant")
    return p, warnings


# ---------------------------------------------------------------------------
# 4. Templates write the actual config.
# ---------------------------------------------------------------------------
def mgmt_ip(role, plan):
    pod, hosts = plan["pod"], plan["hosts"]
    if role == "cucm":
        return f"10.{pod}.100.{hosts['cucm']}"
    return f"10.{pod}.1.{hosts[role]}"


def lab_layout(plan):
    """Every Day 1 access port, with the prompt's cameras/phones/APs filled in on top."""
    pod, ports = plan["pod"], DEFAULTS["default_ports"]

    def merge(given, defaults):
        taken = {x["port"] for x in given}
        return given + [x for x in defaults if x["port"] not in taken][:max(0, len(defaults) - len(given))]

    cams = merge(plan["cameras"], [{"ip": f"10.{pod}.50.{h}", "mac": None, "port": pt}
                                   for h, pt in zip(DEFAULTS["default_camera_hosts"], ports["cameras"])])
    for c in cams:
        c["client_id"] = None
        if c["mac"]:
            h = "01" + hex_only(c["mac"])
            c["client_id"] = ".".join(h[i:i + 4] for i in range(0, 14, 4))
    aps = merge(plan["access_points"], [{"ip": None, "port": pt} for pt in ports["access_points"]])
    phones = merge(plan["phones"], [{"number": None, "mac": None, "port": pt} for pt in ports["phones"]])
    return cams, aps, phones


def render(plan, save=True):
    """Returns a list of (role, hostname, mgmt_ip, config_text)."""
    env = Environment(loader=FileSystemLoader(HERE / "templates"), undefined=StrictUndefined,
                      trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True)
    pod = plan["pod"]
    cams, aps, phone_ports = lab_layout(plan)
    wifi = {**DEFAULTS["wifi"]}
    wifi["ssid"] = plan["wifi_ssid"] or wifi["ssid"].format(pod=pod)
    wifi["password"] = plan["wifi_password"] or wifi["password"]

    out = []
    for role in plan["devices"]:
        ctx = dict(d=DEFAULTS, pod=pod, hosts=plan["hosts"], ssh=plan["ssh"], save=save,
                   uplink=plan["uplink"], ospf=plan["ospf"], default_route=plan["default_route"],
                   cameras=cams, access_points=aps, wifi=wifi,
                   phones=phone_ports if role == "corebaba" else plan["phones"],
                   block_cameras_from_wifi=plan["block_cameras_from_wifi"],
                   octet=plan["hosts"].get(role))
        if role == "aironet":
            given = plan["access_points"] or [{"ip": f"10.{pod}.10.3", "port": None}]
            for i, ap in enumerate(given, 1):
                name = HOSTNAMES[role].format(pod=pod) + (f"-{i}" if len(given) > 1 else "")
                ip = ap["ip"] or f"10.{pod}.10.3"
                text = env.get_template("aironet.j2").render(**ctx, hostname=name, ap_ip=ip)
                out.append((role, name, ip, text))
            continue
        name = HOSTNAMES[role].format(pod=pod)
        text = env.get_template(f"{role}.j2").render(**ctx, hostname=name)
        out.append((role, name, mgmt_ip(role, plan), text))
    return out


def command_lines(text):
    """The lines a human would actually type: no blanks, no '!' comments."""
    return [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("!")]


# ---------------------------------------------------------------------------
# 5. SecureCRT: a script per device that types the config into the open tab.
# ---------------------------------------------------------------------------
def securecrt_script(configs, pod):
    """One script for one or more devices. With several, it stays in the same SecureCRT tab
    and asks you to move the console cable between devices."""
    src = (HERE / "templates" / "securecrt_typer.py").read_text(encoding="utf-8")
    passwords = list(dict.fromkeys([DEFAULTS["line_password"], DEFAULTS["enable_secret"],
                                    DEFAULTS["admin_secret"]]))
    devices = "[\n" + "".join(
        f"    {{'name': {name!r}, 'ip': {ip!r}, 'lines': [\n"
        + "".join(f"        {l!r},\n" for l in command_lines(text)) + "    ]},\n"
        for _, name, ip, text in configs) + "]"
    for key, val in {
        "__LANGUAGE__": DEFAULTS["securecrt"]["script_language"],
        "__DEVICES_REPR__": devices,
        "__USERNAME_REPR__": repr(DEFAULTS["admin_user"]), "__PASSWORDS_REPR__": repr(passwords),
        "__TITLE__": ", ".join(name for _, name, _, _ in configs), "__POD__": str(pod),
    }.items():
        src = src.replace(key, val)
    return src


def find_securecrt():
    exe = DEFAULTS["securecrt"].get("exe")
    if exe and Path(exe).is_file():
        return exe
    found = shutil.which("SecureCRT") or shutil.which("SecureCRT.exe")
    if found:
        return found
    for base in filter(None, [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                              os.environ.get("LOCALAPPDATA")]):
        for sub in ("VanDyke Software/Clients", "VanDyke Software/SecureCRT", "Programs/VanDyke Software/Clients"):
            p = Path(base) / sub / "SecureCRT.exe"
            if p.is_file():
                return str(p)
    return None


def com_ports():
    """{'COM3': 'USB-SERIAL CH340 (COM3)', ...} with USB console cables first."""
    ports = {}
    try:  # Device Manager names tell a USB console cable apart from the PC's own COM1
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_PnPEntity | Where-Object { $_.Name -match '\\(COM\\d+\\)' } "
             "| ForEach-Object { $_.Name }"],
            capture_output=True, text=True, timeout=30).stdout
        for line in out.splitlines():
            m = re.search(r"\((COM\d+)\)", line)
            if m:
                ports[m.group(1)] = line.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    try:  # registry as a backup (no friendly names)
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM")
        i = 0
        while True:
            port = winreg.EnumValue(key, i)[1]
            ports.setdefault(port, port)
            i += 1
    except (ImportError, OSError):
        pass

    def rank(item):
        port, desc = item
        usb = bool(re.search(USB_SERIAL_RE, desc, re.I))
        return (not usb, -int(re.sub(r"\D", "", port) or 0))
    return dict(sorted(ports.items(), key=rank))


def pick_com_port():
    """Always ask. Default = the port used last time, else the USB cable."""
    ports = com_ports()
    if not ports:
        return input("No COM ports found. Is the USB console cable plugged in? Type the port (e.g. COM3) "
                     "or press Enter to cancel: ").strip().upper()
    last = LAST_COM.read_text(encoding="utf-8").strip() if LAST_COM.is_file() else ""
    default = last if last in ports else next(iter(ports))
    print("\nCOM ports on this PC:")
    names = list(ports)
    for i, port in enumerate(names, 1):
        print(f"  {i}) {port:<6} {ports[port]}{'   <- default' if port == default else ''}")
    ans = input(f"Which one is the console cable? [Enter = {default}]: ").strip().upper()
    if not ans:
        return default
    if ans.isdigit() and 1 <= int(ans) <= len(names):
        return names[int(ans) - 1]
    return ans if ans.startswith("COM") else f"COM{ans}"


def run_securecrt(configs, scripts, all_script, via, com):
    exe = find_securecrt()
    if not exe:
        print("\nSecureCRT.exe not found. Set \"securecrt.exe\" in lab_defaults.json, or in SecureCRT:\n"
              "connect to the device, then Script > Run... and pick the .securecrt.py file.")
        return
    baud = str(DEFAULTS["securecrt"]["baud"])
    if via == "telnet":
        for (_, name, ip, _), script in zip(configs, scripts):
            if not ip:
                print(f"  {name}: no IP to telnet to, skipped")
                continue
            print(f"  Opening SecureCRT: telnet {ip} -> {name}")
            subprocess.Popen([exe, "/T", "/SCRIPT", str(script), "/TELNET", ip])
        print("Telnet only works once the device already has its IP + vty password "
              "(i.e. after the first console run).")
        return

    com = com.upper() if com else pick_com_port()
    if not com:
        print("No COM port, not opening SecureCRT.")
        return
    LAST_COM.parent.mkdir(exist_ok=True)
    LAST_COM.write_text(com, encoding="utf-8")
    print(f"\nConsole cable on {com} @ {baud} baud. (Close any SecureCRT tab already using {com} first.)")
    # One Serial tab for every device: the script asks you to move the cable between them.
    subprocess.Popen([exe, "/T", "/SCRIPT", str(all_script), "/SERIAL", com, "/BAUD", baud])
    names = [name for _, name, _, _ in configs]
    print(f"SecureCRT opened one {com} tab for {', '.join(names)}.")
    if len(names) > 1:
        print("Plug the cable into each device when SecureCRT asks, then click Yes. It stays in the same tab.")


def check_reachable(configs):
    print("\n=== Telnet check (TCP 23) ===")
    for _, name, ip, _ in configs:
        if not ip:
            continue
        try:
            with socket.create_connection((ip, 23), timeout=3):
                print(f"  {name:<16} {ip:<15} reachable over telnet")
        except OSError as e:
            print(f"  {name:<16} {ip:<15} NOT reachable ({e.__class__.__name__}) - is your PC in VLAN 1 "
                  f"(10.x.1.x) and the trunk up?")


# ---------------------------------------------------------------------------
# 6. Optional: push with Netmiko over SSH/telnet (the old way). Always asks first.
# ---------------------------------------------------------------------------
def push(configs, use_telnet):
    try:
        from netmiko import ConnectHandler
    except ImportError:
        sys.exit("Pushing needs Netmiko:  pip install netmiko")

    print("\nAbout to send config to:")
    for _, name, ip, _ in configs:
        print(f"  {name:<16} {ip}  ({'telnet' if use_telnet else 'ssh'})")
    if input("Type 'yes' to push: ").strip().lower() != "yes":
        print("Not pushed. The config files are saved, you can paste them by hand.")
        return

    for _, name, ip, text in configs:
        lines = [l for l in command_lines(text)
                 if l.lower() not in ("config t", "conf t", "end", "write memory")]
        dev = {"device_type": "cisco_ios_telnet" if use_telnet else "cisco_ios", "host": ip,
               "username": DEFAULTS["admin_user"],
               "password": DEFAULTS["admin_secret"] if not use_telnet else DEFAULTS["line_password"],
               "secret": DEFAULTS["enable_secret"]}
        print(f"\n--- {name} ({ip}) ---")
        try:
            with ConnectHandler(**dev) as conn:
                conn.enable()
                print(conn.send_config_set(lines, read_timeout=120))
                conn.save_config()
                print(f"OK, {name} configured and saved.")
        except Exception as e:  # keep going with the other devices
            print(f"FAILED on {name}: {e}")


# ---------------------------------------------------------------------------
def build(prompt, args):
    if args.plan:
        raw = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        prompt = raw.pop("_prompt", prompt or "")
        print("Using saved plan, skipping the AI.")
    else:
        pod_hint = args.pod or read_rules(prompt)["pod"]
        prompt = fill_placeholder(prompt, pod_hint)
        raw = {} if args.no_ai else (ask_ollama(prompt, args.model, args.ollama) or {})

    if args.show_raw:
        print("\nAI's raw answer:\n" + json.dumps(raw, indent=2))
    plan, warnings = check_plan(raw, prompt, args.pod)

    print("\n=== PLAN ===")
    print(f"  pod {plan['pod']}  ->  " + ", ".join(
        f"{HOSTNAMES[d].format(pod=plan['pod'])}"
        + (f" ({mgmt_ip(d, plan)})" if d != "aironet" else "") for d in plan["devices"]))
    extras = [k for k in ("uplink", "ssh", "ospf", "default_route", "block_cameras_from_wifi") if plan[k]]
    if extras:
        print("  with: " + ", ".join(extras).replace("uplink", "trunk+etherchannel"))
    for w in warnings:
        print(f"  ! {w}")

    configs = render(plan, save=not args.no_save)
    folder = HERE / "out" / f"pod{plan['pod']}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "plan.json").write_text(json.dumps({"_prompt": prompt, **plan}, indent=2), encoding="utf-8")
    scripts = []
    for cfg in configs:
        _, name, _, text = cfg
        (folder / f"{name}.txt").write_text(text, encoding="utf-8")
        script = folder / f"{name}.securecrt.py"
        script.write_text(securecrt_script([cfg], plan["pod"]), encoding="utf-8")
        scripts.append(script)
        if not args.quiet:
            print(f"\n{text}")
    all_script = folder / "ALL.securecrt.py"
    all_script.write_text(securecrt_script(configs, plan["pod"]), encoding="utf-8")
    print(f"\nSaved to {folder}:")
    for _, name, _, _ in configs:
        print(f"  {name}.txt  (paste it)   {name}.securecrt.py  (SecureCRT: Script > Run)")
    print(f"  ALL.securecrt.py  (every device in one console tab, move the cable when it asks)")

    if args.securecrt:
        run_securecrt(configs, scripts, all_script, args.via, args.com)
    if args.push:
        push(configs, args.telnet)
    if args.check:
        check_reachable(configs)


def main():
    ap = argparse.ArgumentParser(description="Build Cisco lab configs from a plain-English prompt using Ollama, "
                                             "and type them into SecureCRT.")
    ap.add_argument("prompt", nargs="*", help='e.g. "configure my switches, coretaas 10.61.1.2 and corebaba 10.61.1.4"')
    ap.add_argument("--pod", type=int, help="force the pod number (the m in 10.m.1.2)")
    ap.add_argument("--model", default=DEFAULTS["model"])
    ap.add_argument("--ollama", default=DEFAULTS["ollama_url"])
    ap.add_argument("--no-ai", action="store_true", help="skip Ollama, read the prompt with the rules only")
    ap.add_argument("--plan", help="re-use a saved plan.json instead of asking the AI")
    ap.add_argument("--show-raw", action="store_true", help="show the AI's answer before checks")
    ap.add_argument("--quiet", action="store_true", help="don't print the configs, only save them")
    ap.add_argument("--no-save", action="store_true", help="don't add 'write memory' at the end")
    ap.add_argument("--securecrt", action="store_true", help="open SecureCRT and type the configs in")
    ap.add_argument("--via", choices=["console", "telnet"], default="console",
                    help="how SecureCRT connects (default: console cable)")
    ap.add_argument("--com", help="console COM port, e.g. COM3 (auto-detected if there's only one)")
    ap.add_argument("--check", action="store_true", help="afterwards, test telnet (TCP 23) to each device")
    ap.add_argument("--push", action="store_true", help="send with Netmiko over SSH instead (asks first)")
    ap.add_argument("--telnet", action="store_true", help="with --push: use telnet instead of SSH")
    args = ap.parse_args()

    prompt = " ".join(args.prompt)
    if prompt or args.plan:
        build(prompt, args)
        return
    print("cisco-ai-builder. Describe what to build (blank line or Ctrl+C to quit).")
    while True:
        try:
            prompt = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not prompt:
            break
        build(prompt, args)


if __name__ == "__main__":
    main()
